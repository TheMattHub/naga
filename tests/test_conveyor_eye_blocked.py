"""`<belt>.eye<k>.blocked` on every exposed belt (conveyor plan A2, Task 5).

Written from the brief and the physics only. API assumptions: the level is the model variable
"B1.eye0.blocked" (1 from the instant a part's front reaches the eye, 0 once no part's back is
still behind it); a part standing with its front exactly on the eye counts as having reached
it; writes at one instant are collapsed to the last value (a back clearing as the next front
arrives, bumper to bumper, leaves the level at 1 -- `_levels`).

Belt A (plain): 4 m at 0.5 m/s, spacing 0.5, parts 0.4 m, an eye at 3.0, the exit held by a hog
until 20, 6 parts arriving at 0, 1, ..., 5 (boarding at once: 0.5 m pitch = spacing). Part k
boards at k - 1, so its front is at x at (k - 1) + 2x. Everyone stands at t = 8 with fronts at
4.0, 3.5, 3.0, 2.5, 2.0, 1.5 (head arrives at 8; part 6 is at 1.5 at 5 + 3 = 8). Eye 3.0: front
reaches it at (k - 1) + 6, back clears when the front is at 3.4, 0.8 s later.
  part 1 [6, 6.8], part 2 [7, 7.8], part 3 [8, ...] stands across it (front on 3.0, back 2.6).
At 20 part 1 leaves and all move at once: part 3 clears 3.4 at 20 + 0.8 = 20.8; part 4 (front
2.5) reaches 3.0 at 21.0, clears 21.8; part 5 (2.0) [22, 22.8]; part 6 (1.5) [23, 23.8].

Belt B: belt A with a stop at 2.0 for 5 s (each part dwells 5 s). Parts ride free to the
stop: everything stands at 4 (part 1's front at 2.0; part 2 at 1.5, 3 at 1.0, 4 at 0.5, 5 at
0.0). Part 1 dwells 4-9, then is at 3.0 at 11 and clears 3.4 at 11.8. Part 2 reaches 2.0 as
part 1's front is 0.5 ahead: 9 + 1 = 10, dwells to 15, eye [17, 17.8]. Part k reaches the stop
1 s after part k - 1 leaves it: dwells 16-21 (3), 22-27 (4), 28-33 (5), 34-39 (6). Eyes:
(3) 21 + 2 = 23 to 23.8, (4) 29 to 29.8, (5) 35 to 35.8, (6) 41 to 41.8. Part 1 stands at the
end 13-20 and part 2 at 3.5 from 18, both past or short of the eye's interval, so no overlap.

Belt C (typed, cell 0.01, gap 0): 4 m at 0.5, the exit held until 20, an eye at 2.5; A 0.3 m
and B 0.6 m alternate A1 B2 A3 B4 A5 B6, boarding at 0, 2, 4, 6, 8, 10. Front at x at
(board time) + 2x. Packed noses at the end: A1 4.0, B2 3.7, A3 3.1, B4 2.8, A5 2.2, B6 1.9
(arriving 8, 9.4, 10.2, 11.6, 12.4, 13.8). Eye 2.5, front reaches at board + 5, back clears
when the front is 2.5 + length:
  A1 [5.0, 5.6]; B2 [7.0, 8.2]; A3 [9.0, 9.6]; B4 from 11.0 stands across (front 2.8, back 2.2).
At 20 the packed chain moves at once: B4's front reaches 3.1 (back at 2.5) at 20 + 0.6 = 20.6,
exactly as A5's front (2.2) reaches 2.5; A5 clears (front 2.8) at 21.2 as B6 (1.9) reaches 2.5;
B6 clears (front 3.1) at 20 + 2.4 = 22.4. So B4, A5 and B6 hand over bumper to bumper: one
blocked interval [11.0, 22.4].
"""
import copy

import pytest

from conftest import create, dispose
from test_conveyor_divert_spec import belt, hog, model, pick
from test_conveyor_eyes import eyed, traced
from test_conveyor_expose import _close, _exposed, _hold, _record

from nagasim import api
from nagasim.core.schema import validate_model_structured

NAME = "B1.eye0.blocked"


def _levels(log):
    """The writes of one variable as (t, value): the last write of an instant, changes only."""
    out = []
    for t, v in log:
        if out and abs(out[-1][0] - t) < 1e-9:
            out[-1] = (out[-1][0], v)
        else:
            out.append((t, v))
    changes, last = [], 0
    for t, v in out:
        if v != last:
            changes.append((t, v))
            last = v
    return changes


def _belt_a(stops=(), expose=True):
    conveyor = eyed([{"at": 3.0}], name="B1", length=4.0, speed=0.5, spacing=0.5,
                    part_length=0.4, exit_resource="R", stops=list(stops))
    conveyor["params"]["expose"] = expose
    hog_blocks, hog_links = hog("R", 20)
    blocks = [create(max_arrivals=6, dist_params={"value": 1.0}, start_time=0), conveyor,
              pick("R"), dispose(), *hog_blocks]
    links = [{"source": "create_1", "target": "B1"}, {"source": "B1", "target": "pick"},
             {"source": "pick", "target": "dispose_1"}, *hog_links]
    return model(blocks, links, ["R"], time_unit="seconds")


def _belt_b(expose=True):
    return _belt_a(stops=[{"at": 2.0, "time": 5.0, "resource": ""}], expose=expose)


_SIZES = {"A": 0.3, "B": 0.6}


def _belt_c(expose=True):
    conveyor = belt(name="B1", length=4.0, speed=0.5, exit_resource="R")
    conveyor["params"].update({"type": "roller", "cell": 0.01, "gap": 0.0,
                               "eyes": [{"at": 2.5}], "expose": expose})
    hog_blocks, hog_links = hog("R", 20)
    blocks, links = [], []
    for k, kind in enumerate("ABABAB"):
        blocks.append(create(name=f"c{k}", e_type=kind, max_arrivals=1, start_time=2.0 * k))
        links.append({"source": f"c{k}", "target": "B1"})
    blocks += [conveyor, pick("R"), dispose(), *hog_blocks]
    links += [{"source": "B1", "target": "pick"}, {"source": "pick", "target": "dispose_1"},
              *hog_links]
    doc = model(blocks, links, ["R"], time_unit="seconds")
    doc["entities"] = [{"name": n, "size": {"length": v, "width": 0.3, "height": 0.2}}
                       for n, v in _SIZES.items()]
    return doc


def test_a_plain_accumulating_belt_is_blocked_while_each_part_spans_the_eye():
    """Belt A (module docstring): parts 1 and 2 pass 3.0 free; part 3 stands across it from 8
    until the release at 20 lets its back clear at 20.8; parts 4-6 follow 1 s apart."""
    log, variables = _record(_belt_a(), [NAME], 60)
    _close(_levels(log[NAME]), [(6.0, 1), (6.8, 0), (7.0, 1), (7.8, 0), (8.0, 1), (20.8, 0),
                                (21.0, 1), (21.8, 0), (22.0, 1), (22.8, 0), (23.0, 1),
                                (23.8, 0)])
    assert variables[NAME] == 0


def test_a_stop_spaces_the_intervals_five_seconds_plus_the_pitch_apart():
    """Belt B (module docstring): one interval per part, 0.8 s long, parts leaving the stop at
    9, 15, 21, 27, 33, 39 (the eye is 1.0 m = 2 s past it)."""
    log, variables = _record(_belt_b(), [NAME], 80)
    want = []
    for start in (11.0, 17.0, 23.0, 29.0, 35.0, 41.0):
        want += [(start, 1), (start + 0.8, 0)]
    _close(_levels(log[NAME]), want)
    assert variables[NAME] == 0


def test_a_typed_belt_with_mixed_sizes_is_blocked_from_the_nose_to_the_back():
    """Belt C (module docstring): A1 [5.0, 5.6], B2 [7.0, 8.2], A3 [9.0, 9.6], then B4 stands
    across the eye from 11.0 and B4, A5, B6 hand over bumper to bumper (20.6, 21.2): one
    interval to 22.4."""
    log, variables = _record(_belt_c(), [NAME], 60)
    _close(_levels(log[NAME]), [(5.0, 1), (5.6, 0), (7.0, 1), (8.2, 0), (9.0, 1), (9.6, 0),
                                (11.0, 1), (22.4, 0)])
    assert variables[NAME] == 0


def test_a_hold_on_blocked_releases_at_the_first_front_edge():
    """Belt A with a part W that waits in a HOLD on VAR("B1.eye0.blocked") == 1: part 1's front
    reaches 3.0 at 6.0, so W leaves at 6.0 (W's own entry is at 0, the level 0 until then)."""
    doc = _belt_a()
    doc["blocks"] += [create(name="w", e_type="W", max_arrivals=1, start_time=0),
                      _hold("watch", f'VAR("{NAME}") == 1'), dispose(name="out")]
    doc["links"] += [{"source": "w", "target": "watch"}, {"source": "watch", "target": "out"}]
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"]
    log = api.run_model(doc, replications=1)["entity_log"]
    assert log[log["Type"] == "W"]["Exited At"].tolist() == [pytest.approx(6.0)]


@pytest.mark.parametrize("make", [_belt_a, _belt_b, _belt_c], ids=["plain", "stop", "typed"])
def test_expose_changes_no_log_and_no_eye_time(make):
    """Exposing the level adds no point to a section: entity log, every other frame and every
    eye firing are as without expose; and the unexposed belt writes no variable."""
    off = make(expose=False)
    plain, exposed = traced(off), traced(_exposed(copy.deepcopy(off)))
    assert [e.t for e in plain[1]] == [e.t for e in exposed[1]] and plain[1]
    assert plain[0]["entity_log"].equals(exposed[0]["entity_log"])
    for key, frame in plain[0].items():
        if hasattr(frame, "equals"):
            assert frame.equals(exposed[0][key]), key
    _log, variables = _record(off, [], 80)
    assert variables == {}
