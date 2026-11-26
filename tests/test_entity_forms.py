"""Plan I Task 5 (D1487, D1488): the item-form rules in the engine, with no editor import."""
import pytest

from nagasim.core.document import ModelDocument
from nagasim.core.entity_forms import EPAL_M, ItemForm, default_carton_size, forms_of


def _doc(blocks=(), entities=()):
    return ModelDocument({"blocks": list(blocks), "links": [], "entities": list(entities)})


def test_a_stretcher_is_a_named_prop():
    forms = forms_of(_doc(entities=[{"name": "PATIENT_STRETCHER"}, {"name": "WHEELCHAIR_USER"}]))
    assert forms["PATIENT_STRETCHER"] == ItemForm("prop", (2.1, 0.95, 1.15), "stretcher")
    assert forms["WHEELCHAIR_USER"] == ItemForm("prop", (1.14, 0.674, 0.94), "wheelchair")


def test_a_patterned_carrier_is_a_pallet():
    batch = {"name": "pack", "description": "pack", "type": "BATCH", "position": [0, 0],
             "params": {"carrier_type": "tray", "pattern": "2x2"}}
    forms = forms_of(_doc([batch]))
    assert forms["TRAY"] == ItemForm("pallet", EPAL_M)


def test_the_default_carton_shrinks_on_a_short_belt():
    belt = {"name": "belt", "description": "belt", "type": "CONVEYOR", "position": [0, 0],
            "params": {"length": 10.0, "speed": 1.0, "part_length": 0.2}}
    doc = _doc([belt], entities=[{"name": "PART"}])
    assert default_carton_size(doc) == pytest.approx((0.2, 0.15, 0.125))
    assert forms_of(doc)["PART"].size == pytest.approx((0.2, 0.15, 0.125))
