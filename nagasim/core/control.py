"""Engine seam for controllable DECIDE blocks (ROADMAP 3.7 A item 2).

Pure Python leaf module: no simpy import beyond the `env.event()` call inside
`PausingController.request` (simpy is already a hard dependency of the
engine), no UI/dataframe dependency of any kind. A controller is attached per policy NAME via
`ctx.controllers: dict[str, Controller]` -- `nagasim.api.run_model(...,
controllers={...})` sets it; several Policy-mode DECIDE blocks may share one
policy name (and so one controller).

Two shapes of controller:

* A plain `Controller` (the built-ins below, or a user's `CallablePolicy`) is
  SYNCHRONOUS: the DECIDE block calls `decide(request)` inline, in the same
  call stack, and gets an action index back immediately -- no simpy process is
  created, so this is zero simulated time and does not perturb event order.
* `PausingController` is the special case `nagasim.rl.SimEnv` uses to drive a
  policy from OUTSIDE the simulation loop: the DECIDE block never calls
  `decide()` on it; it calls `request()` to get a simpy event, yields on it
  (so the block's process really does suspend), and `SimEnv.step()` resolves
  the event later with `resolve(action)`.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

__all__ = ["DecisionRequest", "Controller", "RandomPolicy", "ShortestQueuePolicy",
           "RoundRobinPolicy", "CallablePolicy", "PausingController"]


@dataclass(frozen=True)
class DecisionRequest:
    """What a Policy-mode DECIDE hands its controller at one decision point."""

    policy: str
    block: str
    t: float
    entity_id: int
    e_type: str | None
    observation: tuple
    n_actions: int


class Controller:
    """Base for a synchronous, in-process decision policy."""

    def decide(self, request: DecisionRequest) -> int:
        raise NotImplementedError


class RandomPolicy(Controller):
    """Uniform over the available actions."""

    def __init__(self, rng=None):
        self.rng = rng if rng is not None else np.random.default_rng()

    def decide(self, request: DecisionRequest) -> int:
        return int(self.rng.integers(0, request.n_actions))


class ShortestQueuePolicy(Controller):
    """Picks the action with the smallest observation value -- the DECIDE
    block's `observation` list is expected to give one queue-length-like
    reading per outgoing link, in branch order."""

    def decide(self, request: DecisionRequest) -> int:
        obs = request.observation[:request.n_actions] or (0.0,) * request.n_actions
        return int(min(range(len(obs)), key=lambda i: obs[i]))


class RoundRobinPolicy(Controller):
    """Cycles 0, 1, ..., n_actions - 1, 0, ...; state is kept per policy name,
    so one instance can serve several shared-name DECIDE blocks correctly."""

    def __init__(self):
        self._next: dict[str, int] = {}

    def decide(self, request: DecisionRequest) -> int:
        i = self._next.get(request.policy, 0) % max(request.n_actions, 1)
        self._next[request.policy] = i + 1
        return i


class CallablePolicy(Controller):
    """Wraps any `fn(request) -> int` as a Controller."""

    def __init__(self, fn):
        self.fn = fn

    def decide(self, request: DecisionRequest) -> int:
        return int(self.fn(request))


class PausingController(Controller):
    """Drives one policy from outside the simulation loop (`nagasim.rl.SimEnv`).

    `decide()` is never called on this class -- a Policy-mode DECIDE detects
    it (`isinstance`) and instead calls `request()` to register a pending
    (DecisionRequest, simpy event) pair and yields on the event; `SimEnv.step()`
    resolves it with `resolve(action)`, which lets that DECIDE process (and the
    rest of the simulation) continue.

    Requests queue FIFO in `requests` as (id, DecisionRequest, event), ids
    drawn from *ids* (PHASE6 L2: a `SimSession` shares one counter across its
    policies, so ids follow creation order; one env step can create several
    requests). `pending` is the oldest (request, event) or None. `SimEnv`
    keeps its one-at-a-time contract itself.
    """

    def __init__(self, ids=None):
        self._ids = ids if ids is not None else itertools.count()
        self.requests: list[tuple[int, DecisionRequest, object]] = []

    @property
    def pending(self) -> tuple[DecisionRequest, object] | None:
        return self.requests[0][1:] if self.requests else None

    def decide(self, request: DecisionRequest) -> int:
        raise RuntimeError("PausingController must be awaited via request()/resolve(), "
                           "not called synchronously")

    def request(self, env, req: DecisionRequest):
        event = env.event()
        self.requests.append((next(self._ids), req, event))
        return event

    def resolve(self, action: int, request_id: int | None = None) -> None:
        """Answer the request *request_id* (None: the oldest)."""
        index = 0 if request_id is None else next(
            (i for i, entry in enumerate(self.requests) if entry[0] == request_id), None)
        if not self.requests or index is None:
            raise RuntimeError(f"no pending decision request {request_id} to resolve")
        _id, _req, event = self.requests.pop(index)
        event.succeed(int(action))
