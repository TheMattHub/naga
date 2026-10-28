"""State in for belts with points (Phase 16 E9, D918 to D924): an accumulating CONVEYOR with
`stops`, `diverts` and `merges` is carried exactly by a state, and what it cannot carry stays
refused by name (D234).

Written from the design alone (`docs/superpowers/specs/2026-10-27-warm-start-conveyors-design.md`,
section "Proof and tests"), before the build. The exit check is `test_state._carried`: the run
started from the state taken at t is the uninterrupted run with a warm-up of t, KPI for KPI, and
a second take at the clock gives the same hash. Every hand-derived time is in its test's
docstring: travel = distance / speed, a part reaches a point no sooner than spacing / speed
after the one before left it.
"""
import copy
import functools

import numpy as np
import pytest

from conftest import create, dispose, layout, problem_codes, process
from test_state import _block, _bundled, _carried, _kpis, _refusal

from nagasim import api, state
from nagasim.core.session import SimSession
from nagasim.core.trace import TraceRecorder

DUE = "an instant with an event due"
# t = 0.31 + 0.37 k (design, tests 3 to 6), rounded so each instant is the decimal written.
SWEEP = [round(0.31 + 0.37 * k, 2) for k in range(25)]
HORIZON = [round(0.31 + 0.37 * k, 2) for k in range(200) if 0.31 + 0.37 * k < 40]
# The design says "if a line as given does not meet a listed stage, the builder changes the
# line's numbers, not the assertion". As given, line S never blocks a part at a stop (stop 1
# lets a part through every 1.2 + 1 = 2.2, as fast as "load" at 2.0 empties the end), and
# line D's lane never fills (an A part every 2.6 against "unload" at 1.5), so "blocked" and the
# held handover never occur, and `when_full` "wait" and "pass" run the same. Hence:
LOAD, UNLOAD = 3.0, 6.0      # the design's 2.0 and 1.5
# DESIGN-AMBIGUITY: tests 3 to 5 sweep k < 25 (to 9.19), yet in the uninterrupted runs some of
# their listed stages first occur later: S "stop" from 14.4 and "blocked" from 32.4; D's held
# handover from 15.45; M "blocked" from 10.25. Read as: the fast tests add a few instants
# inside those stretches (from the uninterrupted runs' `belt_stage` trace, which no change of
# this task moves) to the design's sweep; test 6 sweeps all to the horizon.
EXTRA = {"S": [14.8, 17.4, 32.8, 35.8], "D": [16.5, 22.3],
         "M": [11.9, 12.5, 15.0, 18.3, 14.2, 14.9, 15.2]}    # the last three: the crowded line


def _belt(name="belt", *, length=10.0, spacing=1.0, exit_resource="", accumulating=True,
          **points):
    return _block(name, "CONVEYOR", length=length, speed=1.0, spacing=spacing,
                  accumulating=accumulating, exit_resource=exit_resource, from_node="",
                  to_node="", **points)


def _links(*pairs):
    return [{"source": a, "target": b} for a, b in pairs]


def _resources(*names):
    return [{"name": n, "capacity": 1} for n in names]


def _run(time_limit):
    return {"time_limit": time_limit, "warmup": 0, "replications": 1}


# ---------------------------------------------------------------- the three lines
def _stops_line(*, accumulating=True, start_time=0.25, second_hog_at=None):
    """Line S: 16 parts every 0.5 from 0.25 onto a belt of 10 (speed 1, spacing 1) whose end
    stop is "Stop"; stops at 2.5 (0.7) and 6.0 (1.2, seizes "Op"); "load" holds Stop LOAD
    after the belt. A hog (one part at 3.0) holds Op 3.0 to 7.0. Sections [0, 2.5), [2.5, 6),
    [6, 10) hold 3, 4 and 4 parts, lags 0.5, 0.5 and 0. *second_hog_at*: a second hog on Op
    (test 12)."""
    c = create("c", dist_params={"value": 0.5}, start_time=start_time, max_arrivals=16)
    belt = _belt(exit_resource="Stop", accumulating=accumulating,
                 stops=[{"at": 2.5, "time": 0.7}, {"at": 6.0, "time": 1.2, "resource": "Op"}])
    load = process("load", resource="Stop", value=LOAD, action="Delay Release")
    hog_src = create("hog_src", e_type="Hog", max_arrivals=1, start_time=3.0)
    hog = process("hog", resource="Op", value=4.0)
    blocks = [c, belt, load, dispose("d"), hog_src, hog, dispose("hog_out")]
    links = _links(("c", "belt"), ("belt", "load"), ("load", "d"), ("hog_src", "hog"),
                   ("hog", "hog_out"))
    if second_hog_at is not None:
        blocks += [create("hog2_src", e_type="Hog", max_arrivals=1, start_time=second_hog_at),
                   process("hog2", resource="Op", value=4.0)]
        links += _links(("hog2_src", "hog2"), ("hog2", "hog_out"))
    return layout(blocks, links, resources=_resources("Stop", "Op"), simulation=_run(40))


def _divert_line(when_full="wait", *, with_stop=False):
    """Line D: "a" (type A, every 1 from 0.25) and "b" (type B, every 1 from 0.75), 8 parts
    each, onto a belt of 10 with a divert at 4 (type A, push 0.6, `when_full` *when_full*).
    Link 0 (the end) goes to a DISPOSE, link 1 to a lane (length 2, 2 cells, its end seizes
    "Gate"), then "unload" (Gate, UNLOAD, "Delay Release") and a DISPOSE. *with_stop* adds a
    stop at 2 (time 0.5), so point 0 is that stop and point 1 the divert (test 16)."""
    a = create("a", e_type="A", dist_params={"value": 1.0}, start_time=0.25, max_arrivals=8)
    b = create("b", e_type="B", dist_params={"value": 1.0}, start_time=0.75, max_arrivals=8)
    points = {"diverts": [{"at": 4, "condition": 'entity.e_type == "A"', "time": 0.6,
                           "when_full": when_full}]}
    if with_stop:
        points["stops"] = [{"at": 2, "time": 0.5}]
    belt = _belt(**points)
    lane = _belt("lane", length=2.0, exit_resource="Gate")
    unload = process("unload", resource="Gate", value=UNLOAD, action="Delay Release")
    blocks = [a, b, belt, dispose("d"), lane, unload, dispose("d_lane")]
    links = _links(("a", "belt"), ("b", "belt"), ("belt", "d"), ("belt", "lane"),
                   ("lane", "unload"), ("unload", "d_lane"))
    return layout(blocks, links, resources=_resources("Gate"), simulation=_run(40))


def _merge_line(priority="belt", *, crowded=False):
    """Line M: 12 main parts every 1 from 0.25 onto a belt of 10 whose end stop is "Stop"
    ("load" holds it 1.6 after); 8 side parts every 1.5 from 0.5 through "side_m" (Side, 0.3),
    which merges them in at 5 with priority *priority*.

    *crowded* (the builder's change of the line, for the stage sweeps): as given, a part waits
    at a spacing before the merge for the gap behind the last part (`gap`) only when a point
    stands within a spacing before the merge (else the gap has passed when it gets there), and
    a second merging part waits at the door (`door`) only when "side_m" can finish two parts
    while one waits to join. So a stop at 4.5 (time 0.1) and Side of capacity 2; the merge is
    then point 1."""
    c = create("c", dist_params={"value": 1.0}, start_time=0.25, max_arrivals=12)
    side = create("side", e_type="Side", dist_params={"value": 0.6 if crowded else 1.5},
                  start_time=0.5,
                  max_arrivals=8)
    side_m = process("side_m", resource="Side", value=0.3)
    points = {"merges": [{"at": 5, "from": "side_m", "priority": priority}]}
    if crowded:
        points["stops"] = [{"at": 4.5, "time": 0.6}]
    belt = _belt(exit_resource="Stop", **points)
    load = process("load", resource="Stop", value=1.6, action="Delay Release")
    blocks = [c, side, side_m, belt, load, dispose("d")]
    links = _links(("c", "belt"), ("side", "side_m"), ("side_m", "belt"), ("belt", "load"),
                   ("load", "d"))
    resources = [{"name": "Stop", "capacity": 1}, {"name": "Side", "capacity": 2 if crowded else 1}]
    return layout(blocks, links, resources=resources, simulation=_run(40))


def _sweep(model, times):
    """Every take at *times* is carried exactly or refused as an instant with an event due;
    any other refusal or difference fails. Returns what the carried takes met: the belt
    stages, "held by X" and "held at X point P" (a handover)."""
    met, carried = set(), 0
    for at in times:
        try:
            taken = _carried(model, at)
        except state.StateError as exc:
            assert (exc.code, getattr(exc, "kind", None)) == ("E_STATE_UNSUPPORTED", DUE), \
                (at, exc.message)
            continue
        carried += 1
        for entry in taken.get("entities", []):
            if "belt" in entry:
                met.add(entry["belt"]["stage"])
            if "held_by" in entry:
                met.add("held by " + entry["held_by"])
                if "held_at" in entry:
                    met.add(f"held at {entry['block']} point {entry['held_at']['point']}")
    assert carried > 0
    return met


def _belts(doc):
    return [e["belt"] for e in doc["state"]["entities"] if "belt" in e]


# ---------------------------------------------------------------- 1, 13: a stop by hand
def _one_stop_line():
    """Two parts at 0 and 0.5 onto a belt of 6 (speed 1, spacing 1) with a stop at 3 (time 2,
    no resource), then a DISPOSE.
      P0 boards at 0, reaches the stop at 3, dwells 3..5, rides 3 to the end: 8.
      P1 boards at 1.0 (a spacing after P0), would reach the stop at 4.0; P0 leaves it at 5,
        so P1 moves up and reaches it at 5 + 1 = 6, dwells 6..8, rides 3 to the end: 11."""
    c = create("c", dist_params={"value": 0.5}, start_time=0.0, max_arrivals=2)
    belt = _belt(length=6.0, stops=[{"at": 3, "time": 2}])
    return layout([c, belt, dispose("d")], _links(("c", "belt"), ("belt", "d")),
                  simulation=_run(20))


def test_two_parts_at_a_stop_by_hand():
    model = _one_stop_line()
    early = state.take(model, 3.5)
    assert _belts(early) == [
        {"stage": "dwell", "until": 5.0, "entered": 0.0, "point": 0},
        {"stage": "ride", "until": 4.0, "entered": 1.0, "point": 0}]
    assert "blocks" not in early["state"]
    late = state.take(model, 5.5)
    assert _belts(late) == [
        {"stage": "ride", "until": 8.0, "entered": 0.0},
        {"stage": "move", "until": 6.0, "entered": 1.0, "point": 0}]
    assert late["state"]["blocks"]["belt"]["left"] == [5.0, None]
    for doc in (early, late):
        started, problems = state.start(model, doc)
        assert problems == []
        log = api.run_model(started)["entity_log"]
        assert sorted(log["Exited At"]) == [8.0, 11.0]


def test_the_end_of_a_dwell_is_an_instant_with_an_event_due():
    """P0's dwell ends at 5.0: its release and its request for room ahead are that event."""
    assert DUE in _refusal(_one_stop_line(), 5.0)


# ---------------------------------------------------------------- 2: a merge by hand
def _one_merge_line():
    """Three main parts every 1 from 0 onto a belt of 10 (speed 1, spacing 1) with a merge at
    5 from "side" (priority "belt"); "side" makes one part at 5.5.
      The mains pass the merge at 5, 6 and 7 and leave the end at 10, 11, 12.
      The side part is ready at 5.5; the last pass is 7, so it may join at 7 + 1 = 8, and it
      leaves at 8 + 5 = 13. Its merge wait is 8 - 5.5 = 2.5."""
    c = create("c", dist_params={"value": 1.0}, start_time=0.0, max_arrivals=3)
    side = create("side", e_type="Side", max_arrivals=1, start_time=5.5)
    belt = _belt(merges=[{"at": 5, "from": "side"}])
    return layout([c, side, belt, dispose("d")],
                  _links(("c", "belt"), ("side", "belt"), ("belt", "d")), simulation=_run(20))


def _side_entry(taken):
    (side,) = [e for e in taken["entities"] if e.get("e_type") == "SIDE"]
    return side


def test_a_merge_by_hand():
    model = _one_merge_line()
    doc = state.take(model, 7.5)
    side = _side_entry(doc["state"])
    assert side["belt"] == {"stage": "join", "until": 8.0, "point": 0}
    assert side["since"] == 5.5
    assert doc["state"]["blocks"]["belt"]["left"] == [7.0, None]
    started, problems = state.start(model, doc)
    assert problems == []
    results = api.run_model(started)
    log = results["entity_log"]
    assert sorted(log[log["Type"] != "SIDE"]["Exited At"]) == [10.0, 11.0, 12.0]
    assert list(log[log["Type"] == "SIDE"]["Exited At"]) == [13.0]
    row = results["conveyor_merges"].iloc[0]
    assert row["Merged"] == 1 and row["Avg Wait"] == pytest.approx(2.5)
    assert DUE in _refusal(model, 8.0)


def _merge_times(model, doc):
    started, problems = state.start(model, doc)
    assert problems == []
    recorder = TraceRecorder()
    api.run_model(started, replications=1, trace=recorder)
    return [e.t for e in recorder.events if e.kind == "belt_stage" and e.state == "merge"]


def test_a_resumed_join_wakes_at_the_carried_until():
    """D921, restore step 5: the first wait of a resumed `join` is the carried timer, not one
    re-timed from the clock. Edited by hand to 8.25 (still at or after 7 + 1), the join is at
    8.25. Assumption: an edited state is a new document (`state.document`), as a hand-written
    one would be."""
    model = _one_merge_line()
    doc = state.take(model, 7.5)
    assert _merge_times(model, doc) == [8.0]
    taken = copy.deepcopy(doc["state"])
    _side_entry(taken)["belt"]["until"] = 8.25
    edited = state.document(model, taken, source="run", at=7.5, seed=0)
    assert _merge_times(model, edited) == [8.25]


# ---------------------------------------------------------------- 3 to 6: every stage
def test_every_stage_of_a_belt_with_stops_is_carried_exactly():
    met = _sweep(_stops_line(), SWEEP + EXTRA["S"])
    assert met >= {"cell", "entry", "slot", "ride", "packed", "move", "due", "wait", "dwell",
                   "blocked", "stop"}


@pytest.mark.parametrize("when_full", ["wait", "pass"])
def test_every_stage_of_a_belt_with_a_divert_is_carried_exactly(when_full):
    met = _sweep(_divert_line(when_full), SWEEP + EXTRA["D"])
    assert met >= {"ride", "packed", "move", "push"}
    if when_full == "wait":
        assert {"held by belt", "held at lane point 0"} <= met


@pytest.mark.parametrize("priority", ["belt", "merge"])
def test_every_stage_of_a_belt_with_a_merge_is_carried_exactly(priority):
    met = _sweep(_merge_line(priority, crowded=True), SWEEP + EXTRA["M"])
    assert met >= {"ride", "packed", "gap", "claim", "blocked", "cell", "door", "join",
                   "held by side_m"}


@pytest.mark.slow
@pytest.mark.parametrize("model", [_stops_line(), _divert_line("wait"), _divert_line("pass"),
                                   _merge_line("belt"), _merge_line("merge")],
                         ids=["stops", "divert-wait", "divert-pass", "merge-belt", "merge-merge"])
def test_the_three_lines_are_carried_at_every_time_to_the_horizon(model):
    # Each line's last part leaves before 40 and the run ends there: a take after it is
    # "the end of the run", so the sweep stops at the run's own end (the builder's change).
    session = SimSession(model, 0)
    session.advance(to=40.0)
    _sweep(model, [at for at in HORIZON if at < session.now])


# ---------------------------------------------------------------- 7: the compared KPIs
@pytest.mark.parametrize("model, keys", [
    (_stops_line(), {"stop/belt/0/Parts", "stop/belt/1/Avg Wait"}),
    (_divert_line("wait"), {"divert/belt/0/Diverted", "divert/belt/0/Avg Wait"}),
    (_merge_line("belt"), {"merge/belt/0/Merged", "merge/belt/0/Avg Wait"})],
    ids=["stops", "divert", "merge"])
def test_the_compared_kpis_include_the_points(model, keys):
    """At the first carried take of the sweep, the KPIs the exactness check compares hold the
    points' keys (`experiments.flatten_kpis`, `<prefix>/<belt>/<index>/<column>`)."""
    for at in SWEEP:
        try:
            _carried(model, at)
        except state.StateError as exc:
            assert getattr(exc, "kind", None) == DUE, exc.message
            continue
        assert keys <= set(_kpis(api.run_model(model, warmup=at, replications=1)))
        assert keys <= set(_kpis(api.run_model(state.start(model, state.take(model, at))[0],
                                               replications=1)))
        return
    pytest.fail("no take of the sweep was carried")


# ---------------------------------------------------------------- 8, 9: a bundled warehouse
_WAREHOUSE_AT = (30.3, 95.1, 170.2, 333.3, 560.4, 901.3)


def _labelled_warehouse(**simulation):
    """`warehouse_forklifts` whose `takeaway_belt` (length 30 from its network, spacing 1.4,
    speed 12) gains a stop at 15.0 (time 2.5) that seizes "Labeller" (capacity 1)."""
    model = _bundled("warehouse_forklifts", **simulation)
    (belt,) = [b for b in model["blocks"] if b["name"] == "takeaway_belt"]
    belt["params"]["stops"] = [{"at": 15.0, "time": 2.5, "resource": "Labeller"}]
    model["resources"].append({"name": "Labeller", "capacity": 1})
    return model


@functools.lru_cache(maxsize=None)
def _warehouse_has_a_part_at_a_point() -> bool:
    model = _labelled_warehouse(deterministic=True)
    return any("point" in e.get("belt", {})
               for at in _WAREHOUSE_AT for e in state.take(model, at)["state"].get("entities", []))


@pytest.mark.parametrize("at", _WAREHOUSE_AT)
def test_the_warehouse_with_a_label_stop_is_carried_exactly(at):
    """Assumption: "at least one take has an entry with `point`" is checked over all six
    instants in each case (a cached sweep of plain takes), as a case cannot see the others."""
    _carried(_labelled_warehouse(deterministic=True), at)
    assert _warehouse_has_a_part_at_a_point()


@pytest.mark.parametrize("at", [61.7, 170.2, 455.5, 850.1])
def test_the_warehouse_with_a_label_stop_is_taken_again_the_same(at):
    model = _labelled_warehouse()
    doc = state.take(model, at)
    started, problems = state.start(model, doc)
    assert problems == [] and state.take(started, at)["hash"] == doc["hash"]


# ---------------------------------------------------------------- 10: in distribution
@pytest.mark.slow
def test_a_belt_with_stops_is_reproduced_in_distribution():
    """Line S with exponential arrivals (mean 0.6, no cap), stop times EXPO(0.7) and EXPO(1.2),
    to 400, taken at 150: as `test_a_belt_line_is_reproduced_in_distribution`, 30 seeds, each
    mean difference inside its 99% interval. Assumption: the KPIs compared are the belt
    line's three plus the Op stop's busy share. (The builder's change: its mean wait is 0
    after 150 in every seed, the hog having left Op at 7, so its interval has no width.)"""
    model = _stops_line()
    model["simulation"]["time_limit"] = 400
    model["blocks"][0]["params"].update(distribution="Exponential",
                                        dist_params={"rate": 1 / 0.6}, max_arrivals=0)
    stops = model["blocks"][1]["params"]["stops"]
    stops[0]["time"], stops[1]["time"] = "EXPO(0.7)", "EXPO(1.2)"
    kpis = ("entity/DEFAULT/Avg Time", "resource/Stop/Idle %",
            "system/Total Entities Completed", "stop/belt/1/Busy %")
    differences = {kpi: [] for kpi in kpis}
    for seed in range(30):
        try:
            doc = state.take(model, 150.0, seed=seed)
        except state.StateError as exc:     # an event due at 150 in this seed: none is
            assert getattr(exc, "kind", None) == DUE, exc.message
            continue
        warm, problems = state.start(model, doc)
        assert problems == []
        reference = _kpis(api.run_model(model, warmup=150.0, replications=1, seed=seed))
        started = _kpis(api.run_model(warm, replications=1, seed=1000 + seed))
        for kpi in kpis:
            differences[kpi].append(started[kpi] - reference[kpi])
    assert len(differences[kpis[0]]) >= 25
    for kpi, values in differences.items():
        mean, error = np.mean(values), np.std(values, ddof=1) / np.sqrt(len(values))
        assert abs(mean) <= 2.756 * error and error > 0, kpi


# ---------------------------------------------------------------- 11 to 15: refusals
def test_a_belt_with_points_under_a_plant_stays_refused_by_name():
    assert state.refused_kind(_stops_line()) == "a conveyor with stops"
    assert state.refused_kind(_divert_line()) == "a conveyor with diverts or merges"
    assert state.refused_kind(_merge_line()) == "a conveyor with diverts or merges"
    # 1.1: P0 rides to the first stop, P1 waits for its slot at 1.25; nothing is due.
    assert state.take(_stops_line(), 1.1)["state"]["entities"]


def test_a_stop_other_jobs_queue_for_is_refused():
    """Line S: P0 boards at 0.25, reaches stop 0 at 2.75, dwells to 3.45, rides 3.5 to stop 1
    at 6.95 and asks for Op, which the hog holds 3.0 to 7.0. A second hog asks for Op at 6.97,
    behind P0. At 6.98 P0 waits at the stop with another job queued for its resource. Written
    by hand, the same entry is refused on load; without the second hog it is accepted."""
    model = _stops_line(second_hog_at=6.97)
    assert "a stop on a belt that other jobs queue for, or that had a unit free" in \
        _refusal(model, 6.98)
    hog = {"block": "hog", "count": 1, "e_type": "HOG", "in_service": 1, "ends": [7.0],
           "since": 3.0}
    part = {"block": "belt", "count": 1, "since": 6.95,
            "belt": {"stage": "wait", "entered": 0.25, "point": 1}}
    model["initial_state"] = {"clock": 6.98, "entities": [hog, part]}
    assert problem_codes(model) == set()
    model["initial_state"]["entities"].append(
        {"block": "hog2", "count": 1, "e_type": "HOG", "since": 6.97})
    assert "E_INITIAL_BELT" in problem_codes(model)


def test_a_part_between_two_stages_of_a_belt_is_still_refused():
    """The guard (D921: every yield is staged, so a run never reaches it): a riding part whose
    record is set to a `cell` it is not queued for."""
    tap = state.Tap()
    session = SimSession(_stops_line(), 0, subscribers=(tap,))
    session.advance(to=1.1)
    riding = [e for e in session.ctx.live_entities
              if (getattr(e, "_belt", None) or ("",))[0] == "ride"]
    assert riding, "no part records a ride stage at 1.1"
    riding[0]._belt = ("cell", None, None, None)
    with pytest.raises(state.StateError) as caught:
        state._read(session, tap)
    assert caught.value.code == "E_STATE_UNSUPPORTED"
    assert getattr(caught.value, "kind", None) == "a part between two stages of a belt"


def test_a_non_accumulating_belt_with_stops():
    """With a part on it (P0 rides at 1.1) refused by the kept kind; empty (the first part at
    5, taken at 2) carried, as the refusal table says: "an EMPTY non-accumulating belt with points is now
    carried" says."""
    assert "a non-accumulating belt with parts on it" in \
        _refusal(_stops_line(accumulating=False), 1.1)
    _carried(_stops_line(accumulating=False, start_time=5.0), 2.0)


# ---------------------------------------------------------------- 16: validation
def _part(stage, *, block="belt", **fields):
    record = {"stage": stage}
    record.update({k: fields.pop(k) for k in ("until", "entered", "point") if k in fields})
    return {"block": block, "count": 1, "belt": record, **fields}


_DWELL0 = _part("dwell", until=5.5, entered=2.0, point=0)
# Op's stop (point 1): the dwelling part holds Op since it reached the stop (D432, D436).
# (made when it entered: #460 refuses a hold from before the part was made)
_DWELL1 = _part("dwell", until=5.5, entered=0.25, point=1, age=4.75,
                holds={"Op": {"since": 4.3}})
_RIDES0 = [_part("ride", until=t + 2.5, entered=t, point=0) for t in (2.6, 3.0, 4.0, 4.5)]
_JOIN = {"stage": "join", "until": 5.5, "point": 0}


def _handover(point):
    # DESIGN-AMBIGUITY: the design names `held_at` {point, since} for a divert handover but
    # not whether D430's `held_since` (when the part entered the belt in held_by) goes with it;
    # read as: it does, as at a belt end's handover.
    return {"block": "lane", "count": 1, "belt": {"stage": "cell"}, "held_by": "belt",
            "held_since": 1.25, "held_at": {"point": point, "since": 4.85}}


def _merging():
    # DESIGN-AMBIGUITY: a merging part "carries entry-level since" and "its source still
    # holds it (held_by)"; read as held_by the merge's source station, with no held_since
    # (optional for a station, D381).
    return {"block": "belt", "count": 1, "e_type": "SIDE", "belt": dict(_JOIN), "since": 4.8,
            "held_by": "side_m"}


@pytest.mark.parametrize("line, good, bad, blocks, code", [
    ("S", [_part("ride", until=6.0, entered=1.0, point=1)],
     [_part("ride", until=6.0, entered=1.0, point=2)], None, "E_INITIAL_BELT"),
    ("S", [_DWELL0], [_part("push", until=5.5, entered=2.0, point=0)], None, "E_INITIAL_BELT"),
    ("S", [_DWELL0], [_part("dwell", entered=2.0, point=0)], None, "E_INITIAL_BELT"),
    ("S", [_DWELL1], [_part("dwell", until=5.5, entered=0.25, point=1)], None,
     "E_INITIAL_BELT"),
    ("S", [_DWELL0], [_DWELL0, _part("dwell", until=5.5, entered=2.5, point=0)], None,
     "E_INITIAL_BELT"),
    ("D+stop", [_handover(1)], [_handover(0)], None, "E_INITIAL_BELT"),
    ("M", [_merging()], [_merging(), _merging()], None, "E_INITIAL_BELT"),
    ("S", _RIDES0[1:], _RIDES0, None, "E_INITIAL_BELT"),
    ("S", [_part("ride", until=6.0, entered=1.0, point=1)],
     [_part("ride", until=6.0, entered=1.0, point=1)], [4.5, None], "E_INITIAL_BLOCK"),
], ids=["point out of range", "push at a stop", "dwell without until",
        "dwell at Op's stop without holds", "two dwells at one stop", "held_at names a stop",
        "two joins at one merge", "more holders than places", "left of the wrong length"])
def test_a_bad_part_on_a_belt_with_points_is_refused(line, good, bad, blocks, code):
    """At clock 5.0, each case next to the good state it breaks (accepted, so the refusal is
    the case's own, not D733's blanket one). S: point 0 is the stop at 2.5, point 1 Op's stop
    at 6.0, section 0 holds 3. D+stop: point 0 a stop at 2, point 1 the divert. M: point 0 the
    merge. `left` of S has one value per point and one for the end: 3."""
    model = {"S": _stops_line, "D+stop": lambda: _divert_line(with_stop=True),
             "M": _merge_line}[line]()
    model["initial_state"] = {"clock": 5.0, "entities": good}
    if blocks is not None:
        model["initial_state"]["blocks"] = {"belt": {"left": [4.5, None, None]}}
    assert problem_codes(model) == set()
    model["initial_state"] = {"clock": 5.0, "entities": bad}
    if blocks is not None:
        model["initial_state"]["blocks"] = {"belt": {"left": blocks}}
    assert code in problem_codes(model)
