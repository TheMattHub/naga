import math
import numpy as np
from scipy import stats
import logging

from nagasim.core.distribution_spec import DistParam, DistributionSpec, ScaleParam
from nagasim.core.expressions import ExpressionScope, compile_expression
from nagasim.core.timeunits import tidy

log = logging.getLogger(__name__)

__all__ = [
    "StochasticDistribution", "get_available_distributions", "get_distribution_params",
    "DISTRIBUTION_REGISTRY", "DISTRIBUTION_USE_CASES", "DISTRIBUTION_PARAM_HINTS",
    "ARRIVAL_DISTRIBUTIONS", "DELAY_DISTRIBUTIONS", "SPECIAL_PURPOSE_DISTRIBUTIONS",
    "distribution_mean", "distribution_needs_finite_mean",
    "distribution_scale_params", "scale_distribution_params", "rescale_distribution_params",
]

# ROADMAP 2.4 A: one registry entry per distribution (name, params, sampler,
# category, use case, arrival/delay applicability, support lower bound),
# replacing the five hand-synced tables that used to live here (D12). Every
# public name below (ARRIVAL_DISTRIBUTIONS, get_distribution_params, ...) is
# now a thin view derived from DISTRIBUTION_REGISTRY, so callers never change,
# and a plugin distribution registered later (`nagasim.plugins`) shows up
# everywhere for free once `_rebuild_derived_tables()` runs.

_MAX_BOUNDED_ATTEMPTS = 100   # BoundedNormal rejection-sampling cap


# ============================================================ samplers
# sampler(rng, params, *, current=None, scope=None, state=None) -> value.
# Almost every one only reads (rng, params) and swallows the rest via **_;
# only FixedSchedule (current/state, its cursor) and CustomExpression (scope)
# need the extra context. Kept as plain module functions -- one per
# distribution, next to its registry entry below -- rather than methods, so a
# plugin can hand in the exact same shape (DEVLOG #25: every sampler still
# takes the caller's rng on every call, no global random state).

def _sample_constant(rng, p, **_):
    return p.get("value", 1)


def _mean_constant(p):
    return float(p.get("value", 1))


def _sample_uniform(rng, p, **_):
    return float(rng.uniform(p.get("low", 0), p.get("high", 1)))


def _mean_uniform(p):
    return float((p.get("low", 0) + p.get("high", 1)) / 2.0)


def _sample_exponential(rng, p, **_):
    return float(rng.exponential(1.0 / p.get("rate", 1.0)))


def _mean_exponential(p):
    rate = p.get("rate", 1.0)
    return float(1.0 / rate) if rate > 0 else None


def _sample_normal(rng, p, **_):
    return float(rng.normal(p.get("mean", 0), p.get("stddev", 1)))


def _mean_normal(p):
    return float(p.get("mean", 0))


def _sample_bounded_normal(rng, p, **_):
    """Truncated normal by rejection sampling; clipped if the bounds are too
    tight to hit within _MAX_BOUNDED_ATTEMPTS draws (DEVLOG #4)."""
    mean, sd = p.get("mean", 1), p.get("stddev", 1)
    lo, hi = p.get("min", 0), p.get("max", float("inf"))
    for _ in range(_MAX_BOUNDED_ATTEMPTS):
        x = float(rng.normal(mean, sd))
        if lo <= x <= hi:
            return x
    log.warning(f"BoundedNormal: no draw inside [{lo}, {hi}] after {_MAX_BOUNDED_ATTEMPTS} attempts; clipping.")
    return float(min(max(rng.normal(mean, sd), lo), hi))


def _mean_bounded_normal(p):
    """Exact truncated-normal mean (what the rejection sampler approximates):
    scipy's own `truncnorm.mean`, not the raw `mean` param -- truncation
    shifts the expectation whenever the bounds cut a real share of the
    underlying normal's mass."""
    mean, sd = p.get("mean", 1), p.get("stddev", 1)
    lo, hi = p.get("min", 0), p.get("max", float("inf"))
    if sd <= 0:
        return float(min(max(mean, lo), hi))
    a, b = (lo - mean) / sd, (hi - mean) / sd
    m = stats.truncnorm.mean(a, b, loc=mean, scale=sd)
    return float(m) if np.isfinite(m) else None


def _sample_lognormal(rng, p, **_):
    return float(rng.lognormal(p.get("mu", 0), p.get("sigma", 1)))


def _mean_lognormal(p):
    mu, sigma = p.get("mu", 0), p.get("sigma", 1)
    return float(math.exp(mu + sigma * sigma / 2.0))


def _sample_triangular(rng, p, **_):
    # numpy takes (left, mode, right), not python's (low, high, mode)
    low, high = p.get("low", 0), p.get("high", 1)
    if high <= low:          # degenerate: numpy rejects left == right
        return float(low)
    mode = min(max(p.get("mode", (low + high) / 2), low), high)
    return float(rng.triangular(low, mode, high))


def _mean_triangular(p):
    low, high = p.get("low", 0), p.get("high", 1)
    if high <= low:
        return float(low)
    mode = min(max(p.get("mode", (low + high) / 2), low), high)
    return float((low + high + mode) / 3.0)


def _sample_gamma(rng, p, **_):
    return float(rng.gamma(p.get("alpha", 1), p.get("beta", 1)))


def _mean_gamma(p):
    alpha, beta = p.get("alpha", 1), p.get("beta", 1)
    return float(alpha * beta) if alpha > 0 and beta > 0 else None


def _sample_weibull(rng, p, **_):
    return float(p.get("scale", 1) * rng.weibull(p.get("shape", 1)))


def _mean_weibull(p):
    scale, shape = p.get("scale", 1), p.get("shape", 1)
    if shape <= 0 or scale < 0:
        return None
    return float(scale * math.gamma(1.0 + 1.0 / shape))


def _sample_beta(rng, p, **_):
    lo, hi = p.get("min", 0), p.get("max", 1)
    return float(lo + (hi - lo) * rng.beta(p.get("alpha", 1), p.get("beta", 1)))


def _mean_beta(p):
    lo, hi = p.get("min", 0), p.get("max", 1)
    alpha, beta = p.get("alpha", 1), p.get("beta", 1)
    if alpha <= 0 or beta <= 0:
        return None
    return float(lo + (hi - lo) * alpha / (alpha + beta))


def _sample_erlang(rng, p, **_):
    return float(rng.gamma(p.get("k", 1), 1.0 / p.get("rate", 1)))


def _mean_erlang(p):
    k, rate = p.get("k", 1), p.get("rate", 1)
    return float(k / rate) if rate > 0 and k > 0 else None


def _sample_cauchy(rng, p, **_):
    return float(stats.cauchy.rvs(loc=p.get("x0", 0), scale=p.get("gamma", 1), random_state=rng))


def _mean_cauchy(p):
    return None   # no finite mean, whatever the params (heavy-tailed by design)


def _sample_inverse_gaussian(rng, p, **_):
    return float(stats.invgauss.rvs(mu=p.get("mu", 1), scale=p.get("lambda", 1), random_state=rng))


def _mean_inverse_gaussian(p):
    mu, lam = p.get("mu", 1), p.get("lambda", 1)
    if mu <= 0 or lam <= 0:
        return None
    m = stats.invgauss.mean(mu, scale=lam)
    return float(m) if np.isfinite(m) else None


def _sample_rayleigh(rng, p, **_):
    return float(rng.rayleigh(p.get("sigma", 1)))


def _mean_rayleigh(p):
    sigma = p.get("sigma", 1)
    return float(sigma * math.sqrt(math.pi / 2.0)) if sigma > 0 else None


def _sample_chi_square(rng, p, **_):
    return float(rng.chisquare(p.get("df", 1)))


def _mean_chi_square(p):
    df = p.get("df", 1)
    return float(df) if df > 0 else None


def _sample_loglogistic(rng, p, **_):
    return float(stats.fisk.rvs(c=p.get("c", 1), scale=p.get("scale", 1), random_state=rng))  # fisk is log-logistic


def _mean_loglogistic(p):
    # fisk's mean is infinite for shape c <= 1 -- scipy returns inf, not finite.
    m = stats.fisk.mean(c=p.get("c", 1), scale=p.get("scale", 1))
    return float(m) if np.isfinite(m) else None


def _sample_johnson_su(rng, p, **_):
    return float(stats.johnsonsu.rvs(a=p.get("a", 0), b=p.get("b", 1), loc=p.get("loc", 0),
                                     scale=p.get("scale", 1), random_state=rng))


def _mean_johnson_su(p):
    m = stats.johnsonsu.mean(a=p.get("a", 0), b=p.get("b", 1), loc=p.get("loc", 0), scale=p.get("scale", 1))
    return float(m) if np.isfinite(m) else None


def _sample_johnson_sb(rng, p, **_):
    z = rng.normal(0, 1)
    a1 = p.get("a1", 0)
    a2 = p.get("a2", 1)
    a = p.get("min", 0)
    b = p.get("max", 1)
    try:
        exp_term = math.exp((z - a1) / a2)
        return a + (b - a) / (1 + exp_term)
    except ZeroDivisionError:
        return (a + b) / 2  # Fallback to midpoint


def _mean_johnson_sb(p):
    """`_sample_johnson_sb` draws Y = 1 / (1 + exp((Z - a1) / a2)), Z ~ N(0,1)
    -- the COMPLEMENT of scipy's own standard `johnsonsb(a1, a2)` variable
    (whose own draw is `logistic((Z - a1) / a2)`, and Y = 1 - that), then
    scales the complement into [min, max]: mean(final) = max - (max - min) *
    E[johnsonsb(a1, a2)]."""
    a1, a2 = p.get("a1", 0), p.get("a2", 1)
    lo, hi = p.get("min", 0), p.get("max", 1)
    if a2 == 0:
        return None
    m = stats.johnsonsb.mean(a1, a2)
    return float(hi - (hi - lo) * m) if np.isfinite(m) else None


def _sample_pearson_v(rng, p, **_):
    return float(stats.invgamma.rvs(a=p.get("a", 1), scale=p.get("scale", 1), random_state=rng))


def _mean_pearson_v(p):
    m = stats.invgamma.mean(a=p.get("a", 1), scale=p.get("scale", 1))
    return float(m) if np.isfinite(m) else None


def _sample_pearson_vi(rng, p, **_):
    y = rng.beta(p.get("a", 1), p.get("b", 1))
    return float(p.get("scale", 1) * y / (1 - y))


def _mean_pearson_vi(p):
    # Y/(1-Y) for Y ~ Beta(a, b) is exactly scipy's "beta prime" variable.
    m = stats.betaprime.mean(p.get("a", 1), p.get("b", 1), scale=p.get("scale", 1))
    return float(m) if np.isfinite(m) else None


def _sample_bernoulli(rng, p, **_):
    return 1 if rng.random() < p.get("p", 0.5) else 0


def _mean_bernoulli(p):
    return float(p.get("p", 0.5))


def _sample_binomial(rng, p, **_):
    return int(rng.binomial(int(p.get("n", 1)), float(p.get("p", 0.5))))


def _mean_binomial(p):
    return float(p.get("n", 1)) * float(p.get("p", 0.5))


def _sample_geometric(rng, p, **_):
    return int(rng.geometric(p.get("p", 0.5)))


def _mean_geometric(p):
    # numpy's geometric counts the trial of the first success (support >= 1).
    prob = p.get("p", 0.5)
    return float(1.0 / prob) if prob > 0 else None


def _sample_poisson(rng, p, **_):
    return int(rng.poisson(p.get("lambda", 1)))


def _mean_poisson(p):
    return float(p.get("lambda", 1))


def _sample_negative_binomial(rng, p, **_):
    return int(rng.negative_binomial(p.get("r", 1), p.get("p", 0.5)))


def _mean_negative_binomial(p):
    # numpy's negative_binomial counts FAILURES before r successes.
    r, prob = p.get("r", 1), p.get("p", 0.5)
    return float(r * (1.0 - prob) / prob) if prob > 0 else None


def _sample_discrete_uniform(rng, p, **_):
    return int(rng.integers(int(p.get("low", 0)), int(p.get("high", 1)) + 1))


def _mean_discrete_uniform(p):
    return float((p.get("low", 0) + p.get("high", 1)) / 2.0)


def _sample_hypergeometric(rng, p, **_):
    N, K, n = int(p.get("N", 10)), int(p.get("K", 5)), int(p.get("n", 3))
    return int(rng.hypergeometric(K, N - K, n))


def _mean_hypergeometric(p):
    N, K, n = p.get("N", 10), p.get("K", 5), p.get("n", 3)
    return float(n * K / N) if N > 0 else None


def _choice(rng, p):
    values = p.get("values", [])
    weights = np.asarray(p.get("probs", []), dtype=float)
    if weights.size != len(values) or weights.sum() <= 0:
        return rng.choice(values)
    return rng.choice(values, p=weights / weights.sum())


def _empirical_continuous(rng, p):
    """True piecewise-linear inverse-CDF sampling (ROADMAP 1.8): `values` are
    len(probs) + 1 bin edges and `probs` the probability mass in each bin --
    pick a bin by `probs`, then draw uniformly within it (a piecewise-uniform
    density, piecewise-linear CDF).

    A model saved before this existed may still carry one point value per
    prob -- the pre-1.8 behaviour, which actually resampled discrete points
    despite the "Continuous" name (a known misnomer). That shape keeps
    working exactly as before so old files still run; validation warns
    to migrate it to EmpiricalDiscrete or to bin-edge values."""
    values = p.get("values", [])
    weights = np.asarray(p.get("probs", []), dtype=float)
    if weights.size and len(values) == weights.size + 1 and weights.sum() > 0:
        probs = weights / weights.sum()
        i = int(rng.choice(weights.size, p=probs))
        lo, hi = float(values[i]), float(values[i + 1])
        return lo + rng.random() * (hi - lo)
    return _choice(rng, p)   # legacy point-value behaviour


def _sample_empirical_continuous(rng, p, **_):
    return float(_empirical_continuous(rng, p))


def _mean_empirical_continuous(p):
    """Mirrors `_empirical_continuous`'s own two shapes: len(values) ==
    len(probs) + 1 bin edges -> weighted mean of each bin's midpoint
    (piecewise-uniform within it); legacy len(values) == len(probs) point
    values (pre-1.8 behaviour) -> their weighted mean."""
    values = p.get("values", [])
    weights = np.asarray(p.get("probs", []), dtype=float)
    if not weights.size or weights.sum() <= 0:
        return None
    probs = weights / weights.sum()
    if len(values) == weights.size + 1:
        return float(sum(((float(values[i]) + float(values[i + 1])) / 2.0) * pi
                         for i, pi in enumerate(probs)))
    if len(values) == weights.size:
        return float(sum(float(v) * pi for v, pi in zip(values, probs)))
    return None


def _sample_empirical_discrete(rng, p, **_):
    return int(_choice(rng, p))


def _mean_empirical_discrete(p):
    values = p.get("values", [])
    weights = np.asarray(p.get("probs", []), dtype=float)
    if not values or weights.size != len(values) or weights.sum() <= 0:
        return None
    probs = weights / weights.sum()
    return float(sum(float(v) * pi for v, pi in zip(values, probs)))


def _sample_fixed_schedule(rng, p, *, current=None, state=None, **_):
    schedule = p.get("schedule", [])
    cursor = state.get("cursor", 0) if state is not None else 0
    while cursor < len(schedule):
        next_time = schedule[cursor]
        cursor += 1
        if current is None or next_time >= current:
            if state is not None:
                state["cursor"] = cursor
            return next_time
    if state is not None:
        state["cursor"] = cursor
    return None


def _sample_custom_expression(rng, p, *, scope=None, **_):
    # Fails loud on a bad expression: validation compiles every one at
    # load time, so a failure here is a real modelling error (#53).
    return compile_expression(p.get("expression", "1")).evaluate(scope)


# ============================================================ registry
# Definition order below is cosmetic only (no consumer relies on it) --
# kept close to the original hand-written tables' order for an easy diff.
DISTRIBUTION_REGISTRY: dict[str, DistributionSpec] = {}


def _add(name, params, sampler, category, use_case, *, arrival=False, delay=False,
        support_lower=None, fit=None, mean=None, scale_params=None):
    DISTRIBUTION_REGISTRY[name] = DistributionSpec(
        name=name, params=tuple(params), sampler=sampler, category=category, use_case=use_case,
        arrival=arrival, delay=delay, support_lower=support_lower, fit=fit, mean=mean,
        scale_params=tuple(scale_params) if scale_params else None)


def _p(name, default, kind="number", hint=""):
    return DistParam(name, default, hint=hint, kind=kind)


_add("Constant", [_p("value", 1.0, hint="fixed duration, >= 0")], _sample_constant, "continuous",
    "Fixed delay or service time, commonly used for deterministic processes.\n"
    "Params: value (fixed duration) >= 0.",
    arrival=True, delay=True, support_lower=0.0, mean=_mean_constant,
    scale_params=[ScaleParam("value")])

_add("Uniform", [_p("low", 0.0, hint="minimum, <= high"), _p("high", 1.0, hint="maximum, >= low")],
    _sample_uniform, "continuous",
    "Equal probability within a bounded range; used when only min and max are known.\n"
    "Params: low (min) =< high (max), both real numbers.",
    arrival=True, delay=True, support_lower=0.0, fit="Uniform", mean=_mean_uniform,
    scale_params=[ScaleParam("low"), ScaleParam("high")])

_add("Exponential", [_p("rate", 1.0, hint="event rate (lambda), > 0")], _sample_exponential, "continuous",
    "Classic model for memoryless interarrival and failure times.\nParam: rate (lambda, event rate) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="Exponential", mean=_mean_exponential,
    scale_params=[ScaleParam("rate", "inverse")])

_add("Normal", [_p("mean", 0.0, hint="central value"), _p("stddev", 1.0, hint="standard deviation, > 0")],
    _sample_normal, "special",
    "Models symmetric variation around a mean (e.g., repair times), can produce negative values "
    "unless mean >> 0 and .\nParams: mean (central value), stddev (standard deviation) > 0.",
    support_lower=None, fit="Normal", mean=_mean_normal,
    scale_params=[ScaleParam("mean"), ScaleParam("stddev")])

_add("BoundedNormal", [_p("mean", 0.0, hint="central value"), _p("stddev", 1.0, hint="standard deviation, > 0"),
    _p("min", 0.0, hint="lower bound, < max"), _p("max", 1.0, hint="upper bound, > min")],
    _sample_bounded_normal, "special",
    "Normal distribution bounded within a range; useful for processes with natural limits, "
    "such as delays.\nParams: mean (central value), stddev (standard deviation) > 0; min < max.",
    delay=True, support_lower=0.0, mean=_mean_bounded_normal)

_add("Lognormal", [_p("mu", 0.0, hint="log-mean"), _p("sigma", 1.0, hint="log-stddev, > 0")],
    _sample_lognormal, "continuous",
    "Right-skewed times like human handling or picking durations.\nParams: mu (log-mean), sigma (log-stddev) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="Lognormal", mean=_mean_lognormal,
    # mean = exp(mu + sigma^2/2): multiplying the mean by f means mu += ln(f)
    # (sigma, the log-scale SHAPE, is untouched -- it sets the coefficient of
    # variation, not the mean); a "log"-kind ScaleParam, not "linear".
    scale_params=[ScaleParam("mu", "log")])

_add("Triangular", [_p("low", 0.0, hint="minimum, <= mode"), _p("high", 1.0, hint="maximum, >= mode"),
    _p("mode", 0.5, hint="most likely value, between low and high")], _sample_triangular, "continuous",
    "Used with expert estimates of min, most likely, and max times.\nParams: low =< mode =< high.",
    arrival=True, delay=True, support_lower=0.0, fit="Triangular", mean=_mean_triangular,
    scale_params=[ScaleParam("low"), ScaleParam("high"), ScaleParam("mode")])

_add("Gamma", [_p("alpha", 1.0, hint="shape, > 0"), _p("beta", 1.0, hint="scale, > 0")], _sample_gamma, "continuous",
    "Flexible model for skewed service/process times.\nParams: alpha (shape) > 0, beta (scale) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="Gamma", mean=_mean_gamma,
    scale_params=[ScaleParam("beta")])   # alpha is shape, left literal

_add("Weibull", [_p("scale", 1.0, hint="lambda, > 0"), _p("shape", 1.0, hint="k, > 0")], _sample_weibull, "continuous",
    "Common for modeling equipment lifetimes and breakdowns.\nParams: scale (lambda) > 0, shape (k) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="Weibull", mean=_mean_weibull,
    scale_params=[ScaleParam("scale")])   # shape is shape, left literal

_add("Beta", [_p("min", 0.0, hint="lower bound, < max"), _p("max", 1.0, hint="upper bound, > min"),
    _p("alpha", 1.0, hint="shape, > 0"), _p("beta", 1.0, hint="shape, > 0")], _sample_beta, "continuous",
    "Useful for bounded times or probabilities.\nParams: alpha, beta > 0; min < max.",
    arrival=True, delay=True, support_lower=0.0, fit="Beta", mean=_mean_beta)

_add("Erlang", [_p("k", 1.0, hint="number of exponential stages, >= 1"), _p("rate", 1.0, hint="lambda, > 0")],
    _sample_erlang, "continuous",
    "Sum of k exponential stages (special case of gamma).\nParams: k (stages) >= 1, rate (lambda) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="Erlang", mean=_mean_erlang,
    scale_params=[ScaleParam("rate", "inverse")])   # mean = k/rate; k (stage count) is shape

# Cauchy/InverseGaussian/ChiSquare/BoundedNormal(above)/Beta(above)/JohnsonSB
# get no `scale_params` (W7 Q5 review item 3, D128 amended 2026-09-26):
# Cauchy has no finite mean at all; ChiSquare's one parameter is simultaneously
# its shape (X -> cX leaves the chi-square family entirely -- it becomes a
# scaled-chi-square/Gamma, not a chi-square of any df); InverseGaussian's
# "lambda" is documented to the user as a SHAPE (`hint="shape"` above, the
# classical Wald parameterisation), even though the sampler happens to pass it
# to scipy as a scale -- treating it as a ScaleParam here would rescale a
# parameter this codebase's own UI calls "shape", a maintenance trap not worth
# the one extra family; BoundedNormal/Beta/JohnsonSB tie their mean to a fixed
# BOUND (min/max), so scaling only the shape/location params without also
# moving the bounds does not scale the resulting bounded mean linearly, and
# moving bounds is outside what this table contract asks for. `tablegen.py`
# refuses a per-product `process_time` override at a station using any of
# these, naming the station/product/distribution.

_add("Cauchy", [_p("x0", 0.0, hint="location"), _p("gamma", 1.0, hint="scale, > 0")], _sample_cauchy, "special",
    "Heavy-tailed distribution for outlier-sensitive systems (rare); often returns negative values.\n"
    "Params: x0 (location), gamma (scale) > 0.",
    support_lower=None, mean=_mean_cauchy)

_add("InverseGaussian", [_p("mu", 1.0, hint="mean, > 0"), _p("lambda", 1.0, hint="shape, > 0")],
    _sample_inverse_gaussian, "continuous",
    "Used in detailed modeling of reaction/decision delays.\nParams: mu (mean) > 0, lambda (shape) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="InverseGaussian", mean=_mean_inverse_gaussian)

_add("Rayleigh", [_p("sigma", 1.0, hint="scale, > 0")], _sample_rayleigh, "continuous",
    "Models magnitudes like movement noise or energy.\nParam: sigma (scale) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="Rayleigh", mean=_mean_rayleigh,
    scale_params=[ScaleParam("sigma")])   # a genuine one-parameter scale family

_add("ChiSquare", [_p("df", 1.0, hint="degrees of freedom, >= 1")], _sample_chi_square, "special",
    "Used mostly for statistical diagnostics.\nParam: df (degrees of freedom) >= 1.",
    support_lower=0.0, mean=_mean_chi_square)

_add("LogLogistic", [_p("c", 1.0, hint="shape, > 0"), _p("scale", 1.0, hint="scale, > 0")],
    _sample_loglogistic, "continuous",
    "Heavy-tailed alternative to lognormal for skewed durations.\nParams: c (shape) > 0, scale > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="LogLogistic", mean=_mean_loglogistic,
    scale_params=[ScaleParam("scale")])   # c is shape, left literal

_add("JohnsonSU", [_p("a", 0.0, hint="shape"), _p("b", 1.0, hint="shape"), _p("loc", 0.0, hint="center"),
    _p("scale", 1.0, hint="scale, > 0")], _sample_johnson_su, "special",
    "Highly flexible for skewed and kurtotic data fitting; can return negative values.\n"
    "Params: a, b (shape), loc (center), scale > 0.",
    support_lower=None, mean=_mean_johnson_su,
    scale_params=[ScaleParam("loc"), ScaleParam("scale")])   # a, b are shape; unbounded, no bound coupling

_add("JohnsonSB", [_p("a1", 0.0, hint="shape"), _p("a2", 1.0, hint="shape"), _p("min", 0.0, hint="lower bound, < max"),
    _p("max", 1.0, hint="upper bound, > min")], _sample_johnson_sb, "special",
    "For skewed variables bounded in [min, max] (e.g., fill %).\nParams: a1, a2 (shape), min < max.",
    support_lower=0.0, mean=_mean_johnson_sb)

_add("PearsonTypeV", [_p("a", 1.0, hint="shape, > 0"), _p("scale", 1.0, hint="scale, > 0")], _sample_pearson_v,
    "continuous",
    "Used for inverse-gamma-shaped delays or failures.\nParams: a (shape) > 0, scale > 0.",
    delay=True, support_lower=0.0, mean=_mean_pearson_v,
    scale_params=[ScaleParam("scale")])   # a is shape, left literal

_add("PearsonTypeVI", [_p("a", 1.0, hint="shape, > 0"), _p("b", 1.0, hint="shape, > 0"), _p("scale", 1.0, hint="scale, > 0")],
    _sample_pearson_vi, "continuous",
    "Ratios of gammas (e.g., cost per service); always positive. Can model time if parameters "
    "are appropriate.\nParams: a, b > 0, scale > 0.",
    delay=True, support_lower=0.0, mean=_mean_pearson_vi,
    scale_params=[ScaleParam("scale")])   # a, b are shape, left literal

# No `scale_params` for the discrete counting families below (Bernoulli
# through Hypergeometric): a discrete distribution's own parameter(s) fix its
# integer-valued support directly, so `X -> cX` for a real factor c does not
# stay inside the family the way it does for a continuous scale family --
# there is no `p`/`lambda`/`r` to move that leaves "Bernoulli"/"Poisson"/etc.
# true and just rescales the mean.
_add("Bernoulli", [_p("p", 0.5, hint="success probability, in [0, 1]")], _sample_bernoulli, "special",
    "Yes/no decisions, e.g. reject/accept, pass/fail.\nParam: p (success probability) in [0, 1].",
    support_lower=0.0, mean=_mean_bernoulli)

_add("Binomial", [_p("n", 1.0, hint="number of trials, >= 0"), _p("p", 0.5, hint="success probability, in [0, 1]")],
    _sample_binomial, "special",
    "Fixed-number trials with success/failure outcomes.\nParams: n (trials) >= 0, p in [0, 1].",
    support_lower=0.0, fit="Binomial", mean=_mean_binomial)

_add("Geometric", [_p("p", 0.5, hint="success probability, in (0, 1]")], _sample_geometric, "special",
    "Models number of trials until first success.\nParam: p in (0, 1].",
    support_lower=0.0, fit="Geometric", mean=_mean_geometric)

_add("Poisson", [_p("lambda", 1.0, hint="average rate, > 0")], _sample_poisson, "discrete",
    "Number of arrivals/events in a fixed time window.\nParam: lambda (average rate) > 0.",
    arrival=True, delay=True, support_lower=0.0, fit="Poisson", mean=_mean_poisson)

_add("NegativeBinomial", [_p("r", 1.0, hint="target number of successes, >= 1"), _p("p", 0.5, hint="success probability, in (0, 1]")],
    _sample_negative_binomial, "special",
    "Trials until r successes, with success chance p.\nParams: r (target successes) >= 1, p in (0, 1].",
    support_lower=0.0, mean=_mean_negative_binomial)

_add("DiscreteUniform", [_p("low", 0.0, hint="minimum integer, <= high"), _p("high", 1.0, hint="maximum integer, >= low")],
    _sample_discrete_uniform, "special",
    "Equal chance of integers between bounds.\nParams: low (min) <= high (max), integers.",
    delay=True, support_lower=0.0, fit="DiscreteUniform", mean=_mean_discrete_uniform)

_add("Hypergeometric", [_p("N", 10.0, hint="population size"), _p("K", 5.0, hint="number of success states in the population"),
    _p("n", 3.0, hint="sample size")], _sample_hypergeometric, "special",
    "Sampling without replacement (e.g., quality inspection).\nParams: N (population) >= K (success states) >= n (sample) >= 0.",
    support_lower=0.0, mean=_mean_hypergeometric)

_add("EmpiricalContinuous", [_p("values", [0.0, 1.0], "list", hint="bin edges, length = len(probs) + 1"),
    _p("probs", [1.0], "list", hint="bin weights, summing to 1")],
    _sample_empirical_continuous, "continuous",
    "Piecewise-uniform continuous sampling from a histogram: picks a bin by weight, then draws "
    "uniformly within it (true continuous, not point resampling).\n"
    "Params: values (float list of bin edges, length = len(probs) + 1), probs (bin weights summing to 1).",
    arrival=True, delay=True, support_lower=0.0, mean=_mean_empirical_continuous)

_add("EmpiricalDiscrete", [_p("values", [0], "list", hint="candidate integer values"),
    _p("probs", [1.0], "list", hint="weight per value, summing to 1")], _sample_empirical_discrete,
    "discrete",
    "Samples discrete values (integers) from a list with associated probabilities.\n"
    "Params: values (int list), probs (weights summing to 1).",
    arrival=True, delay=True, support_lower=0.0, mean=_mean_empirical_discrete)

_add("FixedSchedule", [_p("schedule", [], "list", hint="ordered list of absolute arrival/event times")],
    _sample_fixed_schedule, "continuous",
    "Step-wise time schedule for entity generation or delays.\nParam: schedule (ordered list of times).",
    arrival=True, support_lower=0.0)
# No `mean`: driven by a cursor over explicit times, never a random draw
# (`_sample_fixed_schedule` reads no rng), so deterministic mode changes
# nothing here (StochasticDistribution.sample skips the substitution).

_add("CustomExpression", [_p("expression", "1", "expression", hint="model expression, e.g. '2 + EXPO(3)'")],
    _sample_custom_expression, "continuous",
    "Model expression (core/expressions.py grammar).\nParam: expression (string, e.g. '2 + EXPO(3)').",
    arrival=True, delay=True, support_lower=0.0)
# No `mean` either: the expression is evaluated normally in deterministic
# mode too, and its own RAND/EXPO/UNIF/NORM/TRIA/DISC calls already return
# their means then (core/expressions.py) -- so a plain expression with no
# random draw is already exact, and one that draws is already its mean.


# -- derived views (rebuilt whenever a plugin adds a distribution) ----------
ARRIVAL_DISTRIBUTIONS: list = []
DELAY_DISTRIBUTIONS: list = []
SPECIAL_PURPOSE_DISTRIBUTIONS: list = []
DISTRIBUTION_USE_CASES: dict = {}
DISTRIBUTION_PARAM_HINTS: dict = {}


def _rebuild_derived_tables() -> None:
    """Recompute every derived table from DISTRIBUTION_REGISTRY. Mutates the
    existing list/dict objects in place (`[:] = ...`, `.clear()` + `.update()`)
    rather than rebinding the names, so `from nagasim.core.distributions import
    ARRIVAL_DISTRIBUTIONS` elsewhere still sees updates after a plugin
    registers a new distribution (`nagasim.plugins.load_plugins`)."""
    ARRIVAL_DISTRIBUTIONS[:] = [n for n, s in DISTRIBUTION_REGISTRY.items() if s.arrival]
    DELAY_DISTRIBUTIONS[:] = [n for n, s in DISTRIBUTION_REGISTRY.items() if s.delay]
    SPECIAL_PURPOSE_DISTRIBUTIONS[:] = [n for n, s in DISTRIBUTION_REGISTRY.items() if s.category == "special"]
    DISTRIBUTION_USE_CASES.clear()
    DISTRIBUTION_USE_CASES.update({n: s.use_case for n, s in DISTRIBUTION_REGISTRY.items()})
    DISTRIBUTION_PARAM_HINTS.clear()
    DISTRIBUTION_PARAM_HINTS.update({n: s.use_case.rsplit("\n", 1)[-1] for n, s in DISTRIBUTION_REGISTRY.items()})


_rebuild_derived_tables()


#Used to model variables bounded in [a,b] with high skewness or kurtosis
class StochasticDistribution:
    """Sampler bound to a caller-supplied numpy Generator.

    Every sample() call takes the block's own rng (DEVLOG #25): no global
    random/np.random state, so runs are reproducible per seed and the streams
    of two blocks are independent. The dispatch itself (params resolution,
    scope building) lives here; the per-distribution arithmetic is one
    function per entry in DISTRIBUTION_REGISTRY (ROADMAP 2.4 A).
    """

    def __init__(self, dist_type="Constant", params=None, time_unit="base"):
        self.dist_type = dist_type
        self.params = params or {}
        self.time_unit = time_unit  # ROADMAP 1.5: "base" = the model's base unit, no conversion
        self._state = {}            # FixedSchedule's cursor lives here, never in params (DEVLOG #5)
        spec = DISTRIBUTION_REGISTRY.get(dist_type)
        # ROADMAP 2.5: computed once instead of re-scanning `params` on every
        # sample() call -- and lets BaseProcess.sample_duration skip building
        # an ExpressionScope entirely for the common case (Constant, Exponential,
        # ... with plain numeric params), confirmed hot by profiling
        # tools/bench.py's mm1_100k benchmark.
        expression_params = {p.name for p in spec.params if p.kind == "expression"} if spec else set()
        self._expression_params = expression_params
        # Bound once: `sample` calls it directly when there is nothing to
        # resolve, instead of a registry lookup on every draw.
        self._sampler = spec.sampler if spec else None
        self.needs_scope = bool(expression_params) or any(
            isinstance(v, str) for k, v in self.params.items() if k not in expression_params)

    def _scope(self, rng, scope):
        """The expression scope for this sample: the caller's, with the rng the
        caller sampled with filled in."""
        if scope is None:
            return ExpressionScope(rng=rng)
        return ExpressionScope(entity=scope.entity, ctx=scope.ctx, rng=scope.rng or rng,
                               locals=scope.locals)

    def _resolved_params(self, rng, scope):
        """Parameters, with any expression string evaluated to a value, e.g.
        `{"mean": "2 * entity.size"}`. Plain numbers stay untouched; a
        distribution's own "expression"-kind param (CustomExpression) is left
        alone here -- its sampler evaluates it directly with the full scope."""
        if not self.needs_scope:
            return self.params
        resolved = self._scope(rng, scope)
        return {k: (compile_expression(v).evaluate(resolved)
                    if isinstance(v, str) and k not in self._expression_params else v)
                for k, v in self.params.items()}

    def sample(self, rng, current=None, scope=None, deterministic=False):
        """*deterministic* (PHASE6 W4 P1, D119): substitute the distribution's
        own mean for a random draw, from the SAME resolved params the sampler
        would have used. Only for a distribution that registers a `mean`
        (every one except CustomExpression and FixedSchedule, which are
        already exact/unaffected in deterministic mode -- see their own
        registry comments); those two, and a plain call with
        `deterministic=False`, fall through to the ordinary sampler. Any other
        distribution without a mean (a plugin's, #207) raises: validation
        flags it at load time, this is the backstop."""
        if not deterministic and not self.needs_scope and self._sampler is not None:
            # Plain numeric params, an ordinary draw: exactly the last line below.
            return self._sampler(rng, self.params, current=current, scope=scope, state=self._state)
        spec = DISTRIBUTION_REGISTRY.get(self.dist_type)
        if spec is None:
            raise ValueError(f"Unknown distribution: {self.dist_type}")
        p = self._resolved_params(rng, scope)
        if deterministic and distribution_needs_finite_mean(self.dist_type):
            mean = distribution_mean(self.dist_type, p)
            if mean is None:
                raise ValueError(
                    f"distribution '{self.dist_type}' has no finite mean for params {p}; "
                    f"deterministic mode needs one (validate_model flags this before a run "
                    f"gets here)")
            return mean
        resolved_scope = self._scope(rng, scope) if self.needs_scope else scope
        return spec.sampler(rng, p, current=current, scope=resolved_scope, state=self._state)

    def to_dict(self):
        return {"dist_type": self.dist_type, "params": self.params}

    @staticmethod
    def from_dict(data):
        return StochasticDistribution(data["dist_type"], data.get("params", {}))

    def __repr__(self):
        return f"<Distribution: {self.dist_type}, params={self.params}>"


def get_available_distributions(block_type=None):
    """Return list of distributions for GUI dropdowns by block type."""
    if block_type == "CREATE":
        return ARRIVAL_DISTRIBUTIONS
    elif block_type in {"DELAY", "SEIZE", "RELEASE", "SIGNAL", "DISPOSE"}:
        return DELAY_DISTRIBUTIONS
    else:
        return ARRIVAL_DISTRIBUTIONS + DELAY_DISTRIBUTIONS + SPECIAL_PURPOSE_DISTRIBUTIONS


def get_distribution_params(dist_type):
    """Return a list of parameter names expected by a given distribution type."""
    spec = DISTRIBUTION_REGISTRY.get(dist_type)
    return [p.name for p in spec.params] if spec else []


# Deterministic by construction, so they need no `mean` (see their registry
# comments above); every other distribution does, a plugin's included (#207).
_EXACT_WITHOUT_MEAN = ("CustomExpression", "FixedSchedule")


def distribution_needs_finite_mean(dist_type: str) -> bool:
    """Whether *dist_type* is checked for a finite mean in deterministic mode
    (PHASE6 W4 P1, D119): every registered distribution except the two
    handled without a `mean()` at all -- CustomExpression and FixedSchedule
    (see their registry comments in this module). A plugin distribution that
    registers no `mean` IS checked, and fails (#207). Used by both
    `StochasticDistribution.sample` and `core/schema/`'s own validation, so the
    two agree on exactly which distributions are checked."""
    return dist_type in DISTRIBUTION_REGISTRY and dist_type not in _EXACT_WITHOUT_MEAN


def distribution_mean(dist_type: str, params: dict):
    """The closed-form mean for *dist_type*/*params*, or None when it has no
    finite one for these params (e.g. Cauchy, always; Exponential with
    rate <= 0). Only meaningful when `distribution_needs_finite_mean` is
    True -- for CustomExpression/FixedSchedule this always returns None, but
    that is never read as "no finite mean" (see the callers above)."""
    spec = DISTRIBUTION_REGISTRY.get(dist_type)
    if spec is None or spec.mean is None:
        return None
    try:
        m = spec.mean(params or {})
    except Exception:
        log.warning(f"{dist_type}: mean(params) raised for {params!r}; treated as no finite mean")
        return None
    return float(m) if m is not None and np.isfinite(m) else None


def distribution_scale_params(dist_type: str):
    """*dist_type*'s `ScaleParam`s (W7 Q5 review item 3, D128 amended
    2026-09-26), or None when it has none -- an unregistered name and one
    with no declared `scale_params` are the same "cannot rescale" answer to
    every caller (`tablegen.py` in particular)."""
    spec = DISTRIBUTION_REGISTRY.get(dist_type)
    return spec.scale_params if spec else None


def scale_distribution_params(dist_type: str, params: dict, factor_expr: str):
    """*params* (a distribution's own literal parameterisation, e.g. a
    station's table-given `dist_params`) rewritten so the resulting
    distribution's MEAN is its current mean times whatever *factor_expr*
    evaluates to at sample time, keeping the family and every non-scale
    (shape) parameter literal. *factor_expr* is a model expression string
    (e.g. `"entity.time_scale"`); each `ScaleParam` becomes an expression
    built from ITS OWN literal value (the param's own value in *params*, or
    its registry default) and *factor_expr* -- "linear": `value * factor`,
    "inverse": `value / factor` (a rate-type param, mean = k/rate), "log":
    `value + log(factor)` (a log-scale location param, mean =
    exp(mu + sigma^2/2)). Returns None when *dist_type* declares no
    `scale_params` -- the caller decides what that means (`tablegen.py`
    raises, naming the station/product/distribution)."""
    params = params or {}
    scale_params = distribution_scale_params(dist_type)
    if not scale_params:
        return None
    defaults = {p.name: p.default for p in DISTRIBUTION_REGISTRY[dist_type].params}
    out = {**defaults, **params}
    for sp in scale_params:
        value = out.get(sp.name, defaults.get(sp.name))
        if sp.kind == "inverse":
            out[sp.name] = f"{value} / {factor_expr}"
        elif sp.kind == "log":
            out[sp.name] = f"{value} + log({factor_expr})"
        else:
            out[sp.name] = f"{value} * {factor_expr}"
    return out


def rescale_distribution_params(dist_type: str, params: dict, factor: float) -> dict | None:
    """*params* re-expressed in a unit whose values are *factor* times larger
    (D161: a duration switched from minutes to seconds has factor 60) -- the
    numeric twin of `scale_distribution_params`: a "linear" ScaleParam
    multiplies, an "inverse" (rate) one divides, a "log" (log-location) one
    adds ln(factor), shape parameters stay; an expression parameter is wrapped.
    CustomExpression wraps its expression. None when the family declares no
    scale parameters: the caller keeps the values and says so."""
    params = dict(params or {})
    if factor == 1.0:
        return params
    if dist_type == "CustomExpression":
        return {**params, "expression": f"({params.get('expression', '1')}) * {factor!r}"}
    scale_params = distribution_scale_params(dist_type)
    if not scale_params:
        return None
    defaults = {p.name: p.default for p in DISTRIBUTION_REGISTRY[dist_type].params}
    for sp in scale_params:
        value = params.get(sp.name, defaults.get(sp.name))
        if isinstance(value, str):
            try:
                value = float(value)        # an old numeric fx field: a number from here on (#305)
            except ValueError:
                params[sp.name] = {"inverse": f"({value}) / {factor!r}",
                                   "log": f"({value}) + log({factor!r})"}.get(
                                       sp.kind, f"({value}) * {factor!r}")
                continue
        if sp.kind == "inverse":
            params[sp.name] = tidy(value / factor, value)
        elif sp.kind == "log":
            params[sp.name] = tidy(value + math.log(factor), value)
        else:
            params[sp.name] = tidy(value * factor, value)
    return params
