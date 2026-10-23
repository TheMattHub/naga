from nagasim.core.graph_model import GraphModel
from nagasim.core.context import build_context
from nagasim.core.outputs import calc_kpis
from nagasim.core import blocking, costs, initial_state, timeseries
from nagasim.core.schema import read_layout
import math
import logging
import time

from simpy.events import URGENT

# Events processed at ONE timestamp before the run is declared stuck (DEVLOG #88).
# A big legitimate burst (100k entities through ten zero-time blocks) stays well under it.
STALL_EVENT_LIMIT = 5_000_000
# Time must move by more than this (relative to the clock) to count as progress.
STALL_TIME_EPSILON = 1e-9

log = logging.getLogger(__name__)

__all__ = ["initialize_sim_env", "prepare_model", "reset_statistics",
           "schedule_warmup_reset", "Pacer", "step_until", "setup_run", "run_single_simulation"]


class Pacer:
    """The paced clock (Phase 11 H, D726): `step_until` asks it to wait before
    each event, so one model second takes 1/*pace* wall seconds.

    *pace* is model seconds per wall second (1 = real time), the player's
    convention (D711); *unit_s* is the seconds in one model time unit. An event
    already behind its wall time runs at once: it is counted in `late_events`
    when more than `LATE_AFTER_S` behind, never raised (simpy.rt's strict mode
    raises instead, so it is not used). `max_lag_s` is the largest lag seen.

    Pacing changes only WHEN events run on the wall clock, never their order or
    values: a paced run's results are an unpaced run's. It adds no input to and
    no output from a running model (ARCHITECTURE section 6, rule 12). *clock*
    (monotonic) and *sleep* are injectable for tests."""

    SLICE_S = 0.1           # the longest sleep between two `should_stop` polls
    # A burst of events at one instant, and a sleep's own granularity, lag by a
    # few ms; nobody watching at the wall clock sees that.
    LATE_AFTER_S = 0.01

    def __init__(self, pace, unit_s: float = 1.0, *, clock=time.monotonic, sleep=time.sleep):
        if isinstance(pace, bool) or not isinstance(pace, (int, float)) \
                or not math.isfinite(pace) or pace <= 0:
            raise ValueError(f"pace must be a positive number of model seconds per wall "
                             f"second, not {pace!r}")
        self.pace = float(pace)
        self.wall_per_model = float(unit_s) / self.pace
        self.clock, self.sleep = clock, sleep
        self.late_events, self.max_lag_s = 0, 0.0
        self._model0, self._wall0 = 0.0, None

    def begin(self, model_t: float) -> None:
        """Anchor model time *model_t* at the wall clock's now (each replication)."""
        self._model0, self._wall0 = model_t, self.clock()

    def wait(self, t: float, should_stop=None, *, late: bool = True) -> bool:
        """Wait until model time *t* is due. Returns True when `should_stop`
        asked to stop during the wait. *late* False: *t* is not an event (the
        horizon), so being behind it is not counted."""
        due = self._wall0 + (t - self._model0) * self.wall_per_model
        now = self.clock()
        if now >= due:
            lag = now - due
            if lag > self.max_lag_s:
                self.max_lag_s = lag
            if late and lag > self.LATE_AFTER_S:
                self.late_events += 1
            return False
        while now < due:
            if should_stop is not None and should_stop():
                return True
            self.sleep(min(due - now, self.SLICE_S))
            now = self.clock()
        return False

    def summary(self) -> dict:
        return {"paced": self.pace, "late_events": self.late_events,
                "max_lag_s": round(self.max_lag_s, 6)}

def initialize_sim_env(seed: int = 0, clock: float = 0.0):
    """Build the context and return its env. `env.ctx` is the only attribute the
    engine ever sets on a SimPy environment (ARCHITECTURE D6)."""
    ctx = build_context(seed, clock)
    ctx.env.ctx = ctx
    return ctx.env

def prepare_model(env, layout_path, *, prepared: bool = False):
    """Build the runnable model. *layout_path* is a path or an already-loaded
    layout dict.

    Raises ModelValidationError from load_from_json's one validation pass
    (schema.validate_model: params, distributions, links, and graph-reachability
    together) instead of crashing mid-run (DEVLOG #33). *prepared* (Wave A2):
    the caller already ran `schema.prepare` on *layout_path* (SimSession does,
    to scan for policies before a game starts) -- skip doing it again here."""
    model = GraphModel()
    model.env = env
    model.load_from_json(env, layout_path, prepared=prepared)   # registers resources and stores first; may raise ModelValidationError
    model.add_all_blocks_from_env(env)
    return model

def reset_statistics(env, t: float) -> None:
    """Warm-up is a statistics reset, not a state reset (DEVLOG #22).

    Every collector restarts at *t* keeping its current level; the entity
    counters restart too. Entity-level warm-up filtering is done in outputs.py
    from creation_time / deletion_time.
    """
    env.ctx.live_entities.wip.reset(t)              # Avg WIP (D683)
    for res in env.ctx.resource_manager.all().values():
        res.reset_stats(t)
    for store in env.ctx.store_manager.all():
        store.reset_stats(t)
    for stat in env.ctx.user_stats.values():
        stat.reset(t)
    # PHASE4 D5: an EVENT cost is a statistic and restarts here; a FIXED cost
    # is the cost of having built the thing and is never prorated (core/costs.py).
    costs.reset_event_costs(env.ctx)
    if env.ctx.level_system is not None:            # PHASE4 D1
        env.ctx.level_system.sync(t)                # integrate up to the reset instant first
        env.ctx.level_system.reset_stats(t)
    if env.ctx.agent_system is not None:            # PHASE5 AB-1
        env.ctx.agent_system.reset_stats(t)
    for fleet in env.ctx.fleets.values():           # PHASE4 E1
        fleet.reset_stats(t)
    for charger in env.ctx.chargers.values():       # D165
        charger.reset_stats(t)
    for stats in env.ctx.tending.values():          # D169
        stats.reset_stats(t)
    for conveyor in env.ctx.conveyors.values():
        conveyor.reset_stats(t)
    for node in env.ctx.blocking_nodes.values():    # PHASE4 T5
        if node.space is not None:
            node.space.reset_stats(t)
        if getattr(node, "blocked", None) is not None:
            node.blocked.reset(t)
    for process in env.ctx.process_list:
        # duck-typed so the runner does not import the block classes;
        # counter_arrivals is left alone, it drives max_arrivals.
        if hasattr(process, "created_entities"):
            process.created_entities = 0
        if hasattr(process, "disposed_entities"):
            process.disposed_entities = 0
        if hasattr(process, "reset_queue_stats"):   # SEIZE/PROCESS, PHASE4 A4
            process.reset_queue_stats()
        if hasattr(process, "setup_count"):          # PROCESS, PHASE4 A2/T3
            process.setup_count = 0
            process.setup_time_total = 0.0
        if hasattr(process, "reset_motion_stats"):   # MOTION, D156
            process.reset_motion_stats()
    # PHASE6 W3 M2, ARCHITECTURE D113: the per-link entity counter and the
    # Roser tracker's closed periods are counts/history, not state -- dropped
    # here like every other counter above; an in-progress active period keeps
    # going, restarted at *t* (Tracker.reset).
    env.ctx.link_flow_counts.clear()
    env.ctx.block_out_counts.clear()
    if env.ctx.bottleneck_tracker is not None:
        env.ctx.bottleneck_tracker.reset(t)
    # PHASE6 W4 P2, ARCHITECTURE D120: the schedule log is a list of closed
    # holds, dropped here like every other counter above -- a hold still open
    # at warm-up is logged whole at its (later) release, the same "no
    # retroactive split" rule `setup_time_total` already follows.
    if env.ctx.schedule_log is not None:
        env.ctx.schedule_log.clear()
    log.info(f"Statistics reset at warm-up end t={t}")

def schedule_warmup_reset(env, warmup: float) -> None:
    """Arm the warm-up reset as a normal SimPy process."""
    if not warmup or warmup <= env.now:      # a clock (D232) already reset at its own instant
        return
    env.ctx.warmup = warmup

    def _reset():
        yield env.timeout(warmup - env.now)     # an absolute time: the clock may not start at 0
        reset_statistics(env, env.now)

    env.process(_reset())

def step_until(env, run_len: float = 0, should_stop=None, pacer: Pacer | None = None) -> bool:
    """Step the environment until the time limit, an empty event queue, or
    `should_stop()` returns True. Returns whether `should_stop` interrupted the run.

    *run_len* <= 0 means no limit. `ctx.sim_end_time` records the horizon the
    KPIs are computed over: the time limit when the run actually reaches it,
    else the time the queue drained (DEVLOG #32 - this replaces the old
    run_len + 1 / env.now - 1 pair).

    *pacer* (D726): wait for the wall clock before each event, and at the end
    up to a reached time limit; `should_stop` is polled during the waits. The
    only cost to an unpaced run is one `is not None` test per event.
    """
    limit = run_len if run_len and run_len > 0 else math.inf
    interrupted = False
    stalled_at, stalled_events = env.now, 0
    peek, step = env.peek, env.step
    ctx, inf = env.ctx, math.inf
    sampler = getattr(ctx, "sampler", None)     # getattr: a stub context has none
    next_tick = inf if sampler is None else sampler.next_tick
    if pacer is not None:
        pacer.begin(env.now)
    while True:
        t = peek()                  # once per event: this loop runs for every one
        if next_tick < t and (next_tick <= limit or t == inf):
            # The sampler's turn (ROADMAP 1.7; never without one, `next_tick`
            # is then inf): every event up to the tick is done. A sample is
            # asked of `should_stop` like an event, so a session pauses and
            # counts its steps the same way whether it is paused here or not;
            # it is asked only when a sample WILL be taken, so a drain is not
            # counted as a step.
            if next_tick <= limit and (t != inf or next_tick <= env.now):
                if should_stop is not None and should_stop():
                    interrupted = True
                    break
                next_tick = sampler.take()
            else:
                # Nothing is left to happen and no tick is due: it stops for
                # good rather than sample past the drain (a session edit that
                # restarts the model gets no more samples, as before).
                ctx.sampler, next_tick = None, inf
            continue
        if t == inf or t > limit:
            break
        if pacer is not None and pacer.wait(t, should_stop):
            interrupted = True
            break
        if should_stop is not None and should_stop():
            interrupted = True
            break
        step()                      # the clock is at t from here on
        # "Not advancing" includes creeping: a loop that moves time by a rounding
        # error per event (1e-13) never sits AT one timestamp, yet never gets
        # anywhere either (DEVLOG #106). Real models do not schedule events a
        # billionth of a time unit apart five million times in a row.
        # `t != stalled_at` first: most events share their instant with the one
        # before, and that skips the arithmetic.
        if t != stalled_at and t - stalled_at > STALL_TIME_EPSILON * max(1.0, abs(t)):
            stalled_at, stalled_events = t, 0
        else:
            stalled_events += 1
            if stalled_events > STALL_EVENT_LIMIT:
                raise RuntimeError(
                    f"simulation time is not advancing: more than {STALL_EVENT_LIMIT} events at "
                    f"t={env.now:g}. Look for arrivals with a zero interarrival time, or a loop "
                    f"of blocks that takes no time.")
    reached_limit = (env.peek() != math.inf) and not interrupted   # events still pending past the limit
    if pacer is not None and reached_limit:
        # The run's last stretch is model time too. Stopping here cuts only the
        # wait: every event of the run is done, so the results stand.
        pacer.wait(limit, should_stop, late=False)
    env.ctx.sim_end_time = limit if reached_limit else env.now
    env.ctx.sim_horizon = None if limit == math.inf else limit
    if not interrupted and env.peek() == math.inf and env.ctx.blocking_nodes:
        # Nothing left to happen. If a station is still blocked, that is a
        # deadlock and the KPIs would describe a quiet system (PHASE4 T5).
        blocking.deadlock_at_end(env.ctx)
    return interrupted

def setup_run(layout_path, *, seed: int, run_len: float, warmup: float,
              subscribers=(), controllers=None, prepared: bool = False):
    """Build one replication ready to step: context, model, warm-up reset,
    warm start, level/agent start, CREATE starts and the sampler. Returns
    (env, model). Shared by `run_single_simulation` and `core.session.SimSession`
    (PHASE6 G3), so a game starts exactly like a normal run.

    *controllers* (ROADMAP 3.7 A) attaches `{policy name: Controller}` to
    `ctx.controllers` before any block runs, so a Policy-mode DECIDE's first
    decision already sees it. *prepared*: see `prepare_model`."""
    # D232: the clock is where the state says, before anything is built, so every
    # controller and schedule is born at that time.
    if not prepared:
        layout_path = read_layout(layout_path)      # once: the clock, then the build
    clock = initial_state.clock_of(layout_path)
    env = initialize_sim_env(seed=seed, clock=clock)
    if controllers:
        env.ctx.controllers = dict(controllers)
    # Subscribed before prepare_model (ROADMAP 2.3 part C): resource
    # registration emits each resource's initial "capacity" resource_state
    # event right there, so a trace recorder must already be listening to
    # catch it -- the only resource_state a never-scheduled, never-failed
    # resource ever gets.
    for subscriber in subscribers:
        env.ctx.bus.subscribe(subscriber)
    # The horizon is known before anything is built (PHASE4 D5): a fixed cost's
    # amount is evaluated inside prepare_model and may read HORIZON to prorate
    # an annuity. `step_until` re-states the same value at the end of the run.
    env.ctx.sim_horizon = float(run_len) if run_len and run_len > 0 else None
    model = prepare_model(env, layout_path, prepared=prepared)
    # armed first so that, among the events stamped exactly at t=warmup, the
    # reset is processed before any arrival or seize at that instant
    schedule_warmup_reset(env, warmup)
    # Warm start (ROADMAP 3.4b) before the CREATE blocks, so every injected
    # entity is already queued/in service when the first arrival happens.
    initial_state.apply(env, model, model._raw_layout.get("initial_state") or {})
    queue = env._queue
    if env.ctx.put_back:
        # #350: the jobs in service seize, the capacity and failure controllers take
        # their units, and only then do the waiting entities join their queues.
        while queue and queue[0][0] <= env.now and (queue[0][1] == URGENT or env.ctx.settling):
            env.step()
        waiting, env.ctx.put_back = env.ctx.put_back, []
        initial_state.put_back(env, waiting)
    if clock > 0:
        # The state is what was there BEFORE the window: every injected job seizes
        # first, then the statistics restart, exactly as a warm-up reset would find
        # them. Only then do the levels, the agents and the arrivals start.
        # Not the events DUE at the clock (a service ending right then): those belong
        # to the window.
        # Every process start of this instant (URGENT: the injections join their queues)
        # and whatever the in-service jobs still need to seize.
        while queue and queue[0][0] <= clock and (queue[0][1] == URGENT or env.ctx.settling):
            env.step()
        reset_statistics(env, clock)
        env.ctx.warmup = max(float(warmup or 0.0), clock)
    # Levels and flows (PHASE4 D1): started after the warm start has set any
    # initial level, so the first rate evaluation already sees it.
    if env.ctx.level_system is not None:
        env.ctx.level_system.start(run_len)
    if env.ctx.agent_system is not None:            # PHASE5 AB-1
        env.ctx.agent_system.start(run_len)
    # #467: put back, the CREATEs start in the order their next arrivals were drawn, so
    # two due at one instant come in the run's order; two drawn in one instant, by their
    # rank (#472). Stable: a run from 0 or an older document keeps block order.
    for start in sorted(model.start_blocks(), key=lambda b: (
            -math.inf if b.scheduled_at is None else b.scheduled_at,
            -1 if b.rank is None else b.rank)):
        start.self_run()
    # ROADMAP 1.7: the sampler schedules nothing and draws nothing, so it never
    # changes another block's schedule or draws -- KPIs with sampling on are
    # bit-identical to sampling off. `step_until` takes its samples.
    # #365: the window sampled is the run's own, from the clock its state starts at; the
    # step of a run from 0 gave a short horizon far from 0 one sample or none.
    interval = timeseries.sample_interval(run_len - clock if run_len and run_len > clock else run_len,
                                          env.ctx.timeseries_points, env.ctx.timeseries_interval)
    if interval > 0:
        env.ctx.sampler = timeseries.Sampler(env, interval, warmup)
    return env, model

def run_single_simulation(layout_path, *, seed: int, run_len: float, warmup: float,
                           should_stop=None, subscribers=(), controllers=None,
                           pacer: Pacer | None = None) -> dict | None:
    """Run one replication. Returns None instead of KPIs if `should_stop` cut it
    short (M4a): a partial run has no meaningful statistics to report.
    *controllers*: see `setup_run`. *pacer*: see `step_until`."""
    env, _model = setup_run(layout_path, seed=seed, run_len=run_len, warmup=warmup,
                            subscribers=subscribers, controllers=controllers)
    if step_until(env, run_len, should_stop, pacer):
        return None
    # PHASE6 W4 P1, D119: a deterministic run's outcome does not depend on the
    # seed at all, so tagging its KPI frames with the seed would make two
    # equally-valid runs (different seed, same everything else) look like
    # different data; every deterministic run reports "Run" 0 instead. Only
    # one replication ever exists in deterministic mode (`run_settings`
    # forces it), so this can never collide with another Run in the same
    # concatenated result.
    run_id = 0 if env.ctx.deterministic else seed
    return calc_kpis(env, warmup=max(warmup, env.ctx.clock), run_id=run_id)
