"""Constants of the optimisation and DOE package, in a module that imports
nothing from this package, so every submodule can read them without importing
the package `__init__` (D208)."""
from __future__ import annotations

__all__ = ["KINDS"]

KINDS = ("continuous", "integer", "categorical")
_KIND_ALIASES = {"float": "continuous", "real": "continuous", "cont": "continuous",
                 "int": "integer", "cat": "categorical", "choice": "categorical"}

# Default responses when a caller names none: the system frame only. Every KPI
# key `flatten_kpis` produces is available, but a design table with one row per
# resource column per point is noise, not information.
_DEFAULT_RESPONSE_PREFIX = "system/"
