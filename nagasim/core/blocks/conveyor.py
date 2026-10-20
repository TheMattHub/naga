"""CONVEYOR: a belt with a length, a speed and a spacing (PHASE4 E1).

A conveyor is not a DELAY: it holds a bounded number of entities, it will not
accept one before the entry zone has cleared, and it reacts to a blocked exit.
All three are exact, event-driven consequences of three numbers -- `length`,
`speed`, `spacing` -- and nothing here steps time or simulates positions.

    capacity        = floor(length / spacing) cells, at least 1
    travel time     = length / speed
    entry spacing   = consecutive entries are at least spacing / speed apart,
                      so the belt can never feed faster than speed / spacing

**What blocks the entry.** The cells ARE a finite space (PHASE4 T7,
ARCHITECTURE D78): a machine that has finished a part keeps holding it -- and
its resource -- until the belt has a cell free and the entry rhythm lets the
part on. A full or stopped belt therefore stops the station feeding it, instead
of queueing invisibly in front of it.

**What blocks the exit.** Two things, and a part leaves only once both hold.
A DOWNSTREAM BELT (PHASE6 W3 M6, D115): when the belt's walk through
transparent blocks (DECIDE, ASSIGN, RECORD, STATION, LOGIC) reaches another
CONVEYOR, its end blocks after service like a station (the D78 token) -- the
end part keeps its cell until the next belt physically takes it, a DECIDE
divert blocks on a full chosen branch, and belts merging on one entry queue
FIFO on its cells. Any other downstream block accepts on arrival, as before.
And the end stop, `exit_resource`, taken FIRST and held while the part waits
for the next belt. When set, an entity at the end must take one unit before
leaving, and it CARRIES it onward, so the natural pairing is a PROCESS with
action "Delay Release" (or a plain DELAY + RELEASE) on the same resource at the
far end. With neither, the exit never blocks and the conveyor is a delay with
a capacity and an entry rhythm, which is what a free-running belt is.

**The two modes**, once the exit is blocked:

* `accumulating` (default): the blocked entity stops at the end, the ones
  behind keep moving until they pack against it, each taking `spacing` of belt.
  A blocked entity releases its follower `spacing / speed` after it leaves --
  the time to close the gap it left.
* non-accumulating: the whole belt STOPS. Nobody moves, nothing new gets on,
  and every entity's remaining travel resumes exactly where it stopped.

Ceiling (ARCHITECTURE D68, kept by D115): only the entity at the END can block
the belt, so a mid-belt divert ends the section there: one conveyor per zone,
now linked belt to belt with back-pressure.
"""
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.blocking import Space, accept, arm, check_deadlock
from nagasim.core.seizing import acquire
from nagasim.core.stats import Tally, TimeWeighted
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Conveyor", "SPEC"]

_EPS = 1e-9


class Conveyor(BaseProcess):
    """Conveyor belt that carries parts from one end to the other at a set speed, holds a limited number, and blocks the machine before it when full."""

    # The token rides through (PHASE4 T7): a belt accepts the part when it is
    # physically on it, not when it is offered -- so a full or stopped belt
    # leaves the upstream machine blocked (ARCHITECTURE D78).
    space_passthrough = True

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.length = 10.0
        self.speed = 1.0
        self.spacing = 1.0
        self.accumulating = True
        self.exit_resource = ""
        self.from_node = ""
        self.to_node = ""
        # Built lazily: the cell space needs the resolved length, which may come
        # from the network, which is only there once the model is loaded.
        self._space = None
        self._next_entry = 0.0
        self._prev_clear = None
        # Non-accumulating belt state: a fresh `stop` event while it runs, a
        # fresh `resume` event while it is stopped.
        self._stopped = False
        self._stop_event = None
        self._resume_event = None
        # The end part waits for a downstream belt to take it (D115); what the
        # deadlock check reads, since `blocked` also covers the exit_resource wait.
        self.held_downstream = False
        self.entered = 0
        self.exited = 0
        self.occupancy = TimeWeighted(env.now)
        self.blocked = TimeWeighted(env.now, level=0.0)
        self.transit = Tally()
        env.ctx.conveyors[self.name] = self

    # -- geometry ------------------------------------------------------------
    @property
    def belt_length(self) -> float:
        """The declared length, or the network distance between its endpoints."""
        if self.length and self.length > 0:
            return float(self.length)
        network = self.ctx.network
        places = self.ctx.places
        a, b = places.get(self.from_node, self.from_node), places.get(self.to_node, self.to_node)
        if network and a and b and network.has(a) and network.has(b):
            return network.distance(a, b)
        raise RuntimeError(f"Conveyor '{self.name}': no length. Set `length`, or give it "
                           f"`from_node`/`to_node` on the path network.")

    @property
    def capacity(self) -> int:
        return max(int(self.belt_length // self.spacing), 1)

    @property
    def travel_time(self) -> float:
        return self.belt_length / self.speed

    @property
    def space(self):
        """The belt's cells as a `blocking.Space` (PHASE4 T7): a full belt is a
        finite space, so the machine feeding it blocks after service exactly as
        it does on a full BUFFER (ARCHITECTURE D78). Built on first use, which
        is `blocking.analyse` -- by then the network that may give the belt its
        length is built."""
        if self._space is None:
            self._space = Space(self.env, self.capacity, self)
        return self._space

    # -- the belt clock (non-accumulating only) ------------------------------
    @property
    def stopped(self) -> bool:
        """A stopped non-accumulating belt takes nothing on (the deadlock check)."""
        return self._stopped

    def _end_blocked(self, on: bool) -> None:
        """The end part cannot leave: an accumulating belt records it, a
        non-accumulating one stops (and records it)."""
        if self.accumulating:
            self.blocked.set(1.0 if on else 0.0, self.env.now)
        elif on:
            self._stop_belt()
        else:
            self._start_belt()

    def _stop_belt(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.blocked.set(1.0, self.env.now)
        self._resume_event = self.env.event()
        if self._stop_event is not None and not self._stop_event.triggered:
            self._stop_event.succeed()

    def _start_belt(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self.blocked.set(0.0, self.env.now)
        self._stop_event = self.env.event()
        if self._resume_event is not None and not self._resume_event.triggered:
            self._resume_event.succeed()

    def _ride(self, distance_time: float):
        """Generator: spend *distance_time* of BELT time, pausing while stopped."""
        if self.accumulating:
            yield self.env.timeout(distance_time)
            return
        if self._stop_event is None:
            self._stop_event = self.env.event()
        remaining = distance_time
        while remaining > _EPS:
            if self._stopped:
                yield self._resume_event
                continue
            started, gate = self.env.now, self._stop_event
            yield self.env.timeout(remaining) | gate
            remaining -= self.env.now - started

    # -- the ride ------------------------------------------------------------
    # Each stage writes where the part is on the belt as `entity._belt` = (stage, the time
    # it ends or None, when it entered or None), for a state (D429, the D383 pattern).
    def self_process(self, entity):
        spacing_time = self.spacing / self.speed
        entity._belt = ("cell", None, None)
        # A free cell on the belt. The upstream machine is NOT released yet:
        # the part is only off it once it is physically on the belt, which is
        # after the stopped-belt wait and the entry rhythm below (PHASE4 T7).
        cell = yield from self.space.take(entity, accepts=False)
        if not self.accumulating and self._stopped:
            yield self._resume_event                # a stopped belt takes nothing on
        # Reserve the entry slot BEFORE sleeping on it: two entities that both
        # read a slot 2 s away would otherwise both take it and ride side by
        # side, which is exactly what spacing forbids.
        slot = max(self._next_entry, self.env.now)
        self._next_entry = slot + spacing_time
        if slot - self.env.now > _EPS:
            # not `slot`: the float the timeout lands on (a state taken again matches)
            entity._belt = ("slot", self.env.now + (slot - self.env.now), None)
            yield self.env.timeout(slot - self.env.now)
        yield from self._board(entity, cell)

    def _board(self, entity, cell):
        accept(entity)                              # on the belt: the machine is free
        entered_at = self.env.now
        self.entered += 1
        self.occupancy.add(1, entered_at)
        yield from self._carry(entity, cell, entered_at)

    def _carry(self, entity, cell, entered_at, end=None):
        """The ride to the end (until *end*, put back by a state), then the way off."""
        if end is None:
            entity._belt = ("ride", self.env.now + self.travel_time, entered_at)
            yield from self._ride(self.travel_time)
        else:
            entity._belt = ("ride", end, entered_at)
            yield timeout_at(self.env, end)
        ahead = self._prev_clear
        clear = self.env.event()
        self._prev_clear = clear
        yield from self._leave(entity, cell, entered_at, ahead, clear)

    def _leave(self, entity, cell, entered_at, ahead, clear, stage=None, until=None):
        """From the end of the ride off the belt. *stage* is where a state put the part back
        (D429): moving up until *until*, at the end stop, or handed over (D430)."""
        if stage == "move":
            entity._belt = ("move", until, entered_at)
            yield timeout_at(self.env, until)
        elif ahead is not None and not ahead.triggered:
            # Packed against the entity in front: move up as it leaves.
            entity._belt = ("packed", None, entered_at)
            if not self.accumulating:
                self._stop_belt()
            yield ahead
            if not self.accumulating:
                self._start_belt()
            entity._belt = ("move", self.env.now + self.spacing / self.speed, entered_at)
            yield from self._ride(self.spacing / self.speed)

        if self.exit_resource and stage != "handover":
            blocking = self.ctx.resource_manager.resolve(self.exit_resource)
            # put back at the stop, it found none free (a state refuses any other case)
            free_now = stage != "stop" and blocking is not None \
                and getattr(blocking, "free", 0) >= 1
            entity._belt = ("stop", None, entered_at)
            if not free_now:
                self._end_blocked(True)
            yield from acquire(self, entity, self.exit_resource)
            if not free_now:
                self._end_blocked(False)

        if self.block_after_service:
            # Back-pressure (PHASE6 W3 M6, D115), AFTER the end stop: the part
            # holds its exit unit and its cell until the next belt takes it.
            yield from self._hand_over(entity, entered_at, forward=stage != "handover")
        clear.succeed()
        t = self.env.now
        self.exited += 1
        self.occupancy.add(-1, t)
        self.transit.add(t - entered_at)
        self.space.give_back(cell)
        if self.ctx.debug:
            log.debug("Entity %s left conveyor '%s' at time %s.", entity.id, self.name, t)
        if not self.block_after_service:
            self.forward(entity, 0)

    def _hand_over(self, entity, entered_at, forward=True):
        """Generator: forward *entity* and keep it on the belt -- its cell,
        its place at the end -- until the downstream belt accepts it (the D78
        token). A DECIDE in between is transparent, so a full CHOSEN branch
        blocks the belt; belts merging on one entry queue FIFO on its cells.
        *forward* False: a state put the part back where it waits (D430)."""
        token = arm(self, entity)
        token.entered = entered_at          # D430: when it entered this belt, for a state
        if forward:
            self.forward(entity, 0)
        if token.triggered:
            return
        self.held_downstream = True
        self._end_blocked(True)
        check_deadlock(self)
        yield token
        self.held_downstream = False
        self._end_blocked(False)

    def self_run(self, entity):
        self.env.process(self.self_process(entity))

    # -- put back by a state (D429, D430) ------------------------------------
    def restore(self, parts) -> list:
        """The parts a state puts back on this belt, [(entity, {stage, until, entered})]; the
        one its end still hands over has stage "handover". Each takes its cell at once and
        the chain of the parts past the ride is rebuilt in the order they entered (the order
        of an accumulating belt). Returns [(generator, joins a queue)]: the part at the end
        stop asks for a unit, so the caller puts it back with the waiting ones."""
        out = []
        for entity, belt in sorted(parts, key=lambda p: (p[1].get("entered") is None,
                                                         p[1].get("entered") or 0.0)):
            stage, cell = belt["stage"], self.space.claim()
            ahead = clear = None
            if stage in ("packed", "move", "stop", "handover"):
                ahead, clear = self._prev_clear, self.env.event()
                self._prev_clear = clear
            out.append((self._resume(entity, belt, cell, ahead, clear), stage == "stop"))
        return out

    def _resume(self, entity, belt, cell, ahead, clear):
        stage, until, entered = belt["stage"], belt.get("until"), belt.get("entered")
        if stage == "slot":
            entity._belt = ("slot", until, None)
            yield timeout_at(self.env, until)
            yield from self._board(entity, cell)
            return
        self.occupancy.add(1, self.env.now)
        if stage == "ride":
            yield from self._carry(entity, cell, entered, until)
        else:
            yield from self._leave(entity, cell, entered, ahead, clear, stage, until)

    def reset_stats(self, t: float) -> None:
        """Warm-up hook, called from `sim_runner.reset_statistics`."""
        self.entered = 0
        self.exited = 0
        self.occupancy.reset(t)
        self.blocked.reset(t)
        self.transit.reset()
        if self._space is not None:
            self._space.reset_stats(t)

    def get_params_dict(self):
        return {"length": self.length, "speed": self.speed, "spacing": self.spacing,
                "accumulating": self.accumulating, "exit_resource": self.exit_resource,
                "from_node": self.from_node, "to_node": self.to_node}

    def set_params_from_dict(self, param_dict):
        units = self.ctx.units
        self.length = scaled(float(param_dict.get("length", 0.0) or 0.0),
                             field_factor(param(SPEC, "length"), param_dict, units, SPEC.params))
        self.speed = scaled(float(param_dict.get("speed", 1.0) or 1.0),
                            field_factor(param(SPEC, "speed"), param_dict, units, SPEC.params))
        self.spacing = scaled(float(param_dict.get("spacing", 1.0) or 1.0),
                              field_factor(param(SPEC, "spacing"), param_dict, units, SPEC.params))
        self.accumulating = bool(param_dict.get("accumulating", True))
        self.exit_resource = str(param_dict.get("exit_resource", "") or "").strip()
        self.from_node = str(param_dict.get("from_node", "") or "").strip()
        self.to_node = str(param_dict.get("to_node", "") or "").strip()
        self._space = None


SPEC = BlockSpec(
    type_name="CONVEYOR", cls=Conveyor,
    params=(
        ParamSpec("length", float, 0.0,
                  description="Belt length in layout units. 0 takes the network distance between "
                              "`from_node` and `to_node`.", quantity="length", min=0),
        ParamSpec("length_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `length`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("speed", float, 1.0, required=True,
                  description="Belt speed, length units per model time unit.", quantity="speed", min=0),
        ParamSpec("speed_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `speed`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("spacing", float, 1.0, required=True,
                  description="Belt length one entity occupies; capacity is "
                              "floor(length / spacing) and the entry rhythm is spacing / speed.", quantity="length", min=0),
        ParamSpec("spacing_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `spacing`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("accumulating", bool, True,
                  description="True: a blocked entity stops and the others pack behind it. "
                              "False: the whole belt stops while the exit is blocked.", quantity="none"),
        ParamSpec("exit_resource", str, "",
                  description="Resource an entity must take to leave the belt -- the end stop "
                              "that makes accumulation happen. It carries the resource onward, so "
                              "release it downstream (a PROCESS with action \"Delay Release\"). "
                              "Empty: no end stop. Either way, a belt that feeds another belt "
                              "keeps its end part until that belt has room (back-pressure).", quantity="none"),
        ParamSpec("from_node", str, "",
                  description="Path-network node the belt starts at (drawing, and the length when "
                              "`length` is 0).", quantity="none"),
        ParamSpec("to_node", str, "",
                  description="Path-network node the belt ends at.", quantity="none"),
    ),
    factory=lambda env, block: Conveyor(env, block),
    family="move",
)
