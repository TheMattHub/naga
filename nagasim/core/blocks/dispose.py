import logging

from nagasim.core import tending
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, DIST_PARAMS, ParamSpec
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.movers import free_held
from nagasim.core.stats import account_and_retain

log = logging.getLogger(__name__)

__all__ = ["Dispose", "SPEC"]


class Dispose(BaseProcess):
    """End of the flow: parts, orders or customers leave the system here, and their throughput time and holding cost are recorded.

    Waits an optional deletion delay, stamps deletion_time and
    throughput_time, accrues the entity type's WIP holding cost,
    and removes the entity from live_entities. Every model needs at least one
    DISPOSE block, and every flow that does not lead back to a CREATE must end
    in one (schema.validate_graph_structured)."""

    def __init__(self, env, block, next_process=None, deletion_delay=0, remove_agent=False):
        super().__init__(env, block, next_process)
        self.deletion_delay = (
            deletion_delay if isinstance(deletion_delay, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": deletion_delay})
        )
        # H2, D130: remove the entity's agent (if it still has a live one) at
        # disposal. Off by default (unchanged behaviour).
        self.remove_agent = remove_agent

        self.disposed_entities = 0
        self._holding_rates = {}     # entity type -> its holding cost rate, filled as types arrive

    def self_process(self, entity):
            self._arrive(entity)
            yield self.env.timeout(self.sample_duration(self.deletion_delay, entity))
            self._finish(entity)

    def _arrive(self, entity):
            entity.finished = True
            if self.ctx.debug:
                log.debug("Entity %s has been disposed at time %s.", entity.id, self.env.now)
            tending.cobot_of(self, entity)     # D169: a cobot that stayed with it is freed
            free_held(self, entity)            # D837: and a vehicle a TRANSPORT kept

    def _finish(self, entity):
            entity.deletion_time = self.env.now
            try:
                entity.throughput_time = entity.deletion_time - entity.creation_time
            except:
                    log.warning(f"Error in throughput_time of entity {entity}")

            self._accrue_holding_cost(entity)
            if self.remove_agent:
                self._remove_agent_of(entity)
            self.ctx.live_entities.remove(entity)
            self.disposed_entities += 1
            self.emit("entity_disposed", entity=entity)
            account_and_retain(self.ctx, entity)   # ROADMAP 2.5, DEVLOG #62

            if self.ctx.debug:
                log.debug("Entity %s has been deleted at time %s.", entity.id, self.env.now)

    def self_run(self, entity):
        delay = self.deletion_delay
        if delay.dist_type == "Constant" and delay.params.get("value") == 0:
            # A plain zero delay waits for nothing and draws nothing, so the
            # entity is finished here, in the caller, the way ASSIGN, DECIDE,
            # RECORD and SEPARATE run. As a process it cost three SimPy events
            # per entity to be disposed later in the same instant. Tested per
            # entity: the delay may be replaced after construction.
            self._arrive(entity)
            self._finish(entity)
            return None
        return self.env.process(self.self_process(entity))

    def _remove_agent_of(self, entity) -> None:
        """remove_agent (D130): a no-op without a live agent; `from_block` as in
        CREATE's `_spawn_agent` (a death outside AgentSystem._run)."""
        system = self.ctx.agent_system
        ref = getattr(entity, "agent", None)
        if system is not None and ref is not None and system.alive(ref):
            system.from_block(system.remove_agent, *system.deref(ref))

    def _accrue_holding_cost(self, entity):
        """Entity type's WIP holding cost, per unit model time in system
        (ROADMAP 2.2), clipped to the post-warm-up period so a job already in
        the system when warm-up ends is billed only from that point on."""
        # Looked up once per entity type: the types and their rates are loaded
        # with the model and nothing changes them during a run.
        e_type = entity.e_type
        rate = self._holding_rates.get(e_type)
        if rate is None:
            rate = self._holding_rates[e_type] = (
                self.ctx.entity_manager.get_type(e_type).get("holding_cost", 0.0))
        if not rate or entity.creation_time is None:
            return
        start = max(entity.creation_time, self.ctx.warmup)
        end = entity.deletion_time
        if end > start:
            entity.add_cost(rate * (end - start), holding=True)

    def get_params_dict(self):
        return {
            "distribution": self.deletion_delay.dist_type,
            "dist_params": self.deletion_delay.params,
            "time_unit": self.deletion_delay.time_unit,
            "remove_agent": self.remove_agent,
        }

    def set_params_from_dict(self, param_dict):
        dist = param_dict.get("distribution", "Constant")
        params = param_dict.get("dist_params", {"value": 0})
        time_unit = param_dict.get("time_unit", "base")
        self.distribution = dist
        self.distribution_params = params
        self.deletion_delay = StochasticDistribution(dist, params, time_unit=time_unit)
        self.remove_agent = param_dict.get("remove_agent", False)


SPEC = BlockSpec(
    type_name="DISPOSE", cls=Dispose,
    params=DIST_PARAMS + (
        ParamSpec("remove_agent", bool, False,
                  description="When true, also remove the entity's agent "
                              "(AgentSystem.remove_agent) at disposal, if it still has a live "
                              "one. An entity with none, or whose agent already died, is a "
                              "no-op.", quantity="none"),
    ),
    factory=lambda env, block: Dispose(env, block),
    family="sink", shape="cap",
    duration=True,
)
