"""Input analyzer (ROADMAP 1.8): fit candidate distributions to a data column.

Top-level module beside `api.py`/`report.py`: it needs pandas/scipy/openpyxl
(already core dependencies) but no PySide6, so `naga fit ...` dispatches
here without importing Qt (see `nagasim/cli.py`).

    from nagasim.inputfit import load_column, describe, fit_candidates
    data = load_column("service_times.csv")
    stats = describe(data.values)
    fits = fit_candidates(data.values)
    best = fits[0]                          # ranked by AIC, ties by KS statistic
    # best.distribution / best.dist_params: ready to paste into a block

Every family fits with scipy and converts straight back into Naga's OWN
parameterization (`nagasim.core.distributions`); see the per-family `_fit_*`
functions for the scipy-call -> Naga-param conversion, one per line of the
table in the module docstring... (kept in DEVLOG/PR notes, not duplicated here).
"""
from __future__ import annotations

import csv
import io
import json as _json
import logging
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sstats

__all__ = [
    "LoadResult", "Fit", "ArrivalRateResult", "load_column", "describe",
    "fit_candidates", "best_fit", "empirical_spec", "arrival_rates", "bootstrap_rng",
    "bootstrap_fits", "bootstrap_arrival_rates", "main",
]

log = logging.getLogger(__name__)

# Catalogue: which families fit_candidates tries by default, split by data type
# (ROADMAP 1.8). A caller may override with `candidates=[...]` (e.g. to force
# a continuous fit attempt on data that happens to be integer-valued).
CONTINUOUS_FAMILIES = ["Exponential", "Normal", "Lognormal", "Gamma", "Erlang", "Weibull",
                       "Triangular", "Uniform", "Beta", "Rayleigh", "InverseGaussian",
                       "LogLogistic"]
DISCRETE_FAMILIES = ["Poisson", "Geometric", "DiscreteUniform", "Binomial"]

# Positive-support families: `loc` is fixed at 0 unless the caller passes
# shift=True (ROADMAP 1.8). Split by whether 0 itself is admissible.
_STRICTLY_POSITIVE = {"Exponential", "Lognormal", "Gamma", "Erlang", "InverseGaussian",
                      "LogLogistic"}
_NONNEGATIVE = {"Weibull", "Rayleigh"}

# scipy.stats.anderson only implements a fixed handful of reference
# distributions; everything else reports ad_stat = NaN (documented, not a bug).
_ANDERSON_SUPPORTED = {"Normal": "norm", "Exponential": "expon", "Weibull": "weibull_min"}

_OPTIMISTIC_KS_WARNING = ("KS p-value is optimistic: parameters were estimated from this "
                          "same sample (no held-out data).")


# ============================================================ data loading
@dataclass
class LoadResult:
    values: np.ndarray
    dropped: int
    column_names: list[str]


def _row_is_numeric(row) -> bool:
    """True if any non-blank cell in *row* parses as a float."""
    for cell in row:
        if not cell.strip():
            continue
        try:
            float(cell)
            return True
        except ValueError:
            continue
    return False


def load_column(path, column: str | None = None) -> LoadResult:
    """Read one numeric column from a CSV/TSV/TXT file (delimiter sniffed with
    `csv.Sniffer`, header optional) or an XLSX workbook. Defaults to the first
    column with any numeric value; non-numeric or blank cells are dropped and
    counted in the result rather than raising."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        sample = text[:4096]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
        # csv.Sniffer().has_header() needs several columns to compare types
        # across; with a single numeric column (the common case here) it is
        # unreliable, so decide directly: the first row is a header if it is
        # not itself numeric while a following row is.
        rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
        has_header = bool(rows) and not _row_is_numeric(rows[0]) and any(
            _row_is_numeric(r) for r in rows[1:6])
        df = pd.read_csv(io.StringIO(text), sep=delimiter, engine="python",
                         header=0 if has_header else None)
        if not has_header:
            df.columns = [str(c) for c in df.columns]

    column_names = [str(c) for c in df.columns]
    if column is not None:
        if column not in df.columns:
            raise KeyError(f"column '{column}' not found; available columns: {column_names}")
        series = df[column]
    else:
        series = next((df[c] for c in df.columns
                       if pd.to_numeric(df[c], errors="coerce").notna().any()), None)
        if series is None:
            raise ValueError("no numeric column found in the file")

    numeric = pd.to_numeric(series, errors="coerce")
    dropped = int(numeric.isna().sum())
    values = numeric.dropna().to_numpy(dtype=float)
    return LoadResult(values=values, dropped=dropped, column_names=column_names)


# ============================================================ description
def describe(values) -> dict:
    """Summary statistics and modelling flags (ROADMAP 1.8) for a 1-D sample."""
    values = np.asarray(values, dtype=float)
    n = int(values.size)
    if n == 0:
        nan = float("nan")
        return {"n": 0, "mean": nan, "sd": nan, "cv": nan, "min": nan, "max": nan,
               "q1": nan, "q2": nan, "q3": nan, "skewness": nan,
               "negative_values": False, "zeros": False, "integers_only": False, "n_small": True}
    mean = float(np.mean(values))
    sd = float(np.std(values, ddof=1)) if n > 1 else float("nan")
    q1, q2, q3 = np.percentile(values, [25, 50, 75])
    return {
        "n": n,
        "mean": mean,
        "sd": sd,
        "cv": (sd / mean) if mean else float("nan"),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "q1": float(q1), "q2": float(q2), "q3": float(q3),
        "skewness": float(sstats.skew(values)) if n > 2 else float("nan"),
        "negative_values": bool(np.any(values < 0)),
        "zeros": bool(np.any(values == 0)),
        "integers_only": bool(np.allclose(values, np.round(values))),
        "n_small": n < 30,
    }


# ============================================================ distribution fitting
@dataclass
class Fit:
    distribution: str            # Naga name (nagasim.core.distributions catalogue)
    dist_params: dict            # Naga's OWN parameterization, ready to paste
    loglik: float
    aic: float
    bic: float
    ks_stat: float
    ks_pvalue: float
    ad_stat: float                # Anderson-Darling statistic, NaN where scipy has no test
    n_params: int
    warnings: list[str] = field(default_factory=list)


def _metrics(values, frozen, n_params: int, *, discrete: bool):
    logf = frozen.logpmf if discrete else frozen.logpdf
    loglik = float(np.sum(logf(values)))
    n = len(values)
    aic = 2 * n_params - 2 * loglik
    bic = n_params * math.log(n) - 2 * loglik
    ks_stat, ks_p = sstats.kstest(values, frozen.cdf)
    return loglik, aic, bic, float(ks_stat), float(ks_p)


def _anderson_stat(name: str, values) -> float:
    key = _ANDERSON_SUPPORTED.get(name)
    if key is None:
        return float("nan")
    try:
        return float(sstats.anderson(values, dist=key).statistic)
    except Exception:  # pragma: no cover - scipy version without this dist
        return float("nan")


def _shift_warning(name: str, shift: bool, loc: float) -> list[str]:
    if shift and abs(loc) > 1e-9:
        return [f"shift=True: fitted location offset {loc:.6g} ignored; Naga's "
                f"{name} has no location parameter"]
    return []


def _scipy_fit(dist, values, shift: bool):
    """dist.fit(values), with loc fixed at 0 unless shift=True (ROADMAP 1.8:
    positive-support families default to loc=0)."""
    return dist.fit(values, **({} if shift else {"floc": 0}))


# -- continuous families: scipy call -> Naga's own parameter names -------
def _fit_exponential(values, shift):
    loc, scale = _scipy_fit(sstats.expon, values, shift)
    return ({"rate": 1.0 / scale}, sstats.expon(loc=loc, scale=scale), 1,
           _shift_warning("Exponential", shift, loc))


def _fit_normal(values, shift):
    loc, scale = sstats.norm.fit(values)     # unbounded support: no loc/shift distinction
    return {"mean": loc, "stddev": scale}, sstats.norm(loc=loc, scale=scale), 2, []


def _fit_lognormal(values, shift):
    s, loc, scale = _scipy_fit(sstats.lognorm, values, shift)
    return ({"mu": math.log(scale), "sigma": s}, sstats.lognorm(s, loc=loc, scale=scale), 2,
           _shift_warning("Lognormal", shift, loc))


def _fit_gamma(values, shift):
    a, loc, scale = _scipy_fit(sstats.gamma, values, shift)
    return ({"alpha": a, "beta": scale}, sstats.gamma(a, loc=loc, scale=scale), 2,
           _shift_warning("Gamma", shift, loc))


def _fit_erlang(values, shift):
    """Gamma with integer shape (ROADMAP 1.8): free-fit shape/scale first,
    round the shape to the nearest integer k >= 1, then re-estimate scale by
    closed-form gamma MLE with k fixed (mean = k * scale)."""
    a_free, loc, _scale_free = _scipy_fit(sstats.gamma, values, shift)
    k = max(1, int(round(a_free)))
    centred_mean = float(np.mean(values)) - loc
    scale = centred_mean / k
    warn = _shift_warning("Erlang", shift, loc)
    warn.append(f"k rounded to the nearest integer from the free-shape gamma fit ({a_free:.3g})")
    return {"k": k, "rate": 1.0 / scale}, sstats.gamma(k, loc=loc, scale=scale), 1, warn


def _fit_weibull(values, shift):
    c, loc, scale = _scipy_fit(sstats.weibull_min, values, shift)
    return ({"scale": scale, "shape": c}, sstats.weibull_min(c, loc=loc, scale=scale), 2,
           _shift_warning("Weibull", shift, loc))


def _fit_rayleigh(values, shift):
    loc, scale = _scipy_fit(sstats.rayleigh, values, shift)
    return ({"sigma": scale}, sstats.rayleigh(loc=loc, scale=scale), 1,
           _shift_warning("Rayleigh", shift, loc))


def _fit_invgauss(values, shift):
    # Naga's InverseGaussian plugs its own mu/lambda straight into scipy's
    # invgauss(mu=..., scale=...) (see core/distributions.py) -- same names,
    # so the fitted shape/scale carry over unchanged.
    mu, loc, scale = _scipy_fit(sstats.invgauss, values, shift)
    return ({"mu": mu, "lambda": scale}, sstats.invgauss(mu, loc=loc, scale=scale), 2,
           _shift_warning("InverseGaussian", shift, loc))


def _fit_loglogistic(values, shift):
    # Same direct pass-through as InverseGaussian: Naga calls
    # stats.fisk.rvs(c=c, scale=scale) already.
    c, loc, scale = _scipy_fit(sstats.fisk, values, shift)
    return ({"c": c, "scale": scale}, sstats.fisk(c, loc=loc, scale=scale), 2,
           _shift_warning("LogLogistic", shift, loc))


def _fit_triangular(values, shift):
    """Method-of-moments, not scipy MLE: the triangular likelihood is
    unbounded as low/high approach the sample extremes, so a direct MLE fit
    is numerically unstable. low/high are the data extremes padded by 1% (as
    for Beta) so the fitted density is never exactly zero at an observed
    point; mode follows from mean = (low + mode + high) / 3."""
    low, high = float(np.min(values)), float(np.max(values))
    span = high - low
    if span <= 0:
        raise ValueError("constant data")
    pad = span * 0.01
    low, high = low - pad, high + pad
    mode = min(max(3 * float(np.mean(values)) - low - high, low), high)
    c = (mode - low) / (high - low)
    return ({"low": low, "high": high, "mode": mode},
           sstats.triang(c, loc=low, scale=high - low), 3, [])


def _fit_uniform(values, shift):
    low, high = float(np.min(values)), float(np.max(values))
    if high <= low:
        raise ValueError("constant data")
    return {"low": low, "high": high}, sstats.uniform(loc=low, scale=high - low), 2, []


def _fit_beta(values, shift):
    dmin, dmax = float(np.min(values)), float(np.max(values))
    span = dmax - dmin
    if span <= 0:
        raise ValueError("constant data")
    pad = span * 0.01
    lo, hi = dmin - pad, dmax + pad
    scaled = (values - lo) / (hi - lo)
    a, b, _loc, _scale = sstats.beta.fit(scaled, floc=0, fscale=1)
    return ({"min": lo, "max": hi, "alpha": a, "beta": b},
           sstats.beta(a, b, loc=lo, scale=hi - lo), 2, [])


# -- discrete families --------------------------------------------------
def _fit_poisson(values, shift):
    lam = float(np.mean(values))
    return {"lambda": lam}, sstats.poisson(lam), 1, []


def _fit_geometric(values, shift):
    mean = float(np.mean(values))
    if mean < 1:
        raise ValueError("Geometric (trials-until-success, support >= 1) needs mean >= 1")
    p = 1.0 / mean
    return {"p": p}, sstats.geom(p), 1, []


def _fit_discrete_uniform(values, shift):
    low, high = int(np.min(values)), int(np.max(values))
    if high <= low:
        raise ValueError("constant data")
    return {"low": low, "high": high}, sstats.randint(low, high + 1), 2, []


def _fit_binomial(values, shift):
    """n fixed at the observed max (ROADMAP 1.8: "Binomial-with-n=max"), p by
    MLE given that n. A crude heuristic: it only recovers the true n when the
    data plausibly reaches it (small n or p not too far from 0.5)."""
    n_trials = int(np.max(values))
    if n_trials <= 0:
        raise ValueError("Binomial needs positive integer data")
    p = min(max(float(np.mean(values)) / n_trials, 1e-9), 1.0)
    return {"n": n_trials, "p": p}, sstats.binom(n_trials, p), 1, []


_FITTERS = {
    "Exponential": _fit_exponential, "Normal": _fit_normal, "Lognormal": _fit_lognormal,
    "Gamma": _fit_gamma, "Erlang": _fit_erlang, "Weibull": _fit_weibull,
    "Triangular": _fit_triangular, "Uniform": _fit_uniform, "Beta": _fit_beta,
    "Rayleigh": _fit_rayleigh, "InverseGaussian": _fit_invgauss, "LogLogistic": _fit_loglogistic,
    "Poisson": _fit_poisson, "Geometric": _fit_geometric,
    "DiscreteUniform": _fit_discrete_uniform, "Binomial": _fit_binomial,
}


def _support_reason(name: str, values: np.ndarray, shift: bool) -> str | None:
    if shift:
        return None
    if name in _STRICTLY_POSITIVE and not np.all(values > 0):
        return f"{name} needs strictly positive data (pass shift=True to fit a location offset)"
    if name in _NONNEGATIVE and not np.all(values >= 0):
        return f"{name} needs non-negative data (pass shift=True to fit a location offset)"
    return None


def fit_candidates(values, *, candidates: list[str] | None = None, shift: bool = False) -> list[Fit]:
    """Fit every candidate family, ranked by AIC (ties by KS statistic).

    `candidates` defaults to Naga's discrete catalogue for integer-valued
    data, the continuous one otherwise (ROADMAP 1.8). A family whose support
    excludes the data, or whose fit fails outright, is skipped (logged, not
    raised) rather than included with a broken row.
    """
    values = np.asarray(values, dtype=float)
    desc = describe(values)
    names = candidates if candidates is not None else (
        list(DISCRETE_FAMILIES) if desc["integers_only"] else list(CONTINUOUS_FAMILIES))

    fits: list[Fit] = []
    for name in names:
        fitter = _FITTERS.get(name)
        if fitter is None:
            log.warning("fit_candidates: unknown distribution '%s'", name)
            continue
        is_discrete = name in DISCRETE_FAMILIES
        if is_discrete and not desc["integers_only"]:
            log.info("%s: skipped, data is not integer-valued", name)
            continue
        reason = _support_reason(name, values, shift)
        if reason is not None:
            log.info("%s: skipped (%s)", name, reason)
            continue
        try:
            params, frozen, n_params, warn = fitter(values, shift)
        except Exception as exc:
            log.info("%s: skipped, fit failed (%s)", name, exc)
            continue
        loglik, aic, bic, ks_stat, ks_p = _metrics(values, frozen, n_params, discrete=is_discrete)
        ad_stat = float("nan") if is_discrete else _anderson_stat(name, values)
        fits.append(Fit(distribution=name, dist_params=params, loglik=loglik, aic=aic, bic=bic,
                        ks_stat=ks_stat, ks_pvalue=ks_p, ad_stat=ad_stat, n_params=n_params,
                        warnings=[*warn, _OPTIMISTIC_KS_WARNING]))

    fits.sort(key=lambda f: (f.aic, f.ks_stat))
    return fits


# ============================================================ empirical fallback
def best_fit(values, *, candidates: list[str] | None = None, shift: bool = False):
    """Fit candidate distributions to a sample and return the winner.

    The winner is the best by AIC, ties broken by the KS statistic. This is the
    one place the criterion is applied, so the ``fit`` data binding and the MES
    parameter feed cannot drift apart.

    Parameters
    ----------
    values : sequence of float
        The observed sample.
    candidates : list of str, optional
        Distribution families to try (default: all that can be fit).
    shift : bool, default False
        Allow a location shift.

    Returns
    -------
    Fit or None
        The winning fit (``distribution``, ``dist_params``, ``aic``, ``ks_stat``
        and more), or None when no family could be fit at all.

    See Also
    --------
    nagasim.calibrate.calibrate : Tune a model rather than fit an input.

    Examples
    --------
    >>> from nagasim.inputfit import best_fit
    >>> fit = best_fit([1.0, 1.4, 0.7, 2.2, 1.1, 0.9, 1.6, 3.0, 0.8, 1.2])
    >>> isinstance(fit.distribution, str) and fit.aic == fit.aic
    True
    """
    fits = fit_candidates(values, candidates=candidates, shift=shift)
    return fits[0] if fits else None


def empirical_spec(values, bins: int | None = None) -> dict:
    """Empirical fallback, ready to paste as `{distribution, dist_params}`
    (ROADMAP 1.8). Integer-valued data becomes an EmpiricalDiscrete point-mass
    spec. Otherwise a piecewise-uniform EmpiricalContinuous spec: `values` are
    `bins` histogram edges (one more entry than `probs`), `probs` the
    fraction of the data in each bin -- sampling picks a bin by `probs` then
    draws uniformly within it (true continuous, not point resampling)."""
    values = np.asarray(values, dtype=float)
    if describe(values)["integers_only"]:
        uniq, counts = np.unique(np.round(values).astype(int), return_counts=True)
        return {"distribution": "EmpiricalDiscrete",
               "dist_params": {"values": uniq.tolist(), "probs": (counts / counts.sum()).tolist()}}
    bins = bins or min(50, max(10, int(math.sqrt(len(values)))))
    counts, edges = np.histogram(values, bins=bins)
    return {"distribution": "EmpiricalContinuous",
           "dist_params": {"values": edges.tolist(), "probs": (counts / counts.sum()).tolist()}}


# ============================================================ arrival-rate estimation
@dataclass
class ArrivalRateResult:
    points: list[list[float]]   # schedules format: [[duration, rate], ...]
    warnings: list[str]         # one per bin whose count dispersion looks non-Poisson
    bands: list[list[float]] = field(default_factory=list)  # [low, high] rate per bin
    cycles: int = 0             # cycles the data spans (0 without a cycle)
    resamples: int = 0          # cycle resamples the bands rest on (0: no bands)


def _relative_stamps(timestamps, bin_width: float) -> np.ndarray:
    if bin_width <= 0:
        raise ValueError("bin_width must be > 0")
    ts = np.sort(np.asarray(timestamps, dtype=float))
    if ts.size == 0:
        raise ValueError("no timestamps")
    return ts - ts[0]


def _cycle_counts(t0: np.ndarray, bin_width: float, cycle: float) -> np.ndarray:
    """Arrivals per (cycle, bin), from timestamps relative to the first one."""
    n_bins = max(1, int(math.ceil(cycle / bin_width)))
    n_cycles = max(1, int(math.floor(t0[-1] / cycle)) + 1)
    counts = np.zeros((n_cycles, n_bins))
    cyc_idx = np.minimum((t0 // cycle).astype(int), n_cycles - 1)
    bin_idx = np.minimum(((t0 % cycle) // bin_width).astype(int), n_bins - 1)
    np.add.at(counts, (cyc_idx, bin_idx), 1)
    return counts


def arrival_rates(timestamps, *, bin_width: float, cycle: float | None = None,
                  resamples: int = 0, seed: int = 0,
                  confidence: float = 0.95) -> ArrivalRateResult:
    """Turn arrival timestamps into a piecewise-constant rate table in the
    `nagasim.core.schedules` "points" format (ROADMAP 1.8). With `cycle` (e.g.
    one day), timestamps fold onto one cycle and each bin's rate is the mean
    count per cycle / bin_width; a bin whose count variance/mean across
    cycles strays far from 1 gets a dispersion warning (not consistent with a
    Poisson/NHPP arrival process).

    With `resamples` (a `cycle` is then required), `bands` holds each bin's
    percentile interval over that many resamples of whole cycles
    (`bootstrap_arrival_rates`, seeded by `bootstrap_rng(seed)`): arrivals within a
    cycle are not independent, whole cycles are taken to be (D720)."""
    t0 = _relative_stamps(timestamps, bin_width)
    warnings: list[str] = []
    if resamples and not cycle:
        raise ValueError("rate bands resample whole cycles: give a cycle")
    bands: list[list[float]] = []
    n_cycles = 0

    if cycle:
        counts = _cycle_counts(t0, bin_width, cycle)
        n_cycles, n_bins = counts.shape
        if resamples:
            draws = np.array([[rate for _w, rate in table] for table in bootstrap_arrival_rates(
                timestamps, bin_width=bin_width, cycle=cycle, resamples=resamples,
                rng=bootstrap_rng(seed))])
            tail = 50.0 * (1.0 - confidence)
            low, high = np.percentile(draws, [tail, 100.0 - tail], axis=0)
            bands = [[float(lo), float(hi)] for lo, hi in zip(low, high)]
        mean_counts = counts.mean(axis=0)
        rates = mean_counts / bin_width
        if n_cycles > 1:
            var_counts = counts.var(axis=0, ddof=1)
            for i in range(n_bins):
                if mean_counts[i] <= 0:
                    continue
                dispersion = var_counts[i] / mean_counts[i]
                if dispersion < 0.5 or dispersion > 2.0:      # ponytail: a fixed rule-of-thumb band
                    warnings.append(
                        f"bin {i} [{i * bin_width:.3g}, {(i + 1) * bin_width:.3g}): "
                        f"variance/mean count = {dispersion:.2f} across {n_cycles} cycles, "
                        f"not consistent with a Poisson arrival process")
    else:
        n_bins = max(1, int(math.ceil((t0[-1] + 1e-9) / bin_width)))
        bin_idx = np.minimum((t0 // bin_width).astype(int), n_bins - 1)
        rates = np.bincount(bin_idx, minlength=n_bins) / bin_width

    return ArrivalRateResult(points=[[float(bin_width), float(r)] for r in rates], warnings=warnings,
                             bands=bands, cycles=int(n_cycles), resamples=int(resamples))


# ============================================================ bootstrap (input uncertainty)
def bootstrap_rng(seed: int) -> np.random.Generator:
    """The bootstrap's own random stream: a spawned child of `SeedSequence(seed)`, which
    shares no state with the engine's per-block streams (`SeedSequence([seed, hash])`),
    so the same seed gives the same resamples and every run draws what it always drew."""
    return np.random.default_rng(np.random.SeedSequence(int(seed)).spawn(1)[0])


def bootstrap_fits(values, family: str, resamples: int, rng: np.random.Generator, *,
                   shift: bool = False) -> list[dict | None]:
    """Refit *family* to *resamples* bootstrap resamples of *values* (drawn with
    replacement, the sample's own size); each one's `dist_params`, plain floats.

    The family is pinned to the caller's (the best fit to the whole sample, D719):
    left free, integer data or a close ranking lets resamples switch families, and
    the spread would then mix families with parameters. A resample that cannot be
    fitted (the fit fails, or its likelihood or a parameter is not finite) is None
    in its place, so the caller can count it."""
    values = np.asarray(values, dtype=float)
    out: list[dict | None] = []
    for _ in range(int(resamples)):
        sample = rng.choice(values, size=values.size, replace=True)
        with warnings.catch_warnings():     # a degenerate resample is counted, not warned about
            warnings.simplefilter("ignore", RuntimeWarning)
            fits = fit_candidates(sample, candidates=[family], shift=shift)
        params = ({k: v.item() if hasattr(v, "item") else v for k, v in fits[0].dist_params.items()}
                  if fits else None)
        usable = params is not None and math.isfinite(fits[0].aic) and all(
            math.isfinite(v) for v in params.values() if isinstance(v, (int, float)))
        out.append(params if usable else None)
    return out


def bootstrap_arrival_rates(timestamps, *, bin_width: float, cycle: float, resamples: int,
                            rng: np.random.Generator) -> list[list[list[float]]]:
    """*resamples* rate tables (the `arrival_rates` points format), each the mean of
    whole cycles drawn with replacement (D720). Raises ValueError for data spanning
    fewer than 2 cycles: one cycle says nothing about how cycles differ."""
    counts = _cycle_counts(_relative_stamps(timestamps, bin_width), bin_width, cycle)
    n_cycles = counts.shape[0]
    if n_cycles < 2:
        raise ValueError(f"the timestamps span {n_cycles} cycle(s); resampling needs at "
                         "least 2 cycles")
    return [[[float(bin_width), float(r)]
             for r in counts[rng.integers(0, n_cycles, n_cycles)].mean(axis=0) / bin_width]
            for _ in range(int(resamples))]


# ============================================================ CLI
def main(argv=None) -> int:
    """`naga fit data.csv [--column NAME] [--top 5] [--shift] [--json out.json]`
    (dispatched from `nagasim/cli.py`, no Qt import)."""
    import argparse

    parser = argparse.ArgumentParser(prog="naga fit",
                                     description="Fit candidate distributions to a data column.")
    parser.add_argument("data", help="CSV/TSV/TXT or XLSX file")
    parser.add_argument("--column", default=None, help="default: the first numeric column")
    parser.add_argument("--top", type=int, default=5, help="ranking rows to print")
    parser.add_argument("--shift", action="store_true",
                        help="fit a free location offset for positive-support families")
    parser.add_argument("--json", default=None, help="write the full ranking + best spec here")
    args = parser.parse_args(argv)

    loaded = load_column(args.data, column=args.column)
    if loaded.dropped:
        print(f"Dropped {loaded.dropped} non-numeric/blank row(s)")
    fits = fit_candidates(loaded.values, shift=args.shift)
    if not fits:
        print("No distribution could be fit to this data.")
        return 1

    print(f"{'Distribution':<16}{'AIC':>12}{'BIC':>12}{'KS p':>10}{'AD':>10}")
    for f in fits[: args.top]:
        ad = f"{f.ad_stat:.3g}" if math.isfinite(f.ad_stat) else "-"
        print(f"{f.distribution:<16}{f.aic:>12.2f}{f.bic:>12.2f}{f.ks_pvalue:>10.3g}{ad:>10}")

    best = fits[0]
    spec = {"distribution": best.distribution, "dist_params": best.dist_params}
    print("\nBest fit, ready to paste:")
    print(_json.dumps(spec, indent=2))

    if args.json:
        payload = {
            "ranking": [{"distribution": f.distribution, "dist_params": f.dist_params,
                        "aic": f.aic, "bic": f.bic, "ks_stat": f.ks_stat, "ks_pvalue": f.ks_pvalue,
                        "ad_stat": f.ad_stat, "warnings": f.warnings} for f in fits],
            "best": spec,
        }
        Path(args.json).write_text(_json.dumps(payload, indent=2))
        print(f"\nWritten to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
