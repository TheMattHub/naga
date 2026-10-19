"""The study input normaliser: each mapping, the refusals, the canonical pass-through."""
import pytest

from nagasim.study import StudyError
from nagasim.studyinput import normalise, normalise_layout


def one(key, rec):
    out, notes = normalise({key: rec})
    return out[key], notes


@pytest.mark.parametrize("rec", [
    {"range": [2, 3]}, {"min": 2, "max": 3}, {"target_low": 2, "target_high": 3}])
def test_goal_bounds(rec):
    got, notes = one("goals", [{"id": "g1", "kpi": "k", **rec}])
    assert got == [{"id": "g1", "kpi": "k", "low": 2, "high": 3}]
    assert notes and all(n.startswith("goals[g1]") for n in notes)


def test_text_synonyms_and_validation_aliases():
    got, notes = one("goals", [{"id": "g1", "statement": "fast"}])
    assert got == [{"id": "g1", "text": "fast"}] and notes == ["goals[g1]: statement -> text"]
    got, _ = one("validation", [{"kind": "expert", "result": "failed"}, {"type": "data", "ok": True}])
    assert got == [{"level": "expert", "passed": False}, {"level": "data", "passed": True}]


def test_dict_to_list_and_target_strings():
    got, notes = one("goals", {"id": "g1", "kpi": "k"})
    assert got == [{"id": "g1", "kpi": "k"}] and notes
    got, _ = one("target", ["Packer", "Belt"])
    assert got == [{"name": "Packer"}, {"name": "Belt"}]


def test_conflict_refused_with_both_readings():
    with pytest.raises(StudyError) as e:
        normalise({"goals": [{"id": "g1", "low": 1, "range": [2, 3]}]})
    assert e.value.code == "E_STUDY_BAD_VALUE" and '"low": 2' in e.value.hint
    with pytest.raises(StudyError):
        normalise({"validation": [{"level": "data", "passed": True, "ok": False}]})
    with pytest.raises(StudyError):
        normalise({"goals": [{"id": "g", "text": "a", "statement": "b"}]})


def test_typo_suggests_with_corrected_example():
    with pytest.raises(StudyError) as e:
        normalise({"goals": [{"id": "g1", "kpii": "k"}]})
    assert "kpi" in str(e.value) and '"kpi": "k"' in e.value.hint
    with pytest.raises(StudyError) as e:
        normalise({"assumtions": []})
    assert "assumptions" in str(e.value)


def test_canonical_passes_unchanged():
    ch = {"question": "q", "plan_approved": True, "goals": [{"id": "g1", "kpi": "k", "low": 1, "high": 2}],
          "target": [{"name": "A", "blocks": ["a"]}], "validation": [{"level": "data", "passed": True}],
          "mystery": 1}
    out, notes = normalise(ch)
    assert out == ch and notes == []


def test_scenario_overrides():
    lay = {"scenarios": [{"name": "s", "overrides": {"resources.P.capacity": 2}}]}
    out, notes = normalise_layout(lay)
    assert out["scenarios"][0]["overrides"] == [{"path": "resources.P.capacity", "value": 2}] and notes
    assert lay["scenarios"][0]["overrides"] == {"resources.P.capacity": 2}   # input not mutated
    good = {"scenarios": [{"name": "s", "overrides": [{"path": "a", "value": 1}]}], "x": 1}
    assert normalise_layout(good) == (good, [])
    with pytest.raises(StudyError):
        normalise_layout({"scenarios": [{"name": "s", "overrides": [{"pth": "a", "value": 1}]}]})
