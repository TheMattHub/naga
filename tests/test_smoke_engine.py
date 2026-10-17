"""Characterization tests for behaviors that work correctly today. Also covers
SimulationContext (ARCHITECTURE D6), the object that replaces the attributes that
used to be patched onto the SimPy environment (formerly test_context.py)."""
import dataclasses

import numpy as np
import pandas as pd
import pytest
import simpy

from nagasim.core.base_process import Block
from nagasim.core.context import SimulationContext, build_context
from nagasim.core.distributions import StochasticDistribution, DISTRIBUTION_PARAM_HINTS, get_distribution_params
from nagasim.core.graph_model import GraphModel
from nagasim.core.sim_classes import Entity
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, run_single_simulation, step_until
from nagasim.core.blocks import Assign, Create, Decide, Delay, Dispose, Hold, Release, Seize, Signal

from conftest import run_layout
from nagasim.core.document import ModelDocument
from nagasim.core.sim_runner import prepare_model, step_until
from nagasim.core.schema import migrate
from conftest import layout as _layout, dispose as _dispose, delay as _delay
from nagasim.core.outputs import calc_kpis, consumed_entities, counted_entities
from nagasim.core.blocks import Match
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until
import time
import tracemalloc
from tools.bench import _load

BROKEN_DISTRIBUTIONS = {"FixedSchedule", "CustomExpression"}   # BoundedNormal fixed (DEVLOG #4)


def _default_params(dist_type):
    """Build a params dict from get_distribution_params() names using generic
    non-degenerate defaults (1 for scalars, small lists for values/probs/schedule)."""
    defaults = {}
    for name in get_distribution_params(dist_type):
        if name in ("values", "schedule"):
            defaults[name] = [1, 2, 3]
        elif name == "probs":
            defaults[name] = [0.2, 0.3, 0.5]
        else:
            defaults[name] = 1
    return defaults


def test_create_delay_dispose_completes_all_entities(env):
    """Create(Constant 1, max_arrivals=5) -> Delay(Constant 1) -> Dispose finishes all 5 entities."""
    create = Create(env, Block(position=(0, 0), type="CREATE", name="create1"),
                     time_between_arrivals=1, max_arrivals=5)
    delay = Delay(env, Block(position=(1, 0), type="DELAY", name="delay1"), delay=1)
    dispose = Dispose(env, Block(position=(2, 0), type="DISPOSE", name="dispose1"))
    create.next_process = [delay]
    delay.next_process = [dispose]

    create.self_run()
    env.run(until=20)

    assert dispose.disposed_entities == 5
    assert env.ctx.live_entities == []


def test_delay_with_normal_negative_samples_truncates_instead_of_raising(env):
    """Normal(mean=0.1, sd=1) regularly samples below 0. BaseProcess.sample_duration
    truncates a negative draw to 0 instead of handing SimPy a negative timeout
    (which raises ValueError), and counts every truncation."""
    delay = Delay(env, Block(position=(0, 0), type="DELAY", name="delay1"),
                  delay=StochasticDistribution("Normal", {"mean": 0.1, "stddev": 1}))
    dispose = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="dispose1"))
    delay.next_process = [dispose]

    for _ in range(200):
        entity = Entity()
        entity.creation_time = env.now
        env.ctx.all_entities.append(entity)
        env.ctx.live_entities.append(entity)
        delay.self_run(entity)
    env.run(until=50)

    assert dispose.disposed_entities == 200   # no SimPy ValueError interrupted the run
    assert delay.negative_samples > 0          # about half of 200 draws should be negative


def test_seize_release_round_trip_frees_resource(env):
    """Seize -> Release returns the resource to count 0 and logs one seize per entity."""
    resource = env.ctx.resource_manager.register("R1", capacity=1)
    create = Create(env, Block(position=(0, 0), type="CREATE", name="create1"),
                     time_between_arrivals=1, max_arrivals=3)
    seize = Seize(env, Block(position=(1, 0), type="SEIZE", name="seize1"), resource=resource, delay=0)
    release = Release(env, Block(position=(2, 0), type="RELEASE", name="release1"), resource=resource)
    dispose = Dispose(env, Block(position=(3, 0), type="DISPOSE", name="dispose1"))
    create.next_process = [seize]
    seize.next_process = [release]
    release.next_process = [dispose]

    create.self_run()
    env.run(until=20)

    assert resource.count == 0
    assert resource.seize_count == 3
    assert dispose.disposed_entities == 3


def test_decide_chance_full_probability_routes_to_output0(env):
    """Decide in Chance mode with probs=[1.0] always routes to next_process[0]."""
    decide = Decide(env, Block(position=(0, 0), type="DECIDE", name="decide1"), probs=[1.0], mode="Chance")
    dispose0 = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="dispose0"))
    dispose1 = Dispose(env, Block(position=(2, 0), type="DISPOSE", name="dispose1"))
    decide.next_process = [dispose0, dispose1]

    entity = Entity()
    entity.creation_time = 0
    env.ctx.all_entities.append(entity)
    env.ctx.live_entities.append(entity)

    decide.self_run(entity)
    env.run(until=5)

    assert dispose0.disposed_entities == 1
    assert dispose1.disposed_entities == 0


def test_assign_sets_entity_type(env):
    """Assign evaluates each expression and writes it to its target."""
    assign = Assign(env, Block(position=(0, 0), type="ASSIGN", name="assign1"),
                    assignments=[{"target": "entity.e_type", "expr": "'B'"}])
    dispose = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="dispose1"))
    assign.next_process = [dispose]

    entity = Entity()
    entity.creation_time = 0
    env.ctx.all_entities.append(entity)
    env.ctx.live_entities.append(entity)

    assign.self_run(entity)

    assert entity.e_type == "B"


def test_signal_releases_hold_in_signal_mode(env):
    """A Signal succeeding its event releases an entity waiting in a Hold(Signal)."""
    store = env.ctx.store_manager.register("hold1_hold")
    hold = Hold(env, Block(position=(0, 0), type="HOLD", name="hold1"), store=store,
                mode="Signal", signal_name="sig1")
    dispose = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="dispose1"))
    hold.next_process = [dispose]

    signal = Signal(env, Block(position=(2, 0), type="SIGNAL", name="signal1"), signal_name="sig1")
    dispose2 = Dispose(env, Block(position=(3, 0), type="DISPOSE", name="dispose2"))
    signal.next_process = [dispose2]

    waiting_entity = Entity()
    waiting_entity.creation_time = 0
    trigger_entity = Entity()
    trigger_entity.creation_time = 0
    env.ctx.all_entities += [waiting_entity, trigger_entity]
    env.ctx.live_entities += [waiting_entity, trigger_entity]

    hold.self_run(waiting_entity)
    signal.self_run(trigger_entity)
    env.run(until=5)

    assert dispose.disposed_entities == 1


def test_every_distribution_samples_without_raising():
    """Every catalogued distribution except the known-broken ones samples a
    number. One loop over the ~29 families instead of 29 parametrized tests:
    sampling once is microseconds, pytest's per-test overhead is not."""
    failures = []
    for dist_type in sorted(set(DISTRIBUTION_PARAM_HINTS) - BROKEN_DISTRIBUTIONS):
        try:
            value = StochasticDistribution(dist_type, _default_params(dist_type)) \
                .sample(np.random.default_rng(0))
        except Exception as exc:                     # noqa: BLE001 - report, don't stop
            failures.append(f"{dist_type}: raised {exc!r}")
            continue
        if not isinstance(value, (int, float)):
            failures.append(f"{dist_type}: sampled {value!r} ({type(value).__name__})")
    assert not failures, "\n  ".join([""] + failures)


def test_a_release_naming_an_undeclared_resource_registers_it_at_capacity_one(env, caplog):
    """DEVLOG #17: the layout's resources are registered first, so a RELEASE
    that still names an unknown resource means the model never declared it --
    the block registers it at capacity 1 and says so, instead of crashing.
    (The Seize/Release M/M/1 oracle used to be the only thing reaching this
    fallback; it is asserted directly now that the oracle is gone.)"""
    release = Release(env, Block(position=(0, 0), type="RELEASE", name="release_1"))
    with caplog.at_level("WARNING"):
        release.set_params_from_dict({"resource_name": "Undeclared"})

    assert release.resource.name == "Undeclared"
    assert release.resource.capacity == 1
    assert any("Undeclared" in record.message for record in caplog.records)


def test_gen3_json_loads_with_correct_counts(env, simple_model_path):
    """A gen-3 model JSON loads through GraphModel.load_from_json with matching counts."""
    graph = GraphModel()
    graph.env = env
    graph.load_from_json(env, simple_model_path)

    assert len(graph.blocks) == 3
    assert sum(len(b.next_process) for b in graph.blocks.values()) == 2  # 2 links wired
    assert graph.blocks["create_1"].max_arrivals == 5


def test_run_single_simulation_returns_all_kpi_frames(simple_model_path):
    """run_single_simulation (headless) returns every KPI DataFrame."""
    result = run_single_simulation(simple_model_path, seed=0, run_len=20, warmup=0)

    expected_keys = {"system", "entity_summary", "entity_log", "resource_summary",
                      "resource_log", "store_summary", "store_log", "user_summary",
                      "order_summary", "due_date_summary", "queue_summary",
                      "motion_summary",
                      "level_summary", "flow_summary", "conversion_summary",
                      "component_summary", "cost_summary", "energy_summary",
                      "fleet_summary", "segment_summary", "conveyor_summary", "buffer_summary",
                      "charging_summary", "battery_summary", "tending_summary",
                      "material_flow_summary", "bottleneck_summary", "schedule_log",
                      "agent_summary", "agent_attributes", "agent_attribute_log", "timeseries",
                      "dashboard"}
    assert set(result.keys()) == expected_keys
    for df in result.values():
        assert isinstance(df, pd.DataFrame)


# ------------------------------------------------------- SimulationContext (D6)

def test_build_context_fields():
    ctx = build_context(7)
    assert isinstance(ctx, SimulationContext)
    # The fields this test reads; a new context field needs no edit here.
    assert {"env", "seed", "all_entities", "live_entities", "process_list", "signals",
            "name_counter", "seize_blocks", "warmup", "sim_end_time"
            } <= {f.name for f in dataclasses.fields(ctx)}
    assert ctx.seed == 7
    assert (ctx.all_entities, ctx.live_entities, ctx.process_list) == ([], [], [])
    assert (ctx.signals, ctx.name_counter, ctx.seize_blocks) == ({}, {}, [])
    assert ctx.warmup == 0.0 and ctx.sim_end_time is None
    assert ctx.now == ctx.env.now


def test_initialize_sim_env_attaches_ctx():
    env = initialize_sim_env(seed=3)
    assert env.ctx.env is env
    assert env.ctx.seed == 3
    assert env.ctx.now == env.now


def test_context_populated_by_a_run(simple_model_path):
    env = initialize_sim_env(seed=0)
    run_layout(env, simple_model_path, 50)

    ctx = env.ctx
    assert len(ctx.all_entities) == 5          # Create max_arrivals=5
    assert ctx.live_entities == []             # all disposed within the horizon
    assert ctx.process_list                    # blocks registered themselves
    assert ctx.sim_end_time is not None


def test_env_has_only_ctx(simple_model_path):
    """The engine patches exactly one attribute onto simpy.Environment."""
    env = initialize_sim_env(seed=0)
    prepare_model(env, simple_model_path)
    step_until(env, 5)

    baseline = {a for a in dir(simpy.Environment()) if not a.startswith("_")}
    assert {a for a in dir(env) if not a.startswith("_")} - baseline == {"ctx"}


def test_process_list_has_each_block_once(simple_model_path):
    """DEVLOG #79: load_from_json used to append every process a second time."""
    env = initialize_sim_env(seed=0)
    model = prepare_model(env, simple_model_path)
    assert len(env.ctx.process_list) == len(model.blocks)


# ======================= link delays ======================================
def _create(name="create_1", max_arrivals=1):
    return {"name": name, "description": name, "type": "CREATE", "position": [0, 0],
            "params": {"e_type": "DEFAULT", "arrival_mode": "Interarrival", "schedule_name": "",
                      "distribution": "Constant", "dist_params": {"value": 1.0},
                      "entities_per_arrival": 1, "max_arrivals": max_arrivals, "start_time": 0}}


def _delay_spec(value):
    return {"distribution": "Constant", "dist_params": {"value": value}}


# ---------------------------------------------------------------- engine
def test_transfer_delay_adds_exactly_to_throughput_time(env):
    layout = _layout(
        [_create(), _delay(value=3.0), _dispose()],
        [{"source": "create_1", "target": "delay_1"},
         {"source": "delay_1", "target": "dispose_1", "delay": _delay_spec(5.0)}],
    )
    model = prepare_model(env, layout)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 100)

    entity = env.ctx.all_entities[0]
    assert entity.deletion_time - entity.creation_time == pytest.approx(3.0 + 5.0)


def test_entity_stays_in_live_entities_during_transit(env):
    layout = _layout(
        [_create(), _delay(value=3.0), _dispose()],
        [{"source": "create_1", "target": "delay_1"},
         {"source": "delay_1", "target": "dispose_1", "delay": _delay_spec(5.0)}],
    )
    model = prepare_model(env, layout)
    for start in model.start_blocks():
        start.self_run()

    step_until(env, 4.0)   # the delay block released it at t=3; still in transit until t=8
    entity = env.ctx.all_entities[0]
    assert entity in env.ctx.live_entities
    assert entity.deletion_time is None

    step_until(env, 100)
    assert entity not in env.ctx.live_entities
    assert entity.deletion_time == pytest.approx(8.0)


def test_zero_delay_link_behaves_exactly_as_before(env):
    """No `delay` key on a link -- today's instant transfer, unchanged."""
    layout = _layout(
        [_create(), _delay(value=3.0), _dispose()],
        [{"source": "create_1", "target": "delay_1"},
         {"source": "delay_1", "target": "dispose_1"}],
    )
    model = prepare_model(env, layout)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 100)

    entity = env.ctx.all_entities[0]
    assert entity.deletion_time - entity.creation_time == pytest.approx(3.0)


def test_branch_order_unaffected_by_a_delay_on_one_branch(env):
    """A DECIDE with a delay on only its second branch still routes by index:
    the delay changes when the entity arrives, never which link it takes."""
    decide = {"name": "decide_1", "description": "decide_1", "type": "DECIDE", "position": [1, 0],
             "params": {"mode": "Chance", "probs": [1.0], "conditions": []}}
    layout = _layout(
        [_create(max_arrivals=1), decide, _dispose("dispose_a"), _dispose("dispose_b")],
        [{"source": "create_1", "target": "decide_1"},
         {"source": "decide_1", "target": "dispose_a"},                       # branch 0: probs[0]=1.0
         {"source": "decide_1", "target": "dispose_b", "delay": _delay_spec(9.0)}],  # branch 1: ELSE
    )
    model = prepare_model(env, layout)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 100)

    entity = env.ctx.all_entities[0]
    # probs sum to 1.0: every draw takes branch 0 (no delay), never branch 1.
    assert entity.deletion_time == pytest.approx(0.0)


# ---------------------------------------------------------------- document API
def test_document_set_and_clear_link_delay_with_undo():
    doc = ModelDocument()
    a = doc.add_block("DELAY", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    depth = len(doc._undo_stack)

    assert doc.link_delay(a, b) is None

    spec = {"distribution": "Constant", "dist_params": {"value": 5.0}, "time_unit": "base"}
    doc.set_link_delay(a, b, spec)
    assert doc.link_delay(a, b) == spec
    assert len(doc._undo_stack) == depth + 1

    doc.set_link_delay(a, b, None)
    assert doc.link_delay(a, b) is None
    assert len(doc._undo_stack) == depth + 2

    doc.undo()
    assert doc.link_delay(a, b) == spec
    doc.undo()
    assert doc.link_delay(a, b) is None


def test_rename_block_keeps_the_link_delay():
    doc = ModelDocument()
    a = doc.add_block("DELAY", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    spec = {"distribution": "Constant", "dist_params": {"value": 5.0}, "time_unit": "base"}
    doc.set_link_delay(a, b, spec)

    doc.rename_block(a, "renamed")

    assert doc.link_delay("renamed", b) == spec
    assert doc.link_delay(a, b) is None


def test_remove_block_drops_its_link_delay():
    doc = ModelDocument()
    a = doc.add_block("DELAY", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    doc.set_link_delay(a, b, {"distribution": "Constant", "dist_params": {"value": 5.0}})

    doc.remove_blocks([b])

    assert doc.link_delay(a, b) is None and doc.links == []


def test_remove_link_drops_its_delay():
    doc = ModelDocument()
    a = doc.add_block("DELAY", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    doc.set_link_delay(a, b, {"distribution": "Constant", "dist_params": {"value": 5.0}})

    doc.remove_link(a, b)

    assert doc.link_delay(a, b) is None and doc.links == []


def test_layout_round_trip_of_link_objects():
    doc = ModelDocument()
    a = doc.add_block("DELAY", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    spec = {"distribution": "Constant", "dist_params": {"value": 5.0}, "time_unit": "base"}
    doc.set_link_delay(a, b, spec)

    layout = doc.to_layout()
    assert layout["links"] == [{"source": a, "target": b, "delay": spec}]

    reloaded = ModelDocument(migrate(layout))
    assert reloaded.link_delay(a, b) == {**spec, "time_unit": "minutes"}   # v28 fills "base"
    assert reloaded.links == [(a, b)]


def test_v7_pair_link_migrates_to_an_object_with_no_delay():
    migrated = migrate({"schema_version": 7, "blocks": [], "links": [["a", "b"]]})
    assert migrated["links"] == [{"source": "a", "target": "b"}]


# ======================= Batch / Separate / Match =========================
class Recorder:
    """Terminal stub that keeps the order entities reached it."""

    def __init__(self, name="rec"):
        self.order = []
        self.block = Block(position=(9, 9), type="DISPOSE", name=name)
        self.next_process = []

    def self_run(self, entity):
        self.order.append(entity)


def build_and_run(make_model, blocks, links, run_len=50):
    """Run a headless model and hand back the env for state inspection."""
    path = make_model(blocks, links)
    env = initialize_sim_env(seed=0)
    model = prepare_model(env, path)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, run_len)
    return env


def create_block(name, per_arrival, max_arrivals, e_type="DEFAULT", interval=1.0):
    return {"name": name, "description": "c", "type": "CREATE", "position": [0, 0],
            "params": {"e_type": e_type, "distribution": "Constant",
                       "dist_params": {"value": interval},
                       "entities_per_arrival": per_arrival,
                       "max_arrivals": max_arrivals, "start_time": 0}}


def batch_block(name, size=2):
    return {"name": name, "description": "b", "type": "BATCH", "position": [1, 0],
            "params": {"batch_size": size, "batched_entity_type": "BATCH"}}


def dispose_block(name="dispose_1"):
    return {"name": name, "description": "d", "type": "DISPOSE", "position": [3, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}}


# --------------------------------------------------------------------- Batch

def test_simultaneous_arrivals_form_exact_disjoint_batches(make_model):
    """DEVLOG #29: 10 entities arriving in 2 bursts (5 at a time, batch_size 2).
    Every entity must end up in exactly one batch and every batch must be exact."""
    env = build_and_run(
        make_model,
        [create_block("create_1", per_arrival=5, max_arrivals=2), batch_block("batch_1", 2),
         dispose_block()],
        [["create_1", "batch_1"], ["batch_1", "dispose_1"]],
    )

    batches = [e for e in env.ctx.all_entities if e.batch_elems]
    members = [m for b in batches for m in b.batch_elems]

    assert len(batches) == 5
    assert all(len(b.batch_elems) == 2 for b in batches)
    assert len(members) == len(set(id(m) for m in members)) == 10  # disjoint, nothing lost
    assert not env.ctx.store_manager.get("batch_1_batch").items


def test_max_wait_releases_a_partial_batch_and_a_full_one_cancels_the_timer(make_model):
    """PHASE4 B6, exact to the instant. batch_size 3, max_wait 10, arrivals
    every 4 (t=0, 4, 8, 12, ...), 5 of them:

    * the first three fill at t=8, BEFORE the timer armed at t=0 expires -- so
      that timer must release nothing at t=10 (tie rule: a full batch wins);
    * the arrivals at 12 and 16 start a new group whose timer, armed at 12,
      fires at 22 and takes the 2 that are waiting.
    """
    batch = {"name": "batch_1", "description": "b", "type": "BATCH", "position": [1, 0],
             "params": {"batch_size": 3, "batched_entity_type": "BATCH", "max_wait": "10"}}
    env = build_and_run(
        make_model,
        [create_block("create_1", per_arrival=1, max_arrivals=5, interval=4.0), batch,
         dispose_block()],
        [["create_1", "batch_1"], ["batch_1", "dispose_1"]], run_len=30)

    batches = [e for e in env.ctx.all_entities if e.batch_elems]
    assert [len(b.batch_elems) for b in batches] == [3, 2]
    assert [b.creation_time for b in batches] == [8.0, 22.0]
    assert not env.ctx.store_manager.get("batch_1_batch").items

    # The TIE itself: arrivals every 5 make the batch full at t=10, exactly
    # when the timer armed at t=0 expires. One full batch, no partial one, and
    # nothing released twice at that timestamp.
    batch["params"] = {**batch["params"], "batch_size": 3}
    env = build_and_run(
        make_model,
        [create_block("create_1", per_arrival=1, max_arrivals=3, interval=5.0), batch,
         dispose_block()],
        [["create_1", "batch_1"], ["batch_1", "dispose_1"]], run_len=30)
    tie = [e for e in env.ctx.all_entities if e.batch_elems]
    assert [(len(b.batch_elems), b.creation_time) for b in tie] == [(3, 10.0)]


def test_leftover_entities_stay_in_the_store_and_in_wip(make_model):
    """Fewer than batch_size at run end: still stored, still live, still 'created'."""
    env = build_and_run(
        make_model,
        [create_block("create_1", per_arrival=3, max_arrivals=1), batch_block("batch_1", 2),
         dispose_block()],
        [["create_1", "batch_1"], ["batch_1", "dispose_1"]],
    )

    leftovers = env.ctx.store_manager.get("batch_1_batch").items
    assert len(leftovers) == 1
    assert leftovers[0] in env.ctx.live_entities
    assert leftovers[0].in_batch is None


def test_permanent_batch_members_are_neither_live_nor_completed(make_model):
    """Create -> Batch(2) -> Dispose: the members are consumed by the batch, so
    they are not live, not completed, and not counted as in system.
    DEVLOG #29: the batch entity represents its members in WIP (live_entities)."""
    env = build_and_run(
        make_model,
        [create_block("create_1", per_arrival=2, max_arrivals=2), batch_block("batch_1", 2),
         dispose_block()],
        [["create_1", "batch_1"], ["batch_1", "dispose_1"]],
    )

    batches = [e for e in env.ctx.all_entities if e.batch_elems]
    members = [m for b in batches for m in b.batch_elems]

    assert len(batches) == 2 and len(members) == 4
    assert env.ctx.live_entities == []
    assert all(m.in_batch is not None for m in members)

    created, completed = counted_entities(env, warmup=0)
    assert set(map(id, created)) == set(map(id, batches))       # members do not count as created
    assert set(map(id, completed)) == set(map(id, batches))     # nor as completed
    assert len(created) - len(completed) == 0                   # nothing left in system
    assert len(consumed_entities(env, warmup=0)) == 4

    entity_log = calc_kpis(env, warmup=0, run_id=0)["entity_log"]
    assert len(entity_log) == 6                                 # members stay visible in the log
    assert entity_log["In Batch"].notna().sum() == 4


def test_separate_returns_children_to_wip_and_conserves_entities(make_model):
    """Create -> Batch(2) -> Separate -> Dispose: everything created completes."""
    env = build_and_run(
        make_model,
        [create_block("create_1", per_arrival=2, max_arrivals=2), batch_block("batch_1", 2),
         {"name": "separate_1", "description": "s", "type": "SEPARATE", "position": [2, 0],
          "params": {"mode": "Retain original entity values", "attrs": []}},
         dispose_block()],
        [["create_1", "batch_1"], ["batch_1", "separate_1"], ["separate_1", "dispose_1"]],
    )

    members = [e for e in env.ctx.all_entities if not e.batch_elems]
    assert len(members) == 4
    assert all(m.in_batch is None for m in members)
    assert env.ctx.live_entities == []

    created, completed = counted_entities(env, warmup=0)
    assert consumed_entities(env, warmup=0) == []
    assert len(created) == len(completed) == 6        # 4 members + 2 batch parents
    assert set(map(id, members)) <= set(map(id, completed))


# --------------------------------------------------------------------- Match

def make_match(env, number_to_match, *, batch_option=False, name="match_1"):
    match = Match(env, Block(position=(0, 0), type="MATCH", name=name),
                  number_to_match=number_to_match, batch_option=batch_option,
                  batched_entity_type="BATCH")
    # what GraphModel.attach_stores does when a model is loaded
    match.stores = [env.ctx.store_manager.register(f"{name}_match_store_{i}")
                    for i in range(number_to_match)]
    recorder = Recorder()
    match.next_process = [recorder]
    return match, recorder


def feed(env, match, types, spacing=1.0):
    """Send one entity per type, one time unit apart, in the given order."""
    arrived = []

    def driver():
        for e_type in types:
            entity = Entity()
            entity.creation_time = env.now
            entity.e_type = e_type
            env.ctx.all_entities.append(entity)
            env.ctx.live_entities.append(entity)
            arrived.append(entity)
            match.self_run(entity)
            yield env.timeout(spacing)

    env.process(driver())
    return arrived


def stored_entities(match):
    return [ent for store in match.stores for _, ent in store.items]


def test_match_never_loses_an_entity():
    """DEVLOG #27: stored + forwarded == arrived, whatever the arrival order.
    Each case needs its own env, so the loop builds one per case."""
    from nagasim.core.sim_runner import initialize_sim_env

    for order, number_to_match, expected_forwarded in [
        (["A", "A", "B"], 2, 2),
        (["B", "A"], 2, 2),
        (["A", "B", "C"], 2, 2),
        (["A", "B", "C"], 3, 3),
        (["A", "A", "B"], 3, 0),
    ]:
        env = initialize_sim_env(seed=0)
        match, recorder = make_match(env, number_to_match)
        arrived = feed(env, match, order)
        env.run(until=20)

        case = (order, number_to_match)
        assert len(recorder.order) == expected_forwarded, case
        assert len(recorder.order) + len(stored_entities(match)) == len(arrived), case
        assert (set(map(id, recorder.order)) | set(map(id, stored_entities(match)))
                == set(map(id, arrived))), case


def test_match_is_fifo_within_a_type(env):
    """A,A,B with number_to_match=2 matches the FIRST A, leaving the second waiting."""
    match, recorder = make_match(env, 2)
    arrived = feed(env, match, ["A", "A", "B"])
    env.run(until=20)
    a1, a2, b = arrived

    assert recorder.order == [a1, b]        # oldest waiting type first, oldest A of that type
    assert stored_entities(match) == [a2]


def test_match_batch_option_absorbs_its_members(env):
    """batch_option follows the Batch rule: the batch represents its members."""
    match, recorder = make_match(env, 2, batch_option=True)
    arrived = feed(env, match, ["A", "B"])
    env.run(until=20)
    a, b = arrived

    assert len(recorder.order) == 1
    batch = recorder.order[0]
    assert batch.batch_elems == [a, b]
    assert batch.e_type == "BATCH"
    assert batch.in_batch is None   # DEVLOG #27: the batch entity itself is not consumed by anything
    assert batch in env.ctx.live_entities
    assert a not in env.ctx.live_entities and b not in env.ctx.live_entities
    assert a.in_batch == b.in_batch == batch.id


def test_match_uses_the_type_store_map(env):
    """A mapped type always lands in its own store."""
    match, _ = make_match(env, 2)
    match.type_store_map = {"B": 1}
    feed(env, match, ["B"])
    env.run(until=5)

    assert [t for t, _ in match.stores[1].items] == ["B"]
    assert not match.stores[0].items


# ======================= CI performance guard =============================
ENTITIES = 30_000
LOG_LIMIT = 5_000

# Measured ~11,500 entities/s and ~13 MB peak (tracemalloc) for this case on
# this machine after the ROADMAP 2.5 optimisations (DEVLOG #62).
THROUGHPUT_FLOOR = 4000.0   # entities/s
MEMORY_BOUND_MB = 40.0


@pytest.mark.slow
def test_throughput_floor_and_bounded_peak_memory():
    """DEVLOG #62: past `entity_log_limit` the run must stop carrying one
    Entity object per arrival forever, so peak memory stays flat instead of
    growing with the entity count -- and the engine must still be fast."""
    layout = _load("single_server.json")
    layout["blocks"][0]["params"]["max_arrivals"] = ENTITIES
    layout["simulation"]["replications"] = 1
    layout["simulation"]["entity_log_limit"] = LOG_LIMIT

    env = initialize_sim_env(seed=0)
    model = prepare_model(env, layout)
    for start in model.start_blocks():
        start.self_run()

    tracemalloc.start()
    started = time.perf_counter()
    step_until(env, 0)
    wall = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    rate = ENTITIES / wall
    peak_mb = peak / 1_048_576
    assert env.ctx.entity_log_truncated, "the log cap never engaged -- test is not exercising the bound"
    assert rate >= THROUGHPUT_FLOOR, (
        f"ran at {rate:.0f} entities/s, below the {THROUGHPUT_FLOOR:.0f} floor "
        f"-- a real performance regression?")
    assert peak_mb <= MEMORY_BOUND_MB, (
        f"peak memory {peak_mb:.1f} MB exceeds the {MEMORY_BOUND_MB:.0f} MB bound "
        f"for {ENTITIES} entities with entity_log_limit={LOG_LIMIT}")
