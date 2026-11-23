"""Area links: a link with a delay and a capacity (conveyor system A10, D1369; one field, D1391).

A link may carry `delay` (any duration spec, as every link may) and `capacity` (a whole
number, default 0). A link with a delay and a capacity above 0 is an area link; a delay alone
is today's delayed link and anything else today's instant link, so `GraphModel` builds nothing
for them and their runs are exactly what they were. A capacity with no delay is not read (a
part that spends no time in a link frees its place in the same instant): validation warns.

An area link is built as one hidden `AreaLink` process spliced into the source's
`next_process` at the link's own slot, so exit order and port lookup (which read the layout's
links) are untouched. A part enters when a place is free -- that takes it off the upstream
block, as a BUFFER place does -- spends its delay inside, drawn as it enters with the SOURCE
block's `sample_duration` (its rng, units, deterministic mode and negative cut, as a plain
link's delay), then is handed to the target and keeps its place until the target accepts it
(the D78 token). A full link is therefore a finite space: the block feeding it holds its
part, a belt by its end (D115), and the deadlock checks see it under the name
"<source> -> <target> link".

The hidden process is never a block of the model: it is not in `GraphModel.blocks`, the
source reports moves and link counts against the real target (`shown_name`), and the target
is reached through `real_target` wherever a block reads its links by name.
"""
from __future__ import annotations

from nagasim.core import blocking
from nagasim.core.base_process import BaseProcess, Block
from nagasim.core.stats import Tally, TimeWeighted

__all__ = ["AREA_KEYS", "AreaLink", "is_area_link", "capacity_of", "real_target",
           "hidden_name", "area_pairs"]

# The link keys an area link adds to a link, as the file writes them (its time is the delay).
AREA_KEYS = ("capacity",)
# The type name the hidden process carries; no block of that type exists in the registry.
KIND = "AREA_LINK"


def capacity_of(link) -> int:
    """The link's `capacity`, 0 when absent or not a whole number."""
    value = link.get("capacity", 0) if isinstance(link, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def is_area_link(link) -> bool:
    """A link the run builds a hidden process for: a delay and a capacity above 0."""
    return capacity_of(link) > 0 and bool(link.get("delay"))


def area_pairs(links) -> dict:
    """(source, target) -> capacity of every area link in *links*: what validation's walks stop
    at, as the run's walks stop at the hidden process."""
    return {(link.get("source"), link.get("target")): capacity_of(link)
            for link in links or [] if is_area_link(link)}


def hidden_name(source: str, target: str, taken: set) -> str:
    """"<source> -> <target> link", numbered from 2 for a second link of that pair (D77) or
    a name a block already has, so it never collides with one."""
    return f"{source} -> {target} link" + _number_suffix(source, target, taken)


def _number_suffix(source: str, target: str, taken: set) -> str:
    """"" for the first link of the pair, " <n>" for the n-th (`hidden_name`)."""
    base = f"{source} -> {target} link"
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base} {n}"
    return name[len(base):]


def real_target(proc):
    """The block *proc* hands to: the target behind a hidden area link, else *proc*."""
    return proc.next_process[0] if getattr(proc, "area_link", False) else proc


class AreaLink(BaseProcess):
    """The hidden process of one area link: `capacity` places, each held from the part's entry
    for its own `delay` draw (made by *source*) and then until the target accepts it."""

    area_link = True

    def __init__(self, env, name: str, target, source, delay, capacity: int, label: str = ""):
        super().__init__(env, Block(type=KIND, name=name))
        self.next_process = [target]
        self.source = source
        self.delay = delay
        self.capacity = max(int(capacity), 1)
        self.blocked = TimeWeighted(env.now)
        self._waiting = 0               # parts past their delay, not yet accepted
        self.space = blocking.Space(env, self.capacity, self)
        self.space_passthrough = True       # the token rides on until a place is free
        self.can_block = True
        # A12 (D1376): the `link_flow` row: "<source> -> <target>", the parts in the link (from
        # entering to the target taking them), each part's time there and its delay draws
        self.label = label or name
        self.inside = TimeWeighted(env.now)
        self.times = Tally()
        self.delays = Tally()
        env.ctx.area_links.append(self)

    def reset_stats(self, t: float) -> None:
        """Warm-up hook (`sim_runner.reset_statistics`); a limited link's `space` and `blocked`
        are reset as a blocking node's."""
        self.inside.reset(t)
        self.times.reset()
        self.delays.reset()
        self.space.reset_stats(t)

    def _draw(self, entity) -> float:
        """This part's delay, drawn by the source block as a plain link's delay is."""
        duration = self.source.sample_duration(self.delay, entity)
        self.delays.add(duration)
        return duration

    def _left(self, entity, entered: float) -> None:
        now = self.env.now
        self.inside.add(-1, now)
        self.times.add(now - entered)

    @classmethod
    def build(cls, env, link: dict, target, taken: set, source_block, delay):
        """The hidden process of *link* (an area link) from *source_block* to the built
        *target*; *delay* is the link's built distribution."""
        source, dest = link["source"], link["target"]
        # the row label drops the " link" the hidden name carries, keeping a parallel link's number
        suffix = _number_suffix(source, dest, taken)
        name = f"{source} -> {dest} link{suffix}"
        return cls(env, name, target, source_block, delay, capacity_of(link),
                   f"{source} -> {dest}{suffix}")

    @property
    def shown_name(self):
        """The name moves and link counts report: the real target's."""
        return getattr(self.next_process[0], "name", None)

    @property
    def held_downstream(self) -> bool:
        """Every part in the link waits for the target (none still in transit): the link
        cannot free a place by itself, which is what the deadlock check asks."""
        return 0 < self._waiting >= self.space.content.level

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def self_process(self, entity):
        # here, not at module level: the blocks package imports this module (real_target)
        from nagasim.core.blocks._belt_points import zone_hold_delay
        place = yield from self.space.take(entity, accepts=False)
        delay = zone_hold_delay(entity, self.env.now)
        if delay > 0:
            # R23: a zoned belt's held lead part waits its zone-1 delay before any hand-off
            yield self.env.timeout(delay)
        blocking.accept(entity)             # in the link: the block feeding it is free
        yield from blocking.placed(entity)
        entered = self.env.now
        self.inside.add(1, entered)
        yield self.env.timeout(self._draw(entity))
        token = blocking.arm(self, entity)
        self._hand_on(entity)
        if not token.triggered:
            self._waiting += 1
            self.blocked.add(1, self.env.now)
            blocking.check_deadlock(self)
            yield token
            self._waiting -= 1
            self.blocked.add(-1, self.env.now)
        self._left(entity, entered)
        self.space.give_back(place)

    def _hand_on(self, entity) -> None:
        """Into the target, with no move of its own: the source already reported the move to
        the target when the part entered the link."""
        target = self.next_process[0]
        if self.space_aware:
            blocking.on_forward(entity, target)
        target.self_run(entity)

    def get_params_dict(self):
        return {}
