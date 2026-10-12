import logging

import simpy

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, DIST_PARAMS, ParamSpec
from nagasim.core.distributions import StochasticDistribution

log = logging.getLogger(__name__)

__all__ = ["Signal", "SPEC"]


class Signal(BaseProcess):
    """After an optional delay, succeeds the named signal event (releasing
    every HOLD block currently waiting on it) and immediately replaces it with
    a fresh one -- broadcast, edge-triggered, auto-reset."""

    def __init__(self, env, block, next_process=None, signal_name=None, delay=0):
        super().__init__(env, block, next_process)
        self.signal_name = signal_name          # signal id read by Hold blocks
        self.delay = (
            delay if isinstance(delay, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": delay})
        )
        log.debug("Signal created: %r", self.signal_name)

    def self_process(self, entity):
        if self.ctx.debug:
            log.debug("Entity %s arrives at Signal '%s' at time %s.", entity.id, self.signal_name, self.env.now)

        self.active_entity = entity
        yield self.env.timeout(self.sample_duration(self.delay, entity))

        if self.signal_name not in self.ctx.signals:
            self.ctx.signals[self.signal_name] = self.env.event()

        if not isinstance(self.ctx.signals[self.signal_name], simpy.Event):
            raise TypeError(f"Expected simpy.Event in ctx.signals['{self.signal_name}']")

        if self.ctx.debug:
            log.debug("Signal '%s' activated by entity %s at time %s.", self.signal_name, entity.id, self.env.now)

         # Succeed the signal event to release all listening Hold blocks
        self.ctx.signals[self.signal_name].succeed()

        # Reset the signal so another entity can trigger it again
        self.ctx.signals[self.signal_name] = self.env.event()

        self.remove_active_entity(entity)

        self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "distribution": self.delay.dist_type,
            "dist_params": self.delay.params,
            "time_unit": self.delay.time_unit,
            "signal_name": self.signal_name
        }

    def set_params_from_dict(self, param_dict):
        dist = param_dict.get("distribution", "Constant")
        params = param_dict.get("dist_params", {"value": 0})
        time_unit = param_dict.get("time_unit", "base")
        self.distribution = dist
        self.distribution_params = params
        self.delay = StochasticDistribution(dist, params, time_unit=time_unit)
        self.signal_name = param_dict.get("signal_name", None)


SPEC = BlockSpec(
    type_name="SIGNAL", cls=Signal,
    params=(*DIST_PARAMS, ParamSpec("signal_name", str, "", description="Name of the signal to succeed; matches a HOLD block's signal_name.", quantity="none")),
    factory=lambda env, block: Signal(env, block, signal_name="Default"),
    family="control",
    duration=True, holding=True,
)
