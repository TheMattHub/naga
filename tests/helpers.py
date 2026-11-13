"""Helpers that several test files had each defined for themselves, with the same body.

Imports only `nagasim` and `conftest`, so the exported engine tests can use it too. A test file
that needs a variant keeps its own: this module holds only what was identical in every copy.
"""
from nagasim.core import agents as A

from conftest import problem_codes, run_env


def run(layout, run_len, seed=0):
    """One headless replication; returns the env (`env.ctx` holds the run's state)."""
    env, _model = run_env(layout, run_len, seed=seed)
    return env


def codes(layout):
    """Every problem code the layout raises, whatever its severity."""
    return problem_codes(layout, severity=None)


def model(*records, time_limit):
    """A layout with no blocks, only agent/level `records`, and the given run length."""
    return A.add({"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}, *records)
