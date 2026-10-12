from nagasim.core.quantities import SECTION_FIELDS, param, to_base
from nagasim.core.timeunits import ModelUnits
import logging

log = logging.getLogger(__name__)

__all__ = ["EntityManager"]


class EntityManager:
    # A model declares its own entity types in its "entities" section; this is
    # only the fallback used when an e_type has no matching entry (get_type()).
    DEFAULT_ENTITIES = {
        "DEFAULT": {"shape": "circle", "color": "BLACK"},
    }

    def __init__(self, load_defaults=True):
        self.entities = {}  # name -> {shape, color}
        
        if load_defaults:
            self._preload_default_entities()
        
    def reset(self):
        """Clear runtime state."""
        self.entities.clear()
        self._preload_default_entities()

    def register(self, name, shape="circle", color="BLACK", holding_cost=0.0):
        #normalize name, shape, and color for safety
        name = name.upper()
        shape = shape.lower()
        color = color.upper()
        # holding_cost (ROADMAP 2.2): money per model base time unit a live
        # entity of this type costs to hold (WIP cost), default 0.
        self.entities[name] = {"shape": shape, "color": color, "holding_cost": float(holding_cost)}

    def all(self):
        return self.entities

    def exists(self, name):
        return name in self.entities

    def remove(self, name):
        if name in self.entities:
            del self.entities[name]

    def load_from_json(self, entity_list, units=None):
        fields = SECTION_FIELDS["entities"]
        for ent in entity_list:
            cost = to_base(param(fields, "holding_cost"), ent, units or ModelUnits(), fields)
            self.register(ent["name"], ent.get("shape", "circle"), ent.get("color", "BLACK"),
                          cost if cost is not None else 0.0)

    def export_to_json(self):
        return [{"name": name.upper(), **props} for name, props in self.entities.items()]

    def _preload_default_entities(self):
        for name, info in EntityManager.DEFAULT_ENTITIES.items():
            if name not in self.entities:
                self.entities[name] = info
                log.info(f"Preloaded default entity: {name}")

    def get_type(self, name):
        """Return shape and color for a given entity name, or default if not found."""
        return self.entities.get(name, self.entities["DEFAULT"])