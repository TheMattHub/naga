"""What next (D727, D728): ranked options from a study's recorded facts, the model-level options, the
assistant's routine and its checked ideas (nagasim/nextoptions.py, nagasim/nextideas.py)."""
import copy
import json
import shutil
import threading
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import assistant, llm, nextideas, nextoptions, studyroom
from nagasim import study as study_mod

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
LAYOUT = json.loads((EXAMPLES / "single_server.json").read_text(encoding="utf-8"))
GOAL = {"id": "g1", "text": "time under 5", "kpi": "Avg Throughput Time", "high": 5}
BIAS = {"name": "initial bias", "verdict": "partial", "kind": "law",
        "why": "the run starts empty with no warm-up: its first 120 time units move the mean time in "
               "system by 9%; set the warm-up to about 120"}


# ---------------------------------------------------------------- a hand-built study folder
def build(tmp_path, *, mean=4.0, h=0.2, target=0.25, n=10, checks=(), goals=(GOAL,), size="standard",
          validation=(), sources=(), layout=None, versions=1, mode="incremental") -> tuple[dict, Path]:
    """A study with `versions` recorded versions of single_server, written as study_step writes them,
    with no run: every fact the rules read is set by hand."""
    folder = tmp_path / "s"
    s = study_mod.create(folder, "Is one server enough?", mode=mode, size=size, save_now=False)
    s.update(goals=list(goals), validation=list(validation), sources=list(sources),
             question="Is one server enough?", plan_approved=True,
             target=[{"name": "Server", "blocks": ["Serve"]}],
             assumptions=[{"id": "a1", "text": "arrivals", "source": "the plant log"}])
    for i in range(versions):
        vid = f"v{i + 1:03d}"
        vdir = folder / "versions" / vid
        vdir.mkdir(parents=True)
        (vdir / "model.json").write_text(json.dumps(layout or LAYOUT), encoding="utf-8")
        kpis = {"Avg Throughput Time": mean}
        precision = {"Avg Throughput Time": {"half_width": h, "target": target, "met": h <= target, "mean": mean}}
        (vdir / "step.json").write_text(json.dumps({
            "id": vid, "verdict": "passed", "kpis": kpis, "replications": n, "seed": 0, "precision": precision,
            "checks": list(checks) + [{"name": "laws", "verdict": "passed", "why": "5 passed", "kind": "law"}],
            "at": f"2026-10-22T10:0{i}:00+00:00"}), encoding="utf-8")
        s["versions"].append({"id": vid, "note": "x", "verdict": "passed", "verification": "passed",
                              "kpis": kpis, "replications": n, "precision": precision})
    study_mod.save(folder, s)
    return s, folder


def opts(s, folder, **kw):
    return study_mod.next_options(s, folder, **kw)


def titles(options):
    return [o["title"] for o in options]


def has(options, tool, kind=None):
    return [o for o in options if o["tool"] == tool and (kind is None or o["kind"] == kind)]


def everything(s, folder):
    """All the options, not only the top five: the rules one at a time."""
    return nextoptions.for_study(s, folder, limit=50)


# ---------------------------------------------------------------- each rule fires, and stays silent
def test_a_failed_step_newer_than_the_last_version_is_a_fix(tmp_path):
    s, folder = build(tmp_path)
    draft = folder / "versions" / "drafts" / "d001"
    draft.mkdir(parents=True)
    (draft / "step.json").write_text(json.dumps({"at": "2026-10-22T11:00:00+00:00", "problems": [
        {"code": "E_RUN_FAILED", "message": "no route from Arrive"}]}), encoding="utf-8")
    first = opts(s, folder)[0]
    assert first["kind"] == "fix" and first["tool"] == "study_step" and "E_RUN_FAILED" in first["why"]
    (draft / "step.json").write_text(json.dumps({"at": "2026-10-22T09:00:00+00:00"}), encoding="utf-8")
    assert not [o for o in everything(s, folder) if o["kind"] == "fix"]


def test_checklist_gaps_are_missing_and_reuse_the_checklist(tmp_path):
    s, folder = build(tmp_path, versions=0)
    s["assumptions"] = []
    out = everything(s, folder)
    gaps = [o for o in out if o["kind"] == "missing"]
    assert [o["title"] for o in gaps] == ["Assumptions have sources", "A model version exists",
                                          "The model does what was intended", "The model matches reality",
                                          "Experiments were run", "Change the model or the goals"]
    assert "Checklist step 5 of 10" in gaps[0]["why"] and gaps[0]["needs"] == ["assumptions"]
    assert "Goals are measurable" not in titles(out)        # passed: silent


def test_a_failed_validation_is_a_fix(tmp_path):
    s, folder = build(tmp_path, validation=[{"level": "data", "passed": False, "text": "x"}])
    first = opts(s, folder)[0]
    assert first["kind"] == "fix" and "failed" in first["why"]


def test_a_goal_whose_interval_spans_its_bound_gets_a_precision_for_that_goal(tmp_path):
    s, folder = build(tmp_path, mean=4.9, h=0.3, target=0.4)
    o = has(everything(s, folder), "study_update", "improve")[0]
    assert o["title"] == "Settle goal g1 on Avg Throughput Time"
    assert "4.9 +-0.3 spans goal g1's bound 5" in o["why"]
    assert o["arguments"]["goals"][0]["precision"] == pytest.approx(0.09)
    assert "about 112 replications" in o["cost"]           # 10 x (0.3 / 0.09)^2
    s2, folder2 = build(tmp_path / "b", mean=4.0, h=0.3, target=0.4)
    assert not [x for x in everything(s2, folder2) if x["title"].startswith("Settle")]


def test_a_missed_precision_target_at_the_cap_offers_the_deep_size(tmp_path):
    s, folder = build(tmp_path, h=0.5, target=0.2, n=200)
    o = next(x for x in everything(s, folder) if x["title"].startswith("Narrow"))
    assert o["arguments"] == {"size": "deep"} and "+-0.5 against a precision target of +-0.2 after 200" in o["why"]
    s2, folder2 = build(tmp_path / "b", h=0.1, target=0.2)
    assert not [x for x in everything(s2, folder2) if x["title"].startswith(("Narrow", "Relax"))]


def test_sensitivity_not_run_fires_and_a_sensitivity_file_silences_it(tmp_path):
    s, folder = build(tmp_path)
    o = has(everything(s, folder), "study_sensitivity")[0]
    assert "about" in o["cost"] and "parameter(s)" in o["why"]
    (folder / "versions" / "v001" / "sensitivity.json").write_text("{}", encoding="utf-8")
    assert not has(everything(s, folder), "study_sensitivity")


def test_data_inputs_without_an_uncertainty_run(tmp_path):
    path = "blocks.Arrive.params.distribution"
    s, folder = build(tmp_path, sources=[{"id": "s1", "text": "gaps", "input": "inputs/gaps.csv", "path": path},
                                         {"id": "s2", "text": "notes", "input": "inputs/notes.txt"}])
    (folder / "inputs" / "gaps.csv").write_text("gap\n1\n2\n", encoding="utf-8")
    (folder / "inputs" / "notes.txt").write_text("x", encoding="utf-8")
    o = has(everything(s, folder), "input_uncertainty")[0]
    assert o["arguments"]["inputs"] == [{"path": path, "data": "inputs/gaps.csv"}] and "needs" not in o
    bound = has(nextoptions.for_study(s, folder, "studies/s", limit=50), "input_uncertainty")[0]
    assert bound["arguments"]["inputs"][0]["data"] == "studies/s/inputs/gaps.csv"
    assert bound["arguments"]["model"] == "study:studies/s"
    (folder / "versions" / "v001" / "asked.json").write_text(json.dumps([{"tool": "input_uncertainty"}]),
                                                             encoding="utf-8")
    assert not has(everything(s, folder), "input_uncertainty")


def test_a_data_source_without_its_model_field_says_what_it_needs(tmp_path):
    s, folder = build(tmp_path, sources=[{"id": "s1", "text": "gaps", "input": "inputs/gaps.csv"}])
    (folder / "inputs" / "gaps.csv").write_text("gap\n1\n", encoding="utf-8")
    o = has(everything(s, folder), "input_uncertainty")[0]
    assert o["needs"] == ["inputs[0].path (the model field source s1 feeds)"]


# C4 (D738): with a sensitivity, only the inputs whose path flips the answer are measured.
ARRIVE, SERVE = "blocks.create_1.params.distribution", "blocks.process_1.params.distribution"


def _sens(folder, flips, delta=0.2):
    rows = [{"path": p, "label": p.split(".")[1], "changes_answer": p in flips}
            for p in ("blocks.create_1.params.dist_params.rate", "blocks.process_1.params.dist_params.rate",
                      "resources.Server.capacity")]
    (folder / "versions" / "v001" / "sensitivity.json").write_text(json.dumps(
        {"version": "v001", "delta": delta, "factors": rows, "changes_answer": sorted(flips)}), encoding="utf-8")


def _two_inputs(tmp_path, extra=()):
    s, folder = build(tmp_path, sources=[{"id": "s1", "text": "gaps", "input": "inputs/gaps.csv", "path": ARRIVE},
                                         {"id": "s2", "text": "service", "input": "inputs/svc.csv", "path": SERVE},
                                         *extra])
    for name in ("gaps.csv", "svc.csv", "day.csv"):
        (folder / "inputs" / name).write_text("x\n1\n2\n", encoding="utf-8")
    return s, folder


def test_sensitivity_narrows_input_uncertainty_to_the_inputs_that_flip(tmp_path):
    s, folder = _two_inputs(tmp_path)
    assert len(has(everything(s, folder), "input_uncertainty")[0]["arguments"]["inputs"]) == 2   # no sensitivity
    _sens(folder, {"blocks.process_1.params.dist_params.rate", "resources.Server.capacity"})
    o = has(everything(s, folder), "input_uncertainty")[0]
    assert o["arguments"]["inputs"] == [{"path": SERVE, "data": "inputs/svc.csv"}]
    assert "1 of 2" in o["why"] and "one at a time" in o["why"]
    _schema_ok(nextoptions._bind(copy.deepcopy(o), "s"))


def test_no_input_flipping_says_so_and_does_not_offer_the_run(tmp_path):
    s, folder = _two_inputs(tmp_path)
    _sens(folder, {"resources.Server.capacity"})      # flips, but has no data: not an input
    out = everything(s, folder)
    assert not has(out, "input_uncertainty")
    o = has(out, "study_sensitivity")[0]
    assert "no input with data changes the answer" in o["why"].lower()
    assert o["arguments"] == {"delta": 0.4} and o["kind"] == "extend"
    _schema_ok(nextoptions._bind(copy.deepcopy(o), "s"))
    _sens(folder, set(), delta=0.5)                   # already wide: nothing more to offer
    assert not has(everything(s, folder), "input_uncertainty")
    assert not has(everything(s, folder), "study_sensitivity")


def test_an_input_the_sensitivity_did_not_vary_is_kept(tmp_path):
    day = {"id": "s3", "text": "arrivals", "input": "inputs/day.csv", "path": "schedules.day.points"}
    s, folder = _two_inputs(tmp_path, extra=[day])
    _sens(folder, set())
    o = has(everything(s, folder), "input_uncertainty")[0]
    assert o["arguments"]["inputs"] == [{"path": "schedules.day.points", "data": "inputs/day.csv"}]
    assert "not varied" in o["why"]


# C3: a verdict smaller than the input spread asks for the difference's own interval.
TT = "system/Avg Throughput Time"


def _level(spread=(3.0, 5.0), inputs=None):
    return {"tool": "input_uncertainty", "inputs": [SERVE], "measure": "level", "overrides": None,
            "input_args": inputs or [{"path": SERVE, "data": "inputs/svc.csv"}], "resamples_used": 20,
            "replications": 5, "kpis": [{"kpi": TT, "mean": 4.0, "input_interval": list(spread),
                                         "input_share": 0.6}]}


def _wi(diff, verdict="lower", overrides=None):
    return {"tool": "what_if", "overrides": overrides or [{"path": "resources.Server.capacity", "value": 2}],
            "kpis": [{"kpi": TT, "diff": diff, "diff_lb": diff - 0.1, "diff_ub": diff + 0.1, "verdict": verdict}]}


def test_a_verdict_smaller_than_the_input_spread_asks_for_the_difference(tmp_path):
    s, folder = build(tmp_path)
    asked = folder / "versions" / "v001" / "asked.json"
    asked.write_text(json.dumps([_level(), _wi(-0.4)]), encoding="utf-8")
    o = next(x for x in everything(s, folder) if x["arguments"].get("overrides") and x["tool"] == "input_uncertainty")
    assert o["kind"] == "improve" and o["arguments"]["inputs"] == [{"path": SERVE, "data": "inputs/svc.csv"}]
    assert o["arguments"]["overrides"] == [{"path": "resources.Server.capacity", "value": 2}]
    assert "0.4" in o["why"] and "+-1" in o["why"] and "level" in o["why"]
    _schema_ok(nextoptions._bind(copy.deepcopy(o), "s"))
    # Silent when the verdict is bigger than the spread, not decisive, or the difference is measured.
    for rows in ([_level(), _wi(-1.5)], [_level(), _wi(-0.4, "not distinguishable")],
                 [_level(), _wi(-0.4), dict(_level(), measure="difference",
                                            overrides=[{"path": "resources.Server.capacity", "value": 2}])]):
        asked.write_text(json.dumps(rows), encoding="utf-8")
        assert not [x for x in everything(s, folder) if x["arguments"].get("overrides")
                    and x["tool"] == "input_uncertainty"]


def test_the_spread_rule_holds_for_a_model_with_no_study(tmp_path):
    ws = T.use_workspace(T.Workspace(tmp_path))
    T.call("write_model", model="m", layout=LAYOUT)
    ws.asked["m"] = [_level(), _wi(-0.4)]
    out = nextoptions.for_model(LAYOUT, ws.asked["m"], "m", limit=10)
    o = has(out, "input_uncertainty")[0]
    assert o["arguments"]["model"] == "m" and o["arguments"]["overrides"][0]["value"] == 2
    _schema_ok(o)


def test_law_warnings_name_their_fix(tmp_path):
    load = {"name": "offered load: Server", "verdict": "partial", "kind": "law",
            "why": "offered load 1.20 >= 1 on Server: unstable by design"}
    s, folder = build(tmp_path, checks=[BIAS, load])
    out = everything(s, folder)
    bias = next(o for o in out if o["title"] == "Set a warm-up of about 120")
    assert bias["tool"] == "study_step" and "9%" in bias["why"]
    more = next(o for o in out if o["title"] == "Test one more unit of Server")
    assert more["arguments"]["overrides"] == [{"path": "resources.Server.capacity", "value": 2}]
    s2, folder2 = build(tmp_path / "b")
    assert not [o for o in everything(s2, folder2) if "warm-up" in o["title"] or "one more unit" in o["title"]]


def test_validation_only_against_a_formula(tmp_path):
    s, folder = build(tmp_path, validation=[{"level": "known_answer", "passed": True, "text": "M/M/1"}])
    assert "Validate against data or an expert" in titles(everything(s, folder))
    s2, folder2 = build(tmp_path / "b", validation=[{"level": "data", "passed": True, "text": "log"}])
    assert "Validate against data or an expert" not in titles(everything(s2, folder2))


def test_explain_the_goal_kpi_then_its_suggested_test(tmp_path):
    s, folder = build(tmp_path)
    o = has(everything(s, folder), "explain_kpi")[0]
    assert o["arguments"] == {"kpi": "system/Avg Throughput Time"} and "(4 on v001)" in o["why"]
    test = {"tool": "what_if", "text": "capacity of Server from 1 to 2",
            "arguments": {"overrides": [{"path": "resources.Server.capacity", "value": 2}],
                          "kpis": ["system/Avg Throughput Time"]}}
    asked = folder / "versions" / "v001" / "asked.json"
    asked.write_text(json.dumps([{"tool": "explain_kpi", "kpi": "system/Avg Throughput Time",
                                  "suggested_test": test}]), encoding="utf-8")
    out = everything(s, folder)
    assert not has(out, "explain_kpi")
    tried = next(o for o in out if o["title"] == "Try the test explain_kpi suggested")
    assert tried["arguments"] == test["arguments"] and "from 1 to 2" in tried["why"]
    rows = json.loads(asked.read_text(encoding="utf-8")) + [{"tool": "what_if",
                                                             "overrides": test["arguments"]["overrides"]}]
    asked.write_text(json.dumps(rows), encoding="utf-8")
    assert "Try the test explain_kpi suggested" not in titles(everything(s, folder))


def test_explain_names_follow_explain_kpi():
    assert nextoptions.explain_name({"kpi": "Avg WIP"}) == "system/Avg WIP"
    assert nextoptions.explain_name({"kpi": "Server/Usage % (snapshot)"}) == "resource/Server/Usage % (snapshot)"
    assert nextoptions.explain_name({"kpi": "Server/Time in Queue", "share_above": 5}) \
        == "resource/Server/Time in Queue Avg (incl. waiting)"
    assert nextoptions.explain_name({"kpi": "my stat"}) is None


def test_extensions_around_the_best_option_abc_and_the_deliverable(tmp_path):
    layout = copy.deepcopy(LAYOUT)
    layout["resources"][0]["capacity"] = 3
    s, folder = build(tmp_path, layout=layout, sources=[{"id": "s1", "text": "log", "input": "inputs/log.csv"}])
    out = everything(s, folder)
    near = next(o for o in out if o["title"] == "Test one unit less around the best option")
    assert near["arguments"]["overrides"] == [{"path": "resources.Server.capacity", "value": 2}]
    abc = has(out, "calibrate")[0]
    assert abc["arguments"] == {"method": "abc"} and abc["needs"][0].startswith("targets")
    assert "Build the deliverable" in titles(out)
    (folder / "package").mkdir()
    (folder / "package" / "model.json").write_text("{}", encoding="utf-8")
    assert "Rebuild the deliverable" in titles(everything(s, folder))
    shutil.copy(folder / "versions" / "v001" / "model.json", folder / "package" / "model.json")
    assert not has(everything(s, folder), "study_package")
    (folder / "versions" / "v001" / "asked.json").write_text(json.dumps([{"tool": "calibrate", "method": "abc"}]),
                                                             encoding="utf-8")
    assert not has(everything(s, folder), "calibrate")
    s2, folder2 = build(tmp_path / "b", mean=6.0)      # misses its goal and has no scenario: nothing nearby
    assert "Test one unit less around the best option" not in titles(everything(s2, folder2))


# ---------------------------------------------------------------- ranking, determinism, validity
def test_ranked_fix_missing_improve_extend_and_at_most_five(tmp_path):
    s, folder = build(tmp_path, checks=[BIAS], mean=4.9, h=0.3, target=0.4)
    s["experiments"] = []
    out = everything(s, folder)
    kinds = [o["kind"] for o in out]
    assert kinds == sorted(kinds, key=nextoptions.KINDS.index) and len(set(kinds)) >= 3
    improve = [o for o in out if o["kind"] == "improve"]
    assert improve[0]["title"].startswith("Settle")      # gap 0.3 / 0.1 = 3 before the rest
    assert len(opts(s, folder)) == 5


def test_the_same_folder_gives_the_same_options(tmp_path):
    s, folder = build(tmp_path, checks=[BIAS])
    a = opts(s, folder, study_id="x/s")
    b = study_mod.next_options(study_mod.load(folder), folder, study_id="x/s")
    assert json.dumps(a) == json.dumps(b)


def test_a_sufficient_study_still_gets_numbered_improvements_and_extensions(tmp_path):
    s, folder = build(tmp_path, validation=[{"level": "known_answer", "passed": True, "text": "M/M/1"}])
    s["experiments"] = [{"id": "e1", "text": "x", "result": "y"}]
    assert study_mod.sufficiency(s)["sufficient"]
    out = opts(s, folder)
    assert out and {o["kind"] for o in out} <= {"improve", "extend"}
    assert all(any(ch.isdigit() for ch in o["why"]) for o in out), out
    assert study_mod.next_step(s, folder) == "Report the result; offer extensions."     # unchanged


def _schema_ok(o):
    spec = T.TOOLS[o["tool"]].input_schema
    props, needs = spec["properties"], [n.split()[0].split("[")[0] for n in o.get("needs", [])]
    assert set(o["arguments"]) <= set(props), o
    assert set(spec["required"]) <= set(o["arguments"]) | set(needs), o


def test_every_option_names_a_tool_and_arguments_that_validate(tmp_path):
    folders = [build(tmp_path / "a", checks=[BIAS], mean=4.9, h=0.3, target=0.4,
                     sources=[{"id": "s1", "text": "x", "input": "inputs/a.csv"}],
                     validation=[{"level": "known_answer", "passed": True, "text": "f"}]),
               build(tmp_path / "b", versions=0), build(tmp_path / "c", h=0.5, target=0.1, n=200, size="deep")]
    (tmp_path / "a" / "s" / "inputs" / "a.csv").write_text("a\n1\n", encoding="utf-8")
    seen = set()
    for s, folder in folders:
        for o in nextoptions.for_study(s, folder, "w/s", limit=50):
            assert o["tool"] in T.TOOLS and o["source"] == "study numbers"
            assert set(o) >= {"kind", "title", "why", "tool", "arguments", "cost"}
            _schema_ok(o)
            seen.add(o["tool"])
    assert {"study_step", "study_update", "study_sensitivity", "explain_kpi", "input_uncertainty",
            "calibrate", "study_package"} <= seen


def test_the_study_answers_carry_the_top_three(tmp_path):
    T.use_workspace(T.Workspace(tmp_path))
    out = T.call("study_start", question="Is one server enough?", changes={"goals": [GOAL]})
    assert 1 <= len(out["options"]) <= 3 and out["options"][0]["arguments"]["study"] == out["study"]
    st = T.call("study_status", study=out["study"])
    assert st["options"] == study_mod.next_options(study_mod.load(tmp_path / out["study"]),
                                                   tmp_path / out["study"], out["study"], 3)


# ---------------------------------------------------------------- what was asked of a study's version
def test_explain_on_the_study_slot_is_kept_and_a_fresh_process_loads_the_slot(tmp_path):
    s, folder = build(tmp_path)
    T.use_workspace(T.Workspace(tmp_path))      # fresh: the slot is not loaded yet
    out = T.call("explain_kpi", model="study:s", kpi="system/Avg Throughput Time")
    assert "error" not in out, out
    rows = json.loads((folder / "versions" / "v001" / "asked.json").read_text(encoding="utf-8"))
    assert rows[0]["tool"] == "explain_kpi" and rows[0]["kpi"] == "system/Avg Throughput Time"
    assert not has(everything(study_mod.load(folder), folder), "explain_kpi")
    T.call("run_model", model="study:s", replications=2)       # a run is not kept on the version
    assert len(json.loads((folder / "versions" / "v001" / "asked.json").read_text(encoding="utf-8"))) == 1


def test_a_draft_in_the_slot_is_not_recorded_as_the_version(tmp_path):
    s, folder = build(tmp_path)
    ws = T.use_workspace(T.Workspace(tmp_path))
    changed = copy.deepcopy(LAYOUT)
    changed["resources"][0]["capacity"] = 2
    T.call("write_model", model="study:s", layout=changed)
    from nagasim.agent_tools.core import note_asked
    note_asked("study:s", {"tool": "what_if", "overrides": []})
    assert not (folder / "versions" / "v001" / "asked.json").exists() and ws.asked["study:s"]


# ---------------------------------------------------------------- a model with no study
def test_model_level_options(tmp_path):
    ws = T.use_workspace(T.Workspace(tmp_path))
    T.call("write_model", model="m", layout=LAYOUT)
    out = T.call("get_model_summary", model="m")["next_options"]
    assert titles(out) == ["Explain what makes up the time in system", "Compare an alternative as a scenario",
                           "Start a study around a decision"]
    assert out[1]["arguments"] == {"model": "m", "name": "Server +1",
                                   "overrides": [{"path": "resources.Server.capacity", "value": 2}]}
    for o in out:
        _schema_ok(o)
    ws.asked["m"] = [{"tool": "run_model", "warnings": [{"name": BIAS["name"], "why": BIAS["why"]}]},
                     {"tool": "explain_kpi", "kpi": "system/Avg Throughput Time"}]
    out = T.call("get_model_summary", model="m")["next_options"]
    assert out[0]["tool"] == "set_simulation_settings" and out[0]["arguments"] == {"model": "m", "warmup": 120.0}
    assert "Explain what makes up the time in system" not in titles(out)


# ---------------------------------------------------------------- the routine in both prompts
def test_both_prompts_carry_the_what_next_routine(tmp_path):
    text = assistant.system_prompt()
    assert "next_options" in text and '"the second"' in text and "Never run an option" in text
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Is one server enough?")["study"]
    room = studyroom.Room(tmp_path, threading.Lock())
    room.start()
    try:
        room.provider = Fake(_text("Run the sensitivity next?"))
        assert room.ask(sid, "What next?", voice=True) is not None
        wait(room, sid)
        system = room.provider.calls[0]["system"]
        assert '"options"' in system and "Never run an option" in system and '"[Voice mode:"' in system
        events = room.chats[sid].events
        assert [e["kind"] for e in events][:2] == ["user", "options"] and events[1]["items"]
        user = room.chats[sid].session.history[0]["text"]
        assert assistant.VOICE_NOTE in user and user.endswith("What next?")
    finally:
        room.stop()


# ---------------------------------------------------------------- layer 2: the assistant's own ideas
class Fake:
    kind = "hosted"

    def __init__(self, *replies, model=None):
        self.replies, self.calls = list(replies), []
        if model:
            self.cfg = llm.Config("anthropic", "", model, "sk-ant-x", "omit")

    def complete(self, system, tools, history, on_text=None, max_tokens=None):
        self.calls.append({"system": system, "tools": [t["name"] for t in tools], "max_tokens": max_tokens})
        item = self.replies.pop(0)
        if isinstance(item, threading.Event):
            item.wait(30)
            item = self.replies.pop(0)
        if on_text is not None and item.text:
            on_text(item.text)
        return item


def _text(text):
    return llm.Reply(text, (), "end", llm.Usage(1, 1))


def wait(room, sid):
    import time
    for _ in range(400):
        chat = room.chats[sid]
        if not chat.running and not chat.pending:
            return
        time.sleep(0.05)
    raise AssertionError("the turn did not end")


def test_ideas_only_for_an_allow_listed_hosted_model():
    assert nextideas.enabled(Fake(model=llm.DEFAULT_ANTHROPIC_MODEL), environ={})
    assert not nextideas.enabled(Fake(model="claude-haiku-5-5"), environ={})
    assert nextideas.enabled(Fake(model="claude-haiku-5-5"), environ={nextideas.ENV: "x, claude-haiku-5-5"})
    local = Fake(model=llm.DEFAULT_ANTHROPIC_MODEL)
    local.kind = "local"
    assert not nextideas.enabled(local, environ={nextideas.ENV: llm.DEFAULT_ANTHROPIC_MODEL})
    assert not nextideas.enabled(Fake(), environ={})      # no configuration at all


TOOLS = assistant.tool_defs(studyroom.StudyAdapter.tool_names())


def test_ideas_are_checked_dropped_and_labelled():
    good = {"title": "Try two servers", "why": "Usage is 80%.", "tool": "what_if",
            "arguments": {"overrides": [{"path": "resources.Server.capacity", "value": 2}]}}
    bad = [{"title": "Edit", "why": "x", "tool": "write_model", "arguments": {}},           # not offered
           {"title": "Bad path", "why": "x", "tool": "what_if",
            "arguments": {"overrides": [{"path": "resources.Nope.capacity", "value": 2}]}},
           {"title": "Missing", "why": "x", "tool": "explain_kpi", "arguments": {}},          # kpi required
           {"title": "Too many", "why": "x", "tool": "study_report", "arguments": {}}]
    provider = Fake(_text("Here: " + json.dumps({"ideas": [good] + bad})))
    out = nextideas.ideas(provider, {"question": "q"}, [], TOOLS, study_id="w/s", layout=LAYOUT)
    assert out["ideas"] == [{**good, "source": "assistant"}]
    assert out["dropped"] == 3 + 1 and "4 idea(s) dropped" in out["note"]       # 3 failed, 1 over the cap
    assert provider.calls[0]["tools"] == [] and provider.calls[0]["max_tokens"] == nextideas.MAX_TOKENS
    assert "what_if" in provider.calls[0]["system"] and "write_model" not in provider.calls[0]["system"]


def test_ideas_never_block_past_their_budget_and_survive_junk():
    gate = threading.Event()
    late = nextideas.ideas(Fake(gate, _text("{}")), {}, [], TOOLS, budget_s=0.2)
    gate.set()
    assert late == {"ideas": [], "dropped": 0, "note": "no ideas within 0.2 s"}
    junk = nextideas.ideas(Fake(_text("no JSON at all")), {}, [], TOOLS)
    assert junk["ideas"] == [] and "not the JSON" in junk["note"]


def test_the_room_shows_ideas_after_the_turn_and_never_runs_them(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Is one server enough?")["study"]
    room = studyroom.Room(tmp_path, threading.Lock())
    room.start()
    try:
        gate, asked = threading.Event(), threading.Event()
        idea = {"title": "Report now", "why": "0 versions.", "tool": "study_report", "arguments": {}}

        class Split(Fake):
            """The turn (offered tools) waits for the gate; the ideas call (no tools) answers at once."""
            def complete(self, system, tools, history, on_text=None, max_tokens=None):
                if not tools:
                    asked.set()
                    return _text(json.dumps({"ideas": [idea]}))
                gate.wait(30)
                return _text("Shall I record the goals?")
        room.provider = Split(model=llm.DEFAULT_ANTHROPIC_MODEL)
        room.ask(sid, "what next?")
        assert asked.wait(30)
        assert "ideas" not in [e["kind"] for e in room.chats[sid].events]     # held until the turn ends
        gate.set()
        wait(room, sid)
        kinds = [e["kind"] for e in room.chats[sid].events]
        assert kinds.index("ideas") > kinds.index("done") and kinds.index("options") == 1
        shown = room.chats[sid].events[kinds.index("ideas")]["items"]
        assert shown == [{**idea, "arguments": {"study": sid}, "source": "assistant"}]
        usage = json.loads((tmp_path / sid / "study.json").read_text(encoding="utf-8"))["usage"]
        assert [u["tool"] for u in usage] == ["study_start"]          # the idea did not run
    finally:
        room.stop()
