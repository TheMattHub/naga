"""Lazy sync of flowless levels (D1097, gated by D1090).

A level no flow touches is folded into its statistics only when a block wrote it
(or `settle` reads it), not on every `sync`. That regroups the area and time sums,
so the gate is a tolerance, not bit-identity: against the eager sync it replaced,
every level statistic within 1e-9 relative and every level value, trace and
integer identical. `tools/regress_tolerance.py` holds the same on every bundled model.
"""
import json
import math
from pathlib import Path

import pytest

import nagasim
from nagasim.core import levels as levels_mod
from nagasim.core.levels import LevelSystem
from nagasim.core.outputs import calc_kpis
from nagasim.core.sim_runner import setup_run, step_until


class _EagerSync(LevelSystem):
    """The sync before D1097: every level folded on every call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._eager, self._lazy = tuple(self.levels.values()), ()


def _run(monkeypatch, cls, name: str, warmup: float):
    monkeypatch.setattr(levels_mod, "LevelSystem", cls)
    doc = json.loads((Path(nagasim.__file__).parent / "models" / f"{name}.json").read_text(encoding="utf-8"))
    env, _model = setup_run(doc, seed=5, run_len=600.0, warmup=warmup)
    seen = []
    env.ctx.bus.subscribe(lambda kind, **p: seen.append((kind, p.get("t"), p.get("resource"), p.get("value")))
                          if kind.startswith("level") else None)
    step_until(env, 600.0)
    kpis = calc_kpis(env, warmup=warmup, run_id=0)
    return env.ctx.level_system, kpis["level_summary"], seen


@pytest.mark.parametrize("name,warmup", [("line_with_buffers", 0.0), ("line_with_buffers", 50.0),
                                         ("inventory_sS", 30.0)])
def test_lazy_levels_give_the_eager_statistics_within_rounding(monkeypatch, name, warmup):
    system, new, new_trace = _run(monkeypatch, LevelSystem, name, warmup)
    assert system._lazy, "the model has no flowless level: the test proves nothing"
    _old_system, old, old_trace = _run(monkeypatch, _EagerSync, name, warmup)
    assert new_trace == old_trace                      # same values, same times, same order
    assert list(new.columns) == list(old.columns) and len(new) == len(old)
    for col in new.columns:
        for a, b in zip(new[col], old[col]):
            if isinstance(a, float):
                assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), (col, a, b)
            else:
                assert a == b, (col, a, b)


def test_a_written_level_folds_the_held_value_then_the_written_one():
    class _Env:
        now = 0.0
        ctx = None
    env = _Env()
    level = levels_mod.Level("tank", capacity=10.0, initial=2.0)
    system = LevelSystem.__new__(LevelSystem)       # just what sync/settle touch
    system.env, system.flows, system.levels = env, [], {"tank": level}
    system.conversions, system._conveyors, system._belt_open = (), (), False
    system._eager, system._lazy, system._last_t = (), (level,), 0.0
    level._held = math.nan
    for t in (1.0, 2.0, 3.0):                       # held at 2 over [0, 3]
        system.sync(t)
    level.level = 6.0                               # a BUFFER write at t = 3
    system.sync(5.0)                                # 6 over [3, 5]
    system.sync(9.0)
    system.settle()                                 # 6 over [5, 9]
    assert level._area == pytest.approx(2.0 * 3 + 6.0 * 6) and level._last_t == 9.0
    assert (level.min, level.max) == (2.0, 6.0)


def test_a_lump_written_by_a_real_transfer_block_folds_the_held_value_then_the_new_one():
    """The same fold as above through the engine: a TRANSFER adds 6 to an idle level at t = 5."""
    from conftest import chain, create, dispose, layout as make_layout, run_env
    c = create(max_arrivals=1, distribution="Constant", dist_params={"value": 5.0}, start_time=5)
    put = {"name": "put", "description": "put", "type": "TRANSFER", "position": [1, 0],
           "params": {"level": "Tank", "amount": "6", "mode": "add", "wait": False}}
    d = dispose()
    env, _model = run_env(make_layout([c, put, d], chain(c, put, d),
                                      levels=[{"name": "Tank", "capacity": 100, "initial": 10}]), 10.0)
    system = env.ctx.level_system
    tank = system.levels["Tank"]
    assert tank in system._lazy
    system.sync(10.0)
    system.settle()
    assert tank.level == pytest.approx(16.0)
    assert tank._area == pytest.approx(10.0 * 5 + 16.0 * 5)          # 10 held to the lump, then 16
    assert (tank.min, tank.max) == (10.0, 16.0)
