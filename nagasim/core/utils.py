""" Set of utility functions for the simulation """

from __future__ import annotations

import logging

from dataclasses import dataclass
from heapq import heappush

from simpy.events import NORMAL

log = logging.getLogger(__name__)

__all__ = ["ResourceSnapshot", "StoreSnapshot", "timeout_at", "is_number"]

# Last-used paths are stored via QSettings in the Qt UI (product/ui/main_window.py),
# not here -- this file used to hold a JSON-file equivalent for the original editor.


# -------- shared validation predicate --------
def is_number(value) -> bool:
    """True for a plain int or float, excluding bool (a bool IS an int in
    Python, but a checkbox value should never pass a numeric-field check).
    C3.5: folds five byte-identical `_is_number` copies (core/conversions.py,
    core/levels.py, core/orders.py, core/tables.py, core/timeseries_inputs.py)
    plus core/initial_state.py's identically-bodied `_number`."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)

# -------- generic log structures --------
@dataclass(slots=True)
class ResourceSnapshot:      # LoggedSimpyResource.log
    time: float
    in_use: int
    queue_length: int
    event_type: str | None   = None

@dataclass(slots=True)
class StoreSnapshot:         # LoggedFilterStore.log
    time: float
    items: int
    event_type: str | None   = None


def timeout_at(env, t: float):
    """An event firing EXACTLY at *t*, as a SimPy timeout would: the agent
    system (CRN) and CREATE's thinned arrivals both need the exact
    instant. SimPy's own `timeout` adds a delay to now, and for some pairs no
    delay makes that sum land on *t* (DEVLOG #341), so this pushes the event on
    SimPy's heap at the absolute time instead: the one place the engine writes
    SimPy internals (D207; `tests/test_regressions.py` pins the behaviour). A
    *t* already past fires in the current instant; the clock never runs back."""
    event = env.event()
    event._ok = True
    event._value = None
    heappush(env._queue, (max(t, env.now), NORMAL, next(env._eid), event))
    return event
