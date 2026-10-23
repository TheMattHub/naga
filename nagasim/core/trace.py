"""Entity-lifecycle trace recorder (M4a): a bus subscriber that stores
`entity_created` / `entity_moved` / `entity_disposed` / `entity_seized` /
`entity_released` / `resource_state` events for later playback (ARCHITECTURE
10.2 - the engine emits, listeners are pure consumers). Stdlib only, no UI
toolkit/pandas dependency.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["DEFAULT_MAX_EVENTS", "TraceEvent", "TraceRecorder"]

# The post-game recording's cap (PHASE6 L1): a named constant, not a bare
# literal on the default argument, so a test can monkeypatch it to force
# truncation without waiting for that many real events.
# 250k since 2026-10-05 (was 50k): an animated floor writes a cycle phase per arm
# segment, and 50k was under six simulated hours of the automation floor template.
# Measured at 250k: about 40 MB more, a second to load, 0.1-0.2 s for a backward seek.
DEFAULT_MAX_EVENTS = 250_000

_RECORDED_EVENTS = {
    "entity_created", "entity_moved", "entity_disposed", "entity_absorbed",
    "entity_seized", "entity_released", "resource_state",
    # PHASE4 E1: where each vehicle of a mover fleet is driving, so playback
    # animates it along the real polyline without any live engine state.
    "mover_moved",
    # PHASE4 T2 (D75): one trajectory BREAKPOINT per level per re-evaluation.
    # A level is piecewise linear between two of them, so playback draws the
    # exact curve from the corners alone.
    "level_changed",
    # PHASE5 AB-3: an agent's state (every firing) and its current motion
    # segment (every placement and move_to), capped at `trace_agents`;
    # PHASE6 C5: a removal, so playback drops the token.
    "agent_state", "agent_moved", "agent_removed",
    # PHASE6 A5: a link made or dropped between two traced agents of one
    # population (`entity_id` and `value` the two indices).
    "agent_linked", "agent_unlinked",
    # Automation sub-project 1 (D156): one MOTION segment start; read by 3D,
    # sub-project 5.
    "cycle_phase",
    # Automation sub-project 2 (D164-D165): a vehicle's state of charge when it
    # is released, and when a charge starts and ends -- a stepped SoC bar and
    # the occupied points of a charger in playback.
    "battery", "charge_start", "charge_end",
    # Automation sub-project 3 (D169): a cobot's pick or place cycle starting
    # at a station; read by 3D, sub-project 5.
    "tend",
    # D608: a variable an asset's picture follows, at each write (`resource` its
    # name, `value` its new value).
    "variable_changed",
    # D734: a part's stage on a CONVEYOR (`x0` -> `x1` along the belt in
    # `duration`, `value` the spacing), so playback places it without the belt.
    "belt_stage",
}
_AGENT_EVENTS = ("agent_state", "agent_moved", "agent_removed", "agent_linked", "agent_unlinked")


@dataclass(frozen=True, slots=True)
class TraceEvent:
    t: float
    kind: str
    entity_id: int
    e_type: str
    block: str
    target: str | None
    # Link transfer delay (ROADMAP 1.5): 0.0 means an instant move, exactly as
    # before; > 0.0 means the entity is in transit from `block` to `target`
    # during (t, t + duration) -- see playback.Timeline.
    duration: float = 0.0
    # resource_state only (ROADMAP 2.3 part C, core/resource_manager.py
    # `emit_state`): the resource named, its new state ("capacity"/"down"/
    # "up") and the value that goes with it (a capacity number, or 1/0 for
    # down/up). Defaulted so an older trace (pickled, or built by hand in a
    # test) still loads with no resource_state events in it.
    resource: str | None = None
    state: str | None = None
    value: float = 0.0
    # Geometry (PHASE4 E1): the network node list this move follows, and the
    # part of `duration` actually spent driving along it (the rest is loading
    # or unloading at the far end). Empty/0.0 on every event of a model with no
    # path network -- and on every trace recorded before this -- in which case
    # `travel` means the whole duration, exactly as before.
    path: tuple = ()
    travel: float = 0.0
    # entity_moved only (PHASE4 T4, D77): which OUTGOING branch of `block` the
    # entity took. Two parallel links to the same target differ only by this,
    # so playback animates each along the curve it belongs to. -1 on an older
    # trace and on every other event kind.
    branch: int = -1
    # agent_moved only (PHASE5 AB-3): the motion segment's endpoints; `target`
    # carries the population name, `entity_id` the agent's index, `duration`
    # (t1 - t0) and `path` (a network mover's node list, empty for a straight
    # line) are the existing fields. agent_state reuses `state` (the agent's
    # new state name), `e_type` (its agent type name) and, PHASE6 A2, `resource`
    # for the population's own color_by category (None with no color_by).
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0


class TraceRecorder:
    """`env.ctx.bus.subscribe(recorder)`; then read `.events`.

    Stops recording silently past `max_events` instead of growing without
    bound over a long or runaway replication; `.truncated` flags that it happened.

    *max_events* None (the default) uses `DEFAULT_MAX_EVENTS`, looked up here
    rather than bound as the parameter's default so a test can monkeypatch the
    module constant and have it take effect on the next plain `TraceRecorder()`
    (used e.g. by the live game's post-game recording, PHASE6 L1).
    """

    def __init__(self, max_events: int | None = None):
        self.max_events = DEFAULT_MAX_EVENTS if max_events is None else max_events
        self.events: list[TraceEvent] = []
        self.truncated = False

    def __call__(self, name, *, block=None, entity=None, t=0.0, target=None, resource=None,
                 duration=0.0, state=None, value=0.0, path=(), travel=None, unit=-1,
                 next_index=-1, pop=None, index=-1, e_type="", x0=0.0, y0=0.0, x1=0.0, y1=0.0,
                 color=None, fleet=None, **_):
        if name not in _RECORDED_EVENTS:
            return
        if len(self.events) >= self.max_events:
            self.truncated = True
            return
        if name in _AGENT_EVENTS:
            # PHASE5 AB-3: `pop`/`index` name the agent (AgentSystem.py has no
            # `block`/`entity`, so these ride the existing target/entity_id
            # fields under their own kwargs); PHASE6 A2's `color` rides the
            # otherwise-unused `resource` field (agent_state only).
            self.events.append(TraceEvent(
                t=t, kind=name, entity_id=index, e_type=e_type, block="", target=pop,
                resource=color, state=state, duration=duration, path=tuple(path),
                travel=duration if travel is None else travel, x0=x0, y0=y0, x1=x1, y1=y1,
                value=value))
            return
        if name == "mover_moved":
            # One vehicle of a fleet (PHASE4 E1): `entity_id` is the unit index
            # within the fleet, `target` the fleet name, so the existing fields
            # carry it with no new ones.
            self.events.append(TraceEvent(
                t=t, kind=name, entity_id=unit, e_type="", block=block.name if block else "",
                target=resource, resource=resource, state=state, duration=duration,
                path=tuple(path), travel=duration if travel is None else travel))
            return
        if name in ("battery", "charge_start", "charge_end"):
            # `entity_id` is the unit index, `target` the fleet, `value` the
            # SoC; `resource` is the charger (the fleet itself for "battery").
            self.events.append(TraceEvent(
                t=t, kind=name, entity_id=unit, e_type="", block="", target=fleet,
                resource=resource, value=value))
            return
        if name == "tend":
            # `entity_id` is the unit index, `target` the fleet, `resource` the
            # station and `state` "pick" or "place".
            self.events.append(TraceEvent(
                t=t, kind=name, entity_id=unit, e_type="", block="", target=fleet,
                resource=resource, state=state, duration=duration))
            return
        if name == "cycle_phase":
            # `state` is the segment kind, `value` its index in the cycle.
            self.events.append(TraceEvent(
                t=t, kind=name, entity_id=entity.id, e_type=getattr(entity, "e_type", "Unknown"),
                block=block.name, target=None, duration=duration, state=state, value=value))
            return
        if name == "belt_stage":
            # `state` is the stage, `x0`/`x1` distances from the belt's entry (D734).
            self.events.append(TraceEvent(
                t=t, kind=name, entity_id=entity.id, e_type=getattr(entity, "e_type", "Unknown"),
                block=block.name, target=None, duration=duration, state=state, value=value,
                x0=x0, x1=x1))
            return
        if name in ("resource_state", "level_changed", "variable_changed"):
            # Both are emitted with block=None (core/resource_manager.py,
            # core/levels.py): no entity, no block, just the record named by
            # `resource` and its new state or value.
            self.events.append(TraceEvent(
                t=t, kind=name, entity_id=-1, e_type="", block="", target=None,
                resource=resource, state=state, value=value))
            return
        # Seize/Release name the resource via `resource=`, not `target=` (that
        # kwarg is entity_moved's next-block name); TraceEvent.target carries
        # either, so playback has one field to read regardless of event kind.
        # PHASE6 H4: entity_created never otherwise sets target/value, so an
        # entity created with `entity.agent` set (CREATE `population`, or an
        # agent's `enter`) rides its (population, index) there -- letting
        # playback colour that entity's token by its agent's state, with no
        # change to a trace for an entity with no agent.
        agent = getattr(entity, "agent", None) if name == "entity_created" else None
        self.events.append(TraceEvent(
            t=t,
            kind=name,
            entity_id=entity.id if entity is not None else -1,
            e_type=getattr(entity, "e_type", "Unknown"),
            block=block.name,
            target=agent[0] if agent is not None else (target if target is not None else resource),
            duration=duration,
            path=tuple(path),
            travel=duration if travel is None else travel,
            branch=next_index,
            value=float(agent[1]) if agent is not None else value,
        ))
