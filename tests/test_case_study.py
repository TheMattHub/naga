"""Phase 1 exit criterion (ROADMAP.md): one complete study, end to end, whose
every headline number is verifiable against exact theory.

nagasim/models/job_shop_jackson.json (built by tools/build_job_shop_example.py)
is an OPEN JACKSON NETWORK: exponential service, Poisson external arrivals,
Markovian routing between stations. Product-form theory gives an EXACT
closed-form for every station's utilization and queue length, and for the
system's mean time in system (via Little's law) -- no other queueing network
shape has that. Assertions use 99% Student-t intervals over replications,
same style as tests/test_oracles.py (whose `_erlang_c_lq`/`_assert_theory_in_ci`
helpers are reused here rather than duplicated).

The whole module drives ONE `run_scenarios` call (module-scoped fixture,
baseline + both scenarios under common random numbers): the station-level
oracle checks read its baseline frames, the scenario checks read its
comparison table, and the report check builds off both -- no replication runs
twice. Run length/warm-up (10000/2000 min, in the model file) are longer than
a simple M/M/c oracle needs: this network has a rework loop, and shorter runs
(tried during development, e.g. 3000/300) under-estimate queue lengths by
10-15% -- a real finite-run bias, not a bug (see DEVLOG).
"""
import json
import os
from pathlib import Path

import numpy as np
import pytest

from nagasim.core.document import ModelDocument
from nagasim import api, experiments
from nagasim.report import build_report

from test_oracles import _assert_theory_in_ci, _erlang_c_lq
from conftest import metric_value as _metric, resource_row as _resource_row

MODEL_PATH = Path(__file__).resolve().parents[1] / "nagasim" / "models" / "job_shop_jackson.json"

# Station name -> (resource/set name in the model, server count).
STATIONS = {
    "Cutting": ("Cutting", 1),
    "Drilling": ("Drilling", 2),
    "Assembly": ("AssemblyStation", 2),
}
ROUTING = {  # p_ij: probability of going from station i to station j (missing = exits)
    "Cutting": {"Drilling": 0.6, "Assembly": 0.4},
    "Drilling": {"Assembly": 0.5},
    "Assembly": {"Cutting": 0.2},
}
RATES = {"Cutting": 1.65, "Drilling": 0.5, "Assembly": 0.55}
GAMMA = {"Cutting": 1.0}
CAPACITIES = {s: c for s, (_, c) in STATIONS.items()}


# ---------------------------------------------------------------- exact theory
def _jackson_theory(gamma: dict, routing: dict, rates: dict, capacities: dict) -> dict:
    """Exact open-Jackson-network results.

    Solves the traffic equations lambda_i = gamma_i + sum_j lambda_j p_ji with
    numpy, then gets each station's rho/Lq (Erlang-C, `_erlang_c_lq` handles
    any server count including 1) and L = Lq + lambda/mu. The system's mean
    time in system follows from Little's law applied to the WHOLE network:
    W = sum_i(L_i) / gamma_total, gamma_total being the external arrival rate.
    Jackson's theorem makes this exact even though the network has a rework
    loop (Assembly -> Cutting): the stationary distribution is product-form
    as if each station were an independent M/M/c with Poisson arrivals at its
    traffic-equation rate, regardless of the feedback that produces it.
    """
    stations = list(rates)
    idx = {s: i for i, s in enumerate(stations)}
    n = len(stations)
    p = np.zeros((n, n))
    for src, dsts in routing.items():
        for dst, prob in dsts.items():
            if dst in idx:
                p[idx[src], idx[dst]] = prob
    g = np.array([gamma.get(s, 0.0) for s in stations])
    lam = np.linalg.solve(np.eye(n) - p.T, g)
    lambdas = dict(zip(stations, lam))

    per_station, total_l = {}, 0.0
    for s in stations:
        lam_s, mu_s, c_s = lambdas[s], rates[s], capacities[s]
        lq, rho = _erlang_c_lq(lam_s, mu_s, c_s)
        l = lq + lam_s / mu_s
        per_station[s] = {"lambda": lam_s, "rho": rho, "Lq": lq, "L": l}
        total_l += l

    gamma_total = sum(gamma.values())
    return {
        "stations": per_station,
        "gamma": gamma_total,
        "W": total_l / gamma_total,
        "visits": {s: lambdas[s] / gamma_total for s in stations},
    }


BASELINE_THEORY = _jackson_theory(GAMMA, ROUTING, RATES, CAPACITIES)
SCENARIO_THEORY = {
    "Extra Assembly server": _jackson_theory(GAMMA, ROUTING, RATES, {**CAPACITIES, "Assembly": 3}),
    "Less rework": _jackson_theory(GAMMA, {**ROUTING, "Assembly": {"Cutting": 0.1}}, RATES, CAPACITIES),
}


def test_theory_utilizations_are_in_the_required_band():
    for name, st in BASELINE_THEORY["stations"].items():
        assert 0.6 <= st["rho"] <= 0.85, f"{name}: rho={st['rho']:.3f} outside [0.6, 0.85]"


def test_model_loads_with_zero_problems():
    doc = ModelDocument.load(MODEL_PATH)
    assert doc.problems() == []


# ---------------------------------------------------------------- one shared run
# Replications for the whole study, overriding the model's own 20 (suite
# reduction, PHASE4 D1/D2). This fixture was the single most expensive thing in
# the suite. Its cost is about 10 s of one-off process-pool spawn plus roughly
# 1.3 s per WAVE of `replications x 3 scenarios` tasks over the machine's
# cores, so the only lever that moves it is the wave count: 12 replications is
# 36 tasks -- two waves on a 20-core machine instead of three.
#
# Measured before/after on this machine, 99% intervals: the worst station
# half-width goes from 9.9% to 16.2% of the theoretical value, and the system
# time-in-system from 3.5% to 5.4%; every interval still CONTAINS its
# theoretical value and both scenario differences stay significant
# ([-1.83, -1.32] and [-2.02, -1.44] against theory -1.56 and -1.69). The
# oracle is unchanged; only its resolution is coarser, which is why
# STATION_TOLERANCE below is stated explicitly instead of taking the 15%
# default from test_oracles.
REPLICATIONS = 12
STATION_TOLERANCE = 0.20


@pytest.fixture(scope="module")
def experiment(worker_pool):
    """Baseline + both scenarios, common random numbers, run once for the
    whole module, over the session-wide process pool (conftest) rather than
    one of its own -- the spawn was about 2.5 s of this fixture's cost and
    three tests were paying it separately (P4-D5). workers=cpu_count keeps
    this well under the runtime budget (run_scenarios/api.run_model
    parallelise replications over a process pool, ROADMAP 1.6); it degrades
    gracefully (just slower) on a smaller machine.

    `record_logs` off: every number these tests read comes from a collector,
    and those are bit-identical without the per-event logs (PHASE4 T7,
    test_api); the logs were about 35% of this fixture's wall time. The pool
    spawn itself is only ~2.4 s of it -- the 36 replications are the cost."""
    workers = min(20, os.cpu_count() or 1)
    layout = api.with_run_setting(str(MODEL_PATH), "record_logs", False)
    result = experiments.run_scenarios(layout, crn=True, workers=workers,
                                       replications=REPLICATIONS, pool=worker_pool)
    assert result.completed
    return result


# ---------------------------------------------------------------- station-level oracle
@pytest.mark.slow
def test_stations_match_jackson_theory(experiment):
    baseline = experiment.results[experiments.BASELINE]
    sys_df, user_df, res_df = baseline["system"], baseline["user_summary"], baseline["resource_summary"]
    runs = sorted(sys_df["Run"].unique())

    utils = {s: [] for s in STATIONS}
    queues = {s: [] for s in STATIONS}
    times, record_times, throughputs = [], [], []
    for run in runs:
        kpi = {"system": sys_df[sys_df["Run"] == run], "user_summary": user_df[user_df["Run"] == run],
              "resource_summary": res_df[res_df["Run"] == run]}
        for station, (res_name, _c) in STATIONS.items():
            row = _resource_row(kpi, res_name)
            utils[station].append(row["Usage % (snapshot)"] / 100.0)
            queues[station].append(row["Queue Length Avg"])
        times.append(_metric(kpi, "Avg Throughput Time"))
        throughputs.append(_metric(kpi, "Throughput (per time unit)"))
        user = kpi["user_summary"]
        record_times.append(float(user.loc[user["Statistic"] == "time_in_system", "Value"].iloc[0]))

    for station, st in BASELINE_THEORY["stations"].items():
        _assert_theory_in_ci(utils[station], st["rho"], f"{station} utilization",
                             max_half_width_pct=STATION_TOLERANCE)
        _assert_theory_in_ci(queues[station], st["Lq"], f"{station} Queue Length Avg",
                             max_half_width_pct=STATION_TOLERANCE)

    _assert_theory_in_ci(times, BASELINE_THEORY["W"], "system Avg Throughput Time")
    _assert_theory_in_ci(record_times, BASELINE_THEORY["W"], "RECORD time_in_system tally")
    _assert_theory_in_ci(throughputs, BASELINE_THEORY["gamma"], "system throughput")


# ---------------------------------------------------------------- scenario comparison
@pytest.mark.slow
def test_scenario_diffs_match_theory(experiment):
    comparison = experiment.comparison(kpis=["system/Avg Throughput Time"], confidence=0.99)

    for scenario, theory in SCENARIO_THEORY.items():
        row = comparison[comparison["Scenario"] == scenario].iloc[0]
        theory_diff = theory["W"] - BASELINE_THEORY["W"]
        assert row["Diff LB"] <= theory_diff <= row["Diff UB"], (
            f"{scenario}: theoretical diff {theory_diff:.4f} outside paired 99% interval "
            f"[{row['Diff LB']:.4f}, {row['Diff UB']:.4f}]"
        )
        assert row["Significant"], f"{scenario}: expected a significant difference vs baseline"


# ---------------------------------------------------------------- report
@pytest.mark.slow
def test_report_mentions_all_stations_and_scenarios(experiment):
    layout = json.loads(MODEL_PATH.read_text())
    html = build_report(layout, experiment.results[experiments.BASELINE], experiment=experiment,
                        title="Job shop (Jackson network)")

    assert html.startswith("<!doctype html>")
    assert html.endswith("</html>")
    for station in ("Cutting", "Drilling", "Assembly"):
        assert station in html
    for scenario in ("Extra Assembly server", "Less rework"):
        assert scenario in html
