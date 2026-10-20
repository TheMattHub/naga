import logging

from nagasim.core import blocking
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import (BlockSpec, DIST_PARAMS, HOLD_SPACE_PARAMS, QUEUE_PARAMS,
                                     WORKER_PARAMS, ParamSpec)
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.seizing import (QueueBehaviour, acquire, acquire_worker, balks, leave,
                                  patience_of)
from nagasim.core.stats import TimeWeighted

log = logging.getLogger(__name__)

__all__ = ["Seize", "SPEC"]


class Seize(QueueBehaviour, BaseProcess):
    """Waits in a queue for a machine, operator, vehicle or other resource, takes it and keeps it until a later RELEASE, so one resource can span several steps.

    Request `quantity` units of `resource_name` (a plain resource or a
    resource set), honouring priority and the resource's queue discipline,
    then hold the block's own delay before forwarding. Pairs with RELEASE for
    a hold split across two blocks; PROCESS fuses the common Seize-Delay-
    Release case into one block.

    `balk_if` and `renege_after` let an entity leave without ever
    being served, by the block's second outgoing link. Preemption is PROCESS
    only: the service a SEIZE starts is finished somewhere else, so there is
    nothing here that could safely be interrupted."""

    # A station accepts the part when it SEIZES (ARCHITECTURE D78).
    space_passthrough = True

    def __init__(self, env, block, resource=None, next_process=None, quantity=1, delay=0, priority=0):
        super().__init__(env, block, next_process)
        self.resource = resource
        self.quantity = quantity
        self.delay = (
            delay if isinstance(delay, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": delay})
        )
        self.priority = priority
        self.init_queue_behaviour()
        self.queue_capacity = 0       # PHASE4 T5: 0 = unlimited, today's default
        self.blocked = TimeWeighted(env.now, level=0.0)   # PHASE5 O4, hold_until_space
        # A worker held until a RELEASE names its fleet (PHASE6 W3-M3, D114).
        self.worker_fleet = ""
        self.worker_skill = ""
        self.node = ""

        # resource_name is a required param (DEVLOG #13): the real resource is
        # assigned via set_params_from_dict right after construction. resource=None
        # here just means "not assigned yet" -- self_process checks it at run time.
        if self.resource is not None:
            if not hasattr(self.resource, "name") or not isinstance(self.resource.name, str):
                raise ValueError(f"Resource {self.resource} must have a unique string .name as attribute.")

            # Assign the resource to the block for animation purposes (if animation is on)
            if not hasattr(self.block, "resource"):
                self.block.resource = self.resource
            else:
                if self.block.resource is not self.resource:
                    raise ValueError(f"Block '{self.block.name}' already has a resource assigned.")

        # Automatically register this Seize block in the context
        env.ctx.seize_blocks.append(self)

    def self_process(self, entity):
        if self.resource is None and not self.worker_fleet:
            raise RuntimeError(f"Seize block '{self.name}' has no resource; set resource_name in its params.")
        arrival_time = self.env.now
        if self.ctx.debug:
            log.debug("Entity %s arrived to Seize '%s' at time %s.", entity.id, self.name, arrival_time)

        # Requesting, the queue discipline and the entity's bookkeeping all live
        # in core/seizing.py, shared with Release and Process (ROADMAP 1.2).
        if entity._since is None and balks(self, entity):
            leave(self, entity, "balk")
            return
        slot = None
        if self.space is not None:          # a finite input queue (PHASE4 T5)
            slot = yield from self.space.take(entity)
        if self.resource is not None and not (yield from acquire(
                self, entity, self.resource.name, self.quantity, self.priority,
                patience=patience_of(self, entity))):
            if slot is not None:
                self.space.give_back(slot)
            self.renege_wait.add(self.env.now - arrival_time)
            leave(self, entity, "renege")
            return
        if slot is not None:
            self.space.give_back(slot)
        if self.hold_until_space:           # blocking BEFORE service (PHASE5 O4)
            yield from blocking.reserve(self, entity)
        if self.worker_fleet:               # D114: the machine first, then the worker
            yield from acquire_worker(self, entity, self.priority)

        self.active_entity = entity
        duration = self.sample_duration(self.delay, entity)
        entity._ends = self.env.now + duration     # D235: what a state reads
        yield self.env.timeout(duration)

        if self.ctx.debug:
            log.debug("Entity %s exits process at time %s.", entity.id, self.env.now)

        self.remove_active_entity(entity)
        # The resource is released in the Release block -- no entity.req_params[0] = None here

        self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "distribution": self.delay.dist_type,
            "dist_params": self.delay.params,
            "time_unit": self.delay.time_unit,
            "priority": self.priority,
            "quantity": self.quantity,
            "resource_name": self.resource.name if self.resource else None,
            "queue_capacity": self.queue_capacity,
            "hold_until_space": self.hold_until_space,
            "space_of": self.space_of,
            "worker_fleet": self.worker_fleet,
            "worker_skill": self.worker_skill,
            "node": self.node,
            **self.queue_params_dict(),
        }

    def set_params_from_dict(self, param_dict):
        dist = param_dict.get("distribution", "Constant")
        params = param_dict.get("dist_params", {"value": 0})
        time_unit = param_dict.get("time_unit", "base")
        self.distribution = dist
        self.distribution_params = params
        self.delay = StochasticDistribution(dist, params, time_unit=time_unit)

        self.priority = param_dict.get("priority", 0)
        self.quantity = param_dict.get("quantity", 1)
        self.set_queue_params(param_dict)
        self.queue_capacity = max(int(param_dict.get("queue_capacity", 0) or 0), 0)
        self.space = (blocking.Space(self.env, self.queue_capacity, self)
                      if self.queue_capacity > 0 else None)
        self.hold_until_space = bool(param_dict.get("hold_until_space", False))
        self.space_of = str(param_dict.get("space_of", "") or "")
        self.worker_fleet = str(param_dict.get("worker_fleet", "") or "").strip()
        self.worker_skill = str(param_dict.get("worker_skill", "") or "").strip()
        self.node = str(param_dict.get("node", "") or "").strip()

        # A worker-only SEIZE leaves resource_name empty (D114).
        if "resource_name" in param_dict and not (self.worker_fleet
                                                  and not param_dict["resource_name"]):
            name = param_dict["resource_name"]
            if not self.ctx.resource_manager.exists(name):
                # The layout's resources section is registered first, so getting
                # here means the model never declared it (DEVLOG #17).
                log.warning(f"Resource '{name}' is not declared in the model; registering it with capacity 1.")
                self.ctx.resource_manager.register(name=name, capacity=1)
            self.resource = self.ctx.resource_manager.resolve(name)


SPEC = BlockSpec(
    type_name="SEIZE", cls=Seize,
    params=(
        *DIST_PARAMS,
        ParamSpec("priority", int, 0, description="Lower draws first when the resource's queue is contested. One number for the block (an entity attribute is not read): to rank entity types, route each to its own block on the shared resource.", quantity="none"),
        ParamSpec("quantity", int, 1, description="Number of resource units requested.", quantity="count", min=1),
        ParamSpec("resource_name", str, "", required=True, description="Name of the resource or resource set to seize.", quantity="none"),
        ParamSpec("queue_capacity", int, 0,
                  description="Places in this block's own input queue. 0 (the default) "
                              "is an unlimited queue; with a positive value a part takes a place "
                              "on arrival and frees it when it seizes, so an upstream machine "
                              "with nowhere to put its finished part stays blocked.", quantity="count", min=0),
        *HOLD_SPACE_PARAMS,
        *WORKER_PARAMS,
        *QUEUE_PARAMS,
    ),
    # No placeholder resource (DEVLOG #13): resource_name is required, so
    # set_params_from_dict always assigns the real resource right after
    # construction. A freshly dropped block has no resource_name yet; the
    # Problems dock flags that until the user fills it in (schema.validate_layout_structured).
    factory=lambda env, block: Seize(env, block, resource=None, delay=1),
    family="resource",
    duration=True, holding=True, service=True, queueing=True, uses_resource=True,
)
