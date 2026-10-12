import itertools
import logging

log = logging.getLogger(__name__)

__all__ = ["Entity", "spawn_entity"]


def spawn_entity(ctx, e_type=None, attrs=None, creation_time=None) -> "Entity":
    """A new entity registered with the run: the one path CREATE, an initial
    state snapshot and an agent's `enter` action (PHASE5 AB-2) all take."""
    entity = Entity(next(ctx.entity_ids))
    entity.creation_time = ctx.now if creation_time is None else creation_time
    if e_type:
        entity.e_type = e_type
    for key, value in (attrs or {}).items():
        setattr(entity, key, value)
    ctx.all_entities.append(entity)
    ctx.live_entities.append(entity)
    return entity


class Entity:
    # D195 (DEVLOG #330): an id belongs to the run. The engine always passes one drawn
    # from its own `ctx.entity_ids`. A bare `Entity()` is a test or a script building
    # one by hand: it draws from the newest context's sequence (`build_context` points
    # this at it), so it never collides with the entities that context's blocks create.
    detached_ids = itertools.count(1)

    def __init__(self, entity_id: int | None = None):
        self._id = next(Entity.detached_ids) if entity_id is None else entity_id
        self.name = f"Entity {self._id}"
        self._resource = None
        self._e_type = "DEFAULT"
        self.batch_elems = []
        self.in_batch = None  # id of the batch entity currently representing this one
        self._creation_time = None
        self._deletion_time = None
        self._throughput_time = None # This could be saved in a table instead of as an entity attribute
        self._ends = None    # D235: when the service in progress ends; stale once it has
        self._since = None   # a warm-started entity's wait began then, not now; cleared at its seize
        # Costing (ROADMAP 2.2): `cost` is a plain public attribute, readable
        # and assignable from a model expression like any other (Assign's
        # `entity.cost = entity.cost + 12.5` just works, since Entity has no
        # __slots__). `_auto_cost`/`_holding_cost` are private shadow totals
        # the engine keeps in lockstep with every automatic addition (resource
        # busy/usage share, holding cost) so a report can recover the
        # model-assigned-only portion as `cost - _auto_cost` without a model
        # ever being able to read or overwrite the shadow itself (leading "_"
        # is off-limits to the expression grammar, core/expressions.py).
        self.cost = 0.0
        self._auto_cost = 0.0
        self._holding_cost = 0.0

    @property
    def id(self):
        return self._id
    
    @property
    def e_type(self):
        return self._e_type
    
    @e_type.setter
    def e_type(self, value):
        if not isinstance(value, str):
            raise ValueError("Entity type must be a string")

        self._e_type = value.upper()


    @property
    def resource(self):
        return self._resource
    
    @resource.setter
    def resource(self, value):
        self._resource = value

    def __repr__(self):
        return self.name
    
    @property
    def creation_time(self):
        return self._creation_time
    
    @creation_time.setter
    def creation_time(self,value):
        self._creation_time = value

    @property
    def deletion_time(self):
        return self._deletion_time
    
    @deletion_time.setter
    def deletion_time(self,value):
        self._deletion_time = value

    @property
    def throughput_time(self):
        return self._throughput_time

    @throughput_time.setter
    def throughput_time(self,value):
        self._throughput_time = value

    def add_cost(self, amount, *, holding=False):
        """Accrue an AUTOMATIC cost (resource share, usage, or WIP holding),
        keeping the private shadow totals in lockstep so a report can recover
        the model-assigned-only portion of `cost` later (ROADMAP 2.2)."""
        self.cost += amount
        self._auto_cost += amount
        if holding:
            self._holding_cost += amount

