"""Area links beyond the spec cases (conveyor system A10, D1369): ports, routing walks, deadlock,
flattening, the document, states and playback. An area link is a delay with a capacity (A2 Task
6, D1391); an old file's transfer_time migrates into the delay (tests/test_link_delay_capacity.py).

Hand derivations; belts speed 1, spacing 1, accumulating, a part at x at (boarding) + x while
nothing holds it; times in the model's base unit (minutes, the default) unless a test says so.
"""
import math

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_divert_spec import belt, divert_at

from nagasim import api, state
from nagasim.core import submodels
from nagasim.core.clipboard import copy_selection, paste_fragment
from nagasim.core.document import ModelDocument
from nagasim.core.playback import Timeline
from nagasim.core.schema import validate_model_structured
from nagasim.core.trace import TraceRecorder


def sim(**extra):
    return {"time_limit": None, "warmup": 0, "replications": 1, **extra}


def exit_times(results, e_type=None):
    log = results["entity_log"]
    if e_type is not None:
        log = log[log["Type"] == e_type]
    return sorted(log["Exited At"].tolist())


def close(got, want):
    assert len(got) == len(want), got
    assert all(math.isclose(g, w, abs_tol=1e-9) for g, w in zip(got, want)), got


def const(value, **unit):
    """A link delay of *value* (a Constant), *unit* an optional time_unit."""
    return {"distribution": "Constant", "dist_params": {"value": value}, **unit}


def codes(doc):
    return {p.code for p in validate_model_structured(doc) if p.severity == "error"}


# -- units ----------------------------------------------------------------------------------
def test_an_area_link_delay_unit_converts_to_the_base():
    """Model in minutes, delay 600 seconds = 10 minutes: one part at 0 leaves at 10."""
    blocks = [create(max_arrivals=1, start_time=0), dispose()]
    links = [{"source": "create_1", "target": "dispose_1",
              "delay": const(600, time_unit="seconds"), "capacity": 1}]
    close(exit_times(api.run_model(layout(blocks, links, simulation=sim()), replications=1)),
          [10.0])


# -- ports: exits keep the drawn link's order -----------------------------------------------
def test_a_divert_exit_through_an_area_link():
    """Belt 10 with divert d0 at 3 (always taken). Links listed d0 first, by source_port: the d0
    link is the area link (delay 7, capacity 1) to dispose_side. Parts board at 0 and 1
    (gap 1): part 0 is pushed off at 3, enters the link, reaches dispose_side at 10. Part 1 is at
    the divert at 4: the link is full until 10, so it waits at the divert (holding its cell),
    enters at 10, leaves at 17. Nothing reaches the end."""
    blocks = [create(max_arrivals=2, dist_params={"value": 1.0}, start_time=0),
              belt(diverts=[divert_at(3)]), dispose(), dispose(name="dispose_side")]
    links = [{"source": "create_1", "target": "belt"},
             {"source": "belt", "target": "dispose_side", "source_port": "d0",
              "delay": const(7), "capacity": 1},
             {"source": "belt", "target": "dispose_1", "source_port": "out"}]
    results = api.run_model(layout(blocks, links, simulation=sim()), replications=1)
    close(exit_times(results), [10.0, 17.0])
    assert results["material_flow_summary"].query("From == 'belt'")["To"].tolist() == \
        ["dispose_side"]        # counted against the real target, never the hidden link


def _transfer(*links):
    xfer = {"name": "xfer", "description": "xfer", "type": "CONVEYOR_TRANSFER", "position": [1, 0],
            "params": {"mechanism": "popup", "transfer_speed": 0.5,
                       "exits": [{"port": "a", "angle": 0}, {"port": "b", "angle": 0}]}}
    blocks = [create(e_type="BOX", entities_per_arrival=3, max_arrivals=1, start_time=0), xfer,
              dispose(name="dispose_a"), dispose(name="dispose_b")]
    doc = layout(blocks, [{"source": "create_1", "target": "xfer"}, *links],
                 simulation=sim(time_unit="seconds"))
    doc["entities"] = [{"name": "BOX", "size": {"length": 0.4, "width": 0.3, "height": 0.2}}]
    return doc


def test_a_transfer_exit_through_an_area_link():
    """Pop-up, two straight exits (no lift, no cross), round robin a, b, a; exit a is an area link
    (5 s, capacity 1) listed second. Its handshake is the link taking the load. Load 1 -> a at 0
    (in the link 0-5); load 2 -> b at 0; load 3 -> a: the link is full until 5, so the module
    holds it and it enters at 5, leaves at 10. Exits 0, 5, 10 (with the links in listed order the
    loads would leave 0, 0, 5)."""
    doc = _transfer({"source": "xfer", "target": "dispose_b", "source_port": "b"},
                    {"source": "xfer", "target": "dispose_a", "source_port": "a",
                     "delay": const(5), "capacity": 1})
    close(exit_times(api.run_model(doc, replications=1)), [0.0, 5.0, 10.0])


# -- routing walks (R50) --------------------------------------------------------------------
def _route(rule):
    route = {"name": "route", "description": "route", "type": "DECIDE", "position": [1, 0],
             "params": {"mode": "Rule", "rule": rule, "probs": [], "conditions": [],
                        "policy": "", "observation": [], "fallback": "first", "queue_exprs": []}}
    creates = [create(name=f"c{i}", e_type=f"E{i}", max_arrivals=1, start_time=t)
               for i, t in enumerate((0.0, 1.0, 1.2))]
    blocks = [*creates, route, belt("B1", length=2.0), belt("B2", length=2.0), dispose()]
    links = [{"source": f"c{i}", "target": "route"} for i in range(3)]
    links += [{"source": "route", "target": "B1", "delay": const(0.5), "capacity": 10},
              {"source": "route", "target": "B2"},
              {"source": "B1", "target": "dispose_1"}, {"source": "B2", "target": "dispose_1"}]
    return layout(blocks, links, simulation=sim())


def test_the_default_target_walk_ends_before_an_area_link():
    """round_robin limit 1 over B1 (behind an area link of 0.5) and B2. The walk of exit B1 ends
    before the link, so it counts nothing: E0 -> B1 (link 0-0.5, leaves B1 at 2.5), E1 at 1 -> B2
    (under way to 3), E2 at 1.2 -> B1 at once: link 1.2-1.7, boards at 1.7 (E0 boarded at 0.5),
    leaves at 3.7. Validation warns that exit B1 counts nothing."""
    doc = _route({"kind": "round_robin", "limit": 1})
    results = api.run_model(doc, replications=1)
    close(exit_times(results, "E2"), [3.7])
    _env, model = run_env(doc, 10)
    assert model.blocks["route"]._rule_point().chain(0) == []
    warned = [p.fields.get("case") for p in validate_model_structured(doc)
              if p.code == "W_ROUTE_RULE"]
    assert "uncounted" in warned


def test_a_named_target_beyond_an_area_link_is_reached_through_it():
    """Same, exit 0 named "b1:B1": B1 lies beyond the link, so E0 counts until it leaves B1 at
    2.5; E2 at 1.2 finds both exits full (E1 under way to 3) and waits; at 2.5 B1 frees, E2
    enters the link, boards at 3.0, leaves at 5.0. No problem is reported."""
    doc = _route({"kind": "round_robin", "limit": 1, "exits": ["b1:B1", "B2"]})
    close(exit_times(api.run_model(doc, replications=1), "E2"), [5.0])
    assert not [p for p in validate_model_structured(doc)
                if p.code in ("E_DECIDE_RULE", "W_ROUTE_RULE")]


# -- blocking and deadlock ------------------------------------------------------------------
def test_a_loop_closed_by_a_full_area_link_is_named_as_a_deadlock():
    """A (2 cells) -> link (capacity 1, delay 1) -> B (1 cell) -> A, five parts into A
    at 0 and nothing leaves the loop: four places, so it locks up. The error names the link."""
    blocks = [create(entities_per_arrival=5, max_arrivals=1, start_time=0),
              belt("A", length=2.0), belt("B", length=1.0),
              create(name="other", max_arrivals=1, start_time=0), dispose()]
    links = [{"source": "create_1", "target": "A"},
             {"source": "A", "target": "B", "delay": const(1), "capacity": 1},
             {"source": "B", "target": "A"}, {"source": "other", "target": "dispose_1"}]
    with pytest.raises(Exception, match=r"deadlock.*A -> B link"):
        api.run_model(layout(blocks, links, simulation=sim()), replications=1)


def test_a_merge_fed_over_an_area_link():
    """Belt 10 with a merge at 5 from `side`; side's one part enters the link (delay 2)
    at 0, joins at 2 (nothing on the main belt) and leaves the end at 2 + 5 = 7."""
    blocks = [create(max_arrivals=0, start_time=1000), belt(merges=[{"at": 5, "from": "side"}]),
              dispose(), create(name="side", e_type="Side", max_arrivals=1, start_time=0)]
    links = [{"source": "create_1", "target": "belt"},
             {"source": "side", "target": "belt", "delay": const(2), "capacity": 1},
             {"source": "belt", "target": "dispose_1"}]
    doc = layout(blocks, links, simulation=sim(time_limit=100))
    close(exit_times(api.run_model(doc, replications=1), "SIDE"), [7.0])


# -- validation -----------------------------------------------------------------------------
@pytest.mark.parametrize("fields, code", [
    ({"transfer_time": 1, "delay": const(1)}, "E_LINK_AREA"),      # an old field left over
    ({"delay": const(1), "capacity": -1}, "E_FIELD_RANGE"),
    ({"delay": const(1), "capacity": 1.5}, "E_FIELD_TYPE"),
    ({"delay": const(1, time_unit="fortnights"), "capacity": 1}, "E_TIME_UNIT"),
])
def test_a_bad_area_link_is_refused(fields, code):
    blocks = [create(max_arrivals=1, start_time=0), dispose()]
    doc = layout(blocks, [{"source": "create_1", "target": "dispose_1", **fields}],
                 simulation=sim())
    assert code in codes(doc)


def test_a_good_area_link_has_no_problem():
    blocks = [create(max_arrivals=1, start_time=0), dispose()]
    doc = layout(blocks, [{"source": "create_1", "target": "dispose_1",
                           "delay": const(1, time_unit="seconds"), "capacity": 2}],
                 simulation=sim())
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"
                or p.code == "W_UNKNOWN_KEY"]


# -- flattening -----------------------------------------------------------------------------
def _cell(belt_params, links, extra_blocks=()):
    """A definition `Cell`: in -> feeder -> belt -> back/out, with *belt_params* on the belt."""
    blocks = [{"name": "in", "type": "SUB_ENTRY", "position": [0, 0], "params": {"port": "in"}},
              {"name": "feeder", "type": "DELAY", "position": [0, 0],
               "params": {"distribution": "Constant", "dist_params": {"value": 0}}},
              {**belt(name="lane", length=4.0), "position": [0, 0]},
              {"name": "back", "type": "DELAY", "position": [0, 0],
               "params": {"distribution": "Constant", "dist_params": {"value": 0}}},
              {"name": "out", "type": "SUB_EXIT", "position": [0, 0], "params": {"port": "out"}},
              *extra_blocks]
    blocks[2]["params"].update(belt_params)
    return {"parameters": [], "blocks": blocks, "links": links}


def _flat(definition, outer_link=None):
    doc = layout([create(max_arrivals=1, start_time=0),
                  {"name": "I1", "type": "SUBMODEL", "position": [0, 0],
                   "params": {"definition": "Cell", "arguments": {}}}, dispose()],
                 [outer_link or {"source": "create_1", "target": "I1"},
                  {"source": "I1", "target": "dispose_1"}], simulation=sim())
    doc["submodels"] = {"Cell": definition}
    return submodels.flatten(doc)


_INNER = [{"source": "in", "target": "feeder"}, {"source": "feeder", "target": "lane"},
          {"source": "lane", "target": "out"}, {"source": "lane", "target": "back"},
          {"source": "back", "target": "out"}]


def _block(flat, name):
    return next(b for b in flat["blocks"] if b["name"] == name)


def test_flattening_renames_reverse_to():
    flat = _flat(_cell({"reversible": True, "reverse_to": "back"}, _INNER))
    assert _block(flat, "I1/lane")["params"]["reverse_to"] == "I1/back"
    flat = _flat(_cell({"reversible": True, "reverse_to": "in"}, _INNER))
    assert _block(flat, "I1/lane")["params"]["reverse_to"] == "in"     # the port stays


def test_flattening_renames_a_merges_from():
    links = [*_INNER[:3], {"source": "back", "target": "lane"}]
    flat = _flat(_cell({"merges": [{"at": 2.0, "from": "back"}]}, links))
    assert _block(flat, "I1/lane")["params"]["merges"][0]["from"] == "I1/back"


def test_flattening_keeps_area_fields_and_ports():
    """An inner link keeps its fields and its source_port; an outer link into the instance keeps
    its own through the port contraction; an inner hop's fields ride on the rewired link."""
    links = [{"source": "in", "target": "feeder", "delay": const(3), "capacity": 2},
             {"source": "feeder", "target": "lane", "delay": const(2, time_unit="seconds"),
              "capacity": 1},
             {"source": "lane", "target": "out", "source_port": "out", "delay": const(4)}]
    flat = _flat(_cell({}, links))
    by_pair = {(k["source"], k["target"]): k for k in flat["links"]}
    assert by_pair["I1/feeder", "I1/lane"] == {
        "source": "I1/feeder", "target": "I1/lane", "delay": const(2, time_unit="seconds"),
        "capacity": 1}
    assert by_pair["create_1", "I1/feeder"]["delay"] == const(3)           # the inner hop's
    assert by_pair["create_1", "I1/feeder"]["capacity"] == 2
    assert by_pair["I1/lane", "dispose_1"]["source_port"] == "out"
    assert by_pair["I1/lane", "dispose_1"]["delay"] == const(4)
    # A2-R7 (final review I4): an outer delay would hide the inner area link (time and places)
    with pytest.raises(submodels.SubmodelError, match="lost behind the delay"):
        _flat(_cell({}, links), {"source": "create_1", "target": "I1", "delay": const(9)})
    timed = [{k: v for k, v in links[0].items() if k != "capacity"}, *links[1:]]
    outer = _flat(_cell({}, timed), {"source": "create_1", "target": "I1", "delay": const(9)})
    rewired = {(k["source"], k["target"]): k for k in outer["links"]}["create_1", "I1/feeder"]
    assert rewired["delay"] == const(9)                                     # the outer link's
    assert "capacity" not in rewired        # D1391: a capacity travels with its own delay only


def test_an_outer_delay_into_an_inner_area_link_is_refused():
    """A2-R7 (final review I4), the reviewer's case: parts -> I1 with a delay of 1, inside
    in -> st an area link (delay 3, capacity 1). Flattened, the first delay won and the
    capacity was dropped (three parts out at 6, 6, 6, no problem); it is refused again, with
    its own case. With no outer delay the area link holds: 8, 11, 14."""
    def doc(outer_delay):
        station = {"name": "st", "type": "DELAY", "position": [0, 0],
                   "params": {"distribution": "Constant", "dist_params": {"value": 5}}}
        definition = {"parameters": [], "blocks": [
            {"name": "in", "type": "SUB_ENTRY", "position": [0, 0], "params": {"port": "in"}},
            station,
            {"name": "out", "type": "SUB_EXIT", "position": [0, 0], "params": {"port": "out"}}],
            "links": [{"source": "in", "target": "st", "delay": const(3), "capacity": 1},
                      {"source": "st", "target": "out"}]}
        outer = {"source": "create_1", "target": "I1"}
        if outer_delay:
            outer["delay"] = const(outer_delay)
        model = layout([create(entities_per_arrival=3, max_arrivals=1, start_time=0),
                        {"name": "I1", "type": "SUBMODEL", "position": [0, 0],
                         "params": {"definition": "Cell", "arguments": {}}}, dispose()],
                       [outer, {"source": "I1", "target": "dispose_1"}],
                       simulation=sim(time_limit=100))
        model["submodels"] = {"Cell": definition}
        return model
    found = [p for p in validate_model_structured(doc(1)) if p.code == "E_LINK_AREA"]
    assert [p.fields["case"] for p in found] == ["port_delay"], found
    from nagasim.core.plainlines import plain
    assert plain(found[0]).startswith("Link I1/in->I1/st"), plain(found[0])
    with pytest.raises(Exception, match="lost behind the delay"):
        api.run_model(doc(1), replications=1)
    assert not [p for p in validate_model_structured(doc(0)) if p.severity == "error"]
    close(exit_times(api.run_model(doc(0), replications=1)), [8.0, 11.0, 14.0])


# -- document, clipboard, states, playback --------------------------------------------------
def _burst_doc():
    blocks = [create(entities_per_arrival=3, dist_params={"value": 1000.0}, start_time=0),
              dispose()]
    return layout(blocks, [{"source": "create_1", "target": "dispose_1", "delay": const(10),
                            "capacity": 2}], simulation=sim(time_limit=100))


def test_the_document_keeps_and_edits_area_fields(tmp_path):
    doc = ModelDocument(_burst_doc())
    assert doc.link_area("create_1", "dispose_1") == {"capacity": 2}
    assert doc.link_delay("create_1", "dispose_1") == const(10)
    assert doc.to_layout()["links"][0]["capacity"] == 2
    doc.save(tmp_path / "m.json")           # the file pins the unit "base" meant
    saved = ModelDocument.load(tmp_path / "m.json")
    assert saved.link_area("create_1", "dispose_1") == {"capacity": 2}
    assert saved.to_layout()["links"] == [
        {"source": "create_1", "target": "dispose_1",
         "delay": const(10, time_unit="minutes"), "capacity": 2}]
    doc.set_link_area("create_1", "dispose_1", {"capacity": 5})
    assert doc.to_layout()["links"][0] == {"source": "create_1", "target": "dispose_1",
                                           "delay": const(10), "capacity": 5}
    doc.undo()
    assert doc.link_area("create_1", "dispose_1")["capacity"] == 2
    for key in ("speed", "transfer_time"):
        with pytest.raises(ValueError, match="unknown key"):
            doc.set_link_area("create_1", "dispose_1", {key: 1})
    assert not [p for p in doc.problems() if p.code == "W_UNKNOWN_KEY"]


def test_copy_and_paste_keep_area_fields():
    doc = ModelDocument(_burst_doc())
    names = paste_fragment(doc, copy_selection(doc, ["create_1", "dispose_1"]))
    assert doc.link_area(*names)["capacity"] == 2 and doc.link_delay(*names) == const(10)


def test_a_state_refuses_an_area_link():
    assert state.refused_kind(_burst_doc()) == "an area link"


def test_playback_draws_a_part_in_a_link_at_its_target():
    """Parts in the link (0-10, the third 10-20) are drawn at dispose_1, the link's target;
    replay never meets the hidden process's name."""
    recorder = TraceRecorder()
    api.run_model(_burst_doc(), replications=1, trace=recorder)
    assert not [e for e in recorder.events if "link" in str(getattr(e, "block", ""))
                or "link" in str(getattr(e, "target", "") or "")]
    timeline = Timeline(recorder.events)
    for t in (5.0, 15.0):
        placed = timeline.state_at(t)["entities"]
        assert placed and {block for block, _e_type in placed.values()} == {"dispose_1"}


def test_the_hidden_link_adds_no_row_to_the_existing_frames():
    """The link is no block: buffer_summary and the flow table name only real blocks."""
    results = api.run_model(_burst_doc(), replications=1)
    assert "buffer_summary" not in results
    flow = results["material_flow_summary"]
    assert set(flow["From"]) | set(flow["To"]) <= {"create_1", "dispose_1"}


def _zoned_into_link(zones):
    """b1 (1.8 m at 0.5, spacing 0.41, maybe zoned) -> link (0.82, capacity 1) -> b2 (0.41 m, one
    cell, exit stop Op) -> pick -> dispose; Op is held to 20; parts board b1 at 0, 5, 10."""
    from test_conveyor_divert_spec import hog, pick
    b1 = belt(name="b1", length=1.8, speed=0.5, spacing=0.41)
    if zones:
        b1["params"]["zones"] = zones
    b2 = belt(name="b2", length=0.41, speed=0.5, spacing=0.41, exit_resource="Op")
    blocks = [create(name="c", max_arrivals=3, dist_params={"value": 5.0}, start_time=0),
              b1, b2, pick("Op"), dispose()]
    links = [{"source": "c", "target": "b1"},
             {"source": "b1", "target": "b2", "delay": const(0.82), "capacity": 1},
             {"source": "b2", "target": "pick"}, {"source": "pick", "target": "dispose_1"}]
    hog_blocks, hog_links = hog("Op", 20.0)
    doc = layout(blocks + hog_blocks, links + hog_links, resources=[{"name": "Op", "capacity": 1}],
                 simulation=sim())
    env, _model = run_env(doc, 200)
    return sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "DEFAULT")


def test_a_zoned_belt_keeps_its_zone_delay_before_an_area_link_takes_the_part():
    """R23 at a link. Part 1: b1 end 3.6, link 3.6-4.42, b2 end 5.24, waits Op: exits 20. Part
    2: b1 end 8.6, link 8.6-9.42, waits there for b2 (full) to 20, rides b2 to 20.82, exits
    20.82. Part 3: b1 end 13.6, held (link full) to 20; unzoned it enters at 20, reaches b2 at
    20.82 (part 2 just left it), exits 21.64; with zones (delay 1) it first waits its zone-1
    delay: enters at 21, exits 22.64, as a zoned belt's part handed to a belt waits (R23)."""
    close(_zoned_into_link(None), [20.0, 20.82, 21.64])
    close(_zoned_into_link({"count": 3, "release_delay": 1.0}), [20.0, 20.82, 22.64])


def _divert_cases(divert, target, **area):
    blocks = [create(max_arrivals=1, start_time=0), belt(diverts=[divert]), dispose(), target]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"},
             {"source": "belt", "target": target["name"], **area},
             {"source": target["name"], "target": "dispose_1"}]
    doc = layout(blocks, links, simulation=sim())
    return {p.fields.get("case") for p in validate_model_structured(doc)
            if p.code == "E_CONVEYOR_DIVERT"}


def test_a_pass_divert_over_an_area_link_reads_the_links_places():
    """A "pass" divert reads its target's room; over an area link the target is the link, so
    it needs a capacity (the run reads the link's places), whatever block lies behind it."""
    lane = belt(name="lane", length=2.0)
    passing = divert_at(3, when_full="pass")
    assert _divert_cases(passing, lane) == set()
    assert _divert_cases(passing, lane, delay=const(1), capacity=1) == set()


def test_a_robot_divert_over_an_area_link_is_refused():
    """A robot meets the part at the divert, so its link must go straight to the robot."""
    robot = {"name": "robot", "description": "robot", "type": "MOTION", "position": [0, 0],
             "params": {"segments": [{"to": "grab"}],
                        "targets": {"grab": {"belt": "belt", "divert": 0}}}}
    meets = divert_at(3, intercept=True)
    assert "a7_robot" not in _divert_cases(meets, robot)
    assert "a7_robot" in _divert_cases(meets, robot, delay=const(1), capacity=1)


def test_a_pass_divert_over_a_full_area_link_lets_the_part_ride_on():
    """Divert at 3 passes when full, its link 9 long with one place; parts board at 0 and 1.
    Part 0 is diverted at 3 into the link (leaves at 12); part 1 reaches the divert at 4, the
    link is full, so it rides on to the end: exits 11 and 12."""
    blocks = [create(max_arrivals=2, dist_params={"value": 1.0}, start_time=0),
              belt(diverts=[divert_at(3, when_full="pass")]), dispose(), dispose(name="side")]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"},
             {"source": "belt", "target": "side", "delay": const(9), "capacity": 1}]
    close(exit_times(api.run_model(layout(blocks, links, simulation=sim()), replications=1)),
          [11.0, 12.0])


def _instance_doc(definition):
    doc = layout([create(max_arrivals=1, start_time=0),
                  {"name": "I1", "type": "SUBMODEL", "position": [0, 0],
                   "params": {"definition": "Cell", "arguments": {}}}, dispose()],
                 [{"source": "create_1", "target": "I1"}, {"source": "I1", "target": "dispose_1"}],
                 simulation=sim())
    doc["submodels"] = {"Cell": definition}
    return doc


def test_a_flattened_reversible_belt_and_merge_run():
    """End to end: the renamed reverse_to and merge `from` load and run. One part: feeder
    (delay 0) -> lane (4 m) end at 4 -> out. With a merge at 2 from `back` (fed by nothing),
    the part still leaves at 4."""
    reverse = _cell({"reversible": True, "reverse_to": "back"}, _INNER)
    close(exit_times(api.run_model(_instance_doc(reverse), replications=1)), [4.0])
    links = [*_INNER[:3], {"source": "back", "target": "lane"}]
    merged = _cell({"merges": [{"at": 2.0, "from": "back"}]}, links)
    close(exit_times(api.run_model(_instance_doc(merged), replications=1)), [4.0])


# -- fix round 1 (R52-R54) ------------------------------------------------------------------
def _one_link(**fields):
    return layout([create(max_arrivals=1, start_time=0), dispose()],
                  [{"source": "create_1", "target": "dispose_1", **fields}], simulation=sim())


def _cases_of(doc, code):
    return {p.fields.get("case") for p in validate_model_structured(doc) if p.code == code}


_CONSTANT = {"distribution": "Constant", "dist_params": {"value": 10}}


def test_r52_delay_and_capacity_rules():
    """D1391: delay + capacity is the area link, with no problem; an old transfer_time beside a
    delay is refused (the migration's case); a capacity alone warns that it is not read
    (W_UNKNOWN_KEY, case unread) and the link stays instant."""
    assert _cases_of(_one_link(delay=_CONSTANT, transfer_time=1), "E_LINK_AREA") == {"migrate"}
    both = _one_link(delay=_CONSTANT, capacity=1)
    assert _cases_of(both, "E_LINK_AREA") == set() and _cases_of(both, "W_UNKNOWN_KEY") == set()
    alone = _one_link(capacity=1)
    assert _cases_of(alone, "E_LINK_AREA") == set()
    assert _cases_of(alone, "W_UNKNOWN_KEY") == {"unread"}
    assert _cases_of(_one_link(capacity=0), "W_UNKNOWN_KEY") == set()


def test_a_link_problem_names_the_link_once_in_its_plain_line():
    """Final review minor: the plain line said "Link link create_1->dispose_1 ..."."""
    from nagasim.core.plainlines import plain
    for fields, code in ((dict(delay=_CONSTANT, transfer_time=1), "E_LINK_AREA"),
                         (dict(capacity=1), "W_UNKNOWN_KEY")):
        found = [p for p in validate_model_structured(_one_link(**fields)) if p.code == code]
        assert found and plain(found[0]).startswith("Link create_1->dispose_1"), plain(found[0])


def test_r53_a_capacity_a_port_would_copy_onto_two_links_is_refused():
    """The reviewer's probe: two inner sources d1, d2 -> out; the outer link I1 -> dispose_1 has
    delay 10 and capacity 1. Flattened, that is two links of capacity 1 each (exits 10,
    10 instead of 10, 20), so it is refused; with no capacity (a time only) it flattens."""
    def doc(**area):
        source = {"name": "src", "type": "CREATE", "position": [0, 0],
                  "params": create()["params"] | {"max_arrivals": 1, "start_time": 0}}
        definition = {"parameters": [], "blocks": [
            {**source, "name": "d1"}, {**source, "name": "d2"},
            {"name": "out", "type": "SUB_EXIT", "position": [0, 0], "params": {"port": "out"}}],
            "links": [{"source": "d1", "target": "out"}, {"source": "d2", "target": "out"}]}
        model = layout([{"name": "I1", "type": "SUBMODEL", "position": [0, 0],
                         "params": {"definition": "Cell", "arguments": {}}}, dispose()],
                       [{"source": "I1", "target": "dispose_1", "delay": const(10), **area}],
                       simulation=sim(time_limit=100))
        model["submodels"] = {"Cell": definition}
        return model
    with pytest.raises(Exception, match="capacity 1 would be copied onto 2 links"):
        api.run_model(doc(capacity=1), replications=1)
    assert any(p.code == "E_LINK_AREA" and p.fields["case"] == "port_fan"
               for p in validate_model_structured(doc(capacity=1)))
    close(exit_times(api.run_model(doc(), replications=1)), [10.0, 10.0])


def test_r54_the_hidden_link_is_no_block_of_the_model():
    """model.blocks holds only drawn blocks, so the debugger neither lists the link nor stops
    on it."""
    from nagasim.core.debugger import Stops
    from nagasim.core.session import SimSession
    _env, model = run_env(_burst_doc(), 10)
    assert sorted(model.blocks) == ["create_1", "dispose_1"]
    session = SimSession(_burst_doc(), 0)
    assert sorted(session.model.blocks) == ["create_1", "dispose_1"]
    with pytest.raises(ValueError, match="no block named"):
        Stops(session, [{"block": "create_1 -> dispose_1 link"}])


def test_r54_the_live_deadlock_text_reads_the_loop_once():
    blocks = [create(entities_per_arrival=5, max_arrivals=1, start_time=0),
              belt("A", length=2.0), belt("B", length=1.0),
              create(name="other", max_arrivals=1, start_time=0), dispose()]
    links = [{"source": "create_1", "target": "A"},
             {"source": "A", "target": "B", "delay": const(1), "capacity": 1},
             {"source": "B", "target": "A"}, {"source": "other", "target": "dispose_1"}]
    with pytest.raises(RuntimeError, match=r"loop A -> 'A -> B link' -> B -> A is full"):
        api.run_model(layout(blocks, links, simulation=sim()), replications=1)


def test_r54_the_run_end_deadlock_names_the_link():
    """Two parts -> link (capacity 1, delay 1) -> belt B (1 cell) whose exit stop needs
    Op, which a hog holds for ever. Part 1 takes B's cell and waits at its end; part 2 waits in
    the link for B when the events run out, and the run-end check names the link."""
    from test_conveyor_divert_spec import hog
    blocks = [create(entities_per_arrival=2, max_arrivals=1, start_time=0),
              belt("B", length=1.0, exit_resource="Op"), dispose()]
    links = [{"source": "create_1", "target": "B", "delay": const(1), "capacity": 1},
             {"source": "B", "target": "dispose_1"}]
    hog_blocks, hog_links = hog("Op", 1e9)
    hog_blocks[1]["params"]["action"] = "Seize Delay"
    doc = layout(blocks + hog_blocks, links + hog_links,
                 resources=[{"name": "Op", "capacity": 1}], simulation=sim())
    with pytest.raises(RuntimeError, match=r"deadlock .*create_1 -> B link"):
        api.run_model(doc, replications=1)


def test_r54_hold_until_space_before_an_area_link_is_refused_in_validation():
    from conftest import process
    blocks = [create(max_arrivals=1, start_time=0),
              process(name="P", resource="Server", value=1.0, hold_until_space=True),
              {"name": "Q", "description": "Q", "type": "BUFFER", "position": [0, 0],
               "params": {"capacity": 2}}, dispose()]
    links = [{"source": "create_1", "target": "P"},
             {"source": "P", "target": "Q", "delay": const(1), "capacity": 1},
             {"source": "Q", "target": "dispose_1"}]
    doc = layout(blocks, links, resources=[{"name": "Server", "capacity": 1}], simulation=sim())
    assert _cases_of(doc, "E_HOLD_SPACE_TARGET") == {"area"}
    found = next(p for p in validate_model_structured(doc) if p.code == "E_HOLD_SPACE_TARGET")
    assert "'Q' is an area link" in found.message
