"""Plan N task 1 (D1529-D1531): `simulation.queue_limit` stops a run on a runaway queue. Engine-only.

Written from the requirements (spec docs/superpowers/specs/2026-11-28-queue-limit-stop-design.md), before
the implementation. API assumed:
  * the setting is `simulation.queue_limit = {"on": bool, "length": int}`; absent = off;
  * `nagasim.api.run_model(...)` raises an exception carrying `.code == "E_QUEUE_LIMIT"` (the existing
    ModelRunError path, code carried through, also from a worker process); `str(exc)` reads
    "Stopped at t=<time>: <block> had <n> waiting, over the model's queue limit of <limit>; ...";
  * the agent tool `T.call("run_model", ...)` answers `{"error": {"code": "E_QUEUE_LIMIT", ...}}`
    (its ToolError path, the run error's code carried through);
  * `validate_model_structured` reports W_QUEUE_LIMIT_SETTING (a warning, never an error) for a bad value
    (`on` not a bool, `length` not a positive integer) and a run then reads the setting as off.
Timing: the engine checks the queues every 4,096 events, so a stop is never earlier than the queue reaching
the limit but may be later; no test pins the exact stop time. Every runaway model below runs long enough
(over 4,096 events) for at least one check to fire.
"""
import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import chain, create, dispose, layout, problem_codes, process  # noqa: E402
from test_network import NETWORK, fleet, station, transport  # noqa: E402

from nagasim import agent_tools as T
from nagasim import api

LIMIT = 50
LONG = 10000          # model time units: far more than 4,096 events for every model below


def _sim(run, **extra):
    return {"time_limit": run, "warmup": 0, "replications": 1, **extra}


def _server_model(run=LONG, **sim):
    """An arrival every 1 into one server taking 2: the queue grows by about 1 per 2 time units."""
    blocks = [create(dist_params={"value": 1.0}), process(value=2.0), dispose()]
    return layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                  simulation=_sim(run, **sim))


def _limited():
    return {"queue_limit": {"on": True, "length": LIMIT}}


def _hold_model():
    """A HOLD whose condition is never true: every arrival waits there, one more per time unit."""
    gate = {"name": "gate", "description": "gate", "type": "HOLD", "position": [1, 0],
            "params": {"mode": "Condition", "conditions": ["0 == 1"], "scan_interval": 1000.0}}
    blocks = [create(dist_params={"value": 1.0}), gate, dispose()]
    return layout(blocks, chain(*blocks), simulation=_sim(LONG, **_limited()))


def _match_model():
    """A MATCH of two on `entity.order`, which is unique per arrival: nothing ever pairs."""
    stamp = {"name": "stamp", "description": "stamp", "type": "ASSIGN", "position": [1, 0],
             "params": {"assignments": [{"target": "entity.order", "expr": "int(TNOW)"}]}}
    match = {"name": "pair", "description": "pair", "type": "MATCH", "position": [2, 0],
             "params": {"number_to_match": 2, "batch_option": False, "match_on": "entity.order"}}
    blocks = [create(dist_params={"value": 1.0}), stamp, match, dispose()]
    return layout(blocks, chain(*blocks), simulation=_sim(LONG, **_limited()))


def _batch_model():
    """A BATCH that needs a million entities: the arrivals collect in it, one more per time unit."""
    batch = {"name": "batch_1", "description": "batch_1", "type": "BATCH", "position": [1, 0],
             "params": {"batch_size": 1000000, "batched_entity_type": "BATCH"}}
    blocks = [create(dist_params={"value": 1.0}), batch, dispose()]
    return layout(blocks, chain(*blocks), simulation=_sim(LONG, **_limited()))


def _mover_model():
    """One forklift, a trip of 10 time units (50 m at 5 m/time), a job every 1: transport requests queue."""
    blocks = [create(dist_params={"value": 1.0}), station("S_A", "dock"),
              transport("move", "Forklifts", destination="S_B"), station("S_B", "store"), dispose()]
    links = [{"source": "create_1", "target": "S_A"}, {"source": "S_A", "target": "move"},
             {"source": "move", "target": "dispose_1"}, {"source": "S_B", "target": "dispose_1"}]
    return layout(blocks, links, network=NETWORK, fleets=[fleet()], simulation=_sim(LONG, **_limited()))


def _stopped(model, **kwargs):
    with pytest.raises(Exception) as caught:
        api.run_model(copy.deepcopy(model), replications=kwargs.pop("replications", 1), **kwargs)
    return caught.value


STOP_TEXT = re.compile(r"Stopped at t=(?P<t>[0-9.]+): (?P<block>.+?) had (?P<n>\d+) waiting, "
                       r"over the model's queue limit of (?P<limit>\d+)")


def _frames(results):
    return {name: results[name] for name in ("system", "entity_summary", "resource_summary")}


def _same_results(a, b):
    for name, frame in _frames(a).items():
        assert frame.equals(_frames(b)[name]), name


# ------------------------------------------------------------------ Review Focus 1: the stop

def test_a_growing_process_queue_stops_the_run_naming_the_block():
    """Arrival 1, service 2: the queue reaches 50 no earlier than t=100, so the stop is at t >= 100 and
    the length is at or above the limit, never below. The block is the PROCESS holding the queue."""
    exc = _stopped(_server_model(**_limited()))
    assert getattr(exc, "code", None) == "E_QUEUE_LIMIT", repr(exc)
    found = STOP_TEXT.search(str(exc))
    assert found, str(exc)
    assert found["block"] == "process_1"
    assert int(found["n"]) >= LIMIT and int(found["limit"]) == LIMIT
    assert float(found["t"]) >= 100.0


@pytest.mark.slow
def test_the_stop_is_the_same_in_a_process_pool():
    """A worker process reports the run error with its code and text, as inline."""
    exc = _stopped(_server_model(**_limited()), replications=2, workers=2)
    assert getattr(exc, "code", None) == "E_QUEUE_LIMIT", repr(exc)
    found = STOP_TEXT.search(str(exc))
    assert found and found["block"] == "process_1" and int(found["n"]) >= LIMIT, str(exc)
    assert float(found["t"]) >= 100.0


def test_the_run_tool_answers_with_the_code(tmp_path):
    ws = T.use_workspace(T.Workspace(tmp_path))
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_server_model(**_limited())), encoding="utf-8")
    assert "error" not in T.call("open_model", path=str(path), name="m")
    answer = T.call("run_model", model="m", replications=1)
    assert answer.get("error", {}).get("code") == "E_QUEUE_LIMIT", answer
    assert "process_1" in answer["error"]["message"]
    assert ws is not None


# ------------------------------------------------------------------ Review Focus 2: off changes nothing

@pytest.mark.parametrize("setting", [None, {"on": False, "length": LIMIT}, {"on": False}],
                         ids=["absent", "off", "off-no-length"])
def test_without_the_limit_the_run_finishes_with_the_same_results(setting):
    """A key-free model runs to its end (queue of ~1,500 at t=3000); `on: false` gives equal frames."""
    base = api.run_model(_server_model(run=3000), replications=1)
    model = _server_model(run=3000)
    if setting is not None:
        model["simulation"]["queue_limit"] = setting
    _same_results(base, api.run_model(model, replications=1))


def test_a_limit_above_the_longest_queue_does_not_stop_the_run():
    """The queue at t=3000 is about 1,500: a limit of 10,000 never trips and changes no result."""
    base = api.run_model(_server_model(run=3000), replications=1)
    _same_results(base, api.run_model(
        _server_model(run=3000, queue_limit={"on": True, "length": 10000}), replications=1))


# ------------------------------------------------------------------ Review Focus 3: each kind of queue

@pytest.mark.parametrize("build, kind", [(_hold_model, "HOLD"), (_match_model, "MATCH"),
                                         (_batch_model, "BATCH"), (_mover_model, "mover")],
                         ids=["hold", "match", "batch", "mover"])
def test_each_kind_of_queue_trips_the_limit(build, kind):
    """One arrival per time unit joins a place that never lets anyone out (a transporter: one trip of
    10 units against an arrival per unit), so it holds at least 50 entities well before the end."""
    model = build()
    assert problem_codes(model) == set(), kind          # a valid model: the stop is the engine's
    exc = _stopped(model)
    assert getattr(exc, "code", None) == "E_QUEUE_LIMIT", f"{kind}: {exc!r}"
    found = STOP_TEXT.search(str(exc))
    assert found and int(found["n"]) >= LIMIT and int(found["limit"]) == LIMIT, f"{kind}: {exc}"
    assert float(found["t"]) >= LIMIT                    # one arrival per unit: 50 waiting needs t >= 50


def _buffer_model():
    """A BUFFER of 2 places before a server that never frees: later arrivals wait for a place."""
    buf = {"name": "buf", "description": "buf", "type": "BUFFER", "position": [1, 0],
           "params": {"capacity": 2}}
    blocks = [create(dist_params={"value": 1.0}), buf, process(value=1e9), dispose()]
    return layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                  simulation=_sim(LONG, **_limited()))


def _set_model():
    """The server queue of _server_model, on a resource set of two members (one unit each)."""
    blocks = [create(dist_params={"value": 0.5}), process(resource="Crew", value=2.0), dispose()]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "A", "capacity": 1}, {"name": "B", "capacity": 1}],
                  resource_sets=[{"name": "Crew", "members": ["A", "B"]}],
                  simulation=_sim(LONG, **_limited()))


def _submodel_model():
    """The server queue inside a submodel instance: the gauge carries the flattened name."""
    def sub(name, type_name, **params):
        return {"name": name, "description": name, "type": type_name, "position": [0, 0],
                "params": params}
    work = process(name="work", value=2.0)
    definition = {"info": {}, "parameters": [], "annotations": [],
                  "blocks": [sub("in", "SUB_ENTRY", port="in"), work,
                             sub("out", "SUB_EXIT", port="out")],
                  "links": [{"source": "in", "target": "work"}, {"source": "work", "target": "out"}]}
    blocks = [create(dist_params={"value": 1.0}), sub("cell", "SUBMODEL", definition="Cell",
                                                      arguments={}), dispose()]
    return layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                  submodels={"Cell": definition}, simulation=_sim(LONG, **_limited()))


@pytest.mark.parametrize("build, block", [(_buffer_model, "buf"), (_set_model, "process_1"),
                                          (_submodel_model, "cell/work")],
                         ids=["buffer", "resource-set", "submodel"])
def test_buffer_set_and_submodel_queues_trip_the_limit(build, block):
    model = build()
    assert problem_codes(model) == set()
    exc = _stopped(model)
    assert getattr(exc, "code", None) == "E_QUEUE_LIMIT", repr(exc)
    found = STOP_TEXT.search(str(exc))
    assert found and found["block"] == block and int(found["n"]) >= LIMIT, str(exc)


def test_a_charger_queue_is_one_of_the_gauges():
    """A charger's queue of vehicles is read like the others: its name in the message."""
    from nagasim.core import queuelimit
    from test_battery import amr, dock, model, run_env
    doc = model(amr(), [dock("dock")], jobs=0)
    env, _ = run_env(doc, 1.0)
    env.ctx.queue_limit = LIMIT
    env.ctx.queue_gauges = []
    queuelimit.register(env.ctx, [])
    assert "dock" in [name for name, _ in env.ctx.queue_gauges]
    queuelimit.check(env.ctx, 0.0)                    # empty: no stop
    env.ctx.chargers["dock"].queue_len.add(LIMIT + 1, 0.0)
    with pytest.raises(Exception) as caught:
        queuelimit.check(env.ctx, 1.0)
    assert caught.value.code == "E_QUEUE_LIMIT" and "dock" in str(caught.value)


@pytest.mark.parametrize("build, block", [(_hold_model, "gate"), (_match_model, "pair"),
                                          (_batch_model, "batch_1")])
def test_each_queue_names_its_own_block(build, block):
    found = STOP_TEXT.search(str(_stopped(build())))
    assert found and found["block"] == block, (block, found)


# ------------------------------------------------------------------ Review Focus 4: a bad setting

@pytest.mark.parametrize("bad", [{"on": "yes"}, {"length": -5}, {"on": True, "length": -5},
                                 {"on": True, "length": 2.5}],
                         ids=["on-yes", "length-neg", "on-neg", "length-float"])
def test_a_bad_setting_warns_and_the_run_reads_it_as_off(bad):
    from nagasim.core.schema import validate_model_structured
    model = _server_model(run=3000, queue_limit=bad)
    warned = [p for p in validate_model_structured(model) if p.code == "W_QUEUE_LIMIT_SETTING"]
    assert len(warned) == 1 and warned[0].severity == "warning", warned
    assert problem_codes(model) == set()                # a warning only, never an error
    _same_results(api.run_model(_server_model(run=3000), replications=1),
                  api.run_model(model, replications=1))


def test_a_good_or_absent_setting_raises_no_warning():
    for setting in (None, {"on": True, "length": 100}, {"on": False}):
        sim = {} if setting is None else {"queue_limit": setting}
        assert "W_QUEUE_LIMIT_SETTING" not in problem_codes(_server_model(**sim), severity=None)


# ------------------------------------------------------------------ final review fixes

def test_a_stopped_scenario_is_named_in_the_message():
    """I1: baseline capacity 3 never queues; 'Slow' has capacity 1 and stops. The text says which."""
    from nagasim import experiments
    model = _server_model(run=LONG, **_limited())
    model["resources"] = [{"name": "Server", "capacity": 3}]
    model["scenarios"] = [{"name": "Slow", "overrides": [
        {"path": "resources.Server.capacity", "value": 1}]}]
    with pytest.raises(Exception) as caught:
        experiments.run_scenarios(model, replications=1)
    exc = caught.value
    assert exc.code == "E_QUEUE_LIMIT" and exc.block == "process_1" and exc.hint
    assert str(exc).startswith("scenario 'Slow': Stopped at t="), str(exc)


def test_a_session_stepped_in_small_chunks_still_stops():
    """The 4,096-event countdown is kept across chunks, not restarted by each short advance."""
    from nagasim.core.session import SimSession
    session = SimSession(_server_model(**_limited()), seed=0)
    with pytest.raises(Exception) as caught:
        for step in range(1, 4000):
            session.advance(to=step * 10.0)      # a few dozen events per chunk, far under 4,096
    assert caught.value.code == "E_QUEUE_LIMIT"
    assert session.now < LONG


def test_queue_limit_zero_on_a_model_without_the_setting_writes_nothing(tmp_path):
    ws = T.use_workspace(T.Workspace(tmp_path))
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_server_model(run=100)), encoding="utf-8")
    assert "error" not in T.call("open_model", path=str(path), name="m")
    answer = T.call("set_simulation_settings", model="m", queue_limit=0)
    assert "error" not in answer, answer
    assert "queue_limit" not in answer["simulation"]
    T.call("set_simulation_settings", model="m", queue_limit=500)
    T.call("set_simulation_settings", model="m", queue_limit=0)
    after = T.call("set_simulation_settings", model="m", replications=1)
    assert after["simulation"]["queue_limit"]["on"] is False      # a model with it: on becomes false
    assert ws is not None
