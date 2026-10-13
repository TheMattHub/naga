"""BUFFER: the finite space between two stations (PHASE4 T5, ARCHITECTURE D78).

`capacity` places. A part takes one on the way in and keeps it until the next
station accepts it, so an upstream machine that finds the buffer full holds its
part -- and its resource -- until a place frees: blocking after service.

`capacity` 0 is legal and is the classic zero-buffer line: no place at all, so
the part never leaves the machine before the next station starts it. There is
no "unlimited" buffer: that is what NOT drawing one means.

An optional `level` mirrors the content into a declared level, which is what a
fill indicator on an asset reads (D75) -- the only reason a buffer ever needs
one.
"""
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocking import Space, arm

log = logging.getLogger(__name__)

__all__ = ["Buffer", "SPEC"]


class Buffer(BaseProcess):
    """A finite number of places between two stations; when they are all taken
    the upstream machine stays blocked, holding its finished part."""

    # The token rides through: a buffer accepts the entity when it gives it a
    # place, not when it arrives (a full buffer has not accepted anything).
    space_passthrough = True

    def __init__(self, env, block, next_process=None, capacity=1):
        super().__init__(env, block, next_process)
        self.capacity = max(int(capacity), 0)
        self.level_name = ""
        self.space = Space(env, self.capacity, self)

    def self_process(self, entity):
        if self.space.capacity <= 0:
            # No place at all: the part rides on still held by its machine,
            # and the next station's seize is what accepts it.
            self.forward(entity, 0)
            return
        slot = yield from self.space.take(entity)
        self._mirror(1)
        token = arm(self, entity)
        self.forward(entity, 0)
        if not token.triggered:
            yield token
        self.space.give_back(slot)
        self._mirror(-1)

    def hold(self, entity) -> None:
        """A state puts *entity* back in the place it had here while it waits further on
        (D381): the place is taken and the token armed at once, and the place is given back
        when the next station accepts the part. The mirrored level already counts it."""
        self.env.process(self._until_accepted(arm(self, entity), self.space.claim()))

    def _until_accepted(self, token, slot):
        yield token
        self.space.give_back(slot)
        self._mirror(-1)

    def _mirror(self, delta: int) -> None:
        """Keep `level` equal to the content, for a fill indicator (D75)."""
        if not self.level_name:
            return
        system = self.ctx.level_system
        level = system.levels.get(self.level_name) if system is not None else None
        if level is None:
            raise RuntimeError(f"Buffer block '{self.name}': no level named '{self.level_name}'")
        if level.conveyor is not None:      # DEVLOG #240: the next grid step would overwrite it
            raise RuntimeError(f"Buffer block '{self.name}': '{self.level_name}' is a conveyor; "
                               f"a BUFFER cannot mirror into it")
        system.sync()
        system.mark_breakpoint()
        level.level += delta
        if delta > 0:
            level.inflow_total += delta
        else:
            level.outflow_total += -delta
        system.after_discrete_change()

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {"capacity": self.capacity, "level": self.level_name}

    def set_params_from_dict(self, param_dict):
        self.capacity = max(int(param_dict.get("capacity", 1) or 0), 0)
        self.level_name = str(param_dict.get("level", "") or "").strip()
        self.space = Space(self.env, self.capacity, self)


SPEC = BlockSpec(
    type_name="BUFFER", cls=Buffer,
    params=(
        ParamSpec("capacity", int, 1, required=True,
                  description="Places in the buffer, 0 or more. A part takes one on the way in "
                              "and holds it until the next station accepts it, so an upstream "
                              "machine blocks while the buffer is full. 0 means no place at all "
                              "(blocking after service with no buffer); for an unlimited queue, "
                              "do not draw a buffer.", quantity="count", min=0),
        ParamSpec("level", str, "",
                  description="Optional level whose value mirrors the buffer's content, so an "
                              "asset can draw it as a fill indicator. Empty = no level.", quantity="none"),
    ),
    factory=lambda env, block: Buffer(env, block),
    family="move", holding=True,
)
