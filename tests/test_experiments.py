"""Scenario manager, parallel replications, precision and warm-up (ROADMAP 1.6).

Pool-based tests are kept to two on purpose: a ProcessPoolExecutor start-up on
Windows costs more than the replications it runs for a model this small.
"""
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sp_stats

from nagasim import api, experiments
from nagasim.core.schema import ModelValidationError
from nagasim.core.stats import paired_diff_ci, welch_diff_ci
from nagasim.core.stats import Tally, TimeWeighted, ci95

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
SINGLE_SERVER = MODELS_DIR / "single_server.json"
CALL_CENTRE = MODELS_DIR / "call_centre_day.json"


@pytest.fixture
def mm1():
    return experiments._as_layout(SINGLE_SERVER)


def deterministic_layout(max_arrivals=20):
    """Constant everything: every replication returns the same KPIs, so the
    across-replication standard deviation is exactly 0."""
    return {
        "schema_version": 9,
        "blocks": [
            {"name": "create_1", "type": "CREATE", "position": [0, 0],
             "params": {"e_type": "DEFAULT", "distribution": "Constant",
                        "dist_params": {"value": 1.0}, "entities_per_arrival": 1,
                        "max_arrivals": max_arrivals, "start_time": 0}},
            {"name": "delay_1", "type": "DELAY", "position": [1, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": 2.0}}},
            {"name": "dispose_1", "type": "DISPOSE", "position": [2, 0],
             "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
        ],
        "links": [{"source": "create_1", "target": "delay_1"},
                  {"source": "delay_1", "target": "dispose_1"}],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [],
        "simulation": {"time_limit": 100, "warmup": 0, "replications": 1, "time_unit": "minutes"},
    }


# -- override path grammar ---------------------------------------------------

def test_every_addressable_section_can_be_overridden():
    """One override per section of the grammar, applied in one call."""
    layout = experiments._as_layout(CALL_CENTRE)
    layout["resource_sets"] = [{"name": "Pool", "members": ["Agents"], "rule": "PreferredOrder"}]
    layout["variables"] = [{"name": "target_sl", "initial": 20}]
    layout["failures"] = [{"name": "outage", "resources": ["Agents"],
                           "uptime": {"distribution": "Exponential", "dist_params": {"rate": 0.01}},
                           "downtime": {"distribution": "Constant", "dist_params": {"value": 5}}}]
    layout["links"][0]["delay"] = {"distribution": "Constant", "dist_params": {"value": 0.5}}

    new = experiments.apply_overrides(layout, [
        {"path": "blocks.process_1.params.dist_params.rate", "value": 0.4},
        {"path": "resources.Agents.capacity", "value": 9},
        {"path": "resource_sets.Pool.rule", "value": "RandomSelection"},
        {"path": "schedules.call_rate.weekly", "value": {"mon": [["08:00", "16:00", 10]]}},
        {"path": "schedules.call_rate.off_value", "value": 1},
        {"path": "failures.outage.uptime.dist_params.rate", "value": 0.05},
        {"path": "variables.target_sl.initial", "value": 30},
        {"path": "links.create_1->process_1.delay.dist_params.value", "value": 1.5},
        {"path": "simulation.time_limit", "value": 999},
    ])

    assert new["blocks"][1]["params"]["dist_params"]["rate"] == 0.4
    assert new["resources"][0]["capacity"] == 9
    assert new["resource_sets"][0]["rule"] == "RandomSelection"
    schedules = {s["name"]: s for s in new["schedules"]}
    assert schedules["call_rate"]["weekly"] == {"mon": [["08:00", "16:00", 10]]}
    assert schedules["call_rate"]["off_value"] == 1
    assert new["failures"][0]["uptime"]["dist_params"]["rate"] == 0.05
    assert new["variables"][0]["initial"] == 30
    assert new["links"][0]["delay"]["dist_params"]["value"] == 1.5
    assert new["simulation"]["time_limit"] == 999


def test_apply_overrides_does_not_touch_the_base_layout(mm1):
    before = copy.deepcopy(mm1)
    experiments.apply_overrides(mm1, [{"path": "resources.Server.capacity", "value": 4}])
    assert mm1 == before


BAD_OVERRIDE_PATHS = [
    "blocks.nope.params.dist_params.rate",       # unknown block
    "blocks.process_1.params.nope",              # unknown param
    "blocks.process_1.position",                 # not params
    "resources.Server.nope",                     # unknown field
    "elephants.Server.capacity",                 # unknown section
    "simulation.nope",                           # unknown simulation field
    "links.create_1->dispose_1.delay.dist_params.value",   # no such link
    "",                                          # empty
]


def test_unknown_override_path_is_refused_and_names_the_path(mm1):
    for path in BAD_OVERRIDE_PATHS:
        with pytest.raises(ModelValidationError) as excinfo:
            experiments.apply_overrides(mm1, [{"path": path, "value": 1}])
        assert path in str(excinfo.value) or "empty path" in str(excinfo.value), path


def test_a_scenario_whose_result_is_invalid_names_the_scenario(mm1):
    mm1["scenarios"] = [{"name": "Impossible",
                         "overrides": [{"path": "blocks.process_1.params.distribution",
                                        "value": "NotADistribution"}]}]
    with pytest.raises(ModelValidationError) as excinfo:
        experiments.run_scenarios(mm1, replications=1, run_length=10)
    assert "Impossible" in str(excinfo.value)
    assert "NotADistribution" in str(excinfo.value)


def test_list_override_paths_finds_the_mm1_service_rate(mm1):
    paths = dict(experiments.list_override_paths(mm1))
    assert paths["blocks.process_1.params.dist_params.rate"] == 1.25
    assert paths["resources.Server.capacity"] == 1
    assert paths["simulation.warmup"] == 100
    assert not any(p.endswith(".name") for p in paths)   # a rename is not an override


def test_every_listed_path_actually_applies(mm1):
    """The picker never offers a path apply_overrides would reject."""
    for path, value in experiments.list_override_paths(mm1):
        experiments.apply_overrides(mm1, [{"path": path, "value": value}])


def _conversion_layout(yield_per_unit=100.0):
    """One cohort fed 1 unit at t<1, no entity work: conversions/flows run on
    the level system's own process (same shape as test_conversions.py)."""
    return {
        "schema_version": 9, "blocks": [], "links": [],
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [],
        "conversions": [{"name": "D", "cohort_interval": 1, "unit": "Nm3", "substrates": [
            {"name": "s", "feed": "1 if TNOW < 1 else 0", "yield": yield_per_unit,
             "curve": [[4, 0.4], [10, 1.0]]}]}],
        "levels": [{"name": "Gas", "capacity": 1000, "initial": 0}],
        "flows": [{"name": "gas", "to": "Gas", "rate": "CONV(\"D\")"}],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "hours"},
    }


def _final_gas(layout, run_len=20):
    from nagasim.core import initial_state
    from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

    env = initialize_sim_env(seed=0)
    model = prepare_model(env, layout)
    initial_state.apply(env, model, {})
    env.ctx.level_system.start(run_len)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, run_len)
    env.ctx.level_system.sync(run_len)
    return env.ctx.level_system.levels["Gas"].level


def test_a_scenario_can_override_a_conversions_yield_and_the_whole_curve():
    """D145: conversions are overridable (yields/curves change the dynamics),
    unlike entity types (see test_entities_stay_refused_as_an_override_target).
    Covers a scalar field (cohort_interval) and the whole yield curve, which
    lives inside the substrates LIST and so is one override-able blob."""
    base = _conversion_layout(yield_per_unit=100.0)

    scaled = experiments.apply_overrides(base, [
        {"path": "conversions.D.cohort_interval", "value": 2}])
    assert scaled["conversions"][0]["cohort_interval"] == 2

    halved = experiments.apply_overrides(base, [
        {"path": "conversions.D.substrates", "value": [
            {"name": "s", "feed": "1 if TNOW < 1 else 0", "yield": 50.0,
             "curve": [[4, 0.4], [10, 1.0]]}]}])

    paths = dict(experiments.list_override_paths(base))
    assert paths["conversions.D.cohort_interval"] == 1
    assert paths["conversions.D.substrates"] == base["conversions"][0]["substrates"]

    # Hand-derived: the cohort's whole yield is released by t=20 either way,
    # so the final level is exactly the yield -- halving it halves the level.
    assert _final_gas(base) == pytest.approx(100.0)
    assert _final_gas(halved) == pytest.approx(50.0)


def test_entities_stay_refused_as_an_override_target(mm1):
    """D145: entity types stay NOT overridable -- their only numeric field,
    holding_cost, feeds cost KPIs only and never the dynamics."""
    mm1["entities"] = [{"name": "Widget", "holding_cost": 1.0}]
    with pytest.raises(ModelValidationError) as excinfo:
        experiments.apply_overrides(mm1, [{"path": "entities.Widget.holding_cost", "value": 2.0}])
    assert "entities.Widget.holding_cost" in str(excinfo.value)


def test_baseline_is_implicit_and_first(mm1):
    assert [s["name"] for s in experiments.scenarios_of(mm1)] == ["Baseline"]
    mm1["scenarios"] = [{"name": "Other", "overrides": []}]
    assert [s["name"] for s in experiments.scenarios_of(mm1)] == ["Baseline", "Other"]


# -- flatten_kpis ------------------------------------------------------------

def test_flatten_kpis_keys_cover_every_frame():
    results = api.run_model(str(CALL_CENTRE), replications=2, run_length=600, warmup=0)
    flat = experiments.flatten_kpis(results)

    assert list(flat.columns) == ["Run", "KPI", "Value"]
    keys = set(flat["KPI"])
    assert "system/Avg Throughput Time" in keys
    assert "resource/Agents/Usage % (snapshot)" in keys
    assert "entity/DEFAULT/Avg Time" in keys
    assert "user/time_in_system" in keys           # the model's RECORD statistic
    assert "system/Run" not in keys                # the Run marker row is not a KPI
    assert "system/Time Unit" not in keys          # non-numeric
    assert flat["Value"].map(lambda v: isinstance(v, float)).all()
    assert set(flat["Run"]) == {0, 1}


# -- common random numbers ---------------------------------------------------

def _faster_service(layout, rate):
    layout = copy.deepcopy(layout)
    layout["scenarios"] = [{"name": "Faster", "overrides": [
        {"path": "blocks.process_1.params.dist_params.rate", "value": rate}]}]
    return layout


def test_crn_keeps_arrivals_identical_across_scenarios(mm1):
    """Only the service rate differs, and streams are per block name, so the
    CREATE block draws exactly the same inter-arrival times in both."""
    result = experiments.run_scenarios(_faster_service(mm1, 2.0), replications=4,
                                       run_length=300, warmup=0, seed=0, crn=True)

    base = result.results["Baseline"]["entity_log"].sort_values(["Run", "Created At"])
    other = result.results["Faster"]["entity_log"].sort_values(["Run", "Created At"])
    for run in sorted(set(base["Run"])):
        a = base[base["Run"] == run]["Created At"].to_numpy(dtype=float)
        b = other[other["Run"] == run]["Created At"].to_numpy(dtype=float)
        n = min(a.size, b.size)
        assert n > 0
        assert np.allclose(a[:n], b[:n])


def test_crn_disabled_gives_each_scenario_its_own_seed_block(mm1):
    result = experiments.run_scenarios(_faster_service(mm1, 2.0), replications=3,
                                       run_length=100, warmup=0, seed=0, crn=False)
    assert result.seeds == {"Baseline": [0, 1, 2], "Faster": [3, 4, 5]}


def test_paired_interval_is_narrower_than_welch_on_the_same_crn_data(mm1):
    """The point of CRN: the shared arrival noise cancels in the differences."""
    result = experiments.run_scenarios(_faster_service(mm1, 1.30), replications=8,
                                       run_length=600, warmup=100, seed=0, crn=True)
    kpi = "system/Avg Throughput Time"
    base = experiments._series(experiments.flatten_kpis(result.results["Baseline"]), kpi)
    other = experiments._series(experiments.flatten_kpis(result.results["Faster"]), kpi)

    paired = paired_diff_ci(other, base)
    welch = welch_diff_ci(other, base)
    assert paired["ub"] - paired["lb"] < welch["ub"] - welch["lb"]


# -- comparison table --------------------------------------------------------

def test_comparison_columns_and_significance(mm1):
    mm1["scenarios"] = [
        {"name": "Twin", "overrides": []},                       # identical to the baseline
        {"name": "Much faster", "overrides": [
            {"path": "blocks.process_1.params.dist_params.rate", "value": 4.0}]},
    ]
    result = experiments.run_scenarios(mm1, replications=6, run_length=500, warmup=100, seed=0)
    table = result.comparison(kpis=["system/Avg Throughput Time"])

    assert list(table.columns) == experiments.COMPARISON_COLS
    rows = table.set_index("Scenario")
    assert set(rows.index) == {"Baseline", "Twin", "Much faster"}

    # an identical copy of the baseline: difference exactly 0, never "significant"
    assert rows.loc["Twin", "Diff vs baseline"] == 0.0
    assert bool(rows.loc["Twin", "Significant"]) is False
    assert bool(rows.loc["Baseline", "Significant"]) is False
    # an obviously faster server: the difference interval excludes 0
    assert rows.loc["Much faster", "Diff vs baseline"] < 0
    assert bool(rows.loc["Much faster", "Significant"]) is True
    assert rows.loc["Much faster", "Diff UB"] < 0


def test_comparison_of_a_deterministic_model_is_exact_not_nan():
    """DEVLOG #96. Zero variance is the normal case for a continuous or a
    fixed-profile model, and it must read as an EXACT difference with a
    zero-width interval -- not as NaN, and not as "not significant"."""
    layout = deterministic_layout()
    layout["scenarios"] = [{"name": "Slower", "overrides": [
        {"path": "blocks.delay_1.params.dist_params.value", "value": 4.0}]}]
    result = experiments.run_scenarios(layout, replications=3, seed=0)
    rows = result.comparison(kpis=["system/Avg Throughput Time"]).set_index("Scenario")

    assert rows.loc["Baseline", "Mean"] == 2.0 and rows.loc["Slower", "Mean"] == 4.0
    assert rows.loc["Slower", "LB"] == rows.loc["Slower", "UB"] == 4.0      # zero width
    assert rows.loc["Slower", "Diff vs baseline"] == 2.0                    # exactly
    assert rows.loc["Slower", "Diff LB"] == rows.loc["Slower", "Diff UB"] == 2.0
    assert bool(rows.loc["Slower", "Significant"]) is True
    assert bool(rows.loc["Baseline", "Significant"]) is False   # a 0 difference never is


def test_comparison_rejects_an_unknown_baseline(mm1):
    result = experiments.run_scenarios(mm1, replications=1, run_length=50, warmup=0)
    with pytest.raises(ValueError):
        result.comparison(baseline="Nope")


# -- parallel replications (2 pool tests, no more) ---------------------------

@pytest.mark.slow
def test_parallel_equals_sequential_and_reports_every_replication(worker_pool):
    sequential = api.run_model(str(SINGLE_SERVER), replications=4, run_length=200, warmup=0)

    calls = []
    parallel = api.run_model(str(SINGLE_SERVER), replications=4, run_length=200, warmup=0,
                             workers=2, pool=worker_pool,
                             on_progress=lambda done, total: calls.append((done, total)))

    for key in sequential:
        pd.testing.assert_frame_equal(sequential[key], parallel[key])
    assert calls == [(1, 4), (2, 4), (3, 4), (4, 4)]   # `total` progress calls, in order


@pytest.mark.slow
def test_cancelling_a_pool_run_returns_the_replications_that_finished(worker_pool):
    """`should_stop` is polled once per WAVE (after each `wait`), and a wave can
    hand back several finished replications at once, so "stop on the second
    poll" let all six through on a warm pool (the race the DEVLOG had flagged).
    Stopping on the FIRST poll with far more replications than two workers can
    finish before the first `wait` wakes up is deterministic: at least one is
    back, the rest are dropped."""
    seen = []

    def should_stop():
        seen.append(1)
        return True

    results = api.run_model(str(SINGLE_SERVER), replications=40, run_length=400, warmup=0,
                            workers=2, pool=worker_pool, should_stop=should_stop)
    assert results is not None
    runs = set(results["system"].loc[results["system"]["Metric"] == "Run", "Value"])
    assert 0 < len(runs) < 40 and len(seen) == 1


@pytest.mark.slow
def test_a_scenario_set_is_the_same_run_over_design_points(worker_pool):
    """PHASE4 T3: with ONE replication per scenario there is nothing to spread
    within a scenario -- the tasks are (scenario, replication) pairs, and the
    numbers must not move."""
    layout = experiments._as_layout(SINGLE_SERVER)
    layout["scenarios"] = [{"name": f"cap {c}", "overrides": [
        {"path": "resources.Server.capacity", "value": c}]} for c in (2, 3, 4)]

    serial = experiments.run_scenarios(layout, replications=1, run_length=200, warmup=0)
    steps = []
    parallel = experiments.run_scenarios(layout, replications=1, run_length=200, warmup=0,
                                         workers=4, pool=worker_pool,
                                         on_progress=lambda done, total: steps.append((done, total)))

    assert parallel.scenarios == serial.scenarios
    for name in serial.scenarios:
        for key, frame in serial.results[name].items():
            pd.testing.assert_frame_equal(parallel.results[name][key], frame)
    assert steps == [(1, 4), (2, 4), (3, 4), (4, 4)]       # progress counts TASKS


def test_workers_one_never_spawns_a_pool(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a pool was spawned for workers <= 1")

    monkeypatch.setattr(api, "ProcessPoolExecutor", boom)
    assert api.run_model(str(SINGLE_SERVER), replications=2, run_length=50, warmup=0) is not None


# -- run_until_precision -----------------------------------------------------

def test_precision_stops_at_n0_on_a_deterministic_model():
    results, report = experiments.run_until_precision(
        deterministic_layout(), "system/Avg Throughput Time", relative=0.05, n0=4,
        max_replications=50)

    assert report["replications"] == 4        # zero variance: the target is met immediately
    assert report["half_width"] == 0.0
    assert report["met"] is True
    assert len(set(results["system"].loc[results["system"]["Metric"] == "Run", "Value"])) == 4


def test_precision_honours_the_cap_on_a_noisy_model():
    _results, report = experiments.run_until_precision(
        str(SINGLE_SERVER), "system/Avg Throughput Time", relative=0.001, n0=4,
        max_replications=10, run_length=300, warmup=50)

    assert report["replications"] == 10       # doubling 4 -> 8 -> capped at 10
    assert report["met"] is False
    assert report["half_width"] > report["target"]


def test_precision_rejects_an_unknown_kpi():
    with pytest.raises(ValueError):
        experiments.run_until_precision(deterministic_layout(), "system/Nope", n0=2,
                                        max_replications=2)


# -- MSER-5 warm-up ----------------------------------------------------------

def _log(values):
    """A single-replication entity log with one completion per time unit."""
    n = len(values)
    return pd.DataFrame({"Run": 0, "Exited At": np.arange(n, dtype=float),
                         "Throughput Time": values})


def test_mser5_finds_a_known_initial_transient():
    rng = np.random.default_rng(0)
    transient, tail = 100, 300
    values = np.concatenate([np.linspace(10, 0.9, transient), np.zeros(tail)])
    values = values + rng.normal(0, 0.05, transient + tail)

    suggestion = experiments.suggest_warmup(_log(values), batch=5)

    assert abs(suggestion.warmup - transient) <= 5      # within one batch of the true end
    assert suggestion.reliable is True


def test_mser5_flags_a_drifting_series_as_unreliable():
    rng = np.random.default_rng(1)
    values = np.arange(400) * 0.1 + rng.normal(0, 0.05, 400)

    suggestion = experiments.suggest_warmup(_log(values), batch=5)

    assert suggestion.reliable is False                  # minimum in the second half
    assert suggestion.curve.size == 80


def test_suggest_warmup_reads_a_results_dict():
    results = api.run_model(str(SINGLE_SERVER), replications=2, run_length=400, warmup=0)
    suggestion = experiments.suggest_warmup(results)
    assert suggestion.warmup >= 0.0
    assert isinstance(suggestion.reliable, bool)


def test_suggest_warmup_on_an_empty_log_is_zero():
    suggestion = experiments.suggest_warmup(_log(np.zeros(0)))
    assert suggestion.warmup == 0.0
    assert suggestion.reliable is False


# -- interval helpers vs hand-computed values --------------------------------

def test_paired_diff_ci_matches_the_hand_computed_interval():
    a, b = [5.0, 7.0, 9.0, 11.0], [4.0, 5.0, 9.0, 10.0]
    # d = [1, 2, 0, 1]; mean 1, sd = sqrt(2/3), sem = sd/2 = 0.4082483,
    # t(0.975, 3) = 3.1824463 -> half-width 1.2992283
    out = paired_diff_ci(a, b)

    assert out["mean"] == pytest.approx(1.0)
    assert out["sem"] == pytest.approx(0.408248290463863)
    assert out["lb"] == pytest.approx(-0.2992282636253374)
    assert out["ub"] == pytest.approx(2.2992282636253374)
    assert out["df"] == 3
    # independent oracle
    low, high = sp_stats.ttest_rel(a, b).confidence_interval(0.95)
    assert (out["lb"], out["ub"]) == pytest.approx((low, high))


def test_welch_diff_ci_matches_the_hand_computed_interval():
    a, b = [10.0, 12.0, 14.0], [5.0, 6.0, 10.0]
    # means 12 and 7; vars 4 and 7; sem = sqrt(4/3 + 7/3) = 1.9148542,
    # Welch df = (11/3)^2 / ((4/3)^2/2 + (7/3)^2/2) = 242/65 = 3.7230769
    out = welch_diff_ci(a, b)

    assert out["mean"] == pytest.approx(5.0)
    assert out["sem"] == pytest.approx(1.9148542155126762)
    assert out["df"] == pytest.approx(3.723076923076923)
    assert out["lb"] == pytest.approx(-0.4761327300065066)
    assert out["ub"] == pytest.approx(10.476132730006507)
    low, high = sp_stats.ttest_ind(a, b, equal_var=False).confidence_interval(0.95)
    assert (out["lb"], out["ub"]) == pytest.approx((low, high))


def test_diff_intervals_degrade_to_nan_below_two_observations():
    for fn in (paired_diff_ci, welch_diff_ci):
        out = fn([1.0], [2.0])
        assert out["mean"] == pytest.approx(-1.0)
        assert np.isnan(out["lb"]) and np.isnan(out["ub"])
        assert np.isnan(fn([], [])["mean"])


def test_zero_variance_samples_collapse_to_the_point_difference():
    paired = paired_diff_ci([3.0, 3.0, 3.0], [1.0, 1.0, 1.0])
    welch = welch_diff_ci([3.0, 3.0, 3.0], [1.0, 1.0, 1.0])
    for out in (paired, welch):
        assert out["lb"] == pytest.approx(2.0)
        assert out["ub"] == pytest.approx(2.0)


# -- schema / model file -----------------------------------------------------

def test_scenarios_survive_a_save_and_load_round_trip(tmp_path, mm1):
    mm1["scenarios"] = [{"name": "A", "description": "d",
                         "overrides": [{"path": "resources.Server.capacity", "value": 2}]}]
    path = tmp_path / "m.json"
    path.write_text(json.dumps(mm1))
    assert experiments._as_layout(path)["scenarios"] == mm1["scenarios"]


def test_the_call_centre_example_ships_two_runnable_scenarios():
    layout = experiments._as_layout(CALL_CENTRE)
    names = [s["name"] for s in layout["scenarios"]]
    assert names == ["One more agent on the peak shift", "Faster handling"]


# ---------------------------------------------------------------- CLI (ROADMAP 1.7 C)
def test_cli_writes_comparison_and_report(tmp_path):
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(deterministic_layout()))
    out_xlsx = tmp_path / "comparison.xlsx"
    out_html = tmp_path / "report.html"

    rc = experiments.main([str(model_path), "--replications", "2", "--run-length", "50",
                           "--out", str(out_xlsx), "--report", str(out_html)])

    assert rc == 0
    assert out_xlsx.exists()
    assert out_html.exists()
    assert "<html" in out_html.read_text(encoding="utf-8").lower()


def test_cli_scenario_filter_and_no_crn(tmp_path):
    rc = experiments.main([str(CALL_CENTRE), "--scenario", "Baseline",
                           "--scenario", "Faster handling",
                           "--replications", "1", "--run-length", "200", "--no-crn"])
    assert rc == 0


# ---------------------------------------------------------------- workers="auto" (ROADMAP 1.7 E)
@pytest.mark.slow
def test_run_scenarios_workers_auto_decides_once(monkeypatch, worker_pool):
    """The probe (first replication of the first scenario) runs once; the study's
    single `api.run_jobs` call then gets a plain int, never "auto". Over the
    session pool (conftest): the spawn, not the 8 tiny tasks, was this test's cost."""
    seen_workers = []
    real_run_jobs = api.run_jobs

    def spy(jobs, **kwargs):
        seen_workers.append(kwargs.get("workers"))
        return real_run_jobs(jobs, **kwargs)

    monkeypatch.setattr(api, "run_jobs", spy)
    times = iter([0.0, 2.0])
    monkeypatch.setattr(api.time, "perf_counter", lambda: next(times))

    layout = deterministic_layout()
    layout["scenarios"] = [{"name": "A", "overrides": [
        {"path": "blocks.delay_1.params.dist_params.value", "value": 3.0}]}]

    result = experiments.run_scenarios(layout, replications=4, run_length=100, warmup=0,
                                       workers="auto", pool=worker_pool)

    assert result.completed
    assert seen_workers == [api.auto_workers(2.0, 7)]   # decided once, for all 8 tasks
    assert result.scenarios == ["Baseline", "A"]
    assert not result.comparison().empty


# ======================= core/stats.py ====================================
def test_time_weighted_integral_is_hand_computable():
    """level 0 on [0,2), 3 on [2,5), 1 on [5,10] -> area 0*2 + 3*3 + 1*5 = 14."""
    tw = TimeWeighted(0.0)
    tw.set(3, 2.0)
    tw.set(1, 5.0)

    assert tw.area(10.0) == pytest.approx(14.0)
    assert tw.time_avg(10.0) == pytest.approx(1.4)
    assert tw.max == 3


def test_time_weighted_add_tracks_level_and_max():
    tw = TimeWeighted(0.0)
    tw.add(1, 1.0)
    tw.add(1, 2.0)
    tw.add(-2, 4.0)

    # 0 on [0,1), 1 on [1,2), 2 on [2,4), 0 on [4,6]
    assert tw.area(6.0) == pytest.approx(1 + 4)
    assert tw.max == 2
    assert tw.level == 0


def test_time_weighted_empty_window_is_zero():
    tw = TimeWeighted(5.0, level=3)
    assert tw.time_avg(5.0) == 0.0


def test_time_weighted_reset_keeps_the_level_and_restarts_the_area():
    """Warm-up semantics: the accumulators restart, the state does not."""
    tw = TimeWeighted(0.0)
    tw.set(4, 1.0)          # 4 units from t=1 onwards
    tw.reset(10.0)

    assert tw.level == 4
    assert tw.integral == 0.0
    assert tw.max == 4      # max restarts at the current level, not at 0
    assert tw.area(20.0) == pytest.approx(40.0)
    assert tw.time_avg(20.0) == pytest.approx(4.0)


def test_tally_statistics_use_ddof_1():
    t = Tally()
    for x in (2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0):
        t.add(x)

    assert t.count == 8
    assert t.mean == pytest.approx(5.0)
    assert t.min == 2.0
    assert t.max == 9.0
    assert t.std == pytest.approx(np.std([2, 4, 4, 4, 5, 5, 7, 9], ddof=1))
    assert t.percentile(50) == pytest.approx(4.5)


def test_tally_edge_cases():
    t = Tally()
    assert t.count == 0
    assert np.isnan(t.mean)
    assert np.isnan(t.std)

    t.add(3.0)
    assert np.isnan(t.std)      # ddof=1 is undefined for a single observation

    t.reset()
    assert t.count == 0


def test_ci95_matches_scipy_for_n_5():
    """DEVLOG #19: lb/ub are not swapped (lb < mean < ub, matching scipy's t-interval)."""
    values = [10.0, 12.0, 11.0, 9.0, 13.0]
    st = ci95(values)

    arr = np.asarray(values)
    sd = arr.std(ddof=1)
    sem = sd / np.sqrt(5)
    half = sp_stats.t.ppf(0.975, 4) * sem

    assert st["mean"] == pytest.approx(11.0)
    assert st["sd"] == pytest.approx(sd)
    assert st["sem"] == pytest.approx(sem)
    assert st["lb"] == pytest.approx(11.0 - half)
    assert st["ub"] == pytest.approx(11.0 + half)
    assert st["lb"] < st["ub"]


def test_ci95_is_nan_for_a_single_observation():
    st = ci95([7.0])
    assert st["mean"] == 7.0
    for key in ("sd", "sem", "lb", "ub"):
        assert np.isnan(st[key]), f"{key} must be NaN, never a placeholder string"


def test_ci95_ignores_nans_and_handles_an_empty_input():
    assert ci95([1.0, np.nan, 3.0])["mean"] == pytest.approx(2.0)
    assert np.isnan(ci95([])["mean"])


def test_ci99_is_wider_than_ci95():
    values = [10.0, 12.0, 11.0, 9.0, 13.0]
    assert ci95(values, confidence=0.99)["ub"] > ci95(values)["ub"]


def test_183_ci95_nonneg_clamps_the_lower_bound_at_zero():
    """DEVLOG #183: a small mean with enough spread gives the symmetric
    Student-t interval a negative lower bound even for a metric that can't be
    one (a queue length, a count, a time, a utilisation, a cost). `nonneg` is
    opt-in and off by default -- ci95 itself never assumes a metric's sign,
    since plenty of callers (scenario deltas, OAT effects) legitimately pass
    values that can go negative."""
    values = [0.1, 0.0, 0.3, 0.0, 0.05]   # small mean, one point already at 0
    plain = ci95(values)
    arr = np.asarray(values)
    sd = arr.std(ddof=1)
    sem = sd / np.sqrt(5)
    half = sp_stats.t.ppf(0.975, 4) * sem
    assert plain["lb"] == pytest.approx(arr.mean() - half)
    assert plain["lb"] < 0.0                        # the bug: unclamped, it IS negative

    clamped = ci95(values, nonneg=True)
    assert clamped["lb"] == 0.0
    assert clamped["mean"] == plain["mean"] == pytest.approx(clamped["ub"] - half)
    assert clamped["ub"] == plain["ub"]              # only the lower bound moves

    # A metric that legitimately can be negative (e.g. a scenario delta) is
    # never clamped just because nonneg defaults to False.
    assert ci95([-5.0, -3.0, -4.0, -6.0, -2.0])["lb"] < 0.0



# ---------------------------------------------------------------- recordings (PHASE7 7.7, D200)
FORKLIFTS = MODELS_DIR / "warehouse_forklifts.json"


def _recorded_study(*names, **settings):
    result = experiments.run_scenarios(str(FORKLIFTS), *names, **settings)
    window = {key: settings[key] for key in ("run_length", "warmup") if key in settings}
    return result, experiments.record_scenarios(str(FORKLIFTS), result, **window)


def test_a_recording_is_the_first_replication_of_its_scenario():
    """D200: `run_scenarios` records nothing (a recorder cannot come back from its
    pool), so the first replication of each scenario is run again with its seed. It is
    that replication: its trace ends with the totals the study reports for that seed."""
    from nagasim.core.playback import Timeline

    result, recordings = _recorded_study(replications=2, seed=5, run_length=150, warmup=0)
    assert list(recordings) == result.scenarios == [
        "Baseline", "Four forklifts", "Dock across the yard", "FIFO dispatch"]
    for name, recorder in recordings.items():
        assert result.seeds[name] == [5, 6]
        system = result.results[name]["system"]
        first = system[system["Run"] == 5]
        totals = dict(zip(first["Metric"], first["Value"]))
        timeline = Timeline(recorder.events)
        assert timeline.created_until(150) == totals["Total Entities Created"] > 20, name
        assert timeline.disposed_until(150) == totals["Total Entities Completed"] > 10, name
    # Common random numbers: every scenario sees the arrivals of the baseline ...
    arrivals = {name: [event.t for event in recorder.events if event.kind == "entity_created"]
                for name, recorder in recordings.items()}
    assert all(times == arrivals["Baseline"] for times in arrivals.values())
    # ... and is not the baseline.
    assert recordings["Four forklifts"].events != recordings["Baseline"].events


def test_a_recording_follows_the_seeds_and_the_window_of_its_study():
    """Review focus: a study without common random numbers gives each scenario its own
    seeds, and one run on the model's own settings is recorded on those settings."""
    result, recordings = _recorded_study(["Four forklifts"], replications=1, seed=3,
                                         run_length=60, warmup=0, crn=False)
    assert result.seeds == {"Baseline": [3], "Four forklifts": [4]}
    again = experiments.record_scenarios(str(FORKLIFTS), result, run_length=60, warmup=0)
    assert [recorder.events for recorder in again.values()] \
        == [recorder.events for recorder in recordings.values()]
    assert recordings["Baseline"].events != recordings["Four forklifts"].events
    assert max(event.t for event in recordings["Baseline"].events) <= 60

    own = experiments.run_scenarios(str(SINGLE_SERVER), replications=1, seed=0)
    recorder = experiments.record_scenarios(str(SINGLE_SERVER), own)["Baseline"]
    system = own.results["Baseline"]["system"]
    totals = dict(zip(system["Metric"], system["Value"]))
    # The model warms up for 100 of its 1000: its statistics start there, the recording at 0.
    assert totals["Simulation Time"] == 900 and max(event.t for event in recorder.events) > 990
    assert sum(event.kind == "entity_created" for event in recorder.events) \
        > totals["Total Entities Created"]


def test_cli_player_writes_one_file_with_every_scenario(tmp_path, capsys):
    out = tmp_path / "scenarios.html"
    rc = experiments.main([str(FORKLIFTS), "--replications", "2", "--run-length", "100",
                           "--warmup", "0", "--scenario", "Four forklifts", "--player", str(out)])
    assert rc == 0 and "Player written to" in capsys.readouterr().out
    assert [path.name for path in tmp_path.iterdir()] == ["scenarios.html"]
    page = out.read_text(encoding="ascii")
    assert page.startswith("<!doctype html>") and "//" not in page
