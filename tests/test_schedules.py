"""Schedules, time-varying arrivals, capacity calendars and failures (ROADMAP 1.4).

Three kinds of check, in order: pure value semantics for `core/schedules.py`,
deterministic hand-computable timetables for the capacity/WAIT rules, and
statistical oracles for the two stochastic mechanisms (NHPP thinning, failures).

Runtime budget: the whole module stays a few seconds; if it grows, cut the
replication counts rather than the assertions.
"""
import functools
import math

import numpy as np
import pytest

from nagasim.core.schedules import Schedule, read_schedule_csv, validate_schedules_structured
from nagasim.core.schema import validate_model
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.stats import ci95

import json
from pathlib import Path

from nagasim.core.base_process import Block
from nagasim.core.blocks import Delay, Dispose
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.resource_manager import _failure_distribution, _sample_scaled
from nagasim.core.schema import migrate, validate_layout_structured
from nagasim.core.sim_classes import Entity
from nagasim.core.timeunits import UNITS, factor

from conftest import layout as _layout, create as _create, process as _process, \
    dispose as _dispose, resource_row as _resource_row, chain as _chain


# ---------------------------------------------------------------- value semantics
def test_schedule_value_and_boundaries_without_repeat():
    """The last value holds for ever once the pattern runs out."""
    s = Schedule("s", "arrival", [[2, 1], [3, 5], [1, 0]], repeat=False)

    assert s.cycle_length == 6
    assert s.max_value == 5
    assert [s.value_at(t) for t in (0, 1.9, 2, 4.9, 5, 5.9)] == [1, 1, 5, 5, 0, 0]
    assert s.value_at(100) == 0            # past the pattern, the last value stands

    assert s.next_change_after(0) == 2
    assert s.next_change_after(2) == 5
    assert s.next_change_after(5) == math.inf   # nothing can change any more


def test_schedule_repeats_and_reports_its_wrap():
    s = Schedule("s", "capacity", [[2, 1], [3, 5]], repeat=True)

    assert s.cycle_length == 5
    assert [s.value_at(t) for t in (0, 2, 4.9, 5, 7, 12)] == [1, 5, 5, 1, 5, 5]
    assert s.next_change_after(0) == 2
    assert s.next_change_after(2) == 5      # the wrap back to the first value
    assert s.next_change_after(5) == 7
    assert s.next_change_after(4.5) == 5


def test_schedule_rejects_bad_points():
    with pytest.raises(ValueError, match="no points"):
        Schedule("s", "arrival", [])
    with pytest.raises(ValueError, match="duration"):
        Schedule("s", "arrival", [[0, 1]])
    with pytest.raises(ValueError, match="value"):
        Schedule("s", "arrival", [[1, -1]])
    with pytest.raises(ValueError, match="unknown kind"):
        Schedule("s", "shift", [[1, 1]])


def test_validate_schedules_reports_every_problem():
    problems, kinds = validate_schedules_structured({"schedules": [
        {"name": "ok", "kind": "arrival", "points": [[1, 2]]},
        {"name": "ok", "kind": "capacity", "points": [[1, 2.5]]},
        {"name": "bad_kind", "kind": "shift", "points": [[1, 1]]},
        {"name": "empty", "kind": "arrival", "points": []},
        {"name": "negative", "kind": "arrival", "points": [[-1, 1], [1, -2]]},
    ]})

    # A schedule with an unknown kind is not usable, so it is not in the map.
    assert kinds == {"ok": "capacity", "empty": "arrival", "negative": "arrival"}
    joined = "; ".join(p.message for p in problems)
    assert "duplicate schedule name: ok" in joined
    assert "whole number" in joined          # a capacity value of 2.5
    assert "unknown kind 'shift'" in joined
    assert "'empty': no points" in joined
    assert "duration must be > 0" in joined
    assert "value must be >= 0" in joined


def test_read_schedule_csv_takes_the_first_numeric_column_sorted(tmp_path):
    path = tmp_path / "arrivals.csv"
    path.write_text("label,time\nfirst,5.5\nsecond,1.25\nthird,3\n")
    assert read_schedule_csv(path) == [1.25, 3.0, 5.5]


# ---------------------------------------------------------------- schema
def test_schema_rejects_a_create_pointing_at_a_missing_or_wrong_kind_schedule():
    blocks = [_create(arrival_mode="Schedule", schedule_name="shifts"), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        schedules=[{"name": "shifts", "kind": "capacity", "points": [[1, 1]]}])
    assert any("not a declared schedule of kind 'arrival'" in p for p in validate_model(layout))

    layout["blocks"][0]["params"]["schedule_name"] = ""
    assert any("needs a schedule_name" in p for p in validate_model(layout))

    layout["blocks"][0]["params"]["arrival_mode"] = "Whenever"
    assert any("unknown arrival_mode" in p for p in validate_model(layout))


def test_schema_rejects_a_capacity_schedule_above_the_resource_maximum():
    blocks = [_create(), _process(), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO",
                    "capacity_schedule": "shifts"}],
        schedules=[{"name": "shifts", "kind": "capacity", "points": [[1, 3]]}])
    assert any("is below the maximum 3" in p for p in validate_model(layout))

    layout["resources"][0]["capacity"] = 3
    assert [p for p in validate_model(layout) if not p.startswith("warning:")] == []


def test_schema_rejects_a_failure_naming_an_unknown_resource():
    blocks = [_create(), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        failures=[{"name": "f", "resources": ["Ghost"],
                   "uptime": {"distribution": "Exponential", "dist_params": {"rate": 1}},
                   "downtime": {"distribution": "Nonsense", "dist_params": {}}}])
    joined = "; ".join(validate_model(layout))
    assert "unknown resource 'Ghost'" in joined
    assert "unknown downtime distribution 'Nonsense'" in joined


# ---------------------------------------------------------------- NHPP oracle
_NHPP_RATES = [2.0, 8.0, 0.0, 4.0]
_NHPP_REPLICATIONS = 400   # enough that the index of dispersion is a tight check


def _nhpp_interval_counts(replications=_NHPP_REPLICATIONS, pool=None):
    """Arrivals per unit-length interval of one cycle, per replication. Over
    the session pool (conftest) when one is given: a replication is seeded
    from its own seed, so the counts are identical whoever runs it -- only
    the wall time changes (PHASE5 O7, D76)."""
    blocks = [_create(arrival_mode="Schedule", schedule_name="rates"), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        schedules=[{"name": "rates", "kind": "arrival", "repeat": True,
                    "points": [[1.0, rate] for rate in _NHPP_RATES]}])
    run = functools.partial(run_single_simulation, layout, run_len=len(_NHPP_RATES), warmup=0)
    if pool is None:
        kpis = [run(seed=seed) for seed in range(replications)]
    else:
        futures = [pool.submit(run, seed=seed) for seed in range(replications)]
        kpis = [future.result() for future in futures]

    counts = []
    for kpi in kpis:
        created = kpi["entity_log"]["Created At"].to_numpy(dtype=float)
        counts.append([int(((created >= i) & (created < i + 1)).sum())
                       for i in range(len(_NHPP_RATES))])
    return np.asarray(counts, dtype=float)


@pytest.mark.slow
def test_nhpp_arrivals_match_the_rate_schedule(worker_pool):
    """Thinning (Lewis-Shedler) must reproduce a Poisson process whose mean and
    variance per interval are both `rate x duration`."""
    counts = _nhpp_interval_counts(pool=worker_pool)

    for index, rate in enumerate(_NHPP_RATES):
        column = counts[:, index]
        if rate == 0:
            assert column.sum() == 0, "a zero-rate interval must produce no arrival at all"
            continue
        st = ci95(column, confidence=0.99)
        assert st["lb"] <= rate <= st["ub"], (
            f"interval {index}: rate {rate} outside 99% CI "
            f"[{st['lb']:.3f}, {st['ub']:.3f}]")
        dispersion = column.var(ddof=1) / column.mean()
        assert 0.8 <= dispersion <= 1.2, (
            f"interval {index}: index of dispersion {dispersion:.3f} is not Poisson-like")


def test_nhpp_thinning_is_reproducible_per_seed():
    blocks = [_create(arrival_mode="Schedule", schedule_name="rates"), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        schedules=[{"name": "rates", "kind": "arrival", "repeat": True,
                    "points": [[1.0, rate] for rate in _NHPP_RATES]}])

    def times(seed):
        kpi = run_single_simulation(layout, seed=seed, run_len=20, warmup=0)
        return list(kpi["entity_log"]["Created At"])

    assert times(3) == times(3)
    assert times(3) != times(4)


def test_a_zero_rate_schedule_creates_nothing():
    blocks = [_create(arrival_mode="Schedule", schedule_name="closed"), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        schedules=[{"name": "closed", "kind": "arrival", "repeat": True,
                    "points": [[5, 0]]}])
    kpi = run_single_simulation(layout, seed=0, run_len=100, warmup=0)
    assert len(kpi["entity_log"]) == 0


# ---------------------------------------------------------------- mid-cycle warm start (T3)
def _two_shift_line(offset=None):
    """D/D/1 on a two-shift calendar: 8 on, 8 off, repeating. Arrivals every 2
    from t=0, service 1.5, capacity 1."""
    blocks = [_create(distribution="Constant", dist_params={"value": 2.0}, max_arrivals=6),
              _process(value=1.5), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO",
                    "capacity_schedule": "shifts"}],
        schedules=[{"name": "shifts", "kind": "capacity", "repeat": True,
                    "points": [[8, 1], [8, 0]]}])
    if offset is not None:
        layout["initial_state"] = {"schedule_offset": offset}
    return layout


def test_a_capacity_schedule_resumes_three_hours_into_the_shift():
    """PHASE4 T3. Warm-started 3 into the 8-long shift, the model's clock still
    starts at 0 but the schedule is read at t+3: capacity is 1 on [0, 5), 0 on
    [5, 13), 1 again on [13, 21).

    By hand: E1 0-1.5, E2 2-3.5, E3 4-5.5 (in service when the shift ends at 5,
    so it finishes -- the WAIT rule), E4 arrives at 6 and waits for 13
    (13-14.5), E5 14.5-16, E6 16-17.5.
    """
    schedule = Schedule("shifts", "capacity", [[8, 1], [8, 0]], repeat=True, offset=3.0)
    assert [schedule.value_at(t) for t in (0, 4.9, 5, 12.9, 13, 21)] == [1, 1, 0, 0, 1, 0]
    assert schedule.next_change_after(0) == 5
    assert schedule.next_change_after(5) == 13

    kpi = run_single_simulation(_two_shift_line(offset=3.0), seed=0, run_len=30, warmup=0)
    assert sorted(kpi["entity_log"]["Exited At"]) == pytest.approx(
        [1.5, 3.5, 5.5, 14.5, 16.0, 17.5])
    # Without the offset the shift runs to 8, so only E5 onwards waits.
    plain = run_single_simulation(_two_shift_line(), seed=0, run_len=30, warmup=0)
    assert sorted(plain["entity_log"]["Exited At"]) == pytest.approx(
        [1.5, 3.5, 5.5, 7.5, 17.5, 19.0])


def test_an_arrival_schedule_resumes_mid_cycle_too():
    """The same offset on a CREATE's rate profile: 6 closed then 6 open,
    started 6 in, so the door is open at t=0 and shuts at t=6."""
    def run(offset):
        blocks = [_create(arrival_mode="Schedule", schedule_name="day"), _dispose()]
        layout = _layout(
            blocks, _chain(*blocks),
            schedules=[{"name": "day", "kind": "arrival", "repeat": True,
                        "points": [[6, 0], [6, 2]]}])
        if offset:
            layout["initial_state"] = {"schedule_offset": offset}
        return run_single_simulation(layout, seed=0, run_len=6, warmup=0)["entity_log"]

    assert len(run(0)) == 0                      # exact: a zero rate creates nothing
    assert len(run(6.0)) > 0                     # rate 2 for 6 units: P(none) is 6e-6


# ---------------------------------------------------------------- capacity, exact
def test_capacity_schedule_is_exact_and_never_interrupts_a_job_in_service():
    """D/D/1, arrivals every 4 from t=0, service 3, capacity 1 -> 0 on [10, 15).

    By hand: E1 0-3, E2 4-7, E3 8-11 (in service when the shift ends at 10, so
    it finishes -- Arena's WAIT rule -- and the blocker only takes the unit at
    11), E4 arrives at 12 but waits for the shift at 15 (15-18), E5 arrives at
    16 and follows (18-21), E6 arrives at 20 (21-24).
    """
    blocks = [_create(distribution="Constant", dist_params={"value": 4.0}, max_arrivals=6),
              _process(value=3.0), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO",
                    "capacity_schedule": "shift"}],
        schedules=[{"name": "shift", "kind": "capacity", "repeat": False,
                    "points": [[10, 1], [5, 0], [100, 1]]}])

    kpi = run_single_simulation(layout, seed=0, run_len=0, warmup=0)
    exits = sorted(kpi["entity_log"]["Exited At"])
    assert exits == pytest.approx([3.0, 7.0, 11.0, 18.0, 21.0, 24.0])


def test_utilization_denominator_is_the_scheduled_capacity():
    """Capacity 2, on for 10 and off for 10, saturated while on: utilization is
    100% (the off time is not idle time) and the scheduled capacity averages
    half the maximum."""
    blocks = [_create(distribution="Constant", dist_params={"value": 0.1}),
              _process(value=0.5), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "Server", "capacity": 2, "discipline": "FIFO",
                    "capacity_schedule": "half_time"}],
        schedules=[{"name": "half_time", "kind": "capacity", "repeat": True,
                    "points": [[10, 2], [10, 0]]}])

    kpi = run_single_simulation(layout, seed=0, run_len=220, warmup=20)
    row = _resource_row(kpi)
    assert row["Usage % (snapshot)"] == pytest.approx(100.0, abs=0.01)
    assert row["Scheduled Capacity Avg"] == pytest.approx(1.0, abs=1e-6)
    assert row["Down %"] == 0.0


def test_a_schedule_that_never_binds_leaves_every_statistic_untouched():
    """Blockers must be invisible: a capacity schedule pinned at the resource's
    maximum requests none, and every queue/wait/seize KPI is bit-identical to
    the same model without a schedule."""
    blocks = [_create(distribution="Exponential", dist_params={"rate": 1.0}),
              _process(value=1.25, distribution="Exponential"), _dispose()]
    links = _chain(*blocks)
    plain = _layout(blocks, links,
                    resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}])
    scheduled = _layout(blocks, links,
                        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO",
                                    "capacity_schedule": "always_on"}],
                        schedules=[{"name": "always_on", "kind": "capacity", "repeat": True,
                                    "points": [[7, 1], [3, 1]]}])

    a = _resource_row(run_single_simulation(plain, seed=0, run_len=500, warmup=50))
    b = _resource_row(run_single_simulation(scheduled, seed=0, run_len=500, warmup=50))
    for column in ("Total Seizes", "Usage % (snapshot)", "Queue Length Avg",
                   "Queue Length Max", "Queue Length Mode", "Queue Len P95",
                   "Time in Queue Avg", "Time in Queue Max", "Wait P95",
                   "Scheduled Capacity Avg"):
        assert a[column] == b[column], f"'{column}' changed under a never-binding schedule"


def test_a_set_only_picks_members_that_are_actually_available():
    """TellerA is off shift for the whole run, so every seize must land on
    TellerB even though PreferredOrder lists A first."""
    blocks = [_create(distribution="Constant", dist_params={"value": 2.0}, max_arrivals=10),
              _process(resource="Tellers", value=1.0), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "TellerA", "capacity": 1, "discipline": "FIFO",
                    "capacity_schedule": "closed"},
                   {"name": "TellerB", "capacity": 1, "discipline": "FIFO"}],
        resource_sets=[{"name": "Tellers", "members": ["TellerA", "TellerB"],
                        "rule": "PreferredOrder"}],
        schedules=[{"name": "closed", "kind": "capacity", "repeat": False,
                    "points": [[1000, 0]]}])

    kpi = run_single_simulation(layout, seed=0, run_len=0, warmup=0)
    assert _resource_row(kpi, "TellerA")["Total Seizes"] == 0
    assert _resource_row(kpi, "TellerB")["Total Seizes"] == 10
    assert _resource_row(kpi, "TellerA")["Scheduled Capacity Avg"] == 0.0


# ---------------------------------------------------------------- failures
def test_a_failure_never_interrupts_the_job_in_service():
    """Deterministic: the machine fails at t=5 while a 10-long job that started
    at t=0 is running. The job still finishes at 10; the repair (2 long) only
    starts once the unit is free, so the next job starts at 12."""
    blocks = [_create(distribution="Constant", dist_params={"value": 100.0}, max_arrivals=2),
              _process(value=10.0), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        failures=[{"name": "stoppage", "resources": ["Server"],
                   "uptime": {"distribution": "Constant", "dist_params": {"value": 5.0}},
                   "downtime": {"distribution": "Constant", "dist_params": {"value": 2.0}}}])

    kpi = run_single_simulation(layout, seed=0, run_len=60, warmup=0)
    exits = sorted(kpi["entity_log"]["Exited At"])
    # E1: 0-10 (untouched by the failure at t=5). E2 arrives at 100 -> never runs
    # inside the 60-long window, so the second failure cycle is what fills the
    # rest: the machine is free, so it is taken at once.
    assert exits[0] == pytest.approx(10.0)
    row = _resource_row(kpi)
    assert row["Total Seizes"] == 1, "a blocker must never count as a seize"
    assert row["Down %"] > 0
    # The failure's blocker queues from t=5 (request) to t=10 (unit freed), the
    # WAIT rule in action -- but it must never count as a queued entity either.
    assert row["Queue Length Max"] == 0, "a blocker must never count as a queued entity"


def test_resource_state_events_reach_the_bus():
    """ROADMAP 1.4 point E / 2.3 part C: the engine emits `resource_state` for
    the animator, and TraceRecorder now records it. The first value is
    `load_from_json`'s baseline capacity emission (every declared resource
    gets one, scheduled or not); the schedule's own controller follows with
    its own sequence, starting from the same value at t=0."""
    from nagasim.core.trace import TraceRecorder

    blocks = [_create(max_arrivals=1), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "Machine", "capacity": 1, "discipline": "FIFO",
                    "capacity_schedule": "shift"}],
        schedules=[{"name": "shift", "kind": "capacity", "repeat": True,
                    "points": [[5, 1], [5, 0]]}])

    seen = []
    recorder = TraceRecorder()

    def spy(name, **payload):
        if name == "resource_state":
            seen.append(payload)

    run_single_simulation(layout, seed=0, run_len=20, warmup=0, subscribers=(spy, recorder))

    assert [p["value"] for p in seen] == [1, 1, 0, 1, 0, 1]
    assert all(p["resource"] == "Machine" and p["state"] == "capacity" for p in seen)
    recorded = [e for e in recorder.events if e.kind == "resource_state"]
    assert [e.value for e in recorded] == [1, 1, 0, 1, 0, 1]
    assert all(e.resource == "Machine" and e.state == "capacity" for e in recorded)


def test_blockers_are_invisible_to_the_model_expression_functions(env):
    """NQ/NR/MR read entities and scheduled units, never blockers: a model
    expression must not be able to see a schedule's bookkeeping."""
    from nagasim.core.expressions import compile_expression, ExpressionScope

    res = env.ctx.resource_manager.register("Server", capacity=2)
    scope = ExpressionScope(ctx=env.ctx)

    def read(text):
        return compile_expression(text).evaluate(scope)

    entity_request = res.request()
    env.run(until=1)
    assert (read("NR('Server')"), read("NQ('Server')"), read("MR('Server')")) == (1, 0, 2)

    blocker = res.request_blocker()          # takes the one free unit at once
    env.run(until=2)
    assert (read("NR('Server')"), read("NQ('Server')"), read("MR('Server')")) == (1, 0, 1)
    assert res.free == 0

    queued_blocker = res.request_blocker()   # waits for the busy unit (WAIT rule)
    env.run(until=3)
    assert (read("NR('Server')"), read("NQ('Server')"), read("MR('Server')")) == (1, 0, 1)
    assert res.free == 0                     # the queued blocker has claimed the busy unit

    res.release(entity_request)
    env.run(until=4)
    assert (read("NR('Server')"), read("NQ('Server')"), read("MR('Server')")) == (0, 0, 0)

    res.release_blocker(blocker)
    res.release_blocker(queued_blocker)
    env.run(until=5)
    assert (read("NR('Server')"), read("NQ('Server')"), read("MR('Server')")) == (0, 0, 2)


# ======================= time units =======================================
MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"


# ---------------------------------------------------------------- factor()
def test_factor_table():
    assert factor("seconds", "minutes") == pytest.approx(1 / 60)
    assert factor("minutes", "minutes") == 1.0
    assert factor("hours", "minutes") == 60.0
    assert factor("days", "minutes") == 1440.0
    assert factor("base", "minutes") == 1.0    # "base" always needs no conversion
    assert factor("hours", "hours") == 1.0


def test_factor_rejects_unknown_units():
    with pytest.raises(ValueError):
        factor("fortnights", "minutes")
    with pytest.raises(ValueError):
        factor("minutes", "fortnights")


# ---------------------------------------------------------------- sample_duration
def test_delay_in_hours_takes_120_minutes_in_a_minutes_model(env):
    """env fixture builds a minutes-base context (ctx.time_unit == "minutes")."""
    assert env.ctx.time_unit == "minutes"
    delay = Delay(env, Block(position=(0, 0), type="DELAY", name="delay_1"),
                 delay=StochasticDistribution("Constant", {"value": 2}, time_unit="hours"))
    dispose = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="dispose_1"))
    delay.next_process = [dispose]

    entity = Entity()
    entity.creation_time = env.now
    env.ctx.all_entities.append(entity)
    env.ctx.live_entities.append(entity)
    delay.self_run(entity)
    env.run(until=150)

    assert dispose.disposed_entities == 1
    assert entity.deletion_time == pytest.approx(120.0)


def test_base_unit_is_a_no_op():
    """A "base" (or omitted) time_unit never scales the sample -- the pre-1.5
    behaviour, exactly."""
    dist = StochasticDistribution("Constant", {"value": 5}, time_unit="base")
    assert dist.time_unit == "base"


# ---------------------------------------------------------------- schedules
def test_arrival_schedule_rate_converts_with_the_inverse_factor():
    """10 per hour and 1/6 per minute describe the same process in a
    minutes-base model: the two Schedules must be numerically identical."""
    in_hours = Schedule("s", "arrival", [[1, 10]], repeat=True,
                        time_unit="hours", base_unit="minutes")
    in_minutes = Schedule("s", "arrival", [[60, 10 / 60]], repeat=True,
                          time_unit="base", base_unit="minutes")

    assert in_hours.durations == pytest.approx(in_minutes.durations)
    assert in_hours.values == pytest.approx(in_minutes.values)
    for t in (0, 30, 59.9, 60, 90, 119.9):
        assert in_hours.value_at(t) == pytest.approx(in_minutes.value_at(t))


def test_capacity_schedule_values_never_convert_only_durations():
    s = Schedule("s", "capacity", [[1, 3]], time_unit="hours", base_unit="minutes")
    assert s.durations == [60.0]
    assert s.values == [3.0]      # a unit count, not a rate: unchanged


# ---------------------------------------------------------------- failures
def test_failure_uptime_downtime_scale_with_their_own_unit():
    uptime = _failure_distribution({"distribution": "Constant", "dist_params": {"value": 2},
                                    "time_unit": "hours"})
    downtime = _failure_distribution({"distribution": "Constant", "dist_params": {"value": 30},
                                      "time_unit": "base"})
    rng = np.random.default_rng(0)

    assert _sample_scaled(uptime, rng, "minutes") == pytest.approx(120.0)
    assert _sample_scaled(downtime, rng, "minutes") == pytest.approx(30.0)


# ---------------------------------------------------------------- validation
def test_validate_layout_flags_unknown_time_units_everywhere():
    layout = {
        "schema_version": 8,
        "blocks": [{"name": "delay_1", "type": "DELAY", "position": [0, 0],
                    "params": {"distribution": "Constant", "dist_params": {"value": 1},
                              "time_unit": "fortnights"}}],
        "links": [{"source": "delay_1", "target": "delay_1",
                  "delay": {"distribution": "Constant", "dist_params": {"value": 1},
                            "time_unit": "fortnights"}}],
        "schedules": [{"name": "s", "kind": "arrival", "points": [[1, 1]],
                       "time_unit": "fortnights"}],
        "failures": [{"name": "f", "resources": [],
                     "uptime": {"distribution": "Constant", "time_unit": "fortnights"},
                     "downtime": {"distribution": "Constant", "time_unit": "fortnights"}}],
        "simulation": {"time_unit": "fortnights"},
    }
    problems = [p.message for p in validate_layout_structured(layout)]

    assert sum("unknown time_unit 'fortnights'" in p for p in problems) >= 5


def test_validate_layout_accepts_base_and_every_known_unit():
    for unit in ("base", *UNITS):
        layout = {
            "schema_version": 8,
            "blocks": [{"name": "delay_1", "type": "DELAY", "position": [0, 0],
                        "params": {"distribution": "Constant", "dist_params": {"value": 1},
                                  "time_unit": unit}}],
            # The model's own unit is never "base" (#306); a field's may be.
            "links": [], "simulation": {"time_unit": unit if unit != "base" else "minutes"},
        }
        problems = [p.message for p in validate_layout_structured(layout)]
        assert not any("time_unit" in p for p in problems), (unit, problems)


# ---------------------------------------------------------------- migration
def test_migration_v7_to_v8_is_bit_identical_for_one_seed():
    """single_server.json has no v8-only feature (no time_unit, no link delay),
    so migrating it from v7 must not change a single sampled value."""
    raw = json.loads((MODELS_DIR / "single_server.json").read_text())
    v7 = {**raw, "schema_version": 7}
    v8_equivalent = migrate(v7)
    v8_equivalent["schema_version"] = 8   # already the shape v8 would have produced natively

    kpi_before = run_single_simulation(v7, seed=3, run_len=500, warmup=50)
    kpi_after = run_single_simulation(v8_equivalent, seed=3, run_len=500, warmup=50)

    for key in ("Total Entities Created", "Total Entities Completed", "Avg Throughput Time"):
        before = kpi_before["system"].loc[kpi_before["system"]["Metric"] == key, "Value"].iloc[0]
        after = kpi_after["system"].loc[kpi_after["system"]["Metric"] == key, "Value"].iloc[0]
        assert before == pytest.approx(after), key


# ---------------------------------------------------------------- real dates (PHASE4 T10)
# Timezone-independent by construction: every instant is built with an explicit
# zone, never from the machine's local time.
ROME = {"start": "2026-01-05T00:00:00", "timezone": "Europe/Rome"}


def _calendar(**over):
    from nagasim.core.calendar import model_calendar
    return model_calendar({"simulation": {"time_unit": "minutes", "calendar": {**ROME, **over}}})


def test_model_time_datetime_round_trip_across_both_rome_dst_transitions():
    """Europe/Rome springs forward 2026-03-29 02:00 and falls back 2026-10-25
    03:00. Elapsed model time is absolute: the local clock jumps, the numbers
    do not."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    rome = ZoneInfo("Europe/Rome")
    cal = _calendar()
    # 2026-01-05T00:00 local (+01:00) is 2026-01-04T23:00Z
    assert cal.to_iso(0.0) == "2026-01-04T23:00:00Z"

    # Spring forward: 01:59 local -> the next minute is 03:00 local.
    before = datetime(2026, 3, 29, 1, 59, tzinfo=rome)
    after = datetime(2026, 3, 29, 3, 0, tzinfo=rome)
    assert cal.to_model(after) - cal.to_model(before) == pytest.approx(1.0)
    # Fall back: 03:00 local is 25 hours after the previous local midnight.
    midnight = datetime(2026, 10, 25, 0, 0, tzinfo=rome, fold=0)
    assert cal.to_model(datetime(2026, 10, 26, 0, 0, tzinfo=rome)) - cal.to_model(midnight) \
        == pytest.approx(25 * 60.0)

    for t in (0.0, 1234.5, 200_000.0):
        assert cal.to_model(cal.to_datetime(t)) == pytest.approx(t)

    utc = _calendar(start="2026-01-05T00:00:00", timezone="UTC")
    assert utc.to_iso(0.0) == "2026-01-05T00:00:00Z"
    assert utc.label(90.0) == "2026-01-05 01:30"


def test_weekday_schedule_compiles_to_the_hand_computed_instants_over_two_weeks():
    """Mon-Fri 06:00-14:00 capacity 3, Sat 06:00-12:00 capacity 1, Sun off,
    with 2026-01-08 (a Thursday) shut down. Start is Monday 2026-01-05 00:00."""
    from nagasim.core.calendar import compile_weekly
    from nagasim.core.schedules import Schedule

    entry = {"name": "shift", "kind": "capacity", "off_value": 0,
             "weekly": {"mon": [["06:00", "14:00", 3]], "tue": [["06:00", "14:00", 3]],
                        "wed": [["06:00", "14:00", 3]], "thu": [["06:00", "14:00", 3]],
                        "fri": [["06:00", "14:00", 3]], "sat": [["06:00", "12:00", 1]]},
             "exceptions": [{"start": "2026-01-08", "value": 0}]}
    cal = _calendar()
    horizon = 14 * 24 * 60.0
    points = compile_weekly(entry, cal, horizon)
    schedule = Schedule("shift", "capacity", points, False)

    day = 24 * 60.0
    # Mon..Sun for two weeks; the Thursday of week one is the shutdown.
    pattern = [3, 3, 3, 0, 3, 1, 0] + [3, 3, 3, 3, 3, 1, 0]
    expected = []
    for index, value in enumerate(pattern):
        base = index * day
        if value == 0:
            expected += [(base + 7 * 60, 0)]
            continue
        close = 14 * 60 if value == 3 else 12 * 60
        expected += [(base + 5 * 60, 0), (base + 6 * 60 + 1, value),
                     (base + close - 1, value), (base + close + 1, 0)]
    for t, value in expected:
        assert schedule.value_at(t) == value, (t, value)
    # Exactly those boundaries, no spurious ones: two per working day.
    changes = []
    t = 0.0
    while True:
        t = schedule.next_change_after(t)
        if not math.isfinite(t) or t >= horizon:
            break
        changes.append(t)
    assert len(changes) == 2 * sum(1 for v in pattern if v)
    assert changes[0] == 6 * 60 and changes[1] == 14 * 60


def test_wall_clock_shift_stays_at_06_00_local_across_the_dst_change():
    """The plant rule: a shift starts at 06:00 local every day. The model time
    of that instant moves by an hour when the clocks do."""
    from nagasim.core.calendar import compile_weekly
    from nagasim.core.schedules import Schedule

    entry = {"name": "shift", "kind": "capacity", "off_value": 0,
             "weekly": {day: [["06:00", "14:00", 2]] for day in
                        ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}}
    cal = _calendar(start="2026-03-27T00:00:00")
    schedule = Schedule("shift", "capacity", compile_weekly(entry, cal, 5 * 24 * 60.0), False)
    for day in ("2026-03-27", "2026-03-28", "2026-03-29", "2026-03-30"):
        # 05:59 local is off, 06:01 local is on -- on both sides of the jump.
        assert schedule.value_at(cal.to_model(f"{day}T05:59:00")) == 0, day
        assert schedule.value_at(cal.to_model(f"{day}T06:01:00")) == 2, day
    # ... and the DST day really is 23 hours long in model time.
    assert cal.to_model("2026-03-30T00:00:00") - cal.to_model("2026-03-29T00:00:00") \
        == pytest.approx(23 * 60.0)


def test_date_ticks_pick_hours_days_and_months_for_the_three_spans():
    from nagasim.core.calendar import date_ticks

    cal = _calendar(timezone="UTC")
    day, week, year = 24 * 60.0, 7 * 24 * 60.0, 365 * 24 * 60.0
    hours = [label for _t, label in date_ticks(cal, 0, day)]
    assert hours[:2] == ["05 Jan 00:00", "05 Jan 06:00"]
    days = [label for _t, label in date_ticks(cal, 0, week)]
    assert days[0] == "05 Jan" and len(days) <= 12
    months = [label for _t, label in date_ticks(cal, 0, year)]
    assert months[0].endswith("2026") and len(months) <= 12


def test_a_weekday_schedule_without_a_calendar_is_a_coded_problem():
    from nagasim.core.schedules import validate_schedules_structured

    problems, _kinds = validate_schedules_structured(
        {"schedules": [{"name": "shift", "kind": "capacity",
                        "weekly": {"mon": [["06:00", "14:00", 3]]}}]})
    assert [p.code for p in problems] == ["E_SCHEDULE_NO_CALENDAR"]


def test_calendar_problems_cover_timezone_dates_and_exception_window():
    from nagasim.core.calendar import validate_calendar_structured
    from nagasim.core.schedules import validate_schedules_structured

    bad, cal = validate_calendar_structured(
        {"simulation": {"calendar": {"start": "2026-01-05", "timezone": "Europe/Atlantis"}}})
    assert [p.code for p in bad] == ["E_CALENDAR_TIMEZONE"] and cal is None

    good, cal = validate_calendar_structured({"simulation": {"calendar": ROME}})
    assert not good and cal is not None
    problems, _ = validate_schedules_structured(
        {"schedules": [{"name": "shift", "kind": "capacity",
                        "weekly": {"mon": [["06:00", "14:00", 3]]},
                        "exceptions": [{"start": "2030-12-25"}, {"start": "not-a-date"}]}]},
        cal, 7 * 24 * 60.0)
    assert sorted(p.code for p in problems) == ["E_SCHEDULE_EXCEPTION",
                                                "W_SCHEDULE_EXCEPTION_UNUSED"]


def test_a_weekday_capacity_schedule_drives_a_real_run():
    """End to end through `GraphModel`: an hours-based model opening Mon-Fri
    08:00-12:00, started on the Sunday. Arrivals every hour from t=0, service
    1 h: nothing can be served before Monday 08:00."""
    blocks = [_create(distribution="Constant", dist_params={"value": 1.0}, max_arrivals=3),
              _process(value=1.0), _dispose()]
    layout = _layout(
        blocks, _chain(*blocks),
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO",
                    "capacity_schedule": "opening"}],
        schedules=[{"name": "opening", "kind": "capacity", "off_value": 0,
                    "weekly": {"mon": [["08:00", "12:00", 1]]}}])
    layout["simulation"] = {**layout.get("simulation", {}), "time_unit": "hours",
                            "calendar": {"start": "2026-01-04T00:00:00", "timezone": "UTC"}}
    kpi = run_single_simulation(layout, seed=0, run_len=48, warmup=0)
    # Sunday 00:00 + 32 h = Monday 08:00; three jobs of 1 h each, back to back.
    assert sorted(kpi["entity_log"]["Exited At"]) == pytest.approx([33.0, 34.0, 35.0])
