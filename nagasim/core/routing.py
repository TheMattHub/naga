"""Routing rules: pick one of several exits for an entity (conveyor system A6).

Pure module: no SimPy, no block imports. A caller (CONVEYOR diverts, DECIDE, the
CONVEYOR_TRANSFER block) builds a RouteRule from a rule dict, asks `choose` for an
exit, calls `routed` when it sends the entity there and `arrived` when the entity
reaches that exit's target piece (R5 says when). `choose` returns None to hold.

ctx protocol (what the engine side supplies):
  ctx.eval_condition(expr, entity) -> bool     for "condition"
  ctx.item_runtime.entry(entity, n_outputs)    for "item" (ItemRuntime.entry)
"""
from __future__ import annotations

KINDS = ("fixed", "sequential", "cyclic", "round_robin", "least_loaded",
         "condition", "attribute", "item")

# Plan ruling (D1358), applied by the callers, not here.
DEFAULT_DIVERT_KIND = "fixed"
DEFAULT_TRANSFER_KIND = "round_robin"


def default_kind(block_kind: str, n_exits: int = 1) -> str | None:
    """Rule used when a block names none: a divert goes fixed to the belt end,
    a transfer with two or more exits goes round_robin; DECIDE (None) keeps its modes."""
    if block_kind == "divert":
        return DEFAULT_DIVERT_KIND
    if block_kind == "transfer" and n_exits >= 2:
        return DEFAULT_TRANSFER_KIND
    return None


def split_exit(spec: str) -> tuple[str, str]:
    """'name:target' -> (name, target); without a colon the target is ''
    (the caller then takes the last conveyor of that branch)."""
    name, _, target = str(spec).partition(":")
    return name, target


OUT = "out"
# The keys of a rule dict; any other (a misspelt "limits") is refused, never ignored (R34).
RULE_KEYS = ("kind", "exits", "conditions", "limit", "attribute")


def rule_spec(rule, default_exits: list, block_kind: str = ""):
    """The rule dict a caller runs and validates (R38: one place for the run and validation):
    a copy of *rule* with its `exits` defaulting to *default_exits*; an empty rule of a
    "transfer" takes the default kind (D1358). Not a dict: returned as is, for
    `validate_rule` to refuse."""
    if block_kind == "transfer" and not rule:
        rule = {"kind": default_kind("transfer", len(default_exits)) or "fixed"}
    if not isinstance(rule, dict):
        return rule
    spec = dict(rule)
    if not spec.get("exits"):
        spec["exits"] = list(default_exits)
    return spec


def divert_groups(diverts: list) -> dict:
    """A7 (D1359): {group: [(index, divert)] in belt order} of the routed diverts among the
    divert dicts *diverts* (a divert is routed when it has a `rule` or a `rule_group` key); a
    divert with a rule and no group is a group of its own, keyed "#<index>". One grouping for
    the run and validation (R38)."""
    groups: dict = {}
    for index, divert in enumerate(diverts):
        if isinstance(divert, dict) and ("rule" in divert or "rule_group" in divert):
            group = divert.get("rule_group")
            key = group.strip() if isinstance(group, str) and group.strip() else f"#{index}"
            groups.setdefault(key, []).append((index, divert))
    for members in groups.values():
        members.sort(key=lambda m: _at(m[1].get("at")))
    return groups


def _at(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0.0
    return float(value)


def divert_exits(indices) -> list:
    """The default exit names of a divert group: its diverts' ports, "d<index>"."""
    return [f"d{index}" for index in indices]


def uses_item_rule(block_type: str, params: dict) -> bool:
    """Does a block dict route by an "item" rule: a Rule-mode DECIDE, a CONVEYOR divert or a
    CONVEYOR_TRANSFER?
    Such a block reads its item's code, so flattening stamps the item on it (D1359)."""
    if not isinstance(params, dict):
        return False
    if block_type == "DECIDE":
        rule = params.get("rule")
        return params.get("mode") == "Rule" and isinstance(rule, dict) and rule.get("kind") == "item"
    if block_type == "CONVEYOR_TRANSFER":
        rule = params.get("rule")
        return isinstance(rule, dict) and rule.get("kind") == "item"
    if block_type == "CONVEYOR":
        diverts = params.get("diverts")
        return any(isinstance(d, dict) and isinstance(d.get("rule"), dict)
                   and d["rule"].get("kind") == "item"
                   for d in (diverts if isinstance(diverts, list) else []))
    return False


def divert_slots(exits: list, n_diverts: int) -> list:
    """Per exit of a divert group's rule (R28): the group divert it leaves by, in belt order,
    or None for the exit named `out`, which passes every divert of the group to the belt's
    end. Raises ValueError unless the other exits match the group's diverts one to one."""
    names = [split_exit(e)[0] for e in exits]
    outs = names.count(OUT)
    if outs > 1:
        raise ValueError(f"exit '{OUT}' (the belt's end) is named {outs} times")
    if len(names) - outs != n_diverts:
        raise ValueError(f"the rule has {len(names) - outs} divert exits for the {n_diverts} "
                         f"diverts of its group (exit '{OUT}' is the belt's end)")
    slots, k = [], 0
    for name in names:
        if name == OUT:
            slots.append(None)
        else:
            slots.append(k)
            k += 1
    return slots


def validate_rule(rule: dict) -> None:
    """Raise ValueError with a plain message when a rule dict is not usable."""
    if not isinstance(rule, dict):
        raise ValueError("a routing rule must be an object")
    RouteRule.from_dict(rule)


class RouteRule:
    """One rule over several exits. The caller must call arrived() for every routed()
    entity on every path (reaches the target, destroyed, run ends); there is no reset,
    a missed arrived() keeps that exit counted. choose() is the only method that
    advances the turn pointer; possible_exits() has no side effects."""

    def __init__(self, kind: str, exits: list[str], conditions: list[str] | None = None,
                 limit: int = 0, attribute: str = ""):
        if kind not in KINDS:
            raise ValueError(f"routing kind '{kind}' is not one of {', '.join(KINDS)}")
        exits = list(exits) if exits else []
        if not exits:
            raise ValueError("a routing rule needs at least one exit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"routing limit must be a whole number >= 0, got {limit!r}")
        conditions = list(conditions) if conditions else []
        if kind == "condition":
            if len(conditions) not in (len(exits), len(exits) - 1) or not conditions:
                raise ValueError(
                    f"a condition rule needs one condition per exit, or one fewer for an ELSE "
                    f"exit (got {len(conditions)} conditions for {len(exits)} exits)")
        elif conditions:
            raise ValueError(f"routing kind '{kind}' takes no conditions")
        if kind == "attribute" and not attribute:
            raise ValueError("an attribute rule needs the attribute name")
        self.kind, self.exits, self.conditions = kind, exits, conditions
        self.limit, self.attribute = limit, str(attribute or "")
        self._where: dict = {}            # entity -> exit index it is under way to
        self._count = [0] * len(exits)
        self._turn = 0

    @classmethod
    def from_dict(cls, d: dict) -> "RouteRule":
        unknown = sorted(str(key) for key in d if key not in RULE_KEYS)
        if unknown:
            raise ValueError(f"unknown key{'s' if len(unknown) > 1 else ''} "
                             f"{', '.join(repr(k) for k in unknown)}; a rule takes "
                             f"{', '.join(RULE_KEYS)}")
        return cls(d.get("kind", ""), d.get("exits") or [], d.get("conditions"),
                   d.get("limit", 0), d.get("attribute", ""))

    def can_hold(self) -> bool:
        """Can choose() ever return None? Only with a limit: an exit is full only then, and a
        condition rule with no true condition and no ELSE raises (R27). Such a point is
        refused on a zoned belt (conveyor system A7)."""
        return self.limit > 0

    def counts(self) -> bool:
        """Does anything read the under-way counts: a limit, or least_loaded's choice?"""
        return self.limit > 0 or self.kind == "least_loaded"

    def under_way(self, index: int) -> int:
        return self._count[index]

    def _full(self, i: int) -> bool:
        return self.limit > 0 and self._count[i] >= self.limit

    def _scan(self, start: int):
        n = len(self.exits)
        for k in range(n):
            i = (start + k) % n
            if not self._full(i):
                return i
        return None

    def choose(self, entity, ctx) -> int | None:
        n, kind = len(self.exits), self.kind
        if kind == "sequential":
            return self._scan(0)
        if kind == "round_robin":
            i = self._scan(self._turn)
            if i is not None:
                self._turn = (i + 1) % n
            return i
        if kind == "cyclic":
            if self._full(self._turn):
                return None
            i = self._turn
            self._turn = (i + 1) % n
            return i
        if kind == "least_loaded":
            free = [i for i in range(n) if not self._full(i)]
            return min(free, key=lambda i: (self._count[i], i)) if free else None
        if kind == "fixed":
            i = 0
        elif kind == "condition":
            i = n - 1 if len(self.conditions) < n else None   # ELSE only with n-1 conditions
            for k, expr in enumerate(self.conditions):
                if ctx.eval_condition(expr, entity):
                    i = k
                    break
            if i is None:
                # R27: fail loud, as DECIDE "Condition" does; a hold would wait for a count
                # drop that a condition's own inputs never cause
                raise ValueError("no condition was true and the rule has no ELSE exit (an exit "
                                 "after the last condition)")
        elif kind == "attribute":
            i = self._attribute_exit(entity)
        else:  # item
            i = ctx.item_runtime.entry(entity, n)
            if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n:
                raise ValueError(f"the item code chose exit {i!r}, but the rule has {n} exits "
                                 f"(0 to {n - 1})")
        return None if self._full(i) else i

    def _attribute_exit(self, entity) -> int:
        # an engine Entity keeps its attributes as plain attributes (entity.line); a dict-like
        # `attributes` is read when the object has one
        attrs = getattr(entity, "attributes", None)
        value = str(attrs.get(self.attribute) if isinstance(attrs, dict)
                    else getattr(entity, self.attribute, None))
        names = [split_exit(e)[0] for e in self.exits]
        if value not in names:
            raise ValueError(f"attribute '{self.attribute}' is {value!r}, "
                             f"which names none of the exits {names}")
        return names.index(value)

    def possible_exits(self, entity, ctx=None) -> set | None:
        """Exits this rule could pick for the entity, without side effects; None = unknown (item)."""
        if self.kind == "fixed":
            return {0}
        if self.kind == "attribute":
            return {self._attribute_exit(entity)}
        if self.kind == "item":
            return None
        return set(range(len(self.exits)))

    def routed(self, entity, index: int) -> None:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self.exits):
            raise ValueError(f"exit index {index!r} is out of range (0 to {len(self.exits) - 1})")
        if entity in self._where:
            self.arrived(entity)
        self._where[entity] = index
        self._count[index] += 1

    def arrived(self, entity) -> None:
        i = self._where.pop(entity, None)
        if i is not None:
            self._count[i] -= 1
