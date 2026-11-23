"""Spec tests for Task 6: nagasim/core/routing.py RouteRule (written before the code).

Assumed API (from the brief; R5 only changes WHEN the caller invokes arrived()):
  RouteRule(kind, exits, conditions=None, limit=0, attribute="")
  choose(entity, ctx) -> int | None   (None = hold; every exit at limit)
  routed(entity, index)               (count entity against exit `index`)
  arrived(entity)                     (uncount it; the caller decides the moment, R5)
Stand-ins assumed sufficient:
  entity: any hashable object with a dict-like `attributes`.
  ctx for "condition": ctx.eval_condition(expr: str, entity) -> bool.
  ctx for "item": ctx.item_runtime with .entry(entity, outputs) -> int (the real
    ItemRuntime.entry signature, itemlogic.py:310; a real runtime needs ItemApi/ctx
    wiring that cannot be known without reading engine code, so a stand-in is used).
Assumed state semantics: for "round_robin"/"cyclic" the turn pointer advances on a
successful choose() (so a preload through routed() alone does not move it); tests
that must hold under either semantics drive choose() then routed() together.
"""
import pytest

from nagasim.core.routing import RouteRule


class Ent:
    def __init__(self, name="e", **attrs):
        self.name = name
        self.attributes = dict(attrs)

    def __repr__(self):
        return self.name


class Ctx:
    def __init__(self, truth=None, runtime=None):
        self.truth = truth or {}
        self.item_runtime = runtime

    def eval_condition(self, expr, entity):
        return bool(self.truth.get(expr, False))


class FakeItem:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def entry(self, entity, outputs):
        self.calls.append((entity, outputs))
        return self.answer


def go(rule, ctx, n):
    """Route n fresh entities with choose + routed; return the chosen indexes."""
    out, ents = [], []
    for i in range(n):
        e = Ent(f"e{i}")
        k = rule.choose(e, ctx)
        out.append(k)
        if k is not None:
            rule.routed(e, k)
        ents.append(e)
    return out


def take(rule, ctx, name):
    e = Ent(name)
    k = rule.choose(e, ctx)
    if k is not None:
        rule.routed(e, k)
    return e, k


# ---- the brief's five cases -------------------------------------------------

def test_round_robin_three_exits():
    r = RouteRule("round_robin", ["A", "B", "C"])
    assert go(r, Ctx(), 6) == [0, 1, 2, 0, 1, 2]


def test_round_robin_skips_full():
    """limit 1; B preloaded under way; turn pointer at A.
    choose -> A (0), then B full so skip -> C (2); A and C full, B full -> None.
    After A's entity arrives: A is free -> 0."""
    r = RouteRule("round_robin", ["A", "B", "C"], limit=1)
    ctx = Ctx()
    pre = Ent("pre")
    r.routed(pre, 1)
    e1, k1 = take(r, ctx, "e1")
    e2, k2 = take(r, ctx, "e2")
    assert (k1, k2) == (0, 2)
    assert r.choose(Ent("e3"), ctx) is None
    r.arrived(e1)
    assert r.choose(Ent("e4"), ctx) == 0


def test_round_robin_skips_full_order_independent():
    """limit 1: A,B,C taken by e0,e1,e2; e0 and e2 arrive (A, C free); next
    pointer at A: take A (e3). Pointer at B, B full: skip -> C. Then None."""
    r = RouteRule("round_robin", ["A", "B", "C"], limit=1)
    ctx = Ctx()
    e0, _ = take(r, ctx, "e0"); take(r, ctx, "e1"); e2, _ = take(r, ctx, "e2")
    r.arrived(e0); r.arrived(e2)
    assert take(r, ctx, "e3")[1] == 0
    assert take(r, ctx, "e4")[1] == 2
    assert r.choose(Ent("e5"), ctx) is None


def test_cyclic_waits_on_full():
    """limit 1: e0->A, e1->B, e2->C; e0 and e2 arrive; e3->A (pointer now at B).
    B is full (e1), A full (e3), C free. cyclic must wait: None (twice, stable).
    When e1 arrives, B is free -> 1."""
    r = RouteRule("cyclic", ["A", "B", "C"], limit=1)
    ctx = Ctx()
    e0, _ = take(r, ctx, "e0"); e1, _ = take(r, ctx, "e1"); e2, _ = take(r, ctx, "e2")
    r.arrived(e0); r.arrived(e2)
    assert take(r, ctx, "e3")[1] == 0
    assert r.choose(Ent("x"), ctx) is None
    assert r.choose(Ent("y"), ctx) is None
    r.arrived(e1)
    assert r.choose(Ent("z"), ctx) == 1


def test_least_loaded_ties_by_order():
    """under way 1,0,0 -> index 1 (first of the tied minimum); then 1,1,0 -> 2."""
    r = RouteRule("least_loaded", ["A", "B", "C"])
    ctx = Ctx()
    r.routed(Ent("a"), 0)
    assert r.choose(Ent("n1"), ctx) == 1
    r.routed(Ent("b"), 1)
    assert r.choose(Ent("n2"), ctx) == 2


def test_attribute_rule():
    r = RouteRule("attribute", ["L1", "L2", "L3"], attribute="line")
    assert r.choose(Ent(line="L2"), Ctx()) == 1
    assert r.choose(Ent(line="L3"), Ctx()) == 2


# ---- the other kinds --------------------------------------------------------

def test_fixed_always_first():
    r = RouteRule("fixed", ["A", "B"])
    assert go(r, Ctx(), 4) == [0, 0, 0, 0]


def test_sequential_first_below_limit_then_none():
    """limit 2, exits A,B: items go A, A, B, B, then None (all full)."""
    r = RouteRule("sequential", ["A", "B"], limit=2)
    assert go(r, Ctx(), 5) == [0, 0, 1, 1, None]


def test_condition_first_true_last_is_else():
    r = RouteRule("condition", ["A", "B", "C"], conditions=["c0", "c1"])
    e = Ent()
    assert r.choose(e, Ctx({"c0": True, "c1": True})) == 0   # first true wins
    assert r.choose(e, Ctx({"c0": False, "c1": True})) == 1
    assert r.choose(e, Ctx({"c0": False, "c1": False})) == 2  # ELSE = last exit


def test_item_uses_entry_hook():
    item = FakeItem(2)
    r = RouteRule("item", ["A", "B", "C"])
    e = Ent()
    assert r.choose(e, Ctx(runtime=item)) == 2
    assert item.calls == [(e, 3)]


def test_arrived_frees_slot_for_held_choice():
    """sequential limit 1, exits A,B: e0->A, e1->B, e2 held (None).
    e0 arrives -> A free -> e2 gets 0."""
    r = RouteRule("sequential", ["A", "B"], limit=1)
    ctx = Ctx()
    e0, _ = take(r, ctx, "e0")
    take(r, ctx, "e1")
    assert r.choose(Ent("e2"), ctx) is None
    r.arrived(e0)
    assert r.choose(Ent("e2"), ctx) == 0


def test_limit_zero_is_unlimited():
    r = RouteRule("sequential", ["A", "B"], limit=0)
    assert go(r, Ctx(), 50) == [0] * 50
    r2 = RouteRule("round_robin", ["A", "B"])
    assert go(r2, Ctx(), 4) == [0, 1, 0, 1]


def test_cyclic_without_limit_rotates():
    r = RouteRule("cyclic", ["A", "B", "C"])
    assert go(r, Ctx(), 7) == [0, 1, 2, 0, 1, 2, 0]


# ---- review round 1 ---------------------------------------------------------

from nagasim.core.routing import split_exit, validate_rule, default_kind  # noqa: E402


@pytest.mark.parametrize("d", [
    {"kind": "nope", "exits": ["A"]},
    {"kind": "fixed", "exits": []},
    {"kind": "fixed", "exits": ["A"], "limit": -1},
    {"kind": "fixed", "exits": ["A"], "conditions": ["x"]},
    {"kind": "condition", "exits": ["A", "B", "C"], "conditions": ["x"]},
    {"kind": "attribute", "exits": ["A"]},
])
def test_validate_rule_errors(d):
    with pytest.raises(ValueError):
        validate_rule(d)


def test_validate_rule_ok_and_helpers():
    validate_rule({"kind": "condition", "exits": ["A", "B"], "conditions": ["x"]})
    assert split_exit("L1:belt2") == ("L1", "belt2") and split_exit("L1") == ("L1", "")
    assert default_kind("divert") == "fixed" and default_kind("transfer", 2) == "round_robin"
    assert default_kind("transfer", 1) is None and default_kind("decide") is None


def test_possible_exits_has_no_side_effects():
    rr = RouteRule("round_robin", ["A", "B", "C"])
    assert rr.possible_exits(Ent()) == {0, 1, 2}
    assert go(rr, Ctx(), 3) == [0, 1, 2]
    assert RouteRule("fixed", ["A", "B"]).possible_exits(Ent()) == {0}
    assert RouteRule("attribute", ["L1", "L2"], attribute="line").possible_exits(Ent(line="L2")) == {1}
    assert RouteRule("item", ["A", "B"]).possible_exits(Ent()) is None


@pytest.mark.parametrize("bad", [-1, 3, None, True])
def test_item_out_of_range_raises(bad):
    r = RouteRule("item", ["A", "B", "C"])
    with pytest.raises(ValueError):
        r.choose(Ent(), Ctx(runtime=FakeItem(bad)))


def test_routed_range_checked():
    r = RouteRule("fixed", ["A"])
    with pytest.raises(ValueError):
        r.routed(Ent(), -1)


def test_routed_twice_moves_count_and_arrived_unknown_noop():
    r = RouteRule("sequential", ["A", "B"], limit=1)
    e = Ent()
    r.routed(e, 0)
    r.routed(e, 1)
    assert (r.under_way(0), r.under_way(1)) == (0, 1)
    r.arrived(Ent("stranger"))
    assert (r.under_way(0), r.under_way(1)) == (0, 1)


def test_attribute_numeric_and_no_match():
    r = RouteRule("attribute", ["1", "2"], attribute="line")
    assert r.choose(Ent(line=2), Ctx()) == 1
    with pytest.raises(ValueError):
        r.choose(Ent(line=9), Ctx())


def test_condition_all_false_with_one_per_exit_fails_loud():
    # R27: no true condition and no ELSE raises, as DECIDE "Condition" does; it never holds
    r = RouteRule("condition", ["A", "B"], conditions=["c0", "c1"])
    with pytest.raises(ValueError, match="no ELSE"):
        r.choose(Ent(), Ctx())
    assert not r.can_hold() and not RouteRule("cyclic", ["A", "B"]).can_hold()
    assert RouteRule("cyclic", ["A", "B"], limit=1).can_hold()
