"""A reversible chain (conveyor A2 task 7, D1392): waits at a joint, one toggle per raise,
the chain start's R45 warning, a loop through a chain, playback, typed pieces and refusals.
The spec cases are in test_conveyor_reverse_chain.py; these use the same 4 m + 4 m chain."""
import copy

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_divert_spec import belt, hog, pick
from test_conveyor_reverse import _assign, _close
from test_conveyor_reverse_chain import _chain_doc, _observe
from test_conveyor_reverse_rules import _XFER, _belt_rows, _deleted, _signals

from nagasim import api
from nagasim.core import schema
from nagasim.core.schema import validate_model_structured


def _block(doc, name):
    return next(b for b in doc["blocks"] if b["name"] == name)


def test_the_same_signal_on_both_pieces_turns_the_chain_once():
    """A and B both name 'rev': one raise at 5 turns the chain once, as the mid-run spec case."""
    doc = _chain_doc([0.0, 2.0], [5.0])
    _block(doc, "B")["params"]["reverse_signal"] = "rev"
    times, n_end, n_back = _observe(doc)
    _close(times, [8.0, 10.0])
    assert (n_end, n_back) == (0, 2)


def _full_joint():
    """A 4 m -> B 1 m (one cell, spacing 1.0, exit stop Op, held by a hog until 50) -> pick.
    P1 enters at 0, P2 at 1, signal at 6."""
    doc = _chain_doc([0.0, 1.0], [6.0])
    b = _block(doc, "B")
    b["params"].update({"length": 1.0, "spacing": 1.0, "exit_resource": "Op"})
    doc["blocks"].append(pick("Op"))
    doc["links"] = [k for k in doc["links"] if k["source"] != "B"]
    doc["links"] += [{"source": "B", "target": "pick"}, {"source": "pick", "target": "n_end"}]
    hog_blocks, hog_links = hog("Op", 50.0)
    doc["blocks"] += hog_blocks
    doc["links"] += hog_links
    doc["resources"] = [{"name": "Op", "capacity": 1}]
    return doc


def test_a_part_waiting_at_a_full_joint_rides_back_at_the_turn():
    """P1 crosses at 4, reaches B's end at 5 and waits there for Op (until 50), holding B's one
    cell. P2 reaches the joint at 5: no room on B, so it waits there, A held toward B. At 6 the
    chain turns: P2 is no exit's, so it rides back 4 m and leaves A's start at 10; P1 is
    committed to B's end and leaves at 50."""
    doc = _full_joint()
    env, _model = run_env(doc, 5.5)
    a = env.ctx.conveyors["A"]
    assert a._rev.joint_held("B") and a.held_downstream and a.blocked.level == 1.0
    env, _model = run_env(doc, 100)
    _close(_deleted(env, "PART"), [10.0, 50.0])
    assert (env.ctx.variables["n_end"], env.ctx.variables["n_back"]) == (1, 1)
    assert not env.ctx.conveyors["A"]._rev.joint_held()


def _full_start(time_limit):
    """The chain's reverse_to is 'back', a one-cell belt whose part Q (made at 0) stands at its
    end until Op frees at 50. Parts at 0 and 2, signal at 5."""
    doc = _chain_doc([0.0, 2.0], [5.0])
    _block(doc, "A")["params"]["reverse_to"] = "back"
    doc["blocks"] += [belt(name="back", length=1.0, speed=1.0, spacing=1.0, exit_resource="Op"),
                      pick("Op"), create(name="q_src", e_type="Q", max_arrivals=1, start_time=0)]
    doc["links"] = [k for k in doc["links"] if k["target"] != "n_back"]
    doc["links"] += [{"source": "A", "target": "back"}, {"source": "q_src", "target": "back"},
                     {"source": "back", "target": "pick"}, {"source": "pick", "target": "n_back"}]
    hog_blocks, hog_links = hog("Op", 50.0)
    doc["blocks"] += hog_blocks
    doc["links"] += hog_links
    doc["resources"] = [{"name": "Op", "capacity": 1}]
    doc["simulation"]["time_limit"] = time_limit
    return doc


def test_a_held_chain_start_is_warned_on_the_first_piece_only(caplog):
    """P2 reaches A's start at 8 and waits for 'back' (full). P1 crosses onto A at 6 and packs
    behind P2. A run of 20 warns for A (backward since 5, start held since 8), none for B."""
    with caplog.at_level("WARNING", logger="nagasim.core.blocking"):
        api.run_model(_full_start(20), replications=1)
    said = [r.message for r in caplog.records]
    assert any("Belt 'A' has run backward since t=5 with a part held at its start since t=8,"
               in m for m in said), said
    assert not any("Belt 'B'" in m for m in said), said


def test_a_loop_through_a_chain_deadlocks_by_name():
    """A (1 m) -> B (1 m), one cell each, B's end feeds A again through a pop-up transfer. Three
    parts fill A, B and the module; the live check names the loop through both pieces."""
    doc = _chain_doc([0.0, 1.0, 2.0], [])
    for name in ("A", "B"):
        _block(doc, name)["params"].update({"length": 1.0, "spacing": 1.0})
    doc["blocks"].append(dict(_XFER))
    doc["links"] = [k for k in doc["links"] if k["source"] != "B"]
    doc["links"] += [{"source": "B", "target": "xfer"},
                     {"source": "xfer", "target": "A", "source_port": "straight"}]
    with pytest.raises(Exception, match=r"deadlock at t=.*loop .*A -> B"):
        api.run_model(doc, replications=1)


def test_playback_draws_parts_crossing_onto_an_empty_reversed_piece():
    """Parts at 0 and 1, signal at 6.5: both on B (x 2.5 and 1.5 on B), A empty. Reversed, P2
    crosses onto A at 8 and P1 at 9. At 10 A draws them nearest its start first, at 2.0 and 3.0
    (a forward clamp would put P1 at 1.5); they leave A's start at 12 and 13."""
    doc = _chain_doc([0.0, 1.0], [6.5])
    times, _n_end, n_back = _observe(doc)
    _close(times, [12.0, 13.0])
    assert n_back == 2
    from nagasim.core.playback import Timeline
    from nagasim.core.trace import TraceRecorder
    recorder = TraceRecorder()
    api.run_model(doc, replications=1, trace=recorder)
    belts = Timeline(recorder.events).state_at(10.0)["belts"]
    assert [x for _id, x, _stage in belts["A"]] == pytest.approx([2.0, 3.0])
    assert "B" not in belts or not belts["B"]


def test_typed_pieces_cross_by_their_cells():
    """Typed A and B (cell 0.01, gap 0): BIG (0.6 m) enters at 0, SMALL (0.3 m) at 1.0. Signal
    4.5: BIG at x 4.5 (0.5 on B), SMALL at 3.5 on A. Reversed: SMALL leaves A's start at 8.0;
    BIG crosses onto A at 5.0 (SMALL 1.5 m from A's end, more than BIG's 0.6) and leaves at 9.0."""
    doc = _chain_doc([0.0], [4.5])
    for name in ("A", "B"):
        _block(doc, name)["params"].update({"type": "belt", "gap": 0.0})
    _block(doc, "parts")["params"]["e_type"] = "BIG"
    doc["blocks"].append(create(name="small_src", e_type="SMALL", max_arrivals=1,
                                start_time=1.0))
    doc["links"].append({"source": "small_src", "target": "A"})
    doc["entities"] = [{"name": "BIG", "size": {"length": 0.6, "width": 0.4, "height": 0.2}},
                       {"name": "SMALL", "size": {"length": 0.3, "width": 0.3, "height": 0.2}}]
    env, _model = run_env(doc, 40)
    _close(_deleted(env, "SMALL"), [8.0])
    _close(_deleted(env, "BIG"), [9.0])


# -- validation -----------------------------------------------------------------------------

def _cases(doc):
    doc["schema_version"] = schema.CURRENT_VERSION
    return [p.fields.get("case") for p in validate_model_structured(doc)
            if p.code == "E_CONVEYOR_REVERSE" and p.severity == "error"]


def test_reverse_to_on_a_later_piece_is_refused():
    doc = _chain_doc([0.0], [5.0])
    _block(doc, "B")["params"]["reverse_to"] = "n_end"
    assert _cases(doc) == ["member"]


def test_a_ring_of_reversible_pieces_is_refused():
    doc = _chain_doc([0.0], [5.0])
    doc["links"] = [k for k in doc["links"] if k["source"] != "B"]
    doc["links"].append({"source": "B", "target": "A"})
    assert "ring" in _cases(doc)


def test_a_delay_at_the_joint_makes_two_belts_not_a_chain():
    """A link with a delay is not a direct joint: B is a belt of its own and needs reverse_to."""
    doc = _chain_doc([0.0], [5.0])
    next(k for k in doc["links"] if k["source"] == "A" and k["target"] == "B")["delay"] = {
        "distribution": "Constant", "dist_params": {"value": 1.0}}
    assert _cases(copy.deepcopy(doc)) == ["no_target"]
