import simpy
from collections import deque
from nagasim.settings import config
from nagasim.core.utils import StoreSnapshot
from nagasim.core.stats import TimeWeighted, Tally
import logging

log = logging.getLogger(__name__)

__all__ = ["StoreManager", "LoggedFilterStore"]


def _item_id(item):
    """Stores hold either an entity or Match's (e_type, entity) tuple."""
    return item[1].id if isinstance(item, tuple) else item.id


class LoggedFilterStore(simpy.FilterStore):
    def __init__(self, env, capacity=float('inf'), name="UnnamedFilterStore"):
        super().__init__(env, capacity)
        self.name = name
        # Snapshot deque feeds the flat Excel log sheet only; KPIs come from
        # the collectors below. `log_truncated` (ROADMAP 2.5, DEVLOG #62):
        # same idea as the resource log -- True once more than LOG_HISTORY
        # snapshots have been produced for this store.
        self.log = deque(maxlen=config.LOG_HISTORY)
        self.log_truncated = False
        # Events that WOULD have been logged: what "did anything happen to this
        # store" means with `record_logs` off (PHASE4 T7).
        self.log_events = 0
        self.size = TimeWeighted(env.now)
        self.residence = Tally()
        self._entry_times = {}  # maps id(entity) to env.now

    def reset_stats(self, t):
        """Warm-up hook: zero the accumulators, keep the current contents."""
        self.size.reset(t)
        self.residence.reset()

    def __repr__(self):
        return f"<LoggedFilterStore name='{self.name}', items={len(self.items)}>"

    def put(self, item):
        # A warm-started entity entered the store at `_since`, not now (D260): its
        # residence time counts from then.
        entity = item[1] if isinstance(item, tuple) else item
        since = getattr(entity, "_since", None)
        if since is not None:
            entity._since = None
        self._entry_times[_item_id(item)] = self._env.now if since is None else since
        return super().put(item)

    def _do_put(self, event):
        super()._do_put(event)
        if event.triggered:
            # Recorded here, where the item enters `items` (at once, or later
            # for a bounded store with no room), not when the put event is
            # processed: other puts and takes of the same instant come in
            # between, and the row would show a size the store never had at
            # that point (DEVLOG #344).
            self.size.set(len(self.items), self._env.now)
            self._log_state(event_type="put")

    def get(self, *args, **kwargs):
        result = super().get(*args, **kwargs)

        def log_exit(event):
            if not hasattr(event, 'value'):
                return
            entry_time = self._entry_times.pop(_item_id(event.value), None)
            if entry_time is not None:
                self.residence.add(self._env.now - entry_time)  # only real residence times (DEVLOG #31)
            self.size.set(len(self.items), self._env.now)
            self._log_state(event_type="get")

        result.callbacks.append(log_exit)
        return result

    def take(self, n=None, predicate=None):
        """Remove up to *n* matching items **now**, in one step, without yielding.

        FIFO over `items`; `predicate` sees the stored object (an entity, or the
        `(e_type, entity)` tuple Match stores); `n=None` takes every match.
        Accounting is exactly `get`'s: `size`, `residence` and the snapshot log.
        Blocks use it when a check-then-remove must not be interleaved by another
        process at the same timestamp (DEVLOG #27, #29).
        """
        if n == 1:
            # One item (MATCH takes one per store): stop at it and delete it in
            # place rather than walking and rebuilding the whole list.
            taken = []
            for i, item in enumerate(self.items):
                if predicate is None or predicate(item):
                    del self.items[i]
                    taken.append(item)
                    break
        else:
            taken, kept = [], []
            for item in self.items:
                wanted = (n is None or len(taken) < n) and (predicate is None or predicate(item))
                (taken if wanted else kept).append(item)
            if taken:
                self.items[:] = kept

        if not taken:
            return taken

        now = self._env.now
        for item in taken:
            entry_time = self._entry_times.pop(_item_id(item), None)
            if entry_time is not None:
                self.residence.add(now - entry_time)
        self.size.set(len(self.items), now)
        self._log_state(event_type="take")
        # Room was freed and the contents changed: let SimPy re-evaluate both
        # queues, exactly as a satisfied get would.
        self._trigger_put(None)
        self._trigger_get(None)
        return taken


    def _log_state(self, *, event_type=None):
        self.log_events += 1
        if self._env.ctx.watched:           # NS() in a HOLD condition (PHASE5 O4)
            self._env.ctx.touch(("store", self.name))
        if not self._env.ctx.record_logs:
            return                      # PHASE4 T7: no flat log for this run
        # Written out, not through a helper, as the resource log is: one
        # per put, get and take. Same record, same truncation rule.
        buffer = self.log
        if buffer.maxlen and len(buffer) >= buffer.maxlen:
            self.log_truncated = True
        buffer.append(StoreSnapshot(self._env.now, len(self.items), event_type))

class StoreManager:
    def __init__(self, env):
        self.env = env
        self.stores = {}

    def __repr__(self):
        return f"<StoreManager with {len(self.stores)} stores: {list(self.stores.keys())}>"

    def register(self, name, capacity=float('inf')):
        if name not in self.stores:
            store = LoggedFilterStore(self.env, capacity=capacity, name=name)
            self.stores[name] = store
        return self.stores[name]

    def get(self, name):
        return self.stores.get(name)

    def all(self):
        return self.stores.values()

    def exists(self, name):
        return name in self.stores