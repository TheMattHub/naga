"""Port rules beyond the spec tests (conveyor system A3): `side` validation and round trip,
exit order edge cases, `connect_ports`, and the snap tool's refusals."""
import pytest

from nagasim import agent_tools as T
from nagasim.core import conveyor_path as cp
from nagasim.core.document import ModelDocument
from nagasim.core.floor import connect_ports

from conftest import create, delay, dispose, layout, problem_codes
from nagasim import api


def links(*ports):
    return [{"source": "b", "target": f"t{i}", **({"source_port": p} if p else {})}
            for i, p in enumerate(ports)]


@pytest.mark.parametrize("port, slot", [("out", 0), ("d0", 1), ("d1", 2), ("d2", None),
                                        ("d00", None), ("d01", None), ("d", None), ("d-1", None),
                                        ("d\u0661", None), ("d\uff11", None), ("D0", None),
                                        ("in", None), (1, None), (None, None)])
def test_exit_slot_reads_ascii_port_names_only(port, slot):
    assert cp.exit_slot(port, 2) == slot


def test_exit_order_places_named_links_and_fills_the_rest_in_order():
    assert cp.exit_order(links("d1", "out", "d0"), 2) == [1, 2, 0]
    assert cp.exit_order(links("d0", None, None), 2) == [1, 0, 2]      # portless take slots 0 and 2
    assert cp.exit_order(links(None, None, None), 2) == [0, 1, 2]


@pytest.mark.parametrize("ports", [("d0", "d0", None), ("d1", None), ("d9", "out", None),
                                   ("d0", "foo", "out")])
def test_exit_order_keeps_the_listed_order_when_ports_do_not_fit(ports):
    got = cp.exit_order(links(*ports), 2)
    assert sorted(got) == list(range(len(ports)))
    if ports in (("d0", "d0", None), ("d1", None)):
        assert got == list(range(len(ports)))


def test_belts_without_ports_come_back_untouched():
    layout = {"blocks": [], "links": links(None, None)}
    assert cp.belt_exit_links(layout) is layout["links"]


def test_old_belt_params_have_no_side_and_a_set_side_round_trips(tmp_path):
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}])
    plain = [{"at": 1.0, "condition": "True"}]
    T.call("update_block", model="m", name="A", params={"diverts": plain})
    got = T.call("get_model_json", model="m")["layout"]["blocks"][0]["params"]["diverts"]
    assert all("side" not in d for d in got)
    T.call("update_block", model="m", name="A",
           params={"diverts": [{**plain[0], "side": "right"}]})
    got = T.call("get_model_json", model="m")["layout"]["blocks"][0]["params"]["diverts"]
    assert got[0]["side"] == "right"


def test_bad_side_is_a_conveyor_divert_problem():
    block = {"name": "b", "description": "b", "type": "CONVEYOR", "position": [0, 0],
             "params": {"length": 5.0, "speed": 1.0, "diverts": [
                 {"at": 2.0, "condition": "True", "side": "up"}]}}
    assert "E_CONVEYOR_DIVERT" in problem_codes(layout([block], []))


def test_connect_ports_adds_a_port_link_and_refuses_wrong_roles():
    doc = ModelDocument()
    doc.add_block("CONVEYOR", (0, 0), "A")
    doc.add_block("CONVEYOR", (1, 0), "B")
    pa = {p.name: p for p in cp.ports("A", cp.Pose(0, 0, 0), cp.parse_path([{"straight": 1.0}]), [])}
    pb = {p.name: p for p in cp.ports("B", cp.Pose(1, 0, 0), cp.parse_path([{"straight": 1.0}]), [])}
    with pytest.raises(ValueError):
        connect_ports(doc, pa["in"], pb["in"])
    connect_ports(doc, pa["out"], pb["in"])
    assert doc.link_ports("A", "B") == {"source_port": "out", "target_port": "in"}


def test_place_piece_refuses_bad_snap_and_type(tmp_path):
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}])
    base = dict(model="m", name="B", segments=[{"straight": 1.0}], x=0.0, y=0.0, rotation=0.0)
    # `type` is the conveyor type (R17): a block type or an unknown name is refused
    for bad in ("DELAY", "CONVEYOR", "conveyor_belt"):
        assert T.call("place_piece", type=bad, **base)["error"]["code"] == "E_PLACE_TYPE"
    assert T.call("place_piece", snap_to="A.in", **base)["error"]["code"] == "E_UNKNOWN_PORT"
    assert T.call("place_piece", snap_to="Z.out", **base)["error"]["code"] == "E_UNKNOWN_BLOCK"
    assert T.call("set_layout", model="m", block="Z", x=0, y=0, rotation=0)["error"]["code"] == "E_UNKNOWN_BLOCK"


# -- port links survive flattening (review C1) ----------------------------------

def _entry(name, kind, port):
    return {"name": name, "description": name, "type": kind, "position": [0, 0],
            "params": {"port": port}}


def _definition(blocks, links):
    return {"info": {}, "parameters": [], "blocks": blocks, "links": links, "annotations": []}


@pytest.mark.parametrize("c0, c1, expected", [("False", "True", 106.0), ("True", "True", 1003.0),
                                              ("False", "False", 20.0)])
def test_port_links_route_the_same_with_an_unused_submodel_definition(c0, c1, expected):
    from test_conveyor_ports import PORT_LINKS, belt
    blocks = [create(max_arrivals=1, start_time=0), belt([
        {"at": 3.0, "condition": c0}, {"at": 6.0, "condition": c1}]),
        delay("to_end", 10.0), delay("to_d0", 1000.0), delay("to_d1", 100.0),
        dispose("x_end"), dispose("x_d0"), dispose("x_d1")]
    plain = [{"source": "create_1", "target": "belt"}, *PORT_LINKS,
             {"source": "to_end", "target": "x_end"}, {"source": "to_d0", "target": "x_d0"},
             {"source": "to_d1", "target": "x_d1"}]
    unused = {"Idle": _definition([_entry("i", "SUB_ENTRY", "in"), _entry("o", "SUB_EXIT", "out")],
                                  [{"source": "i", "target": "o"}])}
    doc = layout(blocks, plain, submodels=unused)
    log = api.run_model(doc, replications=1)["entity_log"]
    assert float(log["Exited At"].iloc[0]) == pytest.approx(expected)


@pytest.mark.parametrize("c0, c1, expected", [("False", "True", 106.0), ("True", "True", 1003.0),
                                              ("False", "False", 20.0)])
def test_a_belt_inside_a_definition_keeps_its_port_links(c0, c1, expected):
    from test_conveyor_ports import belt
    inner = _definition(
        [_entry("in", "SUB_ENTRY", "in"), belt([{"at": 3.0, "condition": c0},
                                                 {"at": 6.0, "condition": c1}]),
         delay("to_end", 10.0), delay("to_d0", 1000.0), delay("to_d1", 100.0),
         _entry("out", "SUB_EXIT", "out")],
        [{"source": "in", "target": "belt"},
         {"source": "belt", "target": "to_d1", "source_port": "d1"},
         {"source": "belt", "target": "to_end", "source_port": "out"},
         {"source": "belt", "target": "to_d0", "source_port": "d0"},
         {"source": "to_end", "target": "out"}, {"source": "to_d0", "target": "out"},
         {"source": "to_d1", "target": "out"}])
    cell = {"name": "cell", "description": "cell", "type": "SUBMODEL", "position": [0, 0],
            "params": {"definition": "Cell", "arguments": {}}}
    doc = layout([create(max_arrivals=1, start_time=0), cell, dispose("x")],
                 [{"source": "create_1", "target": "cell"}, {"source": "cell", "target": "x"}],
                 submodels={"Cell": inner})
    log = api.run_model(doc, replications=1)["entity_log"]
    assert float(log["Exited At"].iloc[0]) == pytest.approx(expected)


# -- unknown exits (review I1) --------------------------------------------------

@pytest.mark.parametrize("port", ["d9", "foo", "D0", "in", 3])
def test_an_unknown_exit_is_a_problem_and_keeps_the_listed_order(port):
    from test_conveyor_ports import belt
    layout_ = layout([create(), belt([{"at": 2.0, "condition": "True"}]), dispose("x"),
                      dispose("y")],
                     [{"source": "create_1", "target": "belt"},
                      {"source": "belt", "target": "x", "source_port": port},
                      {"source": "belt", "target": "y", "source_port": "out"}])
    assert "E_CONVEYOR_DIVERT" in problem_codes(layout_)
    assert cp.exit_order(layout_["links"][1:], 1) == [0, 1]


def test_known_exits_are_not_a_problem():
    from test_conveyor_ports import belt
    layout_ = layout([create(), belt([{"at": 2.0, "condition": "True"}]), dispose("x"),
                      dispose("y")],
                     [{"source": "create_1", "target": "belt"},
                      {"source": "belt", "target": "x", "source_port": "d0"},
                      {"source": "belt", "target": "y", "source_port": "out"}])
    assert "E_CONVEYOR_DIVERT" not in problem_codes(layout_)


def test_an_unknown_exit_inside_a_definition_is_a_problem():
    from test_conveyor_ports import belt
    inner = _definition(
        [_entry("in", "SUB_ENTRY", "in"), belt([{"at": 3.0, "condition": "True"}]),
         delay("to_end", 10.0), _entry("out", "SUB_EXIT", "out")],
        [{"source": "in", "target": "belt"},
         {"source": "belt", "target": "to_end", "source_port": "d7"},
         {"source": "to_end", "target": "out"}])
    cell = {"name": "cell", "description": "cell", "type": "SUBMODEL", "position": [0, 0],
            "params": {"definition": "Cell", "arguments": {}}}
    doc = layout([create(max_arrivals=1), cell, dispose("x")],
                 [{"source": "create_1", "target": "cell"}, {"source": "cell", "target": "x"}],
                 submodels={"Cell": inner})
    assert "E_CONVEYOR_DIVERT" in problem_codes(doc)


# -- a repeated exit (checkpoint T3 I2) ----------------------------------------------------

def _twice_layout():
    from test_conveyor_ports import belt
    return layout([create(), belt([{"at": 3.0, "condition": "True"}, {"at": 6.0, "condition": "True"}]),
                   dispose("x"), dispose("y"), dispose("z")],
                  [{"source": "create_1", "target": "belt"},
                   {"source": "belt", "target": "x", "source_port": "d1"},
                   {"source": "belt", "target": "y", "source_port": "out"},
                   {"source": "belt", "target": "z", "source_port": "d1"}])


def test_an_exit_named_twice_is_a_problem_with_a_plain_line():
    from nagasim.core import plainlines
    from nagasim.core.schema import validate_model_structured
    found = [p for p in validate_model_structured(_twice_layout()) if p.code == "E_CONVEYOR_DIVERT"]
    assert [p.fields["case"] for p in found] == ["a3_port_twice"]
    assert found[0].path == "/links/3/source_port" and found[0].fields["first"] == 1
    assert plainlines.plain(found[0]) == "Belt belt: exit d1 has two links"


def test_one_pass_grouping_keeps_each_belts_exit_order():
    """Two divert belts with interleaved port links: each belt's links come back in exit order
    in its own positions; other links stay put."""
    def belt_block(name):
        return {"name": name, "type": "CONVEYOR", "params": {"diverts": [{"at": 1.0}]}}
    lay = {"blocks": [belt_block("a"), belt_block("b")],
           "links": [{"source": "a", "target": "a0", "source_port": "d0"},
                     {"source": "b", "target": "b0", "source_port": "d0"},
                     {"source": "x", "target": "y"},
                     {"source": "a", "target": "a1", "source_port": "out"},
                     {"source": "b", "target": "b1"}]}
    assert [k["target"] for k in cp.belt_exit_links(lay)] == ["a1", "b1", "y", "a0", "b0"]
    assert cp.belt_exit_targets(lay, {"a": {}, "b": {}}) == {"a": ["a1", "a0"], "b": ["b1", "b0"]}


# -- place_piece with a conveyor type (R17, checkpoint T3 I4) -------------------------------

def test_a_typed_piece_runs_at_its_datasheet_speed(tmp_path):
    """type "pallet" writes the datasheet 0.22 m/s into `speed` with `speed_unit` "m/s" (final
    review I1: in its own unit, so a later time-unit change keeps it). A part on a 2.2 m pallet
    piece in a minutes model then rides 2.2 / 0.22 = 10 s = 1/6 min; asserted on the run."""
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    got = T.call("place_piece", model="m", name="P", segments=[{"straight": 2.2}], type="Pallet")
    assert "error" not in got, got
    lay = T.call("get_model_json", model="m")["layout"]
    params = next(b for b in lay["blocks"] if b["name"] == "P")["params"]
    assert params["type"] == "pallet" and (params["speed"], params["speed_unit"]) == (0.22, "m/s")
    params = {**params, "type": ""}            # untyped: no cell rules, only the speed counts
    blocks = [create(max_arrivals=1, start_time=0),
              {"name": "P", "description": "P", "type": "CONVEYOR", "position": [1, 0], "params": params},
              dispose("x")]
    doc = layout(blocks, [{"source": "create_1", "target": "P"}, {"source": "P", "target": "x"}],
                 simulation={"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes"})
    exit_at = float(api.run_model(doc, replications=1)["entity_log"]["Exited At"].iloc[0])
    assert exit_at == pytest.approx(10.0 / 60.0)


def test_a_typed_piece_in_millimetres_and_seconds_keeps_the_datasheet_speed(tmp_path):
    """A model in millimetres and seconds: a 3000 mm "belt" piece (0.33 m/s, written as such)
    placed by place_piece and linked create -> P -> dispose: a part rides 3 / 0.33 s."""
    T.use_workspace(T.Workspace(tmp_path))
    start = layout([create(max_arrivals=1, start_time=0), dispose("x")], [],
                   simulation={"time_limit": None, "warmup": 0, "replications": 1,
                               "time_unit": "seconds", "length_unit": "millimeters"})
    assert "error" not in T.call("write_model", layout=start, name="m")
    assert "error" not in T.call("place_piece", model="m", name="P", segments=[{"straight": 3000.0}],
                                 type="belt")
    T.call("connect", model="m", source="create_1", target="P")
    T.call("connect", model="m", source="P", target="x")
    lay = T.call("get_model_json", model="m")["layout"]
    params = next(b for b in lay["blocks"] if b["name"] == "P")["params"]
    assert (params["speed"], params["speed_unit"]) == (0.33, "m/s")
    exit_at = float(api.run_model(lay, replications=1)["entity_log"]["Exited At"].iloc[0])
    assert exit_at == pytest.approx(3.0 / 0.33)
