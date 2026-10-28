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
the UI subscribe a recorder from the first event on (`ui/game.py` streams its own).
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
from nagasim.core.timeseries import _take_dashboard_sample, peeking

__all__ = ["SimSession"]


class SimSession:
    """One replication of a model, driven from outside in chunks.

    The session builds replication 0 as a normal run does, then lets the caller advance
    it, read the dashboard, make the edits that are safe mid-run and answer Policy-mode
    decisions. One thread drives a session. An illegal call raises ``ValueError``. The
    module docstring has the rules of pausing, logging and replay.

    Parameters
    ----------
    layout : dict or str
        The model, or the path of its file.
    seed : int, default 0
        The seed of the replication.
    trace : callable, optional
        A recorder subscribed from the first event on (the editor animates with it).
    subscribers : iterable of callable, optional
        Further listeners from the first event on.

    See Also
    --------
    nagasim.api.run_model : A whole run in one call.
    nagasim.rl.SimEnv : The same stepping as a reinforcement-learning environment.

    Examples
    --------
    >>> from nagasim import build as nb
    >>> from nagasim.core.session import SimSession
    >>> m = nb.Model(time_limit=100)
    >>> m.add_resource(name="Server", capacity=2)
    'Server'
    >>> arrive = m.create(distribution=nb.Exponential(rate=1.0))
    >>> _ = arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=1.0)) >> m.dispose()
    >>> session = SimSession(m.to_dict(), seed=0)
    >>> session.advance(to=10)
    'time'
    >>> session.now
    10.0
    >>> session.finish()["system"].shape[0] > 0
    True
    """

    def __init__(self, layout, seed: int = 0, trace=None, subscribers=()):
        # One prepare pass (read/migrate/validate/flatten/expand, Wave A2) shared
        # between the early checks below and the real run setup_run builds --
        # this used to run twice, once here and once again inside setup_run.
        flat, problems = prepare(layout)
        fatal = fatal_problems(problems)   # by severity, not the message text (#247)
        if fatal:
            raise ModelValidationError(format_problems(fatal), structured=fatal)
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
        """Run the replication forward.

        Without *to*: run until a decision is pending, the horizon or the
        model drains.

        With *to* (capped at the horizon): process every event strictly before
        it and move the clock there (simpy's `run(until=...)`). A pending
        decision stops it early; a drain leaves the clock at the drain time,
        as a normal run's KPIs do.

        Parameters
        ----------
        to : float, optional
            The model time to move the clock to.
        until : callable, optional
            Called with no arguments before each event while no decision is
            pending. True stops the run there: that event is not processed and the
            clock stays where the last one left it. With *to* as well the first to
            hold wins, and "until" when both hold before the same event. It only
            decides where this call returns: the events and their order are a plain
            run's.

        Returns
        -------
        str
            ``"time"`` (reached *to*), ``"decision"``, ``"horizon"``, ``"drained"``
            or ``"until"``.

        See Also
        --------
        nagasim.core.session.SimSession.finish : Runs to the end.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=100)
        >>> m.add_resource(name="Server", capacity=2)
        'Server'
        >>> arrive = m.create(distribution=nb.Exponential(rate=1.0))
        >>> _ = arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=1.0)) >> m.dispose()
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> session.advance(to=40)
        'time'
        >>> session.advance(to=500)
        'horizon'
        >>> session.now
        100.0
        """
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
        """The decisions waiting for `decide()`, oldest first.

        Returns
        -------
        list of dict
            One ``{"id", "policy", "block", "t", "entity_id", "options"}`` per request;
            ``options`` are the target block names of the DECIDE's outgoing links, in
            option order.

        See Also
        --------
        nagasim.core.session.SimSession.decide : Answers a request.

        Examples
        --------
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim.core.session import SimSession
        >>> path = Path(nagasim.__file__).parent / "models" / "parallel_routing_policy.json"
        >>> session = SimSession(str(path), seed=0)
        >>> session.advance()
        'decision'
        >>> request = session.pending()[0]
        >>> request["policy"], request["options"]
        ('routing', ['process_a', 'process_b'])
        """
        return [{"id": rid, "policy": req.policy, "block": req.block, "t": req.t,
                 "entity_id": req.entity_id,
                 "options": [p.name for p in self.model.blocks[req.block].next_process]}
                for rid, req, _controller in self._requests()]

    def snapshot(self) -> dict:
        """The dashboard now, with the sampler's own evaluation (no rng, a failed read is NaN).

        Read-only: the levels are peeked, never integrated, so any number of
        snapshots leaves the run bit-identical to a plain one.

        Returns
        -------
        dict
            ``{"t", "widgets": {name: value | {label: value}}}``.

        See Also
        --------
        nagasim.core.session.SimSession.advance : Moves the clock.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=100, dashboard={"widgets": [
        ...     {"name": "clock", "kind": "kpi", "expr": "TNOW"}]})
        >>> _ = m.create(distribution=nb.Constant(value=1.0)) >> m.dispose()
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> _ = session.advance(to=10)
        >>> session.snapshot()
        {'t': 10.0, 'widgets': {'clock': 10.0}}
        """
        with peeking(self.ctx):
            rows = _take_dashboard_sample(self.ctx, self.now, self.warmup)
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
        """Set a model variable now, as an ASSIGN block would.

        Parameters
        ----------
        name : str
            A variable the model declares.
        value : float or str or bool
            The new value.

        Returns
        -------
        None

        See Also
        --------
        nagasim.core.session.SimSession.set_constant : Replaces a system dynamics auxiliary.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=100)
        >>> m.add_variable(name="boost", initial=1)
        'boost'
        >>> _ = m.create(max_arrivals=1) >> m.dispose()
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> _ = session.advance(to=10)
        >>> session.set_variable("boost", 3)
        >>> session.game_log[-1]["action"], session.game_log[-1]["value"]
        ('set_variable', 3)
        """
        if name not in self.ctx.variables:
            raise ValueError(f"unknown variable '{name}'")
        self._log("set_variable", name, value)
        self.ctx.variables[name] = value
        self.ctx.wrote_variable(name)       # a HOLD (O4), a picture (D608)
        self._rates_changed()

    def set_constant(self, name: str, value) -> None:
        """Replace a system dynamics auxiliary with a constant; AUX() reads it on every read.

        Parameters
        ----------
        name : str
            An auxiliary the model declares.
        value : float
            The constant; it must be finite.

        Returns
        -------
        None

        See Also
        --------
        nagasim.core.session.SimSession.set_variable : Sets a model variable.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=10, dashboard={"widgets": [
        ...     {"name": "stock", "kind": "kpi", "expr": 'LEVEL("Stock")'}]})
        >>> m.add_level(name="Stock", initial=0)
        'Stock'
        >>> m.add_flow(name="in", to="Stock", rate='AUX("r")')
        'in'
        >>> m.add_auxiliary(name="r", expr="1")
        'r'
        >>> _ = m.create(max_arrivals=1) >> m.dispose()
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> _ = session.advance(to=4)
        >>> session.snapshot()["widgets"]["stock"]
        4.0
        >>> session.set_constant("r", 3)
        >>> _ = session.advance(to=6)
        >>> round(session.snapshot()["widgets"]["stock"], 6)
        10.0
        """
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
        """Cap a resource's capacity, from 0 to the capacity it declares.

        The capacity in force is the least of the schedule and the cap, less what
        failures hold, which the resource's `CapacityArbiter` decides: lowering waits
        for a unit to free, and a raise during a failure waits for the repair.

        Parameters
        ----------
        resource : str
            A resource the model declares.
        units : int
            The cap, a whole number from 0 to the declared capacity.

        Returns
        -------
        None

        See Also
        --------
        nagasim.core.session.SimSession.apply_control : Sets a cap through a dashboard control.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=100)
        >>> m.add_resource(name="Server", capacity=2)
        'Server'
        >>> arrive = m.create(distribution=nb.Exponential(rate=1.0))
        >>> _ = arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=1.0)) >> m.dispose()
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> _ = session.advance(to=10)
        >>> session.set_capacity("Server", 1)
        >>> try:
        ...     session.set_capacity("Server", 5)
        ... except ValueError as error:
        ...     print(error)
        resource 'Server': capacity 5 outside 0..2
        """
        res = self.ctx.resource_manager.resources.get(resource)
        if res is None:
            raise ValueError(f"unknown resource '{resource}'")
        if units != int(units) or not 0 <= int(units) <= res.capacity:
            raise ValueError(f"resource '{resource}': capacity {units} outside 0..{res.capacity}")
        self._log("set_capacity", resource, int(units))
        res.has_controller = True       # it earns a summary row, as a scheduled one does
        res.arbiter.set_cap(int(units))

    def set_probs(self, block: str, probs) -> None:
        """Set a Chance-mode DECIDE's branch probabilities, checked like the block's own.

        Parameters
        ----------
        block : str
            The name of a DECIDE block in Chance mode.
        probs : sequence of float
            One probability per branch, or one fewer than the links when the last link
            is the ELSE branch; they must be finite, not negative and sum to at most 1.

        Returns
        -------
        None

        See Also
        --------
        nagasim.core.session.SimSession.decide : Answers a Policy-mode DECIDE instead.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=8)
        >>> gate = m.decide("gate", mode="Chance", probs=[1.0])
        >>> arrive = m.create(max_arrivals=6, distribution=nb.Constant(value=1.0))
        >>> _ = arrive >> gate
        >>> _ = gate >> m.dispose("kept")
        >>> _ = gate >> m.dispose("rest")
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> _ = session.advance(to=2.5)
        >>> session.set_probs("gate", [0.0])
        >>> session.game_log[-1]["action"], session.game_log[-1]["value"]
        ('set_probs', [0.0])
        """
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
        """Answer a pending Policy-mode decision.

        Parameters
        ----------
        policy : str
            The policy the DECIDE block names.
        option : int
            The index of the outgoing link to take.
        request_id : int, optional
            The request to answer; by default the policy's oldest pending one.

        Returns
        -------
        None

        See Also
        --------
        nagasim.core.session.SimSession.pending : Lists the requests and their options.

        Examples
        --------
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim.core.session import SimSession
        >>> path = Path(nagasim.__file__).parent / "models" / "parallel_routing_policy.json"
        >>> session = SimSession(str(path), seed=0)
        >>> session.advance()
        'decision'
        >>> session.decide("routing", 0)
        >>> [entry["action"] for entry in session.game_log]
        ['decide']
        """
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
        """Use a dashboard control: a slider sets its target, a decision control answers its policy.

        A value outside the slider's ``[min, max]`` raises. The call is logged as the
        underlying call, so replay needs nothing new.

        Parameters
        ----------
        name : str
            A control of the model's ``dashboard``.
        value : float
            The slider's new value, or the option a decision control answers with.

        Returns
        -------
        None

        See Also
        --------
        nagasim.core.session.SimSession.set_variable : What a variable slider calls.
        nagasim.core.session.SimSession.set_capacity : What a capacity slider calls.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=100, dashboard={
        ...     "widgets": [{"name": "clock", "kind": "kpi", "expr": "TNOW"}],
        ...     "controls": [{"name": "boost", "kind": "slider", "target": {"variable": "boost"},
        ...                   "min": 0, "max": 10, "step": 1}]})
        >>> m.add_variable(name="boost", initial=1)
        'boost'
        >>> _ = m.create(max_arrivals=1) >> m.dispose()
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> session.apply_control("boost", 4)
        >>> session.game_log[-1]["action"], session.game_log[-1]["value"]
        ('set_variable', 4)
        """
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
        """Run to the horizon (or drain) and return the run's frames.

        Finishing ends the player's part: every decision still pending or met on the
        way takes the DECIDE's own `fallback`, as with no controller attached. Those
        are not logged, because a replay's ``finish()`` makes the same ones from the
        same state.

        Returns
        -------
        dict
            The frames of a normal run, plus ``"game_log"``, a frame of the edits and
            decisions made.

        See Also
        --------
        nagasim.core.session.SimSession.advance : Moves the clock part of the way.

        Examples
        --------
        >>> from nagasim import build as nb
        >>> from nagasim.core.session import SimSession
        >>> m = nb.Model(time_limit=100)
        >>> m.add_resource(name="Server", capacity=2)
        'Server'
        >>> arrive = m.create(distribution=nb.Exponential(rate=1.0))
        >>> _ = arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=1.0)) >> m.dispose()
        >>> session = SimSession(m.to_dict(), seed=0)
        >>> _ = session.advance(to=10)
        >>> session.set_capacity("Server", 1)
        >>> results = session.finish()
        >>> list(results["game_log"]["Action"])
        ['set_capacity']
        """
        while self._run(lambda: False) == "decision":
            for rid, req, controller in self._requests():
                entity = self.ctx.live_entities._items.get(req.entity_id)
                controller.resolve(
                    self.model.blocks[req.block]._fallback_action(entity, req.n_actions), rid)
        run_id = 0 if self.ctx.deterministic else self.seed
        results = calc_kpis(self.env, warmup=self.warmup, run_id=run_id)
        results["game_log"] = summarize_game_log(self.game_log, run_id=run_id)
        return results

    def _replay_log(self, game_log) -> None:
        """Make the calls of *game_log* again, each at its own step and time."""
        actions = {"set_variable": self.set_variable, "set_constant": self.set_constant,
                   "set_capacity": self.set_capacity, "set_probs": self.set_probs,
                   "decide": self.decide}
        for entry in game_log:
            action = actions.get(entry["action"])
            if action is None:
                raise ValueError(f"unknown game_log action '{entry['action']}'")
            self._goto(int(entry["step"]), float(entry["t"]))
            # A decision answers the request it answered then (L2); a log
            # saved before L2 has no id and meant the oldest.
            extra = {"request_id": entry["request_id"]} if "request_id" in entry else {}
            action(entry["target"], entry["value"], **extra)

    @classmethod
    def replay(cls, layout, seed: int, game_log, trace=None) -> dict:
        """Re-play a game from its seed and `game_log`; returns `finish()`.
        *trace* records the replayed run, e.g. to animate it."""
        session = cls(layout, seed, trace=trace)
        session._replay_log(game_log)
        return session.finish()

    @classmethod
    def resumed(cls, layout, seed: int, game_log, step: int, t: float, trace=None,
                subscribers=()) -> "SimSession":
        """A new session standing where a run of *layout* and *seed* stood after
        *step* steps at time *t*, *game_log* being the calls it had made by then
        (D598: a step back). Built by replay, so its state is that run's, exactly:
        continuing it gives the results the run would have given."""
        session = cls(layout, seed, trace=trace, subscribers=subscribers)
        session._replay_log(game_log)
        session._goto(int(step), float(t))
        return session
