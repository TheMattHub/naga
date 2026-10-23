"""The tape of a run (PHASE7 7.7, D197): `core/tape.py` against `Timeline.state_at`.

The reference is `Timeline` itself, asked out of order (its rewind path): there is no
second reducer here, in Python or anywhere else (ARCHITECTURE section 6, rule 13).
"""
import json
import random
import re
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core import tape, trace as trace_module
from nagasim.core.network import build_network, point_on_path
from nagasim.core.playback import Timeline
from nagasim.core.schema import load_layout
from nagasim.core.trace import TraceRecorder

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
# Submodels, fleets on a network, levels with assets, motion and chargers, system
# dynamics, agents in a space.
SIX = ("two_cell_line.json", "warehouse_forklifts.json", "warehouse_library.json",
       "automation_floor.json", "sd_sir.json", "abm_nagel_schreckenberg.json")
_RECORDED: dict = {}


def recorded(model: str, max_events=None):
    """(layout, TraceRecorder) of one replication of a bundled model: run once, or
    again with a recorder that stops at *max_events*."""
    if model in _RECORDED and max_events is None:
        return _RECORDED[model]
    recorder = TraceRecorder(max_events)
    api.run_model(str(MODELS / model), replications=1, seed=0, warmup=0, run_length=200,
                  trace=recorder)
    record = (load_layout(str(MODELS / model)), recorder)
    if max_events is None:
        _RECORDED[model] = record
    return record


def assert_tape_equals_timeline(layout: dict, events, run: dict, picks: int = 40) -> None:
    """Every frame picked (shuffled, so the reference rewinds) says what a fresh
    `Timeline` says at that instant. Also used on the tape read back from a player
    file (`tests/test_player.py`)."""
    scene, times, frames = run["scene"], run["t"], run["frames"]
    names = [block["name"] for block in scene["blocks"]]
    blocks = {block["name"]: block for block in scene["blocks"]}
    floor = scene["view"] == "layout"
    network = build_network(layout.get("network"))
    reference = Timeline(events)
    indices = random.Random(0).sample(range(len(times)), min(picks, len(times)))
    assert indices != sorted(indices) or len(indices) < 3
    for k in indices:
        t, frame = times[k], frames[k]
        state = reference.state_at(t)
        where = f"sample {k} (t = {t})"
        assert frame["n"] == [state["counts"].get(name, 0) for name in names], where
        assert frame["s"] == [state["in_service"].get(name, 0) for name in names], where
        assert frame["k"] == [reference.created_until(t), reference.disposed_until(t)], where
        if not floor:       # every block is drawn: no entity may be lost
            assert sum(frame["n"]) == sum(state["counts"].values()), where
        assert {scene["resources"][index]: {"state": lamp, "busy": busy, "capacity": capacity}
                for index, lamp, busy, capacity in frame.get("r", [])} == state["resources"], where
        for index, name in enumerate(names):
            waiting = state["queues"].get(name, [])
            assert frame["n"][index] - frame["s"][index] == len(waiting), (where, name)
            row = frame.get("q", {}).get(str(index), [[], []])
            types = [scene["types"][i]["name"] for i in row[0]]
            assert types == [state["entities"][e][1].upper() for e in waiting[:tape.ROW]], where
            assert len(row[1]) == min(frame["s"][index], tape.ROW), (where, name)
        levels = {scene["levels"][int(index)]["name"]: value
                  for index, value in frame.get("lv", {}).items()}
        expected = {name: value for name, value in state["levels"].items() if value is not None}
        assert levels == pytest.approx(expected, abs=1e-5), where
        tokens = len(frame.get("tok", [])) + frame.get("tokx", 0)
        if not floor:
            assert tokens == len(state["in_transit"]), where
        for entity, kind, x, y in frame.get("tok", []):
            source, target, progress, e_type, path, _branch = state["in_transit"][entity]
            assert scene["types"][kind]["name"] == e_type.upper(), where
            path = [node for node in path if network.has(node)]
            if floor and path:
                want = point_on_path(network, path, progress)
            else:       # on the line between the two block centres, at its progress
                (x0, y0), (x1, y1) = ((blocks[source]["x"], blocks[source]["y"]),
                                      (blocks[target]["x"], blocks[target]["y"]))
                share = min(max(progress, 0.0), 1.0)
                want = (x0 + (x1 - x0) * share, y0 + (y1 - y0) * share)
            assert [x, y] == pytest.approx(want, abs=2e-3), (where, entity)
        if k > 0 and "mvx" not in frame:        # the moves of the step, as the scene names them
            want = [[e_type.upper(), source, target, when]
                    for entity, e_type, source, target, when, _branch
                    in reference.moves_between(times[k - 1], t)
                    if entity not in state["in_transit"] and source in blocks and target in blocks]
            got = [[scene["types"][i]["name"], names[a], names[b], when]
                   for i, a, b, when in frame.get("mv", [])]
            assert got == want, where
        if floor:
            placed = 0
            for (fleet, unit), (path, progress, moving) in state["movers"].items():
                path = [node for node in path if network.has(node)]
                if not path:
                    continue
                placed += 1
                x, y = point_on_path(network, path, progress)
                entry = next(e for e in frame["veh"]
                             if (scene["fleets"][e[0]], e[1]) == (fleet, unit))
                assert entry[2:4] == pytest.approx([x, y], abs=1e-3), where
                assert entry[5] == moving, where
            assert len(frame.get("veh", [])) == placed, where
            agents = {(scene["pops"][pop], index): (x, y)
                      for pop, index, x, y, _colour in frame.get("ag", [])}
            assert len(agents) + frame.get("agx", 0) == len(state["agents"]), where
            for key, (x, y) in agents.items():
                assert (x, y) == pytest.approx(state["agents"][key][:2], abs=1e-3), where


@pytest.mark.parametrize("model", SIX)
@pytest.mark.parametrize("view", tape.VIEWS)
def test_state_at_t_equals_timeline(model, view):
    layout, recorder = recorded(model)
    run = tape.TapeWriter(layout, view).run(recorder, 400)
    assert len(run["t"]) == len(run["frames"]) == 401
    assert run["t"][0] == 0.0 and run["t"][-1] == Timeline(recorder.events).duration
    assert run["t"] == sorted(run["t"])
    assert_tape_equals_timeline(layout, recorder.events, run)
    json.dumps(run)                                 # plain data, nothing else


def test_the_six_models_put_every_frame_key_on_a_tape():
    """The check above would pass on a writer that left a key out everywhere."""
    seen = set()
    for model in SIX:
        layout, recorder = recorded(model)
        for view in tape.VIEWS:
            for frame in tape.TapeWriter(layout, view).run(recorder, 200)["frames"]:
                seen |= set(frame)
    assert seen >= {"n", "s", "st", "u", "q", "r", "lv", "k", "tok", "veh", "ag", "mv"}


def test_the_scene_holds_the_layouts_geometry_and_nothing_else():
    layout, recorder = recorded("warehouse_forklifts.json")
    flat = tape.TapeWriter(layout, "schematic").run(recorder, 20)["scene"]
    floor = tape.TapeWriter(layout, "layout").run(recorder, 20)["scene"]
    by_name = {block["name"]: block for block in layout["blocks"]}
    for block in flat["blocks"]:
        assert [block["x"], block["y"]] == by_name[block["name"]]["position"]
        assert (block["w"], block["h"]) == (120.0, 60.0)
    assert all((block["w"], block["h"]) == (6.0, 3.0) for block in floor["blocks"])
    assert flat["segments"] == [] and len(floor["segments"]) == len(layout["network"]["segments"])
    assert len(flat["links"]) == len(layout["links"])
    assert set(flat) == {"view", "box", "blocks", "links", "segments", "types", "resources",
                         "levels", "fleets", "pops", "colors"}
    assert all(set(block) <= {"name", "label", "x", "y", "w", "h", "q"} for block in flat["blocks"])
    x0, y0, x1, y1 = floor["box"]
    assert all(x0 <= x <= x1 and y0 <= y <= y1 for x, y, *_ in
               [(b["x"], b["y"]) for b in floor["blocks"]])


def test_an_auto_named_asset_is_labelled_with_its_description():
    blocks = [{"name": "submodel_3", "description": "Paint shop", "type": "SUBMODEL",
               "position": [0, 0], "params": {}},
              {"name": "saw", "description": "the saw", "type": "PROCESS", "position": [200, 0],
               "params": {"resource_name": "Saw"}}]
    scene = tape.TapeWriter({"blocks": blocks, "links": [{"source": "submodel_3", "target": "saw"},
                                                         {"source": "saw", "target": "gone"}]},
                            "schematic").run([], 10)["scene"]
    assert [block["label"] for block in scene["blocks"]] == ["Paint shop", "saw"]
    assert [block.get("q") for block in scene["blocks"]] == [None, 1]
    assert scene["links"] == [[0, 1]]           # a link to a block that is not drawn is not drawn


def test_past_a_cap_a_frame_holds_a_count(monkeypatch):
    """Review focus: a burst may not make the file grow without bound, and what is
    left out is counted, never silently dropped."""
    layout, recorder = recorded("warehouse_forklifts.json")
    full = tape.TapeWriter(layout, "schematic").run(recorder, 100)
    busiest = max(full["frames"], key=lambda frame: len(frame.get("tok", [])))
    assert len(busiest["tok"]) > 1 and "tokx" not in busiest
    moves = max(len(frame.get("mv", [])) for frame in full["frames"])
    assert moves > 1
    monkeypatch.setattr(tape, "MAX_TOKENS", 1)
    monkeypatch.setattr(tape, "MAX_MOVES", 1)
    monkeypatch.setattr(tape, "ROW", 1)
    capped = tape.TapeWriter(layout, "schematic").run(recorder, 100)
    for before, after in zip(full["frames"], capped["frames"]):
        assert len(after.get("tok", [])) <= 1
        assert len(after.get("tok", [])) + after.get("tokx", 0) == len(before.get("tok", []))
        if len(before.get("mv", [])) > 1:
            assert "mv" not in after and after["mvx"] == len(before["mv"])
        assert all(len(row[0]) <= 1 and len(row[1]) <= 1 for row in after.get("q", {}).values())
    assert capped["frames"][-1]["n"] == full["frames"][-1]["n"]     # the counts are whole
    agents_layout, agents = recorded("abm_nagel_schreckenberg.json")
    crowded = tape.TapeWriter(agents_layout, "layout").run(agents, 20)["frames"][-1]
    assert len(crowded["ag"]) == 1 and crowded["agx"] == 5


def test_a_truncated_trace_gives_a_tape_ending_at_its_last_event():
    layout, recorder = recorded("two_cell_line.json", max_events=300)
    assert recorder.truncated and len(recorder.events) == 300
    run = tape.TapeWriter(layout, "schematic").run(recorder, 50)
    assert run["truncated"] is True and run["events"] == 300
    assert run["t"][-1] == max(event.t for event in recorder.events) < 200


def test_an_empty_trace_is_one_frame_and_a_bare_event_list_is_a_trace():
    layout, recorder = recorded("two_cell_line.json")
    empty = tape.TapeWriter(layout, "schematic").run([], 50)
    assert empty["t"] == [0.0] and len(empty["frames"]) == 1 and empty["truncated"] is False
    assert sum(empty["frames"][0]["n"]) == 0
    assert tape.TapeWriter(layout, "schematic").run(list(recorder.events), 30) \
        == tape.TapeWriter(layout, "schematic").run(recorder, 30)


def test_a_live_timeline_gives_the_recorded_frame():
    """The monitor page (later) feeds `frame` from a live timeline, which keeps no
    event log and only moves forward: same frames, sample by sample."""
    layout, recorder = recorded("two_cell_line.json")
    events = sorted(recorder.events, key=lambda event: event.t)
    times = tape.TapeWriter(layout, "schematic").run(recorder, 60)["t"]
    recorded_writer, live_writer = (tape.TapeWriter(layout, "schematic") for _ in range(2))
    whole, live = Timeline(events), Timeline([], live=True)
    cursor, previous = 0, None
    for t in times:
        chunk = []
        while cursor < len(events) and events[cursor].t <= t:
            chunk.append(events[cursor])
            cursor += 1
        live.append(chunk)
        assert live_writer.frame(live, t, previous) == recorded_writer.frame(whole, t, previous), t
        previous = t


def test_views_and_the_default_view():
    assert tape.default_view({}) == "schematic"
    assert tape.default_view({"network": {"nodes": [{"name": "a", "x": 0, "y": 0}]}}) == "layout"
    assert tape.default_view({"agent_types": [{"name": "A"}]}) == "schematic"
    assert tape.default_view({"agent_types": [{"name": "A", "space": {"kind": "grid"}}]}) == "layout"
    with pytest.raises(ValueError, match="unknown view 'top'"):
        tape.TapeWriter({}, "top")


def test_the_writer_never_names_a_trace_event():
    """Rule 13: the tape is read from `Timeline`, so no event kind appears in the
    module (a second reducer would have to name them)."""
    source = Path(tape.__file__).read_text(encoding="utf-8")
    named = [kind for kind in sorted(trace_module._RECORDED_EVENTS)
             if re.search(rf"\b{kind}\b", source)]
    assert named == []
    assert ".kind" not in source
