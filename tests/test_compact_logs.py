"""D1061: the resource usage log, the flat entity log and the per-type samples are
kept packed. They read back exactly what was put in."""
import json
import pickle
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import nagasim
from nagasim.core import resource_manager, stats
from nagasim.core.sim_runner import run_single_simulation
from nagasim.settings import config

from nagasim.core.sim_classes import Entity, EntityRecord
from nagasim.core.stats import FloatSeries, Tally
from nagasim.core.utils import ResourceLog, ResourceSnapshot


def test_resource_log_keeps_the_newest_rows_in_order_and_the_int_times():
    log = ResourceLog(maxlen=3)
    log.add(0, 1, 0, "request")
    log.add(1.5, 1, 1, "seize")
    log.add(2, 0, 0, None)
    log.add(3.25, 2, 4, "release")          # full: drops the oldest
    rows = list(log)
    assert rows == [ResourceSnapshot(1.5, 1, 1, "seize"), ResourceSnapshot(2, 0, 0, None),
                    ResourceSnapshot(3.25, 2, 4, "release")]
    assert [type(r.time) for r in rows] == [float, int, float]
    assert log[0] == rows[0] and log[-1] == rows[-1] and len(log) == 3
    assert list(pickle.loads(pickle.dumps(log))) == rows
    columns = log.columns()                 # what the result readers take instead (#1561)
    assert columns == ([1.5, 2, 3.25], [1, 0, 2], [1, 0, 4], ["seize", None, "release"])
    assert [type(t) for t in columns[0]] == [float, int, float]


def test_float_series_packs_floats_and_keeps_any_other_value_as_given():
    series = FloatSeries([1.5, 2.0])
    assert list(series) == [1.5, 2.0] and type(series._data).__name__ == "array"
    series.append(3)
    assert list(series) == [1.5, 2.0, 3] and type(series[-1]) is int
    assert series == [1.5, 2.0, 3] and pickle.loads(pickle.dumps(series)) == series


def test_tally_values_are_packed_floats():
    tally = Tally()
    tally.add(2)
    tally.add(4.0)
    assert list(tally.values) == [2.0, 4.0] and tally.mean == 3.0 and tally.percentile(50) == 3.0


def test_entity_record_reads_as_the_entity_it_replaces():
    entity = Entity(7)
    entity.e_type = "part"
    entity.creation_time, entity.deletion_time = 1.0, 4.5
    entity.throughput_time = 3.5
    entity.cost = 2.5
    entity.finished = True
    entity.tag = "A"
    record = EntityRecord.of(entity)
    assert (record.id, record.e_type, record.throughput_time, record.cost) == (7, "PART", 3.5, 2.5)
    assert record.tag == "A" and record.name == "Entity 7" and record.batch_elems == []
    assert record.in_batch is None and record.finished is True and record._creation_time == 1.0
    assert not hasattr(record, "missing")
    plain = Entity(8)
    plain.creation_time = plain.deletion_time = 0.0
    assert EntityRecord.of(plain)._extra is None       # a plain entity pays for no dict


# -- D1061's "bit-identical" claim, held against the containers it replaced ----------

class _SnapshotDeque(deque):
    """The resource log before D1061: a deque of `ResourceSnapshot` objects."""

    def add(self, time, in_use, queue_length, event_type=None):
        self.append(ResourceSnapshot(time, in_use, queue_length, event_type))

    def columns(self):
        """What the readers take since the snapshot path left `calc_kpis`."""
        return tuple(list(c) for c in zip(*((s.time, s.in_use, s.queue_length, s.event_type) for s in self))) or ([], [], [], [])


def _kpi_frames(monkeypatch, old: bool):
    """One bundled-model run with the flat logs on, the entity log capped small and the resource
    log overflowing (so both truncation paths run); with *old*, the containers D1061 replaced:
    deque of snapshots, whole entities, list-backed Tally."""
    monkeypatch.setattr(config, "LOG_HISTORY", 40)
    if old:
        monkeypatch.setattr(resource_manager, "ResourceLog", lambda maxlen: _SnapshotDeque(maxlen=maxlen))
        monkeypatch.setattr(stats, "EntityRecord", SimpleNamespace(of=lambda entity: entity))
        monkeypatch.setattr(Tally, "reset", lambda self: setattr(self, "values", []))
    doc = json.loads((Path(nagasim.__file__).parent / "models" / "call_centre_day.json").read_text(encoding="utf-8"))
    doc["simulation"].update(record_logs=True, entity_log_limit=25, time_limit=1500)
    return run_single_simulation(doc, seed=2, run_len=1500.0, warmup=200.0)


def test_compact_logs_give_the_frames_and_kpis_of_the_plain_containers(monkeypatch):
    new = _kpi_frames(monkeypatch, old=False)
    monkeypatch.undo()
    old = _kpi_frames(monkeypatch, old=True)
    system = new["system"].set_index("Metric")["Value"]
    assert system["Entity Log Truncated"] == 1 and system["Resource/Store Log Truncated"] == 1
    assert new.keys() == old.keys()
    for name in new:
        pd.testing.assert_frame_equal(new[name], old[name], check_exact=True, obj=name)
    assert len(new["resource_log"]) == 40       # the cap held; the entity log adds the live entities
