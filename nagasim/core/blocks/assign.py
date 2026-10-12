import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import compiled_closure, entity_scope, evaluate_expression
from nagasim.core.expressions import ExpressionError, ExpressionScope, entity_agent, parse_target

log = logging.getLogger(__name__)

__all__ = ["Assign", "SPEC"]


class Assign(BaseProcess):
    """Ordered list of assignments: each is
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
        costs two dict reads per assignment, far less than a parse. A bad target
        or expression leaves its slot None and takes the original path, so the
        error is raised per entity with its original message."""
        key = tuple((a.get("target", ""), a.get("expr", "")) for a in self.assignments)
        plan = self._plan
        if plan is None or plan[0] != key:
            rows = []
            for target, expr in key:
                try:
                    parsed = parse_target(target)
                except (ExpressionError, ValueError):
                    parsed = None
                rows.append((target, expr, parsed, compiled_closure(expr) if parsed else None))
            plan = self._plan = (key, rows)
        return plan[1]

    def self_process(self, entity):
        wrote_variable = wrote_agent = False
        scope = entity_scope(self, entity)
        for target, expr, parsed, fn in self._rows():
            try:
                kind, name = parsed if parsed is not None else parse_target(target)
                if kind == "self":      # an agent action's target (PHASE5 AB-2)
                    raise ExpressionError("self.<attribute> is only valid in an agent action")
                value = fn(scope) if fn is not None else evaluate_expression(self, expr, entity)
                if kind == "agent":     # H1: entity.agent.<attr>, the agent's own column
                    population, index = entity_agent(ExpressionScope(ctx=self.ctx, entity=entity))
                    self.ctx.agent_system.set_attr(population, index, name, value)
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
                    self.ctx.variables[name] = value
                    wrote_variable = True
                    if self.ctx.watched:        # an event-driven HOLD reads it (O4)
                        self.ctx.touch(("var", name))
            except (ExpressionError, ValueError) as exc:
                raise ExpressionError(f"Assign block '{self.name}': {target} = {expr}: {exc}") from None
        # A flow rate may read a model variable (PHASE4 D1), so writing one is
        # one of the discrete events that re-evaluates the rates. The check
        # costs one `is None` on a model with no levels, which is every model
        # written before this existed.
        if wrote_variable and self.ctx.level_system is not None:
            self.ctx.level_system.trigger()
        if wrote_agent:     # D129: settled now, so the next block sees the new state
            self.ctx.agent_system.settle()
        elif wrote_variable and self.ctx.agent_system is not None:   # PHASE5 AB-1: conditions
            self.ctx.agent_system.trigger()
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
