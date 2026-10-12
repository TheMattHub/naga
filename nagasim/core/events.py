"""Engine event bus (ARCHITECTURE 10.2).

The engine emits; listeners are pure consumers. Headless means "no listener
subscribed", not a flag checked inside block bodies. Exceptions raised by a
listener propagate on purpose: a broken listener is a bug, not a warning.
"""

__all__ = ["EventBus"]


class EventBus:
    def __init__(self):
        self._subscribers = []

    def subscribe(self, fn):
        self._subscribers.append(fn)

    def unsubscribe(self, fn):
        self._subscribers.remove(fn)

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    def emit(self, name, **payload):
        for fn in self._subscribers:
            fn(name, **payload)
