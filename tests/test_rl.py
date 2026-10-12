"""SimEnv / evaluate_policy (ROADMAP 3.7 A items 3-4): reset/step shapes,
determinism, termination vs truncation, the three built-in rewards on a
hand-computable model, and the policy-ordering example."""
import json
from pathlib import Path

import numpy as np
import pytest

from nagasim.core import schema as S
from nagasim.core.control import CallablePolicy, RandomPolicy, ShortestQueuePolicy
from nagasim.rl import BUILTIN_REWARDS, Box, Discrete, SimEnv, _policy_blocks, evaluate_policy

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
EXAMPLE = json.loads((MODELS_DIR / "parallel_routing_policy.json").read_text())
CURRENT = S.CURRENT_VERSION


def _block(name, btype, params=None):
    return {"name": name, "description": name, "type": btype, "position": [0, 0],
            "params": params or {}}


def _hand_computable_layout():
    """CREATE (Constant gap 2.0, arrivals at t=0,2,4) -> Policy DECIDE (2
    branches) -> PROCESS on ServerA/ServerB (Constant service 1.0) -> DISPOSE.
    No randomness anywhere, so with every decision routed to ServerA (action
    0) the trajectory is fully hand-computable: each entity is served
    immediately (service 1.0 < inter-arrival 2.0), so WIP is 1 for exactly one
    minute after each arrival and 0 otherwise."""
    return {
        "schema_version": CURRENT,
        "blocks": [
            _block("create_1", "CREATE", {"distribution": "Constant", "dist_params": {"value": 2.0},
                                          "max_arrivals": 3}),
            _block("decide_1", "DECIDE", {"mode": "Policy", "probs": [], "conditions": [],
                                          "policy": "routing", "observation": [],
                                          "fallback": "first", "queue_exprs": []}),
            _block("process_a", "PROCESS", {"action": "Seize Delay Release",
                                            "distribution": "Constant", "dist_params": {"value": 1.0},
                                            "priority": 0, "quantity": 1, "resource_name": "ServerA"}),
            _block("process_b", "PROCESS", {"action": "Seize Delay Release",
                                            "distribution": "Constant", "dist_params": {"value": 1.0},
                                            "priority": 0, "quantity": 1, "resource_name": "ServerB"}),
            _block("dispose_1", "DISPOSE"),
        ],
        "links": [
            {"source": "create_1", "target": "decide_1"},
            {"source": "decide_1", "target": "process_a"},
            {"source": "decide_1", "target": "process_b"},
            {"source": "process_a", "target": "dispose_1"},
            {"source": "process_b", "target": "dispose_1"},
        ],
        "resources": [{"name": "ServerA", "capacity": 1, "discipline": "FIFO"},
                     {"name": "ServerB", "capacity": 1, "discipline": "FIFO"}],
        "resource_sets": [], "entities": [], "variables": [], "schedules": [], "failures": [],
        "scenarios": [], "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }


def _asset_layout():
    """The same decision, but inside a SUBMODEL definition placed TWICE -- the
    shape of every library asset (ARCHITECTURE §8 item 33, D72). The two
    instances share one policy name and have their own scoped block names."""
    cell = {"info": {}, "parameters": [], "annotations": [],
            "blocks": [_block("in", "SUB_ENTRY", {"port": "in"}),
                       _block("decide_1", "DECIDE",
                              {"mode": "Policy", "probs": [], "conditions": [],
                               "policy": "routing", "observation": ["NQ(\"ServerA\")"],
                               "fallback": "first", "queue_exprs": []}),
                       _block("process_a", "PROCESS",
                              {"action": "Seize Delay Release", "distribution": "Constant",
                               "dist_params": {"value": 1.0}, "priority": 0, "quantity": 1,
                               "resource_name": "ServerA"}),
                       _block("process_b", "PROCESS",
                              {"action": "Seize Delay Release", "distribution": "Constant",
                               "dist_params": {"value": 1.0}, "priority": 0, "quantity": 1,
                               "resource_name": "ServerB"}),
                       _block("out", "SUB_EXIT", {"port": "out"})],
            "links": [{"source": "in", "target": "decide_1"},
                      {"source": "decide_1", "target": "process_a"},
                      {"source": "decide_1", "target": "process_b"},
                      {"source": "process_a", "target": "out"},
                      {"source": "process_b", "target": "out"}]}
    blocks = [_block("create_1", "CREATE", {"distribution": "Constant",
                                            "dist_params": {"value": 2.0}, "max_arrivals": 3}),
              _block("cell_a", "SUBMODEL", {"definition": "Cell", "arguments": {}}),
              _block("cell_b", "SUBMODEL", {"definition": "Cell", "arguments": {}}),
              _block("dispose_1", "DISPOSE")]
    links = [{"source": "create_1", "target": "cell_a"},
             {"source": "cell_a", "target": "cell_b"},
             {"source": "cell_b", "target": "dispose_1"}]
    return {"schema_version": CURRENT, "blocks": blocks, "links": links,
            "submodels": {"Cell": cell},
            "resources": [{"name": "ServerA", "capacity": 1, "discipline": "FIFO"},
                          {"name": "ServerB", "capacity": 1, "discipline": "FIFO"}],
            "resource_sets": [], "entities": [], "variables": [], "schedules": [],
            "failures": [], "scenarios": [],
            "simulation": {"time_limit": None, "warmup": 0, "replications": 1}}


def test_a_policy_inside_an_asset_is_found_under_its_scoped_name():
    """ARCHITECTURE §8 item 33, closed in PHASE4 T7: SimEnv discovers policies
    on the FLATTENED model, so a Policy-mode DECIDE inside a submodel -- which
    is what a library asset is -- is a decision the environment can train."""
    env = SimEnv(_asset_layout(), policy="routing", reward="throughput", horizon=20.0)
    assert env.action_space.n == 2
    assert env.observation_space.shape == (1,)
    assert sorted(b["name"] for b in _policy_blocks(env.layout, "routing")) == \
        ["cell_a/decide_1", "cell_b/decide_1"]

    env.reset(seed=0)
    steps = 0
    while steps < 20:
        _obs, _reward, terminated, truncated, _info = env.step(0)
        steps += 1
        if terminated or truncated:
            break
    assert steps > 1                      # both instances really did ask


# -- shapes and spaces --------------------------------------------------------

def test_spaces_and_reset_step_shapes():
    env = SimEnv(EXAMPLE, policy="routing", reward="neg_wip", horizon=50.0)
    assert isinstance(env.action_space, Discrete) and env.action_space.n == 2
    assert isinstance(env.observation_space, Box) and env.observation_space.shape == (4,)

    obs, info = env.reset(seed=0)
    assert obs.shape == env.observation_space.shape
    assert "t" in info
    obs, reward, terminated, truncated, info = env.step(0)
    assert obs.shape == env.observation_space.shape
    assert isinstance(reward, float)
    assert isinstance(terminated, bool) and isinstance(truncated, bool)


# -- determinism --------------------------------------------------------------

def test_determinism_same_seed_and_actions_gives_identical_trajectory():
    def run():
        env = SimEnv(EXAMPLE, policy="routing", reward="throughput", horizon=80.0)
        obs, _info = env.reset(seed=3)
        trace = [tuple(obs)]
        terminated = truncated = False
        i = 0
        while not (terminated or truncated) and i < 40:
            obs, reward, terminated, truncated, _info = env.step(i % 2)
            trace.append((tuple(obs), reward))
            i += 1
        return trace

    assert run() == run()


# -- termination vs truncation, and the three built-in rewards ---------------

@pytest.mark.parametrize("reward, expected_total", [
    ("neg_wip", -3.0),
    ("throughput", 3.0),
    ("neg_time_in_system", -3.0),
])
def test_builtin_rewards_hand_computable(reward, expected_total):
    """See `_hand_computable_layout` docstring: always routing to ServerA (an
    idle single server, service 1.0 < inter-arrival 2.0) makes every window's
    WIP-time-integral, completion count and time-in-system exactly 1."""
    env = SimEnv(_hand_computable_layout(), policy="routing", reward=reward, horizon=100.0)
    env.reset(seed=0)
    total, terminated = 0.0, False
    for _ in range(3):
        _obs, r, terminated, truncated, _info = env.step(0)
        total += r
    assert terminated is True
    assert total == pytest.approx(expected_total)


@pytest.mark.parametrize("horizon, expected_steps, expect_terminated, expect_truncated", [
    (3.5, 2, False, True),      # the 3rd arrival (t=4) is past the horizon
    (1000.0, 3, True, False),   # the model drains (3 arrivals, all served) well inside it
])
def test_termination_vs_truncation(horizon, expected_steps, expect_terminated, expect_truncated):
    env = SimEnv(_hand_computable_layout(), policy="routing", reward="throughput", horizon=horizon)
    env.reset(seed=0)
    terminated = truncated = False
    steps = 0
    while not (terminated or truncated):
        _obs, _r, terminated, truncated, info = env.step(0)
        steps += 1
    assert (terminated, truncated) == (expect_terminated, expect_truncated)
    assert steps == expected_steps
    assert "kpi_digest" in info


# -- evaluate_policy and the ordering example (item 4) -----------------------

def test_shortest_beats_random_beats_always_slow():
    """ROADMAP 3.7 A item 4: over a few seeded episodes on the bundled example,
    ShortestQueuePolicy has a higher (less negative) mean time-in-system return
    than RandomPolicy, which beats always picking the slow server."""
    kwargs = dict(policy="routing", reward="neg_time_in_system", horizon=150.0)
    shortest = evaluate_policy(EXAMPLE, ShortestQueuePolicy(), episodes=4, seed=0, **kwargs)
    random_ = evaluate_policy(EXAMPLE, RandomPolicy(np.random.default_rng(0)), episodes=4, seed=0,
                              **kwargs)
    always_slow = evaluate_policy(EXAMPLE, CallablePolicy(lambda req: req.n_actions - 1),
                                  episodes=4, seed=0, **kwargs)
    assert shortest["mean"] > random_["mean"] > always_slow["mean"]


# -- optional dependency: gymnasium conversion skips cleanly -----------------

def test_as_gymnasium_reports_a_clear_error_when_gymnasium_is_missing(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "gymnasium":
            raise ImportError("no module named gymnasium")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    space = Discrete(3)
    with pytest.raises(ImportError):
        space.as_gymnasium()
