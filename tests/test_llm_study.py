"""The LLM case study (`benchmarks/llm_study/`): every task is solvable in both arms and graded
right, the harness records and resumes, and the published table is the committed results
file's. No model is called: the local comparison talks to a scripted endpoint, the Claude one
to a scripted stand-in for the command."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from nagasim import assistant
from nagasim.core.document import ModelDocument
from test_llm import reply, serve

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "benchmarks" / "llm_study"

_spec = importlib.util.spec_from_file_location("llm_study", STUDY / "study.py")
study = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(study)
TASKS = study.load_tasks()


def _replay(task):
    """The task's reference tool calls through the loop's own adapter, offered what the study
    offers: (the document, the results)."""
    document = ModelDocument()
    adapter = assistant.WorkspaceAdapter(document)
    adapter.offer(study.study_tools())
    return document, [adapter.run_tool(name, dict(arguments)) for name, arguments in task.module.NAGA]


# ---------------------------------------------------------------- tasks and graders
def test_the_task_list_is_the_folder():
    assert {path.stem for path in (STUDY / "tasks").glob("*.py")} == set(study.TASK_IDS)
    assert all(task.tolerance and task.tolerance < 0.25 * abs(task.answer) for task in TASKS.values())


@pytest.mark.parametrize("task", TASKS.values(), ids=lambda task: task.id)
def test_a_tasks_tool_solution_is_inside_its_tolerance(task):
    """Solvable with the tools the study offers, and the answer read off the results is right."""
    _document, results = _replay(task)
    assert not [result for result in results if "error" in result]
    assert results[0]["ok"], results[0]["problems"]      # write_model: the whole file, valid
    assert abs(task.module.naga_answer(results) - task.answer) <= task.tolerance


@pytest.mark.parametrize("task", TASKS.values(), ids=lambda task: task.id)
def test_a_tasks_simpy_solution_is_graded_correct_and_a_wrong_one_is_not(task, tmp_path):
    """The task file is its own SimPy solution; the same file made to print another number is
    runnable and not correct, one tolerance and a bit off either way."""
    grade, told = study.check_script(STUDY / "tasks" / f"{task.id}.py", task, 60)
    assert (grade["valid"], grade["runnable"], grade["correct"], told) == (True, True, True, ""), grade
    for off in (-1.05, 1.05):
        wrong = tmp_path / "solution.py"
        wrong.write_text(f"import simpy\nprint('ANSWER: {task.answer + off * task.tolerance}')\n", encoding="utf-8")
        grade, _told = study.check_script(wrong, task, 60)
        assert (grade["valid"], grade["runnable"], grade["correct"]) == (True, True, False), grade


@pytest.mark.parametrize("task", TASKS.values(), ids=lambda task: task.id)
def test_a_tasks_builder_solution_is_graded_correct(task, tmp_path):
    script = tmp_path / "solution.py"
    script.write_text(task.module.BUILDER, encoding="utf-8")
    grade, told = study.check_script(script, task, 120, "builder")
    assert (grade["valid"], grade["runnable"], grade["correct"], told) == (True, True, True, ""), grade


def test_the_measured_tolerances_cover_the_statistical_tasks_without_bias():
    measured = json.loads((STUDY / "calibration.json").read_text(encoding="utf-8"))
    assert set(measured["tasks"]) == {t.id for t in TASKS.values() if t.module.TOLERANCE is None}
    assert measured["seeds"] == study.SEEDS
    for name, entry in measured["tasks"].items():       # the reference's own mean sits on theory
        assert abs(entry["mean"] - TASKS[name].answer) <= entry["sd"], name


@pytest.mark.slow
def test_the_calibration_file_is_the_calibrations():
    assert study.calibrate() == json.loads((STUDY / "calibration.json").read_text(encoding="utf-8"))


def test_reading_an_answer():
    assert study.read_answer("I ran it.\nANSWER: 12.5 minutes") == 12.5
    assert study.read_answer("ANSWER: 1\n**ANSWER: 1,234.5**\nthanks") == 1234.5
    assert study.read_answer("about 12") is None and study.read_answer("ANSWER: none") is None
    assert study.read_answer("ANSWER: 3\ndone", last_line_only=True) is None
    assert study.read_answer("x\nANSWER: -2e-1\n\n", last_line_only=True) == -0.2


def test_a_script_that_is_not_a_simpy_script_is_not_valid(tmp_path):
    task = TASKS["mm1"]
    for source, note in (("print('ANSWER: 12'", "syntax error"), ("print('ANSWER: 12')", "does not import simpy"),
                         ("import simpy, nagasim\nprint('ANSWER: 12')", "imports another simulation library")):
        (tmp_path / "solution.py").write_text(source, encoding="utf-8")
        grade, told = study.check_script(tmp_path / "solution.py", task, 60)
        assert not grade["valid"] and grade["note"] == note and told
    for source, note in (("import nagasim\nprint('ANSWER: 12')", "does not import nagasim.build"),
                         ("import simpy\nfrom nagasim import build as nb\nprint('ANSWER: 12')",
                          "imports another simulation library")):
        (tmp_path / "solution.py").write_text(source, encoding="utf-8")
        grade, told = study.check_script(tmp_path / "solution.py", task, 60, "builder")
        assert not grade["valid"] and grade["note"] == note and "nagasim import build" in told
    grade, _told = study.check_script(tmp_path / "missing.py", task, 60)
    assert grade["note"] == "no script"
    (tmp_path / "solution.py").write_text("import simpy, time\ntime.sleep(30)\n", encoding="utf-8")
    grade, told = study.check_script(tmp_path / "solution.py", task, 1)
    assert (grade["valid"], grade["runnable"], grade["note"]) == (True, False, "timeout") and "1 seconds" in told


def test_a_model_file_is_graded_on_the_file_the_run_and_the_answer(tmp_path):
    task = TASKS["mm1"]
    document, _results = _replay(task)
    document.save(tmp_path / "model.json")
    right, wrong = f"Done.\nANSWER: {task.answer}", f"ANSWER: {task.answer + 2 * task.tolerance}"
    graded = lambda text, ran: study.grade_model(tmp_path / "model.json", text, task, ran, 120)
    assert graded(right, True) == {"valid": True, "runnable": True, "correct": True,
                                   "answer": task.answer, "note": ""}
    assert (graded(wrong, True)["runnable"], graded(wrong, True)["correct"]) == (True, False)
    assert graded(right, False)["note"] == "never ran the model" and not graded(right, False)["correct"]
    assert graded("it is twelve", True)["note"] == "no answer line"
    ModelDocument().save(tmp_path / "model.json")           # what a failed turn leaves
    empty = graded(right, True)
    assert (empty["valid"], empty["runnable"], empty["correct"]) == (False, False, False) and empty["note"]
    assert study.grade_model(tmp_path / "none.json", right, task, True, 120)["note"] == "no model file"


# ---------------------------------------------------------------- the local harness
CRASH = "```python\nimport simpy\nraise ValueError('boom')\n```"


def _local(url, out, *more):
    return study.main(["local", "--models", "fake:1b", "--base-url", url, "--tasks", "mm1",
                       "--rounds", "1", "--out", str(out), *more])


def _rows(out):
    return {(r["arm"], r["repeat"]): r for r in study.collect(out)["attempts"]}


def test_the_local_harness_grades_both_arms_and_resumes(tmp_path, capsys):
    task = TASKS["mm1"]
    calls = [(f"c{i}", name, json.dumps(arguments)) for i, (name, arguments) in enumerate(task.module.NAGA)]
    script = "```python\n" + (STUDY / "tasks" / "mm1.py").read_text(encoding="utf-8") + "```"
    built = "```python\n" + task.module.BUILDER + "```"
    first = [reply(calls=calls), reply(f"Built and run.\nANSWER: {task.answer}"),       # tools: correct
             reply(built),                                                              # builder: correct
             reply(CRASH), reply("Fixed.\n" + script)]                                  # simpy: fixed after the error
    with serve(first) as (url, seen):
        assert _local(url, tmp_path) == 0
    assert len(seen) == 5
    assert "Build a model in Python" in seen[2]["body"]["messages"][0]["content"] and "tools" not in seen[2]["body"]
    assert [tool["function"]["name"] for tool in seen[0]["body"]["tools"]] == study.study_tools()
    assert seen[0]["body"]["messages"][-1]["content"].endswith(task.request + study.TOOLS_ASK)
    assert "tools" not in seen[3]["body"] and seen[3]["body"]["messages"][-1]["content"] == task.request
    assert "ValueError: boom" in seen[4]["body"]["messages"][-1]["content"]     # the traceback went back
    rows = _rows(tmp_path)
    for arm, turns, tool_calls in (("whole", 2, len(calls)), ("builder", 1, 1), ("simpy", 2, 2)):
        row = rows[arm, 1]
        assert (row["valid"], row["runnable"], row["correct"]) == (True, True, True), row
        assert (row["turns"], row["tool_calls"], row["budget"], row["model"]) == (turns, tool_calls, 2, "fake:1b")
    transcript = json.loads((tmp_path / "local" / "fake_1b" / "mm1.whole.1.transcript.json").read_text(encoding="utf-8"))
    assert transcript["tool_calls"][-1]["name"] == "run_model" and len(transcript["replies"]) == 2

    # A second repeat: an answer that is off, and a script that never stops crashing. The
    # first repeat is on file and is not asked for again.
    second = [reply(calls=calls), reply("ANSWER: 99"), reply(built), reply(CRASH), reply(CRASH)]
    with serve(second) as (url, seen):
        assert _local(url, tmp_path, "--repeats", "2") == 0
    assert len(seen) == 5
    rows = _rows(tmp_path)
    assert (rows["whole", 2]["valid"], rows["whole", 2]["runnable"], rows["whole", 2]["correct"]) == (True, True, False)
    assert (rows["simpy", 2]["valid"], rows["simpy", 2]["runnable"], rows["simpy", 2]["correct"]) == (True, False, False)
    assert rows["simpy", 2]["note"] == "reply limit; exit 1" and rows["whole", 1]["correct"]

    with serve([]) as (url, seen):                          # everything is on file
        assert _local(url, tmp_path, "--repeats", "2") == 0
    assert not seen
    capsys.readouterr()

    shown = study.render(study.collect(tmp_path))            # a pilot is counted, never tabulated
    assert "(6 pilot attempts are on file" in shown and "fake:1b" not in shown
    assert "| `mm1` | One clerk, one queue (M/M/1): the mean time a customer spends in the shop | 12 | 2.1 |" in shown
    assert not [line for line in json.dumps(study.collect(tmp_path)).split('"') if str(Path.home()) in line]


def test_a_turn_the_loop_undoes_leaves_no_model_and_an_unreachable_server_stops_the_run(tmp_path, capsys):
    task = TASKS["mm1"]
    calls = [(f"c{i}", name, json.dumps(arguments)) for i, (name, arguments) in enumerate(task.module.NAGA)]
    with serve([reply(calls=calls), reply("")]) as (url, _seen):        # an empty reply fails the turn
        assert _local(url, tmp_path, "--arms", "whole") == 0
    row = _rows(tmp_path)["whole", 1]
    assert not row["valid"] and row["note"].startswith("turn undone (LLMError)")
    with serve([]) as (url, _seen):
        pass                                                           # the server is gone
    with pytest.raises(SystemExit, match="nothing written"):
        _local(url, tmp_path, "--arms", "simpy")
    assert ("simpy", 1) not in _rows(tmp_path)
    capsys.readouterr()


# ---------------------------------------------------------------- the Claude Code harness
STAND_IN = r'''
import json, os, shutil, sys
from pathlib import Path
args, ask = sys.argv[1:], sys.stdin.read()
value = lambda flag: args[args.index(flag) + 1]
tools_arm = value("--tools") == ""
log = {"args": args, "ask": ask, "cwd_empty": not os.listdir("."),
       "settings": json.loads(Path(value("--settings")).read_text()),
       "servers": json.loads(Path(value("--mcp-config")).read_text()),
       "consent": os.environ.get("NAGASIM_ALLOW_AGENT_PYTHON"), "key": os.environ.get("ANTHROPIC_API_KEY"),
       "python": shutil.which("python"),
       "pythonpath": os.environ.get("PYTHONPATH")}
Path(os.environ["STAND_IN_LOG"]).write_text(json.dumps(log))
say = lambda **event: print(json.dumps(event))
say(type="system", subtype="init", model="claude-stand-in-1")
if os.environ["STAND_IN_MODE"] == "quota":
    say(type="result", subtype="success", is_error=True, result="usage limit reached")
    sys.exit(1)
shutil.copy(os.environ["STAND_IN_MODEL" if tools_arm else "STAND_IN_SCRIPT"],
            "model.json" if tools_arm else "solution.py")
say(type="assistant", message={"content": [{"type": "text", "text": "working"},
    {"type": "tool_use", "name": "mcp__naga__run_model" if tools_arm else "Bash", "input": {}}]})
say(type="result", subtype="success", is_error=False, num_turns=3, usage={"input_tokens": 7, "output_tokens": 9},
    result="Done.\nANSWER: " + os.environ["STAND_IN_ANSWER"])
'''


def test_the_claude_harness_isolates_grades_and_stops_on_a_quota_error(tmp_path, monkeypatch, capsys):
    task = TASKS["mm1"]
    document, _results = _replay(task)
    document.save(tmp_path / "reference.json")
    (tmp_path / "stand_in.py").write_text(STAND_IN, encoding="utf-8")
    monkeypatch.setattr(study, "CLAUDE", [sys.executable, str(tmp_path / "stand_in.py")])
    for name, value in {"STAND_IN_LOG": tmp_path / "log.json", "STAND_IN_MODEL": tmp_path / "reference.json",
                        "STAND_IN_SCRIPT": STUDY / "tasks" / "mm1.py", "STAND_IN_ANSWER": task.answer,
                        "STAND_IN_MODE": "ok", "NAGASIM_ALLOW_AGENT_PYTHON": "1",
                        "ANTHROPIC_API_KEY": "sk-not-used"}.items():
        monkeypatch.setenv(name, str(value))
    out = tmp_path / "attempts"
    run = lambda *more: study.main(["claude", "--tasks", "mm1", "--out", str(out), *more])

    assert run("--arms", "whole") == 0
    log = json.loads((tmp_path / "log.json").read_text(encoding="utf-8"))
    assert log["ask"] == task.request + study.CLAUDE_TOOLS_ASK and "Python" not in study.CLAUDE_TOOLS_ASK
    assert log["cwd_empty"] and log["consent"] is None and log["key"] is None
    assert "--dangerously-skip-permissions" not in log["args"] and "--allow-dangerously-skip-permissions" not in log["args"]
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in log["args"]
    value = lambda flag: log["args"][log["args"].index(flag) + 1]
    assert (value("--model"), value("--max-turns"), value("--permission-prompts")) == ("haiku", "50", "none")
    assert log["settings"] == {"permissions": {"allow": ["mcp__naga"]}}
    server = log["servers"]["mcpServers"]["naga"]
    assert server["args"][:4] == ["-m", "nagasim", "mcp", "--root"] and "--allow-network" not in server["args"]

    assert run("--arms", "simpy") == 0
    log = json.loads((tmp_path / "log.json").read_text(encoding="utf-8"))
    assert log["ask"] == task.request + study.CLAUDE_SIMPY_ASK and log["servers"] == {"mcpServers": {}}
    assert log["settings"]["permissions"]["allow"] == ["Read", "Write", "Edit", "Bash(python solution.py)"]
    assert log["args"][log["args"].index("--tools") + 1] == "Bash,Write,Edit,Read"
    assert Path(log["python"]).parent == Path(sys.executable).parent and log["pythonpath"] is None

    builder = tmp_path / "builder.py"
    builder.write_text(task.module.BUILDER, encoding="utf-8")
    monkeypatch.setenv("STAND_IN_SCRIPT", str(builder))
    assert run("--arms", "builder") == 0
    log = json.loads((tmp_path / "log.json").read_text(encoding="utf-8"))
    assert log["ask"] == task.request + study.CLAUDE_BUILDER_ASK.format(reference=study.builder_reference())
    assert log["settings"]["permissions"]["allow"] == ["Read", "Write", "Edit", "Bash(python solution.py)"]
    assert log["servers"] == {"mcpServers": {}} and log["pythonpath"] == str(ROOT)

    rows = _rows(out)
    for arm in ("whole", "builder", "simpy"):
        row = rows[arm, 1]
        assert (row["valid"], row["runnable"], row["correct"]) == (True, True, True), row
        assert (row["turns"], row["tool_calls"], row["model_id"], row["budget"]) == (3, 1, "claude-stand-in-1", 50)

    monkeypatch.setenv("STAND_IN_ANSWER", "99")              # off: runnable, not correct
    assert run("--arms", "whole", "--repeats", "2") == 0
    row = _rows(out)["whole", 2]
    assert (row["valid"], row["runnable"], row["correct"]) == (True, True, False)

    monkeypatch.setenv("STAND_IN_MODE", "quota")             # no result file: the run can resume
    with pytest.raises(SystemExit, match="usage limit reached"):
        run("--arms", "whole", "--repeats", "3")
    assert ("whole", 3) not in _rows(out)
    capsys.readouterr()


# ---------------------------------------------------------------- the published table
def _full(model, comparison="local", repeats=study.FULL_REPEATS):
    return [{"comparison": comparison, "model": model, "task": task, "arm": arm, "repeat": repeat,
             "valid": True, "runnable": arm in study.TOOL_ARMS or repeat > 1, "correct": arm in study.TOOL_ARMS and task != "tank",
             "turns": 9, "tool_calls": 12, "seconds": 90.0, "budget": 25, "date": f"2026-01-0{repeat}",
             "versions": {"nagasim": "0", "simpy": "0", "python": "3"}}
            for task in study.TASK_IDS for arm in study.ARMS for repeat in range(1, repeats + 1)]


def test_a_model_that_reached_every_task_is_tabulated_and_one_stopped_short_says_so():
    tasks = study.collect(STUDY / "no-such-folder")["tasks"]
    one = study.render({"tasks": tasks, "attempts": _full("m:1")})
    assert "from 2026-01-01 to 2026-01-03: 12 tasks, 3 attempts per task and arm, at most 25 model replies" in one
    assert "| `m:1` | Naga tools, whole model | 36 | 36/36 (100%) | 36/36 (100%) | 33/36 (92%) | 9 | 12 |  |  |  | 1.5 |" in one
    assert "| `m:1` | SimPy code | 36 | 36/36 (100%) | 24/36 (67%) | 0/36 (0%) | 9 | 12 |  |  |  | 1.5 |" in one
    assert "stopped after" not in one
    # a model stopped after one round is shown with its own counts and said to be short;
    # one that has not reached every task is a pilot and is not shown
    pilot = [row for row in _full("m:3", repeats=1) if row["task"] != "tank"]
    before = [row for row in _full("m:2", repeats=1) if row["arm"] in ("tools", "simpy")]   # before D396
    shown = study.render({"tasks": tasks, "attempts": _full("m:1") + before + pilot})
    assert "12 tasks, 1 to 3 attempts per task and arm" in shown
    assert "`m:2` was stopped after 24 of the 72 planned attempts (3 per task and arm)" in shown
    assert "| `m:2` | Naga tools, block by block | 12 | 12/12 (100%) |" in shown and "| `tank`: " in shown
    assert "m:3" not in shown and "| `m:2` | Naga tools, whole model" not in shown
    assert shown.count("The full runs have not been made yet.") == 1        # the Claude comparison


def test_the_published_table_is_the_results_files():
    results = json.loads((STUDY / "results.json").read_text(encoding="utf-8"))
    assert (STUDY / "results.md").read_text(encoding="utf-8") == study.render(results)
    assert results["tasks"] == json.loads(json.dumps(study.collect(STUDY / "no-such-folder")["tasks"]))


def test_the_export_carries_every_file_of_the_study():
    if not (ROOT / "tools" / "export_engine.py").is_file():
        pytest.skip("an engine checkout: there is no export to check")
    sys.path.insert(0, str(ROOT))
    listed = {name.removeprefix("benchmarks/llm_study/")
              for name in importlib.import_module("tools.export_engine").EXTRA_FILES
              if name.startswith("benchmarks/llm_study/")}
    files = {path.relative_to(STUDY).as_posix() for path in STUDY.rglob("*")
             if path.is_file() and not any(p.startswith("attempts") for p in path.parts) and "__pycache__" not in path.parts}
    assert files == listed
