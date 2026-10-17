"""PHASE5 AB-1: agent-based modelling (`core/agents.py`, ARCHITECTURE 5.25, D93/D94).

The oracles are exact: each agent's times are rebuilt from its own stream,
`conftest.AgentStream` (D107: draw call n is Philox at counter (0, n, i, 0)), in the order the
runtime documents (attribute inits, then per state entry its transitions in
declaration order: E ~ Exp(1) for a rate, the timeout expression for a timeout).
"""
import json

import numpy as np
import pytest

from nagasim import agent_tools as T
from nagasim.core import agents as A
from nagasim.core.document import ModelDocument
from nagasim.core.playback import Timeline
from nagasim.core.schema import CURRENT_VERSION, migrate, validate_model_structured
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.trace import TraceRecorder
from nagasim.experiments import flatten_kpis

from conftest import AgentStream, chain, create, delay, dispose, problem_codes, run_env

PERSON = A.AgentType(
    "Person", parameters={"beta": 0.3, "gamma": 0.1}, attributes=[A.Attribute("t_inf", "-1")],
    states=["S", "I", "R"], initial="S",
    transitions=[A.Transition("S", "I", "rate", 'beta * COUNT("People", "I") / COUNT("People")',
                              actions=[A.Action.set("self.t_inf", "TNOW")]),
                 A.Transition("I", "R", "timeout", "EXPO(1 / gamma)")])
CLOCK = A.AgentType("Clock", states=["A", "B"],
                    transitions=[A.Transition("A", "B", "timeout", "1")])


def _model(*records, time_limit=100):
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)


def _sir(*extra):
    return _model(PERSON, A.Population("People", "Person", 300, {"I": 5}), *extra)


def _run(layout, seed=1):
    """Run headless and hand back the AgentSystem, whose columns are the oracle's input."""
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed)
    return env.ctx.agent_system


def _stream(seed, population, i):
    return AgentStream(seed, population, i)


# ---------------------------------------------------------------- oracles
def test_recovery_time_is_the_agents_own_rebuilt_stream():
    """CRN: agent i's recovery is `t_inf + EXPO(10)` drawn from ITS stream, after
    the E it drew on entering S -- whatever every other agent did."""
    people = _run(_sir()).by_name["People"]
    recovered = np.flatnonzero(people.state == people.type.index["R"])
    assert len(recovered) > 50
    for i in recovered:
        rng = _stream(1, "People", int(i))
        if i < 5:           # started in I: the recovery is its first draw
            assert people.entered[i] == rng.exponential(1 / 0.1)
        else:
            rng.standard_exponential()
            assert people.entered[i] == people.attrs["t_inf"][i] + rng.exponential(1 / 0.1)


def test_piecewise_shared_rate_and_product_form_fire_where_the_integral_equals_E():
    """Rate 0.5 before t=1 and 1.5 after (a clock agent flips at t=1), so
    Lambda(t) = 0.5 t, then 0.5 + 1.5 (t - 1): an agent fires where Lambda
    reaches E / w (w = 1 shared, w = self.w product, own only self.w)."""
    waiter = A.AgentType("Waiter", states=["W", "D"],
                         transitions=[A.Transition("W", "D", "rate", '0.5 + COUNT("Clock", "B")')])
    weighted = A.AgentType("Weighted", attributes=[A.Attribute("w", "UNIF(1, 3)")],
                           states=["W", "D"], transitions=[
                               A.Transition("W", "D", "rate", 'self.w * (0.5 + COUNT("Clock", "B"))')])
    own = A.AgentType("Own", attributes=[A.Attribute("w", "UNIF(1, 3)")], states=["W", "D"],
                      transitions=[A.Transition("W", "D", "rate", "self.w")])
    system = _run(_model(CLOCK, waiter, weighted, own, A.Population("Clock", "Clock", 1),
                         A.Population("Waiters", "Waiter", 40),
                         A.Population("Weighted", "Weighted", 40),
                         A.Population("Own", "Own", 20), time_limit=1000))

    def fire_time(key):
        return key / 0.5 if key < 0.5 else 1.0 + (key - 0.5) / 1.5

    branches = set()
    for name in ("Waiters", "Weighted", "Own"):
        pop = system.by_name[name]
        assert (pop.state == 1).all()
        for i in range(pop.n):
            rng = _stream(1, name, i)
            w = rng.uniform(1, 3) if name != "Waiters" else 1.0
            key = rng.standard_exponential() / w
            expected = key if name == "Own" else fire_time(key)
            assert pop.entered[i] == expected
            branches.add(key < 0.5)
    assert branches == {True, False}


def test_constant_timeout_gives_avg_count_half_the_population():
    kind = A.AgentType("T", states=["A", "B"], transitions=[A.Transition("A", "B", "timeout", "5")])
    results = run_single_simulation(_model(kind, A.Population("P", "T", 100), time_limit=10),
                                    seed=0, run_len=10, warmup=0)
    rows = results["agent_summary"].set_index("Agent State")
    assert rows.loc["P/A", "Avg Count"] == 50.0
    assert rows.loc["P/B", "Avg Count"] == 50.0
    assert rows.loc["P/B", "Entries"] == 100 and rows.loc["P/A", "Avg Sojourn"] == 5.0
    kpis = flatten_kpis(results).set_index("KPI")["Value"]
    assert kpis["agent/P/A/Avg Count"] == 50.0
    series = results["timeseries"]
    assert set(series[series["Series"].str.startswith("agents/")]["Series"]) == {"agents/P/A",
                                                                                 "agents/P/B"}


def test_condition_fires_at_t7_when_another_population_moves():
    clock = A.AgentType("Clock", states=["A", "B"],
                        transitions=[A.Transition("A", "B", "timeout", "7")])
    watcher = A.AgentType("Watcher", states=["Wait", "Go"], transitions=[
        A.Transition("Wait", "Go", "condition", 'COUNT("Clock", "B") > 0')])
    system = _run(_model(clock, watcher, A.Population("Clock", "Clock", 1),
                         A.Population("Watchers", "Watcher", 3), time_limit=20))
    watchers = system.by_name["Watchers"]
    assert (watchers.state == 1).all() and (watchers.entered == 7.0).all()


def test_clock_conditions_fire_at_their_instant_with_no_other_event():
    """`TNOW >= 7` is solved to a wake-up at 7, `TNOW > 3` to the next float
    after 3: nothing else in the model ever moves."""
    kind = A.AgentType("K", states=["Wait", "Go", "Late"], transitions=[
        A.Transition("Wait", "Go", "condition", "TNOW >= 7"),
        A.Transition("Go", "Late", "condition", "3 < TNOW and COUNT(\"P\", \"Go\") > 99")])
    pop = _run(_model(kind, A.Population("P", "K", 2), time_limit=20)).by_name["P"]
    assert (pop.state == 1).all() and (pop.entered == 7.0).all()
    gt = A.AgentType("G", states=["A", "B"], transitions=[A.Transition("A", "B", "condition",
                                                                      "TNOW > 3")])
    pop = _run(_model(gt, A.Population("P", "G", 1), time_limit=20)).by_name["P"]
    assert pop.entered[0] == np.nextafter(3.0, np.inf)


def test_step_and_ts_driven_shared_rates_fire_where_the_integral_equals_E():
    """Rate 0.2 before t=1, 0.8 after, from STEP and from a step TS, with no
    other event in the model: the change instant alone re-anchors Lambda."""
    step = A.AgentType("Step", states=["W", "D"],
                       transitions=[A.Transition("W", "D", "rate", "0.2 + STEP(0.6, 1)")])
    series = A.AgentType("Series", states=["W", "D"],
                         transitions=[A.Transition("W", "D", "rate", 'TS("r")')])
    layout = _model(step, series, A.Population("Step", "Step", 30),
                    A.Population("Series", "Series", 30), time_limit=1000)
    layout["timeseries_inputs"] = [{"name": "r", "times": [0, 1], "values": [0.2, 0.2 + 0.6]}]
    system = _run(layout)
    branches = set()
    for name in ("Step", "Series"):
        pop = system.by_name[name]
        for i in range(pop.n):
            e = _stream(1, name, i).standard_exponential()
            assert pop.entered[i] == (e / 0.2 if e < 0.2 else 1.0 + (e - 0.2) / (0.2 + 0.6))
            branches.add(e < 0.2)
    assert branches == {True, False}


def test_scan_rechecks_a_level_reading_condition_on_its_grid():
    watcher = A.AgentType("W", states=["Wait", "Go"], transitions=[
        A.Transition("Wait", "Go", "condition", 'LEVEL("Tank") > 5', scan=0.5)])
    layout = _model(watcher, A.Population("P", "W", 1), time_limit=20)
    layout.update(levels=[{"name": "Tank", "capacity": 100}],
                  flows=[{"name": "fill", "rate": "1", "to": "Tank"}])
    assert not problem_codes(layout)
    assert _run(layout).by_name["P"].entered[0] == 5.5
    del layout["agent_types"][0]["transitions"][0]["scan"]
    assert "W_AGENT_SCAN" in problem_codes(layout, severity=None)


def test_an_unrelated_population_leaves_every_time_bit_identical():
    noise = A.AgentType("Noise", states=["X", "Y"], transitions=[
        A.Transition("X", "Y", "rate", "2"), A.Transition("Y", "X", "timeout", "EXPO(0.3)")])
    alone = _run(_sir()).by_name["People"]
    mixed = _run(_sir(noise, A.Population("Noise", "Noise", 50)))
    assert mixed.by_name["Noise"].entries[1] > 1000
    people = mixed.by_name["People"]
    assert np.array_equal(alone.state, people.state)
    assert np.array_equal(alone.entered, people.entered)
    assert np.array_equal(alone.attrs["t_inf"], people.attrs["t_inf"])


def test_a_zero_time_condition_loop_hits_the_cascade_cap():
    loop = A.AgentType("Loop", states=["A", "B"], transitions=[
        A.Transition("A", "B", "condition", "true"), A.Transition("B", "A", "condition", "true")])
    with pytest.raises(RuntimeError, match="agent transitions at t=0"):
        _run(_model(loop, A.Population("L", "Loop", 1), time_limit=5))


# ---------------------------------------------------------- populations from data (PHASE6 C3)
def test_population_count_and_attribute_init_read_a_table_exactly():
    """count: {"table": ...} is the table's row count; an attribute's
    {"column": ...} init reads row i for agent i -- straight data, no draw,
    so CRN and every other agent's draw order are untouched."""
    kind = A.AgentType("Person", states=["S"], initial="S",
                       attributes=[A.Attribute("age", init={"column": "age"})])
    layout = _model(kind, A.Population("People", "Person", count={"table": "People"}))
    layout["tables"] = [{"name": "People", "keys": [0, 1, 2, 3, 4], "columns": ["age"],
                         "values": [[10], [20], [30], [40], [50]]}]
    system = _run(layout)
    pop = system.populations[0]
    assert pop.n == 5 and pop.max_count == 5     # max_count follows the resolved count (C5)
    assert list(pop.attrs["age"]) == [10.0, 20.0, 30.0, 40.0, 50.0]
    assert system.aggregate("SUM", "People", "age") == 150.0
    assert system.aggregate("MEAN", "People", "age") == 30.0


def test_population_count_sums_a_table_column():
    """count: {"table": ..., "column": ...} is the SUM of that column (a
    group table: rows are groups -- e.g. a region -- not individual agents)."""
    kind = A.AgentType("Person", states=["S"], initial="S")
    layout = _model(kind, A.Population("People", "Person",
                                       count={"table": "Regions", "column": "headcount"}))
    layout["tables"] = [{"name": "Regions", "keys": ["North", "South"],
                         "columns": ["headcount"], "values": [[30], [70]]}]
    assert _run(layout).populations[0].n == 100


@pytest.mark.parametrize("count, tables, code", [
    ({"table": "Nope"}, [], "E_POPULATION_COUNT"),                     # unknown table
    ({"table": "T", "column": "nope"},                                 # unknown count column
     [{"name": "T", "keys": [1, 2], "columns": ["n"], "values": [[1], [2]]}], "E_POPULATION_COUNT"),
    (5, [], "E_AGENT_ATTRIBUTE"),                                       # plain int, column init
    ({"table": "T"},                                                   # unknown attribute column
     [{"name": "T", "keys": [1, 2], "columns": ["other"], "values": [[1], [2]]}],
     "E_AGENT_ATTRIBUTE"),
    ({"table": "T", "column": "n"},                                    # grouped count, column init
     [{"name": "T", "keys": [1, 2], "columns": ["n"], "values": [[1], [2]]}], "E_AGENT_ATTRIBUTE"),
])
def test_data_driven_population_problem_codes(count, tables, code):
    kind = dict(_TYPE, attributes=[{"name": "x", "init": {"column": "age"}}])
    layout = {"blocks": [], "links": [], "agent_types": [kind],
             "populations": [dict(_POP, count=count)], "tables": tables}
    assert code in problem_codes(layout)


def test_data_driven_population_json_schema_and_record_round_trip():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    kind = A.AgentType("Person", states=["S"], initial="S",
                       attributes=[A.Attribute("age", init={"column": "age"})])
    layout = _model(kind, A.Population("People", "Person", count={"table": "People"}))
    layout["tables"] = [{"name": "People", "keys": [0, 1], "columns": ["age"],
                         "values": [[1], [2]]}]
    errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(migrate(layout)))
    assert not errors, errors[0].message
    population = A.Population.from_dict(json.loads(json.dumps(
        A.Population("People", "Person", count={"table": "People"}).to_dict())))
    assert population.count == {"table": "People"}
    attribute = A.Attribute.from_dict(json.loads(json.dumps(
        A.Attribute("age", init={"column": "age"}).to_dict())))
    assert attribute.init == {"column": "age"}


# ---------------------------------------------------------------- validation
_TYPE = {"name": "T", "parameters": {"k": 1}, "attributes": [{"name": "x", "init": "0"}],
         "states": [{"name": "A"}, {"name": "B"}], "initial_state": "A",
         "transitions": [{"from": "A", "to": "B", "timeout": "1"}]}
_POP = {"name": "P", "type": "T", "count": 3}


def _with(transition=None, **type_keys):
    kind = dict(_TYPE, **type_keys)
    if transition is not None:
        kind["transitions"] = [dict({"from": "A", "to": "B"}, **transition)]
    return kind


@pytest.mark.parametrize("code, types, pops", [
    ("E_AGENT_TYPE_NAME", [_TYPE, _TYPE], [_POP]),
    ("E_AGENT_STATE", [_with({"to": "C", "timeout": "1"})], [_POP]),
    ("E_AGENT_TRANSITION", [_with({"timeout": "1", "rate": "2"})], [_POP]),
    ("E_AGENT_RATE_FORM", [_with({"rate": 'self.x + COUNT("P")'})], [_POP]),
    ("E_AGENT_RATE_FORM", [_with({"rate": "0.1 * TNOW"})], [_POP]),
    ("E_AGENT_TRANSITION", [_with({"condition": "TNOW > 1", "scan": 0})], [_POP]),
    ("E_AGENT_ATTRIBUTE", [_with({"rate": "self.y"})], [_POP]),
    ("E_AGENT_ACTION", [_with({"timeout": "1", "actions": [{"target": "self.y", "expr": "1"}]})],
     [_POP]),
    ("E_AGENT_EXPRESSION", [_with({"timeout": "1 +"})], [_POP]),
    ("E_AGENT_SPACE", [_with(space={"kind": "continuous"})], [_POP]),
    ("E_POPULATION_NAME", [_TYPE], [_POP, _POP]),
    ("E_POPULATION_TYPE", [_TYPE], [dict(_POP, type="Q")]),
    ("E_POPULATION_COUNT", [_TYPE], [dict(_POP, count=2, initial=[{"state": "B", "count": 3}])]),
    ("E_POPULATION_UNKNOWN", [_with({"condition": 'COUNT("Nobody") > 0'})], [_POP]),
    ("E_AGGREGATE_AGENTS", [_with({"condition": 'SUM("P", 1) > 0'})], [_POP]),
])
def test_every_agent_problem_code(code, types, pops):
    layout = {"blocks": [], "links": [], "agent_types": types, "populations": pops}
    assert problem_codes(layout) == {code}


def test_a_valid_agent_model_has_no_problems_and_self_stays_inside_agents():
    assert problem_codes({"blocks": [], "links": [], "agent_types": [_TYPE], "populations": [_POP]}) \
        == set()
    assert not problem_codes(_sir())
    block = {"name": "A", "type": "ASSIGN",
             "params": {"assignments": [{"target": "entity.v", "expr": "self.x"}]}}
    assert "E_EXPRESSION" in problem_codes({"blocks": [block], "links": []})


# ---------------------------------------------------------------- format and API
def test_schema_v22_migrates_an_older_file_to_empty_agent_sections():
    migrated = migrate({"schema_version": 21, "blocks": [], "links": []})
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["agent_types"] == [] and migrated["populations"] == []


def test_records_round_trip_through_json_and_the_document():
    population = A.Population("People", "Person", 300, {"I": 5})
    for record in (PERSON, population, A.Transition("I", "R", "timeout", "5", guard="true")):
        assert type(record).from_dict(json.loads(json.dumps(record.to_dict()))) == record
    doc = ModelDocument()
    doc.upsert_agent_type(PERSON)
    doc.upsert_population(population)
    layout = json.loads(json.dumps(doc.to_layout()))
    assert layout["agent_types"] == [PERSON.to_dict()]
    assert layout["populations"] == [population.to_dict()]
    doc.undo()
    assert doc.populations == []


def test_the_sir_layout_validates_against_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(migrate(_sir())))
    assert not errors, errors[0].message


def test_agent_tools_build_list_run_and_remove(tmp_path):
    T.use_workspace(T.Workspace(tmp_path))

    def ok(result):
        assert "error" not in result, result.get("error")
        return result

    ok(T.call("new_model", name="sir"))
    ok(T.call("upsert_record", model="sir", section="agent_types", record={
        "name": "Person", "states": [{"name": s} for s in "SIR"],
        "parameters": {"beta": 0.3, "gamma": 0.1},
        "transitions": PERSON.to_dict()["transitions"],
        "attributes": [{"name": "t_inf", "init": "-1"}]}))
    ok(T.call("upsert_record", model="sir", section="populations", record={
        "name": "People", "type": "Person", "count": 100,
        "initial": [{"state": "I", "count": 5}]}))
    listed = ok(T.call("list_records", model="sir", section="populations"))
    assert [p["name"] for p in listed["records"]] == ["People"]
    ok(T.call("set_simulation_settings", model="sir", run_length=50))
    assert ok(T.call("run_model", model="sir", replications=1))
    removed = ok(T.call("remove_record", model="sir", section="agent_types", key="Person"))
    assert "E_POPULATION_TYPE" in json.dumps(removed)
    ok(T.call("remove_record", model="sir", section="populations", key="People"))
    assert ok(T.call("list_records", model="sir", section="agent_types"))["records"] == []


# ---------------------------------------------------------------- AB-2: messages and coupling
def _block(name, btype, **params):
    return {"name": name, "description": name, "type": btype, "position": [0, 0],
            "params": params}


def test_payload_guard_where_filter_and_dropped_counter_per_state():
    """t=1: "go" (payload 5) to the Workers with an odd id; the guard `payload > 3`
    holds, so exactly those move. t=2: "go" (payload 1) to everyone: the guard
    fails for the Idle ones and the Done ones have no transition, so all four
    are dropped, counted against the state each recipient was in."""
    boss = A.AgentType("Boss", states=["S0", "S1", "S2"], transitions=[
        A.Transition("S0", "S1", "timeout", "1", actions=[
            A.Action.send("go", {"population": "W", "where": "self.id % 2 == 1"}, 5)]),
        A.Transition("S1", "S2", "timeout", "1", actions=[
            A.Action.send("go", {"population": "W"}, 1)])])
    worker = A.AgentType("Worker", attributes=["got"], states=["Idle", "Done"], transitions=[
        A.Transition("Idle", "Done", "message", "go", guard="payload > 3",
                     actions=[A.Action.set("self.got", "payload")])])
    system = _run(_model(boss, worker, A.Population("B", "Boss", 1),
                         A.Population("W", "Worker", 4), time_limit=10))
    w = system.by_name["W"]
    assert list(w.attrs["got"]) == [0, 5, 0, 5] and list(w.entered) == [0, 1, 0, 1]
    assert w.dropped == [2, 2] and system.dropped == 4


def test_send_targets_delivery_order_and_the_cap_on_a_message_loop():
    # A random pick from a one-agent population, and AGENT("Q", 0) as `to`.
    pinger = A.AgentType("Pinger", attributes=["n"], states=["A"], transitions=[
        A.Transition("A", "A", "message", "tick", actions=[
            A.Action.set("self.n", "self.n + 1"), A.Action.send("tock", 'AGENT("Q", 0)')])])
    ponger = A.AgentType("Ponger", attributes=["n"], states=["A"], transitions=[
        A.Transition("A", "A", "message", "tock", actions=[A.Action.set("self.n", "self.n + 1")])])
    kicker = A.AgentType("Kicker", states=["A", "B"], transitions=[
        A.Transition("A", "B", "timeout", "2", actions=[
            A.Action.send("tick", {"population": "P", "random": True})])])
    system = _run(_model(pinger, ponger, kicker, A.Population("P", "Pinger", 1),
                         A.Population("Q", "Ponger", 1), A.Population("K", "Kicker", 2),
                         time_limit=5))
    assert system.by_name["P"].attrs["n"][0] == 2 and system.by_name["Q"].attrs["n"][0] == 2
    # "self" and an index address the SENDER's population. Messages are delivered
    # after the firing that sent them and before the next tied one (D95): agent
    # 0's go to agent 1 (still in A) and to itself (already in B).
    starter = A.AgentType("Starter", states=["A", "B"], transitions=[
        A.Transition("A", "B", "timeout", "2", actions=[
            A.Action.send("tick", 1), A.Action.send("tick", "self")])])
    system = _run(_model(starter, A.Population("S", "Starter", 2), time_limit=5))
    assert system.by_name["S"].dropped == [1, 3]
    loop = A.AgentType("Loop", states=["A"], transitions=[
        A.Transition("A", "A", "message", "m", actions=[A.Action.send("m", "self")])])
    kick = A.AgentType("Kick", states=["A", "B"], transitions=[
        A.Transition("A", "B", "timeout", "1", actions=[A.Action.send("m", 'AGENT("L", 0)')])])
    with pytest.raises(RuntimeError, match="agent transitions at t=1"):
        _run(_model(loop, kick, A.Population("L", "Loop", 1), A.Population("K", "Kick", 1),
                    time_limit=5))


def test_enter_then_send_block_answers_the_agent_that_made_the_entity():
    """Agent i enters one entity at t = i + 1; it waits 2 in a DELAY, then SEND
    (default `to` = entity.agent) moves exactly that agent to Back at i + 3."""
    worker = A.AgentType("Worker", states=["Home", "Away", "Back"], transitions=[
        A.Transition("Home", "Away", "timeout", "self.id + 1",
                     actions=[A.Action.enter("wait", "Job")]),
        A.Transition("Away", "Back", "message", "done")])
    layout = _model(worker, A.Population("W", "Worker", 3), time_limit=10)
    layout["blocks"] = [delay("wait", 2), _block("tell", "SEND", message="done"), dispose("out")]
    layout["links"] = chain(*layout["blocks"])
    assert not problem_codes(layout)
    w = _run(layout).by_name["W"]
    assert list(w.entered) == [3, 4, 5] and list(w.state) == [2, 2, 2]
    entities = w.system.env.ctx.all_entities
    assert [e.agent for e in entities] == [("W", 0), ("W", 1), ("W", 2)]
    assert {e.e_type for e in entities} == {"JOB"}      # e_type is upper-cased
    assert [e.deletion_time for e in entities] == [3, 4, 5]


def test_transfer_conserves_the_level_and_signal_releases_a_hold():
    """4 agents remove 2 each from a stock of 5 at t = 1..4: moved 5 + unmet 3
    = 8 asked, the level ends at 0; the first signal releases the held entity."""
    kind = A.AgentType("Taker", states=["A", "B"], transitions=[
        A.Transition("A", "B", "timeout", "self.id + 1", actions=[
            A.Action.transfer("Stock", 2), A.Action.signal("go")])])
    layout = _model(kind, A.Population("T", "Taker", 4), time_limit=10)
    layout["levels"] = [{"name": "Stock", "initial": 5}]
    layout["blocks"] = [create("c", max_arrivals=1),
                        _block("h", "HOLD", mode="Signal", signal_name="go"), dispose("d")]
    layout["links"] = chain(*layout["blocks"])
    env, _ = run_env(layout, 10, seed=1)
    stock = env.ctx.level_system.levels["Stock"]
    assert (stock.level, stock.outflow_total, stock.unmet) == (0, 5, 3)
    assert [e.deletion_time for e in env.ctx.all_entities] == [1]


@pytest.mark.parametrize("code, action, trigger", [
    ("E_SEND_TARGET", {"send": "m", "to": {"population": "P", "who": 1}}, {"timeout": "1"}),
    ("E_SEND_TARGET", {"send": "m", "to": None}, {"timeout": "1"}),
    ("E_POPULATION_UNKNOWN", {"send": "m", "to": {"population": "Nope"}}, {"timeout": "1"}),
    ("E_POPULATION_UNKNOWN", {"send": "m", "to": 'AGENT("Nope", 0)'}, {"timeout": "1"}),
    ("E_AGENT_ATTRIBUTE", {"send": "m", "to": {"population": "P", "where": "self.nope"}},
     {"timeout": "1"}),
    ("E_AGENT_ATTRIBUTE", {"target": "self.x", "expr": 'AGENT("P", 0).nope'}, {"timeout": "1"}),
    ("E_AGENT_EXPRESSION", {"target": "self.x", "expr": "payload"}, {"timeout": "1"}),
    ("E_AGENT_ACTION", {"enter": "nowhere"}, {"timeout": "1"}),
    ("E_AGENT_ACTION", {"signal": "s", "send": "m"}, {"timeout": "1"}),
    ("E_LEVEL_UNKNOWN", {"transfer": "Tank", "amount": "1"}, {"timeout": "1"}),
    ("E_TRANSFER_MODE", {"transfer": "Stock", "amount": "1", "mode": "pour"}, {"timeout": "1"}),
    ("E_AGENT_TRANSITION", {"target": "self.x", "expr": "payload"}, {"message": 3}),
])
def test_every_ab2_problem_code(code, action, trigger):
    kind = _with(dict(trigger, actions=[action]))
    layout = {"blocks": [], "links": [], "agent_types": [kind], "populations": [_POP],
              "levels": [{"name": "Stock"}]}
    assert problem_codes(layout) == {code}


def test_payload_is_valid_in_a_message_transition_and_misuse_is_reported():
    kind = _with({"message": "m", "guard": "payload > 0",
                  "actions": [{"target": "self.x", "expr": "payload"},
                              {"send": "m", "to": "self", "payload": "self.x"}]})
    layout = {"blocks": [], "links": [], "agent_types": [kind], "populations": [_POP]}
    assert [p.code for p in validate_model_structured(layout)] == []
    del kind["transitions"][0]["actions"][1]
    assert [p.code for p in validate_model_structured(layout)] == ["W_AGENT_MESSAGE_UNUSED"]
    send = _block("s", "SEND", message="m", to="self")
    assign = _block("a", "ASSIGN", assignments=[{"target": "self.x", "expr": "1"}])
    layout["blocks"] = [create("c"), send, assign, dispose("d")]
    layout["links"] = chain(*layout["blocks"])
    assert problem_codes(layout) == {"E_SEND_TARGET", "E_ASSIGN_TARGET"}


def test_action_records_round_trip_and_the_json_schema_accepts_every_action():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    actions = [A.Action.set("self.x", 1), A.Action.send("m", {"population": "P", "random": True}, 2),
               A.Action.enter("b", "Job"), A.Action.transfer("Stock", 1, "add"),
               A.Action.signal("s")]
    tr = A.Transition("A", "B", "message", "m", actions=actions)
    assert A.Transition.from_dict(json.loads(json.dumps(tr.to_dict()))) == tr
    assert A.Action.from_dict(actions[1].to_dict()) == actions[1]
    assert actions[1].to_dict() == {"send": "m", "to": {"population": "P", "random": True},
                                    "payload": "2"}
    layout = migrate(_model(A.AgentType("T", states=["A", "B"], transitions=[tr]),
                            A.Population("P", "T", 1)))
    errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(layout))
    assert not errors, errors[0].message


# ---------------------------------------------------------------- AB-3: space and movement
def _run_traced(layout, seed=1):
    """Like `_run`, plus a `TraceRecorder` subscribed before `prepare_model`
    (so a population's placement/initial-state trace is not missed)."""
    recorder = TraceRecorder()
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed, subscribers=(recorder,))
    return env.ctx.agent_system, recorder


_WALKER = A.AgentType(
    "Walker", states=["Start", "Moving", "Arrived"], initial="Start",
    space=A.Space("continuous", [0, 0, 100, 100]), speed="2",
    transitions=[A.Transition("Start", "Moving", "timeout", "0",
                              actions=[A.Action.move_to([3, 4])]),
                 A.Transition("Moving", "Arrived", "message", "arrived")])


def _walker_model(count=1, placement=None, time_limit=10):
    pop = A.Population("Walkers", "Walker", count, placement=placement)
    return _model(_WALKER, pop, time_limit=time_limit)


def test_near_and_nearest_match_a_brute_force_scan():
    n = 40
    rng = np.random.default_rng(7)
    points = rng.uniform(0.0, 100.0, size=(n, 2))
    person = A.AgentType(
        "Person", states=["S", "Done"], initial="S",
        space=A.Space("continuous", [0, 0, 100, 100]),
        attributes=[A.Attribute("near_r", "0"), A.Attribute("nearest_d", "0")],
        transitions=[A.Transition("S", "Done", "timeout", "0",
                                  actions=[A.Action.set("self.near_r", 'NEAR("People", 20)'),
                                          A.Action.set("self.nearest_d", 'NEAREST("People")')])])
    pop = A.Population("People", "Person", n,
                       placement={"kind": "table", "points": [[float(x), float(y)]
                                                              for x, y in points]})
    layout = _model(person, pop, time_limit=0.5)
    system = _run(layout)
    result = system.by_name["People"]
    for i in range(n):
        d = np.hypot(points[:, 0] - points[i, 0], points[:, 1] - points[i, 1])
        others = np.arange(n) != i
        assert int(result.attrs["near_r"][i]) == int(((d <= 20) & others).sum())
        assert float(result.attrs["nearest_d"][i]) == pytest.approx(float(d[others].min()))


def test_playback_timeline_agents_state_matches_the_engine_at_a_frame():
    layout = _walker_model(placement={"kind": "table", "points": [[0, 0]]}, time_limit=1.25)
    system, recorder = _run_traced(layout)
    pop = system.by_name["Walkers"]
    timeline = Timeline(recorder.events)
    x, y, state, category = timeline.state_at(1.25)["agents"][("Walkers", 0)]
    ex, ey = system._position(pop, 0, 1.25)
    assert (x, y) == pytest.approx((ex, ey))
    assert state == "Moving"
    assert category == ""


@pytest.mark.parametrize("code, kind_keys, pop_keys", [
    ("E_AGENT_SPACE", {"kind": "bogus"}, {}),
    ("E_AGENT_SPACE", {"kind": "continuous"}, {}),
    ("E_AGENT_SPACE", {"kind": "continuous", "bounds": [0, 0, 0, 5]}, {}),
    ("E_AGENT_SPACE", {"kind": "network"}, {}),
    ("E_AGENT_PLACEMENT", {"kind": "continuous", "bounds": [0, 0, 10, 10]},
     {"placement": {"kind": "bogus"}}),
    ("E_AGENT_PLACEMENT", {"kind": "continuous", "bounds": [0, 0, 10, 10]},
     {"placement": {"kind": "table"}}),
])
def test_every_ab3_space_problem_code(code, kind_keys, pop_keys):
    kind = _with({"timeout": "1"}, space=kind_keys)
    pop = dict(_POP, **pop_keys)
    layout = {"blocks": [], "links": [], "agent_types": [kind], "populations": [pop]}
    assert problem_codes(layout) == {code}


def test_a_valid_space_and_placement_have_no_problems():
    layout = _walker_model(placement={"kind": "table", "points": [[0, 0]]})
    assert not problem_codes(layout)


def test_move_to_action_round_trips_and_the_json_schema_accepts_it():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    action = A.Action.move_to([3, 4])
    assert A.Action.from_dict(action.to_dict()) == action
    assert action.to_dict() == {"move_to": [3.0, 4.0]}
    layout = migrate(_walker_model(placement={"kind": "table", "points": [[0, 0]]}))
    errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(layout))
    assert not errors, errors[0].message


# ---------------------------------------------------------------- PHASE6 C5: births and deaths
# A cell divides at rate b and dies at rate d (own rates, so Lambda(t) = t and
# a firing time is exactly `entry + E / w`). Stream keys: initial agent i -> i,
# the s-th birth of the run -> count + s, whatever slot it lands in (D98).
def _cell(b=0.3, d=0.1, space=None):
    return A.AgentType(
        "Cell", attributes=[A.Attribute("b", b), A.Attribute("d", d), A.Attribute("born", "TNOW")],
        states=["A"], space=space,
        transitions=[A.Transition("A", "A", "rate", "self.b", actions=[A.Action.spawn("Cells")]),
                     A.Transition("A", "A", "rate", "self.d", actions=[A.Action.remove()])])


def _birth_death_oracle(seed, n0, b, d, horizon):
    """(live birth times, births, integral of the count): the model rebuilt
    by hand from the documented streams and draw order (per entry: E_birth
    then E_death, both from the agent's own stream)."""
    import heapq
    streams, heap, live = {}, [], {}
    births, area, t_last = 0, 0.0, 0.0

    def enter(key, t):
        rng = streams.setdefault(key, _stream(seed, "Cells", key))
        tb, td = t + rng.standard_exponential() / b, t + rng.standard_exponential() / d
        heapq.heappush(heap, (min(tb, td), key, tb < td))
    for key in range(n0):
        live[key] = 0.0
        enter(key, 0.0)
    while heap and heap[0][0] <= horizon:
        t, key, is_birth = heapq.heappop(heap)
        area += len(live) * (t - t_last)
        t_last = t
        if is_birth:
            child = n0 + births
            births += 1
            live[child] = t
            enter(child, t)
            enter(key, t)
        else:
            del live[key]
    area += len(live) * (horizon - t_last)
    return sorted(live.values()), births, area


def test_birth_death_counts_and_times_match_the_hand_rebuilt_streams():
    layout = _model(_cell(0.5, 0.2), A.Population("Cells", "Cell", 6, max_count=400),
                    time_limit=10)
    system = _run(layout)
    pop = system.by_name["Cells"]
    born, births, area = _birth_death_oracle(1, 6, 0.5, 0.2, 10)
    live = pop.members()
    assert pop.births == births > 10 and pop.live == len(live) == len(born)
    assert sorted(pop.attrs["born"][live].tolist()) == born       # exact, bit for bit
    assert pop.n < 6 + births                                   # dead slots were recycled
    assert system.count("Cells") == pop.live
    row = pop.summary(10.0)[0]
    assert row["Final"] == pop.live and row["Avg Count"] == pytest.approx(area / 10, rel=1e-12)


def test_a_ref_to_a_removed_agent_is_refused_even_after_its_slot_is_reused():
    from nagasim.core.expressions import ExpressionError, compile_expression
    layout = _model(_cell(b=0.0, d=0.0), A.Population("Cells", "Cell", 3), time_limit=1)
    system = _run(layout)
    pop = system.by_name["Cells"]
    ref = system.ref("Cells", 1)
    read = compile_expression('AGENT("Cells", 1).born')
    scope = pop.scope(0)
    assert system.alive(ref) and read.evaluate(scope) == 0.0
    system.remove_agent(pop, 1)
    assert not system.alive(ref) and system.count("Cells") == 2
    with pytest.raises(ExpressionError, match="was removed"):
        system.deref(ref)
    with pytest.raises(ExpressionError, match="was removed"):
        read.evaluate(scope)
    assert system.add_agent(pop) == 1                   # the slot is reused ...
    assert not system.alive(ref)                        # ... by a different agent
    with pytest.raises(ExpressionError, match="was removed"):
        system.deref(ref)
    assert system.alive(system.ref("Cells", 1)) and ref == ("Cells", 1)
    assert compile_expression('ALIVE(AGENT("Cells", 1))').evaluate(scope) is True


def test_playback_tokens_appear_at_birth_and_disappear_at_death():
    kind = _cell(space=A.Space("continuous", [0, 0, 10, 10]))
    layout = _model(kind, A.Population("Cells", "Cell", 4, max_count=400), time_limit=12)
    system, recorder = _run_traced(layout)
    pop = system.by_name["Cells"]
    removed = [e for e in recorder.events if e.kind == "agent_removed"]
    assert removed
    timeline = Timeline(recorder.events)
    assert {j for _p, j in timeline.state_at(12.0)["agents"]} == set(pop.members().tolist())
    first = removed[0]
    assert ("Cells", first.entity_id) in timeline.state_at(first.t - 1e-9)["agents"]
    assert ("Cells", first.entity_id) not in Timeline(
        [e for e in recorder.events if e.t <= first.t]).state_at(first.t)["agents"]


_SPAWNER = dict(_TYPE, transitions=[{"from": "A", "to": "B", "timeout": "1", "actions": []}])


def _spawn_layout(action, **pop_keys):
    kind = json.loads(json.dumps(_SPAWNER))
    kind["transitions"][0]["actions"] = [action]
    return {"blocks": [], "links": [], "agent_types": [kind],
            "populations": [dict(_POP, **dict({"max_count": 10}, **pop_keys))]}


@pytest.mark.parametrize("code, action, pop_keys", [
    ("E_AGENT_ACTION", {"spawn": "P", "colour": "red"}, {}),
    ("E_AGENT_ACTION", {"spawn": "P", "count": -1}, {}),
    ("E_AGENT_ACTION", {"remove": ""}, {}),
    ("E_POPULATION_UNKNOWN", {"spawn": "Nope"}, {}),
    ("E_POPULATION_UNKNOWN", {"remove": {"population": "Nope"}}, {}),
    ("E_AGENT_STATE", {"spawn": "P", "state": "Zzz"}, {}),
    ("E_AGENT_ATTRIBUTE", {"spawn": "P", "init": {"nope": "1"}}, {}),
    ("E_AGENT_EXPRESSION", {"spawn": "P", "count": "1 +"}, {}),
    ("E_POPULATION_COUNT", {"remove": "self"}, {"max_count": 2}),
])
def test_every_c5_problem_code(code, action, pop_keys):
    assert problem_codes(_spawn_layout(action, **pop_keys)) == {code}


def test_spawn_and_remove_round_trip_and_the_json_schema_accepts_them():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    spawn = A.Action.spawn("P", count="self.x + 1", state="B", init={"x": "self.x / 2"})
    assert spawn.to_dict() == {"spawn": "P", "count": "self.x + 1", "state": "B",
                               "init": {"x": "self.x / 2"}}
    assert A.Action.remove().to_dict() == {"remove": "self"}
    pop = A.Population("P", "T", 3, max_count=10)
    assert A.Population.from_dict(pop.to_dict()) == pop and pop.to_dict()["max_count"] == 10
    for action in (spawn.to_dict(), {"spawn": "P", "count": 1},    # a literal int count too
                   {"remove": {"population": "P", "where": "self.x > 1"}}):
        layout = _spawn_layout(action)
        assert not problem_codes(layout)
        errors = list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(
            migrate(dict(layout, simulation={"time_limit": 5}))))
        assert not errors, errors[0].message
