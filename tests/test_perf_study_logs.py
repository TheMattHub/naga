"""Section 7 item 4 of the performance report (2026-10-07): a study runs with the
per-event logs off unless a KPI it names is computed from them.

The first test is the guard on `outputs.LOG_DERIVED_COLUMNS`: whoever adds a KPI
that reads a log fails it until the declaration is updated."""
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nagasim import api, calibrate as cal, experiments, optimize
from nagasim.core.outputs import LOG_DERIVED_COLUMNS
from nagasim.optimize import Factor, evaluation

MODELS = Path(api.__file__).parent / "models"
# Resources, stores and queues between them; every declared column shows up.
PROOF_MODELS = ["single_server", "warehouse_library", "inventory_sS", "automation_floor",
                "hold_signal_flow", "mfg_line_library", "call_centre_day"]
SINGLE_SERVER = MODELS / "single_server.json"
KPI = "system/Avg Throughput Time"
LOG_KPI = "resource/Server/Queue Len P95"
CAPACITY = Factor("resources.Server.capacity", "integer", 1, 3)


def _differing_columns(on, off) -> dict[str, set]:
    out: dict[str, set] = {}
    for name, a in on.items():
        b = off[name]
        if not isinstance(a, pd.DataFrame) or name.endswith("_log"):
            continue                       # the log frames themselves are empty by design
        if name == "system":
            a, b = a.set_index("Metric")["Value"], b.set_index("Metric")["Value"]
            cols = {m for m in a.index if str(a[m]) != str(b.get(m))}
        else:
            assert list(a.columns) == list(b.columns) and len(a) == len(b), name
            # str() per value, not astype(str): pandas 3 keeps a NaN missing there, and a
            # missing value equals nothing, so an all-NaN column differed from itself.
            cols = {c for c in a.columns if [str(v) for v in a[c]] != [str(v) for v in b[c]]}
        if cols:
            out[name] = cols
    return out


def test_log_derived_columns_are_exactly_the_ones_that_change_without_logs():
    seen: dict[str, set] = {}
    for model in PROOF_MODELS:
        path = str(MODELS / f"{model}.json")
        on = api.run_model(path, seed=0, replications=1)
        off = api.run_model(path, seed=0, replications=1, record_logs=False)
        for frame, cols in _differing_columns(on, off).items():
            assert cols <= LOG_DERIVED_COLUMNS.get(frame, frozenset()), (model, frame, cols)
            seen.setdefault(frame, set()).update(cols)
    # And nothing declared is a column that never changes: the declaration is not padded.
    assert {frame: set(cols) for frame, cols in LOG_DERIVED_COLUMNS.items()} == seen


def test_needs_event_logs_reads_flatten_keys():
    needs = experiments.needs_event_logs
    assert not needs([])
    assert not needs([KPI, "system/Total Cost", "resource/Server/Usage % (snapshot)",
                      "store/Buffer/Average Size", "entity/DEFAULT/Avg Time"])
    assert needs(["resource/Server/Queue Length Mode"])
    assert needs(["store/Buffer/P99 Size"])
    assert needs([KPI, "resource/A/B/Queue Len Std"])      # an object name may hold a "/"
    assert needs(["entity_log/Throughput Time"])           # a calibration sample target
    # The default responses (`system/...`) of a DOE never ask for a log.
    first = experiments.flatten_kpis(api.run_model(str(SINGLE_SERVER), seed=0, replications=1,
                                                   run_length=200))
    assert not needs(k for k in first["KPI"] if k.startswith("system/"))


def _logs_flag(layout) -> bool:
    return layout["simulation"]["record_logs"]


def test_study_layout_resolution():
    layout = api.load_layout(SINGLE_SERVER)
    assert _logs_flag(evaluation.study_layout(layout, [KPI])) is False
    assert _logs_flag(evaluation.study_layout(layout, [KPI, LOG_KPI])) is True
    # An explicit value from the caller wins, both ways.
    assert _logs_flag(evaluation.study_layout(layout, [LOG_KPI], False)) is False
    assert _logs_flag(evaluation.study_layout(layout, [KPI], True)) is True
    # The study's need overrides a model that switched its own logs off ...
    quiet = api.with_run_setting(layout, "record_logs", False)
    assert _logs_flag(evaluation.study_layout(quiet, [LOG_KPI])) is True
    # ... and a model that has them on is still run without them when nothing needs them.
    assert _logs_flag(evaluation.study_layout(layout, [KPI])) is False
    # The caller's layout is never mutated.
    assert "record_logs" not in layout.get("simulation", {})


def _design():
    return optimize.full_factorial([CAPACITY], 2)


def _run(responses, **kwargs):
    return optimize.run_design(api.load_layout(SINGLE_SERVER), [CAPACITY], _design(),
                               responses=responses, replications=3, seed=5, run_length=300,
                               warmup=30, **kwargs)


def _table(result, response):
    return [list(result.values[i][response]) for i in sorted(result.values)]


def test_a_study_naming_only_plain_kpis_is_unchanged_by_the_logs():
    default = _run([KPI])                       # resolves to logs off
    forced_on = _run([KPI], record_logs=True)
    assert _table(default, KPI) == _table(forced_on, KPI)


def test_a_study_naming_a_log_kpi_gets_real_values_equal_to_a_logs_on_run():
    default = _run([LOG_KPI])                   # resolves to logs on
    forced_on = _run([LOG_KPI], record_logs=True)
    values = _table(default, LOG_KPI)
    assert np.isfinite(np.concatenate(values)).all()
    assert values == _table(forced_on, LOG_KPI)
    # An explicit False is honoured: the blank column is dropped by `flatten_kpis`,
    # which is what a silent default would have handed this study.
    silent = _run([LOG_KPI], record_logs=False)
    assert all(LOG_KPI not in point for point in silent.values.values())


def test_a_model_with_logs_off_still_gets_a_requested_log_kpi():
    quiet = api.with_run_setting(api.load_layout(SINGLE_SERVER), "record_logs", False)
    result = optimize.run_design(quiet, [CAPACITY], _design(), responses=[LOG_KPI],
                                 replications=2, seed=5, run_length=300)
    assert np.isfinite(np.concatenate(_table(result, LOG_KPI))).all()


def _spy(monkeypatch):
    """The `record_logs` setting every `_evaluate_point` call actually received."""
    real, seen = evaluation._evaluate_point, []

    def wrapped(layout, *args, **kwargs):
        seen.append(layout["simulation"]["record_logs"])
        return real(layout, *args, **kwargs)

    monkeypatch.setattr(evaluation, "_evaluate_point", wrapped)
    return seen


def test_every_entry_point_resolves_through_its_own_requested_kpis(monkeypatch):
    seen = _spy(monkeypatch)
    layout = api.load_layout(SINGLE_SERVER)
    small = dict(run_length=200, warmup=20)

    optimize.optimize(layout, [CAPACITY], response=KPI, budget=8, replications_per_point=2, **small)
    assert set(seen) == {False}
    seen.clear()
    optimize.optimize(layout, [CAPACITY], response=KPI, constraints=[(LOG_KPI, "<=", 1e9)],
                      budget=8, replications_per_point=2, **small)
    assert set(seen) == {True}
    seen.clear()

    optimize.tornado(layout, [CAPACITY], KPI)
    assert set(seen) == {False}
    seen.clear()
    optimize.tornado(layout, [CAPACITY], LOG_KPI)
    assert set(seen) == {True}
    seen.clear()

    optimize.select_best(layout, [[], [{"path": CAPACITY.name, "value": 2}]], response=LOG_KPI,
                         n0=2, max_replications=3, **small)
    assert set(seen) == {True}
    seen.clear()
    optimize.sobol(layout, [CAPACITY], KPI, n=2, bootstrap=0, **small)
    assert set(seen) == {False}
    seen.clear()

    # Calibration: a scalar KPI target needs no log, a distributional one reads the entity log.
    cal.calibrate(layout, [{"kpi": KPI, "observed": 5.0}], [CAPACITY], budget=6,
                  replications_per_point=2, **small)
    assert set(seen) == {False}
    seen.clear()
    cal.calibrate(layout, [{"kpi": "entity_log/Throughput Time", "observed_sample": [1.0, 2.0, 3.0]}],
                  [CAPACITY], budget=6, replications_per_point=2, **small)
    assert set(seen) == {True}


def test_the_result_a_worker_returns_is_smaller_without_logs():
    run = lambda flag: pickle.dumps(api.run_model(str(MODELS / "warehouse_library.json"),
                                                  seed=0, replications=1, record_logs=flag))
    assert len(run(False)) < len(run(True))
