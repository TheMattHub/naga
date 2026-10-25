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
from nagasim.core.schema.sections_validation import parse_when

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


# -- "when": windows and "eventually" (D677) ------------------------------------
#
# A ramp: L(t) = t, sampled at t = 0, 1, ..., 10 (the SD grid and the sampler agree), so what a
# constraint on L does at every sample is known by hand. _LE5 holds for t <= 5, _GE6 for t >= 6.

_LE5, _GE6, _GE0, _NEVER = ('LEVEL("L") <= 5', 'LEVEL("L") >= 6', 'LEVEL("L") >= 0',
                            'LEVEL("L") > 100')
_FROM_START = "held from the start of the window: nothing had to happen"


def _ramp(constraint, when):
    return {"blocks": [], "links": [],
            "levels": [{"name": "L", "initial": 0.0}],
            "flows": [{"name": "up", "rate": "1", "to": "L"}],
            "reality_checks": [{"name": "n", "inputs": {}, "constraint": constraint,
                                "when": when}],
            "simulation": {"time_limit": 10.0, "timeseries_points": 10, "sd": {"dt": 1.0}}}


def _outcome(layout):
    row = modelcheck.run_reality_checks(layout).iloc[0]
    return bool(row["Passed"]), row["Violation time"], row["Note"]


@pytest.mark.parametrize("constraint, when, passed, at, note", [
    # The kept forms: same results, no note.
    (_LE5, "always", False, 6.0, ""),
    (_GE6, "always", False, 0.0, ""),
    (_LE5, "final", False, 10.0, ""),
    (_GE6, "final", True, None, ""),
    (_LE5, "after 7", False, 7.0, ""),
    (_LE5, "after 11", True, None, ""),
    # "before <t>" and "between <t1> and <t2>": every sample in the window, both ends in.
    (_LE5, "before 5", True, None, ""),
    (_LE5, "before 6", False, 6.0, ""),
    (_LE5, "between 0 and 5", True, None, ""),
    (_LE5, "between 2 and 5", True, None, ""),
    (_LE5, "between 2 and 6", False, 6.0, ""),
    (_LE5, "between 7 and 9", False, 7.0, ""),
    # "eventually": at least once in the window; a hold at the window's first sample is noted.
    (_GE6, "eventually", True, None, ""),
    (_GE0, "eventually", True, None, _FROM_START),
    (_GE6, "eventually after 3", True, None, ""),
    (_GE6, "eventually after 8", True, None, _FROM_START),
    (_GE6, "eventually before 6", True, None, ""),
    (_GE6, "eventually before 5", False, 5.0, "never held before 5"),
    (_GE6, "eventually between 5 and 7", True, None, ""),
    (_GE6, "eventually between 6 and 9", True, None, _FROM_START),
    (_GE6, "eventually between 1 and 5", False, 5.0, "never held between 1 and 5"),
    # The failure is stamped at the window's end, or at the run's end when the window has none
    # or runs past it.
    (_NEVER, "eventually", False, 10.0, "never held during the run"),
    (_NEVER, "eventually after 3", False, 10.0, "never held after 3"),
    (_NEVER, "eventually before 20", False, 10.0, "never held before 20"),
    (_NEVER, "eventually between 2 and 4", False, 4.0, "never held between 2 and 4"),
    (_GE6, "eventually between 12 and 20", False, 10.0, "never held between 12 and 20"),
])
def test_when_forms_on_a_ramp(constraint, when, passed, at, note):
    got_passed, got_at, got_note = _outcome(_ramp(constraint, when))
    assert got_passed is passed
    assert got_at == pytest.approx(at) if at is not None else got_at is None
    assert got_note == note


def test_the_result_messages_name_the_window_and_the_vacuous_pass():
    def message(constraint, when):
        [result] = modelcheck.check(_ramp(constraint, when))
        return result.message
    assert message(_LE5, "always") == "reality check 'n' failed at t=6"
    assert message(_GE6, "final") == "reality check 'n' passed"
    assert message(_GE0, "eventually") == f"reality check 'n' passed ({_FROM_START})"
    assert message(_GE6, "eventually between 1 and 5") == \
        "reality check 'n' failed at t=5 (never held between 1 and 5)"


def test_a_vacuous_pass_is_printed_but_a_plain_pass_stays_ok(capsys):
    modelcheck._print_table("m", modelcheck.check(_ramp(_GE0, "eventually")))
    modelcheck._print_table("m", modelcheck.check(_ramp(_GE6, "eventually")))
    out = capsys.readouterr().out.splitlines()
    assert out[1].endswith(f"n: ok ({_FROM_START})")
    assert out[3].endswith("n: ok")


def test_the_kept_forms_leave_the_live_probe_as_it_was():
    base = {"blocks": [], "links": []}
    probe = json.dumps("~reality_check")
    for when, tail in (("always", ""), ("after 3", " and TNOW >= 3.0")):
        [delay] = modelcheck._with_probe(base, _LE5, *parse_when(when))["delays"]
        assert delay["condition"] == f"DELAY({probe}) < 0 and not ({_LE5}){tail}"
    assert modelcheck._with_probe(base, _LE5, *parse_when("final")) is base


def test_held_from_the_start_needs_the_probe_to_agree_when_samples_are_coarse():
    # L = t on a 1-unit grid, sampled every 5: L >= 3 first holds at t=3, after the window's
    # start (1), though t=5 is the window's first sample; L >= 0 holds at 1 itself.
    coarse = {**_ramp('LEVEL("L") >= 3', "eventually after 1")}
    coarse["simulation"] = {**coarse["simulation"], "timeseries_points": 2}
    assert _outcome(coarse) == (True, None, "")
    coarse["reality_checks"][0]["constraint"] = _GE0
    assert _outcome(coarse) == (True, None, _FROM_START)


def test_the_live_probe_for_eventually_holds_the_first_instant_it_held_in_the_window():
    [delay] = modelcheck._with_probe({}, _GE6, *parse_when("eventually between 2 and 4"))["delays"]
    assert delay["condition"] == (f"DELAY({json.dumps('~reality_check')}) < 0 and ({_GE6})"
                                  " and TNOW >= 2.0 and TNOW <= 4.0")


def _dip(constraint, when):
    """x is -1 only on the SD grid points 5.01 and 5.015; the sampler (every 0.05) never lands there."""
    return {"levels": [{"name": "L", "initial": 0.0}],
            "flows": [{"name": "f", "to": "L", "rate": "1 + 0*TNOW"}],
            "auxiliaries": [{"name": "x", "expr": "IF(TNOW > 5.005 and TNOW < 5.02, -1, 1)"}],
            "reality_checks": [{"name": "n", "constraint": constraint, "when": when}],
            "simulation": {"time_limit": 10.0, "sd": {"dt": 0.005}, "timeseries_points": 200}}


@pytest.mark.parametrize("constraint, when, passed, at, note", [
    # A dip between samples: only the live probe can see it, and it honours the window.
    ('AUX("x") > 0', "between 5 and 5.5", False, 5.01, ""),
    ('AUX("x") > 0', "between 5.5 and 7", True, None, ""),
    ('AUX("x") > 0', "before 4.5", True, None, ""),
    ('AUX("x") > 0', "after 5.5", True, None, ""),
    ('AUX("x") < 0', "eventually between 5 and 5.5", True, None, ""),
    ('AUX("x") < 0', "eventually", True, None, ""),
    ('AUX("x") < 0', "eventually before 4.5", False, 4.5, "never held before 4.5"),
    ('AUX("x") < 0', "eventually after 5.5", False, 10.0, "never held after 5.5"),
])
def test_the_live_probe_sees_between_samples_inside_the_window(constraint, when, passed, at, note):
    got_passed, got_at, got_note = _outcome(_dip(constraint, when))
    assert got_passed is passed
    assert got_at == pytest.approx(at) if at is not None else got_at is None
    assert got_note == note


def test_parse_when_gives_the_mode_and_the_window():
    inf = float("inf")
    parse = parse_when
    assert parse("always") == ("always", -inf, inf)
    assert parse("final") == ("final", -inf, inf)
    assert parse("after 3") == ("always", 3.0, inf)
    assert parse("before 2.5") == ("always", -inf, 2.5)
    assert parse("between 1 and 5") == ("always", 1.0, 5.0)
    assert parse("between 4 and 4") == ("always", 4.0, 4.0)
    assert parse("eventually") == ("eventually", -inf, inf)
    assert parse("eventually after 3") == ("eventually", 3.0, inf)
    assert parse("eventually before 2") == ("eventually", -inf, 2.0)
    assert parse("  eventually   between -1.5  and 5 ") == ("eventually", -1.5, 5.0)


_BAD_WHEN = ["sometimes", "between 5 and 2", "eventually between 5 and 2", "between 1", "between 1 and",
             "between and 2", "before", "after x", "eventually final", "eventually always",
             "eventually eventually", "after 3 and 4", "between 1 and 2 and 3", "eventually between"]


@pytest.mark.parametrize("when", _BAD_WHEN)
def test_a_bad_when_is_refused_before_the_run(when, monkeypatch):
    layout = _ramp(_LE5, when)
    assert "E_REALITY_CHECK_WHEN" in {p.code for p in schema.validate_model_structured(layout)}

    def no_run(*args, **kwargs):
        raise AssertionError("the model ran")
    monkeypatch.setattr(modelcheck.api, "run_model", no_run)
    with pytest.raises(ValueError, match="reality check 'n'.*when"):
        modelcheck.run_reality_checks(layout)


def test_a_window_that_ends_before_it_starts_says_so():
    with pytest.raises(ValueError, match="ends before it starts"):
        parse_when("eventually between 5 and 2")


def test_every_good_when_validates():
    for when in ("always", "final", "after 3", "before 3", "between 1 and 2", "eventually",
                 "eventually after 3", "eventually before 3", "eventually between 1 and 2"):
        assert "E_REALITY_CHECK_WHEN" not in {
            p.code for p in schema.validate_model_structured(_ramp(_LE5, when))}, when


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
        {"check": "shutdown", "passed": True, "violation_time": None, "snapshot": None, "note": ""},
    ]


# ------------------------------------------------------- W_LONG_NAME (Phase 13 D)
def _long_name_problems(doc):
    return [p for p in validate_model_structured(doc) if p.code == "W_LONG_NAME"]


def _line_with(name):
    c, d = create(), dispose()
    p = process(name)
    return layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 1}])


def test_long_name_20_chars_is_silent_and_21_warns_with_its_path():
    assert not _long_name_problems(_line_with("x" * 20))
    found = _long_name_problems(_line_with("x" * 21))
    assert len(found) == 1
    assert found[0].severity == "warning"
    assert found[0].block == "x" * 21 and found[0].path == "/blocks/1"
    assert "21 characters" in found[0].message and found[0].hint


def test_long_name_warns_for_a_block_nobody_links():
    doc = _line_with("ok")
    doc["blocks"].append(process("y" * 30, resource="Server"))
    assert [p.block for p in _long_name_problems(doc)] == ["y" * 30]


def test_long_name_in_a_submodel_warns_with_its_own_flattened_name():
    def blk(name, type_name, **params):
        return {"name": name, "description": "", "type": type_name, "position": [0, 0],
                "params": params}
    long = "a_rather_long_inner_block"
    inner = {"info": {}, "parameters": [], "annotations": [],
             "blocks": [blk("in", "SUB_ENTRY", port="in"),
                        blk(long, "DELAY", distribution="Constant", dist_params={"value": 1.0}),
                        blk("out", "SUB_EXIT", port="out")],
             "links": [{"source": "in", "target": long}, {"source": long, "target": "out"}]}
    c, d = create(), dispose()
    cell = blk("cell", "SUBMODEL", definition="Cell", arguments={})
    doc = layout([c, cell, d], chain(c, cell, d), submodels={"Cell": inner})
    doc["schema_version"] = schema.CURRENT_VERSION
    found = _long_name_problems(doc)
    assert len(found) == 1 and found[0].block == "cell/" + long
    assert found[0].path.startswith("/blocks/")


def test_cli_validate_prints_the_long_name_warning(tmp_path, capsys):
    from nagasim import cli
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_line_with("n" * 25)), encoding="utf-8")
    cli.main(["validate", str(path)])
    out = capsys.readouterr().out
    assert "n" * 25 in out and "canvas wraps it past 20" in out
