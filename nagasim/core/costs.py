"""Lump-sum costing (PHASE4 D5, ARCHITECTURE D63).

Everything else in the costing model accrues as a RATE: a resource's cost per
busy hour, an entity's holding cost per time unit, a flow's price per unit
moved. Three real costs are not rates, and each one used to be computed outside
the run:

* a cost per EVENT -- an ordering cost every time a replenishment is placed, a
  penalty per lost customer, a changeover cost. `RECORD` with `kind="cost"`
  adds its expression to a named item as the entity passes.
* a FIXED cost of the whole scenario -- capex, or an annuity over a sized
  parameter. The `fixed_costs` section names one, with an `amount` expression
  evaluated ONCE at run start.
* a PEAK charge -- a demand charge is peak imported power times a tariff, and
  a flow already tracks its `Peak Rate`. A flow's `peak_price` bills it.

All three land in the `cost_summary` frame and join the model's `Total Cost`.
This module is a leaf: stdlib plus `core/expressions.py`, no pandas, no engine
state beyond the context it is handed.

Warm-up rule: an EVENT cost is a statistic, so it resets with every other one
at the end of the warm-up. A FIXED cost is not -- it is the cost of having
built the thing, not of running it, and it is NOT prorated to the observed
window. An author who wants a share of an annuity writes that share into the
`amount` expression.
"""
from __future__ import annotations

from dataclasses import dataclass

from nagasim.core.expressions import (ExpressionError, ExpressionScope, compile_expression)
from nagasim.core.problems import problem

__all__ = ["CostItem", "build_fixed_costs", "accrue",
           "reset_event_costs", "validate_fixed_costs_structured",
           "build_energy_price", "validate_energy_price_structured"]

@dataclass
class CostItem:
    """One itemised non-rate cost. `amount` is the running total for an event
    item and the once-evaluated amount for a fixed one."""

    name: str
    kind: str = "event"
    category: str = ""
    amount: float = 0.0


def build_fixed_costs(layout: dict, ctx) -> dict[str, CostItem]:
    """The "fixed_costs" section, each `amount` evaluated once, now.

    Called from `GraphModel.load_from_json` after variables, tables and the
    time series are in place, so an amount may read them. A `$`-cloned entry
    carries its submodel instance's parameters as `locals`, exactly as a cloned
    flow's rate does.
    """
    items: dict[str, CostItem] = {}
    for entry in layout.get("fixed_costs") or []:
        name = str(entry.get("name", ""))
        if not name or name in items:
            continue        # validation reported it; a run must not crash here
        locals_ = entry.get("locals") or {}
        names = set(getattr(ctx, "variables", {}) or {}) | set(locals_)
        amount = entry.get("amount", 0.0)
        if isinstance(amount, str):
            scope = ExpressionScope(ctx=ctx, locals=dict(locals_) if locals_ else None)
            amount = compile_expression(amount, names).evaluate(scope)
        items[name] = CostItem(name, "fixed", str(entry.get("category", "") or ""),
                               float(amount))
    return items


def accrue(ctx, name: str, amount: float, category: str = "") -> CostItem:
    """Add *amount* to the event cost item *name*, creating it if needed."""
    item = ctx.cost_items.get(name)
    if item is None:
        item = ctx.cost_items[name] = CostItem(name, "event", category)
    item.amount += float(amount)
    return item


def reset_event_costs(ctx) -> None:
    """Warm-up: the event totals restart, the fixed amounts stay (see module
    docstring)."""
    for item in ctx.cost_items.values():
        if item.kind == "event":
            item.amount = 0.0


# ---------------------------------------------------------------- validation
def validate_fixed_costs_structured(layout: dict) -> list:
    """Check the "fixed_costs" section. Returns [Problem]; the declared names
    are not needed anywhere else, so nothing else comes back."""
    problems: list = []
    seen: set[str] = set()
    variables = {str(v.get("name", "")) for v in layout.get("variables", []) or []
                 if isinstance(v, dict)}
    for index, entry in enumerate(layout.get("fixed_costs") or []):
        at = f"/fixed_costs/{index}"
        if not isinstance(entry, dict):
            problems.append(problem("E_FIXED_COST_NAME", "fixed cost: each entry must be an object",
                                    at, hint="{\"name\": \"Battery capex\", \"amount\": \"kwh * 600\"}"))
            continue
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_FIXED_COST_NAME", "fixed cost: name must not be empty",
                                    f"{at}/name", hint="give every fixed cost a unique name"))
            continue
        if name in seen:
            problems.append(problem("E_FIXED_COST_NAME", f"duplicate fixed cost name: {name}",
                                    f"{at}/name", hint="rename one of the two"))
        seen.add(name)
        amount = entry.get("amount", 0.0)
        if isinstance(amount, str):
            # `locals` are a submodel instance's parameters, legal names here
            # exactly as in a cloned flow's rate.
            names = variables | set(entry.get("locals") or {})
            try:
                compile_expression(amount, names)
            except ExpressionError as exc:
                problems.append(problem(
                    "E_FIXED_COST_AMOUNT", f"fixed cost '{name}': {exc}", f"{at}/amount",
                    hint="the amount is an expression over variables and model data, "
                         "evaluated once at run start"))
        elif not isinstance(amount, (int, float)) or isinstance(amount, bool):
            problems.append(problem(
                "E_FIXED_COST_AMOUNT", f"fixed cost '{name}': \"amount\" must be a number or "
                                       f"an expression", f"{at}/amount",
                hint="a number, or an expression like \"capacity_kwh * 600 * 0.08\""))
    return problems


# ------------------------------------------------------------ energy (M1, D112)
def build_energy_price(layout: dict, ctx) -> float:
    """"simulation.energy_price" (per kWh), evaluated ONCE at run start like a
    fixed cost's `amount` -- a model that declares none prices every kWh at 0."""
    price = layout.get("simulation", {}).get("energy_price", 0.0)
    if isinstance(price, str):
        names = set(getattr(ctx, "variables", {}) or {})
        price = compile_expression(price, names).evaluate(ExpressionScope(ctx=ctx))
    return float(price)


def validate_energy_price_structured(layout: dict, variables: set[str]) -> list:
    """Check "simulation.energy_price". Returns [Problem]."""
    price = layout.get("simulation", {}).get("energy_price", 0.0)
    if isinstance(price, str):
        try:
            compile_expression(price, variables)
        except ExpressionError as exc:
            return [problem("E_ENERGY_PRICE", f"simulation.energy_price: {exc}",
                            "/simulation/energy_price",
                            hint="an expression over variables, evaluated once at run start")]
    elif not isinstance(price, (int, float)) or isinstance(price, bool):
        return [problem(
            "E_ENERGY_PRICE", "simulation.energy_price must be a number or an expression",
            "/simulation/energy_price", hint="money per kWh, e.g. 0.18")]
    return []
