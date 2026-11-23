"""CONVEYOR reverse during a run (conveyor system A9, D1368): the rules beyond the spec tests.

Hand derivations in each docstring. The reverse rule: at the reversal an item at x has
remaining distance x and rides back at the belt speed; a queue keeps its spacing (the spacing
of two neighbours is the occupied length of the one farther from the entry); a part that has
reached an exit (waiting there for the next block) stays committed to it.
"""
import copy
import math

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_divert_spec import belt, hog, pick
from test_conveyor_reverse import _doc, _observe, _signal

from nagasim import api, state
from nagasim.core import schema
from nagasim.core.schema import validate_model_structured

TOL = 1e-9


def _close(got, want):
    assert len(got) == len(want), (got, want)
    assert all(math.isclose(g, w, abs_tol=TOL) for g, w in zip(got, want)), (got, want)


def _signals(blocks, links, times, signal_name="rev"):
    """A SIG creator raising *signal_name* through a SIGNAL block at *times* (a first time,
    then a constant interval)."""
    gap = (times[1] - times[0]) if len(times) > 1 else 1.0
    blocks += [create(name="sig_src", e_type="SIG", max_arrivals=len(times),
                      dist_params={"value": gap}, start_time=times[0]),
               _signal(signal_name=signal_name), dispose(name="sig_out")]
    links += [{"source": "sig_src", "target": "fire"}, {"source": "fire", "target": "sig_out"}]


def _reversible(name="belt", reverse_to="dispose_back", **params):
    conveyor = belt(name=name, **params)
    conveyor["params"].update({"reversible": True, "reverse_to": reverse_to,
                               "reverse_signal": "rev"})
    return conveyor


def _deleted(env, e_type):
    return sorted(e.deletion_time for e in env.ctx.all_entities
                  if e.e_type == e_type and e.deletion_time is not None)


# -- a belt that never turns times as a plain belt ------------------------------------

def _queue_line(reversible: bool):
    """6 parts every 0.5 onto a 4.0 m belt (1 m/s, spacing 0.5) whose end feeds a slow 1.0 m
    belt (0.1 m/s, cell 0.5: two parts, 10 s each), so the first belt's end blocks and its
    parts queue."""
    first = belt(length=4.0, speed=1.0, spacing=0.5)
    blocks = [create(e_type="PART", max_arrivals=6, dist_params={"value": 0.5}, start_time=0),
              first, belt(name="slow", length=1.0, speed=0.1, spacing=0.5), dispose()]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "slow"},
             {"source": "slow", "target": "dispose_1"}]
    if reversible:
        first["params"].update({"reversible": True, "reverse_to": "dispose_back"})
        blocks.append(dispose(name="dispose_back"))
        links.append({"source": "belt", "target": "dispose_back"})
    return layout(blocks, links, simulation={"time_limit": None, "warmup": 0,
                                             "replications": 1})


def test_a_reversible_belt_that_never_turns_times_as_a_plain_belt():
    """Same exits, entries and summary as the plain belt with the same queue."""
    plain = api.run_model(_queue_line(False), replications=1)
    turning = api.run_model(_queue_line(True), replications=1)
    for column in ("Created At", "Exited At"):
        _close(sorted(turning["entity_log"][column]), sorted(plain["entity_log"][column]))
    a, b = (r["conveyor_summary"].set_index("Conveyor").loc["belt"] for r in (plain, turning))
    for column in ("Entered", "Exited"):
        assert a[column] == b[column]


# -- typed belt: two sizes queued at the end, then reversed ----------------------------

def _typed_queue():
    """Typed belt (cell 0.01, gap 0) 4.0 m at 1 m/s. BIG (0.6 m, 60 cells) enters at 0, SMALL
    (0.3 m) at 1.0, BIG at 2.0. The end hands to 'full', a one-cell belt whose part (made at 0)
    stands at its end until the hog frees 'Op' at 50, so the end blocks. Signal at 10."""
    conveyor = _reversible(length=4.0, speed=1.0, spacing=0.5)
    conveyor["params"].update({"type": "belt", "gap": 0.0})
    blocks = [conveyor, belt(name="full", length=1.0, speed=1.0, spacing=1.0,
                             exit_resource="Op"),
              pick("Op"), dispose(), dispose(name="dispose_back"),
              create(name="big_src", e_type="BIG", max_arrivals=2, dist_params={"value": 2.0},
                     start_time=0),
              create(name="small_src", e_type="SMALL", max_arrivals=1, start_time=1.0),
              create(name="fill_src", e_type="FILL", max_arrivals=1, start_time=0)]
    links = [{"source": "belt", "target": "full"}, {"source": "belt", "target": "dispose_back"},
             {"source": "full", "target": "pick"}, {"source": "pick", "target": "dispose_1"},
             {"source": "big_src", "target": "belt"}, {"source": "small_src", "target": "belt"},
             {"source": "fill_src", "target": "full"}]
    hog_blocks, hog_links = hog("Op", 50.0)
    _signals(blocks, links, [10.0])
    doc = layout(blocks + hog_blocks, links + hog_links,
                 simulation={"time_limit": None, "warmup": 0, "replications": 1},
                 resources=[{"name": "Op", "capacity": 1}])
    doc["entities"] = [{"name": "BIG", "size": {"length": 0.6, "width": 0.4, "height": 0.2}},
                       {"name": "SMALL", "size": {"length": 0.3, "width": 0.3, "height": 0.2}}]
    return doc


def test_typed_queue_keeps_its_spacing_when_it_turns():
    """Forward: BIG1 at the end at 4.0, held ('full' is full), committed to the end. SMALL packs
    a BIG's 0.6 behind it, at x = 3.4 (from 4.4); BIG2 a SMALL's 0.3 behind that, at 3.1 (from
    5.1). Reversed at 10: BIG2 leads, 3.1 to the start: 13.1. SMALL follows nose to tail, its
    own 0.3 behind (x 3.4 - 3.1), so it reaches the start at 13.4 (a lead's 0.6 would give 13.7).
    BIG1 leaves by the end when 'full' takes it at 50: Op frees at 50, FILL leaves 'full'."""
    env, _model = run_env(_typed_queue(), 100)
    _close(_deleted(env, "SMALL"), [13.4])
    _close(_deleted(env, "BIG"), [13.1, 51.0])
    flows = env.ctx.conveyors["belt"]
    assert flows.exited == 3 and flows._rev.turns == 1


# -- non-accumulating: the belt stands while its exit's part waits ------------------------

def test_non_accumulating_belt_stands_then_runs_backward():
    """Non-accumulating 4.0 m belt, parts at 0 and 1.0, end to the held one-cell belt 'full'.
    P1 at the end at 4.0 waits: the belt stands with P2 at x 3.0. Reversed at 10: the start is
    not held, so it runs backward; P2 reaches the start at 13.0. P1 leaves by the end at 50."""
    doc = _typed_queue()
    conveyor = next(b for b in doc["blocks"] if b["name"] == "belt")
    del conveyor["params"]["type"], conveyor["params"]["gap"]
    conveyor["params"]["accumulating"] = False
    doc["entities"] = []
    for block in doc["blocks"]:
        if block["name"] == "big_src":
            block["params"].update({"e_type": "PART", "max_arrivals": 1})
        if block["name"] == "small_src":
            block["params"]["e_type"] = "PART"
    env, _model = run_env(doc, 100)
    _close(_deleted(env, "PART"), [13.0, 51.0])


# -- the exit stop works at whichever exit the belt runs to ---------------------------------

def test_exit_resource_holds_the_part_at_the_start_while_reversed():
    """As test_reverse_mid_run (at the start at 3.0) but the belt's exit_resource 'Gate' is
    held by a hog until 5.0: the part waits at the start and leaves at 5.0."""
    doc = _doc([0.0], [1.5])
    conveyor = next(b for b in doc["blocks"] if b["name"] == "belt")
    conveyor["params"]["exit_resource"] = "Gate"
    hog_blocks, hog_links = hog("Gate", 5.0)
    doc["blocks"] += hog_blocks + [pick("Gate")]
    doc["blocks"] = [b for b in doc["blocks"] if b["name"] != "n_back"] + [
        {"name": "n_back", "description": "n_back", "type": "ASSIGN", "position": [1, 1],
         "params": {"assignments": [{"target": "n_back", "expr": "n_back + 1"}]}}]
    doc["links"] = [k for k in doc["links"] if k["source"] != "n_back"] + hog_links + [
        {"source": "n_back", "target": "pick"}, {"source": "pick", "target": "dispose_back"}]
    doc["resources"] = [{"name": "Gate", "capacity": 1}]
    times, n_end, n_back = _observe(doc)
    _close(times, [5.0])
    assert (n_end, n_back) == (0, 1)


# -- reverse_to full: blocking and the deadlock name the start's next block ------------------

_XFER = {"name": "xfer", "description": "xfer", "type": "CONVEYOR_TRANSFER", "position": [1, 0],
         "params": {"mechanism": "popup", "transfer_speed": 1.0,
                    "exits": [{"port": "straight", "angle": 0}]}}


def _two_belts(loop: bool, arrivals: int = 2, time_limit=None):
    """'belt' (4.0 m, 1 m/s, reversible, reverse_to 'back') gets part P at 0 and turns at 1.5.
    'back' is a one-cell belt (1.0 m). With *loop* its end feeds 'belt' again through a straight
    pop-up transfer (R48: a mechanism between the pieces) and Q parts enter it every 1.0 from
    1.5 (*arrivals*, 0 open-ended); else its part Q stands at its end waiting for 'Op' (hog
    until 50) and leaves by pick."""
    blocks = [_reversible(reverse_to="back", length=4.0, speed=1.0, spacing=0.5),
              belt(name="back", length=1.0, speed=1.0, spacing=1.0,
                   exit_resource="" if loop else "Op"),
              dispose(), create(e_type="P", max_arrivals=1, start_time=0)]
    links = [{"source": "belt", "target": "dispose_1"}, {"source": "belt", "target": "back"},
             {"source": "create_1", "target": "belt"}]
    if loop:
        blocks += [create(name="q_src", e_type="Q", max_arrivals=arrivals,
                          dist_params={"value": 1.0}, start_time=1.5), dict(_XFER)]
        links += [{"source": "q_src", "target": "back"}, {"source": "back", "target": "xfer"},
                  {"source": "xfer", "target": "belt", "source_port": "straight"}]
        hog_blocks, hog_links = [], []
    else:
        blocks += [create(name="q_src", e_type="Q", max_arrivals=1, start_time=0), pick("Op")]
        links += [{"source": "q_src", "target": "back"}, {"source": "back", "target": "pick"},
                  {"source": "pick", "target": "dispose_1"}]
        hog_blocks, hog_links = hog("Op", 50.0)
    _signals(blocks, links, [1.5])
    return layout(blocks + hog_blocks, links + hog_links,
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1,
                              "time_unit": "seconds"},
                  resources=[] if loop else [{"name": "Op", "capacity": 1}])


def test_a_full_reverse_target_holds_the_part_at_the_start():
    """P at x 1.5 at the turn reaches the start at 3.0; 'back' is full (Q stands at its end
    until Op frees at 50), so P waits at the start, holding 'belt' blocked by 'back'. At 50 Q
    leaves, P boards 'back', rides 1.0 m and leaves at 51."""
    doc = _two_belts(loop=False)
    env, model = run_env(doc, 10)
    conveyor = env.ctx.conveyors["belt"]
    assert conveyor.held_downstream and conveyor.exit_blocks == {True: False, False: True}
    assert conveyor._rev.standing[False] is not None
    assert conveyor.blocked.level == 1.0
    env, _model = run_env(doc, 100)
    _close(_deleted(env, "P"), [51.0])


def test_a_reversed_belt_and_its_full_reverse_target_deadlock_by_name():
    """'back' feeds 'belt' again through the transfer. Q1 reaches back's end at 2.5 and the
    module takes it, held: 'belt' runs backward. Q2 boards 'back' at 2.5. P reaches belt's start
    at 3.0 and waits for 'back' (full); Q2 waits at back's end for the module from 3.5. Nothing
    else can happen: since T11 (R45) the live check names the loop at 3.5, as Q2 blocks, since
    nothing boards 'belt' while P waits at its start, whichever way it runs."""
    with pytest.raises(Exception, match=r"deadlock at t=3.5: .*loop back -> xfer -> belt -> back"):
        api.run_model(_two_belts(loop=True), replications=1)


def test_an_open_loop_with_a_reversed_hold_is_a_live_deadlock():
    """R45 (T11): the same loop with Q parts arriving for ever: the queue never drains, and the
    live check names the loop at 3.5 instead of the run going on to its time limit."""
    with pytest.raises(Exception, match=r"deadlock at t=3.5: .*loop back -> xfer -> belt -> back"):
        api.run_model(_two_belts(loop=True, arrivals=0, time_limit=100), replications=1)


def test_a_reversed_hold_open_at_a_time_limited_runs_end_is_warned(caplog):
    """R45: no loop: P waits at belt's start for 'back' (Q stands at its end until Op frees at
    50); a run of 10 ends with that hold open and warns, naming the belt, since when it runs
    backward (1.5) and since when its start holds P (3.0)."""
    with caplog.at_level("WARNING", logger="nagasim.core.blocking"):
        api.run_model(_two_belts(loop=False, time_limit=10), replications=1)
    assert any("Belt 'belt' has run backward since t=1.5" in r.message and "since t=3," in r.message
               for r in caplog.records), [r.message for r in caplog.records]


@pytest.mark.parametrize("between", [False, True])
def test_a_reverse_target_that_feeds_the_belt_is_refused(between):
    """R48: 'back' feeding 'belt' straight (or through an ASSIGN) is refused: nothing boards a
    belt by its end, so a part sent back there would board 'belt' at its start."""
    doc = _two_belts(loop=True)
    doc["links"] = [k for k in doc["links"] if k["source"] not in ("back", "xfer")]
    doc["blocks"] = [b for b in doc["blocks"] if b["name"] != "xfer"]
    if between:
        doc["blocks"].append({"name": "tag", "description": "tag", "type": "ASSIGN",
                              "position": [1, 1], "params": {"assignments": []}})
        doc["links"] += [{"source": "back", "target": "tag"}, {"source": "tag", "target": "belt"}]
    else:
        doc["links"].append({"source": "back", "target": "belt"})
    found = _problems(doc)
    assert [p.fields["case"] for p in found] == ["feeder"]
    assert found[0].hint == "put a mechanism between the pieces"


def test_reverse_to_in_needs_a_link_whose_port_is_in():
    """R47: "in" with no link naming source_port "in" is refused at load."""
    doc = _doc([0.0], [1.5])
    next(b for b in doc["blocks"] if b["name"] == "belt")["params"]["reverse_to"] = "in"
    assert [(p.fields["case"], p.fields["target"]) for p in _problems(doc)] == [("target", "in")]


def test_a_text_flag_is_refused_not_read_as_false():
    doc = _doc([0.0], [1.5])
    next(b for b in doc["blocks"] if b["name"] == "belt")["params"]["reversible"] = "true"
    assert "flag" in [p.fields["case"] for p in _problems(doc)]


# -- the transfer's no-eye handshake ends at the reversal ---------------------------------

def _transfer_doc():
    """CREATE (two 0.41 m BOXes at 0) -> straight popup (no lift, no cross) -> reversible 'recv'
    (5.0 m, 0.5 m/s, cell 0.41, no eye), end to dispose_1, reverse_to dispose_back. Signals at
    0.5 (back) and 2.0 (forward)."""
    xfer = {"name": "xfer", "description": "xfer", "type": "CONVEYOR_TRANSFER",
            "position": [1, 0], "params": {"mechanism": "popup", "transfer_speed": 0.5,
                                           "exits": [{"port": "straight", "angle": 0}]}}
    blocks = [create(e_type="BOX", entities_per_arrival=2, max_arrivals=1, start_time=0), xfer,
              _reversible(name="recv", length=5.0, speed=0.5, spacing=0.41), dispose(),
              dispose(name="dispose_back")]
    links = [{"source": "create_1", "target": "xfer"},
             {"source": "xfer", "target": "recv", "source_port": "straight"},
             {"source": "recv", "target": "dispose_1"},
             {"source": "recv", "target": "dispose_back"}]
    _signals(blocks, links, [0.5, 2.0])
    doc = layout(blocks, links, simulation={"time_limit": None, "warmup": 0, "replications": 1,
                                            "time_unit": "seconds"})
    doc["entities"] = [{"name": "BOX", "size": {"length": 0.41, "width": 0.3, "height": 0.2}}]
    return doc


def test_a_transfer_waiting_on_the_no_eye_handshake_is_freed_at_the_reversal():
    """Load 1 boards 'recv' at 0; its handshake (0.41 / 0.5 = 0.82) would end at 0.82, but the
    belt turns at 0.5: the module frees then and takes load 2, which waits on it (refused while
    reversed). Load 1 (x 0.25) leaves at the start at 1.0. Forward at 2.0: load 2 boards and
    leaves the 5.0 m belt's end at 12.0."""
    doc = _transfer_doc()
    env, model = run_env(doc, 0.6)
    assert model.blocks["xfer"].blocked.level == 1      # load 2 on the module, held
    env, _model = run_env(doc, 30)
    _close(_deleted(env, "BOX"), [1.0, 12.0])


# -- exits: order, names, refusals ------------------------------------------------------------

def test_the_reverse_link_may_be_drawn_first():
    """test_reverse_mid_run and test_forward_only_control with the two links swapped: the end is
    still the link that is not reverse_to."""
    for signals, want, path in (([1.5], [3.0], (0, 1)), ([], [4.0], (1, 0))):
        doc = _doc([0.0], signals)
        links = doc["links"]
        links[0], links[2] = links[2], links[0]
        times, n_end, n_back = _observe(doc)
        _close(times, want)
        assert (n_end, n_back) == path


def test_reverse_to_in_names_the_link_whose_port_is_in():
    doc = _doc([0.0], [1.5])
    conveyor = next(b for b in doc["blocks"] if b["name"] == "belt")
    conveyor["params"]["reverse_to"] = "in"
    back = next(k for k in doc["links"] if k["source"] == "belt" and k["target"] == "n_back")
    back["source_port"] = "in"
    doc["links"].remove(back)
    doc["links"].insert(0, back)
    times, n_end, n_back = _observe(doc)
    _close(times, [3.0])
    assert (n_end, n_back) == (0, 1)


def _problems(doc, code="E_CONVEYOR_REVERSE"):
    doc = copy.deepcopy(doc)
    doc["schema_version"] = schema.CURRENT_VERSION
    return [p for p in validate_model_structured(doc) if p.code == code]


@pytest.mark.parametrize("params,case", [
    ({"eyes": [{"at": 2.0}]}, "photo-eyes"),
    ({"diverts": [{"at": 2.0, "condition": "True", "intercept": True}]}, "robot picks"),
    ({"reverse_to": ""}, "no_target"),
    ({"reverse_to": "nowhere"}, "target"),
])
def test_refusals_by_case(params, case):
    doc = _doc([0.0], [1.5])
    next(b for b in doc["blocks"] if b["name"] == "belt")["params"].update(params)
    found = _problems(doc)
    assert case in [p.fields.get("what", p.fields["case"]) for p in found], [p.fields for p in found]
    assert all(p.severity == "error" for p in found)


def test_a_port_other_than_out_or_in_is_refused_on_a_reversible_belt():
    doc = _doc([0.0], [1.5])
    doc["links"][0]["source_port"] = "d0"
    assert [p.fields["case"] for p in _problems(doc, "E_CONVEYOR_DIVERT")] == ["a3_port"]


def test_the_run_refuses_a_reversible_belt_with_stops_when_validation_is_skipped():
    doc = _doc([0.0], [1.5])
    env, model = run_env(doc, 0)
    conveyor = env.ctx.conveyors["belt"]
    conveyor.stops = [(2.0, 0.0, "")]
    conveyor._space = None
    with pytest.raises(RuntimeError, match="E_CONVEYOR_REVERSE"):
        conveyor.space          # noqa: B018


# -- params, snapshots, item code ----------------------------------------------------------

def test_params_round_trip():
    doc = _doc([0.0], [1.5])
    env, _model = run_env(doc, 0)
    params = env.ctx.conveyors["belt"].get_params_dict()
    assert (params["reversible"], params["reverse_signal"], params["reverse_to"]) == \
        (True, "rev", "n_back")
    plain = belt()
    env, _model = run_env(layout([create(max_arrivals=1), plain, dispose()],
                                 [{"source": "create_1", "target": "belt"},
                                  {"source": "belt", "target": "dispose_1"}]), 0)
    assert "reversible" not in env.ctx.conveyors["belt"].get_params_dict()


def test_a_snapshot_refuses_a_reversible_belt_by_name():
    doc = _doc([0.0], [1.5])
    doc["simulation"]["time_limit"] = 30
    assert state.refused_kind(doc) == "a reversible conveyor"


def test_set_direction_refuses_a_belt_that_cannot_reverse():
    env, _model = run_env(layout([create(max_arrivals=1), belt(), dispose()],
                                 [{"source": "create_1", "target": "belt"},
                                  {"source": "belt", "target": "dispose_1"}]), 0)
    with pytest.raises(ValueError, match="not reversible"):
        env.ctx.conveyors["belt"].set_direction(False)


def test_the_same_direction_again_changes_nothing():
    """Signals at 1.0 and 1.5 reverse and go forward again (test_reverse_twice: 5.0). Calling
    set_direction(True) while it runs forward adds no turn."""
    env, _model = run_env(_doc([0.0], [1.0, 1.5]), 0.5)
    conveyor = env.ctx.conveyors["belt"]
    conveyor.set_direction(True)
    assert conveyor._rev.turns == 0


# -- playback (R46) -----------------------------------------------------------------------------

def _belt_rows(doc, *times):
    from nagasim.core.playback import Timeline
    from nagasim.core.trace import TraceRecorder
    recorder = TraceRecorder()
    api.run_model(doc, replications=1, trace=recorder)
    timeline = Timeline(recorder.events)
    return [[(x, stage) for _id, x, stage in timeline.state_at(t)["belts"].get("belt", [])]
            for t in times]


def test_playback_draws_a_queue_packed_at_the_start_after_a_turn():
    """Three parts enter at 0, 0.5, 1.0; at 1.2 they are at 1.2, 0.7, 0.2 (end first). The belt
    turns at 1.5 (1.5, 1.0, 0.5) and 'back' is full until 50: the part nearest the start gets
    there at 2.0 and waits, the others pack its own spacing apart, at 0.5 and 1.0 (from 2.0)."""
    doc = _two_belts(loop=False)
    next(b for b in doc["blocks"] if b["name"] == "create_1")["params"].update(
        {"max_arrivals": 3, "dist_params": {"value": 0.5}})
    before, after = _belt_rows(doc, 1.2, 10.0)
    assert [x for x, _stage in before] == pytest.approx([1.2, 0.7, 0.2])
    assert [x for x, _stage in after] == pytest.approx([0.0, 0.5, 1.0])
    assert after[0][1] == "handover"


def test_playback_draws_a_non_accumulating_stand_halted():
    """As test_non_accumulating_belt_stands_then_runs_backward: at 8 the belt stands with P2 at
    3.0 (drawn halted, as on a plain belt) and P1 at the end; at 11, turned at 10, P2 is at 2.0."""
    doc = _typed_queue()
    conveyor = next(b for b in doc["blocks"] if b["name"] == "belt")
    del conveyor["params"]["type"], conveyor["params"]["gap"]
    conveyor["params"]["accumulating"] = False
    doc["entities"] = []
    for block in doc["blocks"]:
        if block["name"] == "big_src":
            block["params"].update({"e_type": "PART", "max_arrivals": 1})
        if block["name"] == "small_src":
            block["params"]["e_type"] = "PART"
    stand, back = _belt_rows(doc, 8.0, 11.0)
    assert stand == [(pytest.approx(4.0), "handover"), (pytest.approx(3.0), "halted")]
    assert (pytest.approx(2.0), "ride") in back
