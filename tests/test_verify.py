"""`naga verify` (D689): every textbook case passes, the report shape is stable,
and the oracle tests share the command's cases."""
import json

import pytest

from nagasim import verify
from test_oracles import ORACLE_CASES


@pytest.fixture(scope="module")
def quick_rows():
    return verify.run(replications=8)


@pytest.mark.slow
def test_every_case_passes_quick(quick_rows):
    failed = [r for r in quick_rows if r["passed"] is False]
    assert not failed, failed
    assert {r["case"] for r in quick_rows} == {c.name for c in verify.CASES}


def test_json_report_shape(capsys, tmp_path):
    # Two cheap cases through main(): the shape is the same for every case.
    cases = [c for c in verify.CASES if c.name in ("D/D/1", "M/M/1/K")]
    verify.CASES, saved = cases, verify.CASES
    try:
        code = verify.main(["--quick", "--json", "--html", str(tmp_path / "v.html")])
    finally:
        verify.CASES = saved
    rows = json.loads(capsys.readouterr().out)
    assert code == 0
    assert all(set(r) == {"case", "kpi", "theory", "mean", "half_width", "passed",
                                 "covered", "blocks"} for r in rows)
    assert "D/D/1" in (tmp_path / "v.html").read_text(encoding="utf-8")


def test_a_wrong_theory_fails():
    case = verify._dd1()
    case.theory = {**case.theory, "W": 5.0}
    rows = verify.run([case])
    assert [r["passed"] for r in rows if r["kpi"] == "W"] == [False]


def test_oracles_use_verify_cases():
    assert set(ORACLE_CASES) <= {c.name for c in verify.CASES}


def test_cases_use_distinct_streams(monkeypatch):
    # #1329: every case used seeds seed + r, so all cases shared their random numbers.
    seen = {}
    current = []

    def fake(layout, seed, run_len, warmup):
        seen.setdefault(current[0], []).append(seed)
        return {}

    monkeypatch.setattr(verify, "run_single_simulation", fake)
    monkeypatch.setattr(verify, "_read", lambda kpi, spec: 1.0)
    for case in (verify._mm1(), verify._jackson()):
        current[:] = [case.name]
        verify.run([case], replications=2)
    assert seen["M/M/1"][0] != seen["Jackson tandem"][0]
    assert seen["M/M/1"] == [seen["M/M/1"][0] + i for i in range(2)]


@pytest.mark.slow
def test_blocks_report_coverage():
    # #1330: the report said nothing about how often the interval holds the theory.
    rows = verify.run([verify._dd1(), verify._mm1k()], replications=8, blocks=3)
    assert rows and all(r["blocks"] == 3 and 0 <= r["covered"] <= 3 for r in rows)
    single = verify.run([verify._mm1k()], replications=8)
    first = [r for r in rows if r["case"] == "M/M/1/K"][0]
    assert (single[0]["mean"], single[0]["passed"]) == (first["mean"], first["passed"])
    assert single[0]["blocks"] == 1
