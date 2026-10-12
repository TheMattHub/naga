"""Policy-mode DECIDE (ROADMAP 3.7 A item 1-2): validation, fallbacks, and the
engine seam in nagasim/core/control.py."""
import numpy as np
import pytest

from nagasim import api
from nagasim.core import schema as S
from nagasim.core.control import CallablePolicy, DecisionRequest

CURRENT = S.CURRENT_VERSION


def _model(blocks, links, **overrides):
    base = {"schema_version": CURRENT, "blocks": blocks, "links": links,
            "resources": [], "resource_sets": [], "entities": [], "variables": [],
            "schedules": [], "failures": [], "scenarios": [],
            "simulation": {"time_limit": 100, "warmup": 0, "replications": 1}}
    base.update(overrides)
    return base


def _block(name, btype, params=None):
    return {"name": name, "description": name, "type": btype, "position": [0, 0],
            "params": params or {}}


def _two_branch_policy_model(policy_params):
    """CREATE -> DECIDE (Policy, 2 branches) -> DISPOSE (x2)."""
    decide = _block("decide_1", "DECIDE", {"mode": "Policy", "probs": [], "conditions": [],
                                           **policy_params})
    return _model(
        [_block("create_1", "CREATE", {"distribution": "Constant", "dist_params": {"value": 2.0},
                                       "max_arrivals": 3}),
         decide, _block("dispose_a", "DISPOSE"), _block("dispose_b", "DISPOSE")],
        [{"source": "create_1", "target": "decide_1"},
         {"source": "decide_1", "target": "dispose_a"},
         {"source": "decide_1", "target": "dispose_b"}])


# -- validation table --------------------------------------------------------

@pytest.mark.parametrize("policy_params, expected_code", [
    ({"policy": "", "observation": [], "fallback": "first", "queue_exprs": []},
     "E_DECIDE_POLICY_NAME"),
    ({"policy": "p", "observation": [], "fallback": "nope", "queue_exprs": []},
     "E_DECIDE_FALLBACK"),
    ({"policy": "p", "observation": [], "fallback": "shortest", "queue_exprs": ["1"]},
     "E_DECIDE_QUEUE_EXPRS"),
    ({"policy": "p", "observation": ["entity.priority"], "fallback": "shortest",
      "queue_exprs": ["1", "2"]}, None),                 # valid config: no policy-related problem
])
def test_policy_mode_validation_problems(policy_params, expected_code):
    layout = _two_branch_policy_model(policy_params)
    codes = {p.code for p in S.validate_model_structured(layout)}
    policy_codes = {"E_DECIDE_POLICY_NAME", "E_DECIDE_FALLBACK", "E_DECIDE_QUEUE_EXPRS"}
    if expected_code is None:
        assert not codes & policy_codes
    else:
        assert expected_code in codes


# -- fallback behaviours (no controller attached) ----------------------------

@pytest.mark.parametrize("fallback, extra_params", [
    ("first", {}),                                  # every entity takes branch 0
    ("shortest", {"queue_exprs": ["0", "1"]}),       # branch 0's queue expr is always smaller
    ("random", {}),
])
def test_policy_fallback_deterministic_per_seed(fallback, extra_params):
    layout = _two_branch_policy_model({"policy": "p", "observation": [], "fallback": fallback,
                                       **extra_params})
    r1 = api.run_model(layout, replications=2, seed=5, run_length=100)
    r2 = api.run_model(layout, replications=2, seed=5, run_length=100)
    assert r1["system"].equals(r2["system"])


# -- controller wiring --------------------------------------------------------

def test_controller_receives_observation_and_action_count():
    requests = []

    def record(request: DecisionRequest) -> int:
        requests.append(request)
        return 0

    layout = _two_branch_policy_model({"policy": "routing",
                                       "observation": ["entity.creation_time", "2"],
                                       "fallback": "first"})
    api.run_model(layout, replications=1, run_length=100,
                  controllers={"routing": CallablePolicy(record)})
    assert len(requests) == 3   # one per arrival (max_arrivals=3)
    assert [req.observation[0] for req in requests] == [0.0, 2.0, 4.0]   # arrivals at t=0,2,4
    for req in requests:
        assert req.policy == "routing"
        assert req.n_actions == 2
        assert req.observation[1] == 2.0
        assert req.block == "decide_1"
    # An out-of-range action fails loud rather than silently misrouting.
    with pytest.raises(Exception):
        api.run_model(layout, replications=1, run_length=100,
                      controllers={"routing": CallablePolicy(lambda req: 5)})


def test_unpicklable_controller_with_workers_raises_a_clear_error():
    layout = _two_branch_policy_model({"policy": "routing", "observation": [], "fallback": "first"})
    unpicklable = CallablePolicy(lambda req: req.n_actions - 1)
    unpicklable.fn = (lambda req: 0)   # a lambda never pickles
    with pytest.raises(ValueError, match="not picklable"):
        api.run_model(layout, replications=4, run_length=100, workers=2,
                      controllers={"routing": unpicklable})


# -- inline equivalence (RNG consumption identical: neither mode draws) ------

def test_policy_inline_controller_matches_condition_mode_exactly():
    """A CallablePolicy that always returns 0 must match a Condition-mode
    DECIDE with a constant true condition: neither draws from the block's own
    rng, so this is an exact comparison, not just a statistical one."""
    policy_layout = _two_branch_policy_model({"policy": "p", "observation": [], "fallback": "first"})
    condition_layout = _model(
        [_block("create_1", "CREATE", {"distribution": "Constant", "dist_params": {"value": 2.0},
                                       "max_arrivals": 3}),
         _block("decide_1", "DECIDE", {"mode": "Condition", "conditions": ["True"], "probs": []}),
         _block("dispose_a", "DISPOSE"), _block("dispose_b", "DISPOSE")],
        [{"source": "create_1", "target": "decide_1"},
         {"source": "decide_1", "target": "dispose_a"},
         {"source": "decide_1", "target": "dispose_b"}])

    r_policy = api.run_model(policy_layout, replications=3, seed=1, run_length=100,
                             controllers={"p": CallablePolicy(lambda req: 0)})
    r_condition = api.run_model(condition_layout, replications=3, seed=1, run_length=100)

    assert r_policy["system"]["Value"].tolist() == r_condition["system"]["Value"].tolist()
