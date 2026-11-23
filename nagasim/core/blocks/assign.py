import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import compiled_closure, entity_scope, evaluate_expression
from nagasim.core.expressions import ExpressionError, ExpressionScope, entity_agent, parse_target

log = logging.getLogger(__name__)

__all__ = ["Assign", "SPEC", "apply_rows", "compile_rows"]


def compile_rows(assignments) -> list:
    """(target, expr, parsed target, closure) per assignment row. A bad target or expression
    leaves its slot None and takes the original path in `apply_rows`, so the error is raised
    per entity with its original message."""
    rows = []
    for row in assignments:
        target, expr = row.get("target", ""), row.get("expr", "")
        try:
            parsed = parse_target(target)
        except (ExpressionError, ValueError):
            parsed = None
        rows.append((target, expr, parsed, compiled_closure(expr) if parsed else None))
    return rows


def apply_rows(block, entity, rows, label: str) -> None:
    """Run compiled assignment *rows* for *entity* in order, as *block*, then the effects a
    variable write has (flow rates, agents, HOLD conditions). *label* opens an error message."""
    ctx = block.ctx
    wrote_variable = wrote_agent = False
    scope = entity_scope(block, entity)
    for target, expr, parsed, fn in rows:
        try:
            kind, name = parsed if parsed is not None else parse_target(target)
            if kind == "self":      # an agent action's target (PHASE5 AB-2)
                raise ExpressionError("self.<attribute> is only valid in an agent action")
            value = fn(scope) if fn is not None else evaluate_expression(block, expr, entity)
            if kind == "agent":     # H1: entity.agent.<attr>, the agent's own column
                population, index = entity_agent(ExpressionScope(ctx=ctx, entity=entity))
                ctx.agent_system.set_attr(population, index, name, value)
                wrote_variable = wrote_agent = True
            elif kind == "entity":
                setattr(entity, name, value)      # e_type keeps its own setter
                if name == "sequence":
                    # Routing (PHASE4 A2): a new sequence always starts at
                    # its first operation, so the step index resets with it.
                    # Assign `entity.step` explicitly AFTER this one to
                    # resume a routing part-way through.
                    entity.step = 0
            else:
                ctx.variables[name] = value
                wrote_variable = True
                if ctx.watched or ctx.traced_variables or ctx.variable_hooks:   # a HOLD, a picture, an item
                    ctx.wrote_variable(name)
        except (ExpressionError, ValueError) as exc:
            raise ExpressionError(f"{label}: {target} = {expr}: {exc}") from None
    # A flow rate may read a model variable (PHASE4 D1), so writing one is
    # one of the discrete events that re-evaluates the rates. The check
    # costs one `is None` on a model with no levels, which is every model
    # written before this existed.
    if wrote_variable and ctx.level_system is not None:
        ctx.level_system.trigger()
    if wrote_agent:     # D129: settled now, so the next block sees the new state
        ctx.agent_system.settle()
    elif wrote_variable and ctx.agent_system is not None:   # PHASE5 AB-1: conditions
        ctx.agent_system.trigger()


class Assign(BaseProcess):
    """Sets attributes on a part, order or customer, or a model variable, from expressions as it passes, for example a due date, a type or a counter.

    Takes an ordered list of assignments, each is
    `{"target": "entity.<attr>" | "entity.agent.<attr>" | "<variable>", "expr": "<expression>"}`.

    Assignments run in order, so a later one sees what an earlier one wrote.
    Targets and expressions both go through core/expressions.py -- a model file
    never executes Python.
    """

    def __init__(self, env, block, next_process=None, assignments=None):
        super().__init__(env, block, next_process)
        self.assignments = list(assignments) if assignments else []
        self._plan = None

    def _rows(self):
        """(target, expr, parsed target, closure) per assignment, built once.

        Keyed on the (target, expr) texts themselves, not on the list object:
        a replaced list, an edited dict and an appended item all change the key,
        so the plan cannot go stale however `assignments` is modified. The key
        costs two dict reads per assignment, far less than a parse."""
        key = tuple((a.get("target", ""), a.get("expr", "")) for a in self.assignments)
        plan = self._plan
        if plan is None or plan[0] != key:
            plan = self._plan = (key, compile_rows({"target": t, "expr": e} for t, e in key))
        return plan[1]

    def self_process(self, entity):
        apply_rows(self, entity, self._rows(), f"Assign block '{self.name}'")
        if self.ctx.debug:
            log.debug("Entity %s assigned %s at time %s.", entity.id, self.assignments, self.env.now)
        self.forward(entity, 0)

    def self_run(self, entity):
        self.self_process(entity)

    def get_params_dict(self):
        return {"assignments": [dict(a) for a in self.assignments]}

    def set_params_from_dict(self, param_dict):
        self.assignments = [dict(a) for a in param_dict.get("assignments", [])]


SPEC = BlockSpec(
    type_name="ASSIGN", cls=Assign,
    params=(ParamSpec("assignments", list, [],
                      description="Ordered list of {\"target\": \"entity.<attr>\" | "
                                  "\"entity.agent.<attr>\" | \"<variable>\", "
                                  "\"expr\": \"<expression>\"}; each runs in order.", quantity="none"),),
    factory=lambda env, block: Assign(env, block),
    family="data",
    transparent=True,
)
