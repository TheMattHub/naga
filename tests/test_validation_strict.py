"""P01b, one strict validation pass (plan 2026-11-11 tier Next, section 3; D1220-D1222).

The S1 review's corpus of wrong model files is inlined here (never read from the repro
folder): each is the M/M/1 model below with one mistake, and each must give a coded
problem with a JSON pointer, the same from `validate`, the document and the agent tools.
"""
from __future__ import annotations

import copy
import json

import pytest

from nagasim.core.document import ModelDocument
from nagasim.core.schema import validate_model_structured


def _mm1() -> dict:
    """The S1 corpus's base file (review repros `qs/mm1.json`), as the editor wrote it."""
    return {
        "$schema": "https://thematthub.github.io/naga/schema/model-v29.json",
        "blocks": [
            {"name": "arrive", "type": "CREATE",
             "params": {"distribution": "Exponential", "dist_params": {"rate": 1.0}}},
            {"name": "serve", "type": "PROCESS",
             "params": {"action": "Seize Delay Release", "resource_name": "Server",
                        "distribution": "Exponential", "dist_params": {"rate": 1.25}}},
            {"name": "leave", "type": "DISPOSE", "params": {}}],
        "links": [{"source": "arrive", "target": "serve"}, {"source": "serve", "target": "leave"}],
        "resources": [{"name": "Server", "capacity": 1}],
        "simulation": {"time_limit": 10000, "warmup": 1000, "replications": 5},
    }


def _with(change) -> dict:
    model = _mm1()
    change(model)
    return model


def _found(model: dict) -> dict:
    """{path: Problem} from the one validation pass, on the file as loaded."""
    from nagasim.core.schema import load_layout
    return {p.path: p for p in validate_model_structured(load_layout(copy.deepcopy(model)))}


def _typo_simkey(m):
    m["simulation"]["replication"] = 3


def _unknown_keys(m):
    m["notes_typo"] = 1
    m["blocks"][0]["nme"] = "x"
    m["resources"][0]["capacty"] = 3
    m["links"][0]["dealy"] = {"distribution": "Constant"}


def test_1765_unknown_key_at_any_level_is_warned_and_kept(tmp_path):
    """DEVLOG #1765 (review S1-03, D1220): an unknown key below the top level validated OK
    and a block's or link's was dropped on save. Every level the schema closes warns with
    the nearest name, and the document keeps the key through load and save."""
    found = _found(_with(_typo_simkey))
    assert found["/simulation/replication"].code == "W_UNKNOWN_KEY"
    assert "'replications'" in found["/simulation/replication"].hint
    model = _with(_unknown_keys)
    found = _found(model)
    for path, near in (("/notes_typo", None), ("/blocks/0/nme", "name"),
                       ("/resources/0/capacty", "capacity"), ("/links/0/dealy", "delay")):
        assert found[path].code == "W_UNKNOWN_KEY" and found[path].severity == "warning", path
        assert near is None or f"'{near}'" in found[path].hint, path
    assert found["/blocks/0/nme"].block == "arrive"
    # The document reads the same result, each key once, and keeps every key through a save.
    path = tmp_path / "m.json"
    path.write_text(json.dumps(model), encoding="utf-8")
    doc = ModelDocument.load(path)
    unknown = [p.path for p in doc.problems() if p.code == "W_UNKNOWN_KEY"]
    assert sorted(unknown) == sorted(["/notes_typo", "/blocks/0/nme", "/resources/0/capacty",
                                      "/links/0/dealy"])
    doc.save(written_by=False)
    once = path.read_text(encoding="utf-8")
    saved = json.loads(once)
    assert saved["blocks"][0]["nme"] == "x" and saved["resources"][0]["capacty"] == 3
    assert saved["links"][0]["dealy"] == {"distribution": "Constant"} and saved["notes_typo"] == 1
    ModelDocument.load(path).save(written_by=False)
    assert path.read_text(encoding="utf-8") == once         # load, save, load: the same file


def test_1765_write_model_reports_each_unknown_key_once_and_keeps_it(tmp_path):
    """The agent tool reads the shared check: each key once, and a top-level one is kept too."""
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    answer = T.call("write_model", model="m", layout=_with(_unknown_keys))
    paths = [p["path"] for p in answer["problems"] if p["code"] == "W_UNKNOWN_KEY"]
    assert sorted(paths) == sorted(["/notes_typo", "/blocks/0/nme", "/resources/0/capacty",
                                    "/links/0/dealy"])
    assert T.call("save_model", model="m", path="m.json")["ok"]
    saved = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    assert saved["notes_typo"] == 1 and saved["blocks"][0]["nme"] == "x"


def _neg_rate(m):
    m["blocks"][0]["params"]["dist_params"] = {"rate": -1}


def _bad_types(m):
    m["simulation"]["time_limit"] = "10000"
    m["resources"][0]["capacity"] = "one"


def _cond_noelse(m):
    """A DECIDE condition on an attribute no block assigns: validate passes (with a warning)."""
    m["blocks"].insert(2, {"name": "d", "type": "DECIDE",
                           "params": {"mode": "Condition", "conditions": ["entity.size > 3"]}})
    m["links"] = [{"source": "arrive", "target": "serve"}, {"source": "serve", "target": "d"},
                  {"source": "d", "target": "leave"}]


def test_1766_bad_value_type_or_range_is_one_coded_problem(tmp_path):
    """DEVLOG #1766 (review S1-04, S3-03, D1221): a negative, zero or empty distribution
    parameter and a field of the wrong JSON type validated OK and the run died with a bare
    library message. Each is now one coded problem with its JSON pointer, read from the
    distribution registry's bounds and the JSON Schema's types; `naga validate` exits 1."""
    from nagasim.modelschema import main_validate
    found = _found(_with(_neg_rate))
    rate = found["/blocks/0/params/dist_params/rate"]
    assert (rate.code, rate.block) == ("E_DIST_PARAM_VALUE", "arrive") and "> 0" in rate.message
    for value in (0, None):         # S3-03: the Properties field typed 0, or cleared
        found = _found(_with(lambda m: m["blocks"][1]["params"]["dist_params"].update(rate=value)))
        assert found["/blocks/1/params/dist_params/rate"].code == "E_DIST_PARAM_VALUE", value
    triangular = {"distribution": "Triangular", "dist_params": {"low": 2, "mode": 1, "high": 3}}
    found = _found(_with(lambda m: m["blocks"][1]["params"].update(triangular)))
    assert found["/blocks/1/params/dist_params/mode"].code == "E_DIST_PARAM_VALUE"
    found = _found(_with(_bad_types))
    assert found["/resources/0/capacity"].code == "E_FIELD_TYPE"
    assert found["/simulation/time_limit"].code == "E_FIELD_TYPE"
    for name, change in (("neg_rate", _neg_rate), ("bad_types", _bad_types)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(_with(change)), encoding="utf-8")
        assert main_validate([str(path)]) == 1, name


def test_1771_a_block_or_link_field_of_the_wrong_type_is_e_field_type():
    """#1771: with link ports typed to match the files (text or a whole number), the type
    check covers blocks and links too; a numbered port stays valid."""
    def bad(m):
        m["blocks"][1]["layout_rotation"] = "left"
        m["links"][0]["source_port"] = 1
        m["links"][1]["target_port"] = ["in"]
    found = _found(_with(bad))
    rotation = found["/blocks/1/layout_rotation"]
    assert (rotation.code, rotation.block) == ("E_FIELD_TYPE", "serve"), rotation
    assert found["/links/1/target_port"].code == "E_FIELD_TYPE"
    assert "/links/0/source_port" not in found


def test_1766_a_run_time_error_names_its_block_and_carries_a_code():
    """What validate cannot see (an attribute no block assigns) stops the run with the block's
    name and a code, never a bare library message."""
    from nagasim import api
    from nagasim.core.expressions import ExpressionError
    model = _with(_cond_noelse)
    assert not [p for p in _found(model).values() if p.severity == "error"]
    with pytest.raises(ExpressionError) as stopped:     # its own type, named by its block
        api.run_model(model, replications=1, run_length=2000)
    assert (stopped.value.code, stopped.value.block) == ("E_RUN_BLOCK", "d")
    assert str(stopped.value).startswith("d: ") and "size" in str(stopped.value)


def test_scenario_override_path_is_checked_on_write(tmp_path):
    """Review S2-05 (D1222): `upsert_scenario` answered ok on `resources.Server.capacty` and
    `validate_model` too; only `run_scenarios` failed. The path is checked in the one
    validation pass, with its JSON pointer and the nearest name, on every surface."""
    from nagasim import agent_tools as T
    model = _with(lambda m: m.__setitem__("scenarios", [
        {"name": "typo", "overrides": [{"path": "resources.Server.capacty", "value": 2}]}]))
    found = _found(model)["/scenarios/0/overrides/0/path"]
    # A warning: the base model still runs, and a study step keeps a bad scenario apart.
    assert found.code == "W_SCENARIO_PATH" and "'capacity'" in found.hint
    T.use_workspace(T.Workspace(tmp_path))
    T.call("write_model", model="m", layout=_mm1())
    answer = T.call("upsert_scenario", model="m", name="typo",
                    overrides=[{"path": "resources.Server.capacty", "value": 2}])
    assert [p["path"] for p in answer["introduced"]] == ["/scenarios/0/overrides/0/path"]
    checked = T.call("validate_model", model="m")
    assert "/scenarios/0/overrides/0/path" in json.dumps(checked)


def test_override_sets_an_optional_field(tmp_path):
    """D1222: a scenario may set any schema field of its record, even one the base leaves
    out (a resource given a capacity schedule only in the scenario); reading the value of
    an absent field still fails, as a data binding's type check needs."""
    from nagasim.core.schema import ModelValidationError
    from nagasim.experiments import apply_overrides, value_at
    model = _with(lambda m: m.update(
        schedules=[{"name": "night", "kind": "capacity", "points": [[480, 1], [480, 2]]}],
        scenarios=[{"name": "staffed", "overrides": [
            {"path": "resources.Server.capacity_schedule", "value": "night"}]}]))
    assert not [p for p in _found(model).values() if p.severity == "error"]
    changed = apply_overrides(model, model["scenarios"][0]["overrides"])
    assert changed["resources"][0]["capacity_schedule"] == "night"
    with pytest.raises(ModelValidationError, match="capacity_schedule"):
        value_at(model, "resources.Server.capacity_schedule")
    # The format's scenarios detail shows that override, and its example validates.
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    assert "capacity_schedule" in T.call("get_model_format", sections=["scenarios"])["format"]


def test_invalid_model_reports_each_problem_once(tmp_path, capsys, caplog):
    """Review S1-14: `naga run` printed every problem twice (a log line, then the error), one
    wrong model unit came back once more per block parameter that inherits it, and a
    Normal service time warned twice per replication with a new number each time."""
    import logging
    from nagasim import api
    from nagasim.cli import main
    found = [p for p in _found(_with(lambda m: m["simulation"].update(time_unit="mins"))).values()
             if p.code == "E_TIME_UNIT"]
    assert [p.path for p in found] == ["/simulation/time_unit"]
    path = tmp_path / "typo_type.json"
    path.write_text(json.dumps(_with(lambda m: m["blocks"][1].update(type="PROCES"))),
                    encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert main(["run", str(path)]) == 2
    assert capsys.readouterr().err.count("unknown block type: PROCES") == 1
    assert not [r for r in caplog.records if "PROCES" in r.getMessage()]
    caplog.clear()
    normal = _with(lambda m: m["blocks"][1]["params"].update(
        distribution="Normal", dist_params={"mean": 0.5, "stddev": 0.5}))
    with caplog.at_level(logging.WARNING):
        api.run_model(normal, replications=3, run_length=2000)
    said = [r.getMessage() for r in caplog.records]
    assert len([m for m in said if "sampled a negative duration" in m]) == 1, said
    assert len([m for m in said if "can produce negative durations" in m]) == 1, said


# ------------------------------------------------------------------ the gate: S1's corpus
def _set(path: str, value):
    """A change that sets one value of the model by its JSON pointer."""
    def change(m):
        *head, last = path.strip("/").split("/")
        node = m
        for key in head:
            node = node[int(key)] if isinstance(node, list) else node[key]
        node[int(last) if isinstance(node, list) else last] = value
    return change


def _rename_param(m):
    m["blocks"][1]["params"]["resource_nam"] = m["blocks"][1]["params"].pop("resource_name")


def _assign(assignment):
    def change(m):
        m["blocks"].insert(1, {"name": "tag", "type": "ASSIGN", "params": {"assignments": [assignment]}})
        m["links"] = [{"source": "arrive", "target": "tag"}, {"source": "tag", "target": "serve"},
                      {"source": "serve", "target": "leave"}]
    return change


def _bad_cond(m):
    _cond_noelse(m)
    m["blocks"][2]["params"]["conditions"] = ["undefined_var > 3"]


# (name, change, the code and JSON pointer validate must give): the review's `mk.py` and
# `extra.py` files, one mistake each on the M/M/1 model.
CORPUS = [
    ("typo_type", _set("/blocks/1/type", "PROCES"), "E_UNKNOWN_BLOCK_TYPE", "/blocks/1/type"),
    ("typo_param", _rename_param, "E_PARAM_UNKNOWN", "/blocks/1/params/resource_nam"),
    ("typo_dist", _set("/blocks/1/params/distribution", "Exponental"), "E_UNKNOWN_DISTRIBUTION",
     "/blocks/1/params/distribution"),
    ("typo_distparam", _set("/blocks/1/params/dist_params", {"mean": 0.8}), "E_DIST_PARAM_UNKNOWN",
     "/blocks/1/params/dist_params/mean"),
    ("typo_resource", _set("/blocks/1/params/resource_name", "Sever"), "E_RESOURCE_UNKNOWN",
     "/blocks/1/params/resource_name"),
    ("typo_link", _set("/links/1/target", "leav"), "E_LINK_UNKNOWN_BLOCK", "/links/1/target"),
    ("no_exit", lambda m: (m["blocks"].pop(), m["links"].pop()), "E_NO_DISPOSE", "/blocks"),
    ("unlinked", lambda m: m["links"].pop(), "E_TERMINAL_NOT_DISPOSE", "/blocks/1"),
    ("bad_unit", _set("/simulation/time_unit", "mins"), "E_TIME_UNIT", "/simulation/time_unit"),
    ("typo_section", lambda m: m.__setitem__("resourcs", m.pop("resources")), "W_UNKNOWN_KEY",
     "/resourcs"),
    ("typo_simkey", _typo_simkey, "W_UNKNOWN_KEY", "/simulation/replication"),
    ("neg_rate", _neg_rate, "E_DIST_PARAM_VALUE", "/blocks/0/params/dist_params/rate"),
    ("bad_expr", _assign({"name": "x", "value": "1 +* 2"}), "E_ASSIGN_KEY",
     "/blocks/1/params/assignments/0/name"),
    ("bad_expr2", _assign({"target": "entity.x", "expr": "1 +* 2"}), "E_EXPRESSION",
     "/blocks/1/params/assignments/0/expr"),
    ("bad_cond", _bad_cond, "E_EXPRESSION", "/blocks/2/params/conditions/0"),
    ("cond_noelse", _cond_noelse, "W_DECIDE_NO_ELSE", "/blocks/2/params/conditions"),
    ("bad_types", _bad_types, "E_FIELD_TYPE", "/resources/0/capacity"),
    ("warmup_gt", _set("/simulation/warmup", 20000), "E_WARMUP_PAST_END", "/simulation/warmup"),
    ("unknown_keys", _unknown_keys, "W_UNKNOWN_KEY", "/resources/0/capacty"),
]
# What the corpus holds that is right: the model itself and an overloaded queue.
VALID = [("mm1", lambda m: None), ("overload", _set("/blocks/1/params/dist_params", {"rate": 0.5}))]


@pytest.mark.parametrize("name, change, code, pointer", CORPUS, ids=[c[0] for c in CORPUS])
def test_each_wrong_file_is_a_coded_problem_with_a_json_pointer(name, change, code, pointer):
    found = _found(_with(change))
    assert pointer in found and found[pointer].code == code, sorted(
        (p.code, p.path) for p in found.values())


def test_a_file_that_is_not_json_is_a_coded_problem(tmp_path, capsys):
    from nagasim.modelschema import main_validate
    path = tmp_path / "broken.json"
    path.write_text('{"blocks": [ {"name": "a", "type": "CREATE",} ]}', encoding="utf-8")
    assert main_validate([str(path)]) == 1
    assert "ERROR E_MODEL_JSON /: not valid JSON" in capsys.readouterr().out


def _runs_or_names_a_block(layout, run_length=None):
    """The property: a model validate passes either runs, or stops with a code naming a block."""
    from nagasim import api
    try:
        api.run_model(layout, replications=1, run_length=run_length)
    except Exception as exc:        # noqa: BLE001 -- the property is about what any error carries
        assert getattr(exc, "code", None) == "E_RUN_BLOCK" and getattr(exc, "block", None), \
            f"{type(exc).__name__}: {exc}"


@pytest.mark.parametrize("name, change", [(c[0], c[1]) for c in CORPUS] + VALID,
                         ids=[c[0] for c in CORPUS] + [v[0] for v in VALID])
def test_what_validate_passes_runs_or_fails_naming_a_block(name, change):
    model = _with(change)
    if any(p.severity == "error" for p in _found(model).values()):
        return
    _runs_or_names_a_block(load_layout_copy(model), run_length=2000)


def load_layout_copy(model: dict) -> dict:
    from nagasim.core.schema import load_layout
    return load_layout(copy.deepcopy(model))


@pytest.mark.slow
def test_what_validate_passes_among_the_bundled_models_runs_or_fails_naming_a_block():
    from nagasim import examples
    from nagasim.core.schema import load_layout
    for name in examples.names():
        layout = load_layout(examples.path(name))
        if any(p.severity == "error" for p in validate_model_structured(layout)):
            continue
        simulation = layout.get("simulation") or {}
        warmup = simulation.get("warmup") or 0
        _runs_or_names_a_block(layout, run_length=simulation.get("time_limit") or warmup + 500)


def test_naga_run_names_the_block_of_a_division_by_zero(tmp_path, capsys):
    """S3-03's zero rate written as an expression ("0") is judged when it is evaluated: the run
    stops on one line naming the block, not with a traceback."""
    from nagasim.cli import main
    path = tmp_path / "zero.json"
    path.write_text(json.dumps(_with(_set("/blocks/1/params/dist_params", {"rate": "0"}))),
                    encoding="utf-8")
    assert main(["run", str(path)]) == 2
    assert capsys.readouterr().err.strip() == "naga run: E_RUN_BLOCK serve: float division by zero"


def test_the_shape_check_builds_the_schema_once_and_sees_a_late_block_type(monkeypatch):
    """Review (tier Next): the shape check built the whole JSON Schema on every model it checked
    (once per replication); it is built once per registry state, so a block type registered later
    (a plugin) is still seen, and gone again once removed."""
    import dataclasses
    from nagasim import modelschema
    from nagasim.core.block_registry import BLOCK_REGISTRY
    from nagasim.core.schema.shape import fields_at
    builds = []
    real = modelschema.build_json_schema
    monkeypatch.setattr(modelschema, "build_json_schema", lambda: builds.append(1) or real())
    late = {"type": "ZZ_LATE", "name": "late"}
    assert fields_at("blocks", late, []) == []
    doc = ModelDocument(_mm1())
    doc.problems(), doc.problems()
    assert len(builds) <= 1, builds
    monkeypatch.setitem(BLOCK_REGISTRY, "ZZ_LATE", dataclasses.replace(BLOCK_REGISTRY["DELAY"], type_name="ZZ_LATE"))
    assert "params" in fields_at("blocks", late, [])
    monkeypatch.delitem(BLOCK_REGISTRY, "ZZ_LATE")
    assert fields_at("blocks", late, []) == []
