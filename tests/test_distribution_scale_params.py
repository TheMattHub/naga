"""nagasim.core.distributions: `ScaleParam`/`scale_distribution_params` (W7 Q5
review item 3, ARCHITECTURE 5.37/D128 amended 2026-09-26). The correctness
proof for "keep the family, rescale it to the target mean": for every
distribution that declares `scale_params`, rescaling its own literal params by
a factor and then taking the mean gives exactly `factor * original_mean`, for
a handful of factors and a couple of parameter sets per family."""
import pytest

from nagasim.core.distributions import (DISTRIBUTION_REGISTRY, distribution_mean,
                                        distribution_scale_params, scale_distribution_params)
from nagasim.core.expressions import ExpressionScope, compile_expression

_FACTORS = [0.5, 1.0, 2.5]

# One extra, non-default parameter set per scale-able family, so the property
# is checked at more than just the registry's own defaults.
_EXTRA_PARAMS = {
    "Constant": {"value": 4.0},
    "Uniform": {"low": 2.0, "high": 10.0},
    "Exponential": {"rate": 0.25},
    "Normal": {"mean": 5.0, "stddev": 2.0},
    "Lognormal": {"mu": 1.0, "sigma": 0.5},
    "Triangular": {"low": 1.0, "high": 9.0, "mode": 4.0},
    "Gamma": {"alpha": 2.0, "beta": 3.0},
    "Weibull": {"scale": 3.0, "shape": 2.0},
    "Erlang": {"k": 3.0, "rate": 0.5},
    "Rayleigh": {"sigma": 2.0},
    "LogLogistic": {"c": 2.0, "scale": 3.0},
    "JohnsonSU": {"a": 0.0, "b": 1.5, "loc": 2.0, "scale": 3.0},
    "PearsonTypeV": {"a": 2.0, "scale": 2.0},
    "PearsonTypeVI": {"a": 2.0, "b": 3.0, "scale": 2.0},
}


def _evaluate(params: dict) -> dict:
    """*params* (numbers and/or plain arithmetic expression strings, e.g.
    "2.0 * 3.0") with every string evaluated -- `scale_distribution_params`
    with a numeric-literal factor produces exactly this shape."""
    scope = ExpressionScope()
    return {k: (compile_expression(v).evaluate(scope) if isinstance(v, str) else v)
            for k, v in params.items()}


_SCALEABLE = [name for name, spec in DISTRIBUTION_REGISTRY.items() if spec.scale_params]

# LogLogistic/PearsonTypeV/PearsonTypeVI's registry-default SHAPE param sits
# exactly at their mean's convergence boundary (fisk needs c > 1, invgamma and
# betaprime need their last shape param > 1; the default is 1.0), so the
# default params have no finite mean at all -- a pre-existing property of
# those families, unrelated to scaling. Only `_EXTRA_PARAMS` (a shape clear of
# the boundary) is used for them below.
_NO_FINITE_MEAN_AT_DEFAULTS = {"LogLogistic", "PearsonTypeV", "PearsonTypeVI"}


def test_every_scaleable_family_has_a_finite_mean_at_its_own_defaults():
    # Sanity check on the fixtures above, not the feature: a family with no
    # mean at all would make the property below vacuous.
    for name in _SCALEABLE:
        if name in _NO_FINITE_MEAN_AT_DEFAULTS:
            continue
        params = {p.name: p.default for p in DISTRIBUTION_REGISTRY[name].params}
        assert distribution_mean(name, params) is not None, name


@pytest.mark.parametrize("name", _SCALEABLE)
def test_mean_of_scaled_params_equals_factor_times_original_mean(name):
    spec = DISTRIBUTION_REGISTRY[name]
    param_sets = [_EXTRA_PARAMS[name]] if name in _NO_FINITE_MEAN_AT_DEFAULTS else [
        {p.name: p.default for p in spec.params}, _EXTRA_PARAMS[name]]
    for params in param_sets:
        base_mean = distribution_mean(name, params)
        assert base_mean is not None, (name, params)
        for factor in _FACTORS:
            scaled = scale_distribution_params(name, params, repr(factor))
            resolved = _evaluate(scaled)
            scaled_mean = distribution_mean(name, resolved)
            assert scaled_mean == pytest.approx(factor * base_mean), (name, params, factor)


def test_scale_distribution_params_is_none_for_a_family_with_no_scale_params():
    assert distribution_scale_params("Beta") is None
    assert scale_distribution_params("Beta", {"min": 0.0, "max": 1.0}, "2.0") is None
    assert distribution_scale_params("CustomExpression") is None
    assert distribution_scale_params("NotARealDistribution") is None


def test_non_scale_shape_params_stay_literal():
    scaled = scale_distribution_params("Gamma", {"alpha": 3.0, "beta": 2.0}, "entity.time_scale")
    assert scaled["alpha"] == 3.0                       # shape: untouched
    assert scaled["beta"] == "2.0 * entity.time_scale"  # scale: rewritten
