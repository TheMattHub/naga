"""The study folder and study.json (nagasim.study)."""
import json
import os

import pytest

from nagasim import study


def test_create_makes_the_folders_and_files(tmp_path):
    folder = tmp_path / "s"
    study.create(folder, "Should we add a second crane?")
    for sub in ("inputs", "versions", "versions/drafts", "tests", "outputs"):
        assert (folder / sub).is_dir()
    assert (folder / "study.json").is_file() and (folder / "AGENTS.md").is_file()
    data = study.load(folder)
    assert data["format"] == 1 and data["mode"] == "one_go" and data["size"] == "standard"
    assert data["plan_approved"] is False and data["versions"] == []
    assert data["question"] == "Should we add a second crane?"


def test_create_refuses_an_existing_study(tmp_path):
    study.create(tmp_path / "s", "q")
    with pytest.raises(study.StudyError) as exc:
        study.create(tmp_path / "s", "q")
    assert exc.value.code == "E_STUDY_EXISTS"


@pytest.mark.parametrize("kw", [{"mode": "fast"}, {"size": "huge"}])
def test_create_rejects_unknown_mode_and_size(tmp_path, kw):
    with pytest.raises(study.StudyError) as exc:
        study.create(tmp_path / "s", "q", **kw)
    assert exc.value.code == "E_STUDY_BAD_VALUE"


def test_load_of_a_missing_study_is_coded(tmp_path):
    with pytest.raises(study.StudyError) as exc:
        study.load(tmp_path / "none")
    assert exc.value.code == "E_STUDY_NOT_FOUND"


def test_load_of_a_truncated_file_is_a_coded_error(tmp_path):
    study.create(tmp_path / "s", "q")
    path = tmp_path / "s" / "study.json"
    path.write_bytes(b'{"format": 1, "quest')
    with pytest.raises(study.StudyError) as exc:
        study.load(tmp_path / "s")
    assert exc.value.code == "E_STUDY_UNREADABLE"
    assert path.read_bytes() == b'{"format": 1, "quest'


def test_load_refuses_wrong_types_of_required_keys(tmp_path):
    study.create(tmp_path / "s", "q")
    data = study.load(tmp_path / "s")
    data["question"] = 5
    study.save(tmp_path / "s", data)
    with pytest.raises(study.StudyError) as exc:
        study.load(tmp_path / "s")
    assert exc.value.code == "E_STUDY_UNREADABLE"


def test_load_of_a_newer_format_is_refused(tmp_path):
    study.create(tmp_path / "s", "q")
    data = study.load(tmp_path / "s")
    data["format"] = 2
    study.save(tmp_path / "s", data)
    with pytest.raises(study.StudyError) as exc:
        study.load(tmp_path / "s")
    assert exc.value.code == "E_STUDY_FORMAT"


def test_load_fills_missing_optional_keys(tmp_path):
    study.create(tmp_path / "s", "q")
    (tmp_path / "s" / "study.json").write_text(
        '{"format": 1, "question": "q", "mode": "one_go", "size": "quick"}')
    data = study.load(tmp_path / "s")
    assert data["goals"] == [] and data["accepted"] is None and data["requests"] == {}


def test_a_failed_save_leaves_the_old_file(tmp_path, monkeypatch):
    study.create(tmp_path / "s", "q")
    old = study.load(tmp_path / "s")
    new = dict(old, question="changed")

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        study.save(tmp_path / "s", new)
    monkeypatch.undo()
    assert study.load(tmp_path / "s") == old


def _fresh(**kw):
    s = {"format": 1, "question": "q", "mode": "one_go", "size": "standard", "plan_approved": False,
         "accepted": None, "decision": "", "requests": {}}
    s.update({k: [] for k in ("goals", "target", "assumptions", "sources", "validation",
                              "experiments", "versions", "usage")})
    s.update(kw)
    return s


def _verdicts(s, folder=None):
    return {c["item"]: c["verdict"] for c in study.checklist(s, folder)}


def test_a_new_study_is_not_verified_where_nothing_is_known():
    v = _verdicts(_fresh())
    assert v["question"] == "passed"
    assert v["verification"] == v["validation"] == v["experiments"] == "not_verified"
    sf = study.sufficiency(_fresh())
    assert sf["sufficient"] is False and sf["label"] == "not yet sufficient"


def _good(**kw):
    return _fresh(goals=[{"kpi": "Avg Throughput Time", "high": 3}],
                  versions=[{"id": "v001", "verdict": "passed", "kpis": {"Avg Throughput Time": 2.0}}], **kw)


def test_not_verified_never_counts_as_passed():
    sf = study.sufficiency(_good())
    assert sf["sufficient"] is False and any("validation" in w for w in sf["why"])


def test_sufficient_needs_tests_validation_and_goals():
    s = _good(validation=[{"level": "known_answer", "passed": True}])
    sf = study.sufficiency(s)
    assert sf["sufficient"] is True and sf["label"] == "sufficient for its goals"
    assert sf["validation_level"] == "known_answer"
    assert study.goals_status(s)[0]["status"] == "met"
    s["versions"][0]["kpis"]["Avg Throughput Time"] = 4.0
    assert study.goals_status(s)[0]["status"] == "not_met"
    assert study.sufficiency(s)["sufficient"] is False


def test_person_acceptance_is_labelled():
    s = _good(validation=[{"level": "data", "passed": True}], accepted={"reason": "pilot only", "by": "person"})
    s["versions"][0]["verdict"] = "failed"
    sf = study.sufficiency(s)
    assert sf["sufficient"] is False and "accepted with open issues: pilot only" in sf["label"]


def test_next_step_asks_for_the_plan_card_first_in_one_go():
    assert "plan card" in study.next_step(_fresh())
    assert "plan card" not in study.next_step(_fresh(mode="incremental"))
    assert "plan card" not in study.next_step(_fresh(plan_approved=True))


def test_next_step_reports_when_sufficient_and_at_the_cap():
    s = _good(validation=[{"level": "data", "passed": True}], plan_approved=True)
    assert study.next_step(s).startswith("Report the result")
    capped = _fresh(size="quick", plan_approved=True, versions=[{"id": f"v{i}"} for i in range(3)])
    assert "size cap is reached" in study.next_step(capped)


def test_effort_grows_with_size():
    calls = [study.effort(_fresh(size=z))["tool_calls"] for z in ("quick", "standard", "deep")]
    assert calls == sorted(calls) and len(set(calls)) == 3
    assert study.effort(_fresh(size="quick"))["tool_calls"] == 18


def test_checklist_items_are_computed_from_the_study(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "a.csv").write_text("x")
    s = _fresh(goals=[{"text": "be fast"}], target=[{"name": "dock", "blocks": ["Server"]}, {"name": "x"}],
               sources=[{"input": "inputs/a.csv"}], assumptions=[{"text": "t", "source": "data"}, {"text": "u"}],
               skipped=["experiments"], size="quick")
    v = _verdicts(s, tmp_path)
    assert v["goals"] == "partial" and v["scope"] == "partial" and v["inputs"] == "passed"
    assert v["assumptions"] == "partial" and v["experiments"] == "not_verified"
    why = {c["item"]: c["why"] for c in study.checklist(s, tmp_path)}
    assert why["experiments"] == "skipped at size quick"
    assert _verdicts(s)["inputs"] == "not_verified"


def test_plan_card_has_the_pieces():
    card = study.plan_card(_fresh())
    assert set(card) == {"question", "goals", "scope", "assumptions", "effort", "mode"}


def test_next_step_is_not_held_up_by_missing_data():
    step = study.next_step(_fresh(plan_approved=True, question="q", goals=[{"kpi": "k", "high": 1}],
                                  target=[{"name": "a", "blocks": ["S"]}], assumptions=[{"source": "x"}]))
    assert "study_step" in step and "inputs" not in step


def test_plan_card_comes_before_the_sufficient_report():
    s = _good(validation=[{"level": "data", "passed": True}])
    assert "plan card" in study.next_step(s)


def test_an_unknown_verdict_is_not_verified():
    s = _good()
    s["versions"][0]["verdict"] = "ok"
    assert _verdicts(s)["verification"] == "not_verified"


def test_validation_failed_and_partial_why_names_the_gap():
    assert _verdicts(_fresh(validation=[{"level": "data", "passed": False}]))["validation"] == "failed"
    s = _fresh(target=[{"name": "dock", "blocks": ["S"]}, {"name": "belt"}], assumptions=[{"source": "a"}, {}])
    why = {c["item"]: c["why"] for c in study.checklist(s)}
    assert "belt" in why["scope"] and "1 assumption" in why["assumptions"]


def test_goals_status_low_bound_and_text_only_and_conclusions_partial():
    s = _fresh(goals=[{"kpi": "k", "low": 5}, {"kpi": "m", "high": 1}, {"text": "be nice"}],
               versions=[{"id": "v1", "verdict": "passed", "kpis": {"k": 4.0, "m": 0.5}}])
    assert [g["status"] for g in study.goals_status(s)] == ["not_met", "met", "not_verified"]
    assert _verdicts(s)["conclusions"] == "partial"


def test_inputs_with_a_missing_file(tmp_path):
    (tmp_path / "inputs").mkdir()
    s = _fresh(sources=[{"input": "inputs/gone.csv"}])
    assert _verdicts(s, tmp_path)["inputs"] == "not_verified"


@pytest.mark.parametrize("key,val", [("mode", "fast"), ("size", "huge"), ("format", 0)])
def test_load_refuses_bad_mode_size_format(tmp_path, key, val):
    study.create(tmp_path / "s", "q")
    data = study.load(tmp_path / "s")
    data[key] = val
    study.save(tmp_path / "s", data)
    with pytest.raises(study.StudyError) as exc:
        study.load(tmp_path / "s")
    assert exc.value.code == "E_STUDY_UNREADABLE" and exc.value.hint


# --- recording a step ----------------------------------------------------------------------

def _layout(*names):
    return {"blocks": [{"name": n, "type": "Delay"} for n in names], "links": []}


def _digest(throughput=7.0):
    return {"system": {"Throughput": {"mean": throughput}}}


_PASSED = [{"name": "c", "verdict": "passed", "why": "", "kind": "law"}]
_FAILED = [{"name": "c", "verdict": "failed", "why": "", "kind": "assumption"}]


def _new(tmp_path):
    folder = tmp_path / "s"
    return folder, study.create(folder, "q")


def test_a_passing_step_is_version_one(tmp_path):
    folder, st = _new(tmp_path)
    out = study.record_step(folder, st, _layout("a"), _digest(), _PASSED, "first", None)
    assert out["id"] == "v001" and out["kind"] == "version" and out["verdict"] == "passed"
    assert out["kpis"] == {"Throughput": 7.0}
    assert json.loads((folder / "versions/v001/model.json").read_text()) == _layout("a")
    assert study.load(folder)["versions"][0] == {"id": "v001", "note": "first", "verdict": "passed",
                                                 "kpis": {"Throughput": 7.0}, "changes_count": 0}


def test_a_failed_step_is_a_draft_not_a_version(tmp_path):
    folder, st = _new(tmp_path)
    out = study.record_step(folder, st, _layout("a"), None, [], "bad", None,
                            problems=[{"code": "E_X", "message": "boom"}])
    assert out["kind"] == "draft" and out["id"] == "d001" and out["verdict"] == "failed"
    assert st["versions"] == []
    step = json.loads((folder / "versions/drafts/d001/step.json").read_text())
    assert step["problems"] == [{"code": "E_X", "message": "boom"}]


def test_the_same_request_id_records_once(tmp_path):
    folder, st = _new(tmp_path)
    a = study.record_step(folder, st, _layout("a"), _digest(), _PASSED, "n", "r1")
    b = study.record_step(folder, st, _layout("a", "b"), _digest(), _PASSED, "n", "r1")
    assert a == b
    assert [p.name for p in (folder / "versions").iterdir() if p.name != "drafts"] == ["v001"]
    assert len(st["versions"]) == 1


def test_changes_are_against_the_previous_version_not_drafts(tmp_path):
    folder, st = _new(tmp_path)
    study.record_step(folder, st, _layout("a", "b"), _digest(), _PASSED, "1", None)
    study.record_step(folder, st, _layout("a", "b", "junk"), None, [], "2", None)
    out = study.record_step(folder, st, _layout("a", "c"), _digest(), _PASSED, "3", None)
    assert out["id"] == "v002"
    assert sorted(out["changes"]) == ["added blocks.c", "removed blocks.b"]


def test_a_failed_check_makes_a_draft(tmp_path):
    folder, st = _new(tmp_path)
    assert study.record_step(folder, st, _layout("a"), _digest(), _FAILED, "n", None)["kind"] == "draft"


def test_revert_makes_a_new_version_from_an_old_one(tmp_path):
    folder, st = _new(tmp_path)
    study.record_step(folder, st, _layout("a"), _digest(1.0), _PASSED, "1", None)
    study.record_step(folder, st, _layout("b"), _digest(2.0), _PASSED, "2", None)
    out = study.revert(folder, st, "v001", "back")
    assert out["id"] == "v003" and out["kind"] == "version"
    assert json.loads((folder / "versions/v003/model.json").read_text()) == _layout("a")
    assert out["checks"] == _PASSED and out["kpis"] == {"Throughput": 1.0}
    assert sorted(out["changes"]) == ["added blocks.a", "removed blocks.b"]


def test_revert_to_an_unknown_version_is_a_coded_error(tmp_path):
    folder, st = _new(tmp_path)
    with pytest.raises(study.StudyError) as exc:
        study.revert(folder, st, "v009", "x")
    assert exc.value.code == "E_STUDY_NO_VERSION"


def test_revert_refuses_an_id_that_is_not_a_version_id(tmp_path):
    folder, st = _new(tmp_path)
    st["versions"].append({"id": "../outside"})     # a hand-edited study.json
    with pytest.raises(study.StudyError) as exc:
        study.revert(folder, st, "../outside", "x")
    assert exc.value.code == "E_STUDY_NO_VERSION"


def test_a_partial_step_is_a_version(tmp_path):
    folder, st = _new(tmp_path)
    warn = [{"name": "c", "verdict": "partial", "why": "", "kind": "law"}]
    out = study.record_step(folder, st, _layout("a"), _digest(), warn, "n", None)
    assert out["kind"] == "version" and out["verdict"] == "partial"


def test_the_change_list_is_capped_but_counted(tmp_path):
    folder, st = _new(tmp_path)
    study.record_step(folder, st, _layout("a"), _digest(), _PASSED, "1", None)
    out = study.record_step(folder, st, _layout(*[f"b{i}" for i in range(40)]), _digest(), _PASSED, "2", None)
    assert len(out["changes"]) == 30 and st["versions"][-1]["changes_count"] == 41


def test_a_draft_request_replays_the_same_answer(tmp_path):
    folder, st = _new(tmp_path)
    a = study.record_step(folder, st, _layout("a"), None, [], "n", "r1")
    b = study.record_step(folder, st, _layout("a"), None, [], "n", "r1")
    assert a == b and a["id"] == "d001" and not (folder / "versions/drafts/d002").exists()


def test_the_same_revert_request_records_once(tmp_path):
    folder, st = _new(tmp_path)
    study.record_step(folder, st, _layout("a"), _digest(), _PASSED, "1", None)
    study.record_step(folder, st, _layout("b"), _digest(), _PASSED, "2", None)
    a = study.revert(folder, st, "v001", "back", "r9")
    b = study.revert(folder, st, "v001", "back", "r9")
    assert a == b and len(st["versions"]) == 3 and not (folder / "versions/v004").exists()


def test_ids_are_not_reused_after_a_folder_is_removed(tmp_path):
    import shutil
    folder, st = _new(tmp_path)
    for n in "12":
        study.record_step(folder, st, _layout(n), _digest(), _PASSED, n, None)
    shutil.rmtree(folder / "versions/v001")
    assert study.record_step(folder, st, _layout("3"), _digest(), _PASSED, "3", None)["id"] == "v003"
