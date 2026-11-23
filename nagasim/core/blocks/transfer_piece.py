"""CONVEYOR_TRANSFER: a pop-up transfer, a turntable or a lift (conveyor system A8).

A module between conveyors that takes one load at a time and hands it to one of its `exits`,
each `{"port", "angle"}` with the angle measured from the entry heading in the floor-plan
frame (D1354). Its routing `rule` (core/routing.py) picks the exit, round robin by default
over two or more exits (D1358). Every rule is an event time measured in Emulate3D (D675, no
physics), and the module is busy from the load's entry until its handshake:

* **Entry.** The module takes a load only while it is free; the piece feeding it holds the load
  (the D78 token) until then. The load travels `entry_length` at `transfer_speed` to its load
  position, which counts in the busy time.
* **Move.** A pop-up's straight exit (angle 0) has no lift and no cross; a side or angled exit
  lifts `lift_time`, crosses `cross_length / transfer_speed`. A turntable turns the shortest
  way to the exit angle, `a / turn_rate + turn_ramp`. A lift travels `lift_height /
  lift_speed`. The load then boards the exit's piece.
* **Handshake.** The load reaches the receiving CONVEYOR's first photo-eye that fires for it;
  with no such eye, it has cleared that belt's entry by its own length (`item_length / speed`
  after it boarded). An exit to any other block hands over as that block takes the load.
* **Return.** A pop-up's side exit lifts down `lift_time`; a lift travels back; a turntable
  with `return_home` turns back to 0 before the next load. With `return_home` false the table
  stays at the exit angle and turns back only once the next load waits.

A full receiving belt keeps the load on the module (blocking after service), so the module is
a node of the deadlock check like a belt.
"""
import math

from nagasim.core.arealink import real_target
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocking import (TRANSPARENT_TYPES, Space, accept, check_deadlock,
                                   hold_until_accepted, placed)
from nagasim.core.blocks._routing import RoutePoint, rule_of
from nagasim.core.conveyor_checks import route_taken
from nagasim.core.errors import ModelRunError
from nagasim.core.blocks.conveyor import write_exposed, zone_hold_delay
from nagasim.core.quantities import field_factor, param
from nagasim.core.stats import TimeWeighted

__all__ = ["ConveyorTransfer", "SPEC", "MECHANISMS", "turn_angle"]

MECHANISMS = ("popup", "turntable", "lift")
# D1378: `<block>.state` of an exposed module
IDLE, LIFTING, TRANSFERRING, TURNING = 0, 1, 2, 3
_EPS = 1e-9


def turn_angle(angle: float) -> float:
    """Degrees a table turns to reach *angle* from 0, the shorter way (270 is 90 back)."""
    return abs((float(angle) + 180.0) % 360.0 - 180.0)


def _number(value, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


class ConveyorTransfer(BaseProcess):
    """Conveyor transfer module that moves each load onto one of several exits: a pop-up transfer, a turntable or a lift, busy until the load reaches the next conveyor's photo-eye."""

    # The token rides through: the module takes the load off the piece feeding it only once
    # it is free (and, for a turntable away from home, turned back), as a belt does.
    space_passthrough = True

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.mechanism = "popup"
        self.exits: list = []           # as declared: [(port, angle)]
        self.rule: dict = {}
        self.lift_time = 0.1
        self.transfer_speed = 0.0       # 0: the feeding belt's speed
        self.cross_length = 0.6
        self.entry_length = 0.0
        self.turn_rate = 18.0
        self.turn_ramp = 0.3
        self.lift_height = 1.0
        self.lift_speed = 0.5
        self.return_home = True
        self.expose = False
        # R39: the numbers above stay as written, in their own units; `_factor` takes each to
        # the engine's base at use, so get_params_dict gives back what was set (BATCH's pattern)
        self._factor: dict = {}
        self._units: dict = {}
        self.space = Space(env, 1, self)    # one load on the module at a time
        self.blocked = TimeWeighted(env.now, level=0.0)
        self._angle = 0.0               # where a turntable stands, degrees from home
        self._route = None
        self._feed_speed = None
        self._waiting = False           # R35: held by its rule or waiting for its handshake
        # A12 (D1374): 1 while the module moves (lift, cross, turn, travel; a turn back included)
        # and while it waits for its handshake (an eye, or the load clearing the entry); the
        # rule hold is its routing point's (`RoutePoint.any_held`)
        self.moving = TimeWeighted(env.now)
        self.handshake = TimeWeighted(env.now)
        self.loads = 0                  # loads handed over since the warm-up

    def reset_transfer_stats(self, t: float) -> None:
        """Warm-up hook (`sim_runner.reset_statistics`; `space` and `blocked` are reset as a
        blocking node's)."""
        self.moving.reset(t)
        self.handshake.reset(t)
        self.loads = 0

    def _move(self, duration: float, state: int):
        """Generator: the module's own motion for *duration*, counted in `moving`; *state*
        what an exposed module shows meanwhile (D1378)."""
        self.moving.set(1.0, self.env.now)
        self._state(state)
        yield self.env.timeout(duration)
        self.moving.set(0.0, self.env.now)

    def _state(self, state: int) -> None:
        if self.expose:
            write_exposed(self, "state", state)

    def _motion(self, angle: float, back: bool = False) -> int:
        """D1378: what the module does in its move to (or back from) exit *angle*: a lift and
        a pop-up's side exit lift, a turntable turns. A pop-up's way out lifts `lift_time` and
        then crosses; the cross shows as transferring from a mark at the lift's end, an event
        that only an exposed module has (it moves no other time)."""
        if self.mechanism == "turntable":
            return TURNING
        if self.expose and not back and self.mechanism == "popup":
            lift = self._base("lift_time")
            if lift > 0 and self._base("cross_length") > 0:
                mark = self.env.timeout(lift)
                mark.callbacks.append(lambda _event: self._state(TRANSFERRING))
            elif lift <= 0:
                return TRANSFERRING
        return LIFTING

    @property
    def held_downstream(self) -> bool:
        """What the deadlock check reads (R35): the load waits on the module for a free exit
        of its rule, for the next block to take it, or for the receiving belt's eye."""
        return self._waiting or self.blocked.level > 0

    def _wait_starts(self) -> None:
        self._waiting = True
        check_deadlock(self)

    # -- exits and routing ------------------------------------------------------
    @property
    def exit_list(self) -> list:
        """(port, angle) per exit in exit order; one straight exit "out" when none is set."""
        return self.exits or [("out", 0.0)]

    def _route_point(self) -> RoutePoint:
        if self._route is None:
            exits = self.exit_list
            links = len(self.next_process or [])
            if links != len(exits):
                raise ModelRunError(f"Block '{self.name}': {len(exits)} exits need "
                                    f"{len(exits)} links out, it has {links} "
                                    f"(E_CONVEYOR_DIVERT).")
            route = rule_of(self, self.rule, [port for port, _angle in exits], "transfer")
            if len(route.exits) != len(exits):
                raise ModelRunError(f"Block '{self.name}': its rule has {len(route.exits)} "
                                    f"exits for {len(exits)} exits (E_CONVEYOR_DIVERT).")
            self._route = RoutePoint(self, route, range(len(exits)))
        return self._route

    def _choose(self, entity):
        """Generator: the exit the rule picks; while every exit is at its limit the load waits
        on the module, re-asked as an exit's count drops (never polled)."""
        route = self._route_point()
        index = route.choose(entity)
        if index is None:
            since = route.held(1)
            self._state(IDLE)               # D1378: the load stands on the module
            self._wait_starts()
            while index is None:
                yield route.freed()
                index = route.choose(entity)
            self._waiting = False
            route.held(-1, since)
        route.routed(entity, index)
        return index

    # -- timing -------------------------------------------------------------------
    def _base(self, name: str) -> float:
        """Param *name* in the engine's base unit."""
        return getattr(self, name) * self._factor.get(name, 1.0)

    def speed(self) -> float:
        """`transfer_speed`, else the speed of the one CONVEYOR that feeds the module."""
        if self.transfer_speed > 0:
            return self._base("transfer_speed")
        if self._feed_speed is None:
            # a belt feeding it over an area link counts too, as validation reads drawn links
            speeds = {belt.speed for belt in self.ctx.conveyors.values()
                      if any(real_target(p) is self for p in belt.next_process or [])}
            if len(speeds) != 1:
                why = ("no CONVEYOR feeds it" if not speeds
                       else "its feeding belts run at different speeds")
                raise ModelRunError(f"Block '{self.name}': transfer_speed is 0, which takes the "
                                   f"feeding belt's speed, but {why}; set transfer_speed "
                                   f"(E_CONVEYOR_PARAM).")
            self._feed_speed = speeds.pop()
        return self._feed_speed

    def _turn(self, angle: float) -> float:
        turn = turn_angle(angle)
        return turn / self._base("turn_rate") + self._base("turn_ramp") if turn > _EPS else 0.0

    def move_time(self, angle: float) -> float:
        """From the load position to boarding the exit's piece."""
        if self.mechanism == "turntable":
            return self._turn(angle)
        if self.mechanism == "lift":
            return self._base("lift_height") / self._base("lift_speed")
        if turn_angle(angle) <= _EPS:       # a pop-up's straight exit
            return 0.0
        return self._base("lift_time") + self._base("cross_length") / self.speed()

    def return_time(self, angle: float) -> float:
        """From the handshake to the module free for the next load."""
        if self.mechanism == "turntable":
            return self._turn(angle) if self.return_home else 0.0
        if self.mechanism == "lift":
            return self._base("lift_height") / self._base("lift_speed")
        return 0.0 if turn_angle(angle) <= _EPS else self._base("lift_time")

    # -- the cycle ------------------------------------------------------------------
    def self_run(self, entity):
        self.env.process(self.self_process(entity))

    def self_process(self, entity):
        place = yield from self.space.take(entity, accepts=False)
        if self._angle:
            # return_home false: the table turns back only now that a load waits
            yield from self._move(self._turn(self._angle), TURNING)
            self._angle = 0.0
        delay = zone_hold_delay(entity, self.env.now)
        if delay > 0:
            # a zoned belt's held lead part waits its zone-1 delay before any hand-off (R23)
            self._state(IDLE)
            yield self.env.timeout(delay)
        accept(entity)                      # on the module: the feeding piece is free
        yield from placed(entity)
        index = yield from self._choose(entity)
        if self.entry_length > 0:
            self._state(TRANSFERRING)       # D1378: the load travels to its load position
            yield self.env.timeout(self._base("entry_length") / self.speed())
        angle = self.exit_list[index][1]
        move = self.move_time(angle)
        if move > 0:
            yield from self._move(move, self._motion(angle))
        if self.mechanism == "turntable" and not self.return_home:
            self._angle = turn_angle(angle)
        # A11: the exit chosen before the move may be one the rule would not choose now
        route_taken(self, self._route.rule, entity, index)
        yield from self._hand_over(entity, index)
        back = self.return_time(angle)
        if back > 0:
            yield from self._move(back, self._motion(angle, back=True))
        self._state(IDLE)
        self.space.give_back(place)

    def receiving_belt(self, index: int):
        """The CONVEYOR exit *index* hands its loads to: the exit's link target, or the belt
        reached from it through transparent blocks with one link out and no link delay (an
        ASSIGN stamping a destination keeps the handshake); None when there is none, or when a
        transparent block on the way fans out (a load-time warning says so)."""
        piece, link, seen = self, index, set()
        while True:
            nexts = piece.next_process or []
            delays = getattr(piece, "link_delays", [])
            if link >= len(nexts) or (link < len(delays) and delays[link] is not None):
                return None
            piece = nexts[link]
            kind = getattr(getattr(piece, "block", None), "block_type", "")
            if kind == "CONVEYOR":
                return piece
            if kind not in TRANSPARENT_TYPES or id(piece) in seen \
                    or len(piece.next_process or []) != 1:
                return None
            seen.add(id(piece))
            link = 0

    def _hand_over(self, entity, index: int):
        """Generator: forward *entity* by exit *index* and return at its handshake: the first
        eye of the receiving belt that fires for it, else its clearing that belt's entry by
        its own length, else (no belt) the next block taking it."""
        belt = self.receiving_belt(index)
        reached = belt.reach_event(entity) if belt is not None and belt.has_eye_for(entity) \
            else None
        self._state(TRANSFERRING)           # D1378: boarding the exit's piece
        yield from hold_until_accepted(self, entity, index)
        self.loads += 1
        if reached is not None:
            if not reached.triggered:
                belt.handshake(entity, self)    # R41: the live check reads where it stands
                self._wait_starts()
                self.handshake.set(1.0, self.env.now)
                yield reached
                self.handshake.set(0.0, self.env.now)
                belt.handshake(entity, None)
                self._waiting = False
        elif belt is not None:
            boarded = self.env.timeout(belt.item_length(entity) / belt.speed)
            # D1368: a reversal frees the module, its load no longer boards forward
            turned = belt.turn_event()
            self.handshake.set(1.0, self.env.now)
            yield boarded if turned is None else boarded | turned
            self.handshake.set(0.0, self.env.now)

    # -- params ---------------------------------------------------------------------
    def get_params_dict(self):
        return {"mechanism": self.mechanism,
                "exits": [{"port": port, "angle": angle} for port, angle in self.exits],
                "rule": dict(self.rule), "lift_time": self.lift_time,
                "transfer_speed": self.transfer_speed, "cross_length": self.cross_length,
                "entry_length": self.entry_length, "turn_rate": self.turn_rate,
                "turn_ramp": self.turn_ramp, "lift_height": self.lift_height,
                "lift_speed": self.lift_speed, "return_home": self.return_home,
                **self._units, **({"expose": True} if self.expose else {})}

    def set_params_from_dict(self, param_dict):
        units = self.ctx.units

        def number(name, default):
            self._factor[name] = field_factor(param(SPEC, name), param_dict, units, SPEC.params)
            unit = param_dict.get(f"{name}_unit")
            self._units[f"{name}_unit"] = unit if unit not in (None, "") \
                else param(SPEC, f"{name}_unit").default
            return _number(param_dict.get(name), default)

        mechanism = str(param_dict.get("mechanism", "popup") or "popup").strip().lower()
        if mechanism not in MECHANISMS:
            raise ValueError(f"Block '{self.name}': mechanism '{mechanism}' is not one of "
                             f"{', '.join(MECHANISMS)} (E_CONVEYOR_PARAM).")
        self.mechanism = mechanism
        exits = []
        for index, spec in enumerate(param_dict.get("exits") or []):
            port = str(spec.get("port", "") or "").strip() if isinstance(spec, dict) else ""
            angle = spec.get("angle", 0.0) if isinstance(spec, dict) else None
            if not port or isinstance(angle, bool) or not isinstance(angle, (int, float)) \
                    or not math.isfinite(angle):
                raise ValueError(f"Block '{self.name}': exit {index} needs a port name and an "
                                 f"angle in degrees (E_CONVEYOR_PARAM).")
            if ":" in port:     # a rule reads "name:target" (split_exit)
                raise ValueError(f"Block '{self.name}': exit {index} port '{port}' has a ':', "
                                 f"which a routing rule reads as name:target "
                                 f"(E_CONVEYOR_PARAM).")
            if any(port == known for known, _angle in exits):
                raise ValueError(f"Block '{self.name}': two exits share the port '{port}' "
                                 f"(E_CONVEYOR_PARAM).")
            exits.append((port, float(angle)))
        self.exits = exits
        self.rule = dict(param_dict.get("rule") or {})
        self.lift_time = number("lift_time", 0.1)
        self.transfer_speed = number("transfer_speed", 0.0)
        self.cross_length = number("cross_length", 0.6)
        self.entry_length = number("entry_length", 0.0)
        self.turn_rate = number("turn_rate", 18.0)
        self.turn_ramp = number("turn_ramp", 0.3)
        self.lift_height = number("lift_height", 1.0)
        self.lift_speed = number("lift_speed", 0.5)
        self.return_home = param_dict.get("return_home", True) is not False
        self.expose = param_dict.get("expose", False) is True
        if self.expose:
            self.ctx.variables.setdefault(f"{self.name}.state", IDLE)
        for name in ("lift_time", "transfer_speed", "cross_length", "entry_length",
                     "turn_ramp", "lift_height"):
            if getattr(self, name) < 0:
                raise ValueError(f"Block '{self.name}': {name} must be 0 or more "
                                 f"(E_CONVEYOR_PARAM).")
        for name in ("turn_rate", "lift_speed"):
            if getattr(self, name) <= 0:
                raise ValueError(f"Block '{self.name}': {name} must be a number > 0 "
                                 f"(E_CONVEYOR_PARAM).")
        self._route = None
        self._feed_speed = None


def _unit(name: str, unit: str) -> ParamSpec:
    # R30: the measured defaults carry their own unit, so 0.1 stays 0.1 s in a minutes model
    # and a block the editor writes (every default set) keeps the measured meaning
    return ParamSpec(f"{name}_unit", str, unit, quantity="none", kinds=("choice",),
                     description=f"Unit of `{name}`: one of its quantity's units; \"base\" or "
                                 f"absent means {unit}, the unit its default was measured in.")


SPEC = BlockSpec(
    type_name="CONVEYOR_TRANSFER", cls=ConveyorTransfer,
    params=(
        ParamSpec("mechanism", str, "popup", quantity="none", kinds=("choice",),
                  description="\"popup\" (a pop-up transfer: chains or rollers lift and carry "
                              "the load sideways), \"turntable\" (turns the load to its exit) "
                              "or \"lift\" (carries the load to another level)."),
        ParamSpec("exits", list, [], quantity="none", kinds=("table",),
                  description="The module's exits, in exit order: {\"port\": <name a link "
                              "gives as its source_port>, \"angle\": <degrees from the entry "
                              "heading, clockwise on screen: 0 straight on, 90 or -90 to a "
                              "side>}. Links without a port take the exits in this order. "
                              "Empty: one straight exit \"out\"."),
        ParamSpec("rule", dict, {}, quantity="none",
                  description="Routing rule picking the exit, {\"kind\", \"exits\"?, "
                              "\"conditions\"?, \"limit\"?, \"attribute\"?} as a Rule-mode "
                              "DECIDE's; its exits default to the exits' ports. Empty: round "
                              "robin over two or more exits, else the one exit. While every "
                              "exit is at its limit the load waits on the module."),
        ParamSpec("lift_time", float, 0.1, quantity="time", min=0,
                  description="A pop-up's lift time each way, up before a side or angled "
                              "exit and down after its handshake; a straight exit has none."),
        _unit("lift_time", "seconds"),
        ParamSpec("transfer_speed", float, 0.0, quantity="speed", min=0,
                  description="Speed of the chains or rollers that carry the load on the "
                              "module. 0: the speed of the CONVEYOR feeding it."),
        _unit("transfer_speed", "m/s"),
        ParamSpec("cross_length", float, 0.6, quantity="length", min=0,
                  description="A pop-up's travel from the entry to the module edge on a side "
                              "or angled exit, at transfer_speed."),
        _unit("cross_length", "meters"),
        ParamSpec("entry_length", float, 0.0, quantity="length", min=0,
                  description="Travel from the entry to the load's position on the module, "
                              "at transfer_speed, before it moves to its exit."),
        _unit("entry_length", "meters"),
        ParamSpec("turn_rate", float, 18.0, quantity="rate", min=0, min_exclusive=True,
                  description="A turntable's turning speed in degrees per time unit (per second by "
                              "default); a turn "
                              "of a degrees takes a / turn_rate + turn_ramp, the shorter way."),
        _unit("turn_rate", "seconds"),
        ParamSpec("turn_ramp", float, 0.3, quantity="time", min=0,
                  description="Time a turntable's turn adds to speed up and slow down."),
        _unit("turn_ramp", "seconds"),
        ParamSpec("lift_height", float, 1.0, quantity="length", min=0,
                  description="A lift's travel between its two levels; the lift travels it up and "
                              "back on every exit, whatever its angle."),
        _unit("lift_height", "meters"),
        ParamSpec("lift_speed", float, 0.5, quantity="speed", min=0, min_exclusive=True,
                  description="A lift's travel speed; each way takes lift_height / "
                              "lift_speed."),
        _unit("lift_speed", "m/s"),
        ParamSpec("return_home", bool, True, quantity="none",
                  description="A turntable turns back to its entry after each handshake, "
                              "before the next load. False: it stays at the exit and turns "
                              "back only once the next load waits."),
        # D1378: state for a later PLC connection (spec goal 14)
        ParamSpec("expose", bool, False, quantity="none",
                  description="True: the module writes `<module>.state` as a model variable, "
                              "read with VAR(\"<module>.state\") in a HOLD condition or item "
                              "code: 0 idle (or a load standing), 1 lifting (a pop-up's lift, "
                              "a lift's travel), 2 transferring (the entry travel, a pop-up's "
                              "cross, boarding the exit's piece until the handshake), 3 "
                              "turning (a turntable)."),
    ),
    factory=lambda env, block: ConveyorTransfer(env, block),
    multi_output=True,
    family="move",
)
