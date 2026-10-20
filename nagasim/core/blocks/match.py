import logging
from collections import defaultdict, deque

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import make_batch_entity

log = logging.getLogger(__name__)

__all__ = ["Match", "SPEC"]


class Match(BaseProcess):
    """Waits until one entity of each of several kinds is present (a part and its kit, an order and its customer), then releases them together.

    Match `number_to_match` *distinct* entity types; with `match_on` set,
    only entities whose value of that attribute is equal (Arena's attribute
    match).

    The arriving entity is always stored first, so it can never be dropped
   ; the match is then taken atomically from the stores, oldest
    waiting type first, FIFO within a type (and within a value).
    """

    def __init__(self, env, block, number_to_match, next_process=None, batch_option=False, batched_entity_type=None, stores=None, type_store_map=None):
        super().__init__(env, block, next_process)
        self.number_to_match = number_to_match
        self.batch_option = batch_option  # True: batch the matched entities; False: forward them separately
        self.batched_entity_type = batched_entity_type
        self.stores = list(stores) if stores else []  # GraphModel.attach_stores fills these
        self.type_store_map = type_store_map or {}   # e.g., {'A': 0, 'B': 1} -- optional
        self.match_on = ""      # PHASE5 O4: attribute whose values must be equal; "" = by type only
        # What waits, by store index: {e_type: deque of (seq, entity)}, oldest
        # first; a type with nothing waiting has no key. It mirrors the stores,
        # which only `self_process` fills and empties, so an arrival reads the
        # head of each queue instead of scanning every stored item.
        self._waiting = defaultdict(dict)
        self._seq = 0           # arrival counter: position order within a store

    def _store_index(self, entity):
        """Mapped store, else the first empty one, else one already holding the
        type, else the least loaded."""
        if entity.e_type in self.type_store_map:
            idx = self.type_store_map[entity.e_type]
            if not 0 <= idx < len(self.stores):
                raise RuntimeError(f"Match block '{self.name}': type_store_map sends "
                                   f"'{entity.e_type}' to store {idx}, but it has {len(self.stores)} stores.")
            return idx
        for i, store in enumerate(self.stores):
            if not store.items:
                return i
        for i in range(len(self.stores)):
            if entity.e_type in self._waiting[i]:
                return i
        return min(range(len(self.stores)), key=lambda i: len(self.stores[i].items))

    def _key(self, entity):
        try:
            return getattr(entity, self.match_on)
        except AttributeError:
            raise RuntimeError(f"Match block '{self.name}': entity {entity.id} has no attribute "
                               f"'{self.match_on}' to match on; ASSIGN it upstream.") from None

    def _oldest_per_type(self, value=None):
        """[(store index, e_type, entry)] -- one per waiting type, the oldest entity of
        that type (`entry` is its `(seq, entity)` in `_waiting`), ordered by
        how long that type has waited. With `match_on`, only the entities
        whose attribute equals *value*.

        Ties are what a scan of every stored item in store order gave before
        the index: within a type the lower store, then the earlier position;
        between types the one met first in that scan, which `first` restates
        as (store index, seq) because a store holds its items in seq order.
        """
        oldest = {}     # e_type -> (entered, first, store index, e_type, entry)
        for i, store in enumerate(self.stores):
            for e_type, queue in self._waiting[i].items():
                if self.match_on:
                    # The attribute is read now, not indexed, so a value written
                    # while the entity waits is seen, as it was by the scan.
                    # ponytail: walks the type's queue to the first equal value;
                    # index by (type, value) if attribute matches get long queues.
                    entry = next((e for e in queue if self._key(e[1]) == value), None)
                    if entry is None:
                        continue
                else:
                    entry = queue[0]
                entered = store._entry_times.get(entry[1].id, self.env.now)
                best = oldest.get(e_type)
                if best is None:
                    oldest[e_type] = (entered, (i, entry[0]), i, e_type, entry)
                elif entered < best[0]:
                    oldest[e_type] = (entered, best[1], i, e_type, entry)
        return [found[2:] for found in sorted(oldest.values(), key=lambda found: found[:2])]

    def self_process(self, entity):
        if self.ctx.debug:
            log.debug("Entity %s of type %s arrives at match at time %s.", entity.id, entity.e_type, self.env.now)
        if not self.stores:
            raise RuntimeError(f"Match block '{self.name}' has no stores; they are attached when the model is built.")

        # Store first, match afterwards: an entity is never in flight, so it
        # cannot be lost whatever its type (DEVLOG #27).
        idx = self._store_index(entity)
        put = self.stores[idx].put((entity.e_type, entity))
        # Indexed before yielding: the stores are unbounded, so the item is in
        # `items` already, and an arrival of the same instant must see it.
        self._seq += 1
        self._waiting[idx].setdefault(entity.e_type, deque()).append((self._seq, entity))
        yield put
        # Arrivals of one instant are all stored before any of them resumes, so
        # an earlier one may already have matched this entity away: it is not
        # waiting, and a "store" row would say it is. The check below still
        # runs, because others stored in this instant may now match.
        if entity.id in self.stores[idx]._entry_times:
            self.stores[idx]._log_state(event_type="store")

        # Only the arrival can complete a match (nothing matched before it), so
        # by attribute only ITS value needs looking at.
        waiting = self._oldest_per_type(self._key(entity) if self.match_on else None)
        if len(waiting) < self.number_to_match:
            return

        matched_entities = []
        for i, e_type, entry in waiting[:self.number_to_match]:
            ent = entry[1]
            queue = self._waiting[i][e_type]
            queue.remove(entry)     # the head unless `match_on` picked a later one
            if not queue:
                del self._waiting[i][e_type]    # `_store_index` reads the keys
            taken = self.stores[i].take(1, lambda item, e=ent: item[1] is e)
            matched_entities.extend(item[1] for item in taken)

        if self.batch_option:
            self.forward(make_batch_entity(self, matched_entities, self.batched_entity_type), 0)
        else:
            for e in matched_entities:
                self.forward(e, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "number_to_match": self.number_to_match,
            "batch_option": self.batch_option,
            "batched_entity_type": self.batched_entity_type,
            "type_store_map": self.type_store_map,
            "match_on": self.match_on,
        }

    def set_params_from_dict(self, param_dict):
        self.number_to_match = param_dict.get("number_to_match", 2)
        self.batch_option = param_dict.get("batch_option", False)
        self.batched_entity_type = param_dict.get("batched_entity_type", "BATCH")
        self.type_store_map = param_dict.get("type_store_map", {})
        match_on = str(param_dict.get("match_on", "") or "").strip()
        self.match_on = match_on[len("entity."):] if match_on.startswith("entity.") else match_on


SPEC = BlockSpec(
    type_name="MATCH", cls=Match,
    params=(
        ParamSpec("number_to_match", int, 2, description="Number of distinct entity types to match together.", quantity="count", min=2),
        ParamSpec("batch_option", bool, False, description="True: combine the matched entities into one batch entity. False: forward them separately.", quantity="none"),
        ParamSpec("batched_entity_type", str, "BATCH", description="Entity type assigned to the batch entity, when batch_option is True.", quantity="none"),
        ParamSpec("type_store_map", dict, {}, description="Optional {e_type: store index} mapping; default is automatic assignment.", quantity="none"),
        ParamSpec("match_on", str, "",
                  description="An entity attribute (e.g. \"order\"); entities are matched "
                              "only with others whose value of it is equal -- Arena's attribute "
                              "match -- still one per distinct type, FIFO within a value. Empty = "
                              "match by type alone.", quantity="none"),
    ),
    factory=lambda env, block: Match(env, block, number_to_match=2, stores=[]),
    family="group",
    holding=True,
)
