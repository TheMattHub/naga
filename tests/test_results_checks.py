"""Results.checks(): the law checks on every run's result, shown and never enforced (D906, D907)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nagasim import api
from nagasim.results import Results
from nagasim.runchecks import RunChecks

DATA = Path(__file__).resolve().parent / "data"
OVERLOADED = DATA / "triage_overloaded.json"    # Doctor offered load ~1.15: unstable by design
BALANCED = DATA / "triage_balanced.json"        # the same model with two doctors


@pytest.fixture(scope="module")
def overloaded():
    return api.run_model(str(OVERLOADED), replications=3, seed=0)


@pytest.fixture(scope="module")
def balanced():
    return api.run_model(str(BALANCED), replications=3, seed=0)


def test_overloaded_model_warns(overloaded):
    found = overloaded.checks()
    assert isinstance(found, RunChecks)
    names = [c["name"] for c in found.partial]
    assert names == ["offered load: Doctor"]
    assert found.passed >= 1 and found.note == ""
    assert found.lines()[0].startswith("offered load: Doctor: offered load 1.")


def test_balanced_model_is_quiet(balanced):
    assert balanced.checks().partial == ()
    assert balanced.checks().passed >= 5


def test_checks_are_not_a_frame():
    results = api.run_model(str(BALANCED), replications=2, seed=0)
    keys = sorted(results)
    first = results.checks()
    assert "checks" not in results and "_checks" not in results
    assert sorted(results) == keys
    assert results.checks() is first


def test_hand_built_results_have_no_layout(overloaded):
    found = Results(dict(overloaded)).checks()
    assert found.note == "no layout" and found.found == () and found.passed == 0
    assert found.as_dict() == {"passed": 0, "partial": [], "note": "no layout"}


def test_a_failing_check_becomes_a_note(overloaded, monkeypatch):
    from nagasim import laws

    def boom(layout, results, **kwargs):
        raise ValueError("bad frame")

    monkeypatch.setattr(laws, "checks", boom)
    copy = Results(dict(overloaded))
    copy._layout, copy._warmup = overloaded._layout, overloaded._warmup
    found = copy.checks()
    assert found.found == () and found.note == "checks failed: bad frame"
    assert "<table" in copy._repr_html_()


def test_html_lists_partial_checks(overloaded, balanced):
    page = overloaded._repr_html_()
    assert "offered load" in page and page.index("offered load") < page.index("<table")
    assert page.startswith("<ul><li>")
    assert "offered load" not in balanced._repr_html_()
    assert balanced._repr_html_().startswith("<p>")


def test_html_escapes_the_why():
    found = RunChecks(({"name": "x<y", "verdict": "partial", "why": "a & b"},))
    assert found._repr_html_() == "<ul><li>x&lt;y: a &amp; b</li></ul>"


def test_digest_uses_the_same_checks(tmp_path):
    from nagasim import agent_tools as T

    shutil.copy(OVERLOADED, tmp_path / "triage.json")
    ws = T.use_workspace(T.Workspace(tmp_path))
    T.call("open_model", path="triage.json", name="triage")
    digest = T.call("run_model", model="triage", replications=3, seed=0)
    results = ws.results["triage"]
    assert "_checks" in results.__dict__            # the digest went through results.checks()
    expected = results.checks().as_dict()
    del expected["note"]
    assert digest["laws"] == expected
    assert [c["name"] for c in digest["laws"]["partial"]] == ["offered load: Doctor"]


def test_digest_with_another_warmup_judges_afresh(overloaded):
    from nagasim.agent_tools.runs import _run_laws
    from nagasim import laws

    fresh = Results(dict(overloaded))
    fresh._layout, fresh._warmup = overloaded._layout, overloaded._warmup
    layout = dict(overloaded._layout)
    direct = laws.summary(laws.checks({**layout, "simulation": {**layout["simulation"], "warmup": 50}},
                                      fresh))
    assert _run_laws(layout, fresh, warmup=50) == direct
    assert "_checks" not in fresh.__dict__           # another warm-up: not the run's own checks
