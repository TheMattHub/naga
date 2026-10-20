import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, DIST_PARAMS
from nagasim.core.distributions import StochasticDistribution

log = logging.getLogger(__name__)

__all__ = ["Delay", "SPEC"]


class Delay(BaseProcess):
    """Pure waiting time (transit, curing, travel, a pause) that holds the entity for a sampled duration without using any machine, operator or other resource.

    Seizes nothing, then forwards it. Use PROCESS instead when the delay is a resource-backed
    service."""

    def __init__(self, env, block, next_process=None, delay=0):
        super().__init__(env, block, next_process)
        self.delay = (
            delay if isinstance(delay, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": delay})
        )

    def self_process(self, entity):

        if self.ctx.debug:
            log.debug("Entity %s arrived at delay at time %s.", entity.id, self.env.now)

        self.active_entity = entity

        duration = self.sample_duration(self.delay, entity)
        entity._ends = self.env.now + duration     # D235: what a state reads
        yield self.env.timeout(duration)

        if self.ctx.debug:
            log.debug("Entity %s left delay at time %s.", entity.id, self.env.now)

        self.remove_active_entity(entity)

        self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "distribution": self.delay.dist_type,
            "dist_params": self.delay.params,
            "time_unit": self.delay.time_unit,
        }

    def set_params_from_dict(self, param_dict):
        dist = param_dict.get("distribution", "Constant")
        params = param_dict.get("dist_params", {"value": 0})
        time_unit = param_dict.get("time_unit", "base")
        self.distribution = dist
        self.distribution_params = params
        self.delay = StochasticDistribution(dist, params, time_unit=time_unit)


SPEC = BlockSpec(
    type_name="DELAY", cls=Delay,
    params=DIST_PARAMS,
    factory=lambda env, block: Delay(env, block, delay=1),
    family="work",
    duration=True, holding=True, service=True,
)
