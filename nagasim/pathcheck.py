"""Entity-path checks: rules over the blocks each entity visits (the `path_checks` section).

A path check states a rule about the route every entity takes, checked on one traced
replication by `naga check` (`modelcheck.check`):

    {"path_checks": [
        {"name": "inspect_first", "rule": "order", "first": "Inspect", "then": "Pack"},
        {"name": "no_scrap", "rule": "never", "block": "Scrap", "e_type": "Premium"},
        {"name": "rework_cap", "rule": "at_most", "block": "Rework", "count": 2},
        {"name": "all_inspected", "rule": "always", "block": "Inspect"}]}

A VISIT is an arrival at a block: the block an entity is created at, and the target of every
move after that, a drawn link or a route jump alike, at the instant the entity gets there
(after a link's transfer delay). The rules:

* `order`: an entity that visits both blocks reaches `first` before `then`. Judged on first
  visits: the violation is the entity's first arrival at `then` when it had not yet been at
  `first`. A rework loop back to `first` later is not a violation.
* `never`: no entity visits `block`; the violation is its first arrival there.
* `at_most`: no entity visits `block` more than `count` times; the violation is the arrival
  that goes over.
* `always`: every COMPLETED entity (one that left through a DISPOSE) visited `block`; the
  violation is the instant it left. An entity still in the system at the end is judged by
  the other three rules only: its route is not over. Neither is one that left after the
  trace stopped (`max_visits`): its last arrivals were not recorded.

`e_type` restricts a rule to entities of that type: the type an entity had when it was last
seen, so a type an ASSIGN gives on the way counts. Block names are the flattened ones
(`instance/block` inside a submodel). An entity is a row of the trace's own ids; an entity
batched into another stops being followed there.

ONE SEED: the rules are checked on the first replication only, the one `api.run_model`
traces. A pass says this run's entities kept the rule, not that every run's will; every
result says so (D722).
"""
from __future__ import annotations

__all__ = ["RULES", "check_paths"]

RULES = ("order", "never", "at_most", "always")

# The examples each failed rule reports; the count of violations is always complete.
_EXAMPLES = 3


class _Visits:
    """A bus subscriber keeping only what the rules read: per entity, its ordered arrivals
    (time, block), its last type, and when it left through a DISPOSE. Stops recording past
    *max_visits* and says so (`truncated`), like `TraceRecorder`'s own cap; completions stop
    at the same instant."""

    def __init__(self, max_visits: int):
        self.max_visits = max_visits
        self.count = 0
        self.truncated = False
        self.arrivals: dict[int, list[tuple[float, str]]] = {}
        self.types: dict[int, str] = {}
        self.completed: dict[int, float] = {}
        self._ctx = None

    def __call__(self, name, *, block=None, entity=None, t=0.0, target=None, duration=0.0, **_):
        if name not in ("entity_created", "entity_moved", "entity_disposed") or entity is None:
            return
        if self._ctx is None:
            self._ctx = getattr(block, "ctx", None)
        key = entity.id
        self.types[key] = getattr(entity, "e_type", "") or ""
        if name == "entity_disposed":
            # Past the cut an entity's later arrivals are missing: a completion recorded then
            # would fail `always` falsely (#939), so it is left out like the arrivals.
            if not self.truncated:
                self.completed[key] = t
            return
        where = block.name if name == "entity_created" else target
        if where is None:
            return
        if self.count >= self.max_visits:
            self.truncated = True
            return
        self.count += 1
        self.arrivals.setdefault(key, []).append((t + (duration or 0.0), where))

    def end(self) -> float:
        """The run's last instant: an arrival after it was still in transit at the end."""
        end = getattr(self._ctx, "sim_end_time", None)
        return float("inf") if end is None else float(end)


def _violation(entity: int, e_type: str, t: float, block: str, what: str) -> dict:
    return {"entity": entity, "e_type": e_type, "t": t, "block": block, "what": what}


def _judge_one(rule: dict, entity: int, e_type: str, route: list, left) -> dict | None:
    """The entity's first violation of *rule*, or None; *left* is when it completed, or None."""
    kind = rule["rule"]
    if kind == "order":
        first, then = rule["first"], rule["then"]
        blocks = [b for _t, b in route]
        if first not in blocks or then not in blocks:
            return None
        t_first = next(t for t, b in route if b == first)
        t_then = next(t for t, b in route if b == then)
        if blocks.index(then) < blocks.index(first):
            return _violation(entity, e_type, t_then, then,
                              f"reached {then} before {first} (at t={t_first:g})")
        return None
    block = rule["block"]
    if kind == "always":
        if left is None or any(b == block for _t, b in route):
            return None
        return _violation(entity, e_type, left, block, f"left without visiting {block}")
    limit = 0 if kind == "never" else int(rule["count"])
    seen = 0
    for t, b in route:
        if b == block:
            seen += 1
            if seen > limit:
                what = f"visited {block}" if kind == "never" else f"visit {seen} to {block}"
                return _violation(entity, e_type, t, block, what)
    return None


def _judge(rule: dict, visits: _Visits, end: float) -> dict:
    wanted = rule.get("e_type")
    violations = []
    judged = 0
    for entity, route in visits.arrivals.items():
        e_type = visits.types.get(entity, "")
        if wanted and e_type != wanted:
            continue
        left = visits.completed.get(entity)
        if rule["rule"] == "always" and left is None:
            continue
        judged += 1
        found = _judge_one(rule, entity, e_type, [v for v in route if v[0] <= end], left)
        if found is not None:
            violations.append(found)
    violations.sort(key=lambda v: (v["t"], v["entity"]))
    return {"name": rule["name"], "rule": rule["rule"], "passed": not violations,
            "violations": len(violations), "examples": violations[:_EXAMPLES], "judged": judged,
            "truncated": visits.truncated}


def check_paths(layout, *, seed: int = 0, run_length=None, warmup=None,
                max_visits: int = 1_000_000) -> list[dict]:
    """Run the model once, traced, and judge every "path_checks" rule on that run.

    Parameters
    ----------
    layout : str or dict
        The model file or the layout dict.
    seed : int, default 0
        The seed of the one replication judged.
    run_length, warmup : optional
        Fall back to the model's own settings, as in `api.run_model`. The whole run is
        judged, the warm-up included: a route has no steady state.
    max_visits : int, default 1 000 000
        Arrivals recorded before the trace stops; the caller's limit, never the model's.

    Returns
    -------
    list of dict
        One per rule, in file order: "name", "rule", "passed", "violations" (the count),
        "examples" (the first few: "entity", "e_type", "t", "block", "what"), "judged" (the
        entities the rule looked at) and "truncated" (the trace stopped at *max_visits*).
        A model with no path checks returns an empty list without running.
    """
    from nagasim import api                        # kept off the module import
    base = api.load_layout(layout)
    rules = base.get("path_checks") or []
    if not rules:
        return []
    visits = _Visits(max_visits)
    results = api.run_model(base, replications=1, seed=seed, run_length=run_length,
                            warmup=warmup, trace=visits)
    if results is None:
        raise ValueError("path checks: the run produced no result")
    end = visits.end()
    return [_judge(rule, visits, end) for rule in rules]
