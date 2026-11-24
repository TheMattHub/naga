"""Speed review E (2026-11-23): a cheaper record per disposed entity. The record and every
result frame stay what the record of D1061 gave, value for value; the disposal path makes
fewer Python calls per entity."""
import cProfile
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import nagasim
from nagasim import api
from nagasim.core import sim_classes, stats
from nagasim.core.sim_classes import _DEFAULTS, _IN_SLOTS, Entity, EntityRecord

MODELS = Path(nagasim.__file__).parent / "models"


def _old_of(entity):
    """`EntityRecord.of` as it was before review E (4cf802a8), verbatim: the oracle."""
    record = EntityRecord()
    record._id = entity.id
    record.e_type = sys.intern(entity.e_type)
    record.creation_time = entity.creation_time
    record.deletion_time = entity.deletion_time
    record.cost = entity.cost
    record._holding_cost = entity._holding_cost
    record.initial = getattr(entity, "initial", False)
    record.agent = getattr(entity, "agent", None)
    attrs = vars(entity)
    default_name = f"Entity {record._id}"
    kept = {key for key in attrs.keys() - _IN_SLOTS
            if not (key in _DEFAULTS and attrs[key] is _DEFAULTS[key])
            and not (key == "name" and attrs[key] == default_name)
            and not (key == "batch_elems" and type(attrs[key]) is list and not attrs[key])}
    record._extra = {key: value for key, value in attrs.items() if key in kept} if kept else None
    return record


def _fields(record):
    extra = record._extra
    return ([getattr(record, slot) for slot in EntityRecord.__slots__ if slot != "_extra"],
            None if extra is None else list(extra.items()))


def _entities():
    def entity(entity_id, **attrs):
        made = Entity(entity_id)
        made.e_type = "part"
        made.creation_time, made.deletion_time = 1.0, 4.5
        made.finished = True
        for key, value in attrs.items():
            setattr(made, key, value)
        return made

    yield entity(1)
    yield entity(2, tag="A")                            # same type, another shape
    yield entity(3, priority=2, tag="B")                # the same names in another order
    yield entity(4, tag="B", priority=2)
    yield entity(5, name="renamed", finished=False)
    yield entity(6, _resource="cell", initial=True, agent="walker")
    yield entity(7, batch_elems=[Entity(70)], cost=3.0, _holding_cost=1.0)
    yield entity(8, batch_elems=(), name="Entity 8")    # an empty tuple is not the default list
    yield entity(9, _ends=5.0, _since=2.0, _auto_cost=1.5, in_batch=None)
    gone = entity(10, _reserved=("x", 1), tag="C")      # an attribute added, then deleted
    del gone._reserved
    yield gone
    yield entity(11, finished=None, name=11)


@pytest.fixture
def fresh_caches(monkeypatch):
    """Empty record caches, so a miss and a hit both run whatever ran before."""
    monkeypatch.setattr(sim_classes, "_OUTSIDE", {})
    monkeypatch.setattr(sim_classes, "_TYPE_NAMES", {})


def test_the_record_holds_what_the_old_record_held_field_for_field(fresh_caches):
    for entity in _entities():
        for _ in range(2):                              # a cache miss, then a hit
            new, old = EntityRecord.of(entity), _old_of(entity)
            assert _fields(new) == _fields(old), entity.id
            assert new.e_type is old.e_type             # still the interned name


def _frames(doc: dict, *, old: bool, monkeypatch, seen=None, **run):
    if old:
        monkeypatch.setattr(stats, "EntityRecord", SimpleNamespace(of=_old_of))
    elif seen is not None:
        def spy(entity):
            record = EntityRecord.of(entity)
            seen.append(record._extra is not None)
            return record
        monkeypatch.setattr(stats, "EntityRecord", SimpleNamespace(of=spy))
    try:
        return api.run_model(doc, keep_empty=True, **run)
    finally:
        monkeypatch.undo()


def _doc(name, **simulation):
    doc = json.loads((MODELS / name).read_text(encoding="utf-8"))
    doc["simulation"].update(record_logs=True, **simulation)
    return doc


CASES = [   # (model, simulation overrides, run_model keywords): warm-up, replications,
            # submodels, batches, due dates, a capped entity log
    ("line_with_buffers.json", {}, {"replications": 2, "run_length": 900.0}),
    ("mfg_line_library.json", {}, {"replications": 2, "run_length": 800.0}),
    ("warehouse_library.json", {}, {"replications": 2}),
    ("palletizing_line.json", {}, {"replications": 1}),
    ("order_due_dates.json", {}, {"replications": 2}),
    ("call_centre_day.json", {"entity_log_limit": 25}, {"replications": 2, "run_length": 1500.0, "warmup": 200.0}),
]


def test_every_result_frame_is_the_frame_of_the_old_record(monkeypatch):
    seen = []
    for name, simulation, run in CASES:
        new = _frames(_doc(name, **simulation), old=False, monkeypatch=monkeypatch, seen=seen, **run)
        old = _frames(_doc(name, **simulation), old=True, monkeypatch=monkeypatch, **run)
        assert new.keys() == old.keys(), name
        assert len(new["entity_log"]) > 0, name
        for frame in new:
            if isinstance(new[frame], pd.DataFrame):
                assert new[frame].equals(old[frame]), (name, frame)
            else:
                assert repr(new[frame]) == repr(old[frame]), (name, frame)
    assert any(seen) and not all(seen)      # records with and without attributes of their own


@pytest.mark.perf
@pytest.mark.slow
# cProfile's counts differ between Python versions; the public engine CI runs 3.11-3.14
@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="call budgets measured on 3.12")
def test_disposal_makes_few_calls_per_entity(fresh_caches):
    """Before review E, about 24 calls per disposed entity under `account_and_retain` on
    single_server (the record 9, a thrown-away accumulator 3, the due-date check 1); after, 11."""
    doc = _doc("single_server.json")
    profile = cProfile.Profile()
    profile.enable()
    api.run_model(doc, replications=1, seed=0)
    profile.disable()
    rows = {}
    for entry in profile.getstats():
        code = entry.code
        if not isinstance(code, str) and "nagasim" in code.co_filename and code.co_filename.endswith(("stats.py", "sim_classes.py")):
            rows[code.co_name] = rows.get(code.co_name, 0) + entry.callcount
            if code.co_name in ("account_and_retain", "of"):
                rows[code.co_name + " callees"] = sum(c.callcount for c in entry.calls or [])
    disposed = rows["account_and_retain"]
    assert disposed > 500
    assert "account_due_date" not in rows               # no orders, no due dates
    assert rows["of callees"] <= 20                     # cache misses only: a few shapes, one type
    assert (rows["account_and_retain callees"] + rows["of callees"]) / disposed < 12        # 11 now; the per-entity throwaway accumulator would make it 12


def test_an_aged_warm_started_entity_counts_as_created_on_both_record_paths(monkeypatch):
    """Speed review R5: `account_and_retain` reads an Entity's fields and writes
    `created_in_window` out, clamp included: a warm-started entity's creation time is minus
    its age, and it still counts as created at t=0. The frames equal those of the general path
    (the Entity type hidden from `stats`)."""
    from conftest import chain, create, dispose, layout, process
    c = create("c", start_time=1000)
    p, d = process("p", value=1.5), dispose("d")
    doc = layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 1}],
                 simulation={"time_limit": 20, "warmup": 0, "replications": 1},
                 initial_state={"entities": [{"block": "p", "count": 3, "age": 4.0}]})
    fast = api.run_model(doc, seed=2)
    assert fast["entity_summary"]["Total Created"].sum() == 3
    monkeypatch.setattr(stats, "Entity", type("NotPlain", (), {}))
    slow = api.run_model(doc, seed=2)
    for key, frame in fast.items():
        if isinstance(frame, pd.DataFrame):
            pd.testing.assert_frame_equal(frame, slow[key], check_exact=True, obj=key)
