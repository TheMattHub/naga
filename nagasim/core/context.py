"""Explicit simulation state (ARCHITECTURE D6, superseded).

Everything the engine used to monkey-patch onto `simpy.Environment` lives here.
`initialize_sim_env` still returns the env, but the only attribute it adds is
`env.ctx`; every reader goes through it.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

import simpy

from nagasim.core.entity_manager import EntityManager
from nagasim.core.events import EventBus
from nagasim.core.resource_manager import ResourceManager
from nagasim.core.sim_classes import Entity
from nagasim.core.stats import TimeWeighted, UserStat
from nagasim.core.timeunits import ModelUnits
from nagasim.core.store_manager import StoreManager

__all__ = ["SimulationContext", "build_context", "EntityIndex"]


class EntityIndex:
    """Insertion-ordered collection of entities, keyed by entity id (DEVLOG
    #61): `remove`/`in` are O(1) instead of the O(n) a plain list gives
    `list.remove`/`in`. Every reader (engine and tests) only ever uses
    list-like operations -- append, remove, `in`, `len`, iteration, `+=`,
    equality with a list -- so this is a drop-in replacement, not a new API.
    Backs both `live_entities` and `all_entities`: the latter needs O(1)
    removal too once disposed entities start being pruned (DEVLOG #62).
    """

    __slots__ = ("_items", "_env", "wip")

    def __init__(self, entities=()):
        self._items = {e.id: e for e in entities}
        self._env = None
        self.wip = None     # time-weighted len(self), once `track` is called

    def track(self, env) -> None:
        """Keep `wip`, the time-weighted count of the collection: the run's `live_entities`
        does, for the measured `Avg WIP` (D683, #761). Set after every change, so an append
        of an id already present cannot count twice."""
        self._env = env
        self.wip = TimeWeighted(env.now, len(self._items))

    def append(self, entity) -> None:
        self._items[entity.id] = entity
        if self.wip is not None:
            self.wip.set(len(self._items), self._env.now)

    def remove(self, entity) -> None:
        try:
            del self._items[entity.id]
        except KeyError:
            raise ValueError(f"{entity!r} not in this collection") from None
        if self.wip is not None:
            self.wip.set(len(self._items), self._env.now)

    def __contains__(self, entity) -> bool:
        return entity.id in self._items

    def __iter__(self):
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index):
        return list(self._items.values())[index]

    def __iadd__(self, entities):
        for e in entities:
            self.append(e)
        return self

    def __eq__(self, other):
        if isinstance(other, EntityIndex):
            return self._items.keys() == other._items.keys()
        return list(self) == other

    def __repr__(self):
        return f"EntityIndex({list(self)!r})"


def _debug_wanted() -> bool:
    """Whether any engine logger would emit a DEBUG record now. "nagasim.core"
    answers for a level set on it or above it; the loop finds one set on a
    single module's logger, which keeps filtering its own records as before."""
    if logging.getLogger("nagasim.core").isEnabledFor(logging.DEBUG):
        return True
    return any(isinstance(logger, logging.Logger) and logger.isEnabledFor(logging.DEBUG)
               for name, logger in list(logging.root.manager.loggerDict.items())
               if name.startswith("nagasim.core."))


@dataclass
class SimulationContext:
    env: simpy.Environment
    seed: int
    resource_manager: ResourceManager
    entity_manager: EntityManager
    store_manager: StoreManager
    bus: EventBus
    all_entities: EntityIndex = field(default_factory=EntityIndex)
    live_entities: EntityIndex = field(default_factory=EntityIndex)
    # Entity ids, 1, 2, ... in creation order (D195, DEVLOG #330): this run's own, so a
    # second run in the process cannot restart them under a paused one.
    entity_ids: object = field(default_factory=lambda: itertools.count(1))
    process_list: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    # Model variables, loaded from the layout's "variables" section once per
    # replication (GraphModel.load_from_json); expressions read and Assign writes.
    variables: dict = field(default_factory=dict)
    # User-defined statistics (RECORD block, ROADMAP 1.3), keyed by stat_name;
    # shared by every RECORD block writing to the same name.
    user_stats: dict[str, UserStat] = field(default_factory=dict)
    # Lump-sum and fixed costs (PHASE4 D5, `core/costs.py`), keyed by item name:
    # the "fixed_costs" section evaluated once at run start, plus whatever a
    # RECORD with kind="cost" accrues as entities pass. Empty for every model
    # that declares neither, which is what keeps their frames unchanged.
    cost_items: dict = field(default_factory=dict)
    # Piecewise-constant schedules (ROADMAP 1.4), keyed by name: CREATE reads
    # the arrival ones, the resource capacity controllers the capacity ones.
    schedules: dict = field(default_factory=dict)
    # Lookup tables (PHASE4 A1), keyed by name: built once per replication from
    # the layout's "tables" section, read by TABLE() in any expression.
    tables: dict = field(default_factory=dict)
    # Time-series inputs (PHASE4 D2), keyed by name: profiles and tariffs the
    # model READS, built once per replication from "timeseries_inputs" and
    # reached by TS() in any expression. Not to be confused with `timeseries`
    # below, which is the OUTPUT sample buffer.
    series: dict = field(default_factory=dict)
    # Levels and flows (PHASE4 D1): the one `levels.LevelSystem` of this run,
    # or None when the model declares neither -- so every existing model pays
    # nothing at all for the feature (one `is None` test per ASSIGN).
    level_system: object = None
    # Agents (PHASE5 AB-1): the one `agents.AgentSystem` of this run, or None
    # for a model without populations -- the same "pays nothing" rule.
    agent_system: object = None
    # Auxiliaries (PHASE5 SD-2): name -> (compiled expression, its submodel
    # locals or None), read by AUX(). `aux_memo` is a dict only while the level
    # engine evaluates one pass (memoised per pass), None at every other moment.
    auxiliaries: dict = field(default_factory=dict)
    aux_memo: object = None
    # Routings (PHASE4 A2): `sequences` is the normalised "sequences" section,
    # keyed by name; `stations` maps a station name to its STATION block, filled
    # by each Station as it is built. ROUTE reads both.
    sequences: dict = field(default_factory=dict)
    stations: dict = field(default_factory=dict)
    # Layout, paths and movers (PHASE4 E1, ARCHITECTURE D68): the run's
    # `network.Network` (empty for a model that declares none, so `if network`
    # is the whole cost a model without one pays), the mover fleets keyed by
    # name, and the CONVEYOR blocks keyed by block name -- each registers
    # itself as it is built, the way Station does in `stations`.
    network: object = None
    # {every name a place answers to: its node} -- a station name, its alias or
    # a node name; what DISTANCE() and the travel blocks resolve through.
    places: dict = field(default_factory=dict)
    fleets: dict = field(default_factory=dict)
    # Automation sub-project 1 (D157): {name: profile record}, read by MOTION.
    motion_profiles: dict = field(default_factory=dict)
    # Automation sub-project 2 (D165): {name: Charger}, used by battery fleets.
    chargers: dict = field(default_factory=dict)
    # Automation sub-project 3 (D169): {(station, fleet): TendingStats}, filled
    # as cobots tend; empty for a model with no arm fleet or tending station.
    tending: dict = field(default_factory=dict)
    conveyors: dict = field(default_factory=dict)
    # Blocking after service (PHASE4 T5, ARCHITECTURE D78): the blocks that own
    # a finite space or hold a resource until their part is accepted, keyed by
    # name, and the loops of them a deadlock could close. Both are EMPTY for a
    # model with no BUFFER and no `queue_capacity`, which is what keeps the
    # feature free for every other model.
    blocking_nodes: dict = field(default_factory=dict)
    blocking_cycles: list = field(default_factory=list)
    # Orders (PHASE4 A3): `orders` is {id: orders.Order}, built once per
    # replication from the "orders" section and already assigned to the CREATE
    # that releases each one; the Order objects carry the run's own counters.
    # `due_date_stats` is {e_type: stats.DueDateStats}, filled at Dispose for
    # every entity with a `due_date` -- from an order or from an ASSIGN.
    orders: dict = field(default_factory=dict)
    # D485: the ids a state says were released before the clock; None: by count.
    released_orders: set | None = None
    due_date_stats: dict = field(default_factory=dict)
    name_counter: dict = field(default_factory=dict)
    seize_blocks: list = field(default_factory=list)
    # Policy-mode DECIDE controllers (ROADMAP 3.7 A), keyed by policy name;
    # empty means every Policy-mode DECIDE falls back to its own `fallback`.
    controllers: dict = field(default_factory=dict)
    warmup: float = 0.0
    # Where the run's clock started (D232, `initial_state.clock`); 0 for every other run.
    clock: float = 0.0
    # Jobs a warm start put IN SERVICE that have not seized yet (#346): while any is
    # left, a capacity or failure controller waits its turn and the clock's reset holds.
    settling: int = 0
    # The waiting entities of that warm start, (block, entity) in order, held back until
    # the jobs in service have seized and the controllers have taken their units (#350).
    put_back: list = field(default_factory=list)
    # The order requests for a resource are made in, stamped on the entity while a
    # listener is attached (a state reads it: #349); a headless run pays one test.
    queue_seq: object = field(default_factory=itertools.count)
    sim_end_time: float | None = None
    # The horizon the run was ASKED for (`step_until`'s limit), None when the
    # caller set no time limit. `outputs.summarize_system` compares it against
    # the window the KPIs actually cover, to warn about a run that drained
    # almost immediately after its warm-up (DEVLOG 2026-09-20).
    sim_horizon: float | None = None
    # The model's base time unit (ROADMAP 1.5); every duration-bearing field
    # without its own unit is already expressed in it. Set from the layout's
    # "simulation.time_unit" in GraphModel.load_from_json.
    time_unit: str = "minutes"
    # Units spec (D159): the model's time/length units and currency, set from
    # "simulation" in GraphModel.load_from_json; every field conversion reads it.
    units: ModelUnits = ModelUnits()
    # The model's real-date calendar (PHASE4 T10, ARCHITECTURE D81) or None,
    # which is what every model without a "simulation.calendar" gets. Read only
    # at build time (weekday schedules, dated series/orders) and by the
    # presentation layer; the engine's clock stays a plain number.
    calendar: object = None
    # Time-series sampling (ROADMAP 1.7), set from "simulation.timeseries_points"
    # / "timeseries_interval" in GraphModel.load_from_json; `timeseries` is the
    # raw sample buffer core/timeseries.py appends to (plain tuples, not a
    # DataFrame -- pandas lives in outputs.py only, ARCHITECTURE 10.3).
    timeseries: list = field(default_factory=list)
    timeseries_points: int = 200
    timeseries_interval: float | None = None
    # The run's `timeseries.Sampler` (the grid `step_until` samples on), or
    # None: sampling off, or the model drained and it stopped for good.
    # `setup_run` attaches it after the build.
    sampler: object = None
    # Dashboard widgets (PHASE6 G1, schema v26): `dashboard_widgets` is built
    # once per replication from the layout's "dashboard" section (name, kind,
    # and its expressions already compiled -- see `timeseries.
    # build_dashboard_widgets`); `dashboard` is the OUTPUT sample buffer the
    # sampler appends to, same shape as `timeseries` above but keyed by widget
    # and series instead of a single series name. Both empty for a model that
    # declares no dashboard.
    dashboard_widgets: list = field(default_factory=list)
    dashboard: list = field(default_factory=list)
    # (widget, series) pairs whose NaN read is already logged (DEVLOG #228).
    dashboard_failed: set = field(default_factory=set)
    # Entity-log memory bound (ROADMAP 2.5, DEVLOG #62): per-type running
    # totals updated once, at Dispose, for every entity that ever completes,
    # exact regardless of how much of `all_entities` is actually retained --
    # see stats.EntityTypeStats and stats.account_and_retain.
    entity_stats: dict = field(default_factory=dict)
    entity_log_limit: int = 100_000
    entity_log_count: int = 0       # disposed entities retained so far
    entity_log_truncated: bool = False
    # Per-EVENT logging (PHASE4 T7, `simulation.record_logs`). False stops the
    # entity/resource/store LOGS being built at all -- the three flat frames
    # come back with their columns and no rows. Every summary statistic is
    # unaffected: they come from the time-weighted collectors and the streaming
    # per-type accumulators, never from these logs.
    record_logs: bool = True
    # Deterministic run mode (PHASE6 W4 P1, ARCHITECTURE D119): every
    # distribution draw becomes its mean (`core/distributions.py`'s `mean()`),
    # DECIDE-by-chance becomes a largest-remainder allocation
    # (`core/blocks/decide.py`) and a failure's TTF/TTR use their mean too
    # (`core/resource_manager.py`). Set from `simulation.deterministic` in
    # `GraphModel.load_from_json`, alongside `record_logs` above; ABM and SD
    # are unaffected (both already deterministic given a seed).
    deterministic: bool = False
    # Per-link entity counter (PHASE6 W3 M2, ARCHITECTURE D113): {(from, to):
    # count}, incremented in `BaseProcess.forward` and reset at warm-up. Always
    # on (a dict increment costs nothing worth gating) -- feeds
    # "material_flow_summary" and `svgcharts.sankey`.
    link_flow_counts: dict = field(default_factory=dict)
    # Departures of the blocks `simulation.timeseries_counts` names (PHASE7 7.14, D290):
    # counted by a bus subscriber only when the setting is there, sampled as
    # `block/<name>/out`, cleared with the statistics.
    counted_blocks: tuple = ()
    block_out_counts: dict = field(default_factory=dict)
    # Roser active-period tracker (PHASE6 W3 M2, ARCHITECTURE D113), built only
    # when `outputs.bottleneck` is set (`GraphModel.load_from_json`); None
    # otherwise, so every hook that would feed it is a cheap `is not None` away
    # from doing nothing.
    bottleneck_tracker: object = None
    # Schedule log (PHASE6 W4 P2, ARCHITECTURE D120): a plain list of row dicts
    # appended in `core/seizing.py`'s `release`, one per resource hold; built
    # only when `outputs.schedule` is set (or the model runs in deterministic
    # mode, which forces it on -- `GraphModel.load_from_json` is the one place
    # that reads both). None otherwise, so every hook is a cheap `is not None`
    # away from doing nothing; feeds the "schedule_log" frame and
    # `svgcharts.gantt`.
    schedule_log: object = None
    # Queue discipline "Plan" (PHASE6 W4 P4, ARCHITECTURE D122): the stations'
    # dispatch lists (`resource_manager.PlanGates`), built only when a resource
    # uses "Plan"; None otherwise, so `seizing.acquire` pays one test.
    plan_gates: object = None
    # Event-driven HOLD conditions (PHASE5 O4): {key: simpy event} for the
    # state a waiting condition reads -- ("var", name), ("res", name),
    # ("store", name), ("level", name). EMPTY unless a HOLD is waiting, so a
    # writer pays one truth test on an empty dict.
    watched: dict = field(default_factory=dict)
    # Variables an asset's picture follows (D608): flattening marks them `trace`, and
    # every write to one is a `variable_changed` trace event. Empty for most models.
    traced_variables: frozenset = frozenset()
    # An RK4 stage's clock offset (PHASE5 SD-1, D90): `levels._rk4_rates` sets
    # it to c*h while it evaluates a stage, so TNOW and TS() read the stage's
    # time; 0.0 at every other moment, and `x + 0.0 == x`.
    stage_offset: float = 0.0
    # Whether this run writes DEBUG records: the per-entity `log.debug` calls
    # are made only then, since each one reads `entity.id` and the clock before
    # the logger can refuse it. Decided once, by `build_context`, so the level
    # has to be chosen before the run starts.
    debug: bool = False

    @property
    def now(self) -> float:
        return self.env.now + self.stage_offset

    def raise_signal(self, name) -> None:
        """Succeed signal *name* (releasing every HOLD waiting on it) and
        replace it: broadcast, edge-triggered, auto-reset -- the SIGNAL block's
        rule, shared by level thresholds and an agent's `signal` action."""
        event = self.signals.get(name)
        if event is not None:
            event.succeed()
        self.signals[name] = self.env.event()

    def wake_power(self) -> None:
        """A resource's or a charger's draw just changed: re-evaluate the flow
        rates now, only when one reads POWER() (D600), so every other model keeps
        its event count."""
        system = self.level_system
        if system is not None and system.reads_power:
            system.trigger()

    def touch(self, key) -> None:
        """*key* just changed: wake every HOLD condition that reads it."""
        event = self.watched.pop(key, None)
        if event is not None:
            event.succeed()

    def wrote_variable(self, name) -> None:
        """Variable *name* was just written: wake the HOLD conditions that read it
        and, for one an asset's picture follows (D608), record its new value."""
        if self.watched:
            self.touch(("var", name))
        if name in self.traced_variables:
            self.trace_variable(name)

    def trace_variable(self, name) -> None:
        value = self.variables.get(name)
        if self.bus.has_subscribers and isinstance(value, (int, float)):
            self.bus.emit("variable_changed", block=None, t=self.env.now, resource=name,
                          value=float(value))


def build_context(seed: int = 0, clock: float = 0.0) -> SimulationContext:
    """Fresh environment plus the state that used to be patched onto it. *clock*: where
    the run's clock starts (D232)."""
    # `or 0`: with no clock the time stays SimPy's own integer 0, as every log line prints it.
    env = simpy.Environment(initial_time=clock or 0)
    ctx = SimulationContext(
        env=env,
        seed=seed,
        resource_manager=ResourceManager(env),
        entity_manager=EntityManager(load_defaults=True),
        store_manager=StoreManager(env),
        bus=EventBus(),
    )
    ctx.clock = clock
    ctx.live_entities.track(env)     # the measured Avg WIP (D683)
    ctx.debug = _debug_wanted()
    Entity.detached_ids = ctx.entity_ids     # what a bare `Entity()` draws from
    return ctx
