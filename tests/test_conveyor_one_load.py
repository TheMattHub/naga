"""One-load CONVEYOR pieces (plan F1, #2054, #2055): the Review Focus rigs, written from the
rule text before the build. Every expected time is derived by hand in the test's docstring.

The rule (plan F1 rulings 2 to 5): with `one_load: true` a load boards the piece, at its start
or at a merge, only when the piece is clear; the piece is clear at
t_leave + (L - x + occ) / v, with L the piece length, x the place the load left by (L at the
end, the divert's `at` for a side exit), occ the load's occupied length (the spacing on an
untyped belt; on a typed belt its cells, here gap 0 and 1 cm cells, so its size length) and v
the speed. t_leave is the real leave time, so a load held at the exit holds the piece.

Assumption used throughout: a waiting load boards at the clear instant itself, with no added
time. No rig lets a load reach an input or a divert at exactly a clear instant, since the rule
text does not order two events at one time.
"""

import copy
import tempfile

import pytest

from conftest import create, dispose, run_env
from test_conveyor_divert_spec import (belt, belt_row, divert_at, divert_row, exits, hog, model,
                                       pick, plain_line, run)
from test_conveyor_types import sized

from nagasim import api, state

def one_load(conveyor):
    conveyor["params"]["one_load"] = True
    return conveyor


# -- Review Focus 1: a "pass" divert reading a busy one-load piece ------------------------------

# Exits of the DEFAULT parts: P0 by the lane, P2 by the lane, P1 by the main belt's end.
EXITS_PASS_DIVERT = [6.0, 11.0, 12.5]


def test_a_pass_divert_rides_past_a_busy_one_load_piece():
    """Main belt 10 m at 1 m/s, spacing 1, a divert at 4 (condition True, when_full "pass") into
    `lane`: a one-load CONVEYOR 2 m at 1 m/s, spacing 1 (occ 1), then a DISPOSE. Three parts
    at 0, 2.5, 5.0 (the main belt's own 1 s rhythm never binds).
      P0 reaches 4 at 4; the lane is clear, it diverts and boards the lane at 4, leaves its end
         at 4 + 2 = 6.0; the lane is clear at 6 + (2 - 2 + 1) / 1 = 7.0.
      P1 reaches 4 at 6.5: P0 has left the lane (6.0) but its tail is not past the end until
         7.0, so the lane has no room (room_now says no); "pass" rides P1 on to the main end at
         2.5 + 10 = 12.5.
      P2 reaches 4 at 9.0: the lane is clear, it diverts and leaves the lane at 11.0.
    Without one_load the lane has two cells and P1 would board it at 6.5 (lane exit 8.5): the
    case pins "busy until clear", not "busy until the load has left".
    Divert counts: Diverted 2, Skipped 1, Passed 0."""
    main = belt(diverts=[divert_at(4, when_full="pass")])
    lane = one_load(belt(name="lane", length=2.0))
    blocks = [create(max_arrivals=3, dist_params={"value": 2.5}, start_time=0), main, lane,
              dispose()]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"},
             {"source": "belt", "target": "lane"}, {"source": "lane", "target": "dispose_1"}]
    results = run(blocks, links)
    assert exits(results) == pytest.approx(EXITS_PASS_DIVERT, abs=1e-9)
    stats = divert_row(results)
    assert (stats["Diverted"], stats["Skipped"], stats["Passed"]) == (2, 1, 0)
    assert belt_row(results, "lane")["Exited"] == 2


# -- Review Focus 2: a load held at the piece's exit -------------------------------------------

EXITS_HELD_EXIT = [10.0, 12.82, 15.64]


def test_a_held_exit_holds_the_piece():
    """A one-load belt 1.0 m at 0.5 m/s, spacing 0.41 (occ 0.41), whose end seizes Gate; a hog
    holds Gate from 0 to 10; the belt goes on to `pick` (gives Gate back at once) and a DISPOSE.
    Boxes at 0, 1, 2.
      Box 1 boards 0, reaches the end at 1.0 / 0.5 = 2.0, waits for Gate until 10 and is handed
         over at 10.0 (its exit; pick takes no time). The piece is clear at
         10 + (1.0 - 1.0 + 0.41) / 0.5 = 10.82 (the real leave time, not the free-ride 2.0).
      Box 2 (waiting since 1) boards at the hand-over + 0.82 = 10.82, reaches the end at 12.82;
         Gate is free (box 1 gave it back at 10), so it leaves at 12.82; clear 13.64.
      Box 3 (waiting since 2) boards 13.64, leaves 15.64.
    Without one_load box 2 packs behind box 1 and leaves at 10 + 0.82 = 10.82."""
    hog_blocks, hog_links = hog("Gate", 10.0)
    conveyor = one_load(belt(length=1.0, speed=0.5, spacing=0.41, exit_resource="Gate"))
    blocks = [create(max_arrivals=3, dist_params={"value": 1.0}, start_time=0), conveyor,
              pick("Gate"), dispose(), *hog_blocks]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "pick"},
             {"source": "pick", "target": "dispose_1"}, *hog_links]
    results = run(blocks, links, resources=("Gate",))
    assert exits(results) == pytest.approx(EXITS_HELD_EXIT, abs=1e-9)


# -- Review Focus 3: mixed load lengths on a typed piece ---------------------------------------

EXITS_OWN_LENGTH = {"SHORT": [3.0, 11.42], "LONG": [6.82, 15.24]}


def test_each_load_clears_by_its_own_length():
    """A typed ("belt") one-load piece 1.5 m at 0.5 m/s, gap 0, default 1 cm cells. SHORT is
    sized 0.41 m (41 cells: occ 0.41), LONG 0.8 m (80 cells: occ 0.8). Four single-load
    CREATEs into the start at 0, 0.1, 0.2, 0.3: SHORT, LONG, SHORT, LONG (one input, FIFO by
    arrival). Every load rides 1.5 / 0.5 = 3.0 and leaves by the end (x = L).
      SHORT 1 boards 0, leaves 3.0, clear 3.0 + 0.41 / 0.5 = 3.82.
      LONG 1 boards 3.82, leaves 6.82, clear 6.82 + 0.8 / 0.5 = 8.42.
      SHORT 2 boards 8.42, leaves 11.42, clear 12.24.
      LONG 2 boards 12.24, leaves 15.24.
    Clearing by the next load's length instead would board LONG 1 at 4.6 (exit 7.6); by a fixed
    0.41 it would board SHORT 2 at 7.64 (exit 10.64)."""
    conveyor = one_load(belt(length=1.5, speed=0.5, spacing=0.41))
    conveyor["params"].update({"type": "belt", "gap": 0.0})
    kinds = ["SHORT", "LONG", "SHORT", "LONG"]
    creates = [create(name=f"c{i}", e_type=kind, max_arrivals=1, start_time=0.1 * i)
               for i, kind in enumerate(kinds)]
    blocks = [*creates, conveyor, dispose()]
    links = [{"source": f"c{i}", "target": "belt"} for i in range(len(kinds))]
    links.append({"source": "belt", "target": "dispose_1"})
    doc = model(blocks, links)
    doc["entities"] = [sized("SHORT", 0.41), sized("LONG", 0.8)]
    results = api.run_model(doc, replications=1)
    for kind, expected in EXITS_OWN_LENGTH.items():
        assert exits(results, kind) == pytest.approx(expected, abs=1e-9), kind


# -- Review Focus 4: settings that cannot work --------------------------------------------------

def _zones():
    doc = plain_line(belt(), arrivals=3)
    doc["blocks"][1]["params"]["zones"] = {"count": 2}
    return doc


def _reversible():
    from test_conveyor_reverse import _doc
    return _doc([0.0], [1.5])


def _intercept():
    from robotcases import gantry_pick_cell
    return gantry_pick_cell()


def _slug():
    doc = plain_line(belt(), arrivals=3)
    doc["blocks"][1]["params"]["release"] = {"slug": {"count": 2, "timeout": 100.0}}
    return doc


REFUSED = {"zones": _zones, "reversible": _reversible, "intercept": _intercept, "slug": _slug}


def _with_one_load(doc):
    doc = copy.deepcopy(doc)
    one_load(next(b for b in doc["blocks"] if b["name"] == "belt"))
    return doc


def _build(doc):
    """Builds the run past validation: `run_env` validates first, and a refusal the validator
    also knows comes out as ModelValidationError without its code in the message (checked on
    zones with stops), so the build's own E_CONVEYOR_PARAM is reached this way."""
    from nagasim.core.schema import prepare
    from nagasim.core.sim_runner import setup_run, step_until
    flat, _problems = prepare(doc)
    env, _built = setup_run(flat, seed=0, run_len=50, warmup=0.0, prepared=True)
    step_until(env, 50)


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_settings_that_cannot_work_are_refused(case):
    """Ruling 5: one_load with zones, reversible, an intercept divert or a slug of count 2 is
    refused with E_CONVEYOR_PARAM when the belt is built (RuntimeError, as the build's other
    E_CONVEYOR_PARAM refusals). Each rig runs as it is without one_load (checked first), so the
    refusal can only come from one_load."""
    doc = REFUSED[case]()
    run_env(copy.deepcopy(doc), 50)
    with pytest.raises(RuntimeError, match="E_CONVEYOR_PARAM"):
        _build(_with_one_load(doc))


def test_a_state_refuses_a_one_load_piece():
    """Ruling 5: a state refuses a one-load piece with E_STATE_UNSUPPORTED. A plain untyped belt
    (10 m, 1 m/s, spacing 1) with only one_load, three parts at 0, 1, 2, time limit 30, taken
    at 1.1: nothing is due then (one_load: part 0 boards 0, leaves 10; the others wait) and the
    run has not ended, so those two refusals with the same code are ruled out; the same doc
    without one_load takes fine at 1.1. Entry point: `state.take` (and `refused_kind` names a
    kind); the kind text is not asserted."""
    doc = plain_line(belt(), arrivals=3)
    doc["simulation"]["time_limit"] = 30
    assert state.take(copy.deepcopy(doc), 1.1)["state"]
    doc = _with_one_load(doc)
    with pytest.raises(state.StateError) as caught:
        state.take(doc, 1.1)
    assert caught.value.code == "E_STATE_UNSUPPORTED"
    assert state.refused_kind(doc) is not None


def test_a_warm_start_with_a_part_on_a_one_load_piece_is_refused():
    """Review Focus 4 (fix round 1): a warm start (`initial_state`, D429) puts parts back by
    `restore`, past the gate, so the gate would not know the piece is busy. A part on a one-load
    belt is refused with E_INITIAL_BELT, case "refuse"; the same state without one_load is not."""
    from nagasim.core.schema import validate_model_structured
    doc = plain_line(belt(), arrivals=3)
    doc["simulation"]["time_limit"] = 30
    doc["initial_state"] = {"clock": 2.0, "entities": [
        # the 10 m belt at 1 m/s: entered 1.0, rides to the end until 11.0
        {"block": "belt", "count": 1, "belt": {"stage": "ride", "until": 11.0, "entered": 1.0}}]}
    assert not [p for p in validate_model_structured(copy.deepcopy(doc))
                if p.code == "E_INITIAL_BELT"]
    found = [p for p in validate_model_structured(_with_one_load(doc))
             if p.code == "E_INITIAL_BELT"]
    assert [p.fields["case"] for p in found] == ["refuse"]


# -- Review Focus 5: an old file saved again ---------------------------------------------------

def test_one_load_round_trips_and_is_absent_by_default():
    """A belt without one_load writes no `one_load` key (old files load and save as they were);
    one with it gives back True. Both halves in one test: the first passes today."""
    doc = plain_line(belt(), arrivals=1)
    _env, built = run_env(copy.deepcopy(doc), 20)
    assert "one_load" not in built.blocks["belt"].get_params_dict()
    _env, built = run_env(_with_one_load(doc), 20)
    params = built.blocks["belt"].get_params_dict()
    assert params["one_load"] is True
    built.blocks["belt"].set_params_from_dict(params)
    assert built.blocks["belt"].get_params_dict() == params


# -- The assistant sets the flag through update_block ------------------------------------------

def test_update_block_sets_one_load():
    """The rig of test_each_load_clears_by_its_own_length, built with the agent tools: a "belt"
    piece placed by `place_piece` (1.5 m straight, speed 0.5, gap 0, default 1 cm cells), four
    single-load CREATEs at 0, 0.1, 0.2, 0.3 (SHORT, LONG, SHORT, LONG) and the two sized
    entities, then `update_block(params={"one_load": True})`. The expected exits are that test's
    own EXITS_OWN_LENGTH (the derivation lives there): without the flag the four loads would
    pack on the belt, so matching those times proves the flag reached the engine."""
    from nagasim import agent_tools as T

    def ok(result):
        assert "error" not in result, result.get("error")
        return result

    with tempfile.TemporaryDirectory() as folder:
        T.use_workspace(T.Workspace(folder))
        ok(T.call("new_model", name="m"))
        ok(T.call("set_entity_types", model="m",
                  entities=[sized("SHORT", 0.41), sized("LONG", 0.8)]))
        ok(T.call("place_piece", model="m", name="belt", type="belt", speed=0.5,
                  segments=[{"straight": 1.5}], gap=0.0))
        ok(T.call("add_block", model="m", type="DISPOSE", name="end"))
        ok(T.call("connect", model="m", source="belt", target="end", source_port="out"))
        for i, kind in enumerate(["SHORT", "LONG", "SHORT", "LONG"]):
            ok(T.call("add_block", model="m", type="CREATE", name=f"c{i}",
                      params={"e_type": kind, "max_arrivals": 1, "start_time": 0.1 * i,
                              "distribution": "Constant", "dist_params": {"value": 1.0}}))
            ok(T.call("connect", model="m", source=f"c{i}", target="belt"))
        ok(T.call("update_block", model="m", name="belt", params={"one_load": True}))
        doc = ok(T.call("get_model_json", model="m"))["layout"]
    belt_params = next(b for b in doc["blocks"] if b["name"] == "belt")["params"]
    assert belt_params["one_load"] is True and belt_params["type"] == "belt"
    doc["simulation"] = {**doc.get("simulation", {}), "time_limit": None, "warmup": 0,
                         "replications": 1}
    results = api.run_model(doc, replications=1)
    for kind, expected in EXITS_OWN_LENGTH.items():
        assert exits(results, kind) == pytest.approx(expected, abs=1e-9), kind


# -- Turn order with two merges (final review) ---------------------------------------------------

def _two_merges(priority=None):
    """A typed one-load belt 3.0 m at 0.5 m/s, gap 0, merges declared out of `at` order: FAR at
    1.0 first, NEAR at 0.2 second. Three CREATEs (MAIN at the start, NEAR, FAR) each put two
    0.41 m loads every 0.82 s from 0, far faster than the piece boards. Returns [(type, exit)]."""
    far, near = {"at": 1.0, "from": "far"}, {"at": 0.2, "from": "near"}
    if priority:
        far["priority"] = near["priority"] = priority
    conveyor = one_load(belt(length=3.0, speed=0.5, spacing=0.41, merges=[far, near]))
    conveyor["params"].update({"type": "belt", "gap": 0.0})
    kinds = ["MAIN", "NEAR", "FAR"]
    creates = [create(name=kind.lower(), e_type=kind, max_arrivals=2,
                      dist_params={"value": 0.82}, start_time=0) for kind in kinds]
    links = [{"source": kind.lower(), "target": "belt"} for kind in kinds]
    links.append({"source": "belt", "target": "dispose_1"})
    doc = model([*creates, conveyor, dispose()], links)
    doc["entities"] = [sized(kind, 0.41) for kind in kinds]
    log = api.run_model(doc, replications=1)["entity_log"].sort_values("Exited At")
    return list(zip(log["Type"], log["Exited At"]))


EXITS_TWO_MERGES = [("MAIN", 6.0), ("NEAR", 12.42), ("FAR", 17.24),
                    ("MAIN", 24.06), ("NEAR", 30.48), ("FAR", 35.3)]
EXITS_TWO_MERGES_FIRST = [("NEAR", 5.6), ("FAR", 10.42), ("MAIN", 17.24),
                          ("NEAR", 23.66), ("FAR", 28.48), ("MAIN", 35.3)]


def test_turns_follow_belt_order_with_two_merges():
    """Ruling 4: turns go in belt order, the start, then the merges by `at`, not by the order
    they are declared in. Rule 2: clear = t_leave + (L - x + occ) / v, every load leaving by
    the end (x = L), occ 0.41, so a load clears 0.41 / 0.5 = 0.82 after it leaves.
      MAIN boards 0, rides 3.0 / 0.5 = 6.0, leaves 6.0, clear 6.82.
      NEAR (at 0.2) boards 6.82, rides 2.8 / 0.5 = 5.6, leaves 12.42, clear 13.24.
      FAR (at 1.0) boards 13.24, rides 2.0 / 0.5 = 4.0, leaves 17.24, clear 18.06.
      MAIN 2 boards 18.06: the period is 6.82 + 6.42 + 4.82 = 18.06 (exits 24.06, 30.48, 35.3).
    Taking the merges in declared order would put FAR second (exit 6.82 + 4.0 = 10.82).
    With priority "merge" on both merges the first by `at` boards first (D1440): NEAR boards 0,
    leaves 5.6, clear 6.42; FAR boards 6.42, leaves 10.42, clear 11.24; MAIN boards 11.24,
    leaves 17.24, clear 18.06; then NEAR again, the same 18.06 period."""
    for priority, expected in ((None, EXITS_TWO_MERGES), ("merge", EXITS_TWO_MERGES_FIRST)):
        got = _two_merges(priority)
        assert [kind for kind, _t in got] == [kind for kind, _t in expected], (priority, got)
        assert [t for _kind, t in got] == pytest.approx([t for _kind, t in expected], abs=1e-9)


# -- A loop through a one-load piece (final review) ----------------------------------------------

def _loop(b_length, arrivals=0):
    """CREATE (every 1 s; *arrivals* 0 keeps it running) -> A, a one-load belt 3 m at 1 m/s,
    spacing 1 -> B, a plain belt *b_length* m -> back to A; time limit 500. The DISPOSE is
    there only because a model needs one."""
    blocks = [create(max_arrivals=arrivals, dist_params={"value": 1.0}, start_time=0),
              one_load(belt("A", length=3.0)), belt("B", length=b_length), dispose()]
    links = [{"source": "create_1", "target": "A"}, {"source": "A", "target": "B"},
             {"source": "B", "target": "A"}]
    return model(blocks, links, time_limit=500)


def test_a_deadlocked_loop_through_a_one_load_piece_is_named():
    """A's load waits at its end for B, which is full (2 cells) and waits for A, whose free
    cells are no room while its load is held. The loop check names A -> B -> A as it closes,
    as it does for a plain A (before: A's free cells read as room and the run froze to 500)."""
    with pytest.raises(RuntimeError, match="deadlock at .*loop A -> B -> A"):
        run_env(_loop(2.0), 500)


def test_a_live_loop_through_a_one_load_piece_runs():
    """Three loads circulate: B has 10 cells and never fills, so A's held load always leaves.
    No deadlock is reported and both belts keep passing loads (about one per 4 s at A: 3 s
    ride plus 1 s to clear)."""
    results = api.run_model(_loop(10.0, arrivals=3), replications=1)
    assert belt_row(results, "A")["Exited"] > 100
    assert belt_row(results, "B")["Exited"] > 100


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
