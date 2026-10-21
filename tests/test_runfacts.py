"""Run facts (D678-D679): what one replication did that nothing else reports, read
from its trace. Every model here is small enough that its facts are worked out by
hand in the test's comment; the surfaces (CLI, agent tool, Trace dock) follow."""
import json

import pytest

from conftest import block, chain, create, decide, delay, dispose, layout, process, resource_record

from nagasim import cli
from nagasim.core import runfacts, trace
from nagasim.core.runfacts import Fact, facts
from nagasim.factscli import run


def _two_resources():
    """Arrivals at t=0..5; each seizes Op (2 units), then Machine (1 unit), works 5."""
    blocks = [create(max_arrivals=6),
              block("seize_op", "SEIZE", {"resource_name": "Op", "quantity": 1, "priority": 0}),
              block("seize_m", "SEIZE", {"resource_name": "Machine", "quantity": 1,
                                         "priority": 0}),
              delay("work", 5.0),
              block("rel_m", "RELEASE", {"resource_name": "Machine"}),
              block("rel_op", "RELEASE", {"resource_name": "Op"}),
              dispose("out")]
    return layout(blocks, chain(*blocks), resources=[resource_record("Op", 2),
                                                     resource_record("Machine", 1)])


def _held():
    """Three arrivals at t=0, 1, 2 into a HOLD that never releases."""
    blocks = [create(max_arrivals=3), block("hold", "HOLD", {"mode": "Infinite"}), dispose("out")]
    return layout(blocks, chain(*blocks))


def _rows(answer):
    return [(f["kind"], f["block"] or f["resource"]) for f in answer["facts"]]


def test_a_branch_never_taken_and_resources_no_entity_seized():
    # route's cumulative probability 1.0 sends all five entities down its first link, so
    # reject (and dispose_b behind it) is never entered and Rework, named only by reject,
    # is never seized; Spare is named by no block at all.
    blocks = [create(max_arrivals=5), decide("route", "Chance", probs=[1.0]),
              dispose("dispose_a"), process("reject", resource="Rework"), dispose("dispose_b")]
    links = [{"source": s, "target": t} for s, t in (
        ("create_1", "route"), ("route", "dispose_a"), ("route", "reject"),
        ("reject", "dispose_b"))]
    answer = run(layout(blocks, links, resources=[resource_record("Rework"),
                                                  resource_record("Spare")]), run_length=0)
    assert answer["end_time"] == 4.0 and answer["truncated"] is False
    assert _rows(answer) == [("never_entered", "reject"), ("never_entered", "dispose_b"),
                             ("never_seized", "Rework"), ("never_seized", "Spare")]
    texts = [f["text"] for f in answer["facts"]]
    assert texts[0].startswith("reject was never entered: no entity reached it in the run "
                               "(to t=4). A branch never taken")
    assert "No entity reached the blocks that name it (reject)." in texts[2]
    assert "No block names it" in texts[3]


def test_a_two_resource_seize_holds_the_first_idle_while_entities_wait_for_it():
    # E2 seizes Op at 1 and waits for Machine until 5, E3 holds Op from 5 to 10 waiting, E4
    # 10-15, E5 15-20, E6 20-25: one Op unit always held by an entity waiting at seize_m.
    # Meanwhile E3..E6 wait for Op at seize_op over [2, 20] (E6 gets Op at 20). Op never has
    # a free unit, so all 18 are "held"; Machine is busy whenever anyone waits for it.
    (fact,) = run(_two_resources(), run_length=100)["facts"]
    assert (fact["kind"], fact["block"], fact["resource"]) == ("idle_while_waiting", "seize_op",
                                                                "Op")
    assert (fact["t"], fact["count"], fact["duration"]) == (2.0, 4, 18.0)
    assert fact["text"].startswith("Op had a unit doing no work for 18 minutes in total while "
                                   "4 entities waited for it at seize_op (from t=2): 18 of them "
                                   "with a unit held by an entity that was itself waiting at "
                                   "seize_m.")


@pytest.mark.parametrize("capacity, idle", [(3, (1.0, 3, 29.0)), (2, None)])
def test_a_seize_of_more_units_than_are_free_leaves_one_idle(capacity, idle):
    # Arrivals at 0..3 each take 2 units of R for 10: E1 0-10, E2 10-20, E3 20-30, E4 30-40.
    # With 3 units one is free the whole time E2..E4 wait (from 1 to 30): 29. With 2 units
    # none is: a seize event counts the block's quantity, not one unit.
    blocks = [create(max_arrivals=4),
              block("seize", "SEIZE", {"resource_name": "R", "quantity": 2, "priority": 0}),
              delay("work", 10.0), block("release", "RELEASE", {"resource_name": "R"}),
              dispose("out")]
    found = run(layout(blocks, chain(*blocks), resources=[resource_record("R", capacity)]),
                run_length=0)["facts"]
    if idle is None:
        assert found == []
        return
    (fact,) = found
    assert (fact["kind"], fact["block"], fact["resource"]) == ("idle_while_waiting", "seize", "R")
    assert (fact["t"], fact["count"], fact["duration"]) == idle
    assert "29 of them with a unit free and up" in fact["text"]


def test_a_failure_is_not_an_idle_unit():
    # Server fails at 4 for 3, 11 for 3, ...; entities queue while it is down and are served
    # the instant it is repaired, so it never has a free unit while one waits.
    blocks = [create(max_arrivals=20), process("work", value=0.5), dispose("out")]
    lay = layout(blocks, chain(*blocks), resources=[resource_record("Server")], failures=[
        {"name": "breakdown", "resources": ["Server"],
         "uptime": {"distribution": "Constant", "dist_params": {"value": 4}},
         "downtime": {"distribution": "Constant", "dist_params": {"value": 3}}}])
    assert run(lay, run_length=30)["facts"] == []


def test_entities_left_at_the_end_with_how_long_their_block_was_never_empty():
    # The HOLD keeps its three arrivals; the run drains at t=2, so out is never entered.
    answer = run(_held(), run_length=0)
    assert _rows(answer) == [("never_entered", "out"), ("left_waiting", "hold")]
    left = answer["facts"][1]
    assert (left["t"], left["count"], left["duration"]) == (0.0, 3, 2.0)
    assert left["text"] == ("3 entities were still at hold at the end (t=2); the oldest arrived "
                            "at t=0, and hold was never empty since t=0. A signal or condition "
                            "that never comes keeps entities at a HOLD for ever.")
    # Cut at 12: seize_op holds E5 (from 4) and E6 (from 5) and has not been empty since E3
    # came at 2 (E1 and E2 passed through in an instant at 0 and 1); work holds E3 since 10,
    # with E1, E2, E3 handing over in an instant at 5 and 10, so never empty since 0; seize_m
    # holds E4 since 10, never empty since E2 came at 1. Largest count first, then longest.
    answer = run(_two_resources(), run_length=12)
    left = [(f["block"], f["count"], f["t"], f["duration"]) for f in answer["facts"]
            if f["kind"] == "left_waiting"]
    assert left == [("seize_op", 2, 4.0, 10.0), ("work", 1, 10.0, 12.0), ("seize_m", 1, 10.0, 11.0)]


def test_a_truncated_trace_says_so_and_its_facts_stop_at_the_cut(monkeypatch):
    # The 30th event is E3's seize of Op at t=5 (three capacity events, then the run as
    # listed in the two-resource test); the facts read the run to there and say so last.
    monkeypatch.setattr(trace, "DEFAULT_MAX_EVENTS", 30)
    answer = run(_two_resources(), run_length=100)
    assert answer["truncated"] is True
    last = answer["facts"][-1]
    assert (last["kind"], last["t"]) == ("truncated", 5.0)
    assert last["text"].startswith("The trace hit its event cap at t=5, so these facts cover "
                                   "the run only up to t=5, not to its end at t=30.")
    lefts = [f["text"] for f in answer["facts"] if f["kind"] == "left_waiting"]
    assert lefts and all("at the end (t=5)" in text for text in lefts)


def test_each_kind_is_capped_and_the_list_is_deterministic():
    lay = layout([delay(f"b{i:02d}") for i in range(25)], [])
    found = facts([], lay, end_time=10.0)
    assert [f.block for f in found[:runfacts.MAX_PER_KIND]] == [f"b{i:02d}" for i in range(20)]
    assert found[20:] == [Fact("never_entered", "And 5 more never entered facts.", count=5)]
    assert facts([], lay, end_time=10.0) == found


def test_submodel_blocks_carry_their_instance_and_a_template_resource_is_no_fact():
    # two_cell_line clones its "$Machine" template per cell: the template stays declared
    # but idle by design, so it is no never-seized fact; blocks are named cell_N/inner.
    from nagasim.examples import model_path
    answer = run(model_path("two_cell_line"), run_length=50)
    assert not [f for f in answer["facts"] if f["kind"] == "never_seized"]
    assert all(f["block"].startswith("cell_") for f in answer["facts"] if f["block"])


# -- surfaces ---------------------------------------------------------------------

def test_naga_facts_prints_the_facts_and_exits_0_whatever_they_say(tmp_path, capsys):
    path = tmp_path / "held.json"
    path.write_text(json.dumps(_held()), encoding="utf-8")
    assert cli.main(["facts", str(path), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"model", "seed", "end_time", "truncated", "facts"}
    assert [f["kind"] for f in out["facts"]] == ["never_entered", "left_waiting"]
    assert set(out["facts"][0]) == {"kind", "text", "block", "resource", "t", "count", "duration"}
    assert cli.main(["facts", str(path), "--seed", "3"]) == 0
    text = capsys.readouterr().out
    assert "seed 3, run to t=2, 2 facts" in text
    assert "  [left_waiting] 3 entities were still at hold" in text


def test_naga_facts_exit_codes(tmp_path, capsys):
    with pytest.raises(SystemExit) as usage:
        cli.main(["facts", "--no-such-option"])
    assert usage.value.code == 2
    assert cli.main(["facts", str(tmp_path / "missing.json")]) == 2
    # A failure never ends on its own: with no run length the model cannot run.
    blocks = [create(max_arrivals=2), process("work"), dispose("out")]
    endless = tmp_path / "endless.json"
    endless.write_text(json.dumps(layout(blocks, chain(*blocks), resources=[resource_record(
        "Server")], failures=[{"name": "f", "resources": ["Server"],
                               "uptime": {"distribution": "Constant", "dist_params": {"value": 9}},
                               "downtime": {"distribution": "Constant",
                                            "dist_params": {"value": 1}}}])), encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["facts", str(endless)]) == 3
    assert "the model could not run" in capsys.readouterr().err
    assert cli.main(["facts", str(endless), "--run-length", "20"]) == 0


def test_the_run_facts_tool_answers_one_run_read_only(tmp_path):
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    (tmp_path / "held.json").write_text(json.dumps(_held()), encoding="utf-8")
    model = T.call("open_model", path="held.json")["model"]
    out = T.call("run_facts", model=model, seed=4)
    assert (out["model"], out["seed"], out["end_time"], out["truncated"]) == (model, 4, 2.0, False)
    assert [(f["kind"], f["block"], f["count"]) for f in out["facts"]] == [
        ("never_entered", "out", 0), ("left_waiting", "hold", 3)]
    json.dumps(out, allow_nan=False)
    assert T.TOOLS["run_facts"].read_only
    assert T.call("run_facts", model="nope")["error"]["code"] == "E_UNKNOWN_MODEL"
