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
    assert all(set(r) == {"case", "kpi", "theory", "mean", "half_width", "passed"} for r in rows)
    assert "D/D/1" in (tmp_path / "v.html").read_text(encoding="utf-8")


def test_a_wrong_theory_fails():
    case = verify._dd1()
    case.theory = {**case.theory, "W": 5.0}
    rows = verify.run([case])
    assert [r["passed"] for r in rows if r["kpi"] == "W"] == [False]


def test_oracles_use_verify_cases():
    assert set(ORACLE_CASES) <= {c.name for c in verify.CASES}
