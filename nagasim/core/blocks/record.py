import logging

from nagasim.core import costs
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import compiled_closure, entity_scope, evaluate_expression
from nagasim.core.stats import RECORD_KINDS, UserStat

log = logging.getLogger(__name__)

__all__ = ["Record", "SPEC", "KINDS"]

KINDS = RECORD_KINDS


class Record(BaseProcess):
    """Counts or measures something as entities pass (a count, a time between arrivals, a defect, a cost) in a user-named statistic, without delaying them.

    A zero-time block that updates a
    user-defined statistic and forwards the entity unchanged.

    Statistics live on the simulation context (`ctx.user_stats`), keyed by
    `stat_name`, so two RECORD blocks writing to the same name share one
    collector -- core/schema/ rejects a same-name pair with different `kind`s at
    load time, before a run can hit the mismatch here.

    `expr` is the increment for "count", the observed value for "tally", the
    new level for "time_persistent", and ignored for "time_between" (it
    tallies the gap since the statistic's last arrival; the first arrival
    records nothing).

    `kind="cost"` is the one kind that builds no collector: `expr`
    is a MONEY amount added to the cost item `stat_name` every time an entity
    passes -- an ordering cost, a changeover cost, a lost-sale penalty. It
    joins the model's Total Cost and is itemised in the `cost_summary` frame.
    """

    def __init__(self, env, block, next_process=None, stat_name="", kind="count", expr="1",
                 category=""):
        super().__init__(env, block, next_process)
        self.stat_name = stat_name
        self.kind = kind
        self.expr = expr
        self.category = category
        self._compiled = None
        self._stat = self._get_or_create_stat() if stat_name else None

    def _get_or_create_stat(self) -> UserStat | None:
        if self.kind == "cost":
            # Created eagerly, like a collector: a cost item no entity ever
            # reached is a 0 row, not a missing KPI key.
            costs.accrue(self.ctx, self.stat_name, 0.0, self.category)
            return None
        stats = self.ctx.user_stats
        existing = stats.get(self.stat_name)
        if existing is None:
            existing = UserStat.new(self.stat_name, self.kind, self.env.now)
            stats[self.stat_name] = existing
        elif existing.kind != self.kind:
            # validation already rejects this at load time; a block built
            # directly (bypassing the schema, e.g. in a test) still fails loud
            # rather than silently mixing two kinds into one collector.
            raise ValueError(
                f"Record block '{self.name}': statistic '{self.stat_name}' is already "
                f"kind '{existing.kind}', cannot also be '{self.kind}'")
        return existing

    def _value(self, entity):
        """`expr` evaluated for *entity*. The closure is compiled once and keyed
        on the text, so an edited `expr` recompiles; one that does not compile
        goes through `evaluate_expression` and raises its original error."""
        if self._compiled is None or self._compiled[0] != self.expr:
            self._compiled = (self.expr, compiled_closure(self.expr))
        fn = self._compiled[1]
        if fn is None:
            return evaluate_expression(self, self.expr, entity)
        return fn(entity_scope(self, entity))

    def self_process(self, entity):
        now = self.env.now
        if self.kind == "cost":
            costs.accrue(self.ctx, self.stat_name, self._value(entity), self.category)
            if self.ctx.debug:
                log.debug("Entity %s charged cost '%s' at time %s.", entity.id, self.stat_name, now)
            self.forward(entity, 0)
            return

        if self._stat is None:
            self._stat = self._get_or_create_stat()

        if self.kind == "time_between":
            if self._stat.last_arrival is not None:
                self._stat.collector.add(now - self._stat.last_arrival)
            self._stat.last_arrival = now
        elif self.kind == "time_persistent":
            self._stat.collector.set(self._value(entity), now)
        else:  # count, tally
            self._stat.collector.add(self._value(entity))

        if self.ctx.debug:
            log.debug("Entity %s recorded '%s' (%s) at time %s.", entity.id, self.stat_name, self.kind, now)
        self.forward(entity, 0)

    def self_run(self, entity):
        self.self_process(entity)

    def get_params_dict(self):
        return {"stat_name": self.stat_name, "kind": self.kind, "expr": self.expr,
                "category": self.category}

    def set_params_from_dict(self, param_dict):
        self.stat_name = param_dict.get("stat_name", self.stat_name)
        self.kind = param_dict.get("kind", self.kind)
        self.expr = param_dict.get("expr", self.expr)
        self.category = param_dict.get("category", self.category)
        self._stat = self._get_or_create_stat() if self.stat_name else None


SPEC = BlockSpec(
    type_name="RECORD", cls=Record,
    params=(
        ParamSpec("stat_name", str, "", required=True,
                  description="Name of the user statistic; two RECORD blocks with the same name share one collector.", quantity="none"),
        ParamSpec("kind", str, "count",
                  description="\"count\" (running total of expr), \"tally\" (mean of expr), \"time_between\" "
                              "(mean gap between arrivals), \"time_persistent\" (time-average level of expr) "
                              "or \"cost\" (adds expr to a named cost item and to Total Cost).", quantity="none"),
        ParamSpec("expr", str, "1", description="Expression evaluated at each arrival; ignored for kind=time_between.", quantity="none", kinds=("expression",)),
        ParamSpec("category", str, "",
                  description="Cost category this item is grouped under in the cost_summary frame; "
                              "used by kind=cost only.", quantity="none"),
    ),
    factory=lambda env, block: Record(env, block),
    family="stats", shape="record",
    transparent=True,
)
