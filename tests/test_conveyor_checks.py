"""Conveyor system A11: the live deadlock check's two gaps (R41, R45), the run guards
(W_CONVEYOR_HANDED_TO_NOTHING, W_ROUTE_FORBIDDEN), the static floor checks (W_CONVEYOR_OPEN_PORT,
W_CONVEYOR_OVERLAP, W_CONVEYOR_SNAP) and the strict switch.

Every expected value is hand-derived in the test's docstring."""
import time
from pathlib import Path

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_checkpoint_rules import _loop, _run_unchecked
from test_conveyor_divert_spec import belt
from nagasim import api
from nagasim.core import conveyor_path as cp
from nagasim.core.conveyor_checks import STRICT_CODES, static_checks
from nagasim.core.schema import ModelValidationError, load_layout, validate_model_structured

FLOOR = ("W_CONVEYOR_OPEN_PORT", "W_CONVEYOR_OVERLAP", "W_CONVEYOR_SNAP")


def _with_stop(doc, at):
    """R gets a stop at *at* with a 0.7 dwell: a timed wait between the eye and the end."""
    r = next(b for b in doc["blocks"] if b["name"] == "R")
    r["params"]["stops"] = [{"at": at, "time": 0.7}]
    return doc


# ------------------------------------------------- R41: a transfer waits for an eye it never sees
def test_a_full_loop_is_named_live_as_the_awaited_load_stands():
    """The #2013 loop with 14 loads and R's eye at 2.5 (4 m belts at 0.5, items 0.41 m): F's
    end holds a load for the module, the module waits for load 5 to reach R's eye, and load 5
    stands at the eye's section at t = 33 behind R's eye-to-end section (1.5 m, 4 cells) full,
    whose end part waits for F. R still has free cells, which the module cannot use: the loop
    is named then, before the run drains (it drained at 36)."""
    with pytest.raises(RuntimeError, match=r"deadlock at t=33: .*loop F -> xfer -> R -> F"):
        run_env(_loop(2.5, 14), 300)


def test_a_dwelling_stop_past_the_eye_is_seen_through():
    """Open arrivals and a stop at 3.0 (0.7 s dwell) between R's eye and its end: the stop's
    wait ends on its own, so the check waits until every part past the load stands untimed.
    Loads board R every 5 s (8, 13, ...: the module waits for each at the eye, 2.5 / 0.5). Load
    1 is held at R's end from 16.7; loads 2 and 3 dwell, then pack at 3.59 and 3.18; load 4
    (on at 23) stands at 2.77, short of the stop, behind load 3's back; load 5 (on at 28) would
    reach the eye at 33 but stands at 2.36 behind load 4: named then (#2014; at 38 before,
    when load 4 rode through load 3 to the stop)."""
    with pytest.raises(RuntimeError, match=r"deadlock at t=33: .*loop F -> xfer -> R -> F"):
        run_env(_with_stop(_loop(2.5, 0), 3.0), 300)


def test_a_loop_that_keeps_moving_is_not_called_a_deadlock():
    """13 loads with the stop at 3.0: R fits 4 loads past its eye (fronts 4.0, 3.59, 3.18 and
    2.77, as above) and F 9, so with the fifth on R short of the eye F has a free cell, R's end
    hands over and loads keep reaching the eye to the run's end; the module's eye wait is
    checked at every stand and never found closed. (14 loads fill it at 33 since #2014: before,
    load 4 rode through load 3's body to the stop and a fifth load fitted past the eye.)"""
    env, model = run_env(_with_stop(_loop(2.5, 13), 3.0), 300)
    assert env.now == 300 and model.blocks["R"].exited > 13


# ------------------------------------------- R45: a reversed belt's held start, seen live
def test_a_reversed_belts_held_start_closes_its_loop_live():
    """The T9 review's open-ended two-way loop: 'belt' runs backward from 1.5, P waits at its
    start from 3.0 for 'back' (one cell, Q2 on it), Q2 waits at back's end from 3.5 for the
    transfer, which holds Q1 for 'belt'. Nothing boards 'belt' while P stands at its start,
    whichever way it runs, so the loop is named as Q2 blocks, at 3.5."""
    from test_conveyor_reverse_rules import _two_belts
    with pytest.raises(Exception, match=r"deadlock at t=3.5: .*loop back -> xfer -> belt -> back"):
        api.run_model(_two_belts(loop=True, arrivals=0, time_limit=100), replications=1)


# ----------------------------------------------------------------------- floor pieces
def piece(name, segments, x=0.0, y=0.0, rotation=0.0, **params):
    """A CONVEYOR placed on the floor: *segments* is its path, from (x, y) at *rotation*."""
    block = belt(name, **params)
    block["params"].pop("length")
    block["params"]["path"] = segments
    block.update(layout_position=[x, y], layout_rotation=rotation)
    return block


def port_of(block, port):
    """Where *port* of a floor piece is: (x, y, heading)."""
    start = cp.Pose(*block["layout_position"], block["layout_rotation"])
    ports = cp.ports(block["name"], start, cp.parse_path(block["params"]["path"]),
                     block["params"].get("diverts") or [])
    return next(p.pose for p in ports if p.name == port)


def after(block, port, name, segments, **params):
    """A piece snapped to *port* of *block*: it starts where the port is, facing its way."""
    x, y, heading = port_of(block, port)
    return piece(name, segments, x, y, heading, **params)


def codes(doc):
    return sorted((p.code, p.block or "", (p.fields or {}).get("port") or (p.fields or {}).get("other")
                   or (p.fields or {}).get("target"))
                  for p in validate_model_structured(doc) if p.code in FLOOR)


def test_no_bundled_model_gets_a_floor_warning():
    """No bundled model has a belt with a path (R55): none of the three codes."""
    models = sorted((Path(api.__file__).parent / "models").glob("*.json"))
    assert models
    assert [(m.name, p.code) for m in models for p in static_checks(load_layout(str(m)))] == []


def test_a_line_snapped_with_place_piece_is_quiet(tmp_path):
    """place_piece A (straight 3 at (1, 2), heading 90), then B snapped to A.out: the two meet
    port to port. Alone, A's in and B's out are open; fed by a CREATE and ending in a DISPOSE,
    nothing is said."""
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    assert T.call("new_model", name="m")["ok"]
    T.call("place_piece", model="m", name="A", segments=[{"straight": 3.0}], x=1.0, y=2.0,
           rotation=90.0)
    T.call("place_piece", model="m", name="B", segments=[{"straight": 2.0}], snap_to="A.out")
    doc = T.call("get_model_json", model="m")["layout"]
    assert codes(doc) == [("W_CONVEYOR_OPEN_PORT", "A", "in"),
                          ("W_CONVEYOR_OPEN_PORT", "B", "out")]
    doc["blocks"] += [create(max_arrivals=1), dispose()]
    doc["links"] += [{"source": "create_1", "target": "A"}, {"source": "B", "target": "dispose_1"}]
    assert codes(doc) == []


def _line(*blocks, links):
    return layout([create(max_arrivals=1), *blocks, dispose()], links)


def test_a_divert_branch_and_a_merge_side_belt_are_joined_not_overlapping():
    """A: straight 6 east from (0, 0), divert 0 at 2 on the left (port d0 at (2, 0) facing -90),
    merge at 4 from S. C snapped to A.d0 runs north from A's centreline, so it lies on A for
    half A's width: joined port to port, not an overlap. S comes down from (4, -3) heading 90
    and ends on A's centreline at the merge (4, 0): its link enters at the merge, not at a
    port, so no snap is asked of it, and it is joined to A by that link. No floor warning."""
    a = piece("A", [{"straight": 6.0}], diverts=[{"at": 2.0, "condition": "True"}],
              merges=[{"at": 4.0, "from": "S"}])
    c = after(a, "d0", "C", [{"straight": 3.0}])
    s = piece("S", [{"straight": 3.0}], 4.0, -3.0, 90.0)
    doc = _line(a, c, s, create(name="c2", max_arrivals=1), links=[
        {"source": "create_1", "target": "A"}, {"source": "c2", "target": "S"},
        {"source": "A", "target": "dispose_1", "source_port": "out"},
        {"source": "A", "target": "C", "source_port": "d0", "target_port": "in"},
        {"source": "S", "target": "A"}, {"source": "C", "target": "dispose_1"}])
    assert codes(doc) == []


def test_belts_on_either_side_of_a_transfer_are_joined_and_its_open_exit_is_named():
    """A ends at (4, 0); a pop-up 'xfer' takes its loads, exit "out" (90 degrees) to B and exit
    "side" (straight on) to nothing; B starts at (4, 0) heading 90. A and B touch at the
    module, which joins them; the transfer has no floor geometry, so no snap is asked across
    it (D1371). Only the side exit is open."""
    a = piece("A", [{"straight": 4.0}])
    b = piece("B", [{"straight": 3.0}], 4.0, 0.0, 90.0)
    xfer = {"name": "xfer", "description": "xfer", "type": "CONVEYOR_TRANSFER",
            "position": [1, 0], "layout_position": [4.0, 0.0], "layout_rotation": 0.0,
            "params": {"exits": [{"port": "out", "angle": 90}, {"port": "side", "angle": 0}]}}
    doc = _line(a, b, xfer, links=[
        {"source": "create_1", "target": "A"}, {"source": "A", "target": "xfer"},
        {"source": "xfer", "target": "B", "source_port": "out"},
        {"source": "B", "target": "dispose_1"}])
    assert codes(doc) == [("W_CONVEYOR_OPEN_PORT", "xfer", "side")]


def test_an_unlinked_divert_port_is_open():
    """A divert with no link of its own: port d0 is open (validation also refuses the missing
    link, E_CONVEYOR_DIVERT)."""
    a = piece("A", [{"straight": 6.0}], diverts=[{"at": 2.0, "condition": "True"}])
    doc = _line(a, links=[{"source": "create_1", "target": "A"},
                          {"source": "A", "target": "dispose_1"}])
    assert codes(doc) == [("W_CONVEYOR_OPEN_PORT", "A", "d0")]


@pytest.mark.parametrize("gap, kind, overlaps", [
    (0.5, "", True),            # untyped, 0.8 m wide: centrelines 0.5 apart < 0.8
    (0.9, "", False),
    (0.6, "belt", True),        # typed belt, 0.61 m wide: 0.6 < 0.61
    (0.7, "belt", False),
])
def test_parallel_belts_overlap_closer_than_their_widths(gap, kind, overlaps):
    """Two straight 5 m belts east, *gap* apart (y 0 and y gap), fed by one CREATE and ending
    in one DISPOSE (which does not join them): they overlap when the gap is under half their
    two widths summed (their width when alike)."""
    a = piece("A", [{"straight": 5.0}])
    b = piece("B", [{"straight": 5.0}], 0.0, gap)
    if kind:
        a["params"]["type"] = b["params"]["type"] = kind
    doc = _line(a, b, links=[{"source": "create_1", "target": "A"},
                             {"source": "create_1", "target": "B"},
                             {"source": "A", "target": "dispose_1"},
                             {"source": "B", "target": "dispose_1"}])
    assert codes(doc) == ([("W_CONVEYOR_OVERLAP", "A", "B")] if overlaps else [])


def test_an_arc_that_crosses_a_belt_overlaps():
    """A straight 6 east; B starts at (3, -2) heading 0 and turns right r = 2 by 90 degrees:
    its arc's centre is (3, 0), so it ends at (5, 0), on A's centreline."""
    a = piece("A", [{"straight": 6.0}])
    b = piece("B", [{"arc": {"radius": 2.0, "angle": 90, "turn": "right"}}], 3.0, -2.0, 0.0)
    doc = _line(a, b, links=[{"source": "create_1", "target": "A"},
                             {"source": "create_1", "target": "B"},
                             {"source": "A", "target": "dispose_1"},
                             {"source": "B", "target": "dispose_1"}])
    assert codes(doc) == [("W_CONVEYOR_OVERLAP", "A", "B")]


@pytest.mark.parametrize("dx, turn, link, snaps", [
    (0.0, 0.0, {}, True),
    (0.04, 3.0, {}, True),                          # within 0.05 m and 5 degrees
    (0.1, 0.0, {}, False),
    (0.0, 6.0, {}, False),
    (2.0, 0.0, {"delay": {"distribution": "Constant", "dist_params": {"value": 5.0}}},
     True),                                         # a timed link joins areas, not ports
])
def test_linked_ports_meet_within_the_snap_tolerance(dx, turn, link, snaps):
    """A straight 3 east ends at (3, 0); B starts *dx* further east, its heading *turn* off."""
    a = piece("A", [{"straight": 3.0}])
    b = piece("B", [{"straight": 2.0}], 3.0 + dx, 0.0, turn)
    doc = _line(a, b, links=[{"source": "create_1", "target": "A"},
                             {"source": "A", "target": "B", **link},
                             {"source": "B", "target": "dispose_1"}])
    assert codes(doc) == ([] if snaps else [("W_CONVEYOR_SNAP", "A", "out")])


def test_a_belt_without_a_path_is_not_a_floor_piece():
    """R55: legacy belts (a length, no path) lie anywhere; two of them, with unlinked ports and
    overlapping at the origin, say nothing."""
    doc = _line(belt("A"), belt("B"), links=[{"source": "create_1", "target": "A"},
                                             {"source": "B", "target": "dispose_1"}])
    assert codes(doc) == []

def _rows(lines, pieces):
    """*lines* lines of *pieces* snapped 1 m pieces, one line every 2 m, each fed and drained."""
    blocks, links = [create(max_arrivals=1), dispose()], []
    for row in range(lines):
        prev = None
        for k in range(pieces):
            name = f"r{row}p{k}"
            block = (piece(name, [{"straight": 1.0}], 0.0, 2.0 * row) if prev is None
                     else after(prev, "out", name, [{"straight": 1.0}]))
            blocks.append(block)
            links.append({"source": prev["name"] if prev else "create_1", "target": name,
                          **({"source_port": "out", "target_port": "in"} if prev else {})})
            prev = block
        links.append({"source": prev["name"], "target": "dispose_1"})
    return layout(blocks, links)


def test_snapped_rows_of_pieces_are_quiet():
    """4 lines of 50 snapped 1 m pieces: nothing to report (the functional half of the perf
    test below, in the fast set). Every piece keeps its geometry (R61): one moved 0.3 m from the
    next line lies on that line's piece."""
    doc = _rows(4, 50)
    assert static_checks(doc) == []
    moved = next(b for b in doc["blocks"] if b["name"] == "r1p20")
    moved["layout_position"] = [moved["layout_position"][0], 4.3]
    assert [c for c in codes(doc) if c[0] == "W_CONVEYOR_OVERLAP"] == [
        ("W_CONVEYOR_OVERLAP", "r1p20", "r2p20")]          # its two snaps break too


@pytest.mark.perf
def test_two_thousand_pieces_check_in_under_a_second():
    """20 lines of 100 pieces: the grid keeps the overlap check near linear (R59: a wall-clock
    guard, run alone)."""
    doc = _rows(20, 100)
    t0 = time.perf_counter()
    found = static_checks(doc)
    assert time.perf_counter() - t0 < 1.0 and found == []


def test_a_point_past_a_paths_end_is_refused():
    """A path of 3 m (straight 2, then a right arc of r = 2 over 1 rad = 57.2958 degrees, 2 m
    long: 4 m in all) with an eye at 4.5: past the end of the path, E_CONVEYOR_PARAM case
    path_at; an eye at 3.5 is on it."""
    a = piece("A", [{"straight": 2.0},
                    {"arc": {"radius": 2.0, "angle": 57.29577951308232, "turn": "right"}}])
    a["params"]["eyes"] = [{"at": 4.5}]
    doc = _line(a, links=[{"source": "create_1", "target": "A"},
                          {"source": "A", "target": "dispose_1"}])
    found = [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]
    assert [(p.fields["case"], p.fields["what"]) for p in found] == [("path_at", "eye 0")]
    a["params"]["eyes"] = [{"at": 3.5}]
    assert not [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_PARAM"]


# --------------------------------------------------------------------- the strict switch
def _unsnapped():
    """A -> B linked, B started 0.2 m past A's end: W_CONVEYOR_SNAP and nothing else."""
    a = piece("A", [{"straight": 3.0}], speed=1.0)
    b = piece("B", [{"straight": 2.0}], 3.2, 0.0, 0.0, speed=1.0)
    return _line(a, b, links=[{"source": "create_1", "target": "A"},
                              {"source": "A", "target": "B"},
                              {"source": "B", "target": "dispose_1"}])


def test_strict_turns_a_floor_warning_into_an_error_that_stops_the_run():
    """One model, two runs: the snap gap is a warning and the model runs (one part through
    both belts); with strict_checks the same problem, same code, is an error and the run is
    refused."""
    doc = _unsnapped()
    found = [p for p in validate_model_structured(doc) if p.code in FLOOR]
    assert [(p.code, p.severity) for p in found] == [("W_CONVEYOR_SNAP", "warning")]
    results = api.run_model(doc, replications=1)
    assert results["conveyor_summary"].set_index("Conveyor").loc["B", "Exited"] == 1
    doc["simulation"]["strict_checks"] = True
    found = [p for p in validate_model_structured(doc) if p.code in FLOOR]
    assert [(p.code, p.severity) for p in found] == [("W_CONVEYOR_SNAP", "error")]
    with pytest.raises(ModelValidationError, match="0.2 m"):
        api.run_model(doc, replications=1)


def test_strict_leaves_every_other_warning_alone():
    """A condition rule with no ELSE is W_ROUTE_RULE: a warning, strict or not."""
    from test_routing_at_points_rules import _trunk_doc
    doc = _trunk_doc([{"at": 3, "rule": {"kind": "condition", "exits": ["d0", "out"],
                                         "conditions": ["True", "False"]}}])
    doc["simulation"]["strict_checks"] = True
    found = [p for p in validate_model_structured(doc) if p.code == "W_ROUTE_RULE"]
    assert found and {p.severity for p in found} == {"warning"}
    assert "W_ROUTE_RULE" not in STRICT_CODES


def test_strict_checks_is_a_simulation_setting_with_a_default():
    from nagasim.modelschema import build_json_schema
    spec = build_json_schema()["$defs"]["simulation"]["properties"]["strict_checks"]
    assert spec["type"] == "boolean" and spec["default"] is False


# ------------------------------------------------------------------------ run guards
def _dead_end(strict):
    """A 3 m belt at 1 m/s whose end links to nothing (validation refuses it,
    E_TERMINAL_NOT_DISPOSE; a run started without validation gets there): two parts 1 s apart."""
    return layout([create(max_arrivals=2, dist_params={"value": 1.0}),
                   belt("A", length=3.0, speed=1.0)],
                  [{"source": "create_1", "target": "A"}],
                  simulation={"time_limit": None, "warmup": 0, "replications": 1,
                              "time_unit": "seconds", "strict_checks": strict})


def test_a_part_handed_to_nothing_holds_its_belt_blocked(caplog):
    """R58: parts arrive every 1 s for ever; the first reaches A's end at 3 and stands there:
    one warning, and A is blocked from 3 (its blocked share reads it) while the parts behind
    pack; nothing exits by 50."""
    doc = _dead_end(False)
    doc["blocks"][0]["params"]["max_arrivals"] = 0
    with caplog.at_level("WARNING", logger="nagasim.core.conveyor_checks"):
        _env, model = _run_unchecked(doc, 50)
    said = [r.message for r in caplog.records if "W_CONVEYOR_HANDED_TO_NOTHING" in r.message]
    assert len(said) == 1 and "Block 'A'" in said[0]
    a = model.blocks["A"]
    assert a.exited == 0 and a.blocked.level == 1.0 and a.dead_end


def test_a_drained_run_names_a_part_handed_to_nothing_as_a_deadlock():
    """R58: two parts, then nothing left to happen but A holding them: the drain check names A,
    instead of the run ending as a quiet system."""
    with pytest.raises(RuntimeError, match=r"deadlock at t=\S+ nothing is left to happen but A "):
        _run_unchecked(_dead_end(False), 50)


def test_strict_stops_the_run_where_a_part_is_handed_to_nothing():
    """The same model with strict_checks: the first part's arrival at the end stops the run
    with the code."""
    with pytest.raises(RuntimeError, match=r"Block 'A'.*W_CONVEYOR_HANDED_TO_NOTHING"):
        _run_unchecked(_dead_end(True), 50)


def _stale_route(strict):
    """Trunk T, 10 m at 1 m/s: a group of diverts at 3 (d0) and 7 (d1) routed by attribute
    "line"; an ASSIGN sets line = "d1" before the trunk and an eye at 5 sets it to "d0". The
    part is routed to d1 at 3, then leaves by d1 at 7, an exit its rule would not choose now."""
    rule = {"kind": "attribute", "attribute": "line"}
    trunk = belt("T", length=10.0, speed=1.0,
                 diverts=[{"at": 3.0, "rule": rule, "rule_group": "g"},
                          {"at": 7.0, "rule_group": "g"}])
    trunk["params"]["eyes"] = [{"at": 5.0, "assign": [{"target": "entity.line",
                                                        "expr": '"d0"'}]}]
    stamp = {"name": "stamp", "description": "stamp", "type": "ASSIGN", "position": [1, 0],
             "params": {"assignments": [{"target": "entity.line", "expr": '"d1"'}]}}
    blocks = [create(max_arrivals=1), stamp, trunk, belt("L0", length=2.0, speed=1.0),
              belt("L1", length=2.0, speed=1.0), dispose()]
    links = [{"source": "create_1", "target": "stamp"}, {"source": "stamp", "target": "T"},
             {"source": "T", "target": "dispose_1"}, {"source": "T", "target": "L0"},
             {"source": "T", "target": "L1"}, {"source": "L0", "target": "dispose_1"},
             {"source": "L1", "target": "dispose_1"}]
    return layout(blocks, links, simulation={"time_limit": None, "warmup": 0, "replications": 1,
                                             "time_unit": "seconds", "strict_checks": strict})


def test_a_part_leaving_by_an_exit_its_rule_would_not_choose_now_is_warned(caplog):
    """The part leaves by d1 at 7 and L1's end at 9, as routed; one W_ROUTE_FORBIDDEN names T,
    the exit it took and the one the rule would choose now."""
    assert not [p for p in validate_model_structured(_stale_route(False))
                if p.severity == "error"]
    with caplog.at_level("WARNING", logger="nagasim.core.conveyor_checks"):
        env, model = run_env(_stale_route(False), 50)
    said = [r.message for r in caplog.records if "W_ROUTE_FORBIDDEN" in r.message]
    assert len(said) == 1 and "Block 'T'" in said[0] and "'d1'" in said[0] and "'d0'" in said[0]
    assert model.blocks["L1"].exited == 1
    assert [e.deletion_time for e in env.ctx.all_entities] == [pytest.approx(9.0)]


def test_strict_stops_the_run_where_a_part_takes_a_forbidden_exit():
    with pytest.raises(RuntimeError, match=r"Block 'T'.*W_ROUTE_FORBIDDEN"):
        run_env(_stale_route(True), 50)


def test_an_attribute_naming_no_exit_at_the_exit_is_warned(caplog):
    """The eye sets line = "nowhere": at d1 the rule cannot choose any exit for the part (its
    possible_exits raises), which is the same code."""
    doc = _stale_route(False)
    trunk = next(b for b in doc["blocks"] if b["name"] == "T")
    trunk["params"]["eyes"][0]["assign"][0]["expr"] = '"nowhere"'
    with caplog.at_level("WARNING", logger="nagasim.core.conveyor_checks"):
        run_env(doc, 50)
    said = [r.message for r in caplog.records if "W_ROUTE_FORBIDDEN" in r.message]
    assert len(said) == 1 and "cannot choose any exit" in said[0] and "nowhere" in said[0]


# ------------------------------------- R56: a hold on a block outside the loop is no deadlock
def _reversed_out_of_the_loop(second_turn):
    """C1 (review): 'belt' (4 m, 1 m/s, reversible) ends in 'back' (1 m, one cell), which feeds
    'belt' again through a straight pop-up: the loop back -> xfer -> belt -> back. Its start
    goes to X, a one-cell belt outside the loop holding part W at its end, whose exit needs
    Op, held by a hog until 10.
    P boards at 0, the belt turns at 1.5 and P waits at the start for X from 3.0; two Q parts
    enter 'back' at 1.5 and 2.5. With *second_turn* a signal turns the belt forward again at
    12."""
    from test_conveyor_divert_spec import hog, pick
    from test_conveyor_reverse_rules import _XFER, _reversible, _signals
    blocks = [_reversible(reverse_to="X", length=4.0, speed=1.0, spacing=0.5),
              belt("back", length=1.0, speed=1.0, spacing=1.0),
              belt("X", length=1.0, speed=1.0, spacing=1.0, exit_resource="Op"),
              dict(_XFER), pick("Op"), dispose(), create(e_type="P", max_arrivals=1),
              create(name="q_src", e_type="Q", max_arrivals=2, start_time=1.5),
              create(name="x_src", e_type="W", max_arrivals=1)]
    links = [{"source": "belt", "target": "back"}, {"source": "belt", "target": "X"},
             {"source": "create_1", "target": "belt"}, {"source": "q_src", "target": "back"},
             {"source": "back", "target": "xfer"},
             {"source": "xfer", "target": "belt", "source_port": "straight"},
             {"source": "x_src", "target": "X"},
             {"source": "X", "target": "pick"}, {"source": "pick", "target": "dispose_1"}]
    _signals(blocks, links, [1.5, 12.0] if second_turn else [1.5])
    hog_blocks, hog_links = hog("Op", 10.0)
    return layout(blocks + hog_blocks, links + hog_links,
                  resources=[{"name": "Op", "capacity": 1}],
                  simulation={"time_limit": 200, "warmup": 0, "replications": 1,
                              "time_unit": "seconds"})


def test_a_reversed_start_waiting_outside_the_loop_is_no_deadlock():
    """R56: P waits at belt's start for X (outside the loop, full with W) until Op frees at 10;
    W leaves, P boards X and leaves it at 11, and after the forward turn at 12 the two Q parts circulate to the run's end:
    belt's start waits for X, not for 'back', the loop member after it."""
    env, model = run_env(_reversed_out_of_the_loop(True), 200)
    assert env.now == 200 and model.blocks["X"].exited == 2 and model.blocks["belt"].entered > 20


def _receiving_belt_out_of_the_loop():
    """C2 (review): the #2013 loop, but R joins the loop back to F only through a divert at 0.3
    that never takes (condition False); R's end goes to Y (0.5 m) outside the loop, whose exit
    needs Op, held by a hog until 60. Open arrivals."""
    from test_conveyor_divert_spec import hog, pick
    doc = _loop(2.5, 0)
    r = next(b for b in doc["blocks"] if b["name"] == "R")
    r["params"]["diverts"] = [{"at": 0.3, "condition": "False"}]
    doc["links"] = [k for k in doc["links"] if k["source"] != "R"] + [
        {"source": "R", "target": "Y", "source_port": "out"},
        {"source": "R", "target": "F", "source_port": "d0"},
        {"source": "Y", "target": "pick"}, {"source": "pick", "target": "dispose_1"}]
    hog_blocks, hog_links = hog("Op", 60.0)
    doc["blocks"] += [belt("Y", length=0.5, speed=0.5, spacing=0.41, exit_resource="Op"),
                      pick("Op"), *hog_blocks]
    doc["links"] += hog_links
    doc["resources"] = [{"name": "Op", "capacity": 1}]
    return doc


def test_a_receiving_belt_held_outside_the_loop_is_no_deadlock():
    """R56: R's end waits for Y (outside the loop) until 60; the module's load stands short of
    R's eye meanwhile, but R's end part waits for Y, not for F, the loop member after R. From
    60 the line flows again to the run's end."""
    env, model = run_env(_receiving_belt_out_of_the_loop(), 200)
    assert env.now == 200 and model.blocks["Y"].exited > 10


# --------------------------------------------- R57: pieces touch at a joint by design
def _chain(*segments_list, kind=""):
    """Pieces p0, p1, ... each snapped to the last one's out port, fed and drained."""
    pieces, links = [], [{"source": "create_1", "target": "p0"}]
    for k, segments in enumerate(segments_list):
        extra = {"type": kind} if kind else {}
        block = (piece("p0", segments) if k == 0
                 else after(pieces[-1], "out", f"p{k}", segments))
        if kind:
            block["params"]["type"] = kind
        if k:
            links.append({"source": f"p{k - 1}", "target": f"p{k}", "source_port": "out",
                          "target_port": "in"})
        pieces.append(block)
    links.append({"source": pieces[-1]["name"], "target": "dispose_1"})
    return _line(*pieces, links=links)


@pytest.mark.parametrize("segments_list, kind", [
    ([[{"straight": 1.0}], [{"straight": 0.3}], [{"straight": 1.0}]], ""),
    ([[{"straight": 0.5}]] * 4, ""),
    ([[{"straight": 1.0}], [{"straight": 0.4}], [{"straight": 1.0}]], "belt"),
    ([[{"straight": 1.0}], [{"arc": {"radius": 0.5, "angle": 90, "turn": "right"}}],
      [{"straight": 1.0}]], ""),
], ids=["short_middle", "four_short", "typed", "corner"])
def test_a_snapped_line_does_not_overlap_itself_at_its_joints(segments_list, kind):
    """The review's probes (I1): pieces snapped port to port touch at each joint, and a short
    middle piece or a tight corner brings p0's end near p2's start; within one belt width of
    a joint nothing is compared, so no warning."""
    assert codes(_chain(*segments_list, kind=kind)) == []


def test_two_belts_leaving_one_transfer_do_not_overlap_at_it():
    """A right-angle pop-up at (4.4, 0) with a 0.8 m module: A ends at its entry (4, 0); C leaves
    straight on from (4.8, 0), B sideways from (4.4, 0.4). The three touch the module by design."""
    a = piece("A", [{"straight": 4.0}])
    c = piece("C", [{"straight": 3.0}], 4.8, 0.0, 0.0)
    b = piece("B", [{"straight": 3.0}], 4.4, 0.4, 90.0)
    xfer = {"name": "xfer", "description": "xfer", "type": "CONVEYOR_TRANSFER",
            "position": [1, 0], "layout_position": [4.4, 0.0], "layout_rotation": 0.0,
            "params": {"exits": [{"port": "side", "angle": 90}, {"port": "on", "angle": 0}]}}
    doc = _line(a, b, c, xfer, links=[
        {"source": "create_1", "target": "A"}, {"source": "A", "target": "xfer"},
        {"source": "xfer", "target": "B", "source_port": "side"},
        {"source": "xfer", "target": "C", "source_port": "on"},
        {"source": "B", "target": "dispose_1"}, {"source": "C", "target": "dispose_1"}])
    assert codes(doc) == []


def test_a_snapped_u_turn_back_over_the_line_overlaps():
    """p0 straight 3 east, p1 a right half-turn of radius 0.3, p2 straight 3 back west: p2 runs
    0.6 m from p0 (untyped, 0.8 m wide) beyond the joints, a true overlap of joined pieces."""
    doc = _chain([{"straight": 3.0}], [{"arc": {"radius": 0.3, "angle": 180, "turn": "right"}}],
                 [{"straight": 3.0}])
    assert codes(doc) == [("W_CONVEYOR_OVERLAP", "p0", "p2")]


def test_overlaps_past_twenty_pairs_are_counted():
    """R59: 8 unlinked pieces stacked at one place make 28 pairs: 20 are listed, then one line
    counts the other 8."""
    stack = [piece(f"s{k}", [{"straight": 2.0}]) for k in range(8)]
    found = [p for p in static_checks(_line(*stack, links=[])) if p.code == "W_CONVEYOR_OVERLAP"]
    assert len(found) == 21 and found[-1].fields == {"case": "more", "count": 8}


# --------------------------------- R60: through a branching block the loop is not counted closed
def test_a_receiving_belt_held_outside_the_loop_runs_on_with_its_eye_near_the_entry():
    """R60 (re-review): the probe above with R's eye at 1.0, so R's first section (entry to eye)
    fills while Y is held; R's end part still waits for Y, not F, so a full first section does
    not make R stuck toward F either. From 60 the line flows."""
    doc = _receiving_belt_out_of_the_loop()
    next(b for b in doc["blocks"] if b["name"] == "R")["params"]["eyes"] = [{"at": 1.0}]
    env, model = run_env(doc, 200)
    assert env.now == 200 and model.blocks["Y"].exited > 10


def _recirculation_decide(condition):
    """The #2013 loop whose return belt R ends in a DECIDE "dec" (*condition* -> Y, else back to
    F). Y is 0.5 m, its exit needs Op, held by a hog until 60. Open arrivals."""
    from test_conveyor_divert_spec import hog, pick
    doc = _loop(2.5, 0)
    hog_blocks, hog_links = hog("Op", 60.0)
    doc["blocks"] += [{"name": "dec", "description": "dec", "type": "DECIDE",
                       "position": [2, 0], "params": {"mode": "Condition",
                                                      "conditions": [condition]}},
                      belt("Y", length=0.5, speed=0.5, spacing=0.5, exit_resource="Op"),
                      pick("Op"), *hog_blocks]
    doc["links"] = [k for k in doc["links"] if k["source"] != "R"] + [
        {"source": "R", "target": "dec"}, {"source": "dec", "target": "Y"},
        {"source": "dec", "target": "F"}, {"source": "Y", "target": "pick"},
        {"source": "pick", "target": "dispose_1"}, *hog_links]
    doc["resources"] = [{"name": "Op", "capacity": 1}]
    return doc


def test_a_loop_closed_through_a_branching_decide_is_not_called_a_deadlock_live():
    """R60: every part leaves R for Y through the DECIDE; while Op is held to 60 the loop F ->
    xfer -> R stands, but R's end part waits for Y, which the DECIDE chose, so the live check
    does not count the loop closed. From 60 the line flows to the run's end."""
    env, model = run_env(_recirculation_decide("1 == 1"), 200)
    assert env.now == 200 and model.blocks["Y"].exited > 10


# ----------------------------- R61: only the two pieces sharing a joint skip each other there
def _cross_belt(x, kind=""):
    """X: an unlinked 3 m belt running south across the line at x, from y = -1.5."""
    block = piece("X", [{"straight": 3.0}], x, -1.5, 90.0)
    if kind:
        block["params"]["type"] = kind
    return block


def _with(doc, *blocks):
    doc["blocks"] += list(blocks)
    doc["links"] += [{"source": "create_1", "target": b["name"]} for b in blocks] + [
        {"source": b["name"], "target": "dispose_1"} for b in blocks]
    return doc


def test_a_belt_across_a_short_interior_piece_overlaps_it():
    """A snapped line of three 1 m pieces; X crosses the middle of p1 (x = 1.5): X shares no
    joint with p1, so the whole of p1 counts, short as it is."""
    doc = _with(_chain(*[[{"straight": 1.0}]] * 3), _cross_belt(1.5))
    assert codes(doc) == [("W_CONVEYOR_OVERLAP", "X", "p1")]


@pytest.mark.parametrize("kind", ["", "roller"])
def test_a_belt_across_a_joint_it_is_not_part_of_overlaps_both_pieces(kind):
    """p0 (1 m) snapped to p1 (3 m); X crosses exactly on their joint (x = 1.0): the joint is
    p0's and p1's, so X is compared with the whole of both and overlaps each."""
    doc = _with(_chain([{"straight": 1.0}], [{"straight": 3.0}]), _cross_belt(1.0, kind))
    assert codes(doc) == [("W_CONVEYOR_OVERLAP", "X", "p0"), ("W_CONVEYOR_OVERLAP", "X", "p1")]


def test_two_snapped_lines_of_short_pieces_on_each_other_overlap_piece_by_piece():
    """Two lines of six snapped 1 m pieces 0.4 m apart: each a<k> lies on b<k> (pieces laid end
    to end only touch, so a<k> and b<k+1> do not count); six pairs."""
    first = _chain(*[[{"straight": 1.0}]] * 6)
    second = [b for b in _chain(*[[{"straight": 1.0}]] * 6)["blocks"] if b["type"] == "CONVEYOR"]
    for k, block in enumerate(first["blocks"]):
        if block["type"] == "CONVEYOR":
            block["name"] = "a" + block["name"][1:]
    for k, block in enumerate(second):
        block["name"] = "b" + block["name"][1:]
        block["layout_position"] = [block["layout_position"][0], 0.4]
    rename = {f"p{k}": f"a{k}" for k in range(6)}
    first["links"] = [{**k, "source": rename.get(k["source"], k["source"]),
                       "target": rename.get(k["target"], k["target"])} for k in first["links"]]
    first["blocks"] += second
    first["links"] += [{"source": f"b{k}", "target": f"b{k + 1}", "source_port": "out",
                        "target_port": "in"} for k in range(5)] + [
        {"source": "create_1", "target": "b0"}, {"source": "b5", "target": "dispose_1"}]
    assert codes(first) == [("W_CONVEYOR_OVERLAP", f"a{k}", f"b{k}") for k in range(6)]


# --------------------------- R62: a divert branch's mouth lies on the trunk its neighbours share
def _t_divert(at, side, feeder=None):
    """Z (2 m) | A (6 m) | B (2 m) snapped east from (-2, 0); branch C (3 m) snapped to A's
    divert at *at* on *side*. *feeder*: Z's path instead (it must end at A's entry)."""
    a = piece("A", [{"straight": 6.0}], diverts=[{"at": at, "condition": "True", "side": side}])
    z = piece("Z", [{"straight": 2.0}], -2.0, 0.0, 0.0) if feeder is None else feeder
    b, c = after(a, "out", "B", [{"straight": 2.0}]), after(a, "d0", "C", [{"straight": 3.0}])
    return _line(z, a, b, c, links=[
        {"source": "create_1", "target": "Z"},
        {"source": "Z", "target": "A", "source_port": "out", "target_port": "in"},
        {"source": "A", "target": "B", "source_port": "out", "target_port": "in"},
        {"source": "A", "target": "C", "source_port": "d0", "target_port": "in"},
        {"source": "B", "target": "dispose_1"}, {"source": "C", "target": "dispose_1"}])


@pytest.mark.parametrize("at", [0.3, 5.7])
@pytest.mark.parametrize("side", ["left", "right"])
def test_a_divert_branch_near_its_pieces_end_is_quiet(at, side):
    """The re-review's T rows: the 0.8 m branch leaves A 0.3 m from a joint, so its mouth (the
    first 0.4 m, on the trunk) spans the joint onto Z or B; past the trunk's edge it touches
    neither. Its mouth is cut against A's neighbours too (R62)."""
    assert codes(_t_divert(at, side)) == []


def test_a_corner_feeding_the_trunk_from_the_branch_side_still_overlaps_the_branch():
    """Z comes south down x = -0.3 and turns left (r = 0.3) into A's entry at (0, 0); C leaves A
    at 0.3 northward (left): past the trunk's edge (y < -0.4) C spans x -0.1..0.7 and Z's
    straight spans x -0.7..0.1, so they truly collide; only C's mouth is cut, so it is flagged."""
    z = piece("Z", [{"straight": 2.7}, {"arc": {"radius": 0.3, "angle": 90, "turn": "left"}}],
              -0.3, -3.0, 90.0)
    x, y, heading = port_of(z, "out")
    assert abs(x) < 1e-9 and abs(y) < 1e-9 and abs(heading % 360.0) < 1e-9
    assert codes(_t_divert(0.3, "left", z)) == [("W_CONVEYOR_OVERLAP", "C", "Z")]
