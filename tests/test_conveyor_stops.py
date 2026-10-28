"""Belt stops (Phase 12 A, D732-D735): a CONVEYOR's `stops` and the set partial-hold warning.

Every expected time is derived by hand in the test's docstring from the rules in
`core/blocks/conveyor.py`: travel = distance / speed, a part reaches a stop no sooner than
spacing / speed after the one before left it, a section between two stops holds
ceil(gap / spacing) parts (#1270, D853), a non-accumulating belt stops while a part is at a stop.
"""

import pytest

from conftest import create, dispose, layout, problem_codes, process

from nagasim import api, state
from nagasim.core.schema import validate_model_structured
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder


def belt(name="belt", length=10.0, speed=1.0, spacing=1.0, stops=(), accumulating=True,
         exit_resource=""):
    params = {"length": length, "speed": speed, "spacing": spacing,
              "accumulating": accumulating, "exit_resource": exit_resource,
              "from_node": "", "to_node": ""}
    if stops is not None:
        params["stops"] = list(stops)
    return {"name": name, "description": name, "type": "CONVEYOR", "position": [1, 0],
            "params": params}


def line(conveyor, *, arrivals, gap=1.0, hog=None, resources=(), **sim):
    """CREATE -> belt -> DISPOSE; *hog* (resource, hold time) seizes it at t = 0."""
    blocks = [create(max_arrivals=arrivals, dist_params={"value": gap}, start_time=0),
              conveyor, dispose()]
    links = [{"source": "create_1", "target": conveyor["name"]},
             {"source": conveyor["name"], "target": "dispose_1"}]
    if hog is not None:
        blocks += [create(name="hog_src", e_type="Hog", max_arrivals=1, start_time=0),
                   process(name="hog", resource=hog[0], value=hog[1])]
        links += [{"source": "hog_src", "target": "hog"}, {"source": "hog", "target": "dispose_1"}]
    return layout(blocks, links, resources=[{"name": r, "capacity": 1} for r in resources],
                  simulation={"time_limit": None, "warmup": 0, "replications": 1, **sim})


def exits(results, e_type="DEFAULT"):
    log = results["entity_log"]
    return sorted(log[log["Type"] == e_type]["Exited At"].tolist())


def test_a_single_stop_on_an_empty_belt_adds_exactly_its_time():
    """Length 10 at speed 1: 10; a stop at 4 holding the part 3: 13."""
    model = line(belt(stops=[{"at": 4, "time": 3}]), arrivals=1)
    results = api.run_model(model, replications=1)
    assert exits(results) == [pytest.approx(13.0)]
    assert results["conveyor_summary"].iloc[0]["Avg Transit Time"] == pytest.approx(13.0)


def test_a_stop_time_is_an_expression_per_part():
    """time = entity.k: the part that carries k = 2 stops 2 (10 + 2 = 12)."""
    blocks = [create(max_arrivals=1, start_time=0),
              {"name": "a", "description": "a", "type": "ASSIGN", "position": [1, 0],
               "params": {"assignments": [{"target": "entity.k", "expr": "2"}]}},
              belt(stops=[{"at": 5, "time": "entity.k"}]), dispose()]
    links = [{"source": "create_1", "target": "a"}, {"source": "a", "target": "belt"},
             {"source": "belt", "target": "dispose_1"}]
    results = api.run_model(layout(blocks, links), replications=1)
    assert exits(results) == [pytest.approx(12.0)]


def _packed_model():
    """Length 10, speed 1, spacing 1, a stop at 5 that seizes Op (time 0); Op is held by a
    hog from 0 to 20. Parts enter at 0, 1, 2 and reach the stop (raw) at 5, 6, 7.
      E1 waits for Op 5..20, leaves the stop at 20, the end at 25.
      E2 packs behind it (a spacing back, at 4); once E1 left it moves up 1: at the stop at
        21, Op is free, leaves at 21, the end at 26.
      E3 packs behind E2 (at 3), moves up once E2 left: 22, the end at 27.
    The run drains at 27."""
    return line(belt(stops=[{"at": 5, "time": 0, "resource": "Op"}]), arrivals=3,
                hog=("Op", 20.0), resources=("Op",))


def test_parts_behind_a_stop_pack_a_spacing_apart_and_leave_a_spacing_apart():
    results = api.run_model(_packed_model(), replications=1)
    assert exits(results) == [pytest.approx(25.0), pytest.approx(26.0), pytest.approx(27.0)]


def test_a_stop_with_a_resource_waits_for_it_and_its_wait_shows_in_conveyor_stops():
    """From `_packed_model`: waits 15, 0, 0 (mean 5, max 15); a part is at the stop 5..20 and
    waits there all that time, over a window of 27."""
    results = api.run_model(_packed_model(), replications=1)
    row = results["conveyor_stops"].iloc[0]
    assert (row["Conveyor"], row["Stop"], row["At"], row["Resource"], row["Parts"]) == \
        ("belt", 0, 5.0, "Op", 3)
    assert row["Avg Wait"] == pytest.approx(5.0)
    assert row["Max Wait"] == pytest.approx(15.0)
    assert row["Busy %"] == pytest.approx(100 * 15 / 27)
    assert row["Blocked %"] == pytest.approx(100 * 15 / 27)


def test_each_stop_flattens_into_kpi_keys_a_study_can_name():
    """D795: `stop/<belt>/<index>/<column>` for every numeric column but the ids, with the
    values of the frame (from `_packed_model`: 3 parts, mean wait 5)."""
    from nagasim.experiments import flatten_kpis, needs_event_logs
    flat = flatten_kpis(api.run_model(_packed_model(), replications=1))
    stops = {k: v for k, v in zip(flat["KPI"], flat["Value"]) if k.startswith("stop/")}
    assert set(stops) == {f"stop/belt/0/{c}" for c in
                          ("At", "Parts", "Avg Wait", "Max Wait", "Busy %", "Blocked %")}
    assert stops["stop/belt/0/Parts"] == 3
    assert stops["stop/belt/0/Avg Wait"] == pytest.approx(5.0)
    assert not needs_event_logs(["stop/belt/0/Avg Wait"])


def test_no_stops_means_no_conveyor_stops_frame_and_the_same_results():
    """An empty `stops` and no key at all run the same belt as before."""
    plain = api.run_model(line(belt(stops=None), arrivals=5, gap=0.5), replications=1)
    empty = api.run_model(line(belt(stops=[]), arrivals=5, gap=0.5), replications=1)
    assert "conveyor_stops" not in plain and "conveyor_stops" not in empty
    assert plain["entity_log"].equals(empty["entity_log"])
    assert plain["conveyor_summary"].equals(empty["conveyor_summary"])


def test_a_non_accumulating_belt_stops_for_a_stop_and_resumes_with_its_remaining_travel():
    """Length 10, speed 1, spacing 2 (rhythm 2), not accumulating, a stop at 4 for 3; parts
    enter at 0 and 2.
      E1 reaches the stop at 4: the belt stops 4..7 (E2 at 2 waits there).
      E2 rides on from 7, 2 more to the stop: 9; the belt stops 9..12 (E1 at 6).
      E1 needs 4 more from 12: 16. E2 needs 6 from 12: 18.
    Each: 10 of travel + 3 at its own stop + 3 at the other's."""
    model = line(belt(spacing=2.0, accumulating=False, stops=[{"at": 4, "time": 3}]),
                 arrivals=2, gap=2.0)
    results = api.run_model(model, replications=1)
    assert exits(results) == [pytest.approx(16.0), pytest.approx(18.0)]
    assert results["conveyor_summary"].iloc[0]["Avg Transit Time"] == pytest.approx(16.0)
    # the stop stops the belt, it does not block its end
    assert results["conveyor_summary"].iloc[0]["Blocked %"] == 0.0


def _backed_up_model():
    """Length 6, speed 1, spacing 1; stops at 2 (time 0) and 4 (seizes Op, time 0); sections
    [0,2], [2,4], [4,6] hold 2 parts each. Op is held 0..20. Six parts offered at 0..5.
      E1: stop 1 at 2, stop 2 at 4, waits for Op until 20, the end at 22.
      E2: stop 1 at 3, into section 2 (2 of 2), packed behind E1 (at 3); 21 at stop 2, 23.
      E3: stop 1 at 4, section 2 full: blocked AT stop 1 (at 2) until E1 leaves at 20; 22 at
          stop 2 (E2 left at 21), the end at 24.
      E4: packed behind E3 at stop 1 (at 1); 21 there, 23 at stop 2, 25.
      E5: section 1 is full (E3, E4): it waits at the entry until 20, boards at 20; 26.
      E6: boards at 21 (its rhythm after E5); 27.
    Transits 22, 22, 22, 22, 6, 6: mean 100 / 6. Without the sections E5 and E6 would board at
    4 and 5 and pack back past stop 1, off the belt (transit 22 each)."""
    return line(belt(length=6.0, stops=[{"at": 2, "time": 0},
                                        {"at": 4, "time": 0, "resource": "Op"}]),
                arrivals=6, hog=("Op", 20.0), resources=("Op",))


def test_a_non_accumulating_belt_stopped_by_a_stop_then_by_its_end():
    """Length 10, speed 1, spacing 2, not accumulating, a stop at 4 for 3, and the end stop
    Gate held by a hog 0..20 (a picker gives it back at once). Parts enter at 0 and 2.
      Stops: E1 4..7 (E2 at 2), E2 9..12 (E1 at 6). E1 reaches the end at 16, Gate is
      busy: the belt stops 16..20 (E2 at 8). E1 leaves at 20, E2 needs 2: 22.
    The end blocks the belt 16..20 of a window of 22; the stop never counts there."""
    blocks = [create(max_arrivals=2, dist_params={"value": 2.0}, start_time=0),
              belt(spacing=2.0, accumulating=False, exit_resource="Gate",
                   stops=[{"at": 4, "time": 3}]),
              process(name="pick", resource="Gate", value=0.0, action="Delay Release"),
              dispose(), create(name="hog_src", e_type="Hog", max_arrivals=1, start_time=0),
              process(name="hog", resource="Gate", value=20.0)]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "pick"},
             {"source": "pick", "target": "dispose_1"}, {"source": "hog_src", "target": "hog"},
             {"source": "hog", "target": "dispose_1"}]
    model = layout(blocks, links, resources=[{"name": "Gate", "capacity": 1}])
    results = api.run_model(model, replications=1)
    assert exits(results) == [pytest.approx(20.0), pytest.approx(22.0)]
    assert results["conveyor_summary"].iloc[0]["Blocked %"] == pytest.approx(100 * 4 / 22)


@pytest.mark.parametrize("accumulating", [True, False])
def test_a_belt_loop_with_room_and_stops_runs_without_a_deadlock(accumulating):
    """A -> B -> A, two parts on 8 cells: stops hold the belts, never the loop."""
    looped = [belt("A", length=4.0, accumulating=accumulating, stops=[{"at": 2, "time": 1}]),
              belt("B", length=4.0, accumulating=accumulating, stops=[{"at": 2, "time": 1.5}])]
    blocks = [create(max_arrivals=2, dist_params={"value": 0.5}), *looped,
              {"name": "again", "description": "again", "type": "DECIDE", "position": [2, 0],
               "params": {"mode": "Condition", "conditions": ["1 == 1"]}}, dispose()]
    links = [{"source": "create_1", "target": "A"}, {"source": "A", "target": "B"},
             {"source": "B", "target": "again"}, {"source": "again", "target": "A"},
             {"source": "again", "target": "dispose_1"}]
    model = layout(blocks, links, simulation={"time_limit": 60, "warmup": 0, "replications": 1})
    rows = api.run_model(model, replications=1)["conveyor_summary"].set_index("Conveyor")
    assert rows.loc["A", "Entered"] >= 5 and rows.loc["B", "Entered"] >= 5


def test_a_belt_loop_closed_by_full_sections_is_a_deadlock_at_once():
    """A (length 10, spacing 1: 10 cells) has stops at 2.5, 5 and 7.5, so its four sections
    hold ceil(2.5) = 3 parts each (#1270, D853), more than its 10 cells; B holds 2. With 12
    parts both fill: A's sections full is what stops its entry once its last part is in, and
    the loop check says so when it closes, not only once nothing is left to happen. (Before
    #1270 the sections held 2 each, 8 in all, and 10 parts closed the loop.)"""
    stops = [{"at": a, "time": 0} for a in (2.5, 5, 7.5)]
    blocks = [create(max_arrivals=12, dist_params={"value": 0.1}), belt("A", stops=stops),
              belt("B", length=2.0),
              {"name": "again", "description": "again", "type": "DECIDE", "position": [2, 0],
               "params": {"mode": "Condition", "conditions": ["1 == 1"]}}, dispose()]
    links = [{"source": "create_1", "target": "A"}, {"source": "A", "target": "B"},
             {"source": "B", "target": "again"}, {"source": "again", "target": "A"},
             {"source": "again", "target": "dispose_1"}]
    model = layout(blocks, links, simulation={"time_limit": 500, "warmup": 0, "replications": 1})
    with pytest.raises(RuntimeError, match="every station and buffer in the loop"):
        api.run_model(model, replications=1)


def test_a_queue_backs_up_past_a_stop_only_by_holding_the_part_there():
    results = api.run_model(_backed_up_model(), replications=1)
    assert exits(results) == [pytest.approx(t) for t in (22, 23, 24, 25, 26, 27)]
    assert results["conveyor_summary"].iloc[0]["Avg Transit Time"] == pytest.approx(100 / 6)
    first, second = results["conveyor_stops"].sort_values("Stop").to_dict("records")
    assert first["Blocked %"] == pytest.approx(100 * 16 / 27)      # E3, 4..20
    assert second["Blocked %"] == pytest.approx(100 * 16 / 27)     # E1 waits for Op, 4..20


@pytest.mark.parametrize("at", [0, 10, -1, 12, "x"])
def test_a_stop_off_the_belt_is_refused_by_code(at):
    assert "E_CONVEYOR_STOP" in problem_codes(line(belt(stops=[{"at": at, "time": 1}]),
                                                   arrivals=1))


def test_a_stop_is_checked_for_its_time_and_resource():
    for stop in ({"at": 2, "time": -1}, {"at": 2, "time": 1, "resource": "Nope"},
                 {"at": 2, "time": 1, "where": 3}, 4):
        assert "E_CONVEYOR_STOP" in problem_codes(line(belt(stops=[stop]), arrivals=1)), stop
    assert "E_CONVEYOR_STOP" not in problem_codes(
        line(belt(stops=[{"at": 2, "time": "1 + 1", "resource": "Op"}]), arrivals=1,
             resources=("Op",)))


def test_a_stop_off_a_network_length_belt_is_refused_when_the_belt_is_built():
    """A belt whose length the network gives (here 5) is checked at run time, by code."""
    model = line(belt(length=0.0, stops=[{"at": 7, "time": 1}]), arrivals=1)
    model["blocks"][1]["params"].update(from_node="A", to_node="B")
    model["network"] = {"nodes": [{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": 5, "y": 0}],
                        "segments": [{"from": "A", "to": "B"}]}
    assert "E_CONVEYOR_STOP" not in problem_codes(model)       # no length to check it against
    with pytest.raises(Exception, match="E_CONVEYOR_STOP"):
        api.run_model(model, replications=1)


def test_a_belt_with_stops_is_refused_in_a_state_by_name():
    """D918: a plant still cannot start one (`refused_kind`), a run's state carries it."""
    model = _packed_model()
    model["simulation"]["time_limit"] = 30
    assert state.refused_kind(model) == "a conveyor with stops"
    assert state.take(model, 3.0)["state"]["entities"]


def test_a_part_on_a_belt_with_stops_in_an_initial_state_is_refused():
    """D918: a part waiting for a cell is accepted; a stage of another point kind is not."""
    model = _packed_model()
    model["initial_state"] = {"entities": [{"block": "belt", "count": 1,
                                            "belt": {"stage": "cell"}}]}
    assert "E_INITIAL_BELT" not in problem_codes(model)
    model["initial_state"] = {"clock": 5.0, "entities": [{"block": "belt", "count": 1, "belt": {
        "stage": "push", "until": 6.0, "entered": 0.0, "point": 0}}]}
    assert "E_INITIAL_BELT" in problem_codes(model)


def _positions(model, times):
    recorder = TraceRecorder()
    api.run_model(model, replications=1, trace=recorder)
    timeline = Timeline(recorder.events)
    out = {}
    for t in times:
        for entity_id, x, _stage in timeline.state_at(t)["belts"].get("belt", []):
            out.setdefault(entity_id, []).append((t, x))
    return out, timeline


def test_timeline_positions_are_monotone_and_match_the_stage_times():
    times = [i / 4 for i in range(0, 28 * 4)]
    tracks, timeline = _positions(_packed_model(), times)
    assert len(tracks) == 3
    for track in tracks.values():
        xs = [x for _t, x in track]
        assert all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))
    e1, e2, e3 = (dict(tracks[k]) for k in sorted(tracks))
    assert e1[3.0] == pytest.approx(3.0) and e1[10.0] == pytest.approx(5.0)
    assert e1[22.0] == pytest.approx(7.0)
    assert e2[15.0] == pytest.approx(4.0) and e3[15.0] == pytest.approx(3.0)   # packed
    assert e2[20.5] == pytest.approx(4.5)          # moving up the spacing E1 left
    assert e3[21.5] == pytest.approx(4.5)
    # E1 is off at 25; a seek back from there replays to the same positions
    assert len(timeline.state_at(25.5)["belts"]["belt"]) == 2
    back = timeline.state_at(15.0)["belts"]["belt"]
    assert [x for _i, x, _s in back] == [pytest.approx(5.0), pytest.approx(4.0),
                                         pytest.approx(3.0)]
    assert [s for _i, _x, s in back] == ["wait", "packed", "packed"]


def test_timeline_puts_a_backed_up_queue_behind_the_stop_that_holds_it():
    """`_backed_up_model` at t = 10: E1 at 4 (waits for Op), E2 at 3, E3 at stop 1 (2,
    blocked), E4 at 1; E5 and E6 are not on the belt yet."""
    _tracks, timeline = _positions(_backed_up_model(), [10.0])
    rows = timeline.state_at(10.0)["belts"]["belt"]
    assert [x for _i, x, _s in rows] == [pytest.approx(v) for v in (4.0, 3.0, 2.0, 1.0)]
    assert [s for _i, _x, s in rows] == ["wait", "packed", "blocked", "packed"]


def test_a_belt_without_stops_traces_its_positions_too():
    """Length 10, speed 2: a part at 2.5 is 5 along; it is off once it left."""
    recorder = TraceRecorder()
    api.run_model(line(belt(speed=2.0), arrivals=1), replications=1, trace=recorder)
    timeline = Timeline(recorder.events)
    [(_id, x, stage)] = timeline.state_at(2.5)["belts"]["belt"]
    assert (x, stage) == (pytest.approx(5.0), "ride")
    assert "belt" not in timeline.state_at(6.0)["belts"]


# -- W_CONVEYOR_STOP_HELD (D796) ---------------------------------------------

def _held_model(*between, first="seize"):
    """CREATE -> <first, which holds Op> -> *between* -> DISPOSE, linked in that order."""
    if first == "seize":
        start = process(name="hold", resource="Op", value=0.0, action="Seize Delay")
    else:
        start = belt(name="hold", exit_resource="Op")
    blocks = [create(max_arrivals=1), start, *between, dispose()]
    names = [b["name"] for b in blocks]
    links = [{"source": a, "target": b} for a, b in zip(names, names[1:])]
    return layout(blocks, links, resources=[{"name": "Op", "capacity": 2}])


def _op_stop_belt():
    return belt(name="line", stops=[{"at": 4, "time": 1, "resource": "Op"}])


def _give_back():
    return process(name="give", resource="Op", value=0.0, action="Delay Release")


def test_a_stop_on_a_resource_the_part_still_holds_warns():
    problems = validate_model_structured(_held_model(_op_stop_belt(), _give_back()))
    [held] = [p for p in problems if p.code == "W_CONVEYOR_STOP_HELD"]
    assert held.severity == "warning" and held.block == "line"
    assert held.path == "/blocks/2/params/stops/0/resource"
    # What the warning foretells: the engine refuses the second request, mid-run.
    with pytest.raises(RuntimeError, match="already requested resource 'Op'"):
        api.run_model(_held_model(_op_stop_belt(), _give_back()), replications=1)


def test_a_hold_released_before_the_belt_does_not_warn():
    model = _held_model(_give_back(), _op_stop_belt())
    assert "W_CONVEYOR_STOP_HELD" not in problem_codes(model, severity=None)


def test_a_belts_exit_resource_held_onto_the_next_belts_stop_warns():
    model = _held_model(_op_stop_belt(), _give_back(), first="belt")
    assert "W_CONVEYOR_STOP_HELD" in problem_codes(model, severity="warning")
