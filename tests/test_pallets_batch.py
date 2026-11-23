"""Palletizing in a carrier BATCH and depalletizing in a keep SEPARATE, by a pallet pattern.
Every time is worked out by hand: the items are present at once, one member time each."""
import pytest

from nagasim.core import schema
from nagasim.core.trace import TraceRecorder

from conftest import block, create, dispose, layout, run_env

DECK, HEIGHT = 0.144, 0.25


def two_by_two(**limits) -> dict:
    """A 2 x 2 layer of 0.4 x 0.3 items on a 1.2 x 0.8 pallet, 2 layers unless *limits* say."""
    return {"name": "P1", "pallet": {"length": 1.2, "width": 0.8, "deck_height": DECK, "tare_kg": 25},
            "item": {"length": 0.4, "width": 0.3, "height": HEIGHT, "weight_kg": 12},
            "layers": {"A": [{"x": -0.3, "y": -0.2}, {"x": 0.3, "y": -0.2},
                             {"x": -0.3, "y": 0.2}, {"x": 0.3, "y": 0.2}]},
            "stack": [{"layer": "A"}],
            "limits": {"layers": 2, "items": None, "weight_kg": None, "height": None, **limits}}


def model(items=8, pallets=1, every=1000.0, pattern='"P1"', member_time="1", max_wait="",
          weight=None, record=None, unload=None, **batch):
    """*pallets* PALLETs (one per *every*) and *items* BOXes at 0 into a carrier BATCH;
    then a DISPOSE, or with *unload* ({SEPARATE params}) a keep SEPARATE first."""
    params = {"batch_size": 4, "batched_entity_type": "PALLET", "carrier_type": "PALLET",
              "member_time": member_time, "max_wait": max_wait, **batch}
    if pattern is not None:
        params["pattern"] = pattern
    blocks = [create("pallets", e_type="PALLET", max_arrivals=pallets, dist_params={"value": every}),
              create("boxes", e_type="BOX", entities_per_arrival=items, max_arrivals=1),
              block("palletizer", "BATCH", params)]
    links = [{"source": "pallets", "target": "palletizer"}]
    if weight is not None:
        blocks.append(block("weigh", "ASSIGN", {"assignments": [
            {"target": "entity.weight", "expr": str(weight)}]}))
        links += [{"source": "boxes", "target": "weigh"}, {"source": "weigh", "target": "palletizer"}]
    else:
        links.append({"source": "boxes", "target": "palletizer"})
    if unload is None:
        blocks.append(dispose("carriers"))
        links.append({"source": "palletizer", "target": "carriers"})
    else:
        blocks += [block("depalletizer", "SEPARATE", {"mode": "Retain original entity values",
                                                      "attrs": [], "carrier": "keep", **unload}),
                   dispose("members"), dispose("carriers")]
        links += [{"source": "palletizer", "target": "depalletizer"},
                  {"source": "depalletizer", "target": "members"},
                  {"source": "depalletizer", "target": "carriers"}]
    return layout(blocks, links, pallet_patterns=[record or two_by_two()])


def _pallets(env):
    return sorted((e for e in env.ctx.all_entities if e.e_type == "PALLET"), key=lambda e: e.id)


def test_pallet_closes_at_its_slots():
    env, _ = run_env(model(items=10, pallets=2, every=8.0, max_wait="20"), 100)
    first, second = _pallets(env)
    assert first.deletion_time == 8.0 and len(first.batch_elems) == 8
    assert (first.items, first.layers, first.pattern) == (8, 2, "P1")
    assert first.load_height == pytest.approx(DECK + 2 * HEIGHT)
    assert first.load_weight == pytest.approx(25 + 8 * 12)
    assert [m.slot for m in first.batch_elems] == list(range(1, 9))
    assert [m.layer for m in first.batch_elems] == [1] * 4 + [2] * 4
    assert second.deletion_time == 28.0 and second.items == 2      # cut off with what it holds


def test_weight_limit_closes_early_and_the_item_waits():
    env, _ = run_env(model(items=13, pallets=2, every=6.0, record=two_by_two(weight_kg=100)), 100)
    first, second = _pallets(env)
    assert [len(p.batch_elems) for p in (first, second)] == [6, 6]
    assert (first.deletion_time, second.deletion_time) == (6.0, 12.0)
    assert first.load_weight == pytest.approx(97)
    assert max(m.id for m in first.batch_elems) < min(m.id for m in second.batch_elems)


def test_an_item_weight_attribute_wins_over_the_patterns():
    env, _ = run_env(model(items=9, weight=30, record=two_by_two(weight_kg=100)), 100)
    pallet, = _pallets(env)
    assert len(pallet.batch_elems) == 2 and pallet.load_weight == pytest.approx(85)


def test_item_heavier_than_the_limit_is_a_run_error():
    with pytest.raises(Exception, match="E_PALLET_ITEM"):
        run_env(model(items=2, weight=200, record=two_by_two(weight_kg=100)), 100)


def test_pattern_expression_naming_nothing_is_a_run_error():
    with pytest.raises(Exception, match="E_PALLET_UNKNOWN.*P9"):
        run_env(model(pattern='IF(1 > 0, "P9", "P1")'), 100)


def test_a_literal_pattern_naming_nothing_is_a_problem_before_the_run():
    for pattern in ('"P9"', "P9"):
        found = [p for p in schema.validate_model_structured(model(pattern=pattern))
                 if p.code == "E_PALLET_UNKNOWN"]
        assert [(p.block, p.fields["pattern"]) for p in found] == [("palletizer", "P9")]
    assert not [p for p in schema.validate_model_structured(model(pattern="P1"))
                if p.code.startswith("E_PALLET")]


def test_sheet_time_before_each_later_layer():
    record = {**two_by_two(), "sheet": {"thickness": 0.002, "time": 4}}
    env, _ = run_env(model(record=record), 100)
    pallet, = _pallets(env)
    assert pallet.deletion_time == 12.0
    assert pallet.load_height == pytest.approx(DECK + 2 * HEIGHT + 0.002)


def test_layer_at_a_time():
    env, _ = run_env(model(member_time="5", place_per="layer"), 100)
    pallet, = _pallets(env)
    assert pallet.deletion_time == 10.0 and pallet.items == 8


def test_layer_at_a_time_cut_off_mid_layer():
    env, _ = run_env(model(items=6, member_time="5", place_per="layer", max_wait="20"), 100)
    pallet, = _pallets(env)
    assert pallet.deletion_time == 25.0 and pallet.items == 6 and pallet.layers == 2


def _trace(m):
    recorder = TraceRecorder()
    run_env(m, 100, subscribers=[recorder])
    return [(e.t, e.kind, e.entity_id, e.block, e.target) for e in recorder.events]


def test_no_pattern_is_unchanged():
    plain = model(pattern=None, items=10, pallets=3, every=2.0, max_wait="3")
    empty = model(pattern="", items=10, pallets=3, every=2.0, max_wait="3", place_per="item")
    assert _trace(plain) == _trace(empty)
    assert len({e for e in _trace(plain) if e[1] == "entity_disposed"}) > 0


# -- depalletizing (a keep SEPARATE) -----------------------------------------------------
def _unloaded(m, run_len=200):
    recorder = TraceRecorder()
    env, _ = run_env(m, run_len, subscribers=[recorder])
    boxes = {e.id: e for e in env.ctx.all_entities if e.e_type == "BOX"}
    out = [(boxes[e.entity_id], e.t) for e in recorder.events
           if e.kind == "entity_moved" and e.block == "depalletizer" and e.e_type == "BOX"]
    carriers = [e.t for e in recorder.events
                if e.kind == "entity_moved" and e.block == "depalletizer" and e.e_type == "PALLET"]
    return env, out, carriers


def test_depalletize_top_layer_first():
    env, out, carriers = _unloaded(model(member_time="0", unload={"member_time": "1"}))
    assert [b.slot for b, _t in out] == list(range(8, 0, -1))
    assert [t for _b, t in out] == [float(k) for k in range(1, 9)]
    assert carriers == [8.0]
    pallet, = _pallets(env)
    assert (pallet.items, pallet.layers) == (0, 0)


def test_cut_off_pallet_unloads_only_what_it_holds():
    _env, out, _c = _unloaded(model(items=6, member_time="0", max_wait="5",
                                    unload={"member_time": "1"}))
    assert [b.slot for b, _t in out] == [6, 5, 4, 3, 2, 1]
    # A carrier loaded with no pattern keeps fifo, under the new default too.
    _env, out, _c = _unloaded(model(pattern=None, member_time="0", unload={"member_time": "1"}))
    assert [b.id for b, _t in out] == sorted(b.id for b, _t in out)
    assert len(out) == 4


def test_explicit_fifo_still_wins():
    _env, out, _c = _unloaded(model(member_time="0", unload={"member_time": "1",
                                                            "release_order": "fifo"}))
    assert [b.slot for b, _t in out] == list(range(1, 9))


def test_carrier_attributes_follow_the_unload():
    m = model(member_time="0", unload={"member_time": "1"})
    env, _ = run_env(m, 3.5)
    pallet, = _pallets(env)
    assert pallet.items == 5 and pallet.load_weight == pytest.approx(25 + 5 * 12)
    assert pallet.layers == 2
    env, _ = run_env(m, 4.5)
    pallet, = _pallets(env)
    assert pallet.layers == 1 and pallet.load_height == pytest.approx(DECK + HEIGHT)


def test_depalletize_a_layer_at_a_time():
    _env, out, carriers = _unloaded(model(member_time="0", unload={"member_time": "3",
                                                                  "place_per": "layer"}))
    assert [t for _b, t in out] == [3.0] * 4 + [6.0] * 4 and carriers == [6.0]


def test_the_display_knows_the_pattern_order():
    from nagasim.core.grouping import Grouping
    m = model(member_time="0", unload={"member_time": "1"})
    recorder = TraceRecorder()
    run_env(m, 100, subscribers=[recorder])
    g = Grouping(recorder.events)
    pallet = next(e.entity_id for e in recorder.events if e.e_type == "PALLET")
    load = g.carrier_load(pallet, 0.5, m)
    assert load["order"] == "pattern"
    assert [r["unloads"] for r in load["slots"]] == list(range(8, 0, -1))
    plain = model(pattern=None, member_time="0", unload={"member_time": "1"})
    recorder = TraceRecorder()
    run_env(plain, 100, subscribers=[recorder])
    pallet = next(e.entity_id for e in recorder.events if e.e_type == "PALLET")
    assert Grouping(recorder.events).carrier_load(pallet, 0.5, plain)["order"] == "fifo"
