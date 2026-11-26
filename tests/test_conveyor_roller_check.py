"""Plan H Task 4: the 3-roller warning. A sized item type that can reach a roller-type piece and is
shorter than three of that piece's roller pitches is warned, never an error. The default roller
pitch is 0.0762 m, so three rollers span 0.2286 m (hand-derived)."""
import copy
import json
from pathlib import Path

from conftest import chain, create, dispose, layout, run_env
from nagasim import api
from nagasim.core.conveyor_checks import static_checks
from nagasim.core.schema import validate_model_structured

CODE = "W_CONVEYOR_ROLLER_PITCH"


def piece(name="R", kind="roller", **params):
    return {"name": name, "description": name, "type": "CONVEYOR", "position": [1, 0],
            "params": {"type": kind, "length": 4.0, "speed": 1.0, "spacing": 1.0, **params}}


def model(length=0.2, kind="roller", e_type="CARTON", entities=None, **params):
    create_ = create(e_type=e_type)
    blocks = [create_, piece(kind=kind, **params), dispose()]
    rows = entities if entities is not None else [{"name": "CARTON", "size": {"length": length}}]
    return layout(blocks, chain(*blocks), entities=rows)


def found(doc):
    return [p for p in static_checks(doc) if p.code == CODE]


def test_a_short_item_on_rollers_is_a_warning():
    """0.2 m < 3 x 0.0762 = 0.2286 m: one warning naming the type and the piece."""
    [one] = found(model(0.2))
    assert (one.severity, one.block, one.path) == ("warning", "R", "/blocks/1")
    assert one.fields == {"case": "roller_pitch", "entity_type": "CARTON"}
    assert "CARTON" in one.message and "Belt R" in one.message and "0.2286" in one.message


def test_the_worked_case_of_a_tenth_of_a_metre():
    assert len(found(model(0.1))) == 1


def test_a_long_item_is_not_warned():
    assert found(model(0.4)) == []


def test_strict_checks_keep_it_a_warning():
    doc = model(0.2)
    doc["simulation"]["strict_checks"] = True
    assert [p.severity for p in found(doc)] == ["warning"]
    doc["simulation"]["time_limit"] = 20
    env, _model = run_env(doc, 20)              # strict_checks does not stop the run on it
    assert env.now == 20


def test_units_convert():
    """A length unit of mm: 200 mm = 0.2 m warns, 250 mm = 0.25 m does not."""
    for value, expected in ((200, 1), (250, 0)):
        doc = model(value)
        doc["simulation"]["length_unit"] = "millimeters"
        assert len(found(doc)) == expected, value


def test_pitch_from_the_look():
    """A pitch of 0.05 m makes three rollers 0.15 m, under the 0.2 m item."""
    assert found(model(0.2, look={"roller_pitch": 0.05})) == []
    assert len(found(model(0.2, look={"roller_pitch": 0.1}))) == 1      # 0.3 m


def test_skipped():
    assert found(model(entities=[{"name": "CARTON"}])) == []             # no size
    assert found(model(0.1, kind="belt")) == []                          # a belt
    assert found(model(0.1, kind="chain")) == []                         # a chain
    assert found(model(0.1, e_type="OTHER")) == []                       # an unsized type reaches it
    unreachable = model(0.1)
    unreachable["links"] = [unreachable["links"][1]]                     # R -> dispose only
    assert found(unreachable) == []                                      # no CREATE reaches R


def test_a_piece_with_no_path_is_checked():
    """The piece is timed by `length` alone (no path): not a floor piece, still checked."""
    doc = model(0.2)
    assert "path" not in doc["blocks"][1]["params"]
    assert len(found(doc)) == 1 and len(found(model(0.2, kind="mdr"))) == 1   # mdr 0.27 m default


def test_one_problem_per_piece_and_type():
    doc = model(0.1)
    doc["entities"].append({"name": "TRAY", "size": {"length": 0.15}})
    doc["blocks"].insert(1, create("c2", e_type="TRAY"))
    doc["links"].append({"source": "c2", "target": "R"})
    assert sorted(p.fields["entity_type"] for p in found(doc)) == ["CARTON", "TRAY"]


def test_it_comes_with_structured_validation():
    problems = validate_model_structured(model(0.2))
    assert [p.code for p in problems].count(CODE) == 1


def test_bundled_models_raise_none():
    for path in sorted((Path(api.__file__).parent / "models").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert found(copy.deepcopy(doc)) == [], path.name


# -- the walk follows the travelling type (D1481) ------------------------------------------------
# The pallet's default pitch is 0.1905 m, so three rollers span 0.5715 m (hand-derived): a 0.41 m
# BOX would warn on a pallet piece, a 1.2 m PALLET does not, a 0.5 m PALLET does.
def palletizing(pallet_length=1.2, box_size=True, middle=None):
    """BOX and PALLET CREATEs into a carrier BATCH, then *middle* (blocks between the BATCH and
    the pallet piece P), P and a DISPOSE."""
    from conftest import block
    boxes, pallets = create("boxes", e_type="BOX"), create("pallets", e_type="PALLET")
    batch = block("Palletize", "BATCH", {"batch_size": 4, "batched_entity_type": "BATCH",
                                         "carrier_type": "PALLET"})
    p = piece("P", kind="pallet")
    end = dispose()
    middle = list(middle or [])
    blocks = [boxes, pallets, batch, *middle, p, end]
    links = [{"source": "boxes", "target": "Palletize"},
             {"source": "pallets", "target": "Palletize"}] + chain(batch, *middle, p, end)
    rows = [{"name": "BOX", **({"size": {"length": 0.41}} if box_size else {})},
            {"name": "PALLET", "size": {"length": pallet_length}}]
    return layout(blocks, links, entities=rows)


def test_a_box_batched_onto_a_pallet_is_not_warned_on_the_pallet_line():
    assert found(palletizing()) == []


def test_a_short_pallet_is_warned_after_the_batch_even_from_an_unsized_box():
    [one] = found(palletizing(0.5))                                     # 0.5 < 0.5715
    assert (one.block, one.fields["entity_type"]) == ("P", "PALLET")
    assert len(found(palletizing(0.5, box_size=False))) == 1


def test_a_plain_batch_carries_its_batched_type():
    doc = palletizing(0.5)
    batch = doc["blocks"][2]["params"]
    batch.pop("carrier_type")
    batch["batched_entity_type"] = "pallet"                             # upper-cased like e_type
    assert [p.fields["entity_type"] for p in found(doc)] == ["PALLET"]


def test_the_walk_stops_at_a_separate_and_a_batching_match():
    from conftest import block
    sep = block("Split", "SEPARATE", {})
    assert found(palletizing(0.5, middle=[sep])) == []
    match = block("Pair", "MATCH", {"number_to_match": 2, "batch_option": True})
    assert found(palletizing(0.5, middle=[match])) == []
    match["params"]["batch_option"] = False                             # the loads go on as themselves
    assert len(found(palletizing(0.5, middle=[match]))) == 1


def test_an_assign_retypes_and_an_expression_stops_the_walk():
    from conftest import block
    to_box = block("Retype", "ASSIGN", {"assignments": [{"target": "entity.e_type", "expr": "'box'"}]})
    [one] = found(palletizing(middle=[to_box]))
    assert one.fields["entity_type"] == "BOX"
    to_box["params"]["assignments"][0]["expr"] = "entity.kind"
    assert found(palletizing(middle=[to_box])) == []


def test_an_eye_assign_retypes_the_load_that_leaves_the_piece():
    """The type that arrives is checked on the piece, the eye's type on the next one."""
    eye_piece = piece("E", kind="belt", eyes=[{"at": 2.0, "assign": [
        {"target": "entity.e_type", "expr": '"BOX"'}]}])
    [one] = found(palletizing(middle=[eye_piece]))
    assert (one.block, one.fields["entity_type"]) == ("P", "BOX")
