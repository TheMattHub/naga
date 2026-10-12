"""Queue disciplines and resource sets (ROADMAP 1.2).

The discipline is a property of the resource, the selection rule a property of
the set; both are exercised on deterministic scenarios where the expected
service order is written out by hand.
"""
import numpy as np
import pytest

from nagasim.core.base_process import Block
from nagasim.core.blocks import Process, Release, Seize
from nagasim.core.outputs import summarize_resources
from nagasim.core.resource_manager import ResourceSet
from nagasim.core.sim_classes import Entity


class Recorder:
    """Terminal stub that records the order entities reach it."""

    def __init__(self, name="rec"):
        self.order = []
        self.block = Block(position=(9, 9), type="DISPOSE", name=name)
        self.next_process = []

    def self_run(self, entity):
        self.order.append(entity)


def _lane(env, resource_name, index, recorder, *, delay=0, priority=0, quantity=1):
    """A Seize -> Release pair feeding *recorder*; returns the Seize block."""
    seize = Seize(env, Block(position=(index, 0), type="SEIZE", name=f"seize_{index}"),
                  delay=delay, priority=priority, quantity=quantity)
    release = Release(env, Block(position=(index, 1), type="RELEASE", name=f"release_{index}"))
    seize.set_params_from_dict({"resource_name": resource_name, "priority": priority,
                                "quantity": quantity,
                                "distribution": "Constant", "dist_params": {"value": delay}})
    release.set_params_from_dict({"resource_name": resource_name,
                                 "distribution": "Constant", "dist_params": {"value": 0}})
    seize.next_process = [release]
    release.next_process = [recorder]
    return seize


def _feed(env, seize, entity, at):
    def _run():
        yield env.timeout(at)
        seize.self_run(entity)
    env.process(_run())


def _service_order(env, discipline, values, *, ranking_expr=None, priorities=None):
    """Occupy the single server until t=10, queue one entity per value at
    t=1,2,3..., and return the order in which they are served."""
    env.ctx.resource_manager.register("R", capacity=1, discipline=discipline,
                                      ranking_expr=ranking_expr)
    recorder = Recorder()
    occupier_entity = Entity()
    occupier_entity.value = 0      # every entity needs the ranking attribute
    occupier = _lane(env, "R", 0, recorder, delay=10)
    _feed(env, occupier, occupier_entity, 0)

    entities = []
    for index, value in enumerate(values):
        entity = Entity()
        entity.value = value
        entities.append(entity)
        priority = 0 if priorities is None else priorities[index]
        lane = _lane(env, "R", index + 1, recorder, priority=priority)
        _feed(env, lane, entity, index + 1)

    env.run()
    served = [e.value for e in recorder.order if e is not occupier_entity]
    return served, entities


# -- queue disciplines ------------------------------------------------------

def test_discipline_orders_the_queue():
    """All four disciplines in one test; each needs its own env, so the loop
    builds one per case rather than paying pytest's overhead four times."""
    from nagasim.core.sim_runner import initialize_sim_env

    for discipline, ranking, expected in [
        ("FIFO", None, [30, 10, 20]),                       # arrival order
        ("LIFO", None, [20, 10, 30]),                       # last in, first out
        ("LowestValueFirst", "entity.value", [10, 20, 30]),
        ("HighestValueFirst", "entity.value", [30, 20, 10]),
    ]:
        served, _ = _service_order(initialize_sim_env(seed=0), discipline, [30, 10, 20],
                                   ranking_expr=ranking)
        assert served == expected, discipline


def test_seize_priority_dominates_the_discipline(env):
    """Priority is the first element of the request key, the discipline's rank
    the second: a high-priority entity is served first whatever its value."""
    served, _ = _service_order(env, "HighestValueFirst", [30, 10, 20],
                               ranking_expr="entity.value", priorities=[0, -1, 0])
    assert served == [10, 30, 20]


def test_unknown_discipline_and_missing_ranking_expr_fail_loud(env):
    with pytest.raises(ValueError, match="unknown queue discipline"):
        env.ctx.resource_manager.register("bad", discipline="SmallestFirst")
    with pytest.raises(ValueError, match="needs a ranking_expr"):
        env.ctx.resource_manager.register("also_bad", discipline="LowestValueFirst")


# -- selection rules --------------------------------------------------------

def _set_of(env, capacities, rule):
    for index, capacity in enumerate(capacities):
        env.ctx.resource_manager.register(f"m{index}", capacity=capacity)
    return env.ctx.resource_manager.register_set(
        "S", [f"m{i}" for i in range(len(capacities))], rule=rule)


def _occupy(rset, member_index, units=1):
    """Take *units* of a member outside the set, to shape the scenario."""
    member = rset.members[member_index]
    return [member.request() for _ in range(units)]


def test_preferred_order_picks_the_first_member_with_room(env):
    rset = _set_of(env, [1, 1, 1], "PreferredOrder")
    rng = np.random.default_rng(0)
    assert rset.pick(1, rng) is rset.members[0]
    _occupy(rset, 0)
    env.run()
    assert rset.pick(1, rng) is rset.members[1]


def test_cyclical_rotates_through_the_members(env):
    rset = _set_of(env, [1, 1, 1], "Cyclical")
    rng = np.random.default_rng(0)
    assert [rset.pick(1, rng).name for _ in range(4)] == ["m0", "m1", "m2", "m0"]


def test_random_is_reproducible_per_seed(env):
    rset = _set_of(env, [1, 1, 1], "Random")
    first = [rset.pick(1, np.random.default_rng(7)).name for _ in range(5)]
    again = [rset.pick(1, np.random.default_rng(7)).name for _ in range(5)]
    other = [rset.pick(1, np.random.default_rng(8)).name for _ in range(5)]
    assert first == again
    assert set(first) <= {"m0", "m1", "m2"}
    assert first != other or len(set(first)) == 1


def test_smallest_number_busy_picks_the_least_loaded_member(env):
    rset = _set_of(env, [2, 2], "SmallestNumberBusy")
    _occupy(rset, 0, units=1)
    env.run()
    assert rset.pick(1, np.random.default_rng(0)) is rset.members[1]


def test_largest_remaining_capacity_picks_the_emptiest_member(env):
    rset = _set_of(env, [3, 1], "LargestRemainingCapacity")
    _occupy(rset, 0, units=1)      # m0 has 2 free, m1 has 1
    env.run()
    assert rset.pick(1, np.random.default_rng(0)) is rset.members[0]


def test_pick_returns_none_when_no_member_has_enough_free_units(env):
    rset = _set_of(env, [1, 1], "PreferredOrder")
    _occupy(rset, 0)
    _occupy(rset, 1)
    env.run()
    assert rset.pick(1, np.random.default_rng(0)) is None


def test_a_set_cannot_name_an_unknown_member(env):
    env.ctx.resource_manager.register("known", capacity=1)
    with pytest.raises(ValueError, match="unknown member"):
        env.ctx.resource_manager.register_set("S", ["known", "ghost"])


# -- wait for any -----------------------------------------------------------

def _set_scenario(env, capacities, rule="PreferredOrder", quantity=1, holds=(), arrivals=()):
    """Members with *capacities* behind one set; each (hold, arrival) pair is an
    entity seizing `quantity` units for `hold` time units from `arrival`."""
    for index, capacity in enumerate(capacities):
        env.ctx.resource_manager.register(f"m{index}", capacity=capacity)
    rset = env.ctx.resource_manager.register_set("S", [f"m{i}" for i in range(len(capacities))],
                                                 rule=rule)
    recorder = Recorder()
    entities = []
    for index, (hold, arrival) in enumerate(zip(holds, arrivals)):
        entity = Entity()
        entities.append(entity)
        lane = _lane(env, "S", index, recorder, delay=hold, quantity=quantity)
        _feed(env, lane, entity, arrival)
    return rset, entities, recorder


def test_waiting_entity_takes_the_first_member_that_frees_and_cancels_the_rest(env):
    """m0 frees at t=5, m1 at t=10: the waiting entity takes m0, and its m1
    request leaves no trace in m1's queue."""
    rset, entities, _ = _set_scenario(env, [1, 1], holds=(5, 10, 1), arrivals=(0, 0, 0))
    env.run(until=6)

    held = entities[2].req_params["S"]
    assert held["resource"].name == "m0"
    assert len(held["requests"]) == 1
    assert [len(m.queue) for m in rset.members] == [0, 0]

    env.run()
    assert not hasattr(entities[2], "req_params")
    assert [m.queue_len.level for m in rset.members] == [0, 0]
    assert rset.queue_len.level == 0
    assert [m.busy.level for m in rset.members] == [0, 0]
    assert rset.busy.level == 0


def test_simultaneous_frees_never_give_one_entity_two_members(env):
    """Both members come free in the same instant; the waiter takes exactly one
    (member order) and the other unit goes back immediately."""
    rset, entities, _ = _set_scenario(env, [1, 1], holds=(5, 5, 1), arrivals=(0, 0, 0))
    env.run(until=5.5)

    held = entities[2].req_params["S"]
    assert held["resource"].name == "m0"
    assert len(held["requests"]) == 1
    assert rset.members[1].count == 0      # released in the same instant it was granted
    assert rset.busy.level == 1

    env.run()
    assert rset.busy.level == 0
    assert all(len(m.queue) == 0 for m in rset.members)


def test_a_set_never_splits_a_multi_unit_seize_across_members(env):
    """quantity=2 needs both units from ONE member of a set of capacity-2
    members; the third entity waits for a whole member to free."""
    rset, entities, _ = _set_scenario(env, [2, 2], quantity=2,
                                      holds=(5, 8, 1), arrivals=(0, 0, 0))
    env.run(until=1)
    assert [entities[i].req_params["S"]["resource"].name for i in (0, 1)] == ["m0", "m1"]
    assert "S" not in getattr(entities[2], "req_params", {})

    env.run(until=6)
    held = entities[2].req_params["S"]
    assert held["resource"].name == "m0"
    assert len(held["requests"]) == 2
    assert held["resource"].count == 2      # never more than the 2 units it asked for

    env.run()
    assert [m.busy.level for m in rset.members] == [0, 0]
    assert rset.busy.level == 0


def test_the_set_counts_a_waiting_entity_once_and_the_members_not_at_all(env):
    """An entity queued on a set has a request in every member queue; only the
    set's collectors count it, so summing member queues never double counts."""
    rset, _, _ = _set_scenario(env, [1, 1], holds=(10, 10, 10), arrivals=(0, 0, 0))
    env.run(until=1)

    assert rset.queue_len.level == 1
    assert [m.queue_len.level for m in rset.members] == [0, 0]
    assert rset.busy.level == 2
    assert rset.capacity == 2
    assert rset.open_waits(1.0) == [1.0]


# -- the Process block ------------------------------------------------------

def test_process_seize_delay_release_holds_the_resource_for_the_delay(env):
    env.ctx.resource_manager.register("R", capacity=1)
    recorder = Recorder()
    block = Process(env, Block(position=(0, 0), type="PROCESS", name="process_1"))
    block.set_params_from_dict({"action": "Seize Delay Release", "resource_name": "R",
                                "quantity": 1, "priority": 0,
                                "distribution": "Constant", "dist_params": {"value": 4.0}})
    block.next_process = [recorder]

    entity = Entity()
    block.self_run(entity)
    env.run(until=2)
    assert env.ctx.resource_manager.get("R").count == 1
    env.run()
    assert env.now == 4
    assert recorder.order == [entity]
    assert env.ctx.resource_manager.get("R").count == 0


def test_process_delay_action_needs_no_resource(env):
    recorder = Recorder()
    block = Process(env, Block(position=(0, 0), type="PROCESS", name="process_1"))
    block.set_params_from_dict({"action": "Delay", "resource_name": "",
                                "distribution": "Constant", "dist_params": {"value": 3.0}})
    block.next_process = [recorder]

    block.self_run(Entity())
    env.run()
    assert env.now == 3
    assert len(recorder.order) == 1


def test_process_split_actions_hand_the_resource_over(env):
    """"Seize Delay" then "Delay Release" on the same name: the second block
    releases what the first one took."""
    env.ctx.resource_manager.register("R", capacity=1)
    recorder = Recorder()
    first = Process(env, Block(position=(0, 0), type="PROCESS", name="p1"))
    first.set_params_from_dict({"action": "Seize Delay", "resource_name": "R",
                               "distribution": "Constant", "dist_params": {"value": 2.0}})
    second = Process(env, Block(position=(1, 0), type="PROCESS", name="p2"))
    second.set_params_from_dict({"action": "Delay Release", "resource_name": "R",
                                "distribution": "Constant", "dist_params": {"value": 3.0}})
    first.next_process = [second]
    second.next_process = [recorder]

    first.self_run(Entity())
    env.run(until=4)
    assert env.ctx.resource_manager.get("R").count == 1     # still held between the blocks
    env.run()
    assert env.now == 5
    assert env.ctx.resource_manager.get("R").count == 0
    assert len(recorder.order) == 1


def test_process_without_a_resource_name_fails_loud(env):
    block = Process(env, Block(position=(0, 0), type="PROCESS", name="process_1"))
    block.set_params_from_dict({"action": "Seize Delay Release", "resource_name": "",
                                "distribution": "Constant", "dist_params": {"value": 1.0}})
    block.next_process = [Recorder()]
    block.self_run(Entity())
    with pytest.raises(RuntimeError, match="has no resource"):
        env.run()


def test_a_set_is_reported_as_one_pseudo_resource_row(env):
    rset, _, _ = _set_scenario(env, [1, 1], holds=(5, 5, 5), arrivals=(0, 0, 0))
    env.run()

    summary, _ = summarize_resources(env, warmup=0.0)
    assert set(summary["Resource"]) == {"m0", "m1", "S"}
    set_row = summary[summary["Resource"] == "S"].iloc[0]
    assert set_row["Total Seizes"] == 3
    assert isinstance(rset, ResourceSet)


def test_system_avg_resource_usage_does_not_double_count_a_set(tmp_path):
    """Regression: a set's own `busy` collector re-counts its members' busy
    units (seizing.py routes a member request with counted=False, but the
    member's own busy collector -- and seize_count -- still update), so the
    system's 'Avg Resource Usage %' must average the real resources
    (TellerA, TellerB), never the set's pseudo-row too."""
    from pathlib import Path

    from nagasim.core.sim_runner import run_single_simulation

    path = Path(__file__).resolve().parents[1] / "nagasim" / "models" / "two_teller_set.json"
    kpi = run_single_simulation(str(path), seed=0, run_len=500, warmup=50)

    res_sum = kpi["resource_summary"]
    member_usage = res_sum[res_sum["Resource"].isin(["TellerA", "TellerB"])]["Usage % (snapshot)"]
    expected = member_usage.mean()

    system_usage = kpi["system"].loc[kpi["system"]["Metric"] == "Avg Resource Usage %", "Value"].iloc[0]
    # Both sides are independently rounded to 2 decimals before this comparison
    # (per-resource, then the system average), so allow for that -- the bug
    # this guards against was a set inflating the average by tens of points,
    # not a rounding-sized discrepancy.
    assert system_usage == pytest.approx(expected, abs=0.01)
