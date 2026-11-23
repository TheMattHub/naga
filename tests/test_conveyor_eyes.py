"""Conveyor photo-eyes (D1347): a point that counts, traces, signals and assigns as a part's
front or back passes, by the same stage rules that move the parts.

Times are hand-derived. Defaults: length 10, speed 1, spacing 1, accumulating, parts every 1
from t = 0, so a free part's front is at x at its boarding time + x.
"""

import pytest

from conftest import create, dispose
from test_conveyor_divert_spec import (approx_all, belt, exits, hog, model, pick, plain_line,
                                       side_line)

from nagasim import api
from nagasim.core.trace import TraceRecorder
from nagasim.experiments import flatten_kpis


def eyed(eyes, **belt_args):
    extra = {key: belt_args.pop(key) for key in ("part_length", "part_length_unit",
                                                  "length_unit") if key in belt_args}
    conveyor = belt(**belt_args)
    conveyor["params"]["eyes"] = list(eyes)
    conveyor["params"].update(extra)
    return conveyor


def traced(doc):
    """Run *doc* once with a recorder; (results, the eye events in order)."""
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, trace=recorder)
    return results, [e for e in recorder.events if e.kind == "eye"]


def eye_row(results, k=0):
    frame = results["conveyor_eyes"]
    return frame[frame["Eye"] == k].iloc[0]


def test_an_empty_eyes_table_runs_the_belt_as_before():
    plain = api.run_model(plain_line(belt(), arrivals=5, gap=0.5), replications=1)
    empty = api.run_model(plain_line(eyed([]), arrivals=5, gap=0.5), replications=1)
    assert "conveyor_eyes" not in empty
    assert plain["entity_log"].equals(empty["entity_log"])
    assert plain["conveyor_summary"].equals(empty["conveyor_summary"])


def test_a_front_eye_fires_as_each_front_passes_and_changes_no_exit():
    """Part k boards at k - 1 and its front is at 2.5 at k + 1.5; it leaves at k + 9."""
    results, fired = traced(plain_line(eyed([{"at": 2.5}]), arrivals=6))
    assert [e.t for e in fired] == approx_all([2.5, 3.5, 4.5, 5.5, 6.5, 7.5])
    assert all(e.state == "front" and e.value == 0.0 and e.block == "belt" for e in fired)
    assert exits(results) == approx_all([10, 11, 12, 13, 14, 15])
    row = eye_row(results)
    assert (row["Fired"], row["At"], row["Edge"]) == (6, pytest.approx(2.5), "front")


@pytest.mark.parametrize("extra, expected", [
    ({"part_length": 0.4}, [2.9, 3.9, 4.9]),        # the back clears 2.5 when the front is at 2.9
    ({}, [3.5, 4.5, 5.5]),                          # no part_length: the spacing, 1
], ids=["part_length", "spacing"])
def test_a_back_eye_fires_a_part_length_later(extra, expected):
    _results, fired = traced(plain_line(eyed([{"at": 2.5, "edge": "back"}], **extra),
                                        arrivals=3))
    assert [e.t for e in fired] == approx_all(expected)
    assert all(e.state == "back" for e in fired)


def _queue_line(eyes, accumulating):
    """4 parts every 1 onto a belt whose end stop R a hog holds until 20."""
    hog_blocks, hog_links = hog("R", 20)
    conveyor = eyed(eyes, accumulating=accumulating, exit_resource="R")
    blocks = [create(max_arrivals=4, dist_params={"value": 1.0}, start_time=0), conveyor,
              pick("R"), dispose(), *hog_blocks]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "pick"},
             {"source": "pick", "target": "dispose_1"}, *hog_links]
    return model(blocks, links, ["R"])


@pytest.mark.parametrize("accumulating", [True, False], ids=["accumulating", "stopping"])
def test_an_eye_in_a_queue_fires_when_the_front_gets_there(accumulating):
    """Parts 1 and 2 pass 8.5 at 8.5 and 9.5; the end holds until 20, exits 20, 21, 22, 23.
    Accumulating: part 3 waits for room past the eye until part 1 leaves at 20, plus the
    section lag (2 x 1 - 1.5) / 1 = 0.5; part 4 a spacing behind it. Stopping: part 3's front
    is at 8 when the belt stops at 10, and the belt restarts at 20."""
    results, fired = traced(_queue_line([{"at": 8.5}], accumulating))
    assert [e.t for e in fired] == approx_all([8.5, 9.5, 20.5, 21.5])
    assert exits(results) == approx_all([20, 21, 22, 23])
    plain = api.run_model(_queue_line([], accumulating), replications=1)
    assert exits(plain) == exits(results)


def test_an_eye_assigns_the_part_before_a_divert_reads_it():
    """The eye at 2 counts `turn` and stamps its parity: parts 1 and 3 leave sideways at 5
    and 7 (boarding 0 and 2), parts 2 and 4 reach the end at 11 and 13."""
    eye = {"at": 2.0, "assign": [{"target": "turn", "expr": "turn + 1"},
                                 {"target": "entity.lane", "expr": "MODULO(turn, 2)"}]}
    blocks, links = side_line(eyed([eye], diverts=[{"at": 5.0, "condition": "entity.lane == 1"}]),
                              arrivals=4)
    doc = model(blocks, links)
    doc["variables"] = [{"name": "turn", "initial": 0}]
    results = api.run_model(doc, replications=1)
    assert exits(results) == approx_all([5.0, 7.0, 11.0, 13.0])
    row = results["conveyor_diverts"].iloc[0]
    assert (row["Diverted"], row["Passed"]) == (2, 2)


def _hold(name, **params):
    return {"name": name, "description": name, "type": "HOLD", "position": [1, 1],
            "params": params}


def test_an_eye_signal_and_assignment_release_holds():
    """The eye at 2.5 fires at 2.5 and 3.5. The signal HOLD opens on the first firing, the
    HOLD on `turn >= 2` on the second, at the firing instant as after an ASSIGN."""
    eye = {"at": 2.5, "signal": "seen", "assign": [{"target": "turn", "expr": "turn + 1"}]}
    blocks = [create(max_arrivals=2, dist_params={"value": 1.0}, start_time=0), eyed([eye]),
              dispose(),
              create(name="sig_src", e_type="S", max_arrivals=1, start_time=0),
              _hold("by_signal", mode="Signal", signal_name="seen"),
              create(name="cond_src", e_type="C", max_arrivals=1, start_time=0),
              _hold("by_condition", mode="Condition", conditions=["turn >= 2"]),
              dispose(name="out")]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"},
             {"source": "sig_src", "target": "by_signal"},
             {"source": "by_signal", "target": "out"},
             {"source": "cond_src", "target": "by_condition"},
             {"source": "by_condition", "target": "out"}]
    doc = model(blocks, links)
    doc["variables"] = [{"name": "turn", "initial": 0}]
    results = api.run_model(doc, replications=1)
    assert exits(results, "S") == approx_all([2.5])
    assert exits(results, "C") == approx_all([3.5])


def test_eye_places_follow_the_belts_units():
    """1000 cm long, the eye at 250 cm, back edge, parts 40 cm: it fires as the front is at
    2.9 m, and the frame gives the sensor's place in base units."""
    conveyor = eyed([{"at": 250, "edge": "back"}], length=1000, length_unit="centimeters",
                    part_length=40, part_length_unit="centimeters")
    results, fired = traced(plain_line(conveyor, arrivals=1))
    assert [e.t for e in fired] == approx_all([2.9])
    assert eye_row(results)["At"] == pytest.approx(2.5)


def test_eye_kpis_flatten_and_other_belts_have_none():
    flat = flatten_kpis(api.run_model(plain_line(eyed([{"at": 2.5}]), arrivals=6),
                                      replications=1))
    assert dict(zip(flat["KPI"], flat["Value"]))["eye/belt/0/Fired"] == 6
    plain = flatten_kpis(api.run_model(plain_line(belt(), arrivals=3), replications=1))
    assert not any(k.startswith("eye/") for k in plain["KPI"].tolist())
