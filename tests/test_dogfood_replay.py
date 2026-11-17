"""The review's two business cases (S2, 2026-11-11) as recorded tool-call scripts, replayed against the
agent tools: the answer and the number of calls are checked (P10 gate). A live run with a model is
`-m live` only; these scripts are what an assistant needs once P10's fixes are in.

Call budgets only go down. The review's cold run took 15 calls (2 failed, 1 misleading, 1 dead end)
for the blood test, hand-building a RECORD for the total wait; its event-log case stopped at the
import (S2-03).
"""
import pytest

from nagasim import agent_tools as T

pytestmark = pytest.mark.slow

BLOOD_TEST_CALLS = 5
EVENT_LOG_CALLS = 3


def _replay(script, ws_root, fill=None):
    """Run each (tool, arguments) in order; `fill` puts earlier answers into later arguments.
    Every call must succeed: a call that fails would cost the assistant another."""
    T.use_workspace(T.Workspace(ws_root))
    answers = []
    for tool, args in script:
        args = fill(args, answers) if fill else args
        answer = T.call(tool, **args)
        assert "error" not in answer, (tool, answer)
        answers.append(answer)
    return answers


def _tri(low, mode, high):
    return {"distribution": "Triangular", "dist_params": {"low": low, "mode": mode, "high": high}}


def _blood_test_centre():
    """A walk-in blood test centre's day: about 125 patients (one every 3.8 minutes), one check-in
    desk (about 2 minutes), three nurses (about 10 minutes a draw), one sample in ten redrawn. The
    two options are scenarios: a fourth nurse, or a kiosk that halves check-in."""
    return {"schema_version": 30,
            "blocks": [
                {"name": "arrive", "type": "CREATE", "position": [0.0, 0.0],
                 "params": {"distribution": "Exponential", "dist_params": {"rate": 0.26}}},
                {"name": "check_in", "type": "PROCESS", "position": [200.0, 0.0],
                 "params": {**_tri(1.0, 2.0, 3.0), "resource_name": "Desk"}},
                {"name": "draw_blood", "type": "PROCESS", "position": [400.0, 0.0],
                 "params": {**_tri(6.0, 10.0, 14.0), "resource_name": "Nurse"}},
                {"name": "sample_ok", "type": "DECIDE", "position": [600.0, 0.0],
                 "params": {"probs": [0.9, 0.1]}},
                {"name": "leave", "type": "DISPOSE", "position": [800.0, 0.0]}],
            "links": [{"source": "arrive", "target": "check_in"},
                      {"source": "check_in", "target": "draw_blood"},
                      {"source": "draw_blood", "target": "sample_ok"},
                      {"source": "sample_ok", "target": "leave"},
                      {"source": "sample_ok", "target": "draw_blood"}],
            "resources": [{"name": "Desk", "capacity": 1}, {"name": "Nurse", "capacity": 3}],
            "simulation": {"time_limit": 480, "warmup": 0},
            "scenarios": [
                {"name": "Fourth nurse", "overrides": [{"path": "resources.Nurse.capacity", "value": 4}]},
                {"name": "Kiosk", "overrides": [{"path": "blocks.check_in.params.dist_params",
                                                 "value": {"low": 0.5, "mode": 1.0, "high": 1.5}}]}]}


def test_blood_test_case_within_its_call_budget(tmp_path):
    """The question to a recommendation and a deliverable: the complaint threshold is the wait over
    the whole visit (desk plus nurses, redraws included), a Total Wait tail goal (D1232) in place of
    the review's two ASSIGNs and a RECORD; the validation the assistant records reads "reported"
    (D1234), so the answer is the scenarios' goal verdicts."""
    sid = lambda answers: answers[0]["study"]       # noqa: E731
    goal = {"id": "g1", "text": "at most 10% of patients wait over 20 minutes in total",
            "kpi": "Total Wait", "share_above": 20, "high": 0.1}
    script = [
        ("study_start", {"question": "Fourth nurse or a check-in kiosk: which keeps the waits down?",
                         "size": "quick",
                         "changes": {"goals": [goal], "plan_approved": True,
                                     "assumptions": [{"id": "a1", "text": "check-in about 2 min",
                                                      "source": "centre manager"}]}}),
        ("study_step", {"note": "the day with both options as scenarios", "layout": _blood_test_centre()}),
        ("study_update", {"changes": {"validation": [{"level": "expert", "passed": True,
                                                      "text": "arrivals and draw times as the manager counts them"}],
                                      "decision": "Add a fourth nurse."}}),
        ("study_slides", {}),
        ("study_status", {}),
    ]
    answers = _replay(script, tmp_path,
                      lambda args, done: {**args, "study": sid(done)} if done else args)
    assert len(answers) == len(script) <= BLOOD_TEST_CALLS
    step, status = answers[1], answers[-1]
    assert "scenario_error" not in step and "tail_note" not in step, step
    assert step["page"]                     # the control room rendered (a failed render answers None)
    name = "Share with total wait over 20"
    assert step["kpis"][name] > 0.1                                  # today misses the goal
    assert step["scenarios"]["Fourth nurse"]["goals"]["g1"] == "met"
    # #1934, D1316: the Kiosk's day ends with patients inside (censoring), which only biases its waits
    # low; a goal already missed stays missed, and the step's check says what may bias it
    assert step["scenarios"]["Kiosk"]["goals"]["g1"] == "not_met"
    assert status["goals"][0]["status"] == "not_met"
    item = next(c for c in status["checklist"] if c["item"] == "validation")
    assert item["verdict"] == "not_verified" and "reported" in item["why"]


def _clinic_log(n=60):
    """The review's second case: a clinic's own export, its own headers, start and end on a row."""
    rows = ["Patient,Step,Start time,End time,Staff"]
    for i in range(n):
        t = 7 * i + (i * 37) % 5
        for step, staff, at, took in (("Check-in", "Desk", t, 2 + i % 2), ("Blood test", "Nurse", t + 3, 5 + i % 4)):
            stamp = lambda m: f"2026-01-05T{8 + m // 60:02d}:{m % 60:02d}:00"     # noqa: E731
            rows.append(f"p{i},{step},{stamp(at)},{stamp(at + took)},{staff}")
    return "\n".join(rows) + "\n"


def test_event_log_case_within_its_call_budget(tmp_path):
    """From the clinic's export to a draft that runs and is checked against the log: the headers are
    named once in `columns` (D1233), the same reading is used by check_draft."""
    (tmp_path / "clinic_visits.csv").write_text(_clinic_log(), encoding="utf-8")
    columns = {"case": "Patient", "activity": "Step", "resource": "Staff"}
    script = [
        ("import_event_log", {"path": "clinic_visits.csv", "name": "clinic", "columns": columns}),
        ("check_draft", {"model": "clinic", "source": "clinic_visits.csv", "max_seconds": 10}),
        ("run_model", {"model": "clinic", "replications": 5}),
    ]
    imported, checked, run = _replay(script, tmp_path)
    assert len(script) <= EVENT_LOG_CALLS
    assert imported["columns"] == {**columns, "start": "Start time", "complete": "End time"}
    assert {a["block"] for a in imported["activities"]} == {"Check_in", "Blood_test"}
    assert imported["observed"]["cases"] == 60
    assert checked["verdict"] in ("passed", "partial") and \
        {c["kpi"] for c in checked["compared"]} >= {"cycle_time", "throughput"}
    # The draft's day matches the log's to within a tenth (60 cases over about 7 hours).
    assert run["system"]["Throughput (per time unit)"]["mean"] == pytest.approx(imported["observed"]["throughput"], rel=0.1)
