"""The `cranes` section: overhead and gantry cranes drawn moving in playback (D1110).

Layout only, like `background` and `building`: the run never reads it. A crane names the
blocks it serves and the runway its bridge travels over, in plan metres (y down):

    "cranes": [{"name": "Line crane", "runway": [x0, y0, x1, y1],
                "serves": ["Exit", "Ground", "Bay"], "axis": "x", "home": [x, y],
                "colour": "#F2C14E", "trail": false, "park_after": 1.0, "speed": 30}]

- A traced `entity_moved` with a duration between two blocks one crane serves is a LIFT. Its
  start and duration stay the run's; playback only splits it into the phases below.
- `axis` is the plan axis the bridge travels along (default: the runway's long side, as the
  3D yard's `crane_axis`); the bridge spans the runway across it, the trolley runs along it.
- `home` is where the hook waits before the first lift (default: the runway's centre), and
  where it goes back to when idle (D1112): after a lift, when the gap until the crane's next
  lift is at least `park_after` minutes (default PARK_AFTER; `false` never), the empty hook
  travels home at `speed` plan metres a minute (default PARK_SPEED), its trip cut short to fit
  the gap, waits there, and the next lift's approach starts from home. A shorter gap keeps it
  over its last drop, so back-to-back lifts do not jitter. Every lift keeps its own times.
- `trail` (default false) draws a faint dashed line from the last lift's pick to its drop.
- One hook makes one lift at a time. Where a model lets a crane's moves overlap, the lifts are
  shown one after another, in equal shares of their joint window (as the 3D yard does): a
  lift's `s0, s1` is when it is SHOWN, its `t0, t1` when it happens in the run.

Pure Python; the 2D player (`product/ui/animation.py`) draws it, so any client can.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from nagasim.core.problems import problem

__all__ = ["PHASES", "GRAB", "LAND", "HOIST", "PARK_AFTER", "PARK_SPEED", "Lift", "validate_cranes",
           "crane_axis", "home_of", "park_options", "park_time", "lifts", "hook_at"]

# A lift's phases as shares of the traced move, the same as the 3D yard's (film3d/yard.py
# PHASES, kept equal by a test): the empty hook travels to the load, lowers, hoists it,
# carries it, sets it down and rises again.
PHASES = (("approach", 0.18), ("lower", 0.1), ("hoist", 0.1), ("travel", 0.37), ("set", 0.15),
          ("release", 0.1))
GRAB = 0.28     # the load leaves its spot at the end of "lower"
LAND = 0.90     # and lands at the end of "set"
HOIST = 0.75    # the hook's height while it travels, a share of the trolley's (0 = at the floor)
# D1112: an idle hook goes home when the gap to its next lift is at least PARK_AFTER minutes,
# at PARK_SPEED plan metres a minute. Shorter gaps keep it put: a hook that left and came
# straight back between two close lifts would only jitter. The 3D yard (film3d/yard.py) uses
# the same rule and defaults, kept equal by a test.
PARK_AFTER = 1.0
PARK_SPEED = 30.0

_FIELDS = {"name", "runway", "serves", "axis", "home", "colour", "trail", "park_after", "speed"}
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_cranes(layout: dict, problems: list) -> None:
    """Shape of the optional `cranes` section (a `core/sections.py` hook); absent is none."""
    section = layout.get("cranes")
    if section is None:
        return

    def bad(text, path="/cranes"):
        problems.append(problem("E_CRANE", f"cranes: {text}", path,
                                hint='[{"name": "Crane", "runway": [x0, y0, x1, y1], '
                                     '"serves": ["A", "B"]}]'))

    if not isinstance(section, list):
        bad("must be a list of cranes")
        return
    seen = set()
    for i, crane in enumerate(section):
        path = f"/cranes/{i}"
        if not isinstance(crane, dict):
            bad("a crane must be an object", path)
            continue
        for field in sorted(set(crane) - _FIELDS):
            bad(f"unknown crane field '{field}'", f"{path}/{field}")
        name = crane.get("name")
        if not (isinstance(name, str) and name.strip()):
            bad("a crane needs a name", f"{path}/name")
        elif name in seen:
            bad(f"two cranes are named '{name}'", f"{path}/name")
        seen.add(name)
        runway = crane.get("runway")
        if not (isinstance(runway, (list, tuple)) and len(runway) == 4 and all(map(_num, runway))
                and runway[0] < runway[2] and runway[1] < runway[3]):
            bad("runway must be [x0, y0, x1, y1] in metres with x0 < x1 and y0 < y1",
                f"{path}/runway")
        serves = crane.get("serves")
        if not (isinstance(serves, list) and len(serves) >= 2
                and all(isinstance(s, str) and s for s in serves)):
            bad("serves must list at least two block names", f"{path}/serves")
        if "axis" in crane and crane["axis"] not in ("x", "y"):
            bad("axis must be \"x\" or \"y\"", f"{path}/axis")
        home = crane.get("home")
        if home is not None and not (isinstance(home, (list, tuple)) and len(home) == 2
                                     and all(map(_num, home))):
            bad("home must be [x, y] in metres", f"{path}/home")
        if "trail" in crane and not isinstance(crane["trail"], bool):
            bad("trail must be true or false", f"{path}/trail")
        park = crane.get("park_after")
        if park is not None and park is not False and not (_num(park) and park >= 0):
            bad("park_after must be minutes (0 or more) or false", f"{path}/park_after")
        if "speed" in crane and not (_num(crane["speed"]) and crane["speed"] > 0):
            bad("speed must be plan metres a minute, above 0", f"{path}/speed")
        if "colour" in crane and not (isinstance(crane["colour"], str)
                                      and _HEX.match(crane["colour"])):
            bad("colour must be a \"#RRGGBB\" colour", f"{path}/colour")


def crane_axis(crane: dict) -> str:
    """The plan axis the bridge travels along: the crane's own `axis`, else the runway's long side."""
    if crane.get("axis") in ("x", "y"):
        return crane["axis"]
    x0, y0, x1, y1 = crane["runway"]
    return "x" if abs(x1 - x0) >= abs(y1 - y0) else "y"


def home_of(crane: dict) -> tuple[float, float]:
    if crane.get("home") is not None:
        return float(crane["home"][0]), float(crane["home"][1])
    x0, y0, x1, y1 = crane["runway"]
    return (x0 + x1) / 2, (y0 + y1) / 2


def park_options(crane: dict) -> tuple[float | None, float]:
    """(park_after minutes or None for never, speed in plan metres a minute) of a crane."""
    park = crane.get("park_after", PARK_AFTER)
    after = None if park is False or park is None else float(park)
    return after, float(crane.get("speed", PARK_SPEED))


def park_time(gap: float, frm, home, after: float | None, speed: float) -> float | None:
    """How long an idle hook at *frm* takes to go home in a *gap* (minutes) before its next
    lift: None when it stays put (no parking, or a gap under *after*); else the trip at
    *speed* (the caller's units a minute), cut short to fit the gap."""
    if after is None or gap < after:
        return None
    dist = ((home[0] - frm[0]) ** 2 + (home[1] - frm[1]) ** 2) ** 0.5
    return min(dist / speed if speed > 0 else 0.0, gap)


@dataclass(frozen=True)
class Lift:
    """One traced move a crane makes. `src_slot` is the load's place in the source block's
    arrival order just before it left (None: it was not in that block, e.g. it rode a carrier
    that was absorbed into it); `dst_slot` its place in the target block's order on arrival.
    `s0, s1` is the window it is shown in (`t0, t1` unless it overlaps another lift)."""
    crane: str
    entity_id: int
    e_type: str
    src: str
    dst: str
    t0: float
    t1: float
    src_slot: int | None
    dst_slot: int | None
    s0: float | None = None
    s1: float | None = None


def lifts(events, cranes) -> dict[str, list[Lift]]:
    """{crane name: its lifts in start order} from a trace's events. One pass replays which
    entities are in which block, in arrival order, with the Timeline's rules: a move with a
    duration enters its target at t + duration, an arrival is applied before any event at
    the same instant, a disposal or absorption leaves the block."""
    import heapq

    serves = [(c["name"], set(c["serves"])) for c in cranes or ()
              if isinstance(c, dict) and c.get("name") and isinstance(c.get("serves"), list)]
    out: dict[str, list[Lift]] = {name: [] for name, _ in serves}
    if not serves:
        return out
    order: dict[str, list[int]] = {}       # block -> entity ids in arrival order
    where: dict[int, str] = {}             # entity id -> its block
    transit: dict[int, tuple] = {}         # entity id -> (target, arrival t, pending lift or None)
    due: list = []                         # (arrival t, seq, entity id)
    seq = 0

    def enter(eid, block):
        order.setdefault(block, []).append(eid)
        where[eid] = block

    def leave(eid):
        transit.pop(eid, None)
        block = where.pop(eid, None)
        if block is not None:
            order[block].remove(eid)

    def arrive_until(t):
        while due and due[0][0] <= t:
            t_arr, _s, eid = heapq.heappop(due)
            moving = transit.get(eid)
            if moving is None or moving[1] != t_arr:
                continue           # it moved on or ended first (the Timeline's #336 rule)
            del transit[eid]
            target, _t, pending = moving
            if pending is not None:
                out[pending[0]].append(Lift(*pending, dst_slot=len(order.get(target, ()))))
            enter(eid, target)

    for e in sorted(events, key=lambda e: e.t):
        arrive_until(e.t)
        if e.kind == "entity_created":
            enter(e.entity_id, e.block)
        elif e.kind == "entity_moved" and e.target and e.target != e.block:
            here = order.get(e.block, [])
            slot = here.index(e.entity_id) if where.get(e.entity_id) == e.block else None
            leave(e.entity_id)
            if e.duration > 0:
                crane = next((name for name, s in serves if e.block in s and e.target in s), None)
                pending = None if crane is None else (
                    crane, e.entity_id, e.e_type, e.block, e.target, e.t, e.t + e.duration, slot)
                t_arr = e.t + e.duration
                transit[e.entity_id] = (e.target, t_arr, pending)
                seq += 1
                heapq.heappush(due, (t_arr, seq, e.entity_id))
            else:
                enter(e.entity_id, e.target)
        elif e.kind in ("entity_disposed", "entity_absorbed"):
            leave(e.entity_id)
    arrive_until(float("inf"))
    for name in out:
        out[name] = _one_at_a_time(sorted(out[name], key=lambda lf: lf.t0))
    return out


def _one_at_a_time(lifts: list) -> list:
    """Each lift with its display window: overlapping lifts share their joint window in order."""
    out, group = [], []

    def flush():
        a, b = group[0].t0, max(lf.t1 for lf in group)
        step = (b - a) / len(group)
        out.extend(replace(lf, s0=a + k * step, s1=a + (k + 1) * step) for k, lf in enumerate(group))

    for lf in lifts:
        if group and lf.t0 >= max(g.t1 for g in group):
            flush()
            group = []
        group.append(lf)
    if group:
        flush()
    return out


def _smooth(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


def _lerp(a, b, u):
    return a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u


def hook_at(crane_lifts: list, t: float, home, place, park=(None, PARK_SPEED)):
    """Where a crane's hook is at *t*: `(x, y, height, carrying, lift, phase)`.

    `x, y` is the floor point the hook hangs over, in the caller's frame; `height` is the
    hook's height as a share of the trolley's (0 at the floor, HOIST while it travels);
    `carrying` the Lift whose load hangs on it, else None; `lift` the lift in progress or
    last done (None before the first); `phase` its phase name, "idle" between lifts.
    *place(lift)* gives `(pick, drop)` floor points, *home* the point before the first lift.
    *park* is `(park_after, speed)` (`park_options`, speed in the caller's units a minute):
    with park_after None the hook waits over its last drop, as before D1112.
    """
    import bisect

    after, speed = park
    starts = [lf.s0 for lf in crane_lifts]
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        return home[0], home[1], HOIST, None, None, "idle"
    lift = crane_lifts[i]
    pick, drop = place(lift)
    span = lift.s1 - lift.s0
    if t >= lift.s1 or span <= 0:
        gap = (crane_lifts[i + 1].s0 if i + 1 < len(crane_lifts) else float("inf")) - lift.s1
        trip = park_time(gap, drop, home, after, speed)
        if trip is None:
            return drop[0], drop[1], HOIST, None, lift, "idle"
        x, y = _lerp(drop, home, _smooth((t - lift.s1) / trip) if trip > 0 else 1.0)
        return x, y, HOIST, None, lift, "idle"
    before = home
    if i > 0:
        prev = crane_lifts[i - 1]
        before = place(prev)[1]
        if park_time(lift.s0 - prev.s1, before, home, after, speed) is not None:
            before = home
    u = (t - lift.s0) / span
    edge = 0.0
    for name, share in PHASES:
        if u < edge + share or name == PHASES[-1][0]:
            v = min(1.0, max(0.0, (u - edge) / share))
            break
        edge += share
    carrying = lift if GRAB <= u < LAND else None
    if name == "approach":
        x, y = _lerp(before, pick, _smooth(v))
        return x, y, HOIST, None, lift, name
    if name in ("lower", "hoist"):
        z = HOIST * (1 - _smooth(v)) if name == "lower" else HOIST * _smooth(v)
        return pick[0], pick[1], z, carrying, lift, name
    if name == "travel":
        x, y = _lerp(pick, drop, _smooth(v))
        return x, y, HOIST, carrying, lift, name
    z = HOIST * (1 - _smooth(v)) if name == "set" else HOIST * _smooth(v)
    return drop[0], drop[1], z, carrying, lift, name
