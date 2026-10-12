"""Model assertions, `naga check`, CI template (PHASE6 6D W7 Q3, ARCHITECTURE D126;
three states and the engine move: Matteo's 2026-09-26 review; F4 folded the
former tests/test_realitycheck.py in here alongside nagasim/realitycheck.py).

The independent oracle, tests/test_modelcheck_oracle.py, covers the plan's own
hand-derived cases (M/D/1 utilisation, deterministic throughput, --junit,
ci-template). This file covers the mechanism the oracle does not touch:
schema validation, the unknown-KPI hint, mixing assertions with reality
checks in one pass, the replication-batching rule, and (since the review)
the three-state pass/fail/inconclusive split, `--strict` and the
`assert_model` warning; and (since F4) `run_reality_checks` itself."""
import json
from pathlib import Path

import pytest

from conftest import chain, create, dispose, layout, process

from nagasim import agent_tools as T
from nagasim import api, modelcheck
from nagasim.core import schema
from nagasim.core.schema import validate_model_structured

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def _resource(name, capacity=1):
    return {"name": name, "capacity": capacity, "discipline": "FIFO",
            "cost_busy": 0.0, "cost_idle": 0.0, "cost_per_use": 0.0, "cost_time_unit": "hours"}


def _md1_layout(assertions):
    """Exponential(rate=0.5) arrivals, Constant(1.0) service, capacity-1
    Server: rho = 0.5 exactly (the derivation is in
    tests/test_modelcheck_oracle.py's own docstring), but with genuine sampling
    variance across 5 replications -- unlike `_deterministic_layout` below,
    its CI has a real width to land inside or straddle. Pinned once with
    seed=0 (`modelcheck.check`'s own fixed seed): mean ~49.27,
    CI = [48.23, 50.32]."""
    c = create(distribution="Exponential", dist_params={"rate": 0.5}, max_arrivals=0, start_time=0)
    p = process(resource="Server", value=1.0, distribution="Constant")
    d = dispose()
    doc = layout([c, p, d], chain(c, p, d), resources=[_resource("Server")],
                simulation={"time_limit": 2000, "warmup": 0, "replications": 5})
    doc["assertions"] = assertions
    return doc


def _md1_assertion(name, op, value, tolerance=0):
    return {"name": name, "kpi": "resource/Server/Usage % (snapshot)", "op": op, "value": value,
            "replications": 5, "confidence": 0.95, "tolerance": tolerance}


def _deterministic_layout(assertions=None, reality_checks=None):
    """Create(Constant 1.0, max_arrivals=5) -> Delay(1.0) -> Dispose: drains at
    t=5, throughput (per time unit) = 5/5 = 1.0 exactly, same fixture shape
    as the oracle's own (kept independent -- no shared helper)."""
    c = create(distribution="Constant", dist_params={"value": 1.0}, max_arrivals=5, start_time=0)
    delay_blk = {"name": "delay_1", "description": "delay_1", "type": "DELAY", "position": [1, 0],
                "params": {"distribution": "Constant", "dist_params": {"value": 1.0}}}
    d = dispose()
    doc = layout([c, delay_blk, d], chain(c, delay_blk, d),
                simulation={"time_limit": 10, "warmup": 0, "replications": 1})
    if assertions is not None:
        doc["assertions"] = assertions
    if reality_checks is not None:
        doc["reality_checks"] = reality_checks
    return doc


def _throughput_assertion(name, op, value, tolerance=0, replications=1):
    return {"name": name, "kpi": "system/Throughput (per time unit)", "op": op, "value": value,
            "replications": replications, "confidence": 0.95, "tolerance": tolerance}


# --------------------------------------------------------------- check()

def test_no_sections_is_an_empty_list():
    doc = _deterministic_layout()
    assert "assertions" not in doc
    assert modelcheck.check(doc) == []


def test_unknown_kpi_fails_with_close_match_hint():
    doc = _deterministic_layout([_throughput_assertion("typo", "==", 1.0)])
    doc["assertions"][0]["kpi"] = "system/Throughput (per time unit"  # missing ")"
    [result] = modelcheck.check(doc)
    assert result.passed is False
    assert result.status == "fail"  # a hard error, never "inconclusive"
    assert result.kind == "assertion"
    assert "close keys" in result.message
    assert "system/Throughput (per time unit)" in result.message


def test_reality_check_runs_alongside_assertions():
    doc = _deterministic_layout(
        assertions=[_throughput_assertion("throughput_ok", "==", 1.0)],
        reality_checks=[{"name": "always_true", "inputs": {}, "constraint": "TNOW >= 0"}])
    results = {r.name: r for r in modelcheck.check(doc)}
    assert set(results) == {"throughput_ok", "always_true"}
    assert results["throughput_ok"].kind == "assertion"
    assert results["throughput_ok"].passed is True
    assert results["always_true"].kind == "reality_check"
    assert results["always_true"].passed is True
    # A reality check has no CI: its numeric fields are all None.
    assert results["always_true"].kpi is None
    assert results["always_true"].mean is None


def test_assertions_sharing_replications_run_in_one_batch(monkeypatch):
    doc = _deterministic_layout([
        _throughput_assertion("a", "==", 1.0, replications=1),
        _throughput_assertion("b", "<=", 2.0, replications=1),
        _throughput_assertion("c", ">=", 0.5, replications=3),
    ])
    calls = []
    from nagasim import api as api_mod
    real_run_model = api_mod.run_model

    def counting_run_model(*args, **kwargs):
        calls.append(kwargs.get("replications"))
        return real_run_model(*args, **kwargs)

    monkeypatch.setattr(modelcheck.api, "run_model", counting_run_model)
    results = {r.name: r.passed for r in modelcheck.check(doc)}
    assert results == {"a": True, "b": True, "c": True}
    # Two distinct replication counts (1 and 3) across three assertions -> two
    # runs total, not three: a and b share the replications=1 batch.
    assert sorted(calls) == [1, 3]


def test_point_interval_for_a_single_deterministic_replication():
    doc = _deterministic_layout([_throughput_assertion("exact", "==", 1.0, tolerance=0)])
    [result] = modelcheck.check(doc)
    assert result.passed is True
    assert result.mean == pytest.approx(1.0)
    assert result.lower == result.upper == pytest.approx(1.0)
    assert result.half_width == 0.0


# --------------------------------------------------------------- three states

def test_straddling_ci_is_inconclusive():
    """value=49.5 lands strictly inside the pinned [48.23, 50.32] CI (see
    `_md1_layout`'s docstring): neither the whole interval clears 49.5 (for
    "<=") nor lies entirely past it, so the result is "inconclusive", not a
    boolean guess."""
    doc = _md1_layout([_md1_assertion("straddles", "<=", 49.5)])
    [result] = modelcheck.check(doc)
    assert result.status == "inconclusive"
    assert result.passed is False  # compatibility: only "pass" is truthy
    assert "inconclusive" in result.message


def test_clear_pass_and_fail_stay_two_state():
    doc = _md1_layout([
        _md1_assertion("clearly_under", "<=", 70),
        _md1_assertion("clearly_over", ">=", 70),
    ])
    by_name = {r.name: r.status for r in modelcheck.check(doc)}
    assert by_name == {"clearly_under": "pass", "clearly_over": "fail"}


def test_reality_check_status_is_never_inconclusive():
    doc = _deterministic_layout(
        reality_checks=[{"name": "always_true", "inputs": {}, "constraint": "TNOW >= 0"}])
    [result] = modelcheck.check(doc)
    assert result.status == "pass"


# --------------------------------------------------------------- assert_model()

def test_assert_model_is_silent_when_everything_passes(tmp_path):
    doc = _deterministic_layout([_throughput_assertion("ok", "==", 1.0)])
    path = tmp_path / "model.json"
    path.write_text(json.dumps(doc))
    modelcheck.assert_model(str(path))  # must not raise


def test_assert_model_warns_on_inconclusive_by_default(tmp_path):
    doc = _md1_layout([_md1_assertion("straddles", "<=", 49.5)])
    path = tmp_path / "model.json"
    path.write_text(json.dumps(doc))
    with pytest.warns(UserWarning, match="inconclusive"):
        modelcheck.assert_model(str(path))  # not strict: warns, does not raise


def test_assert_model_strict_raises_on_inconclusive(tmp_path):
    doc = _md1_layout([_md1_assertion("straddles", "<=", 49.5)])
    path = tmp_path / "model.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(AssertionError, match="straddles"):
        modelcheck.assert_model(str(path), strict=True)


# --------------------------------------------------------------- schema validation

def _bad_assertions_layout(assertions):
    c, d = create(max_arrivals=1), dispose()
    doc = layout([c, d], chain(c, d))
    doc["assertions"] = assertions
    return doc


def test_validation_flags_unknown_op():
    doc = _bad_assertions_layout([{"name": "a", "kpi": "system/x", "op": "!=", "value": 1}])
    codes = {p.code for p in validate_model_structured(doc)}
    assert "E_ASSERTION_OP" in codes


def test_validation_flags_missing_name_and_kpi():
    doc = _bad_assertions_layout([{"name": "", "kpi": "", "op": "==", "value": 1}])
    codes = {p.code for p in validate_model_structured(doc)}
    assert "E_ASSERTION_NAME" in codes
    # kpi is only checked once name is non-empty (mirrors _validate_reality_checks);
    # a second, well-named entry isolates the kpi check.
    doc["assertions"] = [{"name": "a", "kpi": "", "op": "==", "value": 1}]
    codes = {p.code for p in validate_model_structured(doc)}
    assert "E_ASSERTION_KPI" in codes


def test_validation_flags_duplicate_names():
    doc = _bad_assertions_layout([
        {"name": "dup", "kpi": "system/x", "op": "==", "value": 1},
        {"name": "dup", "kpi": "system/y", "op": "==", "value": 2},
    ])
    codes = {p.code for p in validate_model_structured(doc)}
    assert "E_DUPLICATE_ASSERTION" in codes


def test_validation_flags_bad_replications_and_confidence():
    doc = _bad_assertions_layout([
        {"name": "a", "kpi": "system/x", "op": "==", "value": 1, "replications": 0},
        {"name": "b", "kpi": "system/y", "op": "==", "value": 1, "confidence": 1.5},
    ])
    codes = {p.code for p in validate_model_structured(doc)}
    assert "E_ASSERTION_REPLICATIONS" in codes
    assert "E_ASSERTION_CONFIDENCE" in codes


def test_schema_migrates_v24_to_v25_with_no_assertions():
    from nagasim.core.schema import CURRENT_VERSION, migrate

    assert CURRENT_VERSION >= 25
    v24 = {"schema_version": 24, "blocks": [], "links": [],
          "simulation": {"time_limit": None, "warmup": 0, "replications": 1}}
    migrated = migrate(v24)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["assertions"] == []


# --------------------------------------------------------------- CLI

def test_cli_check_exits_1_on_failure_and_prints_table(tmp_path, capsys):
    doc = _deterministic_layout([_throughput_assertion("wrong", "==", 2.0)])
    path = tmp_path / "model.json"
    path.write_text(json.dumps(doc))

    from nagasim.cli import main as launcher_main
    code = launcher_main(["check", str(path)])
    assert code == 1
    out = capsys.readouterr().out
    assert "wrong" in out
    assert "FAIL" in out


def test_cli_check_missing_file_exits_2(capsys):
    from nagasim.cli import main as launcher_main
    code = launcher_main(["check", "does-not-exist.json"])
    assert code == 2
    assert "naga check" in capsys.readouterr().err


def test_cli_ci_template_uses_given_path(capsys):
    from nagasim.cli import main as launcher_main
    code = launcher_main(["ci-template", "--path", "my_models/"])
    assert code == 0
    out = capsys.readouterr().out
    assert "naga check" in out
    assert "my_models/" in out


def test_cli_check_strict_treats_inconclusive_as_failure(tmp_path, capsys):
    doc = _md1_layout([_md1_assertion("straddles", "<=", 49.5)])
    path = tmp_path / "model.json"
    path.write_text(json.dumps(doc))

    from nagasim.cli import main as launcher_main
    code = launcher_main(["check", str(path)])
    assert code == 0  # inconclusive alone does not fail a plain run
    out = capsys.readouterr().out
    assert "INCONCLUSIVE" in out

    code = launcher_main(["check", str(path), "--strict"])
    assert code == 1


def test_junit_marks_inconclusive_as_skipped(tmp_path):
    import xml.etree.ElementTree as ET

    doc = _md1_layout([_md1_assertion("straddles", "<=", 49.5)])
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(doc))
    junit_path = tmp_path / "out.xml"

    from nagasim.cli import main as launcher_main
    code = launcher_main(["check", str(model_path), "--junit", str(junit_path)])
    assert code == 0

    [case] = ET.parse(junit_path).getroot().findall(".//testcase")
    assert case.find("failure") is None
    assert case.find("skipped") is not None


# --------------------------------------------------------------- run_reality_checks()
#
# Folded from tests/test_realitycheck.py (F4): the standalone `nagasim.realitycheck`
# module no longer exists, `run_reality_checks` lives in this module instead.

# A level with no in/outflow just holds its initial value; "sd": {"dt": 1.0}
# forces the fixed-step grid (rather than the event-driven exact-crossing
# default), so the sampler ticks on every integer up to time_limit regardless
# of whether anything is actually changing -- a deterministic grid to assert
# a violation TIME against.
_SD_SETTINGS = {"time_limit": 4.0, "timeseries_points": 4, "sd": {"dt": 1.0}}


def _workforce_model(production_expr: str) -> dict:
    return {
        "blocks": [], "links": [],
        "levels": [{"name": "Workforce", "initial": 10.0}],
        "auxiliaries": [{"name": "Production", "expr": production_expr}],
        "reality_checks": [
            {"name": "shutdown", "inputs": {"levels.Workforce.initial": 0},
             "constraint": 'AUX("Production") <= 0'},
        ],
        "simulation": dict(_SD_SETTINGS),
    }


def test_a_correct_model_passes_the_shutdown_check():
    layout = _workforce_model('LEVEL("Workforce") * 2')
    table = modelcheck.run_reality_checks(layout)
    assert list(table["Check"]) == ["shutdown"]
    row = table.iloc[0]
    assert bool(row["Passed"])
    assert row["Violation time"] is None
    assert row["Snapshot"] is None


def test_a_wrong_model_fails_at_the_right_time():
    # A spurious "+ STEP(1, 2)" term keeps Production positive from t=2 on,
    # even though Workforce is forced to 0 for the whole run -- exactly the
    # kind of typo a reality check exists to catch, and the check must name
    # the FIRST sampled instant it shows up at, not just "some" violation.
    layout = _workforce_model('LEVEL("Workforce") * 2 + STEP(1, 2)')
    table = modelcheck.run_reality_checks(layout)
    row = table.iloc[0]
    assert not bool(row["Passed"])
    assert row["Violation time"] == pytest.approx(2.0)
    assert row["Snapshot"]["aux/Production"] == pytest.approx(1.0)


def test_inventory_never_negative_with_a_min_bound_passes():
    layout = {
        "blocks": [], "links": [],
        "levels": [{"name": "Inventory", "initial": 5.0, "min": 0.0}],
        "flows": [{"name": "Ship", "rate": "20", "from": "Inventory"}],
        "reality_checks": [
            {"name": "no shortage", "inputs": {}, "constraint": 'LEVEL("Inventory") >= 0'},
        ],
        "simulation": dict(_SD_SETTINGS),
    }
    table = modelcheck.run_reality_checks(layout)
    assert bool(table.iloc[0]["Passed"])


def test_when_after_skips_samples_before_the_given_time():
    # A bump that shows up only at the t=2 sample, gone again by t=3: checked
    # "always" it fails at t=2; the SAME model checked "after 3" passes,
    # proving "when" actually narrows which samples are looked at rather than
    # just happening to pass.
    layout = _workforce_model('LEVEL("Workforce") * 2 + STEP(1, 2) * (1 if TNOW < 3 else 0)')
    always = modelcheck.run_reality_checks(layout)
    assert not bool(always.iloc[0]["Passed"])
    assert always.iloc[0]["Violation time"] == pytest.approx(2.0)

    layout["reality_checks"][0]["when"] = "after 3"
    after = modelcheck.run_reality_checks(layout)
    assert bool(after.iloc[0]["Passed"])


def test_a_model_with_no_reality_checks_is_unaffected():
    layout = schema.migrate(json.loads((MODELS_DIR / "single_server.json").read_text()))
    assert layout["reality_checks"] == []
    results = api.run_model(layout, replications=1, run_length=50, warmup=0, seed=0)
    assert results is not None
    table = modelcheck.run_reality_checks(layout)
    assert list(table.columns) == modelcheck.RESULT_COLUMNS
    assert table.empty


def test_bad_reality_checks_produce_validation_codes():
    layout = _workforce_model('LEVEL("Workforce") * 2')
    layout["reality_checks"] = [
        {"name": "", "inputs": {}, "constraint": "1 > 0"},
        {"name": "dup", "inputs": {}, "constraint": "1 > 0"},
        {"name": "dup", "inputs": {}, "constraint": "1 > 0"},
        {"name": "bad inputs", "inputs": ["not", "a", "map"], "constraint": "1 > 0"},
        {"name": "bad when", "inputs": {}, "constraint": "1 > 0", "when": "sometimes"},
        {"name": "bad syntax", "inputs": {}, "constraint": "AUX("},
        {"name": "unknown level", "inputs": {}, "constraint": 'LEVEL("Nope") > 0'},
    ]
    problems = schema.validate_model_structured(layout)
    codes = {p.code for p in problems}
    assert "E_REALITY_CHECK_NAME" in codes
    assert "E_DUPLICATE_REALITY_CHECK" in codes
    assert "E_REALITY_CHECK_INPUT" in codes
    assert "E_REALITY_CHECK_WHEN" in codes
    assert "E_EXPRESSION" in codes
    assert "E_LEVEL_UNKNOWN" in codes


def test_run_reality_checks_raises_a_clear_error_on_an_empty_constraint():
    layout = _workforce_model('LEVEL("Workforce") * 2')
    layout["reality_checks"][0]["constraint"] = ""
    with pytest.raises(ValueError, match="constraint"):
        modelcheck.run_reality_checks(layout)


# -- the agent tool ------------------------------------------------------------

def test_the_run_reality_checks_agent_tool(tmp_path):
    T.use_workspace(T.Workspace(tmp_path))
    model = "sd_shutdown"
    T.call("new_model", name=model)
    doc = T.workspace().document(model)
    doc.set_levels([{"name": "Workforce", "initial": 10.0}])
    doc.set_auxiliaries([{"name": "Production", "expr": 'LEVEL("Workforce") * 2'}])
    doc.set_reality_checks([
        {"name": "shutdown", "inputs": {"levels.Workforce.initial": 0},
         "constraint": 'AUX("Production") <= 0'},
    ])
    doc.set_simulation(**_SD_SETTINGS)

    result = T.call("run_reality_checks", model=model)
    assert "error" not in result, result.get("error")
    assert result["checks"] == [
        {"check": "shutdown", "passed": True, "violation_time": None, "snapshot": None},
    ]
