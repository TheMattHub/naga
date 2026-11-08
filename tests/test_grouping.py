"""Who is inside what, read from a trace (nagasim/core/grouping.py, D1147, D1148): batches,
carriers loaded by a carrier BATCH and unloaded by a keep-mode SEPARATE, FIFO or LIFO, and
nesting. Real runs of small models, so the rule follows the engine's own emit order."""
import pytest

from nagasim.core.grouping import Grouping
from nagasim.core.trace import TraceEvent, TraceRecorder

from conftest import block, chain, create, dispose, layout, run_env
from test_carriers import _load_model, _pallets, _truck, _unload_model


def _events(model, run_len=100):
    recorder = TraceRecorder()
    run_env(model, run_len, subscribers=[recorder])
    return recorder.events


def _ids(events, e_type):
    return sorted({e.entity_id for e in events if e.e_type == e_type and e.kind == "entity_created"})


@pytest.mark.parametrize("order", ["fifo", "lifo"])
def test_a_batch_holds_its_members_until_each_is_unloaded(order):
    """Four parts batched into a TRUCK at 0, unloaded one every 2 min (keep mode)."""
    model = _unload_model(release_order=order)
    events = _events(model)
    g = Grouping(events)
    parts, (truck,) = _ids(events, "PART"), _ids(events, "TRUCK")
    assert g.path(parts[0], 1.0) == [(truck, 1)] and g.path(parts[3], 1.0) == [(truck, 4)]
    assert [s.entity for s in g.load(truck, 1.0)] == parts
    load = g.carrier_load(truck, 1.0, model)
    assert load["order"] == order and [r["slot"] for r in load["slots"]] == [1, 2, 3, 4]
    first = parts[0] if order == "fifo" else parts[3]
    assert next(r for r in load["slots"] if r["unloads"] == 1)["entity"] == first
    assert g.path(first, 2.5) == [] and len(g.load(truck, 2.5)) == 3      # off, and back alone
    assert g.load(truck, 9.0) == []


def test_a_carrier_batch_names_the_carrier_and_its_slots():
    model = _load_model([_truck(at=0.0)], [_pallets(1.0, 4)], then_unload=True)
    events = _events(model)
    g = Grouping(events)
    (truck,), pallets = _ids(events, "TRUCK"), _ids(events, "PALLET")
    # loaded at 1, 2, 3, 4 (one a minute), unloaded at once when the fourth is aboard
    loaded = [g.stays[p][0].t0 for p in pallets]
    assert [g.path(p, loaded[-1] - 1e-9) for p in pallets[:3]] == [[(truck, k)] for k in (1, 2, 3)]
    assert g.carrier_load(truck, loaded[-1] - 1e-9)["order"] == "fifo"


def test_nesting_and_a_disposed_group_by_hand():
    """A pallet (10) batched from parts 1-2 and loaded onto truck 20: part 1 is in the pallet in
    the truck; disposing the truck ends both stays."""
    ev = lambda t, kind, eid, blk, target=None, value=0.0, e_type="P": TraceEvent(
        t=t, kind=kind, entity_id=eid, e_type=e_type, block=blk, target=target, value=value)
    events = [ev(0, "entity_created", 1, "src"), ev(0, "entity_created", 2, "src"),
              ev(1, "entity_absorbed", 1, "pal"), ev(1, "entity_absorbed", 2, "pal"),
              ev(1, "entity_created", 10, "pal", e_type="PALLET"),
              ev(2, "entity_absorbed", 10, "load", value=20.0, e_type="PALLET"),
              ev(5, "entity_disposed", 20, "out", e_type="TRUCK")]
    g = Grouping(events)
    assert g.path(1, 1.5) == [(10, 1)] and g.path(2, 3.0) == [(10, 2), (20, 1)]
    assert g.carrier_load(20, 3.0)["slots"][0]["inside"] == [
        {"slot": 1, "entity": 1, "type": "P"}, {"slot": 2, "entity": 2, "type": "P"}]
    assert g.path(1, 5.0) == [(10, 1)]          # the truck is gone; the pallet is not unpacked
    assert g.path(10, 5.0) == [] and g.path(1, 0.5) == []


def test_a_plain_batch_and_separate_round_trip():
    blocks = [create("parts", e_type="PART", entities_per_arrival=3, max_arrivals=1),
              block("batch_1", "BATCH", {"batch_size": 3, "batched_entity_type": "LOT"}),
              block("delay", "DELAY", {"distribution": "Constant", "dist_params": {"value": 5.0}}),
              block("sep", "SEPARATE", {"mode": "Retain original entity values", "attrs": []}),
              dispose("out")]
    events = _events(layout(blocks, chain(*blocks)))
    g = Grouping(events)
    parts, (lot,) = _ids(events, "PART"), _ids(events, "LOT")
    assert g.path(parts[1], 2.0) == [(lot, 2)] and g.path(parts[1], 5.0) == []
