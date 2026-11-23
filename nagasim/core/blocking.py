"""Blocking after service and finite buffers (PHASE4 T5, ARCHITECTURE D78).

A machine that has finished a part cannot always put it down: with a finite
buffer downstream it keeps holding the part -- and its resource unit -- until
there is room. That is *blocking after service*, the throughput loss every
production line with finite buffers has, and this module is the whole of it.

One mechanism, three pieces:

* **Space.** A BUFFER block's places, or the `queue_capacity` of a SEIZE /
  PROCESS: a fixed number of places an entity must take before it may wait
  there. A `simpy.Resource` of that capacity, so several machines blocked on
  one buffer are served FIFO by the instant each one blocked.
* **The token.** A block that blocks after service forwards the entity
  carrying a one-shot event and only releases its resource once somebody
  triggers it -- "the part has been accepted". Until then the entity is
  physically at the next block and logically still in the machine.
* **Acceptance.** The token is consumed by the first block that gives the
  entity a place of its own (a buffer place, or the resource of the next
  station) or that is not TRANSPARENT to it. ASSIGN, RECORD, DECIDE, STATION
  and LOGIC are transparent: they take no time and hold nothing, so an entity
  crosses them without ever leaving the machine. Everything else -- a DELAY, a
  BATCH, a link with a transfer delay, a DISPOSE -- accepts the entity on
  arrival, because it really does take it off the machine. A CONVEYOR owns a
  finite space of its own (its cells, PHASE4 T7) and accepts the part only once
  it is physically on the belt, so a full or stopped belt blocks the machine
  feeding it instead of queueing invisibly. A belt whose transparent walk
  reaches another belt is itself such a machine (PHASE6 W3 M6, D115): its end
  part stays on it, holding its cell, until the next belt takes it. A link with
  a transfer time and a capacity (`core/arealink.py`, D1369) is a finite space
  too, and a belt feeding it holds its end part the same way.

Blocking is never a switch the user sets: a block holds its resource exactly
when a finite space is statically reachable from it through transparent blocks
(`analyse`), so a model with no BUFFER and no `queue_capacity` runs the code it
always did, and pays one boolean test per forward for the feature.

Blocking BEFORE service (PHASE5 O4, ARCHITECTURE 8 #11a) is the same space
used the other way round: a station with `hold_until_space` reserves a place
in the downstream space (`reserve`) after it seizes and before it starts the
job, and the part carries the reservation to that space, which `Space.take`
honours instead of queueing for a second place.
"""
from __future__ import annotations

import logging

import simpy

from nagasim.core.conveyor_checks import keep_run_warning
from nagasim.core.stats import TimeWeighted

log = logging.getLogger(__name__)

__all__ = ["Space", "TRANSPARENT_TYPES", "accept", "arm", "holder_of", "analyse", "cycles", "gate", "placed",
           "hold_until_accepted", "layout_blocking_graph", "on_forward", "reserve",
           "drop_reservation", "hold_target", "resource_name_of", "check_deadlock",
           "deadlock_at_end", "PROCESS_TYPES", "BELT_TYPES"]

# Block types an entity crosses without ever leaving the machine that holds it:
# they take no simulation time, hold nothing and have no space of their own.
# Kept as a literal: blocks import this module, so it cannot import the block
# registry; tests/test_block_registry.py checks it equals the SPEC.transparent flags.
TRANSPARENT_TYPES = ("ASSIGN", "RECORD", "DECIDE", "STATION", "LOGIC")

# The block types that ARE a PROCESS at run time (D156): MOTION subclasses it
# and differs only in its service, so every "is this a PROCESS" check reads this.
PROCESS_TYPES = ("PROCESS", "MOTION")

# The conveyor pieces that hold a part until the next piece takes it (D115): a belt, and a
# transfer module (conveyor system A8), which is busy until its load's handshake.
BELT_TYPES = ("CONVEYOR", "CONVEYOR_TRANSFER")

_TOKEN = "_space_token"
# (space, request): a place reserved before service, carried to that space.
_RESERVED = "_space_reserved"
# The event a part a cobot is still placing carries (`gate`, `placed`).
_GATE = "_place_gate"


class Space:
    """The finite input space of one block: `capacity` places and their stats.

    `capacity` 0 is legal and means no waiting place at all -- the part stays
    in the machine that made it until the next station starts it, which is
    blocking after service with no buffer.
    """

    def __init__(self, env, capacity: int, owner=None):
        self.env = env
        self.capacity = max(int(capacity), 0)
        self.owner = owner
        self._pool = simpy.Resource(env, capacity=self.capacity) if self.capacity else None
        self.content = TimeWeighted(env.now)
        # 1 while every place is taken; a zero-capacity space is always full.
        self.full = TimeWeighted(env.now, level=1.0 if not self.capacity else 0.0)
        self.entered = 0

    def take(self, entity, accepts: bool = True):
        """Generator: take one place, then free whatever still holds *entity*.

        Returns the request to hand back to `give_back`. Never called on a
        zero-capacity space -- that one has nothing to take.

        *accepts* False takes the place WITHOUT releasing the upstream machine:
        a CONVEYOR reserves its entry cell first and accepts the part only once
        it is physically on the belt, which is after the stopped-belt wait and
        the entry rhythm (PHASE4 T7).
        """
        held = getattr(entity, _RESERVED, None)
        if held is not None and held[0] is self:
            # Reserved before service (blocking BEFORE service): the place was
            # counted when it was granted, so the part just steps into it.
            delattr(entity, _RESERVED)
            request = held[1]
        else:
            request = self._pool.request()
            request.entity = entity         # D383: who waits for a place, for a state
            yield request
            self._granted()
        if accepts:
            accept(entity)
            yield from placed(entity)
        return request

    def claim(self):
        """A place taken at once, for a state putting back what had one: the request to hand
        back to `give_back`."""
        request = self._pool.request()
        self._granted()
        return request

    def room_for(self, entity) -> bool:
        """D1392: a place for *entity* now, nobody queueing for one (`take_now` takes it)."""
        return self.content.level < self.capacity and not self.waiting

    def take_now(self, entity):
        """D1392: a place taken in this instant, when `room_for` says there is one: the
        request to hand back to `give_back`."""
        request = self._pool.request()
        request.entity = entity
        self._granted()
        return request

    def occupy(self, entity) -> None:
        """A state puts *entity* back in a place it had (D382): taken at once and carried
        as a reservation, so `take` steps into it without a wait of its own."""
        setattr(entity, _RESERVED, (self, self.claim()))

    def _granted(self) -> None:
        self.entered += 1
        self.content.add(1, self.env.now)
        self._sync()

    def give_back(self, request) -> None:
        self._pool.release(request)
        self.content.add(-1, self.env.now)
        self._sync()

    def has_room(self) -> bool:
        """The next part offered would get a place now (a typed belt's own rule, D1355)."""
        return self.content.level < self.capacity

    def _sync(self) -> None:
        self.full.set(0.0 if self.has_room() else 1.0, self.env.now)

    @property
    def waiting(self) -> int:
        """Entities queued for a place -- upstream that is not itself blocking."""
        return len(self._pool.queue) if self._pool is not None else 0

    def reset_stats(self, t: float) -> None:
        """Warm-up hook: zero the accumulators, keep the current content."""
        self.content.reset(t)
        self.full.reset(t)
        self.entered = 0


# ---------------------------------------------------------------- the token
def arm(block, entity):
    """Keep *entity* in *block* until somebody accepts it; returns the event."""
    token = block.env.event()
    token.holder = block            # D383: who still holds the entity, for a state
    setattr(entity, _TOKEN, token)
    return token


def accept(entity) -> None:
    """Take *entity* off the machine still holding it (a no-op for an entity
    nobody is holding)."""
    token = getattr(entity, _TOKEN, None)
    if token is None:
        return
    delattr(entity, _TOKEN)
    if not token.triggered:
        token.succeed()


def holder_of(entity):
    """The block that still holds *entity* (a BUFFER whose place it has, a station blocked
    after service), or None."""
    return getattr(getattr(entity, _TOKEN, None), "holder", None)


def gate(block, entity):
    """Tending (D169): a cobot is about to put *entity* into a station. The
    returned event is what the block that accepts the part waits for (`placed`)
    before it works on it; the cobot succeeds it when its place cycle ends."""
    event = block.env.event()
    setattr(entity, _GATE, event)
    return event


def placed(entity):
    """Generator, called right after a block accepts *entity*: wait until the
    cobot still placing it has finished. Returns at once for any other."""
    event = getattr(entity, _GATE, None)
    if event is not None and not event.triggered:
        yield event


def on_forward(entity, target) -> None:
    """`BaseProcess.forward`'s hook: a target that is not transparent to the
    token takes the entity off the machine right here."""
    if getattr(entity, _TOKEN, None) is None:
        return
    if getattr(target, "space_passthrough", False):
        return
    if getattr(getattr(target, "block", None), "block_type", "") in TRANSPARENT_TYPES:
        return
    accept(entity)


def hold_until_accepted(block, entity, index: int = 0, forward: bool = True):
    """Generator: forward *entity* and keep holding the resource until some
    block downstream accepts it. The caller releases once this returns.

    `block.blocked` is the time-weighted "this station is blocked" collector,
    and the resource gets a `blocked` state event for playback. An entity
    accepted in the same instant costs a zero-length interval, i.e. nothing.
    *forward* False: a state put the part back where it waits (D381), it is not sent again.
    """
    token = arm(block, entity)
    if forward:
        block.forward(entity, index)
    if token.triggered:
        return
    block.blocked.add(1, block.env.now)
    _emit_blocked(block, 1)
    _notify_bottleneck_blocked(block)
    check_deadlock(block)
    yield token
    block.blocked.add(-1, block.env.now)
    _emit_blocked(block, 0)
    _notify_bottleneck_blocked(block)


def reserve(block, entity):
    """Generator: blocking BEFORE service (PHASE5 O4). Hold the station, idle,
    until the downstream space has a free place, then reserve it for *entity*.

    The place counts as taken from here on (content and `Time Full %` include
    it), which is what makes the next job wait: the literature's "blocking
    before service, position occupied". The wait is the station's `blocked`
    time, the same collector and trace state blocking after service uses.
    """
    space = block.reserve_space
    request = space._pool.request()
    if not request.triggered:          # SimPy grants a free place synchronously
        block.blocked.add(1, block.env.now)
        _emit_blocked(block, 1)
        _notify_bottleneck_blocked(block)
        check_deadlock(block)
        yield request
        block.blocked.add(-1, block.env.now)
        _emit_blocked(block, 0)
        _notify_bottleneck_blocked(block)
    space._granted()
    setattr(entity, _RESERVED, (space, request))


def drop_reservation(entity) -> None:
    """Give back a place *entity* reserved but will never use (it left by a
    queue's second output instead)."""
    held = getattr(entity, _RESERVED, None)
    if held is None:
        return
    delattr(entity, _RESERVED)
    held[0].give_back(held[1])


def hold_target(space_of: str, out_links: list) -> str:
    """The block whose space a `hold_until_space` station reserves: the named
    `space_of`, else the target of its first outgoing link."""
    return space_of or (out_links[0] if out_links else "")


def resource_name_of(block) -> str:
    """The resource a station holds, whichever block type it is (PROCESS keeps
    the name, SEIZE and RELEASE the resolved object)."""
    return (getattr(block, "target_name", "")
            or getattr(getattr(block, "resource", None), "name", "") or "")


def _emit_blocked(block, value: int) -> None:
    """Publish the resource's `blocked` state, for the trace and playback
    (`docs/ASSET_GRAPHICS.md`); nothing at all when no one is listening."""
    if not block.ctx.bus.has_subscribers:
        return
    resource = block.ctx.resource_manager.resolve(resource_name_of(block))
    if resource is not None and hasattr(resource, "emit_state"):
        resource.emit_state("blocked", value)


def _notify_bottleneck_blocked(block) -> None:
    """Feed the Roser tracker (PHASE6 W3 M2, ARCHITECTURE D113): a blocked
    unit is INACTIVE even though it is still seized (`resource.busy` counts
    it). Independent of `_emit_blocked`'s bus-subscriber guard -- the tracker
    runs whether or not anything is watching the animation. It reports the
    node's blocked LEVEL (units), keyed by node, so a multi-unit station or
    two blocking blocks on one resource net out correctly (#190)."""
    block.ctx.wake_power()       # a "blocked" kW of its own changes POWER() (D600)
    tracker = getattr(block.ctx, "bottleneck_tracker", None)
    if tracker is None:
        return
    resource = block.ctx.resource_manager.resolve(resource_name_of(block))
    if resource is not None:
        tracker.blocked(resource.name, block.blocked.level, block.env.now, node=block.name)


# ------------------------------------------------------- the blocking graph
def stops(start, successors, is_stop, is_transparent) -> list:
    """The first blocks an entity leaving *start* can reach without leaving
    the machine that holds it: the walk crosses transparent blocks and ends at
    anything that can hold the entity itself."""
    seen, found = set(), []
    stack = list(successors(start))
    while stack:
        node = stack.pop()
        key = node if isinstance(node, str) else id(node)
        if key in seen:
            continue
        seen.add(key)
        if is_stop(node):
            found.append(node)
        elif is_transparent(node):
            stack.extend(successors(node))
    return found


def cycles(edges: dict) -> list[tuple]:
    """Every elementary cycle of *edges* ({node: [node, ...]}), each starting
    at its alphabetically smallest member so the same loop is reported once.

    The graph has one node per buffer and per blocking station, so it is tiny;
    a plain DFS is the whole algorithm.
    """
    found: set[tuple] = set()
    path: list = []
    on_path: set = set()

    def walk(node):
        path.append(node)
        on_path.add(node)
        for nxt in edges.get(node, ()):
            if nxt in on_path:
                loop = path[path.index(nxt):]
                start = loop.index(min(loop))
                found.add(tuple(loop[start:] + loop[:start]))
            elif nxt in edges:
                walk(nxt)
        path.pop()
        on_path.discard(node)

    for node in sorted(edges):
        walk(node)
    return sorted(found)


def layout_blocking_graph(blocks: list, out_links: dict) -> tuple[dict, set]:
    """The blocking-dependency graph of a LAYOUT dict: `(edges, space names)`.

    An edge means "this block cannot let go until that one has room". Edges
    out of a STATION are kept only when they land on a finite space, because a
    station whose next stop is an ordinary unbounded queue never blocks -- it
    is what breaks a loop. `core/schema/` warns about the cycles; `analyse`
    below builds the same graph from the built processes at run time.
    """
    by_name = {b.get("name"): b for b in blocks}

    def kind(name: str) -> str:
        return (by_name.get(name) or {}).get("type", "").upper()

    def params(name: str) -> dict:
        return (by_name.get(name) or {}).get("params") or {}

    def is_space(name: str) -> bool:
        # A CONVEYOR's cells are a finite space too (PHASE4 T7): a full belt
        # blocks the machine feeding it, exactly as a full buffer does; so is a
        # CONVEYOR_TRANSFER's one place.
        if kind(name) in ("BUFFER", *BELT_TYPES):
            return True
        return kind(name) in ("SEIZE", *PROCESS_TYPES) and _as_int(params(name).get("queue_capacity")) > 0

    def is_seizer(name: str) -> bool:
        if kind(name) == "SEIZE":
            return True
        return (kind(name) in PROCESS_TYPES
                and str(params(name).get("action", "Seize Delay Release")).startswith("Seize"))

    def can_hold(name: str) -> bool:
        if kind(name) in ("BUFFER", "RELEASE"):
            return True
        return (kind(name) in PROCESS_TYPES
                and str(params(name).get("action", "Seize Delay Release")).endswith("Release"))

    spaces = {name for name in by_name if is_space(name)}
    edges: dict[str, list[str]] = {}
    for name in by_name:
        if not (name in spaces or can_hold(name)):
            continue
        reached = stops(name, lambda n: out_links.get(n, []),
                        lambda n: is_space(n) or is_seizer(n),
                        lambda n: kind(n) in TRANSPARENT_TYPES)
        kept = [t for t in reached if name in spaces or t in spaces]
        if kept:
            edges[name] = kept
    # Blocking BEFORE service (PHASE5 O4): the station waits for room in its
    # target space before it starts, so it depends on that space too.
    for name in by_name:
        if not params(name).get("hold_until_space"):
            continue
        target = hold_target(str(params(name).get("space_of") or ""), out_links.get(name, []))
        if target in spaces and target not in edges.get(name, []):
            edges.setdefault(name, []).append(target)
    return edges, spaces


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def analyse(ctx) -> None:
    """Decide which blocks block after service, and remember the loops that
    could deadlock. Called once, after every link is wired.

    A model with no finite space anywhere returns immediately and nothing in
    the engine changes behaviour.
    """
    processes = ctx.process_list
    by_name = {p.name: p for p in processes}
    for proc in processes:
        if not getattr(proc, "hold_until_space", False):
            continue
        name = hold_target(proc.space_of, [getattr(n, "name", "") for n in proc.next_process])
        target = by_name.get(name)
        space = getattr(target, "space", None)
        # A CONVEYOR's cells are a space too, but a belt accepts a part only
        # once it is ON the belt (T7): reserving a cell is not offered.
        kind = getattr(getattr(target, "block", None), "block_type", "")
        if getattr(target, "area_link", False):
            # D1369: a place is reserved in a block, never across an area link
            raise RuntimeError(
                f"Block '{proc.name}': hold_until_space reserves a place in the next block, but "
                f"its link to '{target.shown_name}' is an area link (a delay with a capacity), "
                f"so no place is reserved across it; name the block in space_of or drop the "
                f"link's capacity (E_HOLD_SPACE_TARGET).")
        if space is None or space.capacity <= 0 or kind not in ("BUFFER", "SEIZE", *PROCESS_TYPES):
            raise RuntimeError(
                f"Block '{proc.name}': hold_until_space needs a downstream block with places "
                f"(a BUFFER with capacity > 0, or a SEIZE/PROCESS with queue_capacity > 0); "
                f"'{name or '(no link)'}' has none (E_HOLD_SPACE_TARGET).")
        proc.reserve_space = space
    if not any(getattr(p, "space", None) is not None or _picks(p) for p in processes):
        return

    def is_space(proc) -> bool:
        return getattr(proc, "space", None) is not None

    def successors(proc):
        return getattr(proc, "next_process", None) or []

    def is_stop(proc) -> bool:
        return is_space(proc) or getattr(proc, "space_passthrough", False)

    def is_transparent(proc) -> bool:
        return getattr(getattr(proc, "block", None), "block_type", "") in TRANSPARENT_TYPES

    edges: dict[str, list[str]] = {}
    nodes: dict = {}
    for proc in processes:
        holds_first = getattr(proc, "hold_until_space", False)
        holds = is_space(proc) or getattr(proc, "can_block", False) or holds_first
        if not holds:
            continue
        reached = stops(proc, successors, is_stop, is_transparent)
        if getattr(proc, "can_block", False) and any(is_space(t) or _picks(t) for t in reached):
            proc.block_after_service = True
        elif _is_belt(proc) and any(_takes_from_belt(t) for t in stops(
                proc, lambda p: successors(p)[:1] if p is proc and _is_conveyor(p)
                else successors(p), is_stop, is_transparent)):
            # Belt-to-belt back-pressure (PHASE6 W3 M6, D115): a belt's end is a
            # blocking station whose next space is the downstream belt's cells.
            # Only a BELT downstream turns it on, so a belt feeding a BUFFER or
            # a station keeps its free-running exit and its golden numbers. Its
            # END is link 0 alone: links 1.. are its diverts' (D852), which hand
            # over with the token anyway, and are in `reached` for the loop edges.
            # A CONVEYOR_TRANSFER counts as a belt both ways, and every link of
            # one is an exit.
            proc.block_after_service = True
        if getattr(proc, "reversible", False):
            # D1368: a reversible belt's start is an exit while it runs backward, link
            # `exit_slots[False]`; it hands over with the token when a belt is that way too
            back = proc.exit_slots[False]
            # D1392: a later chain piece's start is a joint, no link (`exit_slots`)
            starts = back is not None and any(_takes_from_belt(t) for t in stops(
                proc, lambda p: successors(p)[back:back + 1] if p is proc else successors(p),
                is_stop, is_transparent))
            proc.exit_blocks = {True: proc.block_after_service, False: starts}
            proc.block_after_service = proc.block_after_service or starts
        if is_space(proc) or proc.block_after_service or holds_first:
            nodes[proc.name] = proc
            kept = [t.name for t in reached if is_space(proc) or is_space(t)]
            if holds_first and proc.reserve_space.owner.name not in kept:
                kept.append(proc.reserve_space.owner.name)
            if kept:
                edges[proc.name] = kept

    for proc in processes:
        proc.space_aware = True        # every forward now checks for a token
    ctx.blocking_nodes = nodes
    ctx.blocking_cycles = [c for c in cycles(edges) if any(n in nodes for n in c)]
    if ctx.blocking_cycles:
        log.info(f"Blocking loops watched at run time: {ctx.blocking_cycles}")


def _is_belt(proc) -> bool:
    """A conveyor piece for back-pressure: a CONVEYOR or a CONVEYOR_TRANSFER, each holding
    its part until the next piece takes it."""
    return getattr(getattr(proc, "block", None), "block_type", "") in BELT_TYPES


def _takes_from_belt(proc) -> bool:
    """A block a belt's end hands over to with the token: a conveyor piece, or an area link
    with places (D1369), which a part enters only when one is free."""
    return _is_belt(proc) or (getattr(proc, "area_link", False)
                              and getattr(proc, "space", None) is not None)


def _is_conveyor(proc) -> bool:
    return getattr(getattr(proc, "block", None), "block_type", "") == "CONVEYOR"


def _picks(proc) -> bool:
    """An arm fleet's TRANSPORT (D169): a stop that takes the part off its
    machine only when the cobot has picked it, so the machine blocks."""
    return (getattr(getattr(proc, "block", None), "block_type", "") == "TRANSPORT"
            and proc.space_passthrough)


# ------------------------------------------------------------------ deadlock
def _stuck(proc, feeder: str = "", successor: str = "") -> bool:
    """*proc* can take nothing from *feeder*, the block before it in the loop, and cannot let
    its own part go to *successor*, the block after it."""
    space = getattr(proc, "space", None)
    # A stopped non-accumulating belt takes nothing on, free cells or not (D115), nor
    # does a belt whose first section before a stop is full (D732), a reversible belt whose
    # start holds a part (R45), or a belt whose free cells *feeder* cannot use: a transfer
    # waiting for its load to reach an eye here while that load stands short of it (R41).
    # R56: those two count only while the part they hinge on waits for *successor*; one
    # waiting for a block outside the loop may leave when that block frees. A part held at a
    # stop is not stuck: its time ends, and a wait for the stop's resource is not a space's.
    if (space is not None and space.has_room()
            and not getattr(proc, "stopped", False) and not getattr(proc, "entry_full", False)
            and not (getattr(proc, "entry_closed", False)
                     and _hands_to(proc, proc.exit_slots[False], successor))
            and not (feeder and hasattr(proc, "handshake_stands")
                     and proc.handshake_stands(feeder) and _hands_to(proc, 0, successor))):
        return False
    if _blocks(proc) and not _held(proc):
        return False
    if successor and _is_conveyor(proc) and not _belt_held_toward(proc, successor):
        return False            # R60: its held part waits for a block outside the loop
    return True


def _hands_to(proc, link: int, successor: str) -> bool:
    """R56, R60: *proc*'s outgoing link *link* hands its part to *successor*, directly or
    through transparent blocks with one link out each; no successor named: any block. Past a
    branching block (a DECIDE, a block with several links out) where the part goes is its own
    choice, so the loop is not counted closed there: a real deadlock through it is still named
    when the run drains (`deadlock_at_end`)."""
    if not successor:
        return True
    nexts = getattr(proc, "next_process", None) or []
    piece, seen = (nexts[link] if link < len(nexts) else None), set()
    while piece is not None and id(piece) not in seen:
        seen.add(id(piece))
        if getattr(piece, "name", None) == successor:
            return True
        if getattr(piece, "space", None) is not None or getattr(piece, "space_passthrough", False) \
                or getattr(getattr(piece, "block", None), "block_type", "") not in TRANSPARENT_TYPES:
            return False
        outs = getattr(piece, "next_process", None) or []
        if len(outs) != 1:
            return False
        piece = outs[0]
    return False


def _belt_held_toward(belt, successor: str) -> bool:
    """R60: a held CONVEYOR waits for *successor*: its end part (or a reversible belt's part at
    the exit it waits at) is handed to it as `_hands_to` reads. A part waiting at a divert, or
    at an exit with no next block, keeps counting as before."""
    if getattr(belt, "_divert_waits", 0) or getattr(belt, "dead_end", False):
        return True
    ride = getattr(belt, "_rev", None)
    if ride is not None:
        start = ride._start_waits
        # D1392: a part at a chain joint waits for the piece it runs into
        return bool(ride.joint_held(successor) or (start and _hands_to(belt, belt.exit_slots[False], successor))
                    or (ride.waits > start and _hands_to(belt, belt.exit_slots[True], successor)))
    return bool(getattr(belt, "_end_held", False)) and _hands_to(belt, 0, successor)


def _held(proc) -> bool:
    """Blocked on the downstream space. A belt's `blocked` also covers its
    `exit_resource` wait, which no loop of spaces decides (D115), so a belt
    answers with its own back-pressure flag."""
    held = getattr(proc, "held_downstream", None)
    return held if held is not None else proc.blocked.level > 0


def _blocks(proc) -> bool:
    """A station that can be blocked: after service, or before it (O4), or a belt whose part
    stands at an exit with no next block (R58)."""
    return (getattr(proc, "block_after_service", False) or getattr(proc, "hold_until_space", False)
            or getattr(proc, "dead_end", False))


def check_deadlock(block) -> None:
    """Raise when the loop *block* just blocked on is closed end to end.

    Checked only at the instant a station blocks, and only for loops the
    static analysis found, so a model with no loop pays nothing. #2019: read once the
    instant's other events are done (an event of lower priority than SimPy's normal ones),
    since a place freed later in the same instant can still let the loop turn.
    """
    ctx = block.ctx
    if not any(block.name in cycle for cycle in ctx.blocking_cycles):
        return
    pending = getattr(ctx, "_deadlock_checks", None)
    if pending is None:
        pending = set()
        setattr(ctx, "_deadlock_checks", pending)
    if block.name in pending:
        return
    pending.add(block.name)
    env = block.env
    event = env.event()
    event._ok, event._value = True, None

    def settled(_event):
        pending.discard(block.name)
        _check_deadlock_now(block)

    event.callbacks.append(settled)
    env.schedule(event, priority=_AFTER_THE_INSTANT)


# SimPy's own events use priorities 0 (urgent) and 1 (normal); 2 sorts after both at one time
_AFTER_THE_INSTANT = 2


def _check_deadlock_now(block) -> None:
    ctx = block.ctx
    for cycle in ctx.blocking_cycles:
        if block.name not in cycle:
            continue
        # each member with the blocks before and after it in the loop (R41, R56)
        if all(_stuck(ctx.blocking_nodes[name], cycle[index - 1],
                      cycle[(index + 1) % len(cycle)])
               for index, name in enumerate(cycle) if name in ctx.blocking_nodes):
            # names quoted: a hidden area link's name holds " -> " itself (D1369)
            chain = " -> ".join(f"'{name}'" if " -> " in name else name
                                for name in (*cycle, cycle[0]))
            raise RuntimeError(
                f"deadlock at t={block.env.now:g}: every station and buffer in the loop "
                f"{chain} is full or blocked, so nothing can move again. Give one of the "
                f"buffers in the loop more places, or let one station leave without blocking.")


def route_holds_at_end(ctx) -> None:
    """R35: a run that ends at its time limit with a load still held by a routing rule names
    each such hold and since when, as a warning: a routing deadlock in an open-ended run is
    not drained, so `deadlock_at_end` never sees it (cheap, not a proof)."""
    for name, holds in sorted(ctx.route_holds.items()):
        if holds:
            text = (f"Block '{name}' still holds {len(holds)} load(s) by its routing rule at the "
                    f"run's end, the first since t={min(holds):g}: every exit was at its limit; "
                    f"if nothing under way there arrives, this is a deadlock.")
            log.warning("%s", text)
            keep_run_warning(ctx, "W_ROUTE_HELD_AT_END", text, name)


def reversed_holds_at_end(ctx) -> None:
    """R45 (D1368): a run that ends at its time limit with a reversible belt running backward
    and a part held at its start names the belt and since when, as a warning: nothing boards
    a belt that runs backward, so if no signal turns it again the two may wait for ever, and
    the queue is not drained, so `deadlock_at_end` never sees it (cheap, not a proof)."""
    for name, belt in sorted(ctx.conveyors.items()):
        ride = getattr(belt, "_rev", None)
        found = ride.hold_at_end() if ride is not None else None
        if found is not None:
            text = (f"Belt '{name}' has run backward since t={found[0]:g} with a part held at its "
                    f"start since t={found[1]:g}, waiting for the next block; nothing boards it "
                    f"meanwhile. If no signal turns it again, this is a deadlock.")
            log.warning("%s", text)
            keep_run_warning(ctx, "W_REVERSED_HELD_AT_END", text, name)


def deadlock_at_end(ctx) -> None:
    """Raise when the event queue drained while a station was still blocked.

    Without this the run simply ends: the KPIs look like a quiet system, which
    is the one wrong answer worse than an error.
    """
    stuck = [name for name, proc in ctx.blocking_nodes.items()
             if _blocks(proc)
             and getattr(proc, "blocked", None) is not None and _held(proc)]
    # A7 (D1359): a load held by a routing rule; only a limit holds (R27), so every exit is full
    routed = sorted(name for name, holds in ctx.route_holds.items() if holds)
    if routed:
        names = sorted(set(stuck) | set(routed))
        why = ("every exit of its routing rule is full for, and no part under way there "
               "arrives. Check the flow after those exits, or raise the rule's limit.")
        raise RuntimeError(
            f"deadlock at t={ctx.env.now:g}: nothing is left to happen but {', '.join(names)} "
            f"{'is' if len(names) == 1 else 'are'} still blocked; {', '.join(routed)} "
            f"{'holds' if len(routed) == 1 else 'hold'} a part {why}")
    if not stuck:
        return
    raise RuntimeError(
        f"deadlock at t={ctx.env.now:g}: nothing is left to happen but {', '.join(sorted(stuck))} "
        f"{'is' if len(stuck) == 1 else 'are'} still blocked, holding a finished part no "
        f"downstream buffer will take. Add buffer places, or check the flow downstream.")
