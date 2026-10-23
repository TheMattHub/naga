"""Data-only policy files for `evaluate_policy` / `naga policy-eval --policy-file` (D723).

A policy file is JSON that maps an observation to an action. It is never code: no pickle, no
torch, no Stable-Baselines3 zip is loaded here, because loading those executes code. Train
elsewhere, export the result as one of the two kinds below.

    {"kind": "table",  "edges": [[2, 5], [1]], "actions": [0, 1, 1, 0, 1, 1]}
    {"kind": "linear", "weights": [[1.0, 0.0], [0.0, 1.0]], "bias": [0.0, 0.5]}

* table: per observation dimension an ascending list of bucket edges (value v goes in bucket
  `bisect_right(edges, v)`); `actions` is the row-major table over the buckets, one action per cell.
* linear: `weights` has one row per action; the action is the argmax of `weights @ obs + bias`.

Sizes are bounded (see the MAX_* constants), every number must be finite, and an unknown kind
is refused by name. The path is the caller's; an agent tool that took one must confine it first.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from pathlib import Path

from nagasim.core.control import Controller, DecisionRequest

__all__ = ["PolicyFileError", "TablePolicy", "LinearPolicy", "parse_policy", "load_policy_file"]

MAX_BYTES = 1_000_000
MAX_DIMS = 16
MAX_EDGES = 64
MAX_CELLS = 65_536
MAX_ACTIONS = 64
MAX_COLS = 256
KINDS = ("table", "linear")


class PolicyFileError(ValueError):
    """A policy file that is too large, malformed, not finite or of an unknown kind."""


def _numbers(value, what: str, limit: int) -> list[float]:
    if not isinstance(value, list):
        raise PolicyFileError(f"{what} must be a list")
    if len(value) > limit:
        raise PolicyFileError(f"{what} has {len(value)} entries, over the limit of {limit}")
    out = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise PolicyFileError(f"{what} must hold finite numbers only")
        out.append(float(x))
    return out


class TablePolicy(Controller):
    def __init__(self, edges: list[list[float]], actions: list[int]):
        self.edges = edges
        self.actions = actions

    def decide(self, request: DecisionRequest) -> int:
        obs = request.observation
        if len(obs) != len(self.edges):
            raise PolicyFileError(f"table policy has {len(self.edges)} observation dimensions, "
                                  f"the decision gave {len(obs)}")
        cell = 0
        for v, e in zip(obs, self.edges):
            cell = cell * (len(e) + 1) + bisect_right(e, v)
        action = self.actions[cell]
        if action >= request.n_actions:
            raise PolicyFileError(f"table policy chose action {action}, the decision has "
                                  f"{request.n_actions} branches")
        return action


class LinearPolicy(Controller):
    def __init__(self, weights: list[list[float]], bias: list[float]):
        self.weights = weights
        self.bias = bias

    def decide(self, request: DecisionRequest) -> int:
        obs = request.observation
        if len(self.weights) != request.n_actions:
            raise PolicyFileError(f"linear policy has {len(self.weights)} rows, the decision "
                                  f"has {request.n_actions} branches")
        if len(self.weights[0]) != len(obs):
            raise PolicyFileError(f"linear policy has {len(self.weights[0])} columns, the "
                                  f"decision gave {len(obs)} observations")
        scores = [sum(w * v for w, v in zip(row, obs)) + b
                  for row, b in zip(self.weights, self.bias)]
        return max(range(len(scores)), key=lambda i: scores[i])   # first of equal scores


def parse_policy(data) -> Controller:
    """A controller from the parsed JSON *data*, or `PolicyFileError` naming the problem."""
    if not isinstance(data, dict):
        raise PolicyFileError("a policy file holds one JSON object")
    kind = data.get("kind")
    if kind not in KINDS:
        raise PolicyFileError(f"unknown policy kind '{kind}' (expected one of {', '.join(KINDS)})")
    if kind == "table":
        edges_in = data.get("edges")
        if not isinstance(edges_in, list) or not 1 <= len(edges_in) <= MAX_DIMS:
            raise PolicyFileError(f"edges must list 1 to {MAX_DIMS} observation dimensions")
        edges = []
        for e in edges_in:
            e = _numbers(e, "edges", MAX_EDGES)
            if any(a >= b for a, b in zip(e, e[1:])):
                raise PolicyFileError("edges must be strictly ascending")
            edges.append(e)
        cells = math.prod(len(e) + 1 for e in edges)
        if cells > MAX_CELLS:
            raise PolicyFileError(f"table has {cells} cells, over the limit of {MAX_CELLS}")
        actions = _numbers(data.get("actions"), "actions", MAX_CELLS)
        if len(actions) != cells:
            raise PolicyFileError(f"actions has {len(actions)} entries, the edges give {cells} cells")
        if any(a != int(a) or not 0 <= a < MAX_ACTIONS for a in actions):
            raise PolicyFileError(f"actions must be whole numbers from 0 to {MAX_ACTIONS - 1}")
        return TablePolicy(edges, [int(a) for a in actions])
    rows_in = data.get("weights")
    if not isinstance(rows_in, list) or not 1 <= len(rows_in) <= MAX_ACTIONS:
        raise PolicyFileError(f"weights must have 1 to {MAX_ACTIONS} rows (one per action)")
    rows = [_numbers(r, "weights row", MAX_COLS) for r in rows_in]
    if not rows[0] or any(len(r) != len(rows[0]) for r in rows):
        raise PolicyFileError("weights rows must be non-empty and all the same length")
    bias = _numbers(data.get("bias", [0.0] * len(rows)), "bias", MAX_ACTIONS)
    if len(bias) != len(rows):
        raise PolicyFileError(f"bias has {len(bias)} entries, weights has {len(rows)} rows")
    return LinearPolicy(rows, bias)


def _refuse_constant(name):
    raise PolicyFileError(f"{name} is not allowed; numbers must be finite")


def load_policy_file(path) -> Controller:
    """Read and validate a policy file; the size is checked before the file is read."""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise PolicyFileError(f"policy file is over {MAX_BYTES} bytes")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_refuse_constant)
    except json.JSONDecodeError as exc:
        raise PolicyFileError(f"not valid JSON: {exc}") from exc
    return parse_policy(data)
