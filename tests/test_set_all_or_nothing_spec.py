"""Phase 15 M10: all-or-nothing multi-unit seizes (D856-D860), written from the
design spec only (docs/superpowers/specs/2026-10-26-phase15-limits-design.md,
"M10 set all-or-nothing"), independently of the builder's regressions in
test_regressions.py.

Every expected value below was derived by hand from the timelines in the
docstrings. A "job" is CREATE (max 1 arrival at `start`) -> PROCESS -> DISPOSE;
the run ends at its last event unless a time limit is given. A losing grant
leaves no trace (D860): no seize, no usage cost, no busy time.
"""
import pytest

from nagasim.api import run_model
from nagasim.core.base_process import Block
from nagasim.core.blocks import Release, Seize
from nagasim.core.sim_classes import Entity

from conftest import chain, create, dispose, layout, process, resource_record


# ---------------------------------------------------------------- helpers
def _job(name, resource, start, delay, qty=1, **extra):
    """One entity arriving at *start*: PROCESS (seize, delay, release) -> DISPOSE.
    The entity type is *name*, so the entity log is read back by name."""
    arrive = create(name + "_in", max_arrivals=1, start_time=start, distribution="Constant",
                    dist_params={"value": 1000.0}, e_type=name)
    work = process(name, resource=resource, value=delay, quantity=qty, **extra)
    out = dispose(name + "_out")
    return [arrive, work, out], chain(arrive, work, out)


def _run(jobs, resources, sets=(), schedules=(), time_limit=None, extra_links=None, extra_blocks=()):
    """jobs: tuples (name, resource, start, delay[, qty[, extras dict]])."""
    blocks, links = [], []
    for spec in jobs:
        name, resource, start, delay = spec[:4]
        qty = spec[4] if len(spec) > 4 else 1
        extra = spec[5] if len(spec) > 5 else {}
        job_blocks, job_links = _job(name, resource, start, delay, qty, **extra)
        blocks += job_blocks
        links += job_links
    blocks += list(extra_blocks)
    links += list(extra_links or [])
    model = layout(blocks, links, resources=list(resources), resource_sets=list(sets),
                   schedules=list(schedules),
                   simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})
    return run_model(model, replications=1, run_length=time_limit or 1000, warmup=0)


def _res(results, name):
    frame = results["resource_summary"]
    return frame[frame["Resource"] == name].iloc[0]


def _entity(results, name):
    """The entity-log row of the entity of type *name* (the log title-cases types)."""
    log = results["entity_log"]
    return log[log["Type"].str.lower() == name.lower()].iloc[0]


def _queue_stat(results, block, column):
    frame = results["queue_summary"]
    rows = frame[frame["Block"] == block]
    return 0 if rows.empty else rows.iloc[0][column]


def _preempted(results):
    """Preempted summed over every block: the count is the victims', wherever the model keeps it."""
    frame = results["queue_summary"]
    return 0 if frame.empty else int(frame["Preempted"].sum())


def _metric(results, name):
    frame = results["system"]
    return float(frame.loc[frame["Metric"] == name, "Value"].iloc[0])


CREW = [{"name": "Crew", "members": ["A", "B"]}]


def _case1(extra_jobs=(), e_extra=None, a=None, schedules=(), time_limit=None):
    """Case 1: A (cap 2) busy 0-4 and 0-10, B (cap 2) busy 0-6 by one 2-unit
    job, and E at 1 takes 2 of Crew {A, B} for 5. E waits 1-6 and is served by B
    (6-11); A never has two free units while E could use them."""
    jobs = [("a1", "A", 0, 4), ("a2", "A", 0, 10), ("b", "B", 0, 6, 2),
            ("e", "Crew", 1, 5, 2, e_extra or {}), *extra_jobs]
    return _run(jobs, [a or resource_record("A", 2), resource_record("B", 2)], CREW,
                schedules=schedules, time_limit=time_limit)


# ---------------------------------------------------------------- case 1
def test_1_a_set_seize_of_two_is_served_by_the_member_that_can_give_both():
    """E (q 2) holds nothing while it waits. A: a1 0-4 and a2 0-10 -> 2 seizes,
    busy 4 + 10 = 14 of 2 x 11 = 63.64 %. B: b (2 units, 0-6) and E (2 units,
    6-11) -> 4 seizes. Crew: one seize, 2 x 5 = 10 of 4 x 11 = 22.73 %. E waits
    1-6 (Time in Queue 5). Throughput times 4, 10, 6, 10 -> mean 7.5."""
    results = _case1()
    a, b, crew = _res(results, "A"), _res(results, "B"), _res(results, "Crew")
    assert a["Total Seizes"] == 2
    assert a["Usage % (snapshot)"] == pytest.approx(63.64, abs=0.01)
    assert b["Total Seizes"] == 4
    assert crew["Total Seizes"] == 1
    assert crew["Usage % (snapshot)"] == pytest.approx(22.73, abs=0.01)
    assert crew["Time in Queue Avg"] == pytest.approx(5.0)
    assert _entity(results, "e")["Exited At"] == pytest.approx(11.0)
    assert _metric(results, "Avg Throughput Time") == pytest.approx(7.5)


# ---------------------------------------------------------------- case 2
X = ("x", "A", 5, 1, 1, {"priority": 0})


def test_2_the_unit_a_cancelled_group_leaves_goes_to_the_next_in_the_same_instant():
    """X (q 1, priority 0) arrives on A at 5 behind the waiting group E (same
    priority; a waiting group blocks its member's queue). E is served by B at 6,
    its A request is cancelled, and the idle unit goes to X at 6 (D858), not at
    10 when a2 releases. X: 6-7, time 2."""
    results = _case1(extra_jobs=[X])
    assert _entity(results, "x")["Throughput Time"] == pytest.approx(2.0)
    assert _entity(results, "x")["Exited At"] == pytest.approx(7.0)
    assert _res(results, "A")["Total Seizes"] == 3


def test_2_a_better_ranked_request_takes_the_free_unit_at_once():
    """E at priority 1 ranks behind X (priority 0): X is granted on arrival at 5,
    time 1 (5-6). A busy 4 + 10 + 1 = 15 of 22 = 68.18 %."""
    results = _case1(extra_jobs=[X], e_extra={"priority": 1})
    assert _entity(results, "x")["Throughput Time"] == pytest.approx(1.0)
    assert _res(results, "A")["Usage % (snapshot)"] == pytest.approx(68.18, abs=0.01)
    assert _entity(results, "e")["Exited At"] == pytest.approx(11.0)


def test_2_a_lifo_member_serves_the_later_arrival_first():
    """A on LIFO, both at priority 0: X arrived later than E, so it ranks ahead
    and is granted at 5 (time 1)."""
    lifo = resource_record("A", 2)
    lifo["discipline"] = "LIFO"
    results = _case1(extra_jobs=[X], a=lifo)
    assert _entity(results, "x")["Throughput Time"] == pytest.approx(1.0)


# ---------------------------------------------------------------- case 3
def test_3_a_blocker_takes_the_free_unit_a_waiting_group_does_not_hold():
    """A's capacity schedule drops it from 2 to 1 at 5 (points are "value until
    time"). The group holds no unit of A, so the drop is on time: Scheduled
    Capacity Avg = (5 x 2 + 6 x 1) / 11 = 16/11 (today 17/11). A's capacity in
    force (1) stays below q, so E is served by B at 6: B has 4 seizes."""
    drop = resource_record("A", 2)
    drop["capacity_schedule"] = "drop"
    results = _case1(a=drop, time_limit=11,
                     schedules=[{"name": "drop", "kind": "capacity",
                                 "points": [[5, 2], [100, 1]]}])
    assert _res(results, "A")["Scheduled Capacity Avg"] == pytest.approx(16 / 11, abs=1e-3)
    assert _res(results, "B")["Total Seizes"] == 4
    assert _res(results, "Crew")["Time in Queue Avg"] == pytest.approx(5.0)


# ---------------------------------------------------------------- case 4
def _renege_case(patience):
    """Case 1 with E reneging after *patience*; the second output of E goes to
    its own DISPOSE."""
    jobs = [("a1", "A", 0, 4), ("a2", "A", 0, 10), ("b", "B", 0, 6, 2)]
    blocks, links = [], []
    for spec in jobs:
        jb, jl = _job(*spec[:4], *spec[4:5])
        blocks += jb
        links += jl
    arrive = create("e_in", max_arrivals=1, start_time=1, distribution="Constant",
                    dist_params={"value": 1000.0}, e_type="e")
    work = process("e", resource="Crew", value=5.0, quantity=2, renege_after=patience)
    done, left = dispose("e_out"), dispose("e_left")
    blocks += [arrive, work, done, left]
    links += [{"source": "e_in", "target": "e"}, {"source": "e", "target": "e_out"},
              {"source": "e", "target": "e_left"}]
    model = layout(blocks, links, resources=[resource_record("A", 2), resource_record("B", 2)],
                   resource_sets=CREW,
                   simulation={"time_limit": None, "warmup": 0, "replications": 1})
    return run_model(model, replications=1, run_length=1000, warmup=0)


def test_4_a_group_that_reneges_leaves_no_trace_on_the_members():
    """Patience 4 -> E leaves at 1 + 4 = 5 without ever holding a unit. A: 2
    seizes (today 3). The run ends at 10 (a2): A busy 4 + 10 = 14 of 20 = 70 %
    (today 15)."""
    results = _renege_case("4")
    assert _queue_stat(results, "e", "Reneged") == 1
    assert _entity(results, "e")["Exited At"] == pytest.approx(5.0)
    a = _res(results, "A")
    assert a["Total Seizes"] == 2
    assert a["Usage % (snapshot)"] == pytest.approx(70.0, abs=0.01)
    assert _res(results, "Crew")["Total Seizes"] == 0


def test_4_a_grant_at_the_expiry_instant_wins_over_the_renege():
    """Patience 5 expires at 6, the instant B frees (D54): E is served by B at
    6 and ends at 11. Reneged 0."""
    results = _renege_case("5")
    assert _queue_stat(results, "e", "Reneged") == 0
    assert _entity(results, "e")["Exited At"] == pytest.approx(11.0)
    assert _res(results, "B")["Total Seizes"] == 4


# ---------------------------------------------------------------- case 5 / #1282
def test_5_preempting_a_unit_whose_holder_still_waits_no_longer_crashes_a_set_seize():
    """#1282. A cap 2, B cap 1, Crew {A, B}. j holds 1 unit of A 0-10. x (q 2 of
    Crew, priority 1) arrives at 1 and waits holding nothing (B is too small).
    p (priority 0, preempt) arrives at 2 and takes A's free unit at once, with
    nobody to preempt: 2-3. x gets both units of A at 10 and ends at 15.
    Times: j 10, p 1, x 14. A seizes: j 1, p 1, x 2 = 4. A busy 10 + 1 + 2 x 5
    = 21 of 2 x 15 = 70 %."""
    results = _run([("j", "A", 0, 10, 1, {"priority": 1}),
                    ("x", "Crew", 1, 5, 2, {"priority": 1}),
                    ("p", "A", 2, 1, 1, {"priority": 0, "preempt": True})],
                   [resource_record("A", 2), resource_record("B", 1)], CREW)
    assert _entity(results, "j")["Throughput Time"] == pytest.approx(10.0)
    assert _entity(results, "p")["Throughput Time"] == pytest.approx(1.0)
    assert _entity(results, "x")["Throughput Time"] == pytest.approx(14.0)
    assert _preempted(results) == 0
    a = _res(results, "A")
    assert a["Total Seizes"] == 4
    assert a["Usage % (snapshot)"] == pytest.approx(70.0, abs=0.01)


def test_5_a_plain_two_unit_seize_holds_nothing_while_it_waits_either():
    """#1282, plain resource (D857). A cap 2: j (q 1, priority 1) 0-10, x (q 2,
    priority 1) at 1, p (q 1, priority 0, preempt) at 2. x holds nothing, so p
    takes the free unit (2-3) and preempts nobody; x gets both units at 10 and
    ends at 15 (time 14)."""
    results = _run([("j", "A", 0, 10, 1, {"priority": 1}),
                    ("x", "A", 1, 5, 2, {"priority": 1}),
                    ("p", "A", 2, 1, 1, {"priority": 0, "preempt": True})],
                   [resource_record("A", 2)])
    assert _entity(results, "p")["Throughput Time"] == pytest.approx(1.0)
    assert _entity(results, "x")["Throughput Time"] == pytest.approx(14.0)
    assert _preempted(results) == 0
    assert _res(results, "A")["Total Seizes"] == 4


# ---------------------------------------------------------------- case 6 / #1284
def test_6_a_granted_group_is_a_victim_and_its_resume_is_not_a_new_seize():
    """#1284. A and B cap 2; a q 2 job holds B 0-20. x takes 2 of Crew (delay
    10, priority 1, resume) and gets A at 0. p (q 1, priority 0, preempt, delay
    1) arrives on A at 4: x is interrupted once, its group is granted again at
    5, and it ends at 5 + 6 = 11. A busy 2 x 4 + 1 + 2 x 6 = 21 of 2 x 20 =
    52.5 % (today 22, 55.0). A seizes: x 2 + p 1 = 3 (today 5, the resume was
    counted again). With cost_per_use 1, A's Usage Cost (3) equals the sum of
    the entity costs."""
    a = resource_record("A", 2)
    a["cost_per_use"] = 1.0
    results = _run([("bb", "B", 0, 20, 2),
                    ("x", "Crew", 0, 10, 2, {"priority": 1, "on_preempt": "resume"}),
                    ("p", "A", 4, 1, 1, {"priority": 0, "preempt": True})],
                   [a, resource_record("B", 2)], CREW)
    row = _res(results, "A")
    assert _entity(results, "x")["Exited At"] == pytest.approx(11.0)
    assert row["Total Seizes"] == 3
    assert row["Usage % (snapshot)"] == pytest.approx(52.5, abs=0.01)
    assert row["Usage Cost"] == pytest.approx(3.0)
    assert float(results["entity_log"]["Cost"].sum()) == pytest.approx(row["Usage Cost"])


# ---------------------------------------------------------------- case 7 / #1283
def test_7_a_two_unit_preemptor_interrupts_a_two_unit_job_once():
    """#1283. A cap 2. v (q 2, priority 1, resume, delay 10) from 0; p (q 2,
    priority 0, preempt, delay 1) at 1 is granted both units at 1 and ends at
    2; v was preempted once (one unit of time done) and ends at 2 + 9 = 11. A
    seizes: v 2 + p 2 = 4 (the resume is not counted). Busy 2 + 2 + 18 = 22 of
    2 x 11: 100 %."""
    results = _run([("v", "A", 0, 10, 2, {"priority": 1, "on_preempt": "resume"}),
                    ("p", "A", 1, 1, 2, {"priority": 0, "preempt": True})],
                   [resource_record("A", 2)])
    assert _entity(results, "p")["Exited At"] == pytest.approx(2.0)
    assert _entity(results, "v")["Exited At"] == pytest.approx(11.0)
    assert _preempted(results) == 1
    a = _res(results, "A")
    assert a["Total Seizes"] == 4
    assert a["Usage % (snapshot)"] == pytest.approx(100.0, abs=0.01)


def test_7_a_group_preemptor_that_cannot_free_enough_units_waits():
    """A cap 2: h (q 1, priority 0, delay 10) and v (q 1, priority 1, delay 10)
    from 0. p (q 2, priority 0, preempt) at 1 needs 2 units; only v has a worse
    key (h ties), so the victims cannot cover the need and nobody is
    preempted. p is granted both units at 10 and ends at 11; v ends at 10."""
    results = _run([("h", "A", 0, 10, 1, {"priority": 0}),
                    ("v", "A", 0, 10, 1, {"priority": 1}),
                    ("p", "A", 1, 1, 2, {"priority": 0, "preempt": True})],
                   [resource_record("A", 2)])
    assert _preempted(results) == 0
    assert _entity(results, "v")["Exited At"] == pytest.approx(10.0)
    assert _entity(results, "p")["Exited At"] == pytest.approx(11.0)


# ---------------------------------------------------------------- case 8 / #1280, #1281
def _race(env, extra_waits):
    """A and B cap 1, each held 0-5 by a bare request; B's holder waits
    *extra_waits* zero-time events after 5 before releasing. E seizes one unit
    of the set S at 1 through a real Seize block and gets A at 5."""
    manager = env.ctx.resource_manager
    a = manager.register("A", capacity=1)
    b = manager.register("B", capacity=1)
    manager.register_set("S", ["A", "B"])

    def holder(resource, waits):
        request = resource.request()
        yield request
        yield env.timeout(5)
        for _ in range(waits):
            yield env.timeout(0)
        resource.release(request)

    env.process(holder(a, 0))
    env.process(holder(b, extra_waits))

    class Recorder:
        block = Block(position=(9, 9), type="DISPOSE", name="rec")
        next_process = []

        def self_run(self, entity):
            pass

    seize = Seize(env, Block(position=(0, 0), type="SEIZE", name="seize_e"))
    release = Release(env, Block(position=(1, 0), type="RELEASE", name="release_e"))
    seize.set_params_from_dict({"resource_name": "S", "priority": 0, "quantity": 1,
                                "distribution": "Constant", "dist_params": {"value": 1}})
    release.set_params_from_dict({"resource_name": "S", "distribution": "Constant",
                                  "dist_params": {"value": 0}})
    seize.next_process = [release]
    release.next_process = [Recorder()]

    def feed():
        yield env.timeout(1)
        seize.self_run(Entity())

    env.process(feed())
    env.run()
    return a, b


@pytest.mark.parametrize("extra_waits", [2, 0])
def test_8_a_losing_grant_leaves_no_trace_on_busy_or_seizes(env, extra_waits):
    """#1280 (2 extra waits: the loser's grant is not yet processed when E
    cancels it; today B's busy level ends at -1) and #1281 (0 extra waits: the
    grant is processed; today B counts 2 seizes). D860: B's busy level is 0 and
    its only seize is the holder's."""
    a, b = _race(env, extra_waits)
    assert b.busy.level == 0
    assert b.seize_count == 1
    assert a.busy.level == 0
    assert a.seize_count == 2      # its holder, then E


# ---------------------------------------------------------------- #1281 at model level
def test_1281_cost_is_conserved_between_the_resources_and_the_entities():
    """Q = 1 set {A, B} (cap 1, cost_per_use 1), both held 0-5 by plain jobs;
    E at 1 takes one unit of the set. Both members come free at 5; the loser's
    grant leaves no trace, so the seizes are 2 holders + E = 3, the resources'
    Usage Cost sums to 3 and equals the sum of the entity costs."""
    a, b = resource_record("A", 1), resource_record("B", 1)
    a["cost_per_use"] = b["cost_per_use"] = 1.0
    results = _run([("ha", "A", 0, 5), ("hb", "B", 0, 5), ("e", "Crew", 1, 3)], [a, b], CREW)
    summary = results["resource_summary"]
    members = summary[summary["Resource"].isin(["A", "B"])]
    assert int(members["Total Seizes"].sum()) == 3
    assert float(members["Usage Cost"].sum()) == pytest.approx(3.0)
    assert float(results["entity_log"]["Cost"].sum()) == pytest.approx(3.0)


# ---------------------------------------------------------------- case 9
def test_9_a_plain_two_unit_seize_on_a_free_resource_is_granted_at_once():
    """A cap 2 is free at 3: the q 2 job is granted both units in that instant
    (no wait), 3-5, 2 seizes."""
    results = _run([("w", "A", 3, 2, 2)], [resource_record("A", 2)])
    assert _entity(results, "w")["Throughput Time"] == pytest.approx(2.0)
    assert _res(results, "A")["Total Seizes"] == 2
    assert _res(results, "A")["Time in Queue Avg"] == pytest.approx(0.0)


def test_9_a_set_seize_of_two_with_a_free_member_is_granted_at_once():
    """Crew {A, B} (cap 2) both free: the q 2 job is granted at its arrival."""
    results = _run([("w", "Crew", 3, 2, 2)], [resource_record("A", 2), resource_record("B", 2)],
                   CREW)
    assert _entity(results, "w")["Throughput Time"] == pytest.approx(2.0)
    assert _res(results, "Crew")["Time in Queue Avg"] == pytest.approx(0.0)
    assert _res(results, "Crew")["Total Seizes"] == 1
