import logging

from nagasim.core import blocking
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import (BlockSpec, DIST_PARAMS, WORKER_BLOCKED_MODES,
                                     WORKER_WHEN_BLOCKED, ParamSpec)
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.seizing import release as release_held
from nagasim.core.stats import TimeWeighted

log = logging.getLogger(__name__)

__all__ = ["Release", "SPEC"]


class Release(BaseProcess):
    """Frees a machine, operator or other resource that an earlier SEIZE gave the entity, after an optional delay.

    Release everything the entity holds under `resource_name` (a plain
    resource or a set member), after an optional delay. Pairs with SEIZE for a
    hold split across two blocks; raises if the entity never seized that name
    (fail-loud by design)."""

    def __init__(self, env, block, resource=None, next_process=None, delay=0):
        super().__init__(env, block, next_process)
        self.resource = resource  # resource_name is required; set via set_params_from_dict (DEVLOG #13)
        self.delay = (
            delay if isinstance(delay, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": delay})
        )
        self.blocked = TimeWeighted(env.now, level=0.0)   # holding a finished part (D78)
        self.worker_when_blocked = WORKER_BLOCKED_MODES[0]

    @property
    def can_block(self) -> bool:
        """The end of a SEIZE ... RELEASE machine holds what it releases until
        the part is accepted downstream, as a PROCESS does (D78, DEVLOG #314).
        The one exception is a WORKER that "leaves" (D171, the default, and a
        PROCESS's rule since D114): it is free when the work ends, so its
        RELEASE never blocks; "stays" keeps it with the part."""
        fleet = self.ctx.fleets.get(getattr(self.resource, "name", ""))
        return not (getattr(fleet, "kind", "") == "worker"
                    and self.worker_when_blocked != "stays")

    def self_process(self, entity):
        if self.resource is None:
            raise RuntimeError(f"Release block '{self.name}' has no resource; set resource_name in its params.")
        name = self.resource.name

        if self.ctx.debug:
            log.debug("Entity %s arrived at release '%s' at time %s.", entity.id, name, self.env.now)

        self.active_entity = entity
        yield self.env.timeout(self.sample_duration(self.delay, entity))

        self.remove_active_entity(entity)

        if self.block_after_service:
            # ARCHITECTURE D78: the resource is not free until the part is taken.
            yield from blocking.hold_until_accepted(self, entity, 0)
            release_held(self, entity, name)
            return

        release_held(self, entity, name)   # shared with Seize/Process (ROADMAP 1.2)
        self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "distribution": self.delay.dist_type,
            "dist_params": self.delay.params,
            "time_unit": self.delay.time_unit,
            "resource_name": self.resource.name if self.resource else None,
            "worker_when_blocked": self.worker_when_blocked,
        }

    def set_params_from_dict(self, param_dict):
        dist = param_dict.get("distribution", "Constant")
        params = param_dict.get("dist_params", {"value": 0})
        time_unit = param_dict.get("time_unit", "base")
        self.distribution = dist
        self.distribution_params = params
        self.delay = StochasticDistribution(dist, params, time_unit=time_unit)
        self.worker_when_blocked = (param_dict.get("worker_when_blocked")
                                    or WORKER_BLOCKED_MODES[0])

        if "resource_name" in param_dict:
            name = param_dict["resource_name"]
            if not self.ctx.resource_manager.exists(name):
                # The layout's resources section is registered first, so getting
                # here means the model never declared it (DEVLOG #17).
                log.warning(f"Resource '{name}' is not declared in the model; registering it with capacity 1.")
                self.ctx.resource_manager.register(name=name, capacity=1)
            self.resource = self.ctx.resource_manager.resolve(name)


SPEC = BlockSpec(
    type_name="RELEASE", cls=Release,
    params=(*DIST_PARAMS, ParamSpec("resource_name", str, "", required=True,
                                    description="Name of the resource or resource set to release.", quantity="none"),
            WORKER_WHEN_BLOCKED),
    factory=lambda env, block: Release(env, block, resource=None),
    family="resource", shape="release",
    duration=True, uses_resource=True,
)
