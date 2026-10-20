"""TRANSFER: the one place a discrete entity moves a lump of a continuous
level (PHASE4 D1) -- a truck unloading 20 t, a customer order drawing 5 units,
a delivery arriving into stock.

Everything else about levels is rate-based and lives in `core/levels.py`; this
block is the bridge, so the engine has exactly one discrete-to-continuous
coupling point.

`wait` is the backorder switch, and it is the reason the block exists rather
than being an ASSIGN option:

* `wait = false` -- move what the level can pay for right now and go on. The
  shortfall is recorded as the level's `unmet` (a lost sale, an overflow).
* `wait = true`  -- queue until the level can pay the FULL amount, then take
  it atomically. Demands are served FIFO, and the level engine schedules the
  exact instant the level reaches the amount, so the wait is exact rather than
  polled. An amount the level never reaches waits for the rest of the run, the
  same documented contract as HOLD's "Infinite" mode.
"""
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.levels import EPS, TRANSFER_MODES  # noqa: F401  (re-exported for callers)

log = logging.getLogger(__name__)

__all__ = ["Transfer", "SPEC", "TRANSFER_MODES"]


class Transfer(BaseProcess):
    """Adds to or takes from a continuous tank, stock or inventory level (fuel, material, a pool) as the entity passes, optionally waiting until it is available."""

    def __init__(self, env, block, next_process=None, level_name="", amount="1",
                 mode="remove", wait=False):
        super().__init__(env, block, next_process)
        self.level_name = level_name
        self.amount = amount
        self.mode = mode
        self.wait = bool(wait)

    def _level(self):
        system = self.ctx.level_system
        if system is None or self.level_name not in system.levels:
            raise RuntimeError(f"Block '{self.name}': no level named '{self.level_name}'")
        return system, system.levels[self.level_name]

    def self_process(self, entity):
        system, level = self._level()
        system.sync()                       # the level's value must be current first
        system.mark_breakpoint()            # ... and is the corner before the jump (D75)
        amount = max(float(evaluate_expression(self, self.amount, entity)), 0.0)

        available = (level.level - level.minimum if self.mode == "remove"
                     else (float("inf") if level.capacity is None
                           else level.capacity - level.level))
        if amount <= available + EPS:
            system.move(self.level_name, amount, self.mode)
        elif self.wait:
            yield from self._wait(system, level, entity, amount)
        else:                               # partial move; the rest is unmet/spilled
            system.move(self.level_name, amount, self.mode)
        self.forward(entity, 0)

    def _wait(self, system, level, entity, amount):
        event = self.env.event()
        level.pending.append([amount, self.mode, event])
        entity._pending = event             # D439: a state finds it in its level's queue
        system.trigger()                    # the engine must schedule this demand's instant
        yield event
        system.after_discrete_change()

    def resume(self, entity, amount: float):
        """D439: a state put back an entity waiting here: it joins its level's queue at the
        clock with the amount it waited for, which is not evaluated again."""
        system, level = self._level()
        yield from self._wait(system, level, entity, amount)
        entity._since = None        # its wait here, which a seize further on must not take
        self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {"level": self.level_name, "amount": self.amount, "mode": self.mode,
                "wait": self.wait}

    def set_params_from_dict(self, param_dict):
        self.level_name = param_dict.get("level", "")
        self.amount = param_dict.get("amount", "1")
        self.mode = param_dict.get("mode", "remove")
        self.wait = bool(param_dict.get("wait", False))


SPEC = BlockSpec(
    type_name="TRANSFER", cls=Transfer,
    params=(
        ParamSpec("level", str, "", required=True,
                  description="Name of the level this block adds to or removes from.", quantity="none"),
        ParamSpec("amount", str, "1",
                  description="How much to move: an expression, e.g. 20 or entity.quantity.", quantity="none", kinds=("number", "expression")),
        ParamSpec("mode", str, "remove",
                  description="\"remove\" takes the amount out of the level, \"add\" puts it in.", quantity="none"),
        ParamSpec("wait", bool, False,
                  description="Wait until the level can pay the full amount (the backorder "
                              "pattern); otherwise move what it can and record the rest as "
                              "unmet.", quantity="none"),
    ),
    factory=lambda env, block: Transfer(env, block),
    family="move",
)
