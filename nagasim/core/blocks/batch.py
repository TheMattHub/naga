"""BATCH: group arrivals into one batch entity (PHASE4 B6 adds `max_wait`;
Phase 15 M2 the carrier mode, D819)."""
import bisect
import logging
import math

import simpy

from nagasim.core import beltpick, pallets
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import absorb_member, make_batch_entity, member_time_of, robot_params
from nagasim.core.expressions import ExpressionError, compile_expression
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.timeunits import base_factor
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Batch", "SPEC"]

# A robot BATCH traces its cycle phases (blocks in 3D, Task 5). A switch only so a test can
# run without them and show the results do not change.
EMIT_PHASES = True


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

    With a `pattern` too, the carrier is a pallet: each member takes the next
    slot of that pallet pattern, and the pallet closes when its slots are full
    or the next member would pass its weight limit (that member waits for the
    next pallet). The members carry `slot` and `layer`; the pallet carries
    `items`, `layers`, `load_weight`, `load_height` and `pattern`.
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
        # Palletizing (D1346): "" = no pattern, the carrier path above unchanged.
        self.pattern = ""
        self.place_per = "item"
        self.robot = ""
        # {waiting carrier's wake event: accepts(member)}: a pallet refuses a member that
        # would pass its weight limit before the member leaves the store.
        self._accepts = {}
        self._slots = {}        # {pattern name: its slots}
        self._place = simpy.Resource(env, 1)    # a robot's one pallet place (#1992)

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

    def ready_now(self) -> bool:
        """The robot would start on a part handed to it now: a pallet holds the place and
        waits for a member, and none is stored. What a belt's "pass" pick reads (D1343)."""
        return bool(self._idle) and not self.store.items

    def _dispatch(self):
        """Give waiting members, oldest first, to idle carriers, oldest first."""
        while self._idle and self.store.items:
            _seq, wake = self._idle.pop(0)
            accept = self._accepts.pop(wake, None) if self._accepts else None
            if accept is not None and not accept(self.store.items[0]):
                wake.succeed(None)          # the pallet closes; the member stays for the next
                continue
            wake.succeed(self.store.take(1)[0])

    def _take_member(self, seq, deadline, accept=None):
        """The next member for carrier *seq*, or None at the cut-off (or when *accept*
        refuses the next one): it waits idle until one is handed over."""
        wake = self.env.event()
        bisect.insort(self._idle, (seq, wake), key=lambda item: item[0])
        if accept is not None:
            self._accepts[wake] = accept
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
                    self._accepts.pop(wake, None)
                    return None
        return wake.value

    def _load(self, carrier):
        """Carrier mode (D819, D824): load one member at a time until full, or
        until the cut-off -- no member is started after it, a load in progress
        finishes, and an idle carrier takes a member present at the cut-off
        instant (the plain BATCH's zero-delay tie rule)."""
        self._carrier_seq += 1
        seq = self._carrier_seq
        if self.pattern:
            yield from self._load_pallet(carrier, seq)
            return
        capacity = self._capacity(carrier)
        patience = self._patience(carrier) if self.max_wait else 0.0
        deadline = self.env.now + patience if patience > 0 else None
        while len(carrier.batch_elems) < capacity:
            # AT the cut-off the wait below fires at once and still re-checks.
            if deadline is not None and self.env.now > deadline:
                break
            member = yield from self._take_member(seq, deadline)
            if member is None:
                break
            wait = member_time_of(self, self.member_time, self._member_factor, member)
            if wait > 0:
                yield self.env.timeout(wait)
            absorb_member(self, member, carrier)
        self.forward(carrier, 0)

    # -- palletizing (D1346) -------------------------------------------------------------
    def _pattern_of(self, carrier) -> tuple[str, dict]:
        """The pattern record *carrier* is loaded by: `pattern` named outright, or
        evaluated in the carrier's scope."""
        records = self.ctx.pallet_patterns
        name = pallets.literal_pattern(self.pattern, records)
        if name is None:
            try:
                name = str(compile_expression(self.pattern).evaluate(self.expression_scope(carrier)))
            except (ExpressionError, TypeError, ValueError) as exc:
                raise ExpressionError(f"Block '{self.name}': pattern: {exc}") from None
        if name not in records:
            raise ValueError(f"E_PALLET_UNKNOWN: block '{self.name}': carrier {carrier.id} names "
                             f"pallet pattern '{name}', which the model does not declare")
        if name not in self._slots:
            self._slots[name] = pallets.slots(records[name])
        return name, records[name]

    def _next_slot(self, carrier, name: str, taken: int = 0):
        """The slot the next member takes (*taken* more already in hand), or None when
        the pallet is full."""
        places = self._slots[name]
        k = len(carrier.batch_elems) + taken
        return places[k] if k < len(places) else None

    def _load_pallet(self, carrier, seq):
        """Load *carrier* slot by slot in its pattern's order, one member time per item
        (or per layer), the sheet time before each layer after the first. It closes
        when its slots are full, the next member would pass the weight limit, or at
        the cut-off; a partial layer then still takes its one member time."""
        # #1992: one robot works at one pallet place (this block's), so its pallets take turns;
        # a waiting pallet's cut-off counts from when it takes the place.
        turn = self._place.request() if self.robot else None
        if turn is not None:
            yield turn
        name, record = self._pattern_of(carrier)
        limit = pallets.weight_limit(record)
        tare = pallets.tare(record)
        weight = [tare + sum(pallets.member_kg(m, record) for m in carrier.batch_elems)]

        def accepts(member) -> bool:
            kg = pallets.member_kg(member, record)
            if limit is not None and tare + kg > limit + 1e-9:
                raise ValueError(f"E_PALLET_ITEM: block '{self.name}': item {member.id} weighs "
                                 f"{kg:g} kg; with the tare of {tare:g} kg no pallet of pattern "
                                 f"'{name}' takes it under its weight limit of {limit:g} kg")
            if limit is not None and weight[0] + kg > limit + 1e-9:
                return False
            weight[0] += kg
            return True

        carrier.pattern = name
        pallets.set_load(carrier, record, weight[0])
        patience = self._patience(carrier) if self.max_wait else 0.0
        deadline = self.env.now + patience if patience > 0 else None
        closed = False
        while not closed:
            first = self._next_slot(carrier, name)
            if first is None:
                break
            # Layer at a time: every slot left in this layer is filled before its member time.
            batch = []
            while not batch or self.place_per == "layer":
                slot = self._next_slot(carrier, name, len(batch))
                if slot is None or slot.layer != first.layer:
                    break
                if deadline is not None and self.env.now > deadline:
                    closed = True
                    break
                member = yield from self._take_member(seq, deadline, accepts)
                if member is None:
                    closed = True
                    break
                batch.append((member, slot))
            if not batch:
                break
            wait = self._slot_time(name, record, first, batch[0][0])
            meet = self.ctx.belt_meets.get(self.name) if self.robot else None
            phases = self._phases_of(first)
            if meet is not None:        # one box at a time (E_ROBOT_INTERCEPT case layer)
                yield from self._pick_from_belt(meet, self._sheet(name, record, first), wait,
                                                batch[0][0], phases)
            elif phases is not None:
                # The slot's own end event is made where it always was, so ties at its end
                # keep their order. Segment 0 takes the sheet time too, as on the belt.
                done = self.env.timeout(wait) if wait > 0 else None
                phases[0] = (phases[0][0], phases[0][1] + self._sheet(name, record, first))
                yield from self._trace_phases(batch[0][0], phases, 0, self.env.now,
                                              self.env.now + wait, done,
                                              [member for member, _slot in batch[1:]])
            elif wait > 0:
                yield self.env.timeout(wait)
            for member, slot in batch:
                member.slot, member.layer = slot.index, slot.layer
                absorb_member(self, member, carrier)
            pallets.set_load(carrier, record, weight[0])
        if turn is not None:
            self._place.release(turn)
        self.forward(carrier, 0)

    def _pick_from_belt(self, meet, sheet: float, wait: float, member, phases=None):
        """Generator, D1343: the robot meets *member* on the moving belt. The slot's time
        *wait* (the *sheet* time, then the constant cycle) is split at the approach: the sheet
        time, the approach, the wait at the meeting point for the part, the rest. With
        *phases*, segment 0 is traced over the sheet time and the approach, the wait at the
        meeting point traces nothing, and the other segments share the rest."""
        approach = meet.approach * base_factor("time", "seconds", self.ctx.units)
        if phases:
            self.emit("cycle_phase", entity=member, state=phases[0][0], value=0.0,
                      duration=sheet + approach if sheet > 0 else approach)
        if sheet > 0:
            yield self.env.timeout(sheet)
        beltpick.robot_starts(member, self.env.now + approach)
        yield self.env.timeout(approach)
        yield from beltpick.robot_arrives(self.env, member)
        rest = wait - sheet - approach
        done = self.env.timeout(rest) if rest > 0 else None
        if phases and len(phases) > 1:
            planned = sum(seconds for _kind, seconds in phases[1:])
            share = max(rest, 0.0) / planned if planned > 0 else 0.0
            yield from self._trace_phases(member, [(k, s * share) for k, s in phases[1:]], 1,
                                          self.env.now, self.env.now + max(rest, 0.0), done)
        elif done is not None:
            yield done

    def _phases_of(self, slot):
        """((segment kind, model time), ...) of a robot slot's plan, or None."""
        plans = self.ctx.pallet_phases.get(self.name) if self.robot and EMIT_PHASES else None
        plan = plans[slot.index - 1] if plans and slot.index - 1 < len(plans) else None
        if plan is None:
            return None
        to_model = base_factor("time", "seconds", self.ctx.units)
        return [(kind, seconds * to_model) for kind, seconds in plan]

    def _trace_phases(self, member, phases, first: int, start: float, end: float, done,
                      others=()):
        """Generator: trace each of *phases* ((kind, model time), indices from *first*) as
        `cycle_phase` at its start, from *start*; the last one lasts until *end*, when the
        event *done* fires (None: no time passes, the phases are traced at once). *others*:
        the layer's further members (`place_per: layer`)."""
        t = start
        for k, (kind, span) in enumerate(phases):
            if done is not None and t > self.env.now:
                # Never past the slot's end: the end stays the one event it always was.
                yield done if t >= end else timeout_at(self.env, t)
            last = k == len(phases) - 1
            if kind == "place":
                # Playback counts boxes by `place` events (the 3D stack), so each further
                # member of a layer gets a zero-length one; *member*'s comes last and stays
                # the block's running phase.
                for other in others:
                    self.emit("cycle_phase", entity=other, state=kind, value=float(first + k),
                              duration=0.0)
            self.emit("cycle_phase", entity=member, state=kind, value=float(first + k),
                      duration=max(end - self.env.now, 0.0) if last and done is not None
                      else span)
            t += span
        if done is not None:
            yield done

    def _sheet(self, name: str, record: dict, slot) -> float:
        """The sheet time before *slot* when it opens a layer after the first, in the
        model's own time unit; else 0."""
        if slot.layer > 1 and pallets.opens_layer(self._slots[name], slot):
            return pallets.sheet_time(record)
        return 0.0

    def _slot_time(self, name: str, record: dict, slot, member) -> float:
        """The time to place *member* (or the layer it starts) at *slot*: the sheet time
        when it opens a layer after the first, plus its member time."""
        wait = self._sheet(name, record, slot)
        if self.robot:
            return wait + self.ctx.pallet_cycles[self.name][slot.index - 1] * base_factor(
                "time", "seconds", self.ctx.units)
        return wait + member_time_of(self, self.member_time, self._member_factor, member)

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
            "pattern": self.pattern,
            "place_per": self.place_per,
            "robot": self.robot,
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
        self.pattern = str(param_dict.get("pattern", "") or "").strip() if self.carrier_type else ""
        self.place_per = str(param_dict.get("place_per", "item") or "item")
        self.robot = str(param_dict.get("robot", "") or "").strip() if self.pattern else ""
        if self.place_per not in pallets.PLACE_PER:
            raise ValueError(f"BATCH block '{self.name}': place_per must be one of "
                             f"{pallets.PLACE_PER}, not {self.place_per!r}")


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
        ParamSpec("pattern", str, "",
                  description="Carrier mode: the carrier is a pallet loaded by this record of "
                              "\"pallet_patterns\", named outright (\"Euro\") or chosen per pallet "
                              "by an expression in the carrier's scope. Each member takes the next "
                              "slot; the pallet closes at the pattern's limits, and carrier_capacity "
                              "is not used. Empty = no pattern.",
                  quantity="none"),
        ParamSpec("place_per", str, "item",
                  description="With a pattern: \"item\" loads one member per member_time; "
                              "\"layer\" waits for a whole layer and places it in one member_time "
                              "(a layer-forming palletizer).", quantity="none", kinds=("choice",)),
        *robot_params("BATCH"),
    ),
    factory=lambda env, block: Batch(env, block, store=None, batch_size=2, batched_entity_type="Batch"),
    family="group",
    holding=True,
)
