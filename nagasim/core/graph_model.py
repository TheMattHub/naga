from nagasim.core import blocking, bottleneck, sd, timeseries
from nagasim.core.base_process import Block
from nagasim.core.blocks import Batch, Hold, Match
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.costs import build_energy_price, build_fixed_costs
from nagasim.core.levels import build_level_system
from nagasim.core.agents import build_agent_system
from nagasim.core.chargers import build_chargers
from nagasim.core.motion import build_motion_profiles, build_zones
from nagasim.core.movers import build_fleets
from nagasim.core.network import block_nodes, build_network
from nagasim.core.orders import build_orders
from nagasim.core.resource_manager import PlanGates
from nagasim.core.initial_state import schedule_offset
from nagasim.core.timeseries_inputs import build_series_table
from nagasim.core.calendar import model_calendar, run_horizon
from nagasim.core.schedules import build_schedule
from nagasim.core.schema import prepare, fatal_problems, format_problems, ModelValidationError
from nagasim.core.sequences import build_sequences
from nagasim.core.tables import build_tables
from nagasim.core.timeunits import units_of

import logging

log = logging.getLogger(__name__)

__all__ = ["GraphModel"]


class GraphModel:
    def __init__(self):
        self.blocks = {}  # name -> BaseProcess subclass
        self.env = None

    def add_all_blocks_from_env(self, env):
        """
        Register every block in ctx.process_list as a node in the graph.
        """
        for process in env.ctx.process_list:
            self.blocks[process.block.name] = process

    def link(self, *connections):
        """
        Take (from_process, to_process) or (from_process, to_process, delay)
        tuples and wire them as graph edges. *delay* (ROADMAP 1.5), a
        StochasticDistribution or None, is kept aligned with next_process by
        index in `from_process.link_delays`, read by BaseProcess.forward.
        """
        for connection in connections:
            from_process, to_process = connection[0], connection[1]
            delay = connection[2] if len(connection) > 2 else None
            if not hasattr(from_process, 'next_process') or from_process.next_process is None:
                from_process.next_process = []
            if not hasattr(from_process, 'link_delays') or from_process.link_delays is None:
                from_process.link_delays = []
            from_process.next_process.append(to_process)
            from_process.link_delays.append(delay)

    def start_blocks(self):
        return [
            b for b in self.blocks.values()
            if b.block.block_type == "CREATE"
        ]

    def load_from_json(self, env, path="layout.json", *, prepared: bool = False):
        # Clear any previous nodes
        self.blocks.clear()

        env.ctx.process_list.clear()  # defensive: start with an empty process list

        if prepared:
            # The caller already ran schema.prepare on *path* (SimSession and
            # rl.SimEnv do, to scan for policies before a game/episode starts,
            # Wave A2): running it again here would be the double work #247's
            # plan flagged, for every session/game/episode start.
            if not isinstance(path, dict):
                raise TypeError("prepared=True takes the dict schema.prepare returned, not a path")
            layout = path
        else:
            # Schema versioning (DEVLOG #33, #78): read (utf-8, #253), migrate
            # gen-1/gen-2 files to gen-3, validate the full layout (schema + graph)
            # in one pass, flatten any submodel instance (ROADMAP 2.3 part B, an
            # editing concept only -- nothing below this line, or anywhere else in
            # the engine, knows they exist) and expand SD delays (PHASE5 SD-2) into
            # hidden levels/flows -- one pass, `schema.prepare` (Wave A2), identity
            # for a plain model. Unknown-key problems only warn (the schema may
            # legitimately grow); everything else fails the load loudly.
            layout, structured = prepare(path)
            fatal = fatal_problems(structured)     # by severity, not the message text (#247)
            for message in format_problems(structured):
                log.warning(message)
            if fatal:
                raise ModelValidationError(format_problems(fatal))

        self._raw_layout = layout #copy for batch runs

        # The model's base time unit (ROADMAP 1.5): every duration without its
        # own unit is already expressed in it.
        sim_settings = layout.get("simulation", {})
        env.ctx.units = units_of(sim_settings)
        env.ctx.time_unit = sim_settings.get("time_unit", "minutes")

        # Time-series sampling settings (ROADMAP 1.7): read here, alongside
        # time_unit, so sim_runner never has to reach into the raw layout dict.
        env.ctx.timeseries_points = sim_settings.get("timeseries_points", 200)
        env.ctx.timeseries_interval = sim_settings.get("timeseries_interval")
        # Counted departures (PHASE7 7.14, D290): opt-in, so a run that names no block
        # has no subscriber and no extra series.
        # Validation refuses a name of no block; only blocks there are, are counted.
        counted, there = sim_settings.get("timeseries_counts"), {
            block.get("name") for block in layout.get("blocks") or []}
        env.ctx.counted_blocks = tuple(
            name for name in dict.fromkeys(counted if isinstance(counted, list) else ())
            if isinstance(name, str) and name in there)
        if env.ctx.counted_blocks:
            env.ctx.bus.subscribe(timeseries.count_departures(env.ctx))

        # Entity-log memory bound (ROADMAP 2.5, DEVLOG #62): same pattern as
        # timeseries_points above, a plain optional setting with a default.
        env.ctx.entity_log_limit = sim_settings.get("entity_log_limit", 100_000)
        # Per-EVENT logs off for a big study (PHASE4 T7). Every summary
        # statistic comes from the collectors and the streaming accumulators,
        # never from these logs, so what is lost is the three flat frames and
        # nothing else.
        env.ctx.record_logs = bool(sim_settings.get("record_logs", True))

        # Deterministic run mode (PHASE6 W4 P1, ARCHITECTURE D119): read once
        # here, same pattern as record_logs above. `core/base_process.py`'s
        # sample_duration, `core/blocks/decide.py`'s Chance mode and
        # `core/resource_manager.py`'s failure sampling all read this flag.
        env.ctx.deterministic = bool(sim_settings.get("deterministic", False))

        # Roser bottleneck analyser (PHASE6 W3 M2, ARCHITECTURE D113): opt-in,
        # top-level "outputs.bottleneck" (not "simulation" -- it is not a run
        # length/seed-style setting, it is a feature switch). None when off, so
        # every hook that would feed it is a cheap `is not None` away from
        # doing nothing.
        if bool(layout.get("outputs", {}).get("bottleneck", False)):
            env.ctx.bottleneck_tracker = bottleneck.Tracker()

        # Schedule log (PHASE6 W4 P2, ARCHITECTURE D120): opt-in top-level
        # "outputs.schedule", same pattern as "outputs.bottleneck" above --
        # also forced on by "simulation.deterministic" (P1, D119), a
        # deterministic run being exactly what a plan export wants a full
        # schedule for. Reading the settled layout flag here, rather than a
        # ctx field P1 would have to set first, is the single hand-off point
        # regardless of which of the two agents' code runs first.
        if (bool(layout.get("outputs", {}).get("schedule", False))
                or bool(sim_settings.get("deterministic", False))):
            env.ctx.schedule_log = []

        # Model variables start each replication at their declared initial value
        # (the context is rebuilt per replication, so this is the whole reset).
        env.ctx.variables = {v["name"]: v.get("initial", 0) for v in layout.get("variables", [])}
        # The ones an asset's picture follows (D608) start their trace at the initial value.
        env.ctx.traced_variables = frozenset(
            v["name"] for v in layout.get("variables", []) if v.get("trace"))
        for name in sorted(env.ctx.traced_variables):
            env.ctx.trace_variable(name)

        # Lookup tables (PHASE4 A1): built once here, read by TABLE() in any
        # expression through ctx.tables.
        env.ctx.tables = build_tables(layout.get("tables", []))

        # Where in its own cycle the calendar already is at model time 0
        # (PHASE4 T3): a mid-shift warm start resumes a schedule and a
        # repeating profile instead of restarting them.
        env.ctx.schedule_offset = schedule_offset(layout)

        # Real calendar dates (PHASE4 T10): None for a model that declares no
        # calendar, which is every model that existed before this. Built once
        # here; nothing downstream does datetime arithmetic at event time.
        env.ctx.calendar = model_calendar(layout)
        # The horizon a weekday schedule compiles over: the run length this
        # replication was actually given, else the model's own setting.
        calendar_horizon = (env.ctx.sim_horizon
                            if getattr(env.ctx, "sim_horizon", None)
                            else run_horizon(layout, env.ctx.calendar))

        # Time-series inputs (PHASE4 D2) before the level system, which reads
        # their change instants to schedule its own re-evaluations.
        env.ctx.series = build_series_table(layout.get("timeseries_inputs", []),
                                            env.ctx.time_unit, env.ctx.schedule_offset,
                                            env.ctx.calendar)
        # Auxiliaries (PHASE5 SD-2) before the level system, which follows AUX()
        # reads to tell a dynamic rate from a static one.
        env.ctx.auxiliaries = sd.build_auxiliaries(layout)
        # Dashboard widgets (PHASE6 G1, D132): compiled once here, same
        # pattern as auxiliaries just above; sampled by the timeseries
        # sampler at the exact same instants (core/timeseries.py).
        env.ctx.dashboard_widgets = timeseries.build_dashboard_widgets(layout)
        env.ctx.level_system = build_level_system(env, layout, env.ctx.series)
        # The storage dispatch plan (Phase 15 M8, D846) reads the levels and flows
        # just built; a model without the section never imports its module.
        env.ctx.dispatch = None
        if layout.get("dispatch"):
            from nagasim.core.dispatch import build_dispatch
            env.ctx.dispatch = build_dispatch(env, layout, env.ctx.level_system)
        # Agents (PHASE5 AB-1) after the variables and the level engine they
        # may read; initialised by `start`, beside the level system.
        env.ctx.agent_system = build_agent_system(env, layout)

        # Fixed costs (PHASE4 D5): evaluated ONCE, here, so an amount may read
        # the variables, tables and time series set up just above. Event costs
        # (RECORD kind="cost") add themselves to the same dict as they happen.
        env.ctx.cost_items = build_fixed_costs(layout, env.ctx)
        # Energy price (PHASE6 W3 M1, D112): per kWh, evaluated ONCE here too,
        # so `outputs.py`'s energy code never re-evaluates an expression.
        env.ctx.energy_price = build_energy_price(layout, env.ctx)

        # Routings (PHASE4 A2): the sequences a ROUTE block walks. `ctx.stations`
        # fills itself as each STATION block is built, below.
        env.ctx.sequences = build_sequences(layout.get("sequences", []))
        env.ctx.stations.clear()

        # Path network (PHASE4 E1): geometry only, so it is built before the
        # resources -- a mover fleet IS a resource and needs the nodes to place
        # its vehicles on. `places` maps every station/alias/node name to its
        # node, which is what DISTANCE() and the travel blocks resolve through.
        env.ctx.network = build_network(layout.get("network"), env.ctx.units)
        env.ctx.places = block_nodes(layout)
        env.ctx.conveyors.clear()

        # Orders (PHASE4 A3): each one already assigned to the CREATE block
        # that releases it; the Order objects also carry the per-order counters
        # the run fills in.
        env.ctx.orders = build_orders(layout, env.ctx.calendar)
        env.ctx.due_date_stats.clear()

        # Schedules before resources: a resource may name a capacity schedule.
        env.ctx.schedules = {s["name"]: build_schedule(s, env.ctx.time_unit,
                                                       env.ctx.schedule_offset,
                                                       env.ctx.calendar, calendar_horizon)
                             for s in layout.get("schedules", [])}

        # Entity types (shape/color, and holding_cost -- ROADMAP 2.2): loaded
        # here so DISPOSE can look up a type's holding cost rate at run time;
        # previously only the UI read this section (animation shape/color).
        env.ctx.entity_manager.load_from_json(layout.get("entities", []), env.ctx.units)

        # Resources first: Seize/Release only auto-register what the layout
        # never declared, so capacity and type must already be in place (DEVLOG #17, #20).
        env.ctx.resource_manager.load_from_json(layout.get("resources", []), env.ctx.schedules)
        env.ctx.resource_manager.load_sets_from_json(layout.get("resource_sets", []))
        # Mover fleets (PHASE4 E1) AFTER the plain resources and BEFORE the
        # failures: each fleet registers itself as a resource under its own
        # name, so a failure or a capacity schedule may name it like any other.
        env.ctx.fleets = build_fleets(env, layout, env.ctx.network, env.ctx.resource_manager)
        env.ctx.motion_profiles = build_motion_profiles(layout)     # D157
        env.ctx.zones = build_zones(layout)                         # D830
        env.ctx.chargers = build_chargers(layout, env)              # D165
        env.ctx.resource_manager.load_failures_from_json(layout.get("failures", []), env.ctx.seed,
                                                          base_unit=env.ctx.time_unit)
        # Queue discipline "Plan" (PHASE6 W4 P4, D122): the order book's
        # per-station dispatch lists, only for a model that asks for them.
        if any(r.discipline == "Plan" for r in env.ctx.resource_manager.resources.values()):
            env.ctx.plan_gates = PlanGates(env, env.ctx.orders)

        block_map = {}

        # Create blocks and wrap them in processes
        for blk in layout["blocks"]:
            name = blk["name"]
            btype = blk["type"].upper()
            # BLOCK_REGISTRY, not the BlockType enum, is the source of truth
            # (ROADMAP 2.4 B): a plugin block registers itself there under a
            # name the enum never heard of.
            if btype not in BLOCK_REGISTRY:
                raise ValueError(f"'{btype}' in the json file is not a registered block type "
                                 f"(built-in or plugin). Was it added manually?")
            # Where the block is drawn; nothing in a run reads it, and a file written by
            # hand need not say it (DEVLOG #390).
            pos = tuple(blk.get("position") or (0.0, 0.0))

            block = Block(position=pos, type=btype)
            block.name = name  # Ensure matching ID
            log.debug("Creating process for block: %s, type: %s", name, btype)
            process = self.create_process(env, block)

            # Submodel parameters bound to this block by flatten() (ROADMAP 2.3
            # part B); empty for every block of a plain model.
            process.locals = dict(blk.get("locals") or {})

            if "params" in blk and hasattr(process, "set_params_from_dict"):
                # Apply loaded params to the blocks, for sim recreation later
                process.set_params_from_dict(blk["params"])

            self.attach_stores(env, process)

            block_map[name] = process
            self.blocks[name] = process


        # Link blocks. Post-migrate, every link is {"source", "target", "delay"?}
        # (ROADMAP 1.5); a delay is sampled with the SOURCE block's own rng, so
        # it is built here as a plain StochasticDistribution, same as any
        # other duration-bearing param.
        for entry in layout["links"]:
            src, tgt = entry["source"], entry["target"]
            if src in block_map and tgt in block_map:
                delay_spec = entry.get("delay")
                delay = None
                if delay_spec:
                    delay = StochasticDistribution(
                        delay_spec.get("distribution", "Constant"), delay_spec.get("dist_params", {}),
                        time_unit=delay_spec.get("time_unit", "base"))
                self.link((block_map[src], block_map[tgt], delay))  # wire the link into the runnable graph

        # Belt merges (D854): a merge's source links to the belt's entry at the merge.
        for process in block_map.values():
            if getattr(process, "merges", None) and hasattr(process, "wire_merges"):
                process.wire_merges(block_map)

        # Blocking after service (PHASE4 T5, ARCHITECTURE D78): once every link
        # is wired, decide which blocks must hold their resource until the part
        # is accepted, and remember the loops that could deadlock. A model with
        # no finite space returns from this immediately.
        blocking.analyse(env.ctx)

    def attach_stores(self, env, process):
        """Give Hold/Batch/Match their SimPy stores (D5: the engine owns them, not
        the GUI). Called after the params are applied because Match sizes its store
        list from number_to_match."""
        name = process.block.name
        if isinstance(process, Hold):
            process.store = env.ctx.store_manager.register(f"{name}_hold")
        elif isinstance(process, Batch):
            process.store = env.ctx.store_manager.register(f"{name}_batch")
        elif isinstance(process, Match):
            process.stores = [env.ctx.store_manager.register(f"{name}_match_store_{i}")
                              for i in range(process.number_to_match)]

    def create_process(self, env, block):
        spec = BLOCK_REGISTRY.get(block.block_type)
        if spec is None:
            raise ValueError(f"Unknown block type: {block.block_type}")
        return spec.factory(env, block)
