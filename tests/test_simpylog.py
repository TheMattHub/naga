"""simpylog (D725): a SimPy script writes a log that discover reads and check_draft accepts."""
import ast
import random
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import discover, simpylog

simpy = pytest.importorskip("simpy")


def _script(path, cases=600):
    """A user's own SimPy script: orders pass a Cut machine, then a Pack machine."""
    rng = random.Random(3)
    env = simpy.Environment()
    machines = {"Cut": simpy.Resource(env, 1), "Pack": simpy.Resource(env, 1)}
    mean = {"Cut": 3.0, "Pack": 2.0}
    log = simpylog.EventLog(path)

    def order(i):
        for step in ("Cut", "Pack"):
            log.ready(f"o{i}", step, env.now)
            with machines[step].request() as req:
                yield req
                log.start(f"o{i}", step, env.now, resource=f"{step}1")
                yield env.timeout(rng.expovariate(1 / mean[step]))
                log.complete(f"o{i}", step, env.now, resource=f"{step}1")

    def arrivals():
        for i in range(cases):
            env.process(order(i))
            yield env.timeout(rng.expovariate(1 / 9.0))

    env.process(arrivals())
    env.run()
    log.close()


def test_helper_imports_only_the_standard_library():
    tree = ast.parse(Path(simpylog.__file__).read_text())
    roots = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    roots |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert roots <= {"csv", "__future__"}, roots


def test_context_manager_closes_and_header_is_discovers(tmp_path):
    with simpylog.EventLog(tmp_path / "a.csv") as log:
        log.start("c1", "A", 0.0)
        log.complete("c1", "A", 2.5, resource="R")
    assert log._file.closed
    events = discover.read_log(tmp_path / "a.csv")
    assert [(e.case, e.activity, e.time, e.lifecycle, e.resource) for e in events] == [
        ("c1", "A", 0.0, "start", ""), ("c1", "A", 2.5, "complete", "R")]


def test_script_log_drafts_a_model_that_check_draft_passes(tmp_path):
    _script(tmp_path / "log.csv")
    events = discover.read_log(tmp_path / "log.csv")
    assert {e.activity for e in events} == {"Cut", "Pack"} and len(events) == 3600
    T.use_workspace(T.Workspace(tmp_path))
    assert T.call("import_event_log", path="log.csv", name="draft").get("ok")
    out = T.call("check_draft", model="draft", source="log.csv")
    assert out["verdict"] in ("passed", "partial"), out
    assert all(c["within"] for c in out["compared"]) and out["sample"]["source_cases"] == 600, out
