"""The view-state baseline read through the engine alone (PHASE7 7.7, D196).

`tests/golden/view_state.json` was written from the editor's player before the
derivations moved beside `Timeline` (`tests/test_ui_view_state.py` still reads it
that way). Here the same hashes come from `core/playback.py` and
`network.block_place` on the layout as loaded from the file: one baseline, two
readers, no editor.
"""
import pytest

from nagasim.core import playback
from nagasim.core.network import block_place
from tools.make_golden import read_view_state, view_baseline, view_models, view_state_hash

# The models of `tests/test_ui_view_state.py`: asset states and chargers (motion,
# fleets), agent colours, level sources, system dynamics, submodels, network places.
FAST = ("automation_floor.json", "ed_game.json", "line_with_buffers.json", "sd_sir.json",
        "two_cell_line.json", "warehouse_library.json")
STRIPPED = "this copy of the model carries no asset graphics: the hash covers what they nominate"


@pytest.mark.parametrize("model", FAST)
def test_the_engine_gives_the_baseline_view_state(model):
    expected = view_baseline(model)
    if expected is None:
        pytest.skip(STRIPPED)
    assert view_state_hash(model, read_view_state) == expected, model


@pytest.mark.slow
def test_the_engine_gives_the_baseline_view_state_of_every_bundled_model():
    comparable = [model for model in view_models() if view_baseline(model) is not None]
    assert len(comparable) >= 40        # 51 here; 44 where the models have lost their artwork
    drifted = [model for model in comparable
               if view_state_hash(model, read_view_state) != view_baseline(model)]
    assert not drifted, drifted


# -- the fleet and charger branches, which no bundled model's baseline reaches ---------
def test_asset_states_and_unit_counts_on_hand_built_fleet_charger_and_off_states():
    from nagasim.core.playback import asset_states, unit_counts

    def res(state, capacity=1, busy=0):
        return {"state": state, "capacity": capacity, "busy": busy}

    sources = {"m": ("m/Machine", None), "c": ("c/Dock", ("c/Dock",)),
               "f": ("f/Fleet", ("f/Charge",))}
    assert asset_states({"resources": {"m/Machine": res("off")}}, sources) == {
        "m": "off", "c": "idle", "f": "idle"}
    assert asset_states({"resources": {}, "chargers": {"c/Dock": 1, "f/Charge": 2}}, sources) == {
        "c": "busy", "f": "busy"}                       # a resource nobody mentioned: no state
    assert asset_states({"resources": {"f/Fleet": res("down")}, "chargers": {"f/Charge": 1}},
                        sources)["f"] == "down"        # down or off wins over charging
    assert asset_states({"resources": {"f/Fleet": res("off")}}, sources)["f"] == "off"

    counts = {"c/Dock": ("charger", "c", 3), "f/Fleet": ("fleet", "f", 4),
              "r": ("resource", "r", 0)}
    movers = {("f/Fleet", 0): ((), 0.0, "loaded"), ("f/Fleet", 1): ((), 0.0, "idle"),
              ("f/Fleet", 2): ((), 0.0, "loaded"), ("f/Fleet", 3): ((), 0.0, "idle")}
    state = {"chargers": {"c/Dock": 2}, "movers": movers, "charging": {("f/Fleet", 2)},
             "resources": {"r": res("idle", 3, 2)}}
    assert unit_counts(state, counts) == {
        "c": [("occupied", 2), ("free", 1)],
        "f": [("idle", 2), ("working", 1), ("charging", 1)],       # unit 2 charges, not works
        "r": [("busy", 2), ("free", 1)]}
    state["resources"] = {"f/Fleet": res("idle", 3), "r": res("down", 3, 2)}
    out = unit_counts(state, counts)
    assert out["f"] == [("idle", 1), ("working", 1), ("charging", 1), ("out", 1)]   # capacity 3 of 4
    assert out["r"] == [("busy", 2), ("out", 1)]
    state["resources"] = {"f/Fleet": res("off")}
    assert unit_counts(state, counts)["f"] == [("out", 4)]
    assert "f" not in unit_counts({"movers": {}}, counts)           # a fleet no trace mentioned
    assert unit_counts({}, {}) == {}


# -- the level argument: a submodel open in the editor --------------------------------
def _nested() -> dict:
    """A cell holding a machine asset, placed once: `line` at the top, `robot` inside."""
    machine = {"info": {"graphics": {"state_source": "Machine"}, "owns": {"resources": ["Machine"]}},
               "blocks": [], "links": []}
    cell = {"info": {}, "blocks": [
        {"name": "robot", "type": "SUBMODEL", "params": {"definition": "machine"}},
        {"name": "belt", "type": "CONVEYOR", "params": {"from_node": "a", "to_node": "b"}}],
        "links": []}
    return {"blocks": [{"name": "line", "type": "SUBMODEL", "params": {"definition": "cell"}},
                       {"name": "dock", "type": "STATION", "params": {"node": "a"}}],
            "links": [], "submodels": {"machine": machine, "cell": cell},
            "network": {"nodes": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 10, "y": 4},
                                  {"name": "line/in", "x": 5, "y": 5}]}}


def test_level_names_the_definition_whose_blocks_are_viewed():
    """Review focus: the editor's player has a `DefinitionView` while a submodel is
    open, whose blocks are that definition's, not the model's. `level` selects them."""
    layout = _nested()
    assert playback.asset_state_sources(layout) == {}
    assert playback.asset_state_sources(layout, level="cell") == {
        "robot": ("robot/Machine", None)}
    assert playback.node_blocks(layout) == {"a": "dock", "line/in": "line"}
    assert playback.node_blocks(layout, level="cell") == {"a": "belt", "b": "belt"}
    assert playback.asset_state_sources(layout, level="no such definition") == {}


def test_owns_is_asked_per_definition_and_defaults_to_the_layouts_own_record():
    layout = _nested()
    layout["submodels"]["machine"]["info"] = {"graphics": {"state_source": "Dock"}}
    layout["blocks"].append({"name": "bay", "type": "SUBMODEL", "params": {"definition": "machine"}})
    assert playback.asset_state_sources(layout)["bay"] == ("bay/Dock", None)
    asked = []

    def owns(definition):
        asked.append(definition)
        return {"chargers": ["Dock"]}

    assert playback.asset_state_sources(layout, owns)["bay"] == ("bay/Dock", ("bay/Dock",))
    assert asked == ["machine"]


def test_state_colors_keeps_an_earlier_colour_when_a_later_one_is_unreadable():
    """Review focus: a declared colour the parser refuses is skipped, never passed on
    as text and never allowed to erase a readable one of the same name."""
    layout = {"agent_types": [
        {"states": [{"name": "Idle", "color": "BLUE"}, {"name": "Busy", "color": "#A1b2C3"}]},
        {"states": [{"name": "Idle", "color": "url(x)"}, {"name": "Odd", "color": "</script>"}]}],
        "populations": [{"colors": {"north": "red", "south": "javascript:alert(1)"}}]}
    assert playback.state_colors(layout) == {"Idle": "#0000ff", "Busy": "#a1b2c3",
                                             "north": "#ff0000"}
    assert playback.state_colors(layout, lambda color: color or None)["Odd"] == "</script>"


def test_block_place_takes_the_node_then_the_belt_then_the_blocks_own_position():
    nodes = {"a": {"name": "a", "x": 2, "y": 4}, "b": {"name": "b", "x": 6, "y": 8}}
    assert block_place({"node": "a"}, [9, 9], nodes) == (2.0, 4.0)
    assert block_place({"from_node": "a", "to_node": "b"}, None, nodes) == (4.0, 6.0)
    assert block_place({"from_node": "a", "to_node": "gone"}, [1, 2], nodes) == (1, 2)
    assert block_place({}, None, nodes) is None


# -- a scoped variable as the asset's state source (D608) -----------------------------
def _lamp_layout() -> dict:
    """One `lamp` asset whose picture follows its own variable `$on_kw`: switched to
    5 at t=0 and back to 0 at t=10 by the one entity passing through."""
    lamp = {"info": {"graphics": {"state_source": "on_kw"}, "owns": {"variables": ["on_kw"]}},
            "blocks": [
                {"name": "src", "type": "CREATE", "position": [0, 0],
                 "params": {"e_type": "Job", "distribution": "Constant",
                            "dist_params": {"value": 2.0}, "max_arrivals": 1}},
                {"name": "on", "type": "ASSIGN", "position": [100, 0],
                 "params": {"assignments": [{"target": "$on_kw", "expr": "5"}]}},
                {"name": "wait", "type": "DELAY", "position": [200, 0],
                 "params": {"distribution": "Constant", "dist_params": {"value": 10.0}}},
                {"name": "off", "type": "ASSIGN", "position": [300, 0],
                 "params": {"assignments": [{"target": "$on_kw", "expr": "0"}]}},
                {"name": "end", "type": "DISPOSE", "position": [400, 0], "params": {}}],
            "links": [{"source": "src", "target": "on"}, {"source": "on", "target": "wait"},
                      {"source": "wait", "target": "off"}, {"source": "off", "target": "end"}]}
    return {"simulation": {"run_length": 20},
            "variables": [{"name": "on_kw", "initial": 0}],
            "blocks": [{"name": "lamp_1", "type": "SUBMODEL", "position": [0, 0],
                        "params": {"definition": "lamp"}}],
            "links": [], "submodels": {"lamp": lamp}}


def test_a_scoped_variable_drives_the_asset_picture_busy_while_above_zero():
    from conftest import run_env
    from nagasim.core.trace import TraceRecorder

    layout = _lamp_layout()
    sources = playback.asset_state_sources(layout)
    assert sources == {"lamp_1": ("lamp_1/on_kw", None)}
    recorder = TraceRecorder()
    run_env(layout, 20, subscribers=(recorder,))
    changes = [(e.t, e.value) for e in recorder.events if e.kind == "variable_changed"]
    assert changes == [(0.0, 0.0), (0.0, 5.0), (10.0, 0.0)]   # the baseline, then each write
    timeline = playback.Timeline(recorder.events)
    assert [playback.asset_states(timeline.state_at(t), sources)["lamp_1"]
            for t in (0, 9, 10, 19)] == ["busy", "busy", "idle", "idle"]


def test_a_variable_no_asset_shows_is_not_traced():
    from conftest import run_env
    from nagasim.core.trace import TraceRecorder

    layout = _lamp_layout()
    del layout["submodels"]["lamp"]["info"]["graphics"]
    recorder = TraceRecorder()
    run_env(layout, 20, subscribers=(recorder,))
    assert not [e for e in recorder.events if e.kind == "variable_changed"]
