"""Write an event log from your own SimPy script, in the format `naga discover` reads.

Copy this file next to your script (it imports only the standard library, so it is cheap
to vendor), then::

    from simpylog import EventLog
    with EventLog("log.csv") as log:
        ...
        log.start(order, "Pack", env.now, resource="Bob")
        yield env.timeout(service)
        log.complete(order, "Pack", env.now, resource="Bob")

`env.now` is written as it is, so `naga discover log.csv --time-unit` should name the unit
your script counts in. Nothing here runs or loads your script: the log is a plain CSV, and
reading it is `discover`'s job (D725).
"""
from __future__ import annotations

import csv

__all__ = ["EventLog"]

_HEADER = ("case", "activity", "timestamp", "lifecycle", "resource")


class EventLog:
    """A CSV event log, one row per call. Close it (or use `with`) to flush the last rows."""

    def __init__(self, path):
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(_HEADER)

    def ready(self, case, activity, time) -> None:
        """*case* reached *activity* and waits for it (call before the `request`). Without it
        the log has no queue waits and discover sees a process that never queues."""
        self._row(case, activity, time, "schedule", None)

    def start(self, case, activity, time, resource=None) -> None:
        """*activity* began for *case* at *time* (`env.now`), on *resource* if it has one."""
        self._row(case, activity, time, "start", resource)

    def complete(self, case, activity, time, resource=None) -> None:
        """*activity* finished for *case* at *time*; discover pairs it with the earlier
        `start` of the same case and activity, in order, so repeated visits work."""
        self._row(case, activity, time, "complete", resource)

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _row(self, case, activity, time, lifecycle, resource) -> None:
        self._writer.writerow((case, activity, time, lifecycle, "" if resource is None else resource))
