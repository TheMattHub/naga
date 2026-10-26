"""BATCH: group arrivals into one batch entity (PHASE4 B6 adds `max_wait`;
Phase 15 M2 the carrier mode, D819)."""
import bisect
import logging
import math

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import absorb_member, make_batch_entity, member_time_of
from nagasim.core.expressions import ExpressionError, compile_expression
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Batch", "SPEC"]


class Batch(BaseProcess):
    """Groups parts or orders into batches, pallets or lots of a set size, or after a maximum wait, and sends the batch on as one entity.

    Groups entities by count: puts each arrival in the block's store and,
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

    With `carrier_type` set, an arrival of that type is a CARRIER (a truck): it
    is not stored, and loads the stored arrivals one at a time, `member_time`
    each, until it holds `carrier_capacity` (default `batch_size`) or its
    `max_wait`, counted from its own arrival, has passed; then it leaves as
    itself, its members aboard.
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
        self._due = None        # D433: when the armed timer falls due, for a state
        # Carrier mode (D819): "" = a plain BATCH. Otherwise an arrival of this
        # type is a carrier that loads members until full or its cut-off.
        self.carrier_type = ""
        self.carrier_capacity = ""
        self.member_time = ""
        self.member_time_unit = "base"
        self._member_factor = 1.0
        # Carriers waiting for a member, in ARRIVAL order: [(seq, event)]. A
        # member goes to the first, so the oldest truck fills first.
        self._idle = []
        self._carrier_seq = 0

    def self_process(self, entity):
        if self.ctx.debug:
            log.debug("Entity %s arrives at the batch at time %s.", entity.id, self.env.now)
        if self.carrier_type:
            if entity.e_type == self.carrier_type:
                yield from self._load(entity)
                return
            put = self.store.put(entity)
            # The store is unbounded, so the member is in `items` already: hand
            # it to an idle carrier in this same step, before anything else of
            # the instant runs (the cut-off's zero-delay re-check relies on it).
            self._dispatch()
            yield put
            if entity.id in self.store._entry_times:
                self.store._log_state(event_type="store")
            return
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
                    self._due = self.env.now + patience
                    self.env.process(self._release_partial(self._timer, patience))
            return

        # The threshold test and the removal happen in one step, so two arrivals
        # at the same timestamp can no longer form a batch from the same items
        # (DEVLOG #29).
        self._timer = None              # a full batch cancels the pending wait
        self._emit(self.store.take(self.batch_size))

    def _capacity(self, carrier) -> int:
        """How many members *carrier* holds when full: `carrier_capacity` in its
        own scope (trucks differ), else `batch_size`."""
        if not self.carrier_capacity:
            return int(self.batch_size)
        try:
            value = float(compile_expression(self.carrier_capacity).evaluate(
                self.expression_scope(carrier)))
        except (ExpressionError, TypeError, ValueError) as exc:
            raise ExpressionError(f"Block '{self.name}': carrier_capacity: {exc}") from None
        if value < 0:
            raise ValueError(f"E_BATCH_CARRIER_CAPACITY: block '{self.name}': carrier {carrier.id} "
                             f"has a capacity of {value:g}; it must be 0 or more")
        return int(math.floor(value))

    def _dispatch(self):
        """Give waiting members, oldest first, to idle carriers, oldest first."""
        while self._idle and self.store.items:
            _seq, wake = self._idle.pop(0)
            wake.succeed(self.store.take(1)[0])

    def _load(self, carrier):
        """Carrier mode (D819, D824): load one member at a time until full, or
        until the cut-off -- no member is started after it, a load in progress
        finishes, and an idle carrier takes a member present at the cut-off
        instant (the plain BATCH's zero-delay tie rule)."""
        self._carrier_seq += 1
        seq = self._carrier_seq
        capacity = self._capacity(carrier)
        patience = self._patience(carrier) if self.max_wait else 0.0
        deadline = self.env.now + patience if patience > 0 else None
        while len(carrier.batch_elems) < capacity:
            # AT the cut-off the wait below fires at once and still re-checks.
            if deadline is not None and self.env.now > deadline:
                break
            wake = self.env.event()
            bisect.insort(self._idle, (seq, wake), key=lambda item: item[0])
            self._dispatch()
            if not wake.triggered:
                if deadline is None:
                    yield wake
                else:
                    # A fresh cut-off timeout per wait, left on the heap until the
                    # deadline: one created once would sort ahead of events of the
                    # cut-off instant and could move the tie rule below.
                    yield wake | timeout_at(self.env, deadline)
                    if not wake.triggered:
                        yield self.env.timeout(0)       # tie rule: a member of this instant wins
                    if not wake.triggered:
                        self._idle.remove((seq, wake))
                        break
            member = wake.value
            wait = member_time_of(self, self.member_time, self._member_factor, member)
            if wait > 0:
                yield self.env.timeout(wait)
            absorb_member(self, member, carrier)
        self.forward(carrier, 0)

    def _patience(self, entity) -> float:
        """`max_wait` in the model's base unit, evaluated for the arrival that
        starts the group -- the same shape as SEIZE's `renege_after`."""
        try:
            value = float(compile_expression(self.max_wait).evaluate(
                self.expression_scope(entity)))
        except (ExpressionError, TypeError, ValueError) as exc:
            raise ExpressionError(f"Block '{self.name}': max_wait: {exc}") from None
        return max(scaled(value, self._wait_factor), 0.0)

    def resume_timer(self, due: float) -> None:
        """A state put back a running maximum wait (D433): it falls due at *due*, and the
        parts arriving again find it armed and arm none."""
        self._timer, self._due = object(), due
        self.env.process(self._release_partial(self._timer, None, due))

    def _release_partial(self, token, patience, due=None):
        yield self.env.timeout(patience) if due is None else timeout_at(self.env, due)
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
            "carrier_type": self.carrier_type,
            "carrier_capacity": self.carrier_capacity,
            "member_time": self.member_time,
            "member_time_unit": self.member_time_unit,
        }

    def set_params_from_dict(self, param_dict):
        self.batch_size = param_dict.get("batch_size", 2)
        self.batched_entity_type = param_dict.get("batched_entity_type", "BATCH")
        self.max_wait = param_dict.get("max_wait", "")
        self.max_wait_time_unit = param_dict.get("max_wait_time_unit", "base")
        self._wait_factor = field_factor(param(SPEC, "max_wait"), param_dict, self.ctx.units,
                                         SPEC.params)
        self.carrier_type = str(param_dict.get("carrier_type", "") or "").strip()
        self.carrier_capacity = str(param_dict.get("carrier_capacity", "") or "").strip()
        self.member_time = str(param_dict.get("member_time", "") or "").strip()
        self.member_time_unit = param_dict.get("member_time_unit", "base")
        self._member_factor = field_factor(param(SPEC, "member_time"), param_dict, self.ctx.units,
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
        ParamSpec("carrier_type", str, "",
                  description="Carrier mode: an arriving entity of this type (a truck) is a CARRIER "
                              "that waits here and loads the other arrivals one at a time, until it "
                              "is full or its max_wait (the cut-off, measured from the carrier's "
                              "arrival) runs out, then leaves as itself. Empty = a plain BATCH.",
                  quantity="none"),
        ParamSpec("carrier_capacity", str, "",
                  description="Carrier mode: how many members a carrier takes, e.g. \"entity.capacity\", "
                              "evaluated for each carrier when it arrives. Empty = batch_size.",
                  quantity="count", kinds=("number", "expression"), min=0),
        ParamSpec("member_time", str, "",
                  description="Carrier mode: time to load one member, evaluated in the member's own "
                              "scope when its load starts. Empty = loading takes no time.",
                  quantity="time", kinds=("number", "expression"), min=0, unit_key="member_time_unit"),
        ParamSpec("member_time_unit", str, "base",
                  description="Unit `member_time` is expressed in: \"base\" (the model's own unit) or "
                              "one of seconds/minutes/hours/days.", quantity="none", kinds=("choice",)),
    ),
    factory=lambda env, block: Batch(env, block, store=None, batch_size=2, batched_entity_type="Batch"),
    family="group",
    holding=True,
)
