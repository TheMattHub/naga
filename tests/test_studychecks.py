from nagasim.studychecks import overall, step_checks


def _d(created=100, completed=100, usage=50.0, **extra):
    d = {"replications": 3,
         "system": {"Total Entities Created": {"mean": created, "ci_half_width": 0},
                    "Total Entities Completed": {"mean": completed, "ci_half_width": 0}},
         "resources": {"Server": {"Usage % (snapshot)": {"mean": usage, "ci_half_width": 0}}},
         "user_stats": {}}
    d.update(extra)
    return d


def _by(checks, name):
    return next(c for c in checks if c["name"] == name)


def test_utilisation_over_100_is_flagged_not_failed():
    c = _by(step_checks({}, _d(usage=101.0), None, {}), "utilisation: Server")
    assert c["verdict"] == "partial" and c["kind"] == "law"


def test_saturated_is_partial_and_normal_passes():
    assert _by(step_checks({}, _d(usage=97), None, {}), "utilisation: Server")["verdict"] == "partial"
    assert _by(step_checks({}, _d(usage=60), None, {}), "utilisation: Server")["verdict"] == "passed"


def test_piling_up_work_is_a_partial():
    c = _by(step_checks({}, _d(created=100, completed=50), None, {}), "flow balance")
    assert c["verdict"] == "partial" and "piling up" in c["why"]
    assert _by(step_checks({}, _d(), None, {}), "flow balance")["verdict"] == "passed"


def test_laws_without_their_kpis_are_omitted():
    assert step_checks({}, {"replications": 1, "levels": {"x": {}}}, None, {}) == []


def test_a_different_rerun_fails_reproducibility():
    same = step_checks({}, _d(), _d(), {})
    assert _by(same, "reproducibility")["verdict"] == "passed"
    bad = step_checks({}, _d(), _d(completed=99), {})
    assert _by(bad, "reproducibility")["verdict"] == "failed"
    assert all(c["name"] != "reproducibility" for c in step_checks({}, _d(), None, {}))


def test_assumption_outside_its_range_fails():
    study = {"assumptions": [
        {"id": "a1", "text": "t", "source": "s", "check": {"kpi": "Total Entities Created", "low": 0, "high": 50}},
        {"id": "a2", "text": "t", "source": "s", "check": {"kpi": "Server/Usage % (snapshot)", "low": 0, "high": 90}},
        {"id": "a3", "text": "t", "source": "s"}],
        "goals": [{"id": "g1", "text": "t", "kpi": "Total Entities Completed", "low": 90, "high": 110}]}
    cs = step_checks({}, _d(), None, study)
    assert _by(cs, "assumption a1")["verdict"] == "failed"
    assert _by(cs, "assumption a2")["verdict"] == "passed"
    assert all(c["name"] != "assumption a3" for c in cs)
    g = _by(cs, "goal g1")
    assert g["verdict"] == "passed" and g["kind"] == "goal"


def test_unknown_kpi_is_not_verified():
    study = {"goals": [{"id": "g1", "text": "t", "kpi": "Nope", "low": 0, "high": 1}]}
    c = _by(step_checks({}, _d(), None, study), "goal g1")
    assert c["verdict"] == "not_verified" and "Nope" in c["why"]


def test_overall_ignores_law_partials_for_failed():
    p = {"name": "x", "verdict": "partial", "why": "", "kind": "law"}
    ok = {"name": "y", "verdict": "passed", "why": "", "kind": "goal"}
    bad = {"name": "z", "verdict": "failed", "why": "", "kind": "assumption"}
    assert overall([p, ok]) == "partial"
    assert overall([p, ok, bad]) == "failed"
    assert overall([ok]) == "passed"
    assert overall([]) == "not_verified"
    nv = {"name": "n", "verdict": "not_verified", "why": "", "kind": "goal"}
    assert overall([ok, nv]) == "partial"


def test_overall_unmet_goal_is_partial_not_failed():
    g = {"name": "goal g", "verdict": "failed", "why": "", "kind": "goal"}
    a = {"name": "assumption a", "verdict": "failed", "why": "", "kind": "assumption"}
    assert overall([g]) == "partial" and overall([g, a]) == "failed"


def test_precision_check_never_fails_and_is_partial_when_unmet():
    from nagasim.studychecks import precision_check
    met = {"k": {"half_width": 0.4, "target": 0.5, "met": True}}
    miss = {"k": {"half_width": 0.9, "target": 0.5, "met": False}}
    assert precision_check(met, 20, 200)["verdict"] == "passed"
    c = precision_check(miss, 200, 200)
    assert c["verdict"] == "partial" and c["kind"] == "precision" and "0.9" in c["why"]
    assert overall([c]) == "partial"
