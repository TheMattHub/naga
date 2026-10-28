"""Conveyor diverts, merges and the #1270 section rule (Phase 15 M9: D851-D855, D853).

Written from the design spec only (section "M9 conveyor diverts"), before the build. Every expected
time is derived by hand in the test's docstring. Defaults: length 10, speed 1, spacing 1,
accumulating, parts every 1 from t = 0, so a part is at x at t = (its boarding time) + x while
nothing holds it. Link 0 of the belt is its end, link k + 1 is divert k.
"""

import pytest

from conftest import create, dispose, layout, problem_codes, process

from nagasim import api, state
from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceRecorder
from nagasim.experiments import flatten_kpis


def belt(name="belt", length=10.0, speed=1.0, spacing=1.0, stops=(), diverts=None, merges=None,
         accumulating=True, exit_resource=""):
    params = {"length": length, "speed": speed, "spacing": spacing,
              "accumulating": accumulating, "exit_resource": exit_resource,
              "from_node": "", "to_node": "", "stops": list(stops)}
    if diverts is not None:
        params["diverts"] = list(diverts)
    if merges is not None:
        params["merges"] = list(merges)
    return {"name": name, "description": name, "type": "CONVEYOR", "position": [1, 0],
            "params": params}


def hog(resource, hold):
    """A part that seizes *resource* at t = 0 and holds it *hold* time units."""
    return ([create(name="hog_src", e_type="Hog", max_arrivals=1, start_time=0),
             process(name="hog", resource=resource, value=hold)],
            [{"source": "hog_src", "target": "hog"}, {"source": "hog", "target": "dispose_1"}])


def pick(resource):
    """Gives the resource back at once (the lane's exit_resource is seized at its end)."""
    return process(name="pick", resource=resource, value=0.0, action="Delay Release")


def model(blocks, links, resources=(), **sim):
    return layout(blocks, links, resources=[{"name": r, "capacity": 1} for r in resources],
                  simulation={"time_limit": None, "warmup": 0, "replications": 1, **sim})


def run(blocks, links, resources=()):
    return api.run_model(model(blocks, links, resources), replications=1)


def side_line(conveyor, *, arrivals, gap=1.0):
    """CREATE -> belt (end -> dispose_1, link 1 -> dispose_side) with no other block."""
    blocks = [create(max_arrivals=arrivals, dist_params={"value": gap}, start_time=0),
              conveyor, dispose(), dispose(name="dispose_side")]
    links = [{"source": "create_1", "target": conveyor["name"]},
             {"source": conveyor["name"], "target": "dispose_1"},
             {"source": conveyor["name"], "target": "dispose_side"}]
    return blocks, links


def plain_line(conveyor, *, arrivals, gap=1.0, extra_blocks=(), extra_links=(), resources=()):
    blocks = [create(max_arrivals=arrivals, dist_params={"value": gap}, start_time=0),
              conveyor, dispose(), *extra_blocks]
    links = [{"source": "create_1", "target": conveyor["name"]},
             {"source": conveyor["name"], "target": "dispose_1"}, *extra_links]
    return model(blocks, links, resources)


def exits(results, e_type="DEFAULT"):
    # The engine stores entity type names upper-cased ("Side" -> "SIDE").
    log = results["entity_log"]
    return sorted(log[log["Type"] == e_type]["Exited At"].tolist())


def approx_all(values):
    return [pytest.approx(v) for v in values]


def belt_row(results, name="belt"):
    return results["conveyor_summary"].set_index("Conveyor").loc[name]


def divert_row(results, k=0):
    frame = results["conveyor_diverts"]
    return frame[frame["Divert"] == k].iloc[0]


def merge_row(results, k=0):
    frame = results["conveyor_merges"]
    return frame[frame["Merge"] == k].iloc[0]


def divert_at(at, condition="True", **extra):
    return {"at": at, "condition": condition, **extra}


# -- test 1: identity ----------------------------------------------------------

def test_an_empty_diverts_and_merges_table_runs_the_belt_as_before():
    """No frames, same log and summary as a belt without the keys. (The wider identity check is
    the unchanged test_conveyor*.py files and the bundled digests, run by the coordinator.)"""
    plain = api.run_model(plain_line(belt(), arrivals=5, gap=0.5), replications=1)
    empty = api.run_model(plain_line(belt(diverts=[], merges=[]), arrivals=5, gap=0.5),
                          replications=1)
    assert "conveyor_diverts" not in empty and "conveyor_merges" not in empty
    assert plain["entity_log"].equals(empty["entity_log"])
    assert plain["conveyor_summary"].equals(empty["conveyor_summary"])


# -- test 2 and 3: #1270 -------------------------------------------------------

def test_1270_a_zero_time_stop_does_not_throttle_a_free_flowing_belt():
    """Length 10, a stop at 2.5 (time 0), six parts every 1: free flow, they leave at 10..15
    (the bug gave 10, 11, 12.5, 13.5, 15, 16). Section [0, 2.5) holds ceil(2.5) = 3 parts."""
    results = api.run_model(plain_line(belt(stops=[{"at": 2.5, "time": 0}]), arrivals=6),
                            replications=1)
    assert exits(results) == approx_all([10, 11, 12, 13, 14, 15])


def test_1270_a_divert_that_passes_every_part_gives_the_same_times():
    """A divert at 2.5 with condition False is a zero-dwell point: same 10..15, Passed 6."""
    blocks, links = side_line(belt(diverts=[divert_at(2.5, "False")]), arrivals=6)
    results = run(blocks, links)
    assert exits(results) == approx_all([10, 11, 12, 13, 14, 15])
    row = divert_row(results)
    assert (row["Diverted"], row["Passed"], row["Skipped"]) == (0, 6, 0)


def test_1270_a_whole_number_gap_is_unchanged():
    """Stops at 3 (time 0): g/s = 3 is whole, d = 0: still 10..15."""
    results = api.run_model(plain_line(belt(stops=[{"at": 3, "time": 0}]), arrivals=6),
                            replications=1)
    assert exits(results) == approx_all([10, 11, 12, 13, 14, 15])


def test_1270_a_section_shorter_than_a_spacing_holds_one_part_and_does_not_throttle():
    """A stop at 0.4: section 0 has length 0.4 < spacing, c = max(1, ceil(0.4)) = 1, d = (1 -
    0.4)/1 = 0.6 after the part ahead left the section end (its boarding + 0.4): the next part
    boards at its own rhythm, boarding + 1, so d never bites. Free flow, 10..15."""
    results = api.run_model(plain_line(belt(stops=[{"at": 0.4, "time": 0}]), arrivals=6),
                            replications=1)
    assert exits(results) == approx_all([10, 11, 12, 13, 14, 15])


def test_1270_packed_belt_leaves_a_spacing_apart_and_the_third_part_waits_half_a_unit_more():
    """Stops at 2.5 (time 0) and 5 (seizes Op, time 0); a hog holds Op 0..20; four parts at
    0, 1, 2, 3. Sections [0,2.5) c=3, [2.5,5) c=3 (g=2.5, d=0.5), [5,10) c=5.
      E0 waits at 5 for Op until 20, the end at 25. E1 and E2 pack behind it (at 4, 3).
      E3 would be the 4th in [2.5, 5): the section is full until E0 leaves its end (at 20),
      and E3 reaches 2.5 no sooner than d = 0.5 later: 20.5 (today 20 and an error of 0.5
      in the later leave times). From 2.5 it needs 2.5 to the stop: 23 (E2 left it at 22).
      E1 leaves the stop at 21, E2 at 22: ends 26, 27; E3 end 28."""
    stops = [{"at": 2.5, "time": 0}, {"at": 5, "time": 0, "resource": "Op"}]
    blocks, links = hog("Op", 20.0)
    results = api.run_model(plain_line(belt(stops=stops), arrivals=4, extra_blocks=blocks,
                                       extra_links=links, resources=("Op",)), replications=1)
    assert exits(results) == approx_all([25, 26, 27, 28])
    # E3's front passes 2.5 at 20.5 and then rides free: at 21 it is at 3.
    recorder = TraceRecorder()
    api.run_model(plain_line(belt(stops=stops), arrivals=4, extra_blocks=blocks,
                             extra_links=links, resources=("Op",)),
                  replications=1, trace=recorder)
    timeline = Timeline(recorder.events)
    rows = timeline.state_at(21.0)["belts"]["belt"]
    assert min(x for _i, x, _s in rows) == pytest.approx(3.0)
    rows = timeline.state_at(20.25)["belts"]["belt"]
    assert min(x for _i, x, _s in rows) <= 2.5 + 1e-9


# -- tests 4 to 6: diverting ---------------------------------------------------

def test_4_every_part_diverts():
    """Divert at 4, condition True, three parts at 0, 1, 2: they reach 4 at 4, 5, 6 and leave
    by link 1 there (dwell 0). Nothing reaches the end (a tail belt behind it stays at 0),
    Exited is 3 and every transit is 4."""
    blocks, links = side_line(belt(diverts=[divert_at(4)]), arrivals=3)
    blocks[2] = belt(name="tail", length=1.0)
    blocks.insert(3, dispose())
    links[1] = {"source": "belt", "target": "tail"}
    links.insert(2, {"source": "tail", "target": "dispose_1"})
    results = run(blocks, links)
    assert exits(results) == approx_all([4, 5, 6])
    row = belt_row(results)
    assert (row["Entered"], row["Exited"]) == (3, 3)
    assert row["Avg Transit Time"] == pytest.approx(4.0)
    assert belt_row(results, "tail")["Entered"] == 0
    stats = divert_row(results)
    assert (stats["Diverted"], stats["Passed"], stats["Skipped"]) == (3, 0, 0)
    assert (stats["Conveyor"], stats["At"], stats["Exit"]) == ("belt", 4.0, 1)


def test_5_a_condition_on_the_entity_type_chooses_the_way_off():
    """CREATE A (2 arrivals at 0 and 2) and CREATE B (1 arrival at 1); divert at 4 for type A.
    A leaves by link 1 at 0 + 4 and 2 + 4; B passes and leaves the end at 1 + 10 = 11."""
    belt_ = belt(diverts=[divert_at(4, 'entity.e_type == "A"')])
    blocks, links = side_line(belt_, arrivals=0)
    blocks[0] = create(name="create_a", e_type="A", max_arrivals=2, dist_params={"value": 2.0},
                       start_time=0)
    blocks.append(create(name="create_b", e_type="B", max_arrivals=1, start_time=1))
    links[0] = {"source": "create_a", "target": "belt"}
    links.append({"source": "create_b", "target": "belt"})
    results = run(blocks, links)
    assert exits(results, "A") == approx_all([4, 6])
    assert exits(results, "B") == approx_all([11])
    stats = divert_row(results)
    assert (stats["Diverted"], stats["Passed"]) == (2, 1)


def test_6_the_push_time_holds_the_part_and_the_parts_behind_it():
    """Test 4 with time 2. E0 reaches 4 at 4, pushes 4..6; E1 reaches no sooner than 1 after E0
    left (7) and pushes to 9; E2 at 10, to 12. Link 1 exits: 6, 9, 12."""
    blocks, links = side_line(belt(diverts=[divert_at(4, time=2)]), arrivals=3)
    results = run(blocks, links)
    assert exits(results) == approx_all([6, 9, 12])


def test_6_a_non_accumulating_belt_stops_for_the_push_and_gives_the_same_times():
    """Non-accumulating: the belt stops 4..6 (E1 at 3, E2 at 2), resumes, E1 reaches 4 at 7
    and stops it 7..9 (E2 at 3), E2 reaches 4 at 10, pushes to 12: 6, 9, 12."""
    blocks, links = side_line(belt(diverts=[divert_at(4, time=2)], accumulating=False),
                              arrivals=3)
    results = run(blocks, links)
    assert exits(results) == approx_all([6, 9, 12])


def test_6_a_negative_time_expression_counts_as_zero():
    blocks, links = side_line(belt(diverts=[divert_at(4, time="0 - 5")]), arrivals=3)
    assert exits(run(blocks, links)) == approx_all([4, 5, 6])


# -- tests 7 and 8: a full lane ------------------------------------------------

def _lane_model(when_full):
    """Divert at 4 (True) into a lane (length 2, spacing 1: 2 cells) whose end seizes Gate;
    a hog holds Gate 0..20; the lane goes on to `pick` (gives Gate back) and a DISPOSE."""
    main = belt(diverts=[divert_at(4, when_full=when_full)])
    lane = belt(name="lane", length=2.0, exit_resource="Gate")
    hog_blocks, hog_links = hog("Gate", 20.0)
    blocks = [create(max_arrivals=3, dist_params={"value": 1.0}, start_time=0), main, lane,
              pick("Gate"), dispose(), *hog_blocks]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"},
             {"source": "belt", "target": "lane"}, {"source": "lane", "target": "pick"},
             {"source": "pick", "target": "dispose_1"}, *hog_links]
    return run(blocks, links, resources=("Gate",))


def test_7_wait_holds_the_part_at_the_divert_until_the_lane_has_room():
    """P0 and P1 board the lane at 4 and 5 (2 cells); at 6 the lane is full (P0 at its end,
    waiting for Gate until 20), so P2 waits at the divert 6..20. The lane exits at 20 (Gate
    given back at once), 21, 22 (a spacing apart, P2 boards at 20 and needs 2). The main belt's
    transits are 4, 4, 18. Divert waits 0, 0, 14."""
    results = _lane_model("wait")
    assert exits(results) == approx_all([20, 21, 22])
    assert belt_row(results)["Avg Transit Time"] == pytest.approx(26 / 3)
    assert belt_row(results)["Exited"] == 3
    stats = divert_row(results)
    assert stats["Diverted"] == 3
    assert stats["Avg Wait"] == pytest.approx(14 / 3)
    assert stats["Max Wait"] == pytest.approx(14.0)
    # Assumption: Blocked % is taken over the drain time (22); P2 waits 14 of it.
    assert stats["Blocked %"] == pytest.approx(100 * 14 / 22)
    assert stats["Busy %"] == pytest.approx(100 * 14 / 22)


def test_8_pass_rides_on_when_the_lane_is_full():
    """Test 7 with when_full "pass": at 6 the lane is full, P2 rides on and leaves the end at
    2 + 10 = 12. Diverted 2, Skipped 1; the lane exits at 20 and 21."""
    results = _lane_model("pass")
    assert exits(results) == approx_all([12, 20, 21])
    stats = divert_row(results)
    assert (stats["Diverted"], stats["Skipped"], stats["Passed"]) == (2, 1, 0)
    assert belt_row(results, "lane")["Exited"] == 2


# -- test 9: sections across diverts -------------------------------------------

def test_9_a_full_section_holds_the_part_at_the_divert_before_it():
    """d0 at 3 (type A, to a DISPOSE), d1 at 5 (type B, wait, to a 1-cell lane of length 1 whose
    Gate is held until 100). Types B B B A B A board at 0..5. Sections [0,3) c=3, [3,5) c=2.
      B0 passes d0 at 3, d1 at 5, boards the lane (5), waits at its end for Gate until 100.
      B1 passes d0 at 4 and waits at d1 from 6 (lane cell taken) until B0 leaves at 100.
      B2 enters [3,5) at 5 (B0 left it), packs behind B1: the section is full (B1, B2).
      A3 reaches d0 at 6 and diverts there at once: leaves at 6.
      B4 reaches d0 at 7 and waits there: [3,5) is full until B1 leaves at 100.
      A5 stands behind B4; B4 leaves d0 at 100, A5 reaches it a spacing later: 101, diverts.
    The B parts then pass the lane a spacing apart (Gate released at 100 by B0): 100..103."""
    main = belt(diverts=[divert_at(3, 'entity.e_type == "A"'),
                         divert_at(5, 'entity.e_type == "B"', when_full="wait")])
    lane = belt(name="lane", length=1.0, exit_resource="Gate")
    hog_blocks, hog_links = hog("Gate", 100.0)
    types = ["B", "B", "B", "A", "B", "A"]
    creates = [create(name=f"c{i}", e_type=t, max_arrivals=1, start_time=i)
               for i, t in enumerate(types)]
    blocks = [*creates, main, lane, pick("Gate"), dispose(), dispose(name="dispose_a"),
              *hog_blocks]
    links = [{"source": f"c{i}", "target": "belt"} for i in range(6)]
    links += [{"source": "belt", "target": "dispose_1"}, {"source": "belt", "target": "dispose_a"},
              {"source": "belt", "target": "lane"}, {"source": "lane", "target": "pick"},
              {"source": "pick", "target": "dispose_1"}, *hog_links]
    results = run(blocks, links, resources=("Gate",))
    assert exits(results, "A") == approx_all([6, 101])
    assert exits(results, "B") == approx_all([100, 101, 102, 103])


# -- tests 10 to 12: merges ----------------------------------------------------

def _merge_model(priority):
    """Three main parts at 0, 1, 2; `side` makes one part at 5.5; merge at 5 from `side`."""
    spec = {"at": 5, "from": "side"}
    if priority is not None:
        spec["priority"] = priority
    blocks = [create(max_arrivals=3, dist_params={"value": 1.0}, start_time=0), belt(merges=[spec]),
              dispose(), create(name="side", e_type="Side", max_arrivals=1, start_time=5.5)]
    links = [{"source": "create_1", "target": "belt"}, {"source": "side", "target": "belt"},
             {"source": "belt", "target": "dispose_1"}]
    return model(blocks, links)


def test_10_a_merge_with_belt_priority_joins_in_a_gap_of_two_spacings():
    """Main fronts reach 5 at 5, 6, 7 and claim it while strictly inside (4, 5), (5, 6), (6, 7).
    The side part is ready at 5.5 (claimed by main 1 until 6); the last pass is 7, so it joins
    at the first t >= 7 + 1 = 8 and leaves at 8 + 5 = 13. Mains leave at 10, 11, 12.
    Wait 2.5; Entered 4, transits (30 + 5) / 4."""
    results = api.run_model(_merge_model("belt"), replications=1)
    assert exits(results) == approx_all([10, 11, 12])
    assert exits(results, "SIDE") == approx_all([13])
    stats = merge_row(results)
    assert (stats["Conveyor"], stats["At"], stats["From"], stats["Merged"]) == \
        ("belt", 5.0, "side", 1)
    assert stats["Avg Wait"] == pytest.approx(2.5)
    assert stats["Max Wait"] == pytest.approx(2.5)
    row = belt_row(results)
    assert (row["Entered"], row["Exited"]) == (4, 4)
    assert row["Avg Transit Time"] == pytest.approx(35 / 4)


def test_10_the_default_priority_is_belt():
    results = api.run_model(_merge_model(None), replications=1)
    assert exits(results, "SIDE") == approx_all([13])


def test_11_a_merge_priority_makes_the_main_part_wait_for_the_join():
    """Priority "merge": the side part is ready at 5.5. Main 1 is already inside (4, 5): it
    passes at 6. Main 2 reaches at - s = 4 at 6, after the side part is ready, so it waits there
    until the join. The join is at t >= 6 + 1 = 7 with no claim: 7. The side part leaves at 12;
    main 2 rides on from 4 at 7 and leaves at 13. Mains: 10, 11, 13. Wait 1.5."""
    results = api.run_model(_merge_model("merge"), replications=1)
    assert exits(results) == approx_all([10, 11, 13])
    assert exits(results, "SIDE") == approx_all([12])
    stats = merge_row(results)
    assert stats["Merged"] == 1 and stats["Avg Wait"] == pytest.approx(1.5)


def test_12_the_timeline_draws_the_joined_part_in_its_place():
    """Test 11 at t = 7.5: main 0 at 7.5, main 1 at 6.5, the side part (joined at 5 at 7) at
    5.5, main 2 (restarted from 4 at 7) at 4.5, in that order."""
    recorder = TraceRecorder()
    api.run_model(_merge_model("merge"), replications=1, trace=recorder)
    rows = Timeline(recorder.events).state_at(7.5)["belts"]["belt"]
    assert [x for _i, x, _s in rows] == approx_all([7.5, 6.5, 5.5, 4.5])
    # main 2 is held at 4 (packed) between 6 and 7
    held = Timeline(recorder.events).state_at(6.5)["belts"]["belt"]
    assert [x for _i, x, _s in held][-1] == pytest.approx(4.0)


# -- test 13: KPIs -------------------------------------------------------------

def test_13_diverts_and_merges_flatten_into_kpi_keys_and_other_belts_have_no_frames():
    blocks, links = side_line(belt(diverts=[divert_at(4)]), arrivals=3)
    flat = flatten_kpis(run(blocks, links))
    keys = dict(zip(flat["KPI"], flat["Value"]))
    assert keys["divert/belt/0/Diverted"] == 3
    merged = flatten_kpis(api.run_model(_merge_model("belt"), replications=1))
    assert dict(zip(merged["KPI"], merged["Value"]))["merge/belt/0/Merged"] == 1
    plain = api.run_model(plain_line(belt(), arrivals=3), replications=1)
    assert "conveyor_diverts" not in plain and "conveyor_merges" not in plain
    keys = flatten_kpis(plain)["KPI"].tolist()
    assert not any(k.startswith(("divert/", "merge/")) for k in keys)


# -- test 14: validation -------------------------------------------------------

def _diverted(diverts, *, links=1, **belt_args):
    """CREATE -> belt with *diverts*, and 1 + *links* ... outgoing links (the end plus
    *links* - 1 side links), all to DISPOSEs."""
    blocks = [create(max_arrivals=1), belt(diverts=diverts, **belt_args), dispose()]
    out = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"}]
    for i in range(links - 1):
        blocks.append(dispose(name=f"side_{i}"))
        out.append({"source": "belt", "target": f"side_{i}"})
    return model(blocks, out)


def test_a_valid_divert_table_raises_nothing():
    codes = problem_codes(_diverted([divert_at(4, "entity.k > 1", time="1 + 1")], links=2))
    assert "E_CONVEYOR_DIVERT" not in codes and "E_TOO_MANY_OUTPUTS" not in codes


@pytest.mark.parametrize("bad", [
    4,                                              # not a table
    {"at": 4},                                      # no condition
    {"at": 4, "condition": "True", "where": 1},     # unknown key
    {"at": 0, "condition": "True"}, {"at": 10, "condition": "True"},
    {"at": -1, "condition": "True"}, {"at": 12, "condition": "True"},
    {"at": "x", "condition": "True"},               # off the belt
    {"at": 4, "condition": ""},                     # empty condition
    {"at": 4, "condition": "   "},
    {"at": 4, "condition": "True", "time": -1},     # negative literal time
    {"at": 4, "condition": "True", "time": [1]},    # neither a number nor an expression
    {"at": 4, "condition": "True", "when_full": "maybe"},
])
def test_a_bad_divert_item_is_refused_by_code(bad):
    assert "E_CONVEYOR_DIVERT" in problem_codes(_diverted([bad], links=2)), bad


def test_a_divert_on_another_stop_or_divert_or_merge_is_refused():
    assert "E_CONVEYOR_DIVERT" in problem_codes(
        _diverted([divert_at(4)], links=2, stops=[{"at": 4, "time": 1}]))
    assert "E_CONVEYOR_DIVERT" in problem_codes(_diverted([divert_at(4), divert_at(4)], links=3))
    both = _diverted([divert_at(4)], links=2, merges=[{"at": 4, "from": "create_1"}])
    # A divert placed on a merge point matches both rules of the spec ("an `at` on another
    # point"), so either code is a correct refusal.
    assert problem_codes(both) & {"E_CONVEYOR_DIVERT", "E_CONVEYOR_MERGE"}


def test_fewer_links_than_one_plus_diverts_is_refused():
    assert "E_CONVEYOR_DIVERT" in problem_codes(_diverted([divert_at(4)], links=1))
    assert "E_CONVEYOR_DIVERT" in problem_codes(_diverted([divert_at(4), divert_at(6)], links=2))


def test_pass_to_a_target_outside_the_list_is_refused():
    """"pass" needs a link straight to a CONVEYOR, a BUFFER with capacity or a station with a
    queue_capacity; a DISPOSE is none of them, a CONVEYOR is."""
    assert "E_CONVEYOR_DIVERT" in problem_codes(
        _diverted([divert_at(4, when_full="pass")], links=2))
    blocks = [create(max_arrivals=1), belt(diverts=[divert_at(4, when_full="pass")]), dispose(),
              belt(name="lane", length=2.0), dispose(name="dispose_2")]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"},
             {"source": "belt", "target": "lane"}, {"source": "lane", "target": "dispose_2"}]
    assert "E_CONVEYOR_DIVERT" not in problem_codes(model(blocks, links))


def test_more_links_than_one_plus_diverts_is_too_many_outputs():
    assert "E_TOO_MANY_OUTPUTS" in problem_codes(_diverted([divert_at(4)], links=3))
    assert "E_TOO_MANY_OUTPUTS" not in problem_codes(_diverted([divert_at(4)], links=2))


def _merging(merges, **belt_args):
    blocks = [create(max_arrivals=1), belt(merges=merges, **belt_args), dispose(),
              create(name="side", max_arrivals=1), create(name="other", max_arrivals=1),
              create(name="loose", max_arrivals=1), dispose(name="dispose_x")]
    links = [{"source": "create_1", "target": "belt"}, {"source": "side", "target": "belt"},
             {"source": "other", "target": "belt"}, {"source": "belt", "target": "dispose_1"},
             {"source": "loose", "target": "dispose_x"}]
    return model(blocks, links)


def test_a_valid_merge_table_raises_nothing():
    assert "E_CONVEYOR_MERGE" not in problem_codes(
        _merging([{"at": 5, "from": "side"}, {"at": 7, "from": "other", "priority": "merge"}]))


@pytest.mark.parametrize("bad", [
    4,                                                   # not a table
    {"at": 5},                                           # no from
    {"at": 5, "from": "side", "where": 1},               # unknown key
    {"at": 0, "from": "side"}, {"at": 10, "from": "side"}, {"at": "x", "from": "side"},
    {"at": 5, "from": "loose"},                          # does not link to the belt
    {"at": 5, "from": "nobody"},
    {"at": 5, "from": "side", "priority": "first"},      # unknown priority
])
def test_a_bad_merge_item_is_refused_by_code(bad):
    assert "E_CONVEYOR_MERGE" in problem_codes(_merging([bad])), bad


def test_two_merges_from_one_block_or_on_one_point_are_refused():
    assert "E_CONVEYOR_MERGE" in problem_codes(
        _merging([{"at": 3, "from": "side"}, {"at": 6, "from": "side"}]))
    assert "E_CONVEYOR_MERGE" in problem_codes(
        _merging([{"at": 5, "from": "side"}, {"at": 5, "from": "other"}]))
    assert "E_CONVEYOR_MERGE" in problem_codes(
        _merging([{"at": 4, "from": "side"}], stops=[{"at": 4, "time": 1}]))


def test_a_merge_on_a_non_accumulating_belt_is_refused():
    assert "E_CONVEYOR_MERGE" in problem_codes(
        _merging([{"at": 5, "from": "side"}], accumulating=False))


def test_a_belt_with_a_divert_is_refused_in_a_state_by_name():
    blocks, links = side_line(belt(diverts=[divert_at(4)]), arrivals=3)
    doc = model(blocks, links)
    doc["simulation"]["time_limit"] = 30
    assert "divert" in (state.refused_kind(doc) or "")      # a plant only (D918)
    assert state.take(doc, 3.0)["state"]["entities"]
