"""Divert `angle` (D1388), the implementer's side of A2 task 3: the angle survives a block's
params read back, and it is floor geometry only, so a run does not change with it."""
import copy

from conftest import run_env
from nagasim.core import conveyor_path as cp
from nagasim.core.trace import TraceRecorder
from test_conveyor_checks import _line, piece


def _model(angle=None):
    divert = {"at": 2.0, "condition": "entity.id % 2 == 0"}
    if angle is not None:
        divert["angle"] = angle
    a = piece("A", [{"straight": 6.0}], diverts=[divert])
    c = piece("C", [{"straight": 3.0}], 2.0, 0.0, -90.0)
    doc = _line(a, c, links=[
        {"source": "create_1", "target": "A"},
        {"source": "A", "target": "dispose_1", "source_port": "out"},
        {"source": "A", "target": "C", "source_port": "d0", "target_port": "in"},
        {"source": "C", "target": "dispose_1"}])
    doc["blocks"][0]["params"]["max_arrivals"] = 20
    return doc


def test_the_angle_reads_back_and_ninety_stays_unwritten():
    _env, model = run_env(copy.deepcopy(_model(45)), 1)
    assert model.blocks["A"].get_params_dict()["diverts"][0]["angle"] == 45
    _env, model = run_env(copy.deepcopy(_model()), 1)
    assert "angle" not in model.blocks["A"].get_params_dict()["diverts"][0]


def test_a_run_does_not_depend_on_the_angle():
    """Twenty parts, the even ids diverted: the traces with angle 45, none and 135 are the
    same event for event, and parts did ride the branch C."""
    def trace(angle):
        recorder = TraceRecorder()
        run_env(_model(angle), 100, subscribers=[recorder])
        return [repr(event) for event in recorder.events]
    base = trace(None)
    assert any("block='C'" in line for line in base)
    assert trace(45) == base == trace(135)


def test_a_refused_angle_reads_as_ninety_for_the_floor():
    """Validation refuses 0 (E_CONVEYOR_PARAM); the pose still has a usable heading."""
    assert cp.divert_angle({"angle": 0}) == cp.divert_angle({"angle": True}) == 90.0
    assert cp.divert_angle({"angle": 30}) == 30.0


def _near_an_end(angle, at, neighbour_path=None, before=False):
    """Trunk A 6 m with a divert at *at* (*angle*), branch C 3 m snapped to A.d0; a belt B
    snapped to A.out (*neighbour_path*, straight 4 m by default) or, *before*, a feeder U ending
    at A.in."""
    from test_conveyor_checks import after, piece
    from test_conveyor_checks import _line as line
    a = piece("A", [{"straight": 6.0}],
              diverts=[{"at": at, "condition": "True", "angle": angle}])
    c = after(a, "d0", "C", [{"straight": 3.0}])
    links = [{"source": "A", "target": "C", "source_port": "d0", "target_port": "in"},
             {"source": "C", "target": "dispose_1"}]
    pieces = [a, c]
    if before:
        pieces.append(piece("U", [{"straight": 4.0}], -4.0, 0.0, 0.0))
        links += [{"source": "create_1", "target": "U"}, {"source": "U", "target": "A"},
                  {"source": "A", "target": "dispose_1", "source_port": "out"}]
    else:
        pieces.append(after(a, "out", "B", neighbour_path or [{"straight": 4.0}]))
        links += [{"source": "create_1", "target": "A"},
                  {"source": "A", "target": "B", "source_port": "out"},
                  {"source": "B", "target": "dispose_1"}]
    return line(*pieces, links=links)


def _overlaps(doc):
    from nagasim.core.schema import validate_model_structured
    return sorted((p.block, (p.fields or {}).get("other")) for p in validate_model_structured(doc)
                  if p.code == "W_CONVEYOR_OVERLAP")


def test_an_angled_branch_next_to_the_trunk_end_does_not_overlap_the_next_belt():
    """A2-R7 (final review I3): against the trunk's neighbours the branch's mouth is cut by
    (h_trunk + h_branch |cos a|) / sin a, where its near corner leaves their band; it was
    h_trunk / sin a, so 45 degrees at 5.4 to 5.9 (and 30 from 5.0) reported B against C."""
    for angle, at in ((45, 5.4), (45, 5.9), (30, 5.0), (30, 5.7), (60, 5.9)):
        assert _overlaps(_near_an_end(angle, at)) == [], (angle, at)


def test_an_angled_branch_next_to_the_trunk_start_does_not_overlap_the_feeder():
    """The mirror case: 135 or 150 degrees within 0.6 m of the trunk's start, a feeder at A.in."""
    for angle, at in ((135, 0.1), (135, 0.6), (150, 0.3), (150, 0.6)):
        assert _overlaps(_near_an_end(angle, at, before=True)) == [], (angle, at)


def test_a_neighbour_turning_over_the_branch_still_overlaps():
    """Control: B snapped to A.out turns left (the branch side) and crosses C beyond the mouth."""
    turn = [{"arc": {"radius": 1.0, "angle": 90, "turn": "left"}}, {"straight": 4.0}]
    assert _overlaps(_near_an_end(45, 5.4, turn)) == [("B", "C")]
    assert _overlaps(_near_an_end(30, 4.0, turn)) == [("B", "C")]
