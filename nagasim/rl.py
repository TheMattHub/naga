"""Step-wise RL environment over a Policy-mode DECIDE (ROADMAP 3.7 A item 3).

No gymnasium import at module import time: `Discrete`/`Box` below are
lightweight stand-ins exposing the same `shape`/`n`/`sample()` surface, and
convert to real `gymnasium.spaces` only in `as_gymnasium()`, which imports
gymnasium lazily and only there -- so this module, and every fast test that
touches it, works with no gymnasium installed at all.

    from nagasim.rl import SimEnv
    from nagasim.core.control import ShortestQueuePolicy

    env = SimEnv("models/parallel_routing_policy.json", policy="routing",
                 reward="neg_wip", horizon=500.0)
    obs, info = env.reset(seed=0)
    action = ShortestQueuePolicy().decide(env.last_request)
    obs, reward, terminated, truncated, info = env.step(action)

Pause/resume mechanics: a `PausingController` is attached to the model's
`ctx.controllers[policy]`; the Policy-mode DECIDE block, on seeing that class,
does not call `.decide()` -- it registers a (request, simpy event) pair and
`yield`s on the event, which really suspends that simpy process. `SimEnv.step`
resolves the pending event with the caller's action, then drives `env.step()`
(the simpy environment, not this wrapper) until either another request is
pending, the event queue drains (`terminated`), or the next event would cross
`horizon` (`truncated`) -- see `_advance` below.
"""
from __future__ import annotations

import numpy as np

from nagasim import api
from nagasim.core.context import build_context
from nagasim.core.control import (
    CallablePolicy, Controller, PausingController, RandomPolicy, RoundRobinPolicy,
    ShortestQueuePolicy,
)
from nagasim.core.expressions import ExpressionScope, compile_expression
from nagasim.core.outputs import calc_kpis
from nagasim.core.schema import ModelValidationError, prepare, fatal_problems, format_problems
from nagasim.core.sim_runner import prepare_model
from nagasim.core.stats import ci95

__all__ = ["Space", "Discrete", "Box", "SimEnv", "evaluate_policy", "BUILTIN_REWARDS", "main"]


def _import_gymnasium():
    """Lazy gymnasium import for `Space.as_gymnasium()` (both subclasses),
    with an install hint instead of a bare ModuleNotFoundError."""
    try:
        import gymnasium
    except ImportError as exc:
        raise ImportError("as_gymnasium() needs the optional gymnasium package: "
                          "pip install \"nagasim[ml]\" (or: pip install gymnasium)") from exc
    return gymnasium

# "neg_wip": minus the WIP time-integral (entity-time-units in system) since
#   the previous decision -- a running "area under the WIP curve" penalty.
# "throughput": entities completed (DISPOSEd) since the previous decision.
# "neg_time_in_system": minus the sum of (deletion_time - creation_time) over
#   entities completed since the previous decision.
BUILTIN_REWARDS = ("neg_wip", "throughput", "neg_time_in_system")

_BUILTIN_CONTROLLERS = {"random": RandomPolicy, "shortest": ShortestQueuePolicy,
                        "roundrobin": RoundRobinPolicy}


class Space:
    """Base of the two gymnasium-shaped spaces below."""

    def as_gymnasium(self):
        raise NotImplementedError


class Discrete(Space):
    def __init__(self, n: int):
        self.n = int(n)
        self.shape: tuple = ()

    def sample(self, rng=None):
        rng = rng if rng is not None else np.random.default_rng()
        return int(rng.integers(0, self.n))

    def as_gymnasium(self):
        gymnasium = _import_gymnasium()
        return gymnasium.spaces.Discrete(self.n)

    def __repr__(self):
        return f"Discrete({self.n})"


class Box(Space):
    def __init__(self, low, high, shape):
        self.shape = tuple(shape)
        self.low = np.full(self.shape, low, dtype=float)
        self.high = np.full(self.shape, high, dtype=float)

    def sample(self, rng=None):
        rng = rng if rng is not None else np.random.default_rng()
        finite = np.isfinite(self.low) & np.isfinite(self.high)
        out = rng.standard_normal(self.shape)
        out[finite] = rng.uniform(self.low[finite], self.high[finite])
        return out

    def as_gymnasium(self):
        gymnasium = _import_gymnasium()
        return gymnasium.spaces.Box(low=self.low, high=self.high, shape=self.shape, dtype=np.float64)

    def __repr__(self):
        return f"Box(shape={self.shape})"


def _policy_blocks(layout: dict, policy: str) -> list[dict]:
    return [b for b in layout.get("blocks", [])
            if b.get("type", "").upper() == "DECIDE"
            and b.get("params", {}).get("mode") == "Policy"
            and b.get("params", {}).get("policy") == policy]


def _out_count(layout: dict, block_name: str) -> int:
    return sum(1 for link in layout.get("links", []) if link.get("source") == block_name)


class SimEnv:
    """One RL episode = one replication of *layout*, paused at every decision
    of *policy*. Gymnasium-shaped but does not require gymnasium installed.

    *reward* is one of `BUILTIN_REWARDS` or an expression string (see
    `core/expressions.py`) evaluated with no `entity` in scope -- so it may
    read model variables and NQ/NR/MR/NS, but not entity attributes or the
    random-sampling functions (there is no rng bound to a "reward" -- only to
    a block). *observation_extra*, when given, is a list of such expressions,
    evaluated the same way and appended after the DECIDE block's own
    `observation` values.
    """

    def __init__(self, layout, *, policy: str, reward: str = "neg_wip", horizon: float,
                observation_extra: list[str] | None = None):
        # One prepare pass (read/migrate/validate/flatten/expand, Wave A2): every
        # episode's reset() reuses this same flat layout (prepared=True) rather
        # than re-migrating/re-validating/re-flattening/re-expanding it, which
        # would otherwise repeat for every episode of an RL training run.
        self.layout, problems = prepare(layout)
        fatal = fatal_problems(problems)   # by severity, not the message text (#247)
        if fatal:
            raise ModelValidationError(format_problems(fatal))
        # Policies are discovered on the FLATTENED model (PHASE4 T7): a
        # Policy-mode DECIDE inside a submodel definition -- which is what every
        # library asset is -- is a real decision of the run, and its block name
        # is the scoped `<instance>/<block>` the engine will use. A model with
        # no submodels is returned unchanged by `flatten`, identity and all.
        blocks = _policy_blocks(self.layout, policy)
        if not blocks:
            raise ValueError(f"no Policy-mode DECIDE block in this model uses policy '{policy}'")
        first = blocks[0]
        n_actions = _out_count(self.layout, first["name"])
        if n_actions < 1:
            raise ValueError(f"DECIDE '{first['name']}' (policy '{policy}') has no outgoing link")
        obs_dim = len(first["params"].get("observation", [])) + len(observation_extra or [])

        self.policy_name = policy
        self.reward_spec = str(reward)
        self._reward_expr = None if self.reward_spec in BUILTIN_REWARDS else compile_expression(self.reward_spec)
        self.horizon = float(horizon)
        self.observation_extra = [compile_expression(e) for e in (observation_extra or [])]
        self.action_space = Discrete(n_actions)
        self.observation_space = Box(-np.inf, np.inf, (obs_dim,))

        self.seed: int = 0
        self.env = None
        self.ctx = None
        self.last_request = None
        self._controller: PausingController | None = None
        self._episode_completions = 0
        self._done = False

    # -- bookkeeping between decisions --------------------------------------
    def _reset_window(self, t: float) -> None:
        self._wip_count = len(self.ctx.live_entities) if self.ctx is not None else 0
        self._wip_area = 0.0
        self._completions = 0
        self._time_in_system_sum = 0.0
        self._last_t = t

    def _flush_wip_area(self, t: float) -> None:
        self._wip_area += self._wip_count * (t - self._last_t)
        self._last_t = t

    def _on_event(self, name, **payload) -> None:
        if name == "entity_created":
            self._flush_wip_area(payload["t"])
            self._wip_count += 1
        elif name == "entity_disposed":
            self._flush_wip_area(payload["t"])
            self._wip_count -= 1
            self._completions += 1
            self._episode_completions += 1
            entity = payload["entity"]
            self._time_in_system_sum += float(entity.deletion_time - entity.creation_time)

    def _reward(self) -> float:
        self._flush_wip_area(self.env.now)
        if self.reward_spec == "neg_wip":
            value = -self._wip_area
        elif self.reward_spec == "throughput":
            value = float(self._completions)
        elif self.reward_spec == "neg_time_in_system":
            value = -self._time_in_system_sum
        else:
            value = float(self._reward_expr.evaluate(ExpressionScope(ctx=self.ctx)))
        self._reset_window(self.env.now)
        return value

    def _observation(self, request) -> np.ndarray:
        extra = [e.evaluate(ExpressionScope(ctx=self.ctx)) for e in self.observation_extra]
        return np.asarray([*request.observation, *extra], dtype=float)

    def _info(self, terminal: bool = False) -> dict:
        info = {"t": self.env.now, "completions": self._episode_completions}
        if terminal:
            info["kpi_digest"] = calc_kpis(self.env, warmup=0.0, run_id=self.seed)
        return info

    # -- gymnasium-shaped API -------------------------------------------------
    def reset(self, seed: int | None = None, options=None):
        """Start a fresh episode; returns (observation, info). No reward yet --
        this is the FIRST decision, nothing happened before it."""
        if seed is not None:
            self.seed = int(seed)
        self.ctx = build_context(self.seed)
        self.env = self.ctx.env
        self.env.ctx = self.ctx
        self._controller = PausingController()
        self.ctx.controllers = {self.policy_name: self._controller}
        self.ctx.bus.subscribe(self._on_event)
        self._episode_completions = 0
        self._done = False
        self._reset_window(0.0)
        self._model = prepare_model(self.env, self.layout, prepared=True)
        if self.ctx.level_system is not None:       # PHASE4 D1
            self.ctx.level_system.start(self.horizon)
        if self.ctx.agent_system is not None:       # PHASE5 AB-1
            self.ctx.agent_system.start(self.horizon)
        for start in self._model.start_blocks():
            start.self_run()
        obs, terminated, truncated = self._advance()
        self._done = terminated or truncated
        self.last_request = self._controller.pending[0] if self._controller.pending else None
        return obs, self._info(terminal=self._done)

    def step(self, action):
        """Resolve the pending decision with *action*, run until the next one
        (or the episode ends), and return (obs, reward, terminated, truncated, info)."""
        if self._done:
            raise RuntimeError("step() called after the episode ended; call reset() first")
        if self._controller.pending is None:
            raise RuntimeError("step() called with no pending decision request")
        self._controller.resolve(action)
        obs, terminated, truncated = self._advance()
        reward = self._reward()
        self._done = terminated or truncated
        self.last_request = self._controller.pending[0] if self._controller.pending else None
        return obs, reward, terminated, truncated, self._info(terminal=self._done)

    def _advance(self):
        """Step the simpy env until a decision is pending, the queue drains
        (terminated), or the next event would cross `horizon` (truncated)."""
        env = self.env
        while True:
            if self._controller.pending is not None:
                if len(self._controller.requests) > 1:     # one env step made two
                    raise RuntimeError(f"policy '{self.policy_name}' already has a pending "
                                       f"decision request; SimEnv.step() must resolve one "
                                       f"before the next can arrive")
                return self._observation(self._controller.pending[0]), False, False
            if env.peek() == float("inf"):
                self.ctx.sim_end_time = env.now
                return self._terminal_observation(), True, False
            if env.peek() > self.horizon:
                self.ctx.sim_end_time = self.horizon
                return self._terminal_observation(), False, True
            env.step()

    def _terminal_observation(self) -> np.ndarray:
        return np.zeros(self.observation_space.shape, dtype=float)


# ---------------------------------------------------------------- evaluation
def _resolve_controller(policy_or_controller, seed: int):
    """A Controller instance, a bare callable, or one of the built-in names
    ("random"/"shortest"/"roundrobin") the CLI/agent tool take by string."""
    if isinstance(policy_or_controller, Controller):
        return policy_or_controller
    if isinstance(policy_or_controller, str):
        cls = _BUILTIN_CONTROLLERS.get(policy_or_controller)
        if cls is None:
            raise ValueError(f"unknown controller '{policy_or_controller}' "
                             f"(expected one of {', '.join(_BUILTIN_CONTROLLERS)})")
        return cls(np.random.default_rng(seed)) if cls is RandomPolicy else cls()
    if callable(policy_or_controller):
        return CallablePolicy(policy_or_controller)
    raise TypeError(f"not a controller: {policy_or_controller!r}")


def evaluate_policy(layout, policy_or_controller, episodes: int, seed: int = 0, *,
                    policy: str, reward: str = "neg_wip", horizon: float = 1000.0,
                    observation_extra: list[str] | None = None) -> dict:
    """Run *episodes* episodes of `SimEnv(layout, policy=policy, ...)`, driving
    the pending decision at each step with *policy_or_controller*.

    *policy_or_controller* is a `Controller` (e.g. `ShortestQueuePolicy()`), a
    bare `fn(request) -> int`, or one of "random"/"shortest"/"roundrobin".
    Episode *i* uses `seed + i`.

    Returns {"mean", "lb", "ub", "episodes", "returns": [one per episode]} --
    the mean episode return with its 95% CI over episodes.
    """
    controller = _resolve_controller(policy_or_controller, seed)
    env = SimEnv(layout, policy=policy, reward=reward, horizon=horizon,
                observation_extra=observation_extra)
    returns = []
    for i in range(int(episodes)):
        env.reset(seed=seed + i)
        total = 0.0
        terminated = truncated = False
        while not (terminated or truncated):
            action = controller.decide(env.last_request)
            _obs, r, terminated, truncated, _info = env.step(action)
            total += r
        returns.append(total)
    stat = ci95(np.asarray(returns, dtype=float))
    return {"mean": stat["mean"], "lb": stat["lb"], "ub": stat["ub"],
            "episodes": int(episodes), "returns": returns}


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    """`naga policy-eval model.json --policy NAME --controller shortest|random|roundrobin
    --episodes N --horizon T`; prints the mean return and KPI digest of the last episode."""
    import argparse

    from nagasim._cli import add_model_argument, setup_cli

    setup_cli(level=None)

    parser = argparse.ArgumentParser(prog="naga policy-eval",
                                     description="Evaluate a controller against a Policy-mode DECIDE.")
    add_model_argument(parser)
    parser.add_argument("--policy", required=True, help="decision-point name")
    parser.add_argument("--controller", required=True, choices=sorted(_BUILTIN_CONTROLLERS),
                        help="built-in controller to evaluate")
    parser.add_argument("--reward", default="neg_wip", choices=list(BUILTIN_REWARDS))
    parser.add_argument("--episodes", type=int, default=20)
    parser.add_argument("--horizon", type=float, required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    layout = api.load_layout(args.model)
    result = evaluate_policy(layout, args.controller, args.episodes, args.seed,
                             policy=args.policy, reward=args.reward, horizon=args.horizon)
    print(f"{args.controller}: mean return = {result['mean']:.6g} "
         f"[{result['lb']:.6g}, {result['ub']:.6g}] over {result['episodes']} episodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
