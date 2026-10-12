"""Run session: one replication driven from outside, step by step (PHASE6 G3,
D133/D134) -- the engine half of game mode.

`SimSession(layout, seed)` builds replication 0 exactly like a normal run
(`sim_runner.setup_run`), then lets the caller advance it in chunks, read the
dashboard, make the edits that are safe mid-run (a variable, an SD constant, a
resource's capacity cap within its declared maximum -- `CapacityArbiter`
combines it with a schedule and failures, PHASE6 L3 -- a Chance DECIDE's
probabilities) and answer Policy-mode decisions. Single-threaded: one thread
drives a session, and nothing else touches its env.

Decisions: every policy name used by a Policy-mode DECIDE gets its own
`PausingController`, so every such DECIDE waits for `decide()`. Any advance
stops once a request is pending and the process starts already due at that
instant have run (`_starting_now`), so one step that sends several entities
into DECIDEs leaves several requests pending (L2); they queue with
session-wide ids in creation order and the clock cannot move until every one
is answered.
`finish()` ends the player's part: later decisions take the block's fallback.

Replay (D133): every edit and decision is logged as {"t", "step", "action",
"target", "value"} (a decision adds "request_id", the request it answered),
where "step" is the number of events and time-series samples taken so far. Replaying steps the same seed to that exact step count, moves the clock
to "t" (an `advance(to=...)` between events) and makes the same call, so an
edit made mid-instant, while paused at a decision, lands between the same two
events. A snapshot changes nothing: it peeks the levels instead of
integrating them (`LevelSystem.peeking`, DEVLOG #229), so any number of
snapshots leaves the run bit-identical to a plain one.

Game mode (G4): `apply_control` maps a dashboard control onto the call above
(logged as that call, so replay is unchanged), and a `trace=` recorder lets
the UI stream the new trace events (`take_events`) into its playback Timeline.
"""
from __future__ import annotations

import itertools
import math

from simpy.events import URGENT

from nagasim.core import initial_state
from nagasim.core.blocks._common import validated_probs
from nagasim.core.blocks.decide import Decide
from nagasim.core.calendar import run_horizon
from nagasim.core.control import PausingController
from nagasim.core.expressions import compile_expression
from nagasim.core.outputs import calc_kpis, summarize_game_log
from nagasim.core.schema import (ModelValidationError, requires_run_length,
                                 prepare, fatal_problems, format_problems)
from nagasim.core.sim_runner import setup_run, step_until
from nagasim.core.timeseries import _take_dashboard_sample

__all__ = ["SimSession"]


class SimSession:
    """See the module docstring. Illegal calls raise ValueError."""

    def __init__(self, layout, seed: int = 0, trace=None, subscribers=()):
        # One prepare pass (read/migrate/validate/flatten/expand, Wave A2) shared
        # between the early checks below and the real run setup_run builds --
        # this used to run twice, once here and once again inside setup_run.
        flat, problems = prepare(layout)
        fatal = fatal_problems(problems)   # by severity, not the message text (#247)
        if fatal:
            raise ModelValidationError(format_problems(fatal))
        # The same settings `api.run_settings` resolves for a normal run.
        self.horizon = float(run_horizon(flat) or 0.0)
        if self.horizon <= 0 and requires_run_length(flat):
            raise ModelValidationError(["model never ends on its own; set a run length"])
        self.warmup = initial_state.default_warmup(flat, None)
        self.seed = int(seed)
        # Policies are found on the flattened model, as rl.SimEnv does: a
        # Policy DECIDE inside a submodel is a real decision of the run.
        policies = {(b.get("params") or {}).get("policy") or ""
                    for b in flat.get("blocks", [])
                    if b.get("type", "").upper() == "DECIDE"
                    and (b.get("params") or {}).get("mode") == "Policy"}
        ids = itertools.count()     # request ids: session-wide, in creation order
        self._controllers = {name: PausingController(ids) for name in sorted(policies)}
        self.controls = {c["name"]: c for c in (flat.get("dashboard") or {}).get("controls") or []}
        self.trace = trace
        self._taken = 0             # trace events already handed out by take_events()
        self.env, self.model = setup_run(flat, seed=self.seed, run_len=self.horizon,
                                         warmup=self.warmup, controllers=self._controllers,
                                         # *subscribers*: further listeners from the first
                                         # event on, e.g. `state.Tap` (D235).
                                         subscribers=((trace,) if trace is not None else ())
                                         + tuple(subscribers),
                                         prepared=True)
        self.ctx = self.env.ctx
        self.game_log: list[dict] = []
        self._steps = 0             # events and samples taken so far: the replay position

    @property
    def now(self) -> float:
        return self.env.now

    # -- stepping -------------------------------------------------------------
    def _pending_any(self) -> bool:
        return any(c.requests for c in self._controllers.values())

    def _starting_now(self) -> bool:
        """The next event starts a process at this instant (simpy URGENT):
        one the step that made a decision pending scheduled, e.g. the second
        entity of a CREATE batch entering the same DECIDE. Stepping on through
        them gathers every decision of that step (L2) without moving the
        clock; a resolved decision is NORMAL, so it runs after them either way."""
        queue = self.env._queue
        return bool(queue) and queue[0][0] == self.env.now and queue[0][1] == URGENT

    def _run(self, stop) -> str:
        """Step until a decision is pending, `stop()`, the horizon or a drain.
        `step_until` always gets the full run length, so HORIZON and
        `sim_horizon` never change between chunks. Neither end is final: an
        edit may schedule new events (a raised capacity restarting a queue
        that had drained)."""
        if self._pending_any():
            return "decision"

        def should_stop():
            if self._pending_any():
                if not self._starting_now():
                    return True
            elif stop():
                return True
            self._steps += 1        # step_until takes one event or sample after every False
            return False

        interrupted = step_until(self.env, self.horizon, should_stop)
        if self._pending_any():
            return "decision"
        if interrupted:
            return "stopped"
        return "drained" if self.env.peek() == math.inf else "horizon"

    def advance(self, to: float | None = None, until=None) -> str:
        """Without *to*: run until a decision is pending, the horizon or the
        model drains; returns "decision" | "horizon" | "drained".

        With *to* (capped at the horizon): process every event strictly before
        it and move the clock there (simpy's `run(until=...)`), returning
        "time", or "horizon" once the clock is at the horizon. A pending
        decision stops it early ("decision"); a drain leaves the clock at the
        drain time, as a normal run's KPIs do ("drained").

        *until* (D184) is a callable with no arguments, asked before each
        event while no decision is pending. True stops the run there and
        returns "until": that event is not processed and the clock stays where
        the last one left it. With *to* as well the first to hold wins, and
        "until" when both hold before the same event. It only decides where
        this call returns: the events, their order and `_steps` are a plain
        run's."""
        t = None
        if to is not None:
            t = float(to)
            if self.horizon > 0:
                t = min(t, self.horizon)
        hit = False

        def reached() -> bool:
            # An empty queue (inf) is a drain, not a stop at *t*: `step_until`
            # asks there too while the sampler finishes, and the clock then
            # stays at the drain time.
            return t is not None and t <= self.env.peek() < math.inf

        if until is None:
            stop = reached
        else:
            def stop() -> bool:
                nonlocal hit
                hit = bool(until())     # remembered: "stopped" does not say who stopped it
                return hit or reached()
        state = self._run(stop)
        if hit and state == "stopped":
            return "until"
        if t is None or (state != "stopped" and state != "horizon"):
            return state
        self._move_clock(t)
        return "horizon" if self.horizon > 0 and t >= self.horizon else "time"

    def _move_clock(self, t: float) -> None:
        """Put the clock at *t*, between two events (what simpy's
        run(until=t) does at its stop event). The samples due before *t* are
        taken first and counted like the ones `step_until` takes, so they see
        the state before an edit made at *t* and the step count does not
        depend on where a run was paused."""
        sampler = self.ctx.sampler
        while sampler is not None and sampler.next_tick < t:
            sampler.take()
            self._steps += 1
        if self.env.now < t:
            self.env._now = t

    def _goto(self, step: int, t: float) -> None:
        """Replay: step to exactly *step* steps, then move the clock to *t*."""
        self._run(lambda: self._steps >= step)
        if self._steps != step:
            raise ValueError(f"game_log does not match this model and seed: "
                             f"step {step} expected, reached {self._steps}")
        self._move_clock(t)

    # -- reading --------------------------------------------------------------
    def _requests(self) -> list[tuple]:
        """Every pending (id, DecisionRequest, controller), oldest first."""
        return sorted(((rid, req, c) for c in self._controllers.values()
                       for rid, req, _event in c.requests), key=lambda entry: entry[0])

    def pending(self) -> list[dict]:
        """The decisions waiting for `decide()`, oldest first; "options" are the
        target block names of the DECIDE's outgoing links, in option order."""
        return [{"id": rid, "policy": req.policy, "block": req.block, "t": req.t,
                 "entity_id": req.entity_id,
                 "options": [p.name for p in self.model.blocks[req.block].next_process]}
                for rid, req, _controller in self._requests()]

    def take_events(self) -> list:
        """The trace events recorded since the last call (G4: the live canvas
        appends them to its Timeline, in time order); [] without a trace."""
        if self.trace is None:
            return []
        events = self.trace.events[self._taken:]
        self._taken = len(self.trace.events)
        return events

    def snapshot(self) -> dict:
        """{"t", "widgets": {name: value | {label: value}}}: the dashboard now,
        with the sampler's own evaluation (no rng, a failed read is NaN).
        Read-only: the levels are peeked, never integrated (DEVLOG #229)."""
        system = self.ctx.level_system
        if system is not None:
            system.peeking = True
        try:
            rows = _take_dashboard_sample(self.ctx, self.now, self.warmup)
        finally:
            if system is not None:
                system.peeking = False
        widgets = {}
        for _t, name, label, value, _warm in rows:
            if label == "":
                widgets[name] = value
            else:
                widgets.setdefault(name, {})[label] = value
        return {"t": self.now, "widgets": widgets}

    # -- edits (D134) ---------------------------------------------------------
    def _log(self, action: str, target: str, value, **extra) -> None:
        self.game_log.append({"t": self.now, "step": self._steps, "action": action,
                              "target": target, "value": value, **extra})

    def _rates_changed(self) -> None:
        """What ASSIGN does after writing a variable: the level engine and the
        agents' conditions re-evaluate now."""
        if self.ctx.level_system is not None:
            self.ctx.level_system.trigger()
        if self.ctx.agent_system is not None:
            self.ctx.agent_system.trigger()

    def set_variable(self, name: str, value) -> None:
        if name not in self.ctx.variables:
            raise ValueError(f"unknown variable '{name}'")
        self._log("set_variable", name, value)
        self.ctx.variables[name] = value
        if self.ctx.watched:        # an event-driven HOLD reads it (O4)
            self.ctx.touch(("var", name))
        self._rates_changed()

    def set_constant(self, name: str, value) -> None:
        """Replace an SD auxiliary with a constant; AUX() reads it on every read."""
        entry = self.ctx.auxiliaries.get(name)
        if entry is None:
            raise ValueError(f"unknown auxiliary '{name}'")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"auxiliary '{name}': {value!r} is not a finite number")
        self._log("set_constant", name, number)
        self.ctx.auxiliaries[name] = (compile_expression(repr(number)), entry[1])
        self._rates_changed()

    def set_capacity(self, resource: str, units) -> None:
        """The player's cap, 0..declared capacity (L3): in force is
        min(schedule, cap) less what failures hold, the resource's
        `CapacityArbiter` deciding (lowering waits for a unit to free, the
        WAIT rule; a raise during a failure waits for the repair)."""
        res = self.ctx.resource_manager.resources.get(resource)
        if res is None:
            raise ValueError(f"unknown resource '{resource}'")
        if units != int(units) or not 0 <= int(units) <= res.capacity:
            raise ValueError(f"resource '{resource}': capacity {units} outside 0..{res.capacity}")
        self._log("set_capacity", resource, int(units))
        res.has_controller = True       # it earns a summary row, as a scheduled one does
        res.arbiter.set_cap(int(units))

    def set_probs(self, block: str, probs) -> None:
        """A Chance-mode DECIDE's branch probabilities, checked like the block's own."""
        proc = self.model.blocks.get(block)
        if not isinstance(proc, Decide) or proc.mode != "Chance":
            raise ValueError(f"'{block}' is not a Chance-mode DECIDE block")
        probs = [float(p) for p in probs]
        if any(not math.isfinite(p) or p < 0 for p in probs):
            raise ValueError(f"Decide block '{block}': probabilities must be finite and >= 0")
        validated_probs(probs, block)
        links = len(proc.next_process or [])
        if len(probs) > links or (len(probs) == links and sum(probs) < 1 - 1e-9):
            raise ValueError(f"Decide block '{block}': {len(probs)} probabilities for {links} "
                             f"links must sum to 1, or leave one link as ELSE")
        self._log("set_probs", block, probs)
        proc.probs = probs
        proc._chance_probs = proc._chance_assigned = None   # deterministic mode restarts
        proc._chance_seen = 0

    def decide(self, policy: str, option: int, request_id: int | None = None) -> None:
        """Answer *policy*'s request *request_id* (None: its oldest pending)."""
        controller = self._controllers.get(policy)
        if controller is None:
            raise ValueError(f"unknown policy '{policy}'")
        if not controller.requests:
            raise ValueError(f"policy '{policy}' has no pending decision")
        entry = controller.requests[0] if request_id is None else next(
            (e for e in controller.requests if e[0] == request_id), None)
        if entry is None:
            raise ValueError(f"policy '{policy}' has no pending request {request_id}")
        rid, req, _event = entry
        n = req.n_actions
        if option != int(option) or not 0 <= int(option) < n:
            raise ValueError(f"policy '{policy}': option {option} outside 0..{n - 1}")
        self._log("decide", policy, int(option), request_id=rid)
        controller.resolve(int(option), rid)

    def apply_control(self, name: str, value) -> None:
        """A dashboard control (G4): a slider sets its target, a value outside
        [min, max] raising; a decision control answers its policy with option
        *value*. Logged as the underlying call, so replay needs nothing new."""
        control = self.controls.get(name)
        if control is None:
            raise ValueError(f"unknown control '{name}'")
        if control.get("kind") == "decision":
            self.decide(control["policy"], value)
            return
        if not control["min"] <= float(value) <= control["max"]:
            raise ValueError(f"control '{name}': {value} outside "
                             f"[{control['min']}, {control['max']}]")
        (kind, target), = control["target"].items()
        {"capacity": self.set_capacity, "variable": self.set_variable,
         "constant": self.set_constant}[kind](target, value)

    # -- results ----------------------------------------------------------------
    def finish(self) -> dict:
        """Run to the horizon (or drain) and return the normal run's frames
        plus "game_log". Finishing ends the player's part: every decision
        still pending or met on the way takes the DECIDE's own `fallback`, as
        with no controller attached. Those are not logged -- a replay's
        `finish()` makes the same ones from the same state."""
        while self._run(lambda: False) == "decision":
            for rid, req, controller in self._requests():
                entity = self.ctx.live_entities._items.get(req.entity_id)
                controller.resolve(
                    self.model.blocks[req.block]._fallback_action(entity, req.n_actions), rid)
        run_id = 0 if self.ctx.deterministic else self.seed
        results = calc_kpis(self.env, warmup=self.warmup, run_id=run_id)
        results["game_log"] = summarize_game_log(self.game_log, run_id=run_id)
        return results

    @classmethod
    def replay(cls, layout, seed: int, game_log, trace=None) -> dict:
        """Re-play a game from its seed and `game_log`; returns `finish()`.
        *trace* records the replayed run, e.g. to animate it."""
        session = cls(layout, seed, trace=trace)
        actions = {"set_variable": session.set_variable, "set_constant": session.set_constant,
                   "set_capacity": session.set_capacity, "set_probs": session.set_probs,
                   "decide": session.decide}
        for entry in game_log:
            action = actions.get(entry["action"])
            if action is None:
                raise ValueError(f"unknown game_log action '{entry['action']}'")
            session._goto(int(entry["step"]), float(entry["t"]))
            # A decision answers the request it answered then (L2); a log
            # saved before L2 has no id and meant the oldest.
            extra = {"request_id": entry["request_id"]} if "request_id" in entry else {}
            action(entry["target"], entry["value"], **extra)
        return session.finish()
