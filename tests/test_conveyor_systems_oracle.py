"""Independent oracle for Phase 6D W3 M6 (Conveyor systems),
docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md ("M6 Conveyor systems" and
"Common rules"), ARCHITECTURE.md 5.21 (CONVEYOR: capacity = floor(length /
spacing), travel = length / speed, entries >= spacing / speed apart,
accumulating vs non-accumulating, `exit_resource`) and 5.22 (the D78 blocking
token).

Every expected number below is derived BY HAND from the spec text, never by
running the implementation (M6 is being built by a concurrent agent):
failures naming a missing attribute/column, or a numeric mismatch, are
expected until it lands, and a mismatch may equally be this oracle's own
wire-format guess -- the coordinator reconciles, as it did for M1/M2
(DEVLOG 2026-09-25 rows).

Belt-to-belt back-pressure (M6, reusing D78): a belt's end part is accepted
onto the next belt the instant that belt's own cell `Space` has room
(`Space.take`, built on a `simpy.Resource`) -- exactly like a PROCESS
blocking on a finite BUFFER (5.22). The upstream belt's own cell is released
at that same instant; if the next belt has no room, the upstream belt's end
part holds (accumulating: packs followers behind it, per the existing
single-belt exit_resource mechanism in `core/blocks/conveyor.py`;
non-accumulating: the whole belt's shared clock stops, `_stop_belt`,
freezing every rider regardless of destination). Two belts feeding one merge
for free because `Space` is a `simpy.Resource`: FIFO by the instant each
upstream belt's part actually blocked (5.22, "several machines blocked on
one buffer are served FIFO by the instant each one blocked"). A DECIDE right
after a belt is transparent (5.22: ASSIGN/RECORD/DECIDE/STATION/LOGIC take no
time and hold nothing), so its condition is evaluated per entity, and an
accumulating belt's back-pressure therefore applies only to entities whose
own chosen branch is full -- a non-accumulating belt has no such
per-entity view, since stopping the belt's clock is a single shared switch.

Wire format taken from `tests/test_network.py::_conveyor_model` and the
belt-backpressure tests below it (CONVEYOR params: `length`, `speed`,
`spacing`, `accumulating`, `exit_resource`, `from_node`, `to_node`, all
required even when empty) and `conveyor_summary`'s columns
(`nagasim/core/outputs.py::summarize_conveyors`): Run, Conveyor, Length,
Capacity, Entered, Exited, "Avg Occupancy", "Max Occupancy", "Blocked %",
"Avg Transit Time". `entity_log`'s per-entity columns (`summarize_entities`):
Type, "Created At", "Exited At".

Horizon (DEVLOG #32): `observed_window` uses the time limit only when events
remain pending past it, else the drain time. Cases 1 and 4 never drain (an
entity is stuck forever), so they need an explicit int `time_limit`; a
"hog" process that seizes a resource forever guarantees a real pending event
past that limit (its own long release timeout), so no keeper CREATE/DISPOSE
pair is needed. Cases 2, 3 and 5 drain naturally.
"""
import math

import pandas as pd
import pytest

from conftest import create, dispose, layout, process

from nagasim import api


def belt(name, length, speed, spacing, accumulating=True, exit_resource="", position=(1, 0)):
    return {"name": name, "description": name, "type": "CONVEYOR", "position": list(position),
            "params": {"length": length, "speed": speed, "spacing": spacing,
                       "accumulating": accumulating, "exit_resource": exit_resource,
                       "from_node": "", "to_node": ""}}


def decide(name, conditions, position=(1, 0)):
    return {"name": name, "description": name, "type": "DECIDE", "position": list(position),
            "params": {"mode": "Condition", "conditions": conditions, "probs": []}}


def hog_pair(resource="Hog", value=1000.0):
    """A separate stream that seizes *resource* forever from t=0 -- the same
    pattern `test_network.py::test_a_non_accumulating_belt_stops_...` uses to
    hold a resource unavailable for a whole run. `value` (1000) is far beyond
    any `time_limit` used here, so the process's own release timeout is a
    real pending event guaranteeing DEVLOG #32's "events remain pending past
    the limit" -- no keeper CREATE/DISPOSE pair needed."""
    src = create(name="hog_src", start_time=0, max_arrivals=1)
    proc = process(name="hog", resource=resource, value=value)
    return [src, proc], [{"source": "hog_src", "target": "hog"}]


def conveyor_row(results, name):
    return results["conveyor_summary"].set_index("Conveyor").loc[name]


# -- Case 1: belt -> belt, downstream permanently full -----------------------

def test_belt_feeding_a_permanently_full_belt_blocks_upstream():
    """belt_A (length 6, speed 1, spacing 3: capacity 2, travel 6, rhythm 3)
    feeds belt_B (length 3, spacing 3: capacity 1, travel 3, rhythm 3), whose
    `exit_resource` "Hog" is held forever by an unrelated process from t=0.
    Three entities offered at t=0, 1, 2 (gap 1).

    Hand trace:
      E1 boards A at 0 (rhythm), rides 6, reaches A's end at 6; B is empty ->
        accepted onto B at 6 (B is full from this instant on: capacity 1).
      E2 boards A at 1 -> rhythm slot 3 (A's own rhythm), reaches A's end at
        3 + 6 = 9; B has been full since 6 -> E2 BLOCKS at A's end at t=9.
      E3's `space.take` on A queues at t=2 (both A cells taken: E1 until 6,
        E2 from 3 on) and is granted the instant E1 vacates, at t=6; rhythm
        slot 6 -> boards at 6, raw ride would finish at 12, but E2 (ahead of
        it, never clears) blocks the "ahead" wait forever -> E3 packs behind
        E2 and never crosses A within the run.
      E1 on B: boards at 6, raw ride finishes at 9, tries "Hog" (never free)
        -> B blocks at t=9 too.
    So both belts' `blocked` flag turns on at t=9 and never turns off. With
    `time_limit=20`, Blocked % = 100*(20-9)/20 = 55 on both. Only E1 ever
    fully crosses A (in 6) or B (never -- it is stuck at B's own exit), so
    A's "Avg Transit Time" is exactly 6 (one sample) and B's has none (NaN).
    """
    hog_blocks, hog_links = hog_pair()
    blocks = [create(max_arrivals=3, dist_params={"value": 1.0}, start_time=0),
              belt("belt_A", 6.0, 1.0, 3.0),
              belt("belt_B", 3.0, 1.0, 3.0, exit_resource="Hog"),
              dispose(), *hog_blocks]
    links = [{"source": "create_1", "target": "belt_A"},
             {"source": "belt_A", "target": "belt_B"},
             {"source": "belt_B", "target": "dispose_1"},
             *hog_links, {"source": "hog", "target": "dispose_1"}]
    model = layout(blocks, links, resources=[{"name": "Hog", "capacity": 1}],
                   simulation={"time_limit": 20, "warmup": 0, "replications": 1})
    results = api.run_model(model, replications=1)

    row_a = conveyor_row(results, "belt_A")
    row_b = conveyor_row(results, "belt_B")
    assert row_a["Length"] == 6.0 and row_a["Capacity"] == 2
    assert row_b["Length"] == 3.0 and row_b["Capacity"] == 1
    assert row_a["Entered"] == 3 and row_a["Exited"] == 1
    assert row_b["Entered"] == 1 and row_b["Exited"] == 0
    assert row_a["Blocked %"] == pytest.approx(100 * (20 - 9) / 20)
    assert row_b["Blocked %"] == pytest.approx(100 * (20 - 9) / 20)
    assert row_a["Avg Transit Time"] == pytest.approx(6.0)
    assert math.isnan(row_b["Avg Transit Time"])


# -- Case 2: free-running series, no blocking ---------------------------------

def test_two_free_running_belts_in_series_just_add_their_travel_times():
    """No `exit_resource` anywhere and a big gap (10) between the two
    entities, so neither belt's capacity (1 each) or entry rhythm ever makes
    one entity wait for the other: belt_A (length 5, travel 5) then belt_B
    (length 3, travel 3). Each entity's system time is exactly 5 + 3 = 8, and
    neither belt ever reports being blocked -- the M6 back-pressure path is
    simply never taken when there is room."""
    blocks = [create(max_arrivals=2, dist_params={"value": 10.0}, start_time=0),
              belt("belt_A", 5.0, 1.0, 5.0), belt("belt_B", 3.0, 1.0, 3.0), dispose()]
    links = [{"source": "create_1", "target": "belt_A"},
             {"source": "belt_A", "target": "belt_B"},
             {"source": "belt_B", "target": "dispose_1"}]
    results = api.run_model(layout(blocks, links), replications=1)

    entities = results["entity_summary"].iloc[0]
    assert entities["Min Time"] == pytest.approx(8.0)
    assert entities["Max Time"] == pytest.approx(8.0)
    row_a, row_b = conveyor_row(results, "belt_A"), conveyor_row(results, "belt_B")
    assert row_a["Entered"] == 2 and row_a["Exited"] == 2 and row_a["Blocked %"] == 0.0
    assert row_b["Entered"] == 2 and row_b["Exited"] == 2 and row_b["Blocked %"] == 0.0
    assert row_a["Avg Transit Time"] == pytest.approx(5.0)
    assert row_b["Avg Transit Time"] == pytest.approx(3.0)


# -- Case 3: merge, FIFO by blocking instant ----------------------------------

def test_a_merge_admits_whichever_blocked_belt_blocked_first():
    """belt_A (e_type "A", length 2: travel 2) and belt_C (e_type "C", length
    5: travel 5) both feed belt_B (length 6, spacing 6: capacity 1, so
    travel == rhythm == 6 -- a belt this shape never adds its own extra
    rhythm delay, since the cell frees exactly `belt.travel_time` after each
    occupant boards, matching `_next_entry`'s own cadence).

    E0 (via A, created 0) reaches B's door at 2; B is empty -> boards
      immediately, occupies B until 2+6=8.
    Ea (via A, created 0.5) reaches A's end at 4 (A's rhythm delays it to
      board A at 2, +2 travel); B is full (until 8) -> Ea BLOCKS at t=4.
    Ec (via C, created 0) reaches C's end at 5; B is still full -> Ec BLOCKS
      at t=5 -- one instant after Ea, even though C's own belt is longer.
    B frees at 8 (E0 done); per 5.22, `Space` grants its queue FIFO by
    blocking instant, so Ea (blocked since 4) boards before Ec (since 5),
    occupying B 8->14; B frees again at 14 and Ec boards, 14->20.
    So Ea disposes at 14 and Ec at 20 -- Ea first, despite C's own belt
    taking longer to cross than A's, because "FIFO" means by blocking
    instant, not by arrival order or belt identity.
    """
    blocks = [create(name="create_A", e_type="A", max_arrivals=2,
                      dist_params={"value": 0.5}, start_time=0),
              create(name="create_C", e_type="C", max_arrivals=1, start_time=0),
              belt("belt_A", 2.0, 1.0, 2.0), belt("belt_C", 5.0, 1.0, 5.0),
              belt("belt_B", 6.0, 1.0, 6.0), dispose()]
    links = [{"source": "create_A", "target": "belt_A"},
             {"source": "create_C", "target": "belt_C"},
             {"source": "belt_A", "target": "belt_B"},
             {"source": "belt_C", "target": "belt_B"},
             {"source": "belt_B", "target": "dispose_1"}]
    results = api.run_model(layout(blocks, links), replications=1)

    log = results["entity_log"]
    e0 = log[(log["Type"] == "A") & (log["Created At"] == 0)].iloc[0]
    ea = log[(log["Type"] == "A") & (log["Created At"] == 0.5)].iloc[0]
    ec = log[log["Type"] == "C"].iloc[0]
    assert e0["Exited At"] == pytest.approx(8.0)
    assert ea["Exited At"] == pytest.approx(14.0)
    assert ec["Exited At"] == pytest.approx(20.0)
    assert ea["Exited At"] < ec["Exited At"]

    row_a, row_c, row_b = (conveyor_row(results, n) for n in ("belt_A", "belt_C", "belt_B"))
    assert row_a["Entered"] == 2 and row_a["Exited"] == 2
    assert row_c["Entered"] == 1 and row_c["Exited"] == 1
    assert row_b["Entered"] == 3 and row_b["Exited"] == 3 and row_b["Blocked %"] == 0.0
    # A is blocked 4->8 (4 of 20), C is blocked 5->14 (9 of 20).
    assert row_a["Blocked %"] == pytest.approx(100 * 4 / 20)
    assert row_c["Blocked %"] == pytest.approx(100 * 9 / 20)
    assert row_a["Avg Transit Time"] == pytest.approx((2.0 + 6.0) / 2)   # E0: 2, Ea: 8-2=6
    assert row_c["Avg Transit Time"] == pytest.approx(14.0)
    assert row_b["Avg Transit Time"] == pytest.approx(6.0)              # every rider: pure travel_B


# -- Case 4: divert after a belt ----------------------------------------------

def _divert_model(accumulating):
    """filler (its own stream) fills belt_full (capacity 1) and immediately
    sticks at its `exit_resource` "Hog" (held forever) from t=2 on, so
    belt_full is full for the whole run from t=0. main_src (start_time=5,
    gap 1) offers two entities onto belt_A (length 6, spacing 3: capacity 2,
    travel 6, rhythm 3) -> DECIDE (`entity.id % 2 == 1`) -> belt_full /
    belt_free (length 2, travel 2, free-running).

    Two entities precede main_src's (filler + hog's own dummy, both at t=0),
    so main_src's entities are ids 3 and 4: mainE1 (id 3, odd, created 5)
    routes to belt_full; mainE2 (id 4, even, created 6) routes to belt_free.
    """
    hog_blocks, hog_links = hog_pair()
    blocks = [create(name="filler_src", start_time=0, max_arrivals=1),
              belt("belt_full", 2.0, 1.0, 2.0, exit_resource="Hog"),
              create(name="main_src", start_time=5, max_arrivals=2, dist_params={"value": 1.0}),
              belt("belt_A", 6.0, 1.0, 3.0, accumulating=accumulating),
              decide("route", ["entity.id % 2 == 1"]),
              belt("belt_free", 2.0, 1.0, 2.0),
              dispose(), *hog_blocks]
    links = [{"source": "filler_src", "target": "belt_full"},
             {"source": "main_src", "target": "belt_A"},
             {"source": "belt_A", "target": "route"},
             {"source": "route", "target": "belt_full"},
             {"source": "route", "target": "belt_free"},
             {"source": "belt_full", "target": "dispose_1"},
             {"source": "belt_free", "target": "dispose_1"},
             *hog_links, {"source": "hog", "target": "dispose_1"}]
    return layout(blocks, links, resources=[{"name": "Hog", "capacity": 1}],
                  simulation={"time_limit": 25, "warmup": 0, "replications": 1})


def test_an_accumulating_belt_packs_a_follower_behind_a_part_blocked_at_a_divert():
    """mainE1 reaches belt_A's end at 5+6=11, is routed to belt_full (full
    since t=2) and blocks there. mainE2, boarding at 8 (A's rhythm), would be
    routed to belt_free, but a belt is FIFO: it packs behind mainE1 and never
    reaches the divert (spec M6: "a full chosen branch blocks the belt").
    Coordinator ruling: the original premise here -- that mainE2 passes the
    stuck part -- came from a wrong line in the coordinator's brief, not the
    spec; corrected to the spec's reading."""
    results = api.run_model(_divert_model(True), replications=1)

    row_full = conveyor_row(results, "belt_full")
    row_a = conveyor_row(results, "belt_A")
    row_free = conveyor_row(results, "belt_free")
    assert row_full["Entered"] == 1 and row_full["Exited"] == 0
    assert row_full["Blocked %"] == pytest.approx(100 * (25 - 2) / 25)
    assert row_a["Entered"] == 2 and row_a["Exited"] == 0
    assert row_a["Blocked %"] == pytest.approx(100 * (25 - 11) / 25)
    assert row_free["Entered"] == 0 and row_free["Exited"] == 0

    log = results["entity_log"]
    main2 = log[log["Created At"] == 6].iloc[0]
    assert pd.isna(main2["Exited At"])


def test_a_non_accumulating_belt_stops_everyone_regardless_of_branch():
    """Same model, `accumulating=False` on belt_A. mainE1 still blocks at
    t=11 (unaffected -- the block happens at DECIDE, downstream of A's own
    ride), but stopping belt_A now freezes its shared clock for EVERY rider:
    mainE2 is 3 of its 6 units into its ride at t=11 (boarded at 8) and
    simply never finishes it, so it never even reaches DECIDE -- unlike the
    accumulating case, its branch being free does not matter, because a
    stopped belt has no per-entity view at all."""
    results = api.run_model(_divert_model(False), replications=1)

    row_a = conveyor_row(results, "belt_A")
    row_free = conveyor_row(results, "belt_free")
    assert row_a["Entered"] == 2 and row_a["Exited"] == 0
    assert row_a["Blocked %"] == pytest.approx(100 * (25 - 11) / 25)
    assert row_free["Entered"] == 0 and row_free["Exited"] == 0

    log = results["entity_log"]
    main2 = log[log["Created At"] == 6].iloc[0]
    assert pd.isna(main2["Exited At"])


# -- Case 5: no belt-to-belt link -- regression guard -------------------------

def test_a_single_free_running_belt_is_unaffected_by_the_m6_change():
    """No belt feeds another belt here, so M6 must leave this exactly as
    ARCHITECTURE 5.21's plain arithmetic always gave it (the T7/E1
    precedent): length 12, speed 2, spacing 3 -> capacity floor(12/3)=4,
    travel 12/2=6, rhythm 3/2=1.5. Four entities offered every 0.5 (faster
    than the rhythm) board at 0, 1.5, 3.0, 4.5 and all exit 6 later: 6, 7.5,
    9.0, 10.5. System time is exit - creation, so 6, 7, 8, 9 -- min 6, max 9,
    avg 7.5."""
    blocks = [create(max_arrivals=4, dist_params={"value": 0.5}, start_time=0),
              belt("belt_A", 12.0, 2.0, 3.0), dispose()]
    links = [{"source": "create_1", "target": "belt_A"}, {"source": "belt_A", "target": "dispose_1"}]
    results = api.run_model(layout(blocks, links), replications=1)

    row = conveyor_row(results, "belt_A")
    assert row["Length"] == 12.0 and row["Capacity"] == 4
    assert row["Entered"] == 4 and row["Exited"] == 4
    assert row["Blocked %"] == 0.0
    assert row["Avg Transit Time"] == pytest.approx(6.0)

    entities = results["entity_summary"].iloc[0]
    assert entities["Min Time"] == pytest.approx(6.0)
    assert entities["Max Time"] == pytest.approx(9.0)
    assert entities["Avg Time"] == pytest.approx(7.5)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
