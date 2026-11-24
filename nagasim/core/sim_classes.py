import itertools
import logging
import sys

log = logging.getLogger(__name__)

__all__ = ["Entity", "EntityRecord", "spawn_entity"]


def spawn_entity(ctx, e_type=None, attrs=None, creation_time=None) -> "Entity":
    """A new entity registered with the run: the one path CREATE, an initial
    state snapshot and an agent's `enter` action (PHASE5 AB-2) all take."""
    entity = Entity(next(ctx.entity_ids))
    # The field, not the property's setter (speed review R5): a plain store either way
    entity._creation_time = ctx.now if creation_time is None else creation_time
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



# What a disposed entity's attribute dict holds that an `EntityRecord` either keeps
# in a slot, can give back unchanged (`_DEFAULTS`) or drops as service-in-progress
# state that is stale once the entity has left (`_ends`, `_since`, `_auto_cost`):
# only the rest is copied.
_STALE = ("_ends", "_since")        # never copied; read back as None
_IN_SLOTS = frozenset({"_id", "_e_type", "_creation_time", "_deletion_time", "_throughput_time",
                       "cost", "_holding_cost", "_auto_cost", "in_batch",
                       "initial", "agent"}) | frozenset(_STALE)
_DEFAULTS = {"_resource": None, "finished": True, **dict.fromkeys(_STALE)}
# Speed review E: what `EntityRecord.of` would otherwise work out again for every disposed entity.
# `_OUTSIDE` maps an attribute-key shape (the keys in order) to its keys outside the slots, so the
# shape is an exact test: an entity with one attribute more or less has another shape. `_TYPE_NAMES`
# holds the interned type names. Both are bounded; past the bound the work is done uncached.
_OUTSIDE: dict = {}
_TYPE_NAMES: dict = {}
_CACHE_BOUND = 512


class EntityRecord:
    """What the flat entity log keeps of a disposed entity (D1061), in place of
    the whole `Entity` and its attribute dict (about 550 B less each, at up to
    `entity_log_limit` of them).

    Slots hold what the log row reads. Any other attribute the entity carries
    (a model's own `tag`, a batch's members, a renamed `name`) is copied into
    `_extra` and read back as before, so a script inspecting
    `ctx.all_entities` after a run still sees it; a plain entity has none and
    pays for no dict. Read-only by intent: the entity is finished. The type
    name is interned: `Entity.e_type` makes a new string per entity.
    `_auto_cost` is dropped: Dispose already summed it into the type totals."""

    __slots__ = ("_id", "e_type", "creation_time", "deletion_time",
                 "cost", "_holding_cost", "initial", "agent", "_extra")
    in_batch = None         # only an entity outside any batch is recorded

    @classmethod
    def of(cls, entity) -> "EntityRecord":
        # This runs once per disposed entity with logs on (speed review E), so it reads the
        # attribute dict straight, not through the properties, which only return those same
        # entries (no class derives from Entity), and looks up the keys outside the slots
        # by the dict's key shape instead of a set difference per entity.
        attrs = entity.__dict__
        record = cls()
        record_id = record._id = attrs["_id"]
        e_type = attrs["_e_type"]
        try:
            record.e_type = _TYPE_NAMES[e_type]
        except KeyError:
            record.e_type = sys.intern(e_type)
            if len(_TYPE_NAMES) < _CACHE_BOUND:
                _TYPE_NAMES[e_type] = record.e_type
        record.creation_time = attrs["_creation_time"]
        record.deletion_time = attrs["_deletion_time"]
        record.cost = attrs["cost"]
        record._holding_cost = attrs["_holding_cost"]
        record.initial = attrs["initial"] if "initial" in attrs else False
        record.agent = attrs["agent"] if "agent" in attrs else None
        shape = tuple(attrs)
        try:
            outside = _OUTSIDE[shape]
        except KeyError:
            outside = tuple(key for key in shape if key not in _IN_SLOTS)
            if len(_OUTSIDE) < _CACHE_BOUND:
                _OUTSIDE[shape] = outside
        # Kept: what differs from what `__getattr__` gives back, in the entity's own order.
        extra = None
        for key in outside:
            value = attrs[key]
            if key in _DEFAULTS:
                if value is _DEFAULTS[key]:
                    continue
            elif key == "name":
                if value == f"Entity {record_id}":
                    continue
            elif key == "batch_elems":
                if type(value) is list and not value:
                    continue
            if extra is None:
                extra = {}
            extra[key] = value
        record._extra = extra
        return record

    def __getattr__(self, name):
        # Only reached for a name no slot or property answers.
        if name == "_extra":
            raise AttributeError(name)
        extra = self._extra
        if extra is not None and name in extra:
            return extra[name]
        if name == "name":
            return f"Entity {self._id}"
        if name == "batch_elems":
            return []
        if name == "resource":
            return self._resource
        if name in ("_e_type", "_creation_time", "_deletion_time", "_throughput_time"):
            return getattr(self, name[1:])      # what the entity's property reads
        if name in _DEFAULTS:
            return _DEFAULTS[name]
        raise AttributeError(f"{type(self).__name__} has no attribute {name!r}")

    @property
    def id(self):
        return self._id

    @property
    def throughput_time(self):
        """As Dispose stamped it on the entity, recomputed instead of kept."""
        if self.deletion_time is None or self.creation_time is None:
            return None
        return self.deletion_time - self.creation_time

    def __repr__(self):
        return self.name
