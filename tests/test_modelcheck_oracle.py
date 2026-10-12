"""Independent oracle for W7 Q3 "Model assertions, `naga check`, CI
template" (PHASE6-6D-W7-PLAN, D126). Written BEFORE `nagasim.modelcheck`
exists, from the plan text and the model's own conventions
(`tests/conftest.py`, `nagasim/experiments.py`'s KPI key grammar) -- not from
the (nonexistent) implementation. Failures here are the spec, not a bug
report; the coordinator reconciles real names/shapes once Q3 lands.

Reading choices this oracle bakes in:
- KPI keys are the ones `experiments.flatten_kpis` actually produces
  (`resource/<name>/<column>`, `system/<Metric>` -- see its docstring). The
  plan text's own M/D/1 example uses a 0..1 utilisation fraction, but the
  real resource KPI is `resource/<name>/Usage % (snapshot)`, on a 0..100
  scale (`core/outputs.py`). Assertions below use that scale throughout
  (value=70 not 0.7, tolerance=10 not 0.1) -- an existing KPI key, used
  exactly, beats inventing a fractional one that doesn't exist.
- "the CI of the KPI mean lies on the right side" is read per the plan text:
  `<=`/`>=`/`<`/`>` pass when the WHOLE confidence interval clears the bound;
  `==` passes when the whole interval lies inside value +/- tolerance.
- M/D/1 utilisation: for a stable single-server queue (capacity 1, no
  blocking upstream), the long-run fraction of time the server is busy
  equals arrival_rate * mean_service_time (each unit of arrival throughput
  needs exactly that much server time; PASTA/renewal-reward, independent of
  the service distribution's shape) -- NOT an approximation. Here
  rate=0.5, service=1.0 (Constant) => rho = 0.5 exactly = 50%.
  Its SAMPLING variance is only approximate: for M/D/1 the busy-indicator
  process mixes over an O(1/rate) = O(2) timescale, so a single T=2000
  replication averages roughly T / 2 ~= 1000 quasi-independent busy/idle
  cycles and the per-replication utilisation estimate has a standard
  deviation on the order of a few percentage points (var ~ rho(1-rho) per
  cycle, divided by ~1000 cycles). Five such replications' CI half-width is
  then well under 10 points -- comfortably inside the 20-point margins used
  below (70 vs. 50, and 50 +/- 10). This is the generous-horizon argument
  the plan brief asks to state, not a tight bound.
- The deterministic case (throughput) uses `simulation.replications: 1`:
  every distribution involved is Constant, so the run is bit-for-bit
  reproducible and a single replication already IS the exact answer --
  sidesteps any question of how a not-yet-built CI formula handles a
  zero-variance / single-replication sample.

PY = the project's Python 3.12 interpreter (not used directly here;
the suite is run through pytest as usual). QT_QPA_PLATFORM=offscreen for headless Qt import safety
(`nagasim.cli` never imports the editor at all; see `_require_cli` below).
"""
from __future__ import annotations

import inspect
import json
import xml.etree.ElementTree as ET

import pytest
import yaml

from conftest import chain, create, dispose, layout, process

# -- adapters: the one place that knows modelcheck's real call shape --------

def _check(doc):
    from nagasim import modelcheck
    return modelcheck.check(doc)


def _name(result):
    return getattr(result, "name")


def _passed(result):
    return getattr(result, "passed")


def _assert_model(path):
    from nagasim import modelcheck
    modelcheck.assert_model(path)


def _run_cli(args):
    from nagasim.cli import main
    return main(args)


def _cli_wired_for(token):
    """Guard, not a feature probe: string-sniffs main()'s source for the
    subcommand literal instead of a real command registry; drop once Q3
    ships and calling it is unconditionally safe."""
    import nagasim.cli as launcher
    try:
        src = inspect.getsource(launcher.main)
    except OSError:
        return False
    return f'"{token}"' in src or f"'{token}'" in src


def _require_cli(token):
    if not _cli_wired_for(token):
        pytest.fail(f"naga {token} is not wired into nagasim.cli.main yet (W7 Q3 pending)")


# -- shared model fixtures (wire format: docs/ARCHITECTURE.md section 7) ---

def _resource(name, capacity=1):
    return {"name": name, "capacity": capacity, "discipline": "FIFO",
            "cost_busy": 0.0, "cost_idle": 0.0, "cost_per_use": 0.0,
            "cost_time_unit": "hours"}


def _md1_layout(assertions):
    """M/D/1: Exponential(rate=0.5) arrivals (mean interarrival 2), Constant(1.0)
    service, capacity-1 Server -> rho = 0.5 exactly (see module docstring).
    T=2000, 5 replications: generous horizon per the plan brief."""
    c = create(distribution="Exponential", dist_params={"rate": 0.5}, max_arrivals=0, start_time=0)
    p = process(resource="Server", value=1.0, distribution="Constant")
    d = dispose()
    doc = layout([c, p, d], chain(c, p, d), resources=[_resource("Server")],
                simulation={"time_limit": 2000, "warmup": 0, "replications": 5})
    doc["assertions"] = assertions
    return doc


def _md1_assertion(name, op, value, tolerance=0):
    return {"name": name, "kpi": "resource/Server/Usage % (snapshot)", "op": op,
            "value": value, "replications": 5, "confidence": 0.95, "tolerance": tolerance}


def _deterministic_layout(assertions):
    """Create(Constant 1.0, max_arrivals=5) -> Delay(Constant 1.0) -> Dispose,
    no resource (so no queueing at all): arrivals at t=0,1,2,3,4, each
    disposes exactly 1 later (t=1..5). No time_limit -> the run drains
    naturally at t=5 (DEVLOG #32 horizon rule), so total_time = 5 - 0 = 5 and
    completed = 5 => throughput (per time unit) = 5 / 5 = 1.0 EXACTLY,
    independent of seed (every distribution here is Constant)."""
    c = create(distribution="Constant", dist_params={"value": 1.0}, max_arrivals=5, start_time=0)
    delay_blk = {"name": "delay_1", "description": "delay_1", "type": "DELAY", "position": [1, 0],
                "params": {"distribution": "Constant", "dist_params": {"value": 1.0}}}
    d = dispose()
    doc = layout([c, delay_blk, d], chain(c, delay_blk, d),
                simulation={"time_limit": None, "warmup": 0, "replications": 1})
    doc["assertions"] = assertions
    return doc


def _throughput_assertion(name, op, value, tolerance=0):
    return {"name": name, "kpi": "system/Throughput (per time unit)", "op": op,
            "value": value, "replications": 1, "confidence": 0.95, "tolerance": tolerance}


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


# -- case 1: M/D1 utilisation, true/false sides + an == case ----------------

def test_md1_utilization_assertions_true_false_and_equal():
    doc = _md1_layout([
        _md1_assertion("util_le_70", "<=", 70),
        _md1_assertion("util_ge_70", ">=", 70),
        _md1_assertion("util_eq_50", "==", 50, tolerance=10),
    ])
    by_name = {_name(r): _passed(r) for r in _check(doc)}
    assert by_name == {"util_le_70": True, "util_ge_70": False, "util_eq_50": True}


# -- case 2: a deterministic KPI, "==" with tolerance 0 ----------------------

def test_deterministic_throughput_equals_exactly():
    doc = _deterministic_layout([_throughput_assertion("throughput_is_one", "==", 1.0, tolerance=0)])
    by_name = {_name(r): _passed(r) for r in _check(doc)}
    assert by_name == {"throughput_is_one": True}


# -- case 3: no assertions section -> [] and CLI exit 0 ----------------------

def test_no_assertions_section_is_an_empty_list_and_cli_exits_zero(tmp_path):
    c, d = create(max_arrivals=1), dispose()
    doc = layout([c, d], chain(c, d))
    assert "assertions" not in doc
    assert _check(doc) == []

    path = _write(tmp_path, "model.json", doc)
    _require_cli("check")
    code = _run_cli(["check", path])
    assert code == 0


# -- case 4: assert_model raises AssertionError naming the failing assertion -

def test_assert_model_raises_naming_the_failing_assertion(tmp_path):
    doc = _deterministic_layout([_throughput_assertion("throughput_should_be_two", "==", 2.0, tolerance=0)])
    path = _write(tmp_path, "model.json", doc)

    with pytest.raises(AssertionError, match="throughput_should_be_two"):
        _assert_model(path)


# -- case 5: --junit writes one testcase per assertion, a failure for the bad one

def test_junit_output_has_one_testcase_per_assertion(tmp_path):
    doc = _deterministic_layout([
        _throughput_assertion("throughput_ok", "==", 1.0, tolerance=0),
        _throughput_assertion("throughput_bad", "==", 2.0, tolerance=0),
    ])
    model_path = _write(tmp_path, "model.json", doc)
    junit_path = tmp_path / "out.xml"

    _require_cli("check")
    code = _run_cli(["check", model_path, "--junit", str(junit_path)])
    assert code == 1  # one assertion fails

    assert junit_path.exists()
    root = ET.parse(junit_path).getroot()
    testcases = root.findall(".//testcase")
    assert len(testcases) == 2

    by_name = {tc.get("name"): tc for tc in testcases}
    assert set(by_name) == {"throughput_ok", "throughput_bad"}
    assert by_name["throughput_ok"].find("failure") is None
    assert by_name["throughput_bad"].find("failure") is not None


# -- case 6: ci-template prints valid YAML mentioning "naga check" ----------

def test_ci_template_is_valid_yaml_mentioning_naga_check(capsys):
    _require_cli("ci-template")
    code = _run_cli(["ci-template"])
    out = capsys.readouterr().out
    assert code == 0
    assert "naga check" in out

    doc = yaml.safe_load(out)
    assert isinstance(doc, dict)  # a real GitHub Actions workflow, not prose that merely parses


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
