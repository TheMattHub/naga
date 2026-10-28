"""`naga run` prints a summary, `--raw` the frame dump, `--json` a document; `naga check --json` (D908, D909)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import nagasim
from nagasim import api, modelcheck

DATA = Path(__file__).resolve().parent / "data"
OVERLOADED = str(DATA / "triage_overloaded.json")
SINGLE = str(Path(nagasim.__file__).parent / "models" / "single_server.json")
QUICK = ["--replications", "5", "--run-length", "500"]


def run(capsys, *argv):
    code = api.main([*argv])
    return code, capsys.readouterr()


def test_summary_is_the_default(capsys):
    code, out = run(capsys, SINGLE, *QUICK)
    lines = out.out.splitlines()
    assert code == 0
    assert lines[0].split() == ["Metric", "Mean", "+/-95%", "n"]
    assert any(line.startswith("Avg Throughput Time") for line in lines)
    assert len(lines) <= 40
    assert lines[-1].startswith("replications for +/-5% on Avg Throughput Time: ")


def test_raw_keeps_the_frame_dump(capsys):
    code, out = run(capsys, SINGLE, *QUICK, "--raw")
    expected = api.run_model(SINGLE, replications=5, run_length=500, seed=0)["system"]
    assert code == 0
    assert out.out == expected.to_string(index=False) + "\n"


def test_json_shape(capsys):
    code, out = run(capsys, SINGLE, *QUICK, "--json")
    doc = json.loads(out.out)
    assert code == 0
    assert set(doc) == {"kpis", "checks", "replications"}
    assert {"passed", "partial"} <= set(doc["checks"])
    assert doc["kpis"][0].keys() == {"metric", "mean", "half_width", "n"}
    assert doc["replications"]["metric"] == "Avg Throughput Time"


def test_json_with_one_replication_has_nulls_not_nan(capsys):
    code, out = run(capsys, SINGLE, "--replications", "1", "--run-length", "300", "--json")
    doc = json.loads(out.out, parse_constant=lambda c: pytest_fail(c))
    assert code == 0
    assert all(row["half_width"] is None for row in doc["kpis"])
    assert doc["replications"] is None


def pytest_fail(constant):      # json.loads calls this for NaN / Infinity
    raise AssertionError(f"{constant} in the JSON output")


def test_overloaded_run_prints_warnings(capsys):
    _, out = run(capsys, OVERLOADED, "--replications", "3")
    assert any(line.startswith("WARN offered load") for line in out.out.splitlines())


def test_one_replication_prints_na(capsys):
    _, out = run(capsys, SINGLE, "--replications", "1", "--run-length", "300")
    row = next(l for l in out.out.splitlines() if l.startswith("Avg Throughput Time"))
    assert row.split()[-2] == "n/a" and row.split()[-1] == "1"
    assert "replications for" not in out.out


def test_replications_needed_math():
    assert api.replications_needed(10.0, 1.0, 5) == 20
    assert api.replications_needed(10.0, math.nan, 5) is None
    assert api.replications_needed(0.0, 1.0, 5) is None


def test_stopped_run_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(api, "run_model", lambda *a, **k: None)
    code, out = run(capsys, SINGLE)
    assert code == 1
    assert "stopped" in out.err


def test_no_checks_flag(capsys):
    _, out = run(capsys, OVERLOADED, "--replications", "3", "--no-checks")
    assert "WARN" not in out.out


def test_check_json(capsys):
    code = modelcheck.main(["check", SINGLE, "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    assert {"problems", "laws"} <= set(doc)
    assert isinstance(doc["problems"], list)
    assert {"passed", "partial"} <= set(doc["laws"])


def test_check_json_problem_shape(monkeypatch, capsys):
    from nagasim.core import schema
    found = schema.problem("W_UNKNOWN_KEY", "demo", "/x", hint="drop it")
    monkeypatch.setattr(modelcheck, "_validation_problems", lambda path: [found])
    modelcheck.main(["check", SINGLE, "--json"])
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert problems == [found.as_dict()]


def test_zero_rows_hidden_in_the_summary_but_not_in_json(capsys):
    _, out = run(capsys, SINGLE, *QUICK)
    assert "Log Truncated" not in out.out and "Total Cost" not in out.out
    _, out = run(capsys, SINGLE, *QUICK, "--json")
    metrics = {row["metric"] for row in json.loads(out.out)["kpis"]}
    assert {"Entity Log Truncated", "Total Cost"} <= metrics
