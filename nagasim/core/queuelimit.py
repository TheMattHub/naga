"""The run stop on a queue limit (D1529-D1531): `simulation.queue_limit` {"on", "length"}.

A model with a capacity mistake grows a queue without bound and the run spends its time
building it. With the limit on, each waiting place has a length reader (a gauge), registered
once at build (`register`); `sim_runner._step_until` reads them every `CHECK_EVERY` events and
`check` stops the run with E_QUEUE_LIMIT when one is over the limit. Nothing is registered and
nothing is read when the setting is absent or off, so such a run is the run it always was.
"""
from __future__ import annotations

from nagasim.core.errors import ModelRunError
from nagasim.core.problems import Problem, problem

__all__ = ["DEFAULT_LENGTH", "CHECK_EVERY", "HINT", "read_setting", "setting_problems",
           "register", "check"]

DEFAULT_LENGTH = 100_000        # "very high": far above the queues of any bundled model
# Gauges are read every 4,096 events, not per event, so the hot paths stay as they are; a
# stop may come a little past the limit, never before it.
CHECK_EVERY = 4096
HINT = ("check the capacity and routing of that block, or raise or turn off "
        "simulation.queue_limit")


def read_setting(sim) -> tuple[int | None, list[str]]:
    """`(limit, faults)` of a `simulation` section: the limit when the setting is on and
    valid, else None; `faults` names each bad value, which makes the whole setting read as
    off (a bad value warns, it never blocks a run)."""
    raw = sim.get("queue_limit") if isinstance(sim, dict) else None
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, ["is not {\"on\": true or false, \"length\": a whole number}"]
    faults = []
    on, length = raw.get("on", False), raw.get("length", DEFAULT_LENGTH)
    if not isinstance(on, bool):
        faults.append("on is not true or false")
    if isinstance(length, bool) or not isinstance(length, int) or length < 1:
        faults.append("length is not a whole number above 0")
    return (length if on and not faults else None), faults


def setting_problems(layout: dict) -> list[Problem]:
    """One W_QUEUE_LIMIT_SETTING for a bad `simulation.queue_limit`, or none."""
    sim = layout.get("simulation") if isinstance(layout, dict) else None
    faults = read_setting(sim)[1]
    if not faults:
        return []
    said = "; ".join(faults)
    return [problem("W_QUEUE_LIMIT_SETTING", f"simulation.queue_limit: {said}; the run reads "
                    "the limit as off", "/simulation/queue_limit",
                    hint='write {"on": true, "length": 100000}, or remove queue_limit')]


def register(ctx, processes) -> None:
    """The gauges of a built model, in block order, then the fleets and chargers: one
    `(name, reader)` per waiting place. A resource queue is read through the SEIZE or
    PROCESS block that waits on it; blocks that share a resource read the same queue, and
    the first of them names it."""
    from nagasim.core.blocks import Batch, Buffer, Hold, Match
    from nagasim.core.seizing import QueueBehaviour, _queue_length

    resolve = ctx.resource_manager.resolve
    gauges = []
    for process in processes:
        if isinstance(process, QueueBehaviour) and process.target_name:
            gauges.append((process.name, lambda p=process: _queue_length(resolve(p.target_name))))
        elif isinstance(process, Match):
            gauges.append((process.name, lambda p=process: sum(len(s.items) for s in p.stores)))
        elif isinstance(process, (Hold, Batch)):
            gauges.append((process.name, lambda p=process: len(p.store.items)))
        elif isinstance(process, Buffer):
            # Entities waiting for a place (the places themselves are bounded).
            gauges.append((process.name, lambda p=process: p.space.waiting))
    for owners in (ctx.fleets, ctx.chargers):
        for name, owner in owners.items():
            gauges.append((name, lambda o=owner: int(o.queue_len.level)))
    ctx.queue_gauges = gauges


def check(ctx, now: float) -> None:
    """Raise E_QUEUE_LIMIT when the longest gauge is over `ctx.queue_limit`."""
    limit, worst, name = ctx.queue_limit, 0, None
    for gauge, read in ctx.queue_gauges:
        n = read()
        if n > worst:
            worst, name = n, gauge
    if worst <= limit:
        return
    at = f"{now:.2f}".rstrip("0").rstrip(".")
    error = ModelRunError(
        f"Stopped at t={at}: {name} had {worst} waiting, over the model's queue limit of "
        f"{limit}; this saves run time on a model whose queue grows without bound.")
    error.block, error.code, error.hint = name, "E_QUEUE_LIMIT", HINT
    raise error
