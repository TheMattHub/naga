"""Present-mode definition rules (D242-D246, D251): the `dashboard` section's
Present keys, the generation rule and the engine-side check of a viewer's value."""
import copy
import json
from pathlib import Path

import pytest

import nagasim
from nagasim import dashboard, experiments
from nagasim.core import schema
from nagasim.core.schema import CURRENT_VERSION

from conftest import chain, create, dispose, layout, process

MODELS = sorted((Path(nagasim.__file__).parent / "models").glob("*.json"))


def _bundled(name: str) -> dict:
    return schema.load_layout(Path(nagasim.__file__).parent / "models" / f"{name}.json")


def _model(**dashboard_keys) -> dict:
    c, p, d = create("c"), process("p"), dispose("d")
    doc = layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 2}],
                 variables=[{"name": "open", "initial": True}, {"name": "rate", "initial": 0.5}],
                 simulation={"time_limit": 50, "warmup": 0, "replications": 3})
    if dashboard_keys:
        doc["dashboard"] = {"widgets": [], **dashboard_keys}
    return schema.migrate(doc)


def _codes(doc: dict) -> list[str]:
    return [p.code for p in schema.validate_model_structured(doc) if "DASHBOARD" in p.code]


# -- schema version and migration --------------------------------------------
def test_v29_migration_adds_nothing_to_a_model_without_the_new_keys():
    assert CURRENT_VERSION == 29
    raw = json.loads((Path(nagasim.__file__).parent / "models" / "ed_game.json").read_text("utf-8"))
    raw["schema_version"] = 28
    migrated = schema.migrate(raw)
    # Nothing but the version: the same file already marked v29 reads the same.
    assert migrated == schema.migrate({**raw, "schema_version": 29})
    assert migrated["dashboard"] == raw["dashboard"] and migrated["schema_version"] == 29
    assert "dashboard" not in schema.migrate({"schema_version": 28, "blocks": [], "links": []})


def test_document_saves_a_section_that_holds_only_present_keys():
    from nagasim.core.document import ModelDocument

    inputs = [{"path": "resources.Server.capacity", "kind": "number", "min": 1, "max": 4}]
    assert ModelDocument(_model(inputs=inputs)).to_layout()["dashboard"]["inputs"] == inputs
    assert "dashboard" not in ModelDocument(_model()).to_layout()


# -- the generation rule -------------------------------------------------------
@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_every_bundled_models_default_validates_and_is_deterministic(path):
    doc = schema.load_layout(path)
    before = schema.format_problems(schema.validate_model_structured(doc))
    generated = dashboard.default_dashboard(doc)
    assert generated == dashboard.default_dashboard(copy.deepcopy(doc))
    assert json.dumps(generated) == json.dumps(dashboard.default_dashboard(doc))
    stored = {**doc, "dashboard": generated}
    assert schema.format_problems(schema.validate_model_structured(stored)) == before
    assert dashboard.check_inputs(stored) == []
    assert dashboard.merge_generated(generated, generated) == (generated, [])
    assert dashboard.resolve_dashboard(stored) == generated
    assert len(generated["inputs"]) <= dashboard.MAX_GENERATED_INPUTS


def test_single_server_generates_one_capacity_input_and_the_process_results():
    generated = dashboard.default_dashboard(_bundled("single_server"))
    assert generated["inputs"] == [{"path": "resources.Server.capacity", "label": "Server",
                                    "kind": "number", "min": 1, "max": 3, "step": 1}]
    assert [r["kpi"] for r in generated["results"]] == [
        "system/Avg Throughput Time", "system/Throughput (per time unit)",
        "system/Avg WIP (Little)", "system/Avg Resource Usage %"]
    assert generated["limits"] == {"replications": 5, "max_replications": 30}
    assert [(p["name"], p["activity"]) for p in generated["pages"]] == [
        ("What-if", "what_if"), ("Play", "play")]
    assert generated["pages"][0]["items"] == [
        {"kind": "input", "ref": "resources.Server.capacity"}, {"kind": "table"}]
    assert generated["widgets"] == [] and generated["controls"] == []


def test_a_records_owned_by_an_asset_are_no_inputs_but_its_instances_arguments_are():
    """A placed asset's top-level records are templates: the run reads each
    instance's clone, which takes its values from the instance's arguments."""
    generated = dashboard.default_dashboard(_bundled("line_with_buffers"))
    paths = [e["path"] for e in generated["inputs"]]
    assert "resources.Mfg Machine.capacity" not in paths
    assert paths[:3] == ["blocks.station_1.params.arguments.process_time",
                         "blocks.station_1.params.arguments.machines",
                         "blocks.station_1.params.arguments.cost_rate"]
    assert generated["inputs"][1] == {
        "path": "blocks.station_1.params.arguments.machines", "label": "station_1 machines",
        "kind": "number", "min": 0, "max": 2, "step": 1}
    assert len(paths) == dashboard.MAX_GENERATED_INPUTS
    # An asset placed with no argument of its own has nothing an override can address.
    sir = dashboard.default_dashboard(_bundled("sd_sir"))
    assert sir["inputs"] == [] and sir["results"] == []
    assert [p["name"] for p in sir["pages"]] == ["Play"]
    assert sir["limits"]["replications"] == 3      # the floor: two give one degree of freedom


def test_number_literal_auxiliaries_are_inputs_written_back_as_text():
    doc = _model()
    doc["auxiliaries"] = [{"name": "beta", "expr": "0.4"}, {"name": "n", "expr": "1000"},
                          {"name": "f", "expr": "beta * 2"}]
    inputs = dashboard.default_dashboard(doc)["inputs"]
    assert [(e["path"], e["min"], e["max"], e["step"]) for e in inputs
            if e["path"].startswith("aux")] == [("auxiliaries.beta.expr", 0, 0.8, 0.01),
                                                 ("auxiliaries.n.expr", 0, 2000, 1)]
    assert dashboard.input_overrides(doc, {"auxiliaries.beta.expr": 0.25,
                                           "auxiliaries.n.expr": 1500.0}) == [
        {"path": "auxiliaries.beta.expr", "value": "0.25"},
        {"path": "auxiliaries.n.expr", "value": "1500"}]


def test_ed_game_keeps_its_widgets_and_controls():
    doc = _bundled("ed_game")
    generated = dashboard.default_dashboard(doc)
    assert generated["widgets"] == doc["dashboard"]["widgets"]
    assert generated["controls"] == doc["dashboard"]["controls"]
    # A capacity slider stays a control: lowering the capacity would invalidate it.
    assert [(e["path"], e["label"], e["min"], e["max"]) for e in generated["inputs"]] == [
        ("variables.diverting.initial", "diverting", 0, 10),
        ("variables.diverted_count.initial", "diverted_count", 0, 10)]
    play = generated["pages"][-1]
    assert play["activity"] == "play" and play["items"][:2] == [
        {"kind": "view", "mode": "schematic"}, {"kind": "decisions"}]
    assert {"kind": "control", "ref": "divert"} in play["items"]


def test_a_variable_slider_becomes_an_input_with_its_own_range_and_title():
    doc = _model(controls=[{"name": "r", "kind": "slider", "title": "Rate knob",
                            "target": {"variable": "rate"}, "min": 0.1, "max": 0.9, "step": 0.1},
                           {"name": "s", "kind": "slider", "target": {"capacity": "Server"},
                            "min": 0, "max": 2}])
    inputs = dashboard.default_dashboard(doc)["inputs"]
    assert inputs[0] == {"path": "variables.rate.initial", "label": "Rate knob", "kind": "number",
                         "min": 0.1, "max": 0.9, "step": 0.1}
    assert [e["path"] for e in inputs] == ["variables.rate.initial", "variables.open.initial"]


def test_scenarios_and_live_pages_follow_the_model():
    names = lambda doc: [p["name"] for p in dashboard.default_dashboard(doc)["pages"]]
    assert names(_bundled("call_centre_day")) == ["What-if", "Scenarios", "Play"]
    live = {**_model(), "data_sources": [{"name": "plc", "kind": "opcua", "connection": "c"}]}
    assert names(live) == ["What-if", "Play", "Live"]
    kinds = lambda doc: [i["kind"] for i in dashboard.default_dashboard(doc)["pages"][-1]["items"]]
    assert kinds(live) == ["status", "view"]       # no state to start from: monitored only
    assert kinds({**live, "initial_state": {"entities": []}}) == [
        "status", "view", "forecast", "recommendations"]
    assert names({**_model(), "data_sources": [{"name": "f", "kind": "csv", "path": "f.csv"}]}) \
        == ["What-if", "Play"]                     # a file is no plant
    assert names(schema.migrate({"blocks": [], "links": []})) == ["Play"]


def test_a_live_page_takes_its_four_item_kinds_and_no_other():
    page = lambda *kinds: _model(pages=[{"name": "L", "activity": "live",
                                         "items": [{"kind": k} for k in kinds]}])
    assert _codes(page("status", "view", "forecast", "recommendations")) == []
    for kind in ("events", "input", "table", "decisions"):
        assert _codes(page(kind)) == ["E_DASHBOARD_DEFINITION"], kind


def test_generated_ranges_and_kinds():
    doc = _model()
    doc["variables"] += [{"name": "neg", "initial": -4}, {"name": "big", "initial": 3e12},
                         {"name": "text", "initial": "a+b"}, {"name": "many", "initial": 40000}]
    by_path = {e["path"]: e for e in dashboard.default_dashboard(doc)["inputs"]}
    assert by_path["variables.open.initial"] == {"path": "variables.open.initial",
                                                 "label": "open", "kind": "switch"}
    assert (by_path["variables.rate.initial"]["max"], by_path["variables.rate.initial"]["step"]) \
        == (1.0, 0.02)
    assert (by_path["variables.neg.initial"]["min"], by_path["variables.neg.initial"]["max"]) \
        == (-8, 0)
    assert by_path["variables.many.initial"]["step"] == 1000       # 10 000 steps at most
    assert "variables.big.initial" not in by_path and "variables.text.initial" not in by_path
    assert by_path["resources.Server.capacity"]["max"] == 4


@pytest.mark.parametrize("value, entry", [
    (5e-324, None),                 # a denormal: its range over 50 is 0, no step exists
    (-5e-324, None),
    (1e308, None),                  # past the magnitude ceiling
    (-1e308, None),
    (0.1 + 0.2, (0, 0.6000000000000001, 0.01)),
    (1e-300, (0, 2e-300, 2e-302)),
])
def test_generation_is_total_at_the_extremes(value, entry):
    doc = _model()
    doc["variables"] = [{"name": "v", "initial": value}]
    generated = dashboard.default_dashboard(doc)
    found = [(e["min"], e["max"], e["step"]) for e in generated["inputs"]
             if e["path"] == "variables.v.initial"]
    assert found == ([entry] if entry else [])
    assert dashboard.check_inputs({**doc, "dashboard": generated}) == []


def test_stored_inputs_win_and_pages_are_generated_around_them():
    """Until a designer exists an author writes `inputs` by hand: they replace the
    generated ones, and the pages follow."""
    inputs = [{"path": "variables.rate.initial", "label": "Rate", "kind": "number",
               "min": 0, "max": 1, "step": 0.1}]
    resolved = dashboard.resolve_dashboard(_model(inputs=inputs))
    assert resolved["inputs"] == inputs
    assert resolved["pages"][0]["items"][0] == {"kind": "input", "ref": "variables.rate.initial"}
    stored = _model(inputs=inputs, pages=[{"name": "Only", "activity": "play", "items": []}])
    assert dashboard.resolve_dashboard(stored) == stored["dashboard"]


def test_regenerate_only_adds_and_remembers_what_was_hidden():
    doc = _model()
    generated = dashboard.default_dashboard(doc)
    stored = copy.deepcopy(generated)
    stored["inputs"] = [{**stored["inputs"][0], "label": "Mine", "max": 99}]
    stored["pages"][0]["items"] = stored["pages"][0]["items"][:1] + [{"kind": "table"}]
    stored["hidden"] = ["input:" + generated["inputs"][1]["path"], "page:Play"]
    stored["pages"] = stored["pages"][:1]
    merged, added = dashboard.merge_generated(stored, generated)
    assert merged["inputs"][0]["label"] == "Mine" and merged["inputs"][0]["max"] == 99
    assert added == ["input:" + e["path"] for e in generated["inputs"][2:]]
    assert [p["name"] for p in merged["pages"]] == ["What-if"]
    assert merged["pages"][0]["items"][-1] == {"kind": "input",
                                               "ref": generated["inputs"][-1]["path"]}
    assert stored["inputs"][0]["label"] == "Mine" and len(stored["inputs"]) == 1    # pure


def test_generated_dashboard_ignores_what_the_author_stored_and_input_for_follows_the_rule():
    inputs = [{"path": "variables.rate.initial", "label": "Rate", "kind": "number",
               "min": 0, "max": 1}]
    doc = _model(inputs=inputs, pages=[{"name": "Only", "activity": "play", "items": []}],
                 hidden=["page:Play"])
    assert dashboard.generated_dashboard(doc) == dashboard.default_dashboard(_model())
    assert dashboard.input_for(doc, "resources.Server.capacity") == {
        "path": "resources.Server.capacity", "label": "resources.Server.capacity",
        "kind": "number", "min": 1, "max": 4, "step": 1}
    assert dashboard.input_for(doc, "variables.open.initial")["kind"] == "switch"
    assert dashboard.input_for(doc, "variables.nope.initial") is None
    assert dashboard.input_for(doc, "blocks.p.params.distribution") is None     # text


# -- a viewer's value is checked in the engine (D246) ---------------------------
def test_input_overrides_types_the_value_as_the_leaf():
    doc = _model()
    overrides = dashboard.input_overrides(doc, {"resources.Server.capacity": 3.0,
                                                "variables.open.initial": False})
    assert overrides == [{"path": "resources.Server.capacity", "value": 3},
                         {"path": "variables.open.initial", "value": False}]
    assert type(overrides[0]["value"]) is int
    assert experiments.apply_overrides(doc, overrides)["resources"][0]["capacity"] == 3


@pytest.mark.parametrize("path, value", [
    ("resources.Server.capacity", 5),               # above its range
    ("resources.Server.capacity", 0),               # below it
    ("resources.Server.capacity", 2.5),             # an integer field
    ("resources.Server.capacity", True),            # a switch is not a number
    ("resources.Server.capacity", "2"),             # never text
    ("resources.Server.capacity", float("nan")),
    ("variables.rate.initial", "RAND()"),           # never an expression
    ("variables.open.initial", 1),                  # a number is not a switch
    ("simulation.time_limit", 10),                  # not an exposed input
    ("resources.Server.name", "x"),
])
def test_input_overrides_refuses_a_value_outside_its_input(path, value):
    with pytest.raises(ValueError):
        dashboard.input_overrides(_model(), {path: value})


def test_a_choice_takes_only_a_listed_value():
    doc = _model(inputs=[{"path": "resources.Server.capacity", "kind": "choice",
                          "choices": [{"label": "Two", "value": 2}, {"label": "Six", "value": 6}]}])
    assert dashboard.check_inputs(doc) == []
    assert dashboard.input_overrides(doc, {"resources.Server.capacity": 6}) == [
        {"path": "resources.Server.capacity", "value": 6}]
    for value in (3, "6", True):
        with pytest.raises(ValueError):
            dashboard.input_overrides(doc, {"resources.Server.capacity": value})


# -- hostile definitions are refused by code (D251) ------------------------------
@pytest.mark.parametrize("path", [
    "data_sources.plc.path",                # a connection is not overridable
    "agent_types.T.python",                 # nor is code
    "resources.Server.name",                # nor a name
    "resources.Nope.capacity",
    "blocks.p.type",
    "../../etc/passwd",
])
def test_an_input_on_a_path_the_grammar_refuses_is_a_problem(path):
    doc = _model(inputs=[{"path": path, "kind": "number", "min": 0, "max": 1}])
    doc["data_sources"] = [{"name": "plc", "kind": "csv", "path": "x.csv"}]
    assert [p.code for p in dashboard.check_inputs(doc)] == ["E_DASHBOARD_INPUT"]
    with pytest.raises(ValueError):
        dashboard.input_overrides(doc, {path: 0.5})


@pytest.mark.parametrize("path", ["simulation.seed", "simulation.replications"])
def test_the_seed_and_the_replications_are_never_inputs(path):
    assert _codes(_model(inputs=[{"path": path, "kind": "number", "min": 0, "max": 9}])) \
        == ["E_DASHBOARD_DEFINITION"]


def test_an_input_whose_kind_does_not_fit_its_leaf_is_a_problem():
    for entry in ({"path": "variables.open.initial", "kind": "number", "min": 0, "max": 1},
                  {"path": "variables.rate.initial", "kind": "switch"},
                  {"path": "variables.rate.initial", "kind": "choice",
                   "choices": [{"label": "a", "value": "x"}, {"label": "b", "value": 1}]}):
        assert [p.code for p in dashboard.check_inputs(_model(inputs=[entry]))] \
            == ["E_DASHBOARD_INPUT"], entry


_INPUT = {"path": "variables.rate.initial", "kind": "number", "min": 0, "max": 1}
_PAGE = {"name": "P", "activity": "what_if", "items": []}


@pytest.mark.parametrize("keys", [
    {"inputs": [{**_INPUT, "path": f"variables.v{i}.initial"} for i in range(5000)]},
    {"results": [{"kpi": f"k{i}"} for i in range(33)]},
    {"pages": [{**_PAGE, "name": str(i)} for i in range(9)]},
    {"pages": [{**_PAGE, "items": [{"kind": "table"}] * 41}]},
    {"hidden": ["input:x"] * 201},
    {"hidden": ["x" * 201]},
    {"widgets": [{"name": f"w{i}", "kind": "kpi", "expr": "1"} for i in range(65)]},
    {"controls": [{"name": f"c{i}", "kind": "slider", "target": {"variable": "rate"},
                   "min": 0, "max": 1} for i in range(65)]},
    {"inputs": [{**_INPUT, "min": 0, "max": 1e300}]},                  # absurd magnitude
    {"inputs": [{**_INPUT, "min": 0, "max": 1e9, "step": 1e-9}]},      # absurd step count
    {"inputs": [{**_INPUT, "min": 5, "max": 5}]},
    {"inputs": [{**_INPUT, "max": float("inf")}]},
    {"inputs": [{**_INPUT, "max": "9"}]},
    {"inputs": [{**_INPUT, "step": 0}]},
    {"inputs": [_INPUT, _INPUT]},                                      # duplicate path
    {"inputs": [{**_INPUT, "label": "x" * 81}]},
    {"inputs": [{**_INPUT, "label": "two\nlines"}]},
    {"inputs": [{**_INPUT, "expr": "RAND()"}]},                        # no formula field
    {"inputs": [{**_INPUT, "kind": "text"}]},
    {"inputs": [{"path": "variables.rate.initial", "kind": "choice",
                 "choices": [{"label": str(i), "value": i} for i in range(21)]}]},
    {"inputs": [{"path": "variables.rate.initial", "kind": "choice",
                 "choices": [{"label": "one", "value": [1, 2]}, {"label": "two", "value": 2}]}]},
    {"inputs": "resources.Server.capacity"},
    {"inputs": [["resources.Server.capacity"]]},
    {"results": [{"kpi": "k", "better": "bigger"}]},
    {"results": [{"kpi": "k"}, {"kpi": "k"}]},
    {"limits": {"replications": 1}},
    {"limits": {"replications": 50, "max_replications": 20}},
    {"limits": {"max_replications": 1001}},
    {"limits": {"replications": 2.5}},
    {"limits": [5]},
    {"pages": [{**_PAGE, "activity": "edit"}]},
    {"pages": [{"name": "L", "activity": "live", "items": [{"kind": "events"}]}]},  # D279
    {"pages": [{"name": "P", "activity": "play", "items": [{"kind": "forecast"}]}]},
    {"pages": [{**_PAGE, "items": [{"kind": "input", "ref": "variables.rate.initial"}]}]},
    {"pages": [{**_PAGE, "items": [{"kind": "status"}]}]},            # a live item on a what-if
    {"pages": [{**_PAGE, "items": [{"kind": "view", "mode": "x-ray"}]}]},
    {"pages": [{**_PAGE, "items": [{"kind": "table", "row": 10 ** 9}]}]},
    {"pages": [_PAGE, _PAGE]},
    {"permissions": {"allow_network": True}},                          # no such key
])
def test_a_hostile_definition_is_refused(keys):
    doc = _model(**keys)
    assert "E_DASHBOARD_DEFINITION" in _codes(doc)
    assert schema.fatal_problems(schema.validate_model_structured(doc))
    assert dashboard.check_inputs(doc)
    with pytest.raises(ValueError):
        dashboard.input_overrides(doc, {"variables.rate.initial": 0.5})


def test_markup_in_a_label_stays_data_and_grants_nothing():
    """Text is never interpreted by the engine (the Presenter draws it as plain
    text); a definition cannot carry a permission (the caller's, never the file's)."""
    label = "<b>x</b><img src=x onerror=alert(1)>"
    doc = _model(inputs=[{**_INPUT, "label": label, "unit": "<i>u</i>"}],
                 results=[{"kpi": "system/Avg Throughput Time", "label": label}])
    assert _codes(doc) == [] and dashboard.check_inputs(doc) == []
    assert dashboard.resolve_dashboard(doc)["inputs"][0]["label"] == label
    overrides = dashboard.input_overrides(doc, {"variables.rate.initial": 0.5})
    assert overrides == [{"path": "variables.rate.initial", "value": 0.5}]


def test_a_what_if_is_a_scenario_nobody_saved():
    doc = _model()
    overrides = dashboard.input_overrides(doc, {"resources.Server.capacity": 3})
    yours = {**doc, "scenarios": [{"name": "Yours", "overrides": overrides}]}
    result = experiments.run_scenarios(yours, ["Yours"], replications=2, seed=0)
    assert result.scenarios == ["Baseline", "Yours"]
    assert result.seeds["Yours"] == result.seeds["Baseline"] == [0, 1]


# -- generated charts (rule 3) ---------------------------------------------------
@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_the_generated_charts_validate_on_every_bundled_model(path):
    doc = schema.load_layout(path)
    charts = dashboard.generated_widgets(doc)
    if (doc.get("dashboard") or {}).get("widgets"):
        assert charts == []                 # the model's own charts win
        return
    shown = {**doc, "dashboard": {**(doc.get("dashboard") or {}), "widgets": charts}}
    assert schema.fatal_problems(schema.validate_model_structured(shown)) == []
    assert dashboard.generated_widgets(doc) == charts       # deterministic


def test_the_generated_charts_name_what_runs_not_the_assets_templates():
    charts = {w["name"]: w for w in dashboard.generated_widgets(_bundled("line_with_buffers"))}
    assert [s["label"] for s in charts["Levels"]["series"]] == [
        f"buffer_{i}/Mfg Buffer stock" for i in (1, 2, 3)]
    assert charts["Levels"]["kind"] == "line"
    assert charts["Levels"]["series"][0]["expr"] == 'LEVEL("buffer_1/Mfg Buffer stock")'
    assert [s["label"] for s in charts["Queues"]["series"]] == [
        f"station_{i}/Mfg Machine" for i in (1, 2, 3, 4)]
    assert charts["Queues"]["kind"] == "bar"
    assert len(dashboard.generated_widgets(_bundled("biogas_plant_library"))[0]["series"]) == 4
    assert dashboard.generated_widgets(_bundled("abm_boids_flock")) == []     # nothing to chart


def test_no_generated_charts_when_the_runs_record_no_samples():
    doc = schema.load_layout(MODELS[0].parent / "line_with_buffers.json")
    assert dashboard.generated_widgets(doc)
    draining = {**doc, "simulation": {**doc.get("simulation", {}), "time_limit": 0,
                                      "timeseries_interval": None}}
    assert dashboard.generated_widgets(draining) == []
    sampled = {**draining, "simulation": {**draining["simulation"], "timeseries_interval": 5}}
    assert dashboard.generated_widgets(sampled)
