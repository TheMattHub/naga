"""A case study: does a language model get a simulation right more often through Naga's tools
than by writing SimPy code?

    python benchmarks/llm_study/study.py local --models gemma4:12b      # a local model, both arms
    python benchmarks/llm_study/study.py claude                         # Claude Code, both arms
    python benchmarks/llm_study/study.py report                         # results.json, results.md
    python benchmarks/llm_study/study.py calibrate                      # calibration.json

The tasks are in `tasks/`: one file each, with the request as a practitioner would type it, the
answer and how it is derived, and a reference solution for each arm (the tool calls in `NAGA`,
and the file itself as the SimPy script).

Three arms, the same request, the same number of model replies allowed:

* `whole`: the model builds, validates and runs the model through Naga's tools and says the
  answer. Locally this is the engine's own loop (`nagasim.assistant`); for Claude Code it is the
  `naga mcp` server and nothing else.
* `simpy`: the model writes a Python script that uses SimPy. The harness runs it and, locally,
  sends back what it printed when it failed; Claude Code runs it itself.
* `builder`: as `simpy`, the script built and run with Naga's Python builder (`nagasim.build`),
  its documentation given with the request (`builder_reference`).

Three outcomes per attempt, each implying the one before:

* valid: the model file passes validation / the script parses and imports SimPy (the builder);
* runnable: it runs to the end within the time limit;
* correct: the answer given, a last line `ANSWER: <number>`, is within the task's tolerance (and,
  in the tools arm, the model was run through the tools at least once).

An attempt is one file under `attempts/` (not committed), written when it ends: a run can be
stopped at any time and started again, and it skips what is done. `report` gathers them.

The two comparisons are not comparable with each other: the local one measures a model inside
one loop, the Claude one a whole coding agent.
"""
import argparse
import ast
import importlib.util
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TASK_IDS = ("mm1", "mmc", "md1", "tandem", "rework", "priority", "batch", "carpark", "shift",
            "cooling", "repair", "tank")
ARMS = {"tools": "Naga tools, block by block", "whole": "Naga tools, whole model",
        "builder": "Naga builder code", "simpy": "SimPy code"}
# "tools" is the record of the tools before write_model (D396): the current tools are not what
# it measured, so it is reported but never run again; "whole" is the tools arm since.
RUNNABLE_ARMS = ("whole", "builder", "simpy")
TOOL_ARMS = {"tools", "whole"}
# What a code arm's script must import, and the simulation libraries it may not lean on.
LIBRARY = {"simpy": "simpy", "builder": "nagasim.build"}
SIGMAS, SEEDS = 5, 20           # a statistical tolerance: SIGMAS standard deviations over SEEDS runs
FULL_REPEATS = 3                # planned attempts per task and arm; a run stopped short says so
RUN_TOOLS = {"run_model", "run_to_precision", "run_scenarios"}
OTHER_LIBRARIES = {"nagasim", "simpy", "salabim", "ciw"}
CLAUDE = None                   # the command that starts Claude Code; a test puts a script here

# The requests end the same way in both arms; only the means differ.
TOOLS_ASK = ("\n\nBuild the model with the tools, run it, and end your reply with a line of the "
             "form `ANSWER: <number>`.")
SIMPY_SYSTEM = (
    "You are a simulation engineer. Answer with ONE complete Python script in a single "
    "```python block. The script uses SimPy (`import simpy`) and the standard library, takes no "
    "input, finishes within {timeout} seconds, and its last printed line is `ANSWER: <number>`. "
    "It is run for you: if it fails you are sent what it printed, and you answer with the whole "
    "corrected script.")
CLAUDE_TOOLS_ASK = ("\n\nUse the simulation tools you have been given. Save the finished model as "
                    "model.json, and end your reply with a line of the form `ANSWER: <number>`.")
CLAUDE_SIMPY_ASK = ("\n\nSolve this with a Python script that uses SimPy (it is installed). Save "
                    "it as solution.py; its last printed line must have the form "
                    "`ANSWER: <number>`. You can run it with `python solution.py`.")
# The builder arm: the SimPy arm's words, the builder for SimPy, and its documentation after them.
BUILDER_SYSTEM = (
    "You are a simulation engineer. Answer with ONE complete Python script in a single "
    "```python block. The script builds the model with Naga's Python builder (`from nagasim "
    "import build as nb`), runs it with `m.run()`, takes no input, finishes within {timeout} "
    "seconds, and its last printed line is `ANSWER: <number>`. It is run for you: if it fails "
    "you are sent what it printed, and you answer with the whole corrected script.\n\n{reference}")
CLAUDE_BUILDER_ASK = (
    "\n\nSolve this with a Python script that builds the model with Naga's Python builder "
    "(`from nagasim import build as nb`, it is installed) and runs it with `m.run()`. Save it "
    "as solution.py; its last printed line must have the form `ANSWER: <number>`. You can run "
    "it with `python solution.py`. The builder's documentation follows.\n\n{reference}")


def builder_reference() -> str:
    """What the builder arm is given, the counterpart of the tools arm's get_model_format: the
    Python API page's sections on running a model and on the builder, as a user reads them,
    and the model file's description the tools arm reads (the builder's keywords are the
    file's keys)."""
    from nagasim.agent_tools.wholemodel import get_model_format

    # The page sits under public/ here, at the root of docs/ in the engine's own repository.
    page = next(path for path in (ROOT / "packages" / "engine" / "public" / "docs" / "python-api.md",
                                  ROOT / "docs" / "python-api.md") if path.is_file())
    sections = re.split(r"(?m)^## ", page.read_text(encoding="utf-8"))
    kept = ["## " + s.strip() for s in sections if s.startswith(("Run a model", "Build a model in Python"))]
    return "\n\n".join(kept + ["## The model file the builder writes", get_model_format()["format"]])


# ---------------------------------------------------------------- tasks
def load_tasks(ids=None) -> dict:
    """{id: task} in the order of TASK_IDS; a task has id, title, request, answer, tolerance and
    its module (NAGA, naga_answer, simulate)."""
    calibration = HERE / "calibration.json"
    measured = json.loads(calibration.read_text(encoding="utf-8"))["tasks"] if calibration.is_file() else {}
    tasks = {}
    for name in ids or TASK_IDS:
        if name not in TASK_IDS:
            raise SystemExit(f"unknown task '{name}': one of {', '.join(TASK_IDS)}")
        spec = importlib.util.spec_from_file_location(f"llm_study_task_{name}", HERE / "tasks" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tolerance = module.TOLERANCE
        if tolerance is None and name in measured:
            tolerance = SIGMAS * measured[name]["sd"]
        tasks[name] = SimpleNamespace(id=name, title=module.__doc__.split("\n")[0].rstrip("."),
                                      request=module.REQUEST, answer=module.ANSWER,
                                      tolerance=tolerance, module=module)
    return tasks


def calibrate() -> dict:
    """The spread of each statistical task's SimPy reference over independent seeds, at the
    run length its request asks for."""
    measured = {}
    for task in load_tasks().values():
        if task.module.TOLERANCE is None:
            values = [task.module.simulate(seed) for seed in range(1, SEEDS + 1)]
            measured[task.id] = {"mean": round(statistics.mean(values), 6),
                                 "sd": round(statistics.stdev(values), 6)}
    return {"seeds": SEEDS, "tasks": measured}


# ---------------------------------------------------------------- grading
_NUMBER = re.compile(r"[-+]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def read_answer(text: str, last_line_only: bool = False):
    """The number after the last `ANSWER:`, or None. A script must print it on its last line."""
    lines = [line for line in text.splitlines() if line.strip()]
    lines = lines[-1:] if last_line_only else [line for line in lines if "ANSWER:" in line]
    if not lines or "ANSWER:" not in lines[-1]:
        return None
    found = _NUMBER.search(lines[-1].rsplit("ANSWER:", 1)[1])
    return float(found.group().replace(",", "")) if found else None


def _within(answer, task) -> bool:
    return answer is not None and abs(answer - task.answer) <= task.tolerance


def _run(command, *, cwd, timeout, stdin="", env=None):
    """(exit code or None on a timeout, stdout, stderr). A timeout kills the children too: a
    script may have started some, and Claude Code starts the tool server."""
    # No consent to run a model's Python, no API key (Claude Code then uses the subscription it
    # is logged in with), and no path to this checkout unless the caller gives one.
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("NAGASIM_ALLOW_AGENT_PYTHON", "PYTHONPATH", "ANTHROPIC_API_KEY")}
    process = subprocess.Popen(command, cwd=cwd, env={**child_env, **(env or {})}, text=True,
                               encoding="utf-8", errors="replace", stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(stdin, timeout=timeout)
        return process.returncode, out, err
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        process.kill()
        out, err = process.communicate()
        return None, out, err


def check_script(path: Path, task, timeout: float, arm: str = "simpy"):
    """(grade, what to tell the author) for a code arm's script; the second is empty once the
    script runs and prints an answer."""
    grade = {"valid": False, "runnable": False, "correct": False, "answer": None, "note": ""}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        grade["note"] = "no script" if isinstance(exc, OSError) else "syntax error"
        return grade, f"The script could not be read as Python: {exc}"
    # Dotted names, so that `from nagasim import build` is `nagasim.build`.
    imported = {name for node in ast.walk(tree)
                for name in ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                             else [node.module or "", *(f"{node.module}.{a.name}" for a in node.names)]
                             if isinstance(node, ast.ImportFrom) else [])}
    library = LIBRARY[arm]
    others = {name.split(".")[0] for name in imported} & (OTHER_LIBRARIES - {library.split(".")[0]})
    if library not in imported or others:
        grade["note"] = f"does not import {library}" if library not in imported else "imports another simulation library"
        return grade, ("The script must use SimPy (`import simpy`) and no other simulation library."
                       if arm == "simpy" else "The script must build the model with "
                       "`from nagasim import build as nb` and use no other simulation library.")
    grade["valid"] = True
    # The builder arm runs this checkout's engine, as the tools arm's server does.
    code, out, err = _run([sys.executable, path.name], cwd=path.parent, timeout=timeout,
                          env={"PYTHONPATH": str(ROOT)} if arm == "builder" else None)
    if code != 0:
        grade["note"] = "timeout" if code is None else f"exit {code}"
        ended = f"did not finish within {timeout:g} seconds" if code is None else f"failed (exit code {code})"
        return grade, f"The script {ended}. Its output ended with:\n{(out + err)[-2000:]}"
    grade["runnable"] = True
    grade["answer"] = read_answer(out, last_line_only=True)
    if grade["answer"] is None:
        grade["note"] = "no answer line"
        return grade, ("The script ran, but its last printed line was not `ANSWER: <number>`. "
                       f"Its output ended with:\n{out[-500:]}")
    grade["correct"] = _within(grade["answer"], task)
    return grade, ""


def check_model(path: Path) -> dict:
    """Validate and run a model file as the tools do, a model's own Python never run."""
    from nagasim import agent_tools

    agent_tools.drop_python_consent()
    agent_tools.use_workspace(agent_tools.Workspace(path.parent))
    opened = agent_tools.call("open_model", path=path.name)
    if "error" in opened:
        return {"valid": False, "codes": [opened["error"]["code"]]}
    # A model with nothing in it validates and runs (a model may be stocks or agents alone),
    # and it is what a turn the loop undid leaves behind: it is not a model of the task.
    layout = json.loads(path.read_text(encoding="utf-8"))
    if not any(layout.get(section) for section in ("blocks", "levels", "populations")):
        return {"valid": False, "codes": ["empty model"]}
    checked = agent_tools.call("validate_model", model=opened["model"])
    if not checked["ok"]:
        return {"valid": False, "codes": sorted({p["code"] for p in checked["problems"]
                                                 if not p["code"].startswith("W_")})}
    ran = agent_tools.call("run_model", model=opened["model"])
    return {"valid": True, "ran": "error" not in ran,
            "codes": [ran["error"]["code"]] if "error" in ran else []}


def grade_model(path: Path, reply: str, task, ran_model: bool, timeout: float) -> dict:
    """The tools arm: the saved model file, and the answer the reply ends with."""
    grade = {"valid": False, "runnable": False, "correct": False, "answer": read_answer(reply), "note": ""}
    if not path.is_file():
        return {**grade, "note": "no model file"}
    code, out, _err = _run([sys.executable, str(Path(__file__).resolve()), "check-model", path.name],
                           cwd=path.parent, timeout=timeout, env={"PYTHONPATH": str(ROOT)})
    try:
        checked = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {**grade, "note": "timeout" if code is None else "the model could not be checked"}
    grade["valid"] = checked["valid"]
    grade["runnable"] = bool(checked.get("ran"))
    grade["correct"] = grade["runnable"] and ran_model and _within(grade["answer"], task)
    grade["note"] = (", ".join(checked["codes"]) or ("no answer line" if grade["answer"] is None else
                                                    "" if ran_model else "never ran the model"))
    return grade


# ---------------------------------------------------------------- the local comparison
def study_tools(_kind: str = "local", _extra: tuple = ()) -> list:
    """What the tools arm offers: the engine's compact list for a local model (D396: the
    whole model file in one write_model, which takes every section and the step grid)."""
    from nagasim import assistant

    return sorted(assistant.offered_tools("local"))


class Recorder:
    """A provider that keeps every reply for the transcript. `unreachable` is set when the
    endpoint itself could not be reached: that is the machine's failure, not the model's."""

    def __init__(self, provider):
        self.provider, self.kind, self.replies, self.unreachable = provider, provider.kind, [], None

    def complete(self, system, tools, history, on_text=None):
        from nagasim import llm

        started = time.perf_counter()
        try:
            reply = self.provider.complete(system, tools, history, on_text)
        except llm.LLMError as exc:
            if str(exc).startswith("cannot reach"):     # the wording of nagasim.llm
                self.unreachable = exc
            self.replies.append({"error": str(exc)})
            raise
        self.replies.append({"seconds": round(time.perf_counter() - started, 1), "text": reply.text,
                             "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in reply.tool_calls],
                             "stop": reply.stop, "tokens": [reply.usage.input_tokens, reply.usage.output_tokens]})
        return reply


def local_tools(task, provider, options, work: Path):
    from nagasim import assistant
    from nagasim.core.document import ModelDocument

    document = ModelDocument()
    adapter = assistant.WorkspaceAdapter(document)
    session = assistant.AssistantSession(adapter, provider, select_tools=study_tools,
                                         max_tool_rounds=options.rounds)
    try:
        turn = session.run_turn(task.request + TOOLS_ASK)
    finally:
        shutil.rmtree(adapter.workspace.root, ignore_errors=True)   # the untitled model's folder
    document.save(work / "model.json")
    ran = any(call.name in RUN_TOOLS and "error" not in call.result for call in turn.tool_calls)
    grade = grade_model(work / "model.json", turn.text, task, ran, options.timeout)
    if turn.error or turn.hit_round_cap:    # a failed turn is undone by the loop: nothing is left
        grade["note"] = "; ".join(filter(None, [
            f"turn undone ({turn.error.split(':')[0]})" if turn.error else "reply limit", grade["note"]]))
    transcript = {"system": session.system, "request": task.request + TOOLS_ASK,
                  "tools": study_tools(), "error": turn.error, "reply": turn.text,
                  "tool_calls": [{"name": c.name, "input": c.input, "result": c.result} for c in turn.tool_calls],
                  "model_file": json.loads((work / "model.json").read_text(encoding="utf-8"))}
    return grade, len(turn.tool_calls), transcript


def local_code(task, provider, options, work: Path, arm: str):
    from nagasim import llm

    system = (SIMPY_SYSTEM if arm == "simpy" else BUILDER_SYSTEM).format(
        timeout=f"{options.timeout:g}", reference=builder_reference() if arm == "builder" else "")
    history, runs, script = [{"role": "user", "text": task.request}], 0, work / "solution.py"
    grade = {"valid": False, "runnable": False, "correct": False, "answer": None, "note": "no script"}
    error = None
    for _ in range(options.rounds + 1):         # as many replies as the tools arm may take
        try:
            reply = provider.complete(system, [], history)
        except llm.LLMError as exc:
            error = str(exc)
            break
        history.append({"role": "assistant", "text": reply.text, "tool_calls": []})
        blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", reply.text, re.S)
        if not blocks:
            told = "Answer with the complete script in one ```python block."
        else:
            script.write_text(blocks[-1], encoding="utf-8")
            runs += 1
            grade, told = check_script(script, task, options.timeout, arm)
        if not told:
            break
        history.append({"role": "user", "text": told})
    else:
        grade["note"] = "; ".join(filter(None, ["reply limit", grade["note"]]))
    return grade, runs, {"system": system, "error": error, "history": history}


def local_attempt(task, arm, options):
    from nagasim import llm

    config = llm.resolve_config(provider="openai", base_url=options.base_url, model=options.model,
                                reasoning=options.reasoning, environ={})
    provider = Recorder(llm.build_provider(config))
    with tempfile.TemporaryDirectory(prefix="naga_llm_study_", ignore_cleanup_errors=True) as work:
        grade, calls, transcript = (local_tools(task, provider, options, Path(work)) if arm in TOOL_ARMS
                                    else local_code(task, provider, options, Path(work), arm))
    if provider.unreachable:
        raise SystemExit(f"stopped, nothing written for this attempt: {provider.unreachable}")
    tokens = [reply["tokens"] for reply in provider.replies if "tokens" in reply]
    return ({**grade, "turns": len(provider.replies), "tool_calls": calls, "budget": options.rounds + 1,
             "tokens_in": sum(t[0] for t in tokens), "tokens_out": sum(t[1] for t in tokens),
             "largest_prompt": max((t[0] for t in tokens), default=0)},
            {**transcript, "replies": provider.replies})


# ---------------------------------------------------------------- the Claude Code comparison
def claude_command(arm: str, options, settings: Path, servers: Path) -> list:
    """Claude Code, headless, with only what a run in a throwaway folder needs. `--restricted`
    ignores this machine's own settings, keeps the file tools inside the folder and refuses to
    skip permissions; whatever is not allowed below is refused, since nobody answers a prompt."""
    tools = "" if arm in TOOL_ARMS else "Bash,Write,Edit,Read"    # the built-in tools that exist at all
    return [*(CLAUDE or [shutil.which("claude") or "claude"]), "-p", "--model", options.model,
            "--max-turns", str(options.max_turns), "--output-format", "stream-json", "--verbose",
            "--restricted", "--strict-mcp-config", "--mcp-config", str(servers),
            "--settings", str(settings), "--permission-prompts", "none",
            "--no-session-persistence", "--disable-slash-commands", "--tools", tools]


def claude_attempt(task, arm, options):
    with tempfile.TemporaryDirectory(prefix="naga_llm_study_", ignore_cleanup_errors=True) as folder:
        # The long path: under Windows' short form of it (NAME~1) Claude Code does not see a
        # file tool's target as inside its folder, and refuses every write.
        folder = Path(folder).resolve()
        work = folder / "work"              # empty; the two configuration files stay outside it
        work.mkdir()
        servers, settings = folder / "mcp.json", folder / "settings.json"
        naga = {"naga": {"command": sys.executable, "args": ["-m", "nagasim", "mcp", "--root", str(work),
                                                          "--tools", "compact"],
                         "env": {"PYTHONPATH": str(ROOT)}}}
        servers.write_text(json.dumps({"mcpServers": naga if arm in TOOL_ARMS else {}}), encoding="utf-8")
        allow = ["mcp__naga"] if arm in TOOL_ARMS else ["Read", "Write", "Edit", "Bash(python solution.py)"]
        settings.write_text(json.dumps({"permissions": {"allow": allow}}), encoding="utf-8")
        # `python` is this interpreter, which has SimPy; the builder arm's is this checkout's engine.
        env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
               **({"PYTHONPATH": str(ROOT)} if arm == "builder" else {})}
        ask = task.request + (CLAUDE_TOOLS_ASK if arm in TOOL_ARMS else CLAUDE_SIMPY_ASK if arm == "simpy"
                              else CLAUDE_BUILDER_ASK.format(reference=builder_reference()))
        code, out, err = _run(claude_command(arm, options, settings, servers), cwd=work,
                              timeout=options.attempt_timeout, stdin=ask, env=env)
        events = []
        for line in out.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        events = [event for event in events if isinstance(event, dict)]
        result = next((e for e in reversed(events) if e.get("type") == "result"), {})
        finished = result.get("subtype") == "error_max_turns" or (
            result.get("subtype") == "success" and not result.get("is_error"))
        if code is not None and not finished:   # not logged in, the quota is used up, a refused flag
            raise SystemExit("stopped, nothing written for this attempt: Claude Code did not finish it: "
                             + (str(result.get("result") or "") or err or out)[-600:])
        used = [block for event in events if event.get("type") == "assistant"
                for block in (event.get("message") or {}).get("content") or []
                if isinstance(block, dict) and block.get("type") == "tool_use"]
        reply = str(result.get("result") or "")
        if arm in TOOL_ARMS:
            ran = any(str(block.get("name", "")).rsplit("__", 1)[-1] in RUN_TOOLS for block in used)
            grade = grade_model(work / "model.json", reply, task, ran, options.timeout)
        else:
            grade, _told = check_script(work / "solution.py", task, options.timeout, arm)
        limit = "timeout" if code is None else "turn limit" if result.get("subtype") == "error_max_turns" else ""
        grade["note"] = "; ".join(filter(None, [limit, grade["note"]]))
        started = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        usage = result.get("usage") or {}
        return ({**grade, "turns": result.get("num_turns"), "tool_calls": len(used),
                 "budget": options.max_turns, "model_id": started.get("model"),
                 "tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens"),
                 "tokens_cached": usage.get("cache_read_input_tokens"),
                 "cost_usd": result.get("total_cost_usd")},     # at list price: what the quota is spent on
                {"request": ask, "command": claude_command(arm, options, Path("settings.json"), Path("mcp.json")),
                 "allow": allow, "events": events, "stderr": err[-2000:]})


# ---------------------------------------------------------------- running and reporting
def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "_", model).strip("_")


def run_attempts(comparison: str, attempt, options) -> int:
    """Every (repeat, task, arm) not on file yet, one result file each."""
    import nagasim
    import simpy

    tasks = load_tasks(options.tasks)
    folder = options.out / comparison / _slug(options.model)
    folder.mkdir(parents=True, exist_ok=True)
    for repeat in range(1, options.repeats + 1):
        for task in tasks.values():
            for arm in options.arms:
                target = folder / f"{task.id}.{arm}.{repeat}.json"
                if target.is_file():
                    continue
                started = time.perf_counter()
                grade, transcript = attempt(task, arm, options)
                row = {"comparison": comparison, "model": options.model, "task": task.id, "arm": arm,
                       "repeat": repeat, **grade, "expected": task.answer, "tolerance": task.tolerance,
                       "seconds": round(time.perf_counter() - started, 1), "date": date.today().isoformat(),
                       "versions": {"nagasim": nagasim.__version__, "simpy": simpy.__version__,
                                    "python": platform.python_version()}}
                (folder / f"{task.id}.{arm}.{repeat}.transcript.json").write_text(
                    json.dumps(transcript, indent=1, default=str), encoding="utf-8")
                partial = target.with_suffix(".part")       # a stop while writing leaves no result
                partial.write_text(json.dumps(row, indent=1) + "\n", encoding="utf-8")
                partial.replace(target)
                outcome = next((name for name in ("correct", "runnable", "valid") if row[name]), "not valid")
                sys.stdout.write(f"{options.model}  {task.id:9} {arm:6} {repeat}  {outcome:9} "
                                 f"{row['seconds']:7.0f} s  {row['turns']} replies, {row['tool_calls']} calls"
                                 f"{'  (' + row['note'] + ')' if row['note'] else ''}\n")
                sys.stdout.flush()
    return 0


def collect(out: Path) -> dict:
    """Everything `report` writes, from the attempt files and the task files."""
    attempts = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(out.rglob("*.json"))
                if not path.name.endswith(".transcript.json")]
    order = {name: index for index, name in enumerate(TASK_IDS)}
    attempts.sort(key=lambda r: (r["comparison"], r["model"], order[r["task"]], r["arm"], r["repeat"]))
    return {"tasks": {task.id: {"title": task.title, "answer": task.answer, "tolerance": task.tolerance}
                      for task in load_tasks().values()},
            "attempts": attempts}


def _share(rows, outcome) -> str:
    hits = sum(bool(row[outcome]) for row in rows)
    return f"{hits}/{len(rows)} ({100 * hits / len(rows):.0f}%)"


def _median(rows, field, scale=1.0) -> str:
    values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
    return f"{statistics.median(values) / scale:.3g}" if values else ""


def _span(shown, tasks) -> str:
    counts = [sum(r["task"] == task and r["arm"] == arm for r in mine)
              for arms, mine in shown.values() for task in tasks for arm in arms]
    low, high = min(counts), max(counts)
    return f"{low} attempt{'s' if low > 1 else ''}" if low == high else f"{low} to {high} attempts"


# Input tokens are every token the model read, cached or not: what a long conversation costs.
HEADER = ("| Model | Arm | Attempts | Valid | Runnable | Correct | Median replies | Median tool calls | "
          "Median input tokens (k) | Median output tokens (k) | Median cost (USD) | Median minutes |")


def render(results: dict) -> str:
    """The Markdown the docs page includes, from a results file's content alone. An arm of a
    model is shown once every task has at least one attempt in it, and a model once its code
    arm and a tools arm are, each cell with its own count; a model with fewer than
    FULL_REPEATS in some cell is said to have been stopped short. Attempts in an arm or a
    model not shown are a pilot: counted, never tabulated."""
    tasks = results["tasks"]
    parts = ["### The tasks", "", "| Task | What is asked | Answer | Accepted within |", "|---|---|---|---|"]
    parts += [f"| `{name}` | {entry['title']} | {entry['answer']:.4g} | {entry['tolerance']:.2g} |"
              for name, entry in tasks.items()] + [""]
    titles = {"local": "Local models, in the engine's own tool loop",
              "claude": "Claude Code, the whole coding agent"}
    for comparison, title in titles.items():
        rows = [row for row in results["attempts"] if row["comparison"] == comparison]
        parts += [f"### {title}", ""]
        shown, pilots = {}, 0
        for model in dict.fromkeys(row["model"] for row in rows):
            mine = [row for row in rows if row["model"] == model]
            arms = [arm for arm in ARMS if all(any(r["task"] == task and r["arm"] == arm for r in mine)
                                               for task in tasks)]
            if "simpy" in arms and TOOL_ARMS & set(arms):
                shown[model] = (arms, [r for r in mine if r["arm"] in arms])
            else:
                arms = []
            pilots += sum(r["arm"] not in arms for r in mine)
        pilot_note = (f" ({pilots} pilot attempts are on file; a pilot is not a result and is "
                      f"not shown.)" if pilots else "")
        if not shown:
            parts += ["The full runs have not been made yet." + pilot_note, ""]
            continue
        every = [row for _arms, mine in shown.values() for row in mine]
        versions = every[0]["versions"]
        engines = sorted({row["versions"]["nagasim"] for row in every},
                         key=lambda v: [int(p) if p.isdigit() else p for p in v.split(".")])
        ids = sorted({row["model_id"] for row in every if row.get("model_id")})
        parts += [
            f"Attempts made from {min(r['date'] for r in every)} to {max(r['date'] for r in every)}: "
            f"{len(tasks)} tasks, {_span(shown, tasks)} per task and arm, at most {every[0]['budget']} "
            f"{'model replies' if comparison == 'local' else 'agent turns'} per attempt. "
            f"Naga {', '.join(engines)}, SimPy {versions['simpy']}, Python {versions['python']}."
            + (f" Model ids as reported by Claude Code: {', '.join(ids)}." if ids else "")
            + pilot_note, "",
            HEADER, "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        header = parts.index(HEADER, len(parts) - 2)
        parts[header:header] = [
            f"`{model}` was stopped after {len(mine)} of the {len(tasks) * len(arms) * FULL_REPEATS} "
            f"planned attempts ({FULL_REPEATS} per task and arm); its cells count what was made.\n"
            for model, (arms, mine) in shown.items() if len(mine) < len(tasks) * len(arms) * FULL_REPEATS]
        for model, (arms, mine) in shown.items():
            for arm in arms:
                group = [{**row, "read": None if row.get("tokens_in") is None
                          else row["tokens_in"] + (row.get("tokens_cached") or 0)}
                         for row in mine if row["arm"] == arm]
                parts.append(f"| `{model}` | {ARMS[arm]} | {len(group)} | {_share(group, 'valid')} | "
                             f"{_share(group, 'runnable')} | {_share(group, 'correct')} | "
                             f"{_median(group, 'turns')} | {_median(group, 'tool_calls')} | "
                             f"{_median(group, 'read', 1000)} | {_median(group, 'tokens_out', 1000)} | "
                             f"{_median(group, 'cost_usd')} | {_median(group, 'seconds', 60)} |")
        columns = [(model, arm) for model, (arms, _mine) in shown.items() for arm in arms]
        parts += ["", "Correct attempts per task:", "",
                  "| Task | " + " | ".join(f"`{model}`, {ARMS[arm]}" for model, arm in columns) + " |",
                  "|---|" + "---|" * len(columns)]
        for task, entry in tasks.items():
            cells = []
            for model, arm in columns:
                group = [r for r in shown[model][1] if r["task"] == task and r["arm"] == arm]
                cells.append(f"{sum(bool(r['correct']) for r in group)}/{len(group)}")
            parts.append(f"| `{task}`: {entry['title']} | " + " | ".join(cells) + " |")
        parts.append("")
    return "\n".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def attempts(name, summary):
        sub = commands.add_parser(name, help=summary)
        sub.add_argument("--tasks", nargs="+", metavar="TASK", help=f"default: all of {', '.join(TASK_IDS)}")
        sub.add_argument("--arms", nargs="+", choices=RUNNABLE_ARMS, default=list(RUNNABLE_ARMS))
        sub.add_argument("--repeats", type=int, default=1, help="attempts per task and arm (default 1)")
        sub.add_argument("--timeout", type=float, default=120,
                         help="seconds a script or a model run may take (default 120)")
        sub.add_argument("--out", type=Path, default=HERE / "attempts", help="where the attempt files go")
        return sub

    local = attempts("local", "local models through an OpenAI-compatible server (Ollama)")
    local.add_argument("--models", nargs="+", required=True, help="names as the server lists them")
    local.add_argument("--base-url", default="http://localhost:11434/v1")
    local.add_argument("--rounds", type=int, default=24,
                       help="tool rounds allowed; the model may reply once more (default 24)")
    local.add_argument("--reasoning", default=None, choices=("none", "low", "medium", "high", "omit"))
    claude = attempts("claude", "Claude Code headless, on the subscription it is logged in with")
    claude.add_argument("--model", default="haiku", help="alias or full name (default: haiku, the cheapest)")
    claude.add_argument("--max-turns", type=int, default=50, help="agent turns per attempt (default 50)")
    claude.add_argument("--attempt-timeout", type=float, default=900,
                        help="seconds one attempt may take in all (default 900)")
    report = commands.add_parser("report", help="write results.json and results.md from the attempts")
    report.add_argument("--out", type=Path, default=HERE / "attempts")
    commands.add_parser("calibrate", help="measure the statistical tolerances (calibration.json)")
    commands.add_parser("check-model").add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.command == "check-model":
        sys.stdout.write(json.dumps(check_model(args.path.resolve())) + "\n")
    elif args.command == "calibrate":
        (HERE / "calibration.json").write_text(json.dumps(calibrate(), indent=1) + "\n",
                                               encoding="utf-8", newline="\n")
    elif args.command == "report":
        results = collect(args.out)
        (HERE / "results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8", newline="\n")
        (HERE / "results.md").write_text(render(results), encoding="utf-8", newline="\n")
        sys.stdout.write(render(results))
    elif args.command == "claude":
        return run_attempts("claude", claude_attempt, args)
    else:
        for args.model in args.models:
            run_attempts("local", local_attempt, args)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))       # this checkout's engine
    sys.exit(main())
