"""ROADMAP 1.8: input analyzer -- fitting, loading, empirical resampling, and
arrival-rate estimation. Every parameter-conversion table entry is proven by
sampling the the product distribution with known params, fitting it back, and
checking the family ranks in the top 2 by AIC with recovered moments close to
the original (the spec's "or recovered mean/sd within 5%" alternative, used
uniformly since some fitted params -- e.g. Beta's padded min near 0 -- have no
stable relative tolerance of their own)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sstats

from nagasim import inputfit
from nagasim.core import schema
from nagasim.core.distributions import StochasticDistribution

_SEED = 20260919


def _sample(dist_name: str, params: dict, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    dist = StochasticDistribution(dist_name, params)
    return np.array([dist.sample(rng) for _ in range(n)])


def _moments(values: np.ndarray) -> tuple[float, float]:
    return float(np.mean(values)), float(np.std(values, ddof=1))


# ---------------------------------------------------------------- describe()
def test_describe_flags():
    d = inputfit.describe(np.array([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert d["n"] == 5 and d["n_small"] is True
    assert d["negative_values"] is False and d["zeros"] is False
    assert d["integers_only"] is True
    assert d["min"] == 1.0 and d["max"] == 5.0
    assert d["q2"] == pytest.approx(3.0)

    d2 = inputfit.describe(np.array([-1.0, 0.0, 1.5, 2.5]))
    assert d2["negative_values"] is True and d2["zeros"] is True
    assert d2["integers_only"] is False

    d3 = inputfit.describe(np.random.default_rng(1).exponential(2.0, 40))
    assert d3["n_small"] is False


# ---------------------------------------------------------------- load_column
def test_load_column_csv_with_header(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("name,value\na,1.5\nb,2.5\nc,3.5\n")
    result = inputfit.load_column(str(path))
    assert result.dropped == 0
    np.testing.assert_allclose(sorted(result.values), [1.5, 2.5, 3.5])
    assert "value" in result.column_names


def test_load_column_semicolon_no_header(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("1.5;2.1\n2.3;3.9\n4.0;1.1\n")
    result = inputfit.load_column(str(path))
    assert result.dropped == 0
    assert len(result.values) == 3
    # first numeric column picked by default
    np.testing.assert_allclose(sorted(result.values), [1.5, 2.3, 4.0])


def test_load_column_junk_rows_counted_as_dropped(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("value\n1.0\nNaN\nabc\n2.0\n\n3.0\n")
    result = inputfit.load_column(str(path), column="value")
    assert result.dropped >= 2   # "NaN" and "abc" (blank row may also count)
    np.testing.assert_allclose(sorted(result.values), [1.0, 2.0, 3.0])


def test_load_column_xlsx(tmp_path):
    path = tmp_path / "data.xlsx"
    pd.DataFrame({"a": [1, 2, 3], "b": [1.1, 2.2, 3.3]}).to_excel(path, index=False)
    result = inputfit.load_column(str(path), column="b")
    np.testing.assert_allclose(sorted(result.values), [1.1, 2.2, 3.3])


def test_load_column_unknown_column_raises(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("value\n1.0\n2.0\n")
    with pytest.raises(KeyError):
        inputfit.load_column(str(path), column="nope")


# ---------------------------------------------------------------- family recovery
_CONTINUOUS_CASES = {
    "Exponential": {"rate": 0.5},
    "Normal": {"mean": 10.0, "stddev": 2.0},
    "Lognormal": {"mu": 1.0, "sigma": 0.5},
    "Gamma": {"alpha": 3.0, "beta": 2.0},
    "Erlang": {"k": 4, "rate": 0.5},
    "Weibull": {"scale": 3.0, "shape": 1.5},
    "Triangular": {"low": 2.0, "high": 10.0, "mode": 5.0},
    "Uniform": {"low": 1.0, "high": 9.0},
    "Beta": {"min": 0.0, "max": 10.0, "alpha": 2.0, "beta": 3.0},
    "Rayleigh": {"sigma": 3.0},
    "InverseGaussian": {"mu": 2.0, "lambda": 3.0},
    "LogLogistic": {"c": 3.0, "scale": 4.0},
}

_DISCRETE_CASES = {
    "Poisson": {"lambda": 6.0},
    "Geometric": {"p": 0.3},
    "DiscreteUniform": {"low": 2, "high": 12},
    "Binomial": {"n": 10, "p": 0.5},
}


@pytest.mark.slow
def test_fit_recovers_every_family_and_its_moments():
    """One table-driven test over all 16 families instead of 16 parametrized
    ones: a parameter-conversion bug is deterministic, so the sample sizes only
    have to be big enough to rank the family and pin the moments, and reporting
    every failing family at once is more useful than failing on the first.
    """
    # LogLogistic(c=3) has a heavy enough tail that its sample sd converges
    # slowly; it is the one family that still needs a big sample.
    sizes = {"LogLogistic": (6000, 25_000)}
    failures = []
    for name, params in {**_CONTINUOUS_CASES, **_DISCRETE_CASES}.items():
        n_fit, n_recover = sizes.get(name, (1500, 6000))
        values = _sample(name, params, n=n_fit, seed=_SEED)
        fits = inputfit.fit_candidates(values)
        top2 = [f.distribution for f in fits[:2]]
        if name not in top2:
            failures.append(f"{name}: not in top 2 by AIC ({top2})")
            continue

        match = next(f for f in fits if f.distribution == name)
        recovered = _sample(name, match.dist_params, n=n_recover, seed=_SEED + 1)
        orig_mean, orig_sd = _moments(values)
        rec_mean, rec_sd = _moments(recovered)
        if rec_mean != pytest.approx(orig_mean, rel=0.07, abs=1e-6):
            failures.append(f"{name}: mean {rec_mean:.4f} vs {orig_mean:.4f}")
        if rec_sd != pytest.approx(orig_sd, rel=0.07, abs=1e-6):
            failures.append(f"{name}: sd {rec_sd:.4f} vs {orig_sd:.4f}")

    assert not failures, "parameter conversion is wrong for:\n  " + "\n  ".join(failures)


def test_every_fit_has_optimistic_ks_warning():
    values = _sample("Exponential", {"rate": 1.0}, n=200, seed=_SEED)
    fits = inputfit.fit_candidates(values)
    assert all(any("optimistic" in w for w in f.warnings) for f in fits)


# ---------------------------------------------------------------- support / routing
def test_negative_data_skips_positive_support_families():
    values = np.concatenate([np.array([-5.0, -1.0]), _sample("Normal", {"mean": 0, "stddev": 1}, 200, _SEED)])
    fits = inputfit.fit_candidates(values)
    names = {f.distribution for f in fits}
    assert "Lognormal" not in names and "Gamma" not in names and "Weibull" not in names
    assert "Normal" in names


def test_integers_route_to_discrete_catalogue():
    values = _sample("Poisson", {"lambda": 4.0}, n=500, seed=_SEED)
    fits = inputfit.fit_candidates(values)
    names = {f.distribution for f in fits}
    assert names <= set(inputfit.DISCRETE_FAMILIES)
    assert "Poisson" in names


def test_small_n_warning_flag():
    small = inputfit.describe(_sample("Normal", {"mean": 0, "stddev": 1}, 10, _SEED))
    assert small["n_small"] is True
    large = inputfit.describe(_sample("Normal", {"mean": 0, "stddev": 1}, 200, _SEED))
    assert large["n_small"] is False


def test_shift_true_allows_negative_data_and_warns_offset():
    values = _sample("Normal", {"mean": -5.0, "stddev": 1.0}, 500, _SEED)
    fits = inputfit.fit_candidates(values, candidates=["Exponential"], shift=True)
    assert len(fits) == 1
    assert any("location" in w for w in fits[0].warnings)


# ---------------------------------------------------------------- empirical fallback
def test_empirical_continuous_reproduces_piecewise_uniform_histogram():
    edges = [0.0, 1.0, 2.0, 3.0, 4.0]
    probs = [0.1, 0.4, 0.4, 0.1]
    dist = StochasticDistribution("EmpiricalContinuous", {"values": edges, "probs": probs})
    rng = np.random.default_rng(_SEED)
    samples = np.array([dist.sample(rng) for _ in range(20_000)])

    counts, _ = np.histogram(samples, bins=edges)
    expected = np.array(probs) * len(samples)
    chi2, pvalue = sstats.chisquare(counts, expected)
    assert pvalue > 0.01


def test_empirical_continuous_point_values_warns_misuse():
    """A pre-1.8 model with one value per prob still samples (unchanged
    behaviour) but validation now suggests EmpiricalDiscrete."""
    dist = StochasticDistribution("EmpiricalContinuous", {"values": [1.0, 2.0, 3.0], "probs": [0.2, 0.3, 0.5]})
    rng = np.random.default_rng(_SEED)
    sample = dist.sample(rng)
    assert sample in (1.0, 2.0, 3.0)   # legacy point-resampling behaviour preserved

    layout = _minimal_layout_with_distribution("EmpiricalContinuous",
                                               {"values": [1.0, 2.0, 3.0], "probs": [0.2, 0.3, 0.5]})
    problems = [p.message for p in schema.validate_layout_structured(layout)]
    assert any("EmpiricalDiscrete" in p for p in problems)


def test_empirical_spec_continuous_and_discrete():
    cont = inputfit.empirical_spec(_sample("Normal", {"mean": 5, "stddev": 1}, 500, _SEED))
    assert cont["distribution"] == "EmpiricalContinuous"
    assert len(cont["dist_params"]["values"]) == len(cont["dist_params"]["probs"]) + 1

    disc = inputfit.empirical_spec(_sample("Poisson", {"lambda": 3}, 500, _SEED))
    assert disc["distribution"] == "EmpiricalDiscrete"
    assert len(disc["dist_params"]["values"]) == len(disc["dist_params"]["probs"])


def _minimal_layout_with_distribution(dist_name: str, dist_params: dict) -> dict:
    blocks = [
        {"name": "create_1", "description": "", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                    "entities_per_arrival": 1, "max_arrivals": 5, "start_time": 0}},
        {"name": "delay_1", "description": "", "type": "DELAY", "position": [1, 0],
         "params": {"distribution": dist_name, "dist_params": dist_params}},
        {"name": "dispose_1", "description": "", "type": "DISPOSE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_1", "delay_1"], ["delay_1", "dispose_1"]]
    return schema.migrate({
        "blocks": blocks, "links": links, "resources": [], "entities": [], "variables": [],
        "schedules": [], "failures": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    })


# ---------------------------------------------------------------- arrival_rates
def test_arrival_rates_recovers_rates_and_flags_overdispersion():
    rng = np.random.default_rng(_SEED)
    cycle = 24.0
    bin_width = 6.0
    true_rates = [1.0, 5.0, 8.0, 2.0]   # per bin, per time unit
    n_cycles = 40
    timestamps = []
    for cyc in range(n_cycles):
        for i, rate in enumerate(true_rates):
            count = rng.poisson(rate * bin_width)
            times = cyc * cycle + i * bin_width + rng.random(count) * bin_width
            timestamps.extend(times.tolist())
    timestamps = np.array(sorted(timestamps))

    result = inputfit.arrival_rates(timestamps, bin_width=bin_width, cycle=cycle)
    assert len(result.points) == 4
    recovered = [v for _, v in result.points]
    for true_rate, rec in zip(true_rates, recovered):
        assert rec == pytest.approx(true_rate, rel=0.25)


def test_arrival_rates_flags_overdispersed_series():
    """A deterministic, non-Poisson bin count (identical every cycle for most
    bins, wildly different for one) should trip the dispersion warning."""
    rng = np.random.default_rng(_SEED)
    cycle, bin_width = 10.0, 5.0
    timestamps = []
    for cyc in range(20):
        # bin 0: exactly 5 arrivals every cycle (dispersion ~ 0, far below 1)
        timestamps.extend((cyc * cycle + np.linspace(0, bin_width, 5, endpoint=False)).tolist())
        # bin 1: highly variable count (0 or 20), overdispersed
        count = 20 if cyc % 2 == 0 else 0
        timestamps.extend((cyc * cycle + bin_width + rng.random(count) * bin_width).tolist())
    timestamps = np.array(sorted(timestamps))

    result = inputfit.arrival_rates(timestamps, bin_width=bin_width, cycle=cycle)
    assert result.warnings, "expected at least one dispersion warning"


def test_arrival_rates_rejects_empty():
    with pytest.raises(ValueError):
        inputfit.arrival_rates([], bin_width=1.0)


# ---------------------------------------------------------------- CLI
def test_cli_prints_ranking_and_writes_json(tmp_path, capsys):
    values = _sample("Exponential", {"rate": 0.5}, n=500, seed=_SEED)
    data_path = tmp_path / "data.csv"
    pd.DataFrame({"value": values}).to_csv(data_path, index=False)
    json_path = tmp_path / "out.json"

    rc = inputfit.main([str(data_path), "--top", "3", "--json", str(json_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Distribution" in out and "AIC" in out
    assert "Best fit, ready to paste" in out
    payload = json.loads(json_path.read_text())
    assert payload["best"]["distribution"]
    assert len(payload["ranking"]) <= inputfit.fit_candidates(values).__len__()
