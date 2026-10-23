"""Phase 11 E, the RL kit (D723): gymnasium env, `others=`, data-only policy files, SB3 recipe."""
import json
from pathlib import Path

import numpy as np
import pytest

from nagasim.core.control import ShortestQueuePolicy
from nagasim.rl import SimEnv, evaluate_policy

from test_rl import EXAMPLE, MODELS_DIR, _block, _hand_computable_layout


def _two_policy_layout():
    """The hand layout with a second Policy DECIDE ("second", 2 branches) after the servers."""
    layout = _hand_computable_layout()
    layout["blocks"] = [b for b in layout["blocks"] if b["name"] != "dispose_1"] + [
        _block("decide_2", "DECIDE", {"mode": "Policy", "probs": [], "conditions": [],
                                      "policy": "second", "observation": [],
                                      "fallback": "first", "queue_exprs": []}),
        _block("dispose_1", "DISPOSE"), _block("dispose_2", "DISPOSE")]
    layout["links"] = [lk for lk in layout["links"] if lk["target"] != "dispose_1"] + [
        {"source": "process_a", "target": "decide_2"}, {"source": "process_b", "target": "decide_2"},
        {"source": "decide_2", "target": "dispose_1"}, {"source": "decide_2", "target": "dispose_2"}]
    return layout


def test_as_gymnasium_env_passes_check_env():
    pytest.importorskip("gymnasium")
    from gymnasium.utils.env_checker import check_env
    env = SimEnv(EXAMPLE, policy="routing", horizon=50.0).as_gymnasium_env()
    check_env(env)
    env.reset(seed=3)
    obs, _r, _te, _tr, _i = env.step(env.action_space.sample())
    assert obs.shape == env.observation_space.shape


def test_others_drive_the_second_policy_and_default_to_shortest_queue():
    seen = []

    def second(request):
        seen.append(request.policy)
        return 1

    env = SimEnv(_two_policy_layout(), policy="routing", horizon=100.0, others={"second": second})
    env.reset(seed=0)
    done = False
    while not done:
        _o, _r, term, trunc, _i = env.step(0)
        done = term or trunc
    assert seen == ["second"] * 3
    # a block name works as the key, and with no `others` the default is shortest queue
    SimEnv(_two_policy_layout(), policy="routing", horizon=100.0, others={"decide_2": "random"}).reset(seed=0)
    default = SimEnv(_two_policy_layout(), policy="routing", horizon=100.0)
    default.reset(seed=0)
    assert isinstance(default.ctx.controllers["second"], ShortestQueuePolicy)


def test_others_refuses_unknown_and_trained_policy():
    with pytest.raises(ValueError, match="nope"):
        SimEnv(_two_policy_layout(), policy="routing", horizon=10.0, others={"nope": "random"})
    with pytest.raises(ValueError, match="being trained"):
        SimEnv(_two_policy_layout(), policy="routing", horizon=10.0, others={"routing": "random"})


def _write(tmp_path, data, raw=None):
    p = tmp_path / "p.json"
    p.write_text(raw if raw is not None else json.dumps(data))
    return p


def test_policy_files_table_and_linear_evaluate(tmp_path):
    n_obs = len(next(b for b in EXAMPLE["blocks"] if b["type"] == "DECIDE")["params"]["observation"])
    assert n_obs == 4
    table = _write(tmp_path, {"kind": "table", "edges": [[1], [1], [], []], "actions": [0, 1, 0, 0]})
    out = evaluate_policy(EXAMPLE, table, 2, 0, policy="routing", horizon=50.0)
    assert out["episodes"] == 2 and np.isfinite(out["mean"])
    # negative identity weights pick the smaller observation: the built-in shortest queue
    linear = _write(tmp_path, {"kind": "linear", "weights": [[-1.0, 0.0, 0.0, 0.0], [0.0, -1.0, 0.0, 0.0]]})
    assert (evaluate_policy(EXAMPLE, linear, 2, 0, policy="routing", horizon=50.0)["returns"]
            == evaluate_policy(EXAMPLE, "shortest", 2, 0, policy="routing", horizon=50.0)["returns"])


@pytest.mark.parametrize("data, raw, message", [
    ({"kind": "pickle"}, None, "unknown policy kind 'pickle'"),
    (None, '{"kind": "linear", "weights": [[NaN, 1.0]]}', "NaN"),
    ({"kind": "linear", "weights": [[1.0, 2.0], [1.0]]}, None, "same length"),
    ({"kind": "table", "edges": [[1, 2]], "actions": [0, 1]}, None, "cells"),
    ({"kind": "table", "edges": [list(range(64))] * 3, "actions": [0]}, None, "cells"),
    ({"kind": "linear", "weights": [[1.0] * 300]}, None, "limit"),
    ({"kind": "table", "edges": [[2, 1]], "actions": [0, 0, 0]}, None, "ascending"),
])
def test_policy_files_are_refused_by_name(tmp_path, data, raw, message):
    from nagasim.policyfile import PolicyFileError, load_policy_file
    with pytest.raises(PolicyFileError, match=message):
        load_policy_file(_write(tmp_path, data, raw))


def test_policy_file_too_large_and_wrong_shape(tmp_path):
    from nagasim.policyfile import MAX_BYTES, PolicyFileError, load_policy_file
    with pytest.raises(PolicyFileError, match="bytes"):
        load_policy_file(_write(tmp_path, None, " " * (MAX_BYTES + 1)))
    wrong = _write(tmp_path, {"kind": "linear", "weights": [[1.0], [2.0]]})   # 1 column, 4 observations
    with pytest.raises(PolicyFileError, match="columns"):
        evaluate_policy(EXAMPLE, wrong, 1, 0, policy="routing", horizon=20.0)


def test_cli_policy_file(tmp_path, capsys):
    from nagasim.rl import main
    path = _write(tmp_path, {"kind": "linear", "weights": [[-1.0, 0.0, 0.0, 0.0], [0.0, -1.0, 0.0, 0.0]]})
    assert main([str(MODELS_DIR / "parallel_routing_policy.json"), "--policy", "routing",
                 "--policy-file", str(path), "--episodes", "2", "--horizon", "50"]) == 0
    assert "p.json: mean return" in capsys.readouterr().out


@pytest.mark.slow
def test_sb3_recipe_runs_a_few_steps():
    """The code of the docs recipe, a few steps (skipped without Stable-Baselines3)."""
    pytest.importorskip("stable_baselines3")
    from stable_baselines3 import PPO
    env = SimEnv(EXAMPLE, policy="routing", horizon=50.0).as_gymnasium_env()
    model = PPO("MlpPolicy", env, n_steps=64, batch_size=32, seed=0, device="cpu")
    model.learn(total_timesteps=64)
    obs, _ = env.reset(seed=0)
    action, _ = model.predict(obs, deterministic=True)
    assert env.action_space.contains(int(action))
