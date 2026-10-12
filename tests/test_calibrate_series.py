"""Calibration to time-series data (PHASE6 S6): `SeriesTarget` and its
Vensim-style weighted payoff in `nagasim.calibrate`."""
import numpy as np
import pandas as pd
import pytest

from nagasim import calibrate as cal
from nagasim.api import run_model
from nagasim.core import agents as A
from nagasim.optimize import Factor
from nagasim.report import _calibration_html

R, K = 0.3, 100.0


def _logistic(r=R, k=K, **extra):
    layout = {"blocks": [], "links": [],
              "levels": [{"name": "P", "initial": 10.0}],
              "auxiliaries": [{"name": "r", "expr": str(r)}, {"name": "K", "expr": str(k)}],
              "flows": [{"name": "g", "rate": 'AUX("r")*LEVEL("P")*(1-LEVEL("P")/AUX("K"))',
                         "to": "P"}],
              "simulation": {"time_limit": 40, "sd": {"dt": 0.25, "method": "rk4"},
                             "timeseries_points": 40}}
    layout.update(extra)
    return layout


def _synthetic(layout, series="level/P"):
    """Noise-free data: the model's own trajectory at integer times."""
    ts = run_model(layout)["timeseries"]
    rows = ts[ts["Series"] == series]
    return pd.DataFrame({"Time": rows["Time"].to_numpy(), "Value": rows["Value"].to_numpy()})


FACTORS = [Factor("auxiliaries.r.expr", "continuous", 0.1, 0.6),
           Factor("auxiliaries.K.expr", "continuous", 50.0, 200.0)]


def test_logistic_recovers_r_and_k():
    data = _synthetic(_logistic())
    result = cal.calibrate(_logistic(0.2, 150.0), [cal.SeriesTarget("level/P", data)],
                           FACTORS, budget=100, replications_per_point=2)
    best = {k.split(".")[1]: float(v) for k, v in result.best.items()}
    assert best["r"] == pytest.approx(R, rel=0.02)
    assert best["K"] == pytest.approx(K, rel=0.02)
    res = result.residuals["level/P"]
    assert list(res.columns) == cal.RESIDUAL_COLUMNS and len(res) == len(data)
    assert result.objective == pytest.approx((res["Weight"] * res["Residual"] ** 2).sum())
    assert "model against data" in _calibration_html(result)


def test_payoff_equals_hand_computation_and_interpolates_between_samples():
    layout = _logistic(0.25, 120.0)
    ts = run_model(layout)["timeseries"]
    p = ts[ts["Series"] == "level/P"].set_index("Time")["Value"]
    # t=3 is a sample; t=3.5 is midway between two samples, so linear interpolation.
    data = [(3.0, 20.0), (3.5, 22.0), (10.0, 60.0)]
    model = [p[3.0], (p[3.0] + p[4.0]) / 2, p[10.0]]
    target = cal.SeriesTarget("level/P", data, weight=2.0).resolve(layout)
    res = cal.series_residuals({"timeseries": ts}, target)
    hand = sum(2.0 * (m - d) ** 2 for m, (_, d) in zip(model, data))
    assert (res["Weight"] * res["Residual"] ** 2).sum() == pytest.approx(hand, rel=1e-12)
    # "1/sd": each error in data standard deviations.
    sd = np.std([d for _, d in data], ddof=1)
    target = cal.SeriesTarget("level/P", data, weight="1/sd").resolve(layout)
    res = cal.series_residuals({"timeseries": ts}, target)
    assert (res["Weight"] * res["Residual"] ** 2).sum() == pytest.approx(
        sum(((m - d) / sd) ** 2 for m, (_, d) in zip(model, data)), rel=1e-12)


def test_data_from_a_model_table_and_a_dict_target():
    truth = _synthetic(_logistic())
    table = {"name": "obs", "keys": truth["Time"].tolist(), "values": truth["Value"].tolist()}
    layout = _logistic(tables=[table])
    [target] = cal._targets([{"series": "level/P", "data": "obs"}], layout)
    res = cal.series_residuals(run_model(layout), target)
    assert np.abs(res["Residual"]).max() < 1e-9


def test_heavily_weighted_series_dominates():
    """Two series disagree on K (the trajectory says 80, the aux says 150);
    the one weighted 1000x wins the fit."""
    truth_a = _synthetic(_logistic(R, 80.0))
    truth_b = [(t, 150.0) for t in range(0, 41, 5)]
    factor = [Factor("auxiliaries.K.expr", "continuous", 50.0, 200.0)]

    def fit(w_a, w_b):
        targets = [cal.SeriesTarget("level/P", truth_a, weight=w_a),
                   cal.SeriesTarget("aux/K", truth_b, weight=w_b)]
        result = cal.calibrate(_logistic(), targets, factor, budget=30,
                               replications_per_point=2)
        return float(next(iter(result.best.values())))

    assert fit(1000.0, 1.0) == pytest.approx(80.0, rel=0.03)
    assert fit(1.0, 1000.0) == pytest.approx(150.0, rel=0.03)


def test_agent_state_series_target():
    """`agents/<pop>/<state>` is an ordinary series key: 200 agents leave A at
    rate k, so E[count in A] = 200 exp(-k t); the fit finds k near 0.2."""
    kind = A.AgentType("T", parameters={"k": 0.4}, states=["A", "B"],
                       transitions=[A.Transition("A", "B", "rate", "k")])
    layout = A.add({"blocks": [], "links": [],
                    "simulation": {"time_limit": 10, "timeseries_points": 10}},
                   kind, A.Population("P", "T", 200))
    data = [(t, 200.0 * np.exp(-0.2 * t)) for t in range(11)]
    result = cal.calibrate(layout, [{"series": "agents/P/A", "data": data}],
                           [Factor("agent_types.T.parameters.k", "continuous", 0.05, 0.5)],
                           budget=40, replications_per_point=3)
    assert float(next(iter(result.best.values()))) == pytest.approx(0.2, rel=0.15)
    assert result.table.loc[0, "KPI"] == "agents/P/A"
    assert len(result.residuals["agents/P/A"]) == len(data)


def test_unknown_series_and_out_of_horizon_data_are_clear_errors():
    layout = _logistic()
    frames = run_model(layout)
    with pytest.raises(ValueError, match="unknown series"):
        cal.series_residuals(frames, cal.SeriesTarget("level/Nope", [(1, 1)]).resolve(layout))
    with pytest.raises(ValueError, match="data spans"):
        cal.series_residuals(frames, cal.SeriesTarget("level/P", [(50, 1)]).resolve(layout))
    with pytest.raises(ValueError, match="merge their data"):
        cal._targets([{"series": "level/P", "data": [(1, 1)]},
                      {"series": "level/P", "data": [(2, 1)]}], layout)
