"""A robot pick from a moving belt: what the belt and the robot block share.

The robot side is planned before the run: the robot's first move is a ptp to a
belt target, met by the intercept solver from the pose that closes the cycle, so
the approach time and the meeting point are constants of the plan. A belt reads
them at build from `belt_meets`, whichever block runs the robot: a MOTION or a
robot pattern BATCH (a palletizer).
"""
from __future__ import annotations

from dataclasses import dataclass

import simpy

__all__ = ["BeltMeet", "Meet", "InstantEnd", "belt_meets", "first_belt_target", "robot_arrives",
           "robot_leaves", "robot_starts"]

# A wait shorter than this is none: the plan's meeting time and the belt's ride to the
# meeting point agree to rounding, not to the bit.
_EPS = 1e-9


class Meet:
    """The rendezvous of one part and its robot at the meeting point. `part` fires when the
    part's front is there, `robot` when the robot is; `robot_at` is when the robot is due
    there, written as its approach starts (None while it is not on its way)."""

    def __init__(self, env):
        self.part = env.event()
        self.robot = env.event()
        self.robot_at: float | None = None


class InstantEnd(simpy.events.Event):
    """An event that fires after every ordinary event of this instant, however many hops
    they chain: SimPy orders an instant's events by priority before creation, and its
    ordinary events have priority 1. Set up as SimPy sets up a Timeout."""

    def __init__(self, env):
        super().__init__(env)
        self._ok, self._value = True, None
        env.schedule(self, priority=2)


def robot_starts(entity, arrive_at: float) -> None:
    """The robot's approach to *entity* starts (or resumes) and ends at *arrive_at*. Nothing
    for a part not called from a belt."""
    meet = getattr(entity, "_meet", None)
    if meet is not None:
        meet.robot_at = arrive_at


def robot_leaves(env, entity) -> None:
    """The robot is taken off its way to *entity*, or off the meeting point (a preemption,
    a failure) before the part got there: it is not there, so the part stands until it
    comes back and arrives again."""
    # #2002: a robot preempted while it waited still counted as there
    meet = getattr(entity, "_meet", None)
    if meet is None or meet.part.triggered:
        return
    meet.robot_at = None
    if meet.robot.triggered:
        meet.robot = env.event()


def robot_arrives(env, entity):
    """Generator: the robot is at the meeting point; it waits there for the part. Returns
    the wait, 0 when under a nanosecond."""
    meet = getattr(entity, "_meet", None)
    if meet is None:
        return 0.0
    if not meet.robot.triggered:
        meet.robot.succeed()
    start = env.now
    if not meet.part.triggered:
        yield meet.part
    waited = env.now - start
    return waited if waited > _EPS else 0.0


@dataclass(frozen=True)
class BeltMeet:
    """A robot block's pick from a moving belt: `approach` the seconds of its first move,
    from the call to the meeting, and `meet` the metres along the belt where it meets
    the part."""
    approach: float
    meet: float


def first_belt_target(params: dict) -> dict | None:
    """The target of a robot block's segment 0 when it is a pick from a moving belt
    ({"belt", "divert", ...}), else None."""
    segments = [s for s in params.get("segments") or [] if isinstance(s, dict)]
    targets = params.get("targets") if isinstance(params.get("targets"), dict) else {}
    first = targets.get(segments[0].get("to")) if segments else None
    return first if isinstance(first, dict) and "belt" in first else None


def belt_meets(layout: dict) -> dict[str, BeltMeet]:
    """{robot block name: BeltMeet} of every MOTION and every robot pattern BATCH whose
    segment 0 picks from a moving belt and plans without error; a BATCH's from its first
    slot's plan, which every slot shares. {} for a model without one."""
    blocks = [b for b in layout.get("blocks") or [] if isinstance(b, dict)
              and str(b.get("type", "")).upper() in ("MOTION", "BATCH")
              and first_belt_target(b.get("params") or {}) is not None]
    if not blocks:
        return {}
    from nagasim.core import motion, pallets
    out = {}
    for block in blocks:
        name = str(block.get("name"))
        if str(block.get("type", "")).upper() == "MOTION":
            plan = motion.block_robot_plan(layout, block)
            move = None if plan is None or plan.errors else plan.moves.get(0)
            if move is not None and move.meet is not None:
                out[name] = BeltMeet(float(move.seconds), float(move.meet))
        elif (block.get("params") or {}).get("robot"):
            got = pallets.slot_meet(layout, block)
            if got is not None:
                out[name] = BeltMeet(float(got[0]), float(got[1]))
    return out
