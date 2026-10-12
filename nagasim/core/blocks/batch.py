"""BATCH: group arrivals into one batch entity (PHASE4 B6 adds `max_wait`)."""
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import make_batch_entity
from nagasim.core.expressions import ExpressionError, compile_expression
from nagasim.core.quantities import field_factor, param, scaled

log = logging.getLogger(__name__)

__all__ = ["Batch", "SPEC"]


class Batch(BaseProcess):
    """Groups entities by count: puts each arrival in the block's store and,
    as soon as `batch_size` items are waiting, takes exactly that many
    atomically to form one batch entity. Members leave live_entities and carry
    `in_batch` until a SEPARATE splits the batch again.

    With `max_wait` set, a PARTIAL batch leaves too: the timer is armed by the
    arrival that starts a new group and releases whatever is waiting once the
    oldest member has waited that long. TIE RULE: a batch
    that fills in the same instant as the timer expires wins and the timer
    releases nothing -- the same zero-delay re-check reneging uses, so
    the outcome never depends on the order SimPy happened to schedule two
    events at one timestamp.
    """

    def __init__(self,env, block, store, batch_size, batched_entity_type, next_process=None):
        super().__init__(env, block, next_process)
        self.store = store
        self.batch_size = batch_size
        self.batched_entity_type = batched_entity_type
        self.max_wait = ""
        self.max_wait_time_unit = "base"
        self._wait_factor = 1.0
        # Identity of the group the armed timer belongs to; None means no timer
        # is waiting for anything (see the tie rule above).
        self._timer = None

    def self_process(self, entity):
        if self.ctx.debug:
            log.debug("Entity %s arrives at the batch at time %s.", entity.id, self.env.now)
        yield self.store.put(entity)

        # Arrivals of one instant are all stored before any of them resumes, so
        # an earlier one may already have taken this entity into its batch.
        # Nothing is left to do for it: a timer armed here would belong to no
        # group and release the next one early (DEVLOG #343).
        if entity.id not in self.store._entry_times:
            return

        if len(self.store.items) < self.batch_size:
            self.store._log_state(event_type="store")
            if self.max_wait and self._timer is None:
                patience = self._patience(entity)
                # 0 (or less) is "no maximum wait", the same convention the
                # library's mtbf uses for "never": a zero-length wait would
                # mean a batch of one, which is never what a BATCH is for.
                if patience > 0:
                    self._timer = object()
                    self.env.process(self._release_partial(self._timer, patience))
            return

        # The threshold test and the removal happen in one step, so two arrivals
        # at the same timestamp can no longer form a batch from the same items
        # (DEVLOG #29).
        self._timer = None              # a full batch cancels the pending wait
        self._emit(self.store.take(self.batch_size))

    def _patience(self, entity) -> float:
        """`max_wait` in the model's base unit, evaluated for the arrival that
        starts the group -- the same shape as SEIZE's `renege_after`."""
        try:
            value = float(compile_expression(self.max_wait).evaluate(
                self.expression_scope(entity)))
        except (ExpressionError, TypeError, ValueError) as exc:
            raise ExpressionError(f"Block '{self.name}': max_wait: {exc}") from None
        return max(scaled(value, self._wait_factor), 0.0)

    def _release_partial(self, token, patience):
        yield self.env.timeout(patience)
        yield self.env.timeout(0)       # tie rule: a batch filled in this instant wins
        if self._timer is not token or not self.store.items:
            return
        self._timer = None
        self._emit(self.store.take(len(self.store.items)))

    def _emit(self, members):
        batched_entity = make_batch_entity(self, members, self.batched_entity_type)
        if self.ctx.debug:
            log.debug("Created batch entity %s with %s items at time %s",
                      batched_entity.id, len(members), self.env.now)
        self.forward(batched_entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "batch_size": self.batch_size,
            "batched_entity_type": self.batched_entity_type,
            "max_wait": self.max_wait,
            "max_wait_time_unit": self.max_wait_time_unit,
        }

    def set_params_from_dict(self, param_dict):
        self.batch_size = param_dict.get("batch_size", 2)
        self.batched_entity_type = param_dict.get("batched_entity_type", "BATCH")
        self.max_wait = param_dict.get("max_wait", "")
        self.max_wait_time_unit = param_dict.get("max_wait_time_unit", "base")
        self._wait_factor = field_factor(param(SPEC, "max_wait"), param_dict, self.ctx.units,
                                         SPEC.params)


SPEC = BlockSpec(
    type_name="BATCH", cls=Batch,
    params=(
        ParamSpec("batch_size", int, 2, description="Number of entities grouped into one batch entity.", quantity="count", min=1),
        ParamSpec("batched_entity_type", str, "BATCH", description="Entity type assigned to the batch entity.", quantity="none"),
        ParamSpec("max_wait", str, "",
                  description="How long a partial batch waits before it leaves anyway, e.g. \"30\" "
                              "or \"entity.deadline\"; measured from the arrival that started the "
                              "group. A batch that fills in the same instant wins. Empty, 0 or "
                              "less = wait for a full batch for ever.", quantity="time", kinds=("number", "expression"), min=0, unit_key="max_wait_time_unit"),
        ParamSpec("max_wait_time_unit", str, "base",
                  description="Unit `max_wait` is expressed in: \"base\" (the model's own unit) or "
                              "one of seconds/minutes/hours/days.", quantity="none", kinds=("choice",)),
    ),
    factory=lambda env, block: Batch(env, block, store=None, batch_size=2, batched_entity_type="Batch"),
    family="group",
    holding=True,
)
