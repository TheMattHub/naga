"""Mobile cobots: machine tending (automation sub-project 3, D168-D170).

A mobile cobot is a fleet with an `arm` (a motion profile, D168). Tending
belongs to the place (D169): a STATION's `tending` group holds the `place` and
`pick` cycles and whether the cobot leaves or stays:

    "tending": {"place": [{"kind": "move", "distance_m": 0.8}, {"kind": "place", "time": 2.0}],
                "pick":  [{"kind": "pick", "time": 1.5}],
                "time_unit": "seconds", "hrc": "none", "zone": {}, "cobot": "leave"}

Tending applies to one end of a TRANSPORT trip when the fleet has an arm AND
the station at that end declares `tending`; each end is decided on its own.
This module owns the format's validation, the cycle time (through
`core/motion.py`), the pick and the place, and their statistics
(`ctx.tending`, the `tending_summary` frame); the trip is TRANSPORT's. The
machine and the cobot wait for each other with the blocking token of
`core/blocking.py`.
"""
from __future__ import annotations

from nagasim.core import blocking, motion
from nagasim.core.problems import problem
from nagasim.core.stats import Tally
from nagasim.core.timeunits import UNITS

__all__ = ["COBOT_MODES", "TendingStats", "applies", "cycle_seconds", "pick", "place", "stay",
           "cobot_of", "call_waiting_cobot", "check_stall", "validate_tending"]

COBOT_MODES = ("leave", "stay")
_FIELDS = ("place", "pick", "time_unit", "hrc", "zone", "cobot")
# (fleet, request, mover, since, stats) on a part whose cobot stayed with it.
_COBOT = "_cobot"


def applies(fleet, station) -> bool:
    """Whether *fleet* tends *station*: it has an arm and the station (None
    for a place that is no STATION) declares `tending`."""
    return bool(fleet.arm) and bool(getattr(station, "tending", None))


def cycle_seconds(block, entity, fleet, station, which: str) -> float | None:
    """How long the `pick` or `place` cycle (*which*) starting now takes, in
    MODEL time units: the station's segments timed with the fleet's arm
    profile. None when the station declares no such cycle -- the caller then
    uses the fleet's flat load or unload time."""
    group = station.tending
    segments = group.get(which)
    if not segments:
        return None
    profile = block.ctx.motion_profiles.get(fleet.arm)
    if profile is None:
        raise RuntimeError(f"Fleet '{fleet.name}': unknown motion profile '{fleet.arm}' for its "
                           f"arm (E_FLEET_ARM).")
    return sum(duration for _kind, duration, _collaborative in motion.segment_seconds(
        block, entity, segments, profile, group.get("time_unit") or "seconds",
        group.get("hrc") or "none", group.get("zone")))


# -- the run -----------------------------------------------------------------

class TendingStats:
    """One (station, fleet) row of `tending_summary`: counts, and a tally per
    time. `calls` is not a statistic: the cobots of an exchange fleet waiting
    loaded here, each with the event a finished part hands itself over by.
    Nor are `loaded`, the parts on cobots that wait loaded here with no
    exchange to come, and `picked_at`, when the last pick ended (`check_stall`)."""

    def __init__(self):
        self.calls: list = []
        self.loaded: list = []
        self.picked_at = None
        self.reset_stats(0.0)

    def reset_stats(self, t: float) -> None:
        """Warm-up: zero everything; a wait in progress counts from *t*."""
        self.picks = self.places = self.exchanges = 0
        self.pick_time, self.place_time = Tally(), Tally()
        self.loaded_wait, self.blocked_for_pick, self.stay = Tally(), Tally(), Tally()
        self.since = t

    def waited(self, tally: Tally, started: float, now: float) -> None:
        tally.add(now - max(started, self.since))


def _stats(ctx, station, fleet) -> TendingStats:
    key = (station.station_name, fleet.name)
    if key not in ctx.tending:
        ctx.tending[key] = TendingStats()
    return ctx.tending[key]


def _cycle(block, entity, fleet, mover, station, which: str):
    """Generator: the cobot runs its `pick` or `place` cycle (handling time,
    busy power); the fleet's flat time when the station declares none.
    Returns the duration."""
    seconds = cycle_seconds(block, entity, fleet, station, which)
    if seconds is None:
        seconds = fleet.duration(block, entity,
                                 fleet.load_time if which == "pick" else fleet.unload_time)
    bus = block.ctx.bus
    if bus.has_subscribers:
        bus.emit("tend", block=block, entity=entity, t=block.env.now, fleet=fleet.name,
                 unit=mover.unit, resource=station.station_name, state=which, duration=seconds)
    # D383: a state reads the pick as the trip's load, the place as its unload (D446, D447)
    mover.task = ("load" if which == "pick" else "unload", mover.task[1], seconds)
    yield from fleet.handle(mover, seconds)
    return seconds


def pick(block, entity, fleet, mover, station, waiting_since: float, until=None):
    """Generator: the cobot, at the station, takes *entity* out of it. The
    machine holding the part is free when the cycle ends (D169). *until*: a
    state's pick in progress ends then (D446); its wait was counted before."""
    stats = _stats(block.ctx, station, fleet)
    if until is None:
        stats.waited(stats.blocked_for_pick, waiting_since, block.env.now)
        seconds = yield from _cycle(block, entity, fleet, mover, station, "pick")
    else:
        seconds = mover.task[2]
        yield from fleet.handle(mover, 0.0, until)
    stats.picks += 1
    stats.pick_time.add(seconds)
    stats.picked_at = block.env.now
    blocking.accept(entity)


def place(block, entity, fleet, mover, station):
    """Generator: the cobot, arrived loaded, puts *entity* into *station*. The
    part enters the station at once and queues for the machine while still on
    the cobot, which waits (idle power, its fleet slot held) until the machine
    accepts it and only then runs the place cycle; the machine starts on the
    part when the cycle ends (D169).

    Exchange (D170): under "leave", a cobot of an `exchange` fleet first picks
    a finished part of this station that waits for a cobot of its fleet -- one
    already queued, or the first to arrive while it waits -- and carries it on
    afterwards. Returns that part's job, or None."""
    env = block.env
    stats = _stats(block.ctx, station, fleet)
    arrived = env.now
    token = blocking.arm(block, entity)
    gate = blocking.gate(block, entity)
    station.self_run(entity)
    mover.enter("idle", arrived)
    job = None
    if fleet.exchange and station.tending.get("cobot", "leave") == "leave":
        found = _queued_part(fleet, station)
        if found is None:
            call = env.event()
            stats.calls.append(call)
            yield token | call
            if call.triggered:
                found = call.value, env.now
            else:
                stats.calls.remove(call)
        if found is not None:
            job, since = found
            yield from pick(job[0], job[1], fleet, mover, station, since)
            stats.exchanges += 1
            mover.trips += 1                 # the finished part is its next trip
            fleet.loads_carried += 1
            mover.enter("idle", env.now)
    else:
        stats.loaded.append(entity)
        check_stall(env, fleet)
    yield token
    if entity in stats.loaded:
        stats.loaded.remove(entity)
    stats.waited(stats.loaded_wait, arrived, env.now)
    seconds = yield from _cycle(block, entity, fleet, mover, station, "place")
    _placed(stats, seconds, gate)
    return job


def placing(block, fleet, mover, station, gate, until):
    """Generator: a state's place in progress (D447): the cycle ends at *until*, its
    duration is the mover's task's, and the machine, which already holds the part,
    starts on it through *gate*."""
    seconds = mover.task[2]
    yield from fleet.handle(mover, 0.0, until)
    _placed(_stats(block.ctx, station, fleet), seconds, gate)


def _placed(stats, seconds, gate) -> None:
    stats.places += 1
    stats.place_time.add(seconds)
    gate.succeed()


def _queued_part(fleet, station):
    """Exchange: the finished part of *station* that has queued longest for a
    cobot of *fleet*, as `(its TRANSPORT's job, since when)`; its request is
    withdrawn the way a tour attaches a load (D116). A part already granted a
    cobot is not in the queue. None when there is none."""
    queued = [r for r in fleet.resource._core.put_queue if r.job is not None
              and getattr(r.job[1], "station", "") == station.station_name]
    if not queued:
        return None
    request = min(queued, key=lambda r: r.time)
    request.attached = True
    fleet.resource.serve_without_unit(request)
    return request.job, request.time


def call_waiting_cobot(ctx, fleet, station, job) -> bool:
    """Exchange: a finished part reaching its TRANSPORT hands *job* to a cobot
    of *fleet* waiting loaded at *station*, instead of asking for one. False
    when none waits."""
    stats = ctx.tending.get((station.station_name, fleet.name))
    if stats is None or not stats.calls:
        return False
    stats.calls.pop(0).succeed(job)
    return True


# -- the stall ---------------------------------------------------------------
# Every cobot of a fleet waits loaded for a machine that waits for a cobot of
# that fleet. `blocking.check_deadlock` cannot see it (a fleet is no node of
# its graph) and `deadlock_at_end` only once arrivals have ended.

def check_stall(env, fleet) -> None:
    """Raise when no cobot of *fleet* can ever be freed and no part picked.
    Called at the two instants that can complete the stall: a cobot starts a
    loaded wait (`place`), a finished part starts waiting for a cobot
    (TRANSPORT).

    Certain when EVERY unit of the fleet waits loaded, with no exchange to
    come, and each machine they wait for is held for good (`_held_for_good`).
    Anything else -- a unit driving, staying, charging or out of service, a
    machine still working -- gives no verdict."""
    waiting = sorted(station for (station, name), stats in env.ctx.tending.items()
                     if name == fleet.name and stats.loaded)
    if (sum(len(env.ctx.tending[station, fleet.name].loaded) for station in waiting)
            < len(fleet.movers)):
        return
    if not all(_held_for_good(env, fleet, station) for station in waiting):
        return
    raise RuntimeError(
        f"deadlock at t={env.now:g}: every cobot of fleet '{fleet.name}' waits loaded at "
        f"{', '.join(waiting)}, whose machine holds a finished part that waits for a cobot of "
        f"the same fleet, so nothing can move again. Turn on the fleet's `exchange`, add a "
        f"cobot, put a BUFFER before the machine, or let the cobot `stay`.")


def _first(proc) -> list:
    """The first blocks after *proc* that are not transparent."""
    def crossed(p):
        return getattr(getattr(p, "block", None), "block_type", "") in blocking.TRANSPARENT_TYPES
    return blocking.stops(proc, lambda p: getattr(p, "next_process", None) or [],
                          lambda p: not crossed(p), crossed)


def machine_of(station):
    """The one block after *station* whose job a part a cobot places there starts: the
    first block that is not transparent (a state's place in progress, D447; validation
    makes it one PROCESS or MOTION). None when that is not one block: nothing after the
    station, or several (a DECIDE after it)."""
    found = _first(station)
    return found[0] if len(found) == 1 else None


def _held_for_good(env, fleet, station: str) -> bool:
    """Whether the machine of *station* can never take the part a cobot of
    *fleet* holds for it: it is one PROCESS (or MOTION) whose finished parts
    all go on by this fleet, and every unit of its resource holds one.

    Conservative, so False for anything that could still let a part in or
    out: a finite space, balking or reneging, preemption, a resource set, a
    capacity schedule, a unit out of service or shared with another block."""
    ctx = env.ctx
    machines = _first(ctx.stations[station])
    machine = machines[0] if len(machines) == 1 else None
    if (machine is None or machine.block.block_type not in blocking.PROCESS_TYPES
            or not machine.block_after_service or machine.space is not None
            or machine.balk_if or machine.renege_after or machine.preempt):
        return False
    resource = ctx.resource_manager.get(machine.resource_name)
    if resource is None or resource.has_controller:
        return False
    if not all(getattr(t, "fleet_name", None) == fleet.name and t.space_passthrough
               for t in _first(machine)):
        return False
    # A pick that ended this instant has not reached `machine.blocked` yet.
    if ctx.tending[station, fleet.name].picked_at == env.now:
        return False
    return machine.blocked.level * machine.quantity >= resource.capacity


def stay(block, entity, fleet, request, mover, station) -> bool:
    """The cobot that placed *entity* keeps its fleet slot and waits at the
    station (idle) until the part reaches a TRANSPORT or a DISPOSE. False, and
    nothing kept, when the part left the model while the cobot was still
    placing it (a balk or renege routed to a DISPOSE, a BATCH that absorbed
    it): the caller then frees the cobot as under "leave"."""
    if getattr(entity, "finished", False) or entity not in block.ctx.live_entities:
        return False
    env = block.env
    setattr(entity, _COBOT, (fleet, request, mover, env.now, _stats(env.ctx, station, fleet)))
    mover.enter("idle", env.now)
    fleet._moved(block, mover, (mover.node,), 0.0, "idle")      # playback: no longer handling
    return True


def cobot_of(block, entity, fleet=None):
    """The cobot that stayed with *entity*, taken off it: `(request, mover)`
    when it is one of *fleet* (its TRANSPORT then uses it with no dispatch),
    freed otherwise -- the part went to another fleet or left the model.
    None when no cobot stayed. Called wherever a part leaves `live_entities`
    (DISPOSE, a BATCH absorbing it, SEPARATE destroying a batch)."""
    held = getattr(entity, _COBOT, None)
    if held is None:
        return None
    delattr(entity, _COBOT)
    theirs, request, mover, since, stats = held
    stats.waited(stats.stay, since, block.env.now)
    if theirs is fleet:
        return request, mover
    theirs.free(request, mover, block=block, entity=entity)
    theirs.park_home(mover, block=block)
    return None


# -- validation --------------------------------------------------------------

def validate_tending(layout: dict, problems: list) -> None:
    """The `fleets` section's hook (core/sections.py): every fleet's `arm` and
    `exchange`, and every STATION's `tending` group, which needs the arm
    fleets, the profiles and the links."""
    profiles = {str(p.get("name", "")).strip(): p for p in layout.get("motion_profiles") or []
                if isinstance(p, dict)}
    arms: dict[str, dict] = {}                      # fleet name -> record, the fleets with an arm
    for index, fleet in enumerate(layout.get("fleets") or []):
        if not isinstance(fleet, dict):
            continue
        name = str(fleet.get("name", index)).strip()

        def bad(field, text, hint, name=name, index=index):
            problems.append(problem("E_FLEET_ARM", f"fleet '{name}': {text}",
                                    f"/fleets/{index}/{field}", hint=hint))

        arm = str(fleet.get("arm") or "").strip()
        if not arm:
            if fleet.get("exchange"):
                bad("exchange", "exchange needs an arm",
                    "set arm to a motion profile, or drop exchange")
            continue
        arms[name] = fleet
        if arm not in profiles:
            bad("arm", f"unknown motion profile '{arm}'", "declare it under \"motion_profiles\"; "
                f"declared: {', '.join(sorted(profiles)) or '(none)'}")
        if fleet.get("kind") == "worker":
            bad("arm", "a worker fleet has no arm", "drop arm, or use kind \"vehicle\"")
        if fleet.get("motion") == "gantry":
            bad("arm", "a gantry has no arm", "drop arm, or use motion \"network\"")
        if (fleet.get("loads") or 1) != 1:
            bad("loads", "an arm fleet carries one load",
                "drop loads; exchange is its only second place")

    graph = None
    for index, block in enumerate(layout.get("blocks") or []):
        if (str(block.get("type", "")).upper() == "STATION"
                and (block.get("params") or {}).get("tending")):
            graph = graph or _Graph(layout)
            _validate_station(block, f"/blocks/{index}/params/tending", arms, profiles, graph,
                              problems)


class _Graph:
    """What a tending station's checks read from the rest of the layout."""

    def __init__(self, layout: dict):
        self.by_name = {b.get("name"): b for b in layout.get("blocks") or []}
        self.out: dict[str, list] = {}
        for link in layout.get("links") or []:
            self.out.setdefault(link.get("source"), []).append(link.get("target"))
        network = layout.get("network") if isinstance(layout.get("network"), dict) else {}
        self.nodes = {str(n.get("name", "")).strip() for n in network.get("nodes") or []
                      if isinstance(n, dict)}
        self.routed = {step.get("station") for s in layout.get("sequences") or []
                       for step in s.get("steps") or []}
        self.zones = motion.zone_names(layout)

    def kind(self, name) -> str:
        return str((self.by_name.get(name) or {}).get("type", "")).upper()

    def params(self, name) -> dict:
        return (self.by_name.get(name) or {}).get("params") or {}

    def first(self, start, is_stop) -> list:
        """The first blocks matching *is_stop* on every way out of *start*."""
        return blocking.stops(start, lambda n: self.out.get(n, []), is_stop, lambda n: True)

    def fleets_out(self, start) -> set:
        """The fleets of the first TRANSPORTs a part leaving *start* reaches."""
        return {str(self.params(t).get("fleet", "")).strip()
                for t in self.first(start, lambda n: self.kind(n) == "TRANSPORT")}

    def fleets_in(self, names: set) -> set:
        """The fleets of the TRANSPORTs that may deliver to a station called
        one of *names*: by `destination`, or by a routing that names it."""
        found = set()
        for block in self.by_name:
            params = self.params(block)
            destination = params.get("destination")
            if self.kind(block) == "TRANSPORT" and (
                    destination in names or (not destination and names & self.routed)):
                found.add(str(params.get("fleet", "")).strip())
        return found

    def has_space(self, start) -> bool:
        """A finite space before the machine: the first block that is not
        transparent is a BUFFER with places, or has a `queue_capacity`."""
        machines = self.first(start, lambda n: self.kind(n) not in blocking.TRANSPARENT_TYPES)
        return any((self.kind(m) == "BUFFER" and _int(self.params(m).get("capacity", 1)) > 0)
                   or _int(self.params(m).get("queue_capacity")) > 0 for m in machines)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _validate_station(block: dict, at: str, arms: dict, profiles: dict, graph: _Graph,
                      problems: list) -> None:
    name = block.get("name", "<unnamed>")
    params = block.get("params") or {}
    group = params["tending"]

    def report(code, text, path, hint):
        # MOTION's segment and zone checks report E_MOTION_*: one code here.
        code = code if code.startswith("W_") else "E_TENDING"
        problems.append(problem(code, f"{name}: tending: {text}", f"{at}/{path}".rstrip("/"),
                                block=name, hint=hint))

    if not isinstance(group, dict):
        report("E_TENDING", "must be an object", "", "{\"place\": [...], \"pick\": [...]}")
        return
    for field in sorted(set(group) - set(_FIELDS)):
        report("E_TENDING", f"unknown field '{field}'", field, f"one of: {', '.join(_FIELDS)}")
    for which in ("place", "pick"):
        segments = group.get(which)
        if segments is None:
            continue
        if not isinstance(segments, list):
            report("E_TENDING", f"{which} must be a list of segments", which,
                   "as a MOTION block's `segments`")
            continue
        for index, segment in enumerate(segments):
            # A time expression is checked where MOTION's are (core/schema/layout.py).
            motion.validate_segment(
                segment, index, None,
                lambda code, text, path, hint, which=which: report(
                    code, f"{which} {text}", path.replace("segments", which, 1), hint))
    unit = group.get("time_unit", "seconds")
    if unit != "base" and unit not in UNITS:
        report("E_TENDING", f"unknown time_unit '{unit}'", "time_unit",
               f"\"base\" or one of: {', '.join(UNITS)}")
    motion.validate_hrc(group, None, graph.nodes, report, graph.zones)
    if group.get("hrc", "none") in motion.HRC_MODES[1:]:
        for fleet, record in sorted(arms.items()):
            profile = profiles.get(str(record.get("arm") or "").strip())
            if profile is not None and not profile.get("collaborative_speed"):
                report("E_TENDING", f"hrc '{group['hrc']}' needs collaborative_speed on "
                       f"'{profile.get('name')}', the arm of fleet '{fleet}'", "hrc",
                       "set collaborative_speed (m/s) on the motion profile")
    mode = group.get("cobot", "leave")
    if mode not in COBOT_MODES:
        report("E_TENDING", f"unknown cobot mode '{mode}'", "cobot",
               f"one of: {', '.join(COBOT_MODES)}")
    if not str(params.get("node", "") or "").strip():
        report("E_TENDING", "the station sits on no node", "",
               "set the station's `node` to the network node the cobot drives to")
    if not arms:
        report("W_TENDING_UNUSED", "no fleet has an arm, so the station is served as a plain "
               "one", "", "set `arm` on the fleet that tends it")
        return
    leaving = graph.fleets_out(name)
    if mode == "stay" and not leaving & set(arms):
        report("W_TENDING_STAY", "the cobot stays, but no TRANSPORT of an arm fleet takes the "
               "part on", "cobot", "it only waits to be freed: use \"leave\", or carry the part "
                                   "on with the same fleet")
    # One cobot both feeds and empties a machine with nowhere to put the new
    # part: it arrives loaded while the machine waits for it to pick.
    feeding = graph.fleets_in({name, str(params.get("station") or "").strip() or name})
    if mode == "leave" and len(leaving) == 1 and leaving == feeding:
        fleet = arms.get(next(iter(leaving)))
        if (fleet is not None and not fleet.get("exchange") and fleet.get("count", 1) == 1
                and not graph.has_space(name)):
            report("W_TENDING_DEADLOCK", f"the one cobot of '{fleet.get('name')}' both feeds and "
                   "empties this machine: it will wait loaded for a machine that waits to be "
                   "emptied", "", "add a cobot, set `exchange` on the fleet, put a BUFFER before "
                                  "the machine, or use cobot \"stay\"")
