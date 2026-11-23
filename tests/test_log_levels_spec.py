"""Spec tests for run log levels, Tasks 1-3 of
docs/superpowers/plans/2026-11-23-run-log-levels.md, written from the plan before the code.

Every expected value is derived by hand from the plan's "Level definitions" table and rules;
each test's docstring names the plan line it comes from. `nagasim.loglevels` is imported inside
the tests, so the file collects before the module exists and its tests fail until it does.
"""
import copy
import functools
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nagasim import api, laws, optimize
from nagasim.core.outputs import LOG_DERIVED_COLUMNS
from nagasim.core.schema import fatal_problems, validate_model_structured
from nagasim.modelschema import build_json_schema
from nagasim.optimize import Factor, evaluation

MODELS = Path(api.__file__).parent / "models"
SINGLE = str(MODELS / "single_server.json")        # resources, no stores, no dashboard
STORES = str(MODELS / "inventory_sS.json")          # stores and store_log
DASH = str(MODELS / "ed_game.json")                 # dashboard, timeseries and all logs, 0.07 s
SHORT = dict(seed=0, replications=2, run_length=300, warmup=30)   # single_server's warm-up is 100
KPI = "system/Avg Throughput Time"
LOG_KPI = "resource/Server/Queue Len P95"
SERIES_KPI = "timeseries/WIP/max"
LOG_FRAMES = ("entity_log", "resource_log", "store_log")
SERIES_FRAMES = ("timeseries", "dashboard")
ALL_NAMES = ("kpi", "series", "full", "study", "explore", "replay")


def _ll():
    from nagasim import loglevels
    return loglevels


def _layout(path=SINGLE, **sim):
    layout = api.load_layout(path)
    for key, value in sim.items():
        layout = api.with_run_setting(layout, key, value)
    return layout


def _sim(layout) -> dict:
    return layout.get("simulation") or {}


# ---------- shared runs (cached helpers, not fixtures, so a missing feature fails, not errors) ----
@functools.lru_cache(maxsize=None)
def _single(level=None, record_logs=None, keep_empty=False):
    kw = dict(SHORT, keep_empty=keep_empty)
    if level is not None:
        kw["log_level"] = level
    if record_logs is not None:
        kw["record_logs"] = record_logs
    return api.run_model(SINGLE, **kw)


@functools.lru_cache(maxsize=None)
def _stores(level=None):
    kw = {} if level is None else {"log_level": level}
    return api.run_model(STORES, seed=0, replications=1, **kw)


@functools.lru_cache(maxsize=None)
def _dash(level=None):
    kw = {} if level is None else {"log_level": level}
    return api.run_model(DASH, seed=0, replications=1, **kw)


def _not_recorded(results, frame):
    """The NotRecordedError `results[frame]` raises (its module is not named by the plan)."""
    with pytest.raises(KeyError) as caught:
        results[frame]
    assert type(caught.value).__name__ == "NotRecordedError", caught.value
    return caught.value.args[0]


def _assert_frames_equal(a, b):
    assert sorted(a) == sorted(b)
    for name in a:
        pd.testing.assert_frame_equal(a[name], b[name], check_exact=True)


# ---------- Task 1: the module's tables ---------------------------------------------------------
def test_level_and_profile_tables():
    """Plan Task 1: LEVELS = ("kpi", "series", "full"), PROFILES = {"study": "kpi", "explore":
    "full", "replay": "full"}, FRAME_LEVEL as written, COLUMN_LEVEL from LOG_DERIVED_COLUMNS
    (all full; the plan says ten columns)."""
    ll = _ll()
    assert tuple(ll.LEVELS) == ("kpi", "series", "full")
    assert dict(ll.PROFILES) == {"study": "kpi", "explore": "full", "replay": "full"}
    assert dict(ll.FRAME_LEVEL) == {"timeseries": "series", "dashboard": "series",
                                    "entity_log": "full", "resource_log": "full",
                                    "store_log": "full"}
    cols = [c for cs in LOG_DERIVED_COLUMNS.values() for c in cs]
    assert len(cols) == 10
    assert len(ll.COLUMN_LEVEL) == 10
    assert set(ll.COLUMN_LEVEL.values()) == {"full"}
    # Keyed by column or "frame.column"; either spelling names every declared column.
    keys = {str(k).rpartition(".")[2] for k in ll.COLUMN_LEVEL}
    assert keys == set(cols)


def test_resolve_maps_profiles_and_refuses_other_names():
    """Plan Task 1: resolve(name) -> level; profiles map to their level; anything else raises
    ValueError naming the choices."""
    ll = _ll()
    assert [ll.resolve(n) for n in ALL_NAMES] == ["kpi", "series", "full", "kpi", "full", "full"]
    with pytest.raises(ValueError) as caught:
        ll.resolve("everything")
    for name in ALL_NAMES:
        assert name in str(caught.value)


def test_level_of_derives_from_the_two_switches():
    """Plan rule: "The result's level is derived from the two switches it actually ran with
    (True: full; False with the sampler on: series; both off: kpi)"; defaults record_logs=True
    (schema default) and timeseries_points=200 (context default)."""
    level_of = _ll().level_of
    assert level_of({}) == "full"
    assert level_of({"record_logs": True}) == "full"
    assert level_of({"record_logs": True, "timeseries_points": 0}) == "full"
    assert level_of({"record_logs": False}) == "series"
    assert level_of({"record_logs": False, "timeseries_points": 168}) == "series"
    assert level_of({"record_logs": False, "timeseries_points": 0}) == "kpi"


# ---------- Task 1: apply_level gives the table's switches -----------------------------------
@pytest.mark.parametrize("name, logs, points", [
    ("kpi", False, 0), ("series", False, 200), ("full", True, 200),
    ("study", False, 0), ("explore", True, 200), ("replay", True, 200)])
def test_apply_level_gives_the_table(name, logs, points):
    """Plan "Level definitions": kpi = (False, 0); series = (False, model's value else 200);
    full = (True, model's value else 200); profiles via their level. apply_level also writes the
    named level as simulation.log_level. single_server sets neither switch."""
    ll = _ll()
    layout = _layout()
    before = copy.deepcopy(layout)
    out = ll.apply_level(layout, ll.resolve(name))
    sim = _sim(out)
    assert sim["record_logs"] is logs
    assert sim.get("timeseries_points", 200) == points
    assert ll.resolve(sim["log_level"]) == ll.resolve(name)
    assert layout == before                       # "caller's dict not mutated"


def test_a_level_never_raises_timeseries_points():
    """Plan rule: "A level never raises timeseries_points above the model's value; kpi forces it
    to 0. A model with timeseries_points: 0 run at series stays at 0"."""
    ll = _ll()
    fifty = _layout(timeseries_points=50)
    assert _sim(ll.apply_level(fifty, "series"))["timeseries_points"] == 50
    assert _sim(ll.apply_level(fifty, "full"))["timeseries_points"] == 50
    assert _sim(ll.apply_level(fifty, "kpi"))["timeseries_points"] == 0
    zero = _layout(timeseries_points=0)
    assert _sim(ll.apply_level(zero, "series"))["timeseries_points"] == 0
    assert _sim(ll.apply_level(zero, "full"))["timeseries_points"] == 0


def test_a_level_overrides_the_model_switches():
    """Plan rule: precedence, the model's simulation.log_level (and any level) wins over the
    model's own record_logs / timeseries_points."""
    ll = _ll()
    on = _layout(record_logs=True)
    assert _sim(ll.apply_level(on, "kpi"))["record_logs"] is False
    off = _layout(record_logs=False)
    assert _sim(ll.apply_level(off, "full"))["record_logs"] is True


# ---------- Task 1: run_model argument, precedence, no level = today --------------------------
def test_run_model_records_level_on_results():
    """Plan Task 1: Results.log_level from level_of on the settings the run used: default ->
    full; record_logs=False -> series (sampler still at 200); log_level="kpi" -> kpi; a profile
    resolves (study -> kpi, explore and replay -> full)."""
    assert _single().log_level == "full"
    assert _single(record_logs=False).log_level == "series"
    assert _single("kpi").log_level == "kpi"
    assert _single("series").log_level == "series"
    assert _single("full").log_level == "full"
    assert _single("study").log_level == "kpi"
    # Legacy logs-off runs are only labelled (rule O4): the argument and the model setting.
    assert api.run_model(_layout(record_logs=False), **SHORT).log_level == "series"
    assert api.run_model(SINGLE, log_level="explore", **SHORT).log_level == "full"
    assert api.run_model(SINGLE, log_level="replay", **SHORT).log_level == "full"


def test_a_study_result_carries_its_level():
    """Plan Task 1: run_design results and ExperimentResult carry log_level; Task 3: a study
    naming only plain KPIs runs at kpi."""
    ll = _ll()
    factor = Factor("resources.Server.capacity", "integer", 1, 2)
    design = optimize.full_factorial([factor], 2)
    result = optimize.run_design(_layout(), [factor], design, responses=[KPI], replications=1,
                                 seed=0, run_length=200, warmup=20)
    assert result.log_level == "kpi"
    from nagasim import experiments
    study = experiments.run_scenarios(_layout(), replications=1, run_length=200, warmup=20)
    # The plan does not route run_scenarios through study_layout: only that it is labelled.
    assert study.log_level in ll.LEVELS


def test_record_logs_and_log_level_together_are_refused():
    """Plan Task 1: record_logs= stays and, given together with log_level, raises ValueError
    ("give one")."""
    for logs in (True, False):
        with pytest.raises(ValueError) as caught:
            api.run_model(SINGLE, record_logs=logs, log_level="kpi", **SHORT)
        assert "record_logs" in str(caught.value) and "log_level" in str(caught.value)


def test_run_model_refuses_an_unknown_level():
    """Plan Task 1: resolve raises ValueError naming the choices; run_model resolves through it."""
    with pytest.raises(ValueError) as caught:
        api.run_model(SINGLE, log_level="most", **SHORT)
    for name in ALL_NAMES:
        assert name in str(caught.value)


def test_no_level_anywhere_is_full_as_today():
    """Plan Decision: a plain run with no level records FULL, as today; full = (True, model's
    value else 200), which is today's default, so the frames are identical."""
    _assert_frames_equal(_single(), _single("full"))


def test_model_record_logs_false_with_no_level_is_unchanged():
    """Plan constraint "No breaking change": a model that sets record_logs itself, with no level,
    behaves as today, i.e. as the record_logs=False argument does."""
    quiet = _layout(record_logs=False)
    own = api.run_model(quiet, **SHORT)
    _assert_frames_equal(own, _single(record_logs=False))


def test_model_log_level_beats_model_switches_and_caller_beats_model():
    """Plan rule: "a caller's level ... wins over the model's simulation.log_level, which wins
    over the model's own record_logs / timeseries_points"."""
    model = _layout(record_logs=True, log_level="kpi")
    by_model = api.run_model(model, **SHORT)
    assert by_model.log_level == "kpi"
    assert "entity_log" not in by_model and "timeseries" not in by_model
    by_caller = api.run_model(model, log_level="full", **SHORT)
    assert by_caller.log_level == "full"
    assert "entity_log" in by_caller and "timeseries" in by_caller


# ---------- Task 1: schema -----------------------------------------------------------------------
def test_schema_refuses_a_bad_log_level_and_lists_the_choices():
    """Plan Task 1: simulation.log_level is an enum of levels and profiles; validation refuses
    other values with the list."""
    fatal = fatal_problems(validate_model_structured(_layout(log_level="verbose")))
    said = [p.message for p in fatal if "log_level" in p.message]
    assert said, [p.message for p in fatal]
    for name in ALL_NAMES:
        assert name in said[0]
    for name in ALL_NAMES:
        ok = fatal_problems(validate_model_structured(_layout(log_level=name)))
        assert not [p for p in ok if "log_level" in p.message], name


def test_json_schema_declares_log_level_enum_without_default():
    """Plan Task 1: simulation.log_level (enum of levels and profiles, no default) in the JSON
    Schema."""
    prop = build_json_schema()["$defs"]["simulation"]["properties"]["log_level"]
    assert set(prop["enum"]) == set(ALL_NAMES)
    assert "default" not in prop


# ---------- Task 2: frames below full are absent and say why ----------------------------------
@pytest.mark.parametrize("frame, needed", [("entity_log", "full"), ("resource_log", "full"),
                                           ("timeseries", "series")])
def test_kpi_run_frames_are_absent_and_name_the_level(frame, needed):
    """Plan Task 2: at a named kpi the logs and (sampler off) timeseries are not built; __missing__
    raises NotRecordedError(KeyError): "<frame> was not recorded: this run was at level kpi;
    re-run with log_level='<needed>'"."""
    assert len(_single()[frame]) > 0                  # non-vacuous: the full run has it
    results = _single("kpi")
    assert frame not in results
    msg = _not_recorded(results, frame)
    assert frame in msg and "not recorded" in msg
    assert "level kpi" in msg and f"log_level='{needed}'" in msg


def test_store_log_absent_below_full():
    """Plan Task 2: store_log is one of the three log frames absent below full (kpi and series)."""
    assert len(_stores()["store_log"]) > 0
    msg = _not_recorded(_stores("kpi"), "store_log")
    assert "store_log" in msg and "level kpi" in msg and "log_level='full'" in msg
    msg = _not_recorded(_stores("series"), "store_log")
    assert "level series" in msg and "log_level='full'" in msg


def test_kpi_dashboard_absent_series_keeps_it():
    """Plan "Level definitions": kpi does not record timeseries or dashboard; series adds them.
    With the same seed they equal the full run's, the sampler being the same (model's value)."""
    full, series, kpi = _dash(), _dash("series"), _dash("kpi")
    assert len(full["dashboard"]) > 0
    msg = _not_recorded(kpi, "dashboard")
    assert "level kpi" in msg and "log_level='series'" in msg
    for frame in SERIES_FRAMES:
        pd.testing.assert_frame_equal(series[frame], full[frame], check_exact=True)
    for frame in LOG_FRAMES:
        if frame in full:
            msg = _not_recorded(series, frame)
            assert "level series" in msg and "log_level='full'" in msg


def test_not_recorded_error_is_a_key_error():
    """Plan Task 2: NotRecordedError(KeyError), so `except KeyError` callers keep working."""
    with pytest.raises(KeyError):
        _single("kpi")["entity_log"]


def test_keep_empty_never_returns_a_not_recorded_frame():
    """Plan Task 2: keep_empty=True still never returns a not-recorded frame; empty summary
    frames (single_server has no store) are still returned, being kpi level."""
    results = _single("kpi", keep_empty=True)
    for frame in LOG_FRAMES + SERIES_FRAMES:
        assert frame not in results, frame
    assert "store_summary" in results


def test_kpi_keeps_every_other_frame_identical():
    """Plan rule (A3): every frame and column outside FRAME_LEVEL / COLUMN_LEVEL is bit-identical
    at kpi; the declared ones are absent."""
    full, kpi = _single(), _single("kpi")
    assert set(kpi) == set(full) - set(LOG_FRAMES) - set(SERIES_FRAMES)
    for name in kpi:
        drop = [c for c in LOG_DERIVED_COLUMNS.get(name, ()) if c in full[name]]
        pd.testing.assert_frame_equal(kpi[name], full[name].drop(columns=drop), check_exact=True)


@pytest.mark.parametrize("level", ["kpi", "series"])
def test_log_derived_columns_absent_below_full(level):
    """Plan rule: with a named level below full the ten LOG_DERIVED_COLUMNS are dropped from the
    frame and listed as not recorded; Results.kpi(key) gives the same line."""
    full, low = _single(), _single(level)
    cols = LOG_DERIVED_COLUMNS["resource_summary"]
    assert cols <= set(full["resource_summary"].columns)
    assert np.isfinite(full.kpi(LOG_KPI))
    assert not cols & set(low["resource_summary"].columns)
    with pytest.raises(KeyError) as caught:
        low.kpi(LOG_KPI)
    msg = caught.value.args[0]
    assert "not recorded" in msg and f"level {level}" in msg and "log_level='full'" in msg

    sfull, slow = _stores(), _stores(level)
    scols = LOG_DERIVED_COLUMNS["store_summary"]
    assert scols <= set(sfull["store_summary"].columns)
    assert not scols & set(slow["store_summary"].columns)
    store = sfull["store_summary"]["Store"].iloc[0]
    assert np.isfinite(sfull.kpi(f"store/{store}/P95 Size"))
    with pytest.raises(KeyError):
        slow.kpi(f"store/{store}/P95 Size")


def test_legacy_logs_off_keeps_todays_frames():
    """Plan rule / O4: with no level named, a legacy record_logs=False run keeps today's frames
    exactly: live rows in entity_log, NaN log-derived columns (the label is checked in
    test_run_model_records_level_on_results). Its resource_log is a plain empty-frame KeyError
    (D1321), not NotRecordedError. Passes today, guarding "no breaking change"."""
    legacy = _single(record_logs=False)
    log = legacy["entity_log"]
    assert len(log) > 0 and log["Exited At"].isna().all()
    cols = sorted(LOG_DERIVED_COLUMNS["resource_summary"])
    assert legacy["resource_summary"][cols].isna().all().all()
    with pytest.raises(KeyError) as caught:
        legacy["resource_log"]
    assert type(caught.value).__name__ != "NotRecordedError"


# ---------- Task 2: live count keeps laws working ---------------------------------------------
@functools.lru_cache(maxsize=None)
def _laws_runs():
    layout = _layout(warmup=0)
    kw = dict(seed=0, replications=2, run_length=300)
    return layout, api.run_model(layout, **kw), api.run_model(layout, log_level="kpi", **kw)


def _counts(value) -> list:
    if isinstance(value, dict):
        return [float(value[k]) for k in sorted(value)]
    return [float(v) for v in np.asarray(value).ravel()]


def test_live_at_end_matches_the_full_entity_log():
    """Plan Task 2: LIVE_KEY is the per-replication count of entities created in the window and
    still inside, not batched, as Results.live_at_end; equals the full log's Exited At and In
    Batch both NaN, per Run."""
    _layout_, full, kpi = _laws_runs()
    log = full["entity_log"]
    live = log[log["Exited At"].isna() & log["In Batch"].isna()].groupby("Run").size()
    expected = [float(live.get(r, 0)) for r in sorted(log["Run"].unique())]
    assert sum(expected) > 0                        # non-vacuous: something is still inside
    assert _counts(kpi.live_at_end) == expected


def test_laws_little_and_flow_checks_work_at_kpi():
    """Plan rule: laws._inside reads the live count when the entity log is absent, "so its check
    keeps working with no frame". Flow conservation exists only with an inside count (warm-up 0);
    initial bias is excluded (at kpi it says "not judged: needs level full")."""
    layout, full, kpi = _laws_runs()

    def by_law(results):
        return {c["law"]: (c["verdict"], c["why"]) for c in laws.checks(layout, results)
                if c["law"] != "initial bias"}

    expected = by_law(full)
    assert {"flow conservation", "little (system)", "censoring"} <= set(expected)
    assert by_law(kpi) == expected


# ---------- Task 3: needed_level and study_layout ---------------------------------------------
@pytest.mark.parametrize("kpis, level", [
    ([], "kpi"),
    ([KPI, "system/Total Cost", "resource/Server/Usage % (snapshot)",
      "store/Buffer/Average Size", "entity/DEFAULT/Avg Time"], "kpi"),
    (["entity_log/Throughput Time"], "full"),
    (["resource_log/Queue Length"], "full"),
    (["store_log/Size"], "full"),
    ([LOG_KPI], "full"),
    (["store/Buffer/P99 Size"], "full"),
    (["resource/A/B/Queue Len Std"], "full"),     # an object name may hold a "/"
    ([SERIES_KPI], "series"),
    (["timeseries/Queue Server/min"], "series"),
    ([KPI, SERIES_KPI], "series"),
    ([SERIES_KPI, LOG_KPI], "full"),
])
def test_needed_level(kpis, level):
    """Plan Task 3: *_log/... and LOG_DERIVED_COLUMNS give full; timeseries/<series>/max|min gives
    series; anything else kpi. A mix needs the highest (reading of "unless a KPI needs more")."""
    assert _ll().needed_level(kpis) == level


def test_needed_level_tail_goal_needs_full():
    """Plan Task 3 / O5: a tail goal (share_above) gives full; a plain goal does not. Goal shape
    from agent_tools/study.py: {"id", "text", "kpi", "share_above", "high"}."""
    ll = _ll()
    tail = {"id": "g1", "text": "few long waits", "kpi": "Server/Time in Queue",
            "share_above": 15, "high": 0.05}
    plain = {"id": "g2", "text": "short stay", "kpi": KPI, "high": 30}
    assert ll.needed_level([KPI], goals=[tail]) == "full"
    assert ll.needed_level([KPI], goals=[plain]) == "kpi"


def test_study_layout_resolves_kpi_for_plain_kpis():
    """Plan Task 3: study_layout resolves through needed_level and apply_level, so a plain-KPI
    study runs at kpi with the sampler off too; a log KPI gets full; a series KPI series."""
    layout = _layout()
    before = copy.deepcopy(layout)
    plain = _sim(evaluation.study_layout(layout, [KPI]))
    assert plain["record_logs"] is False and plain["timeseries_points"] == 0
    logs = _sim(evaluation.study_layout(layout, [KPI, LOG_KPI]))
    assert logs["record_logs"] is True and logs.get("timeseries_points", 200) > 0
    series = _sim(evaluation.study_layout(layout, [SERIES_KPI]))
    assert series["record_logs"] is False and series.get("timeseries_points", 200) > 0
    assert layout == before


def test_study_layout_caller_level_wins():
    """Plan Task 3: an explicit log_level or record_logs from the caller still wins; the resolver
    is a caller, so it also beats the model's own simulation.log_level."""
    layout = _layout()
    full = _sim(evaluation.study_layout(layout, [KPI], log_level="full"))
    assert full["record_logs"] is True
    kpi = _sim(evaluation.study_layout(layout, [LOG_KPI], log_level="kpi"))
    assert kpi["record_logs"] is False and kpi["timeseries_points"] == 0
    series = _sim(evaluation.study_layout(layout, [KPI], log_level="series"))
    assert series["record_logs"] is False and series.get("timeseries_points", 200) > 0
    assert _sim(evaluation.study_layout(layout, [KPI], record_logs=True))["record_logs"] is True
    model_full = _layout(log_level="full")
    resolved = _sim(evaluation.study_layout(model_full, [KPI]))
    assert resolved["record_logs"] is False and resolved["timeseries_points"] == 0
