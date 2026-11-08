"""Group-wise levels solve (D1060): old-vs-new differential.

`LevelSystem._saturate` solves each flow-connected group of levels on its own,
and only when the group's desired rates or full/empty states moved. The
reference below is the all-levels solve it replaced, verbatim; on a model with
several independent groups (bounds hit, a power limit, a FLOWSUM bus, a series,
an SD grid, an entity TRANSFER, a flowless level) both must give the same time
series to the last bit.
"""
import pytest

from nagasim.core import levels as levels_mod
from nagasim.core.levels import EPS, MAX_SATURATION_PASSES, LevelSystem
from nagasim.core.sim_runner import setup_run, step_until
from conftest import block, chain, create, dispose, layout as make_layout


class _AllLevels(LevelSystem):
    """The solve before D1060: every level, every flow, on every event."""

    def _saturate(self) -> bool:
        src = [1.0] * len(self.flows)
        dst = [1.0] * len(self.flows)
        for _ in range(MAX_SATURATION_PASSES):
            changed = False
            for level in self.levels.values():
                full = level.capacity is not None and level.level >= level.capacity - EPS
                empty = level.level <= level.minimum + EPS
                if not (full or empty) and level.max_inflow is None and level.max_outflow is None:
                    continue
                ins = self._ins[level]
                outs = self._outs[level]
                for _inner in range(4):
                    in_rate = sum(f.desired * src[f.index] * dst[f.index] * f.efficiency
                                  for f in ins)
                    out_rate = sum(f.desired * src[f.index] * dst[f.index] for f in outs)
                    limit = None
                    if level.max_inflow is not None and in_rate > level.max_inflow + EPS:
                        limit = (ins, dst, in_rate, level.max_inflow)
                    elif level.max_outflow is not None and out_rate > level.max_outflow + EPS:
                        limit = (outs, src, out_rate, level.max_outflow)
                    elif full and in_rate > out_rate + EPS:
                        limit = (ins, dst, in_rate, out_rate)
                    elif empty and out_rate > in_rate + EPS:
                        limit = (outs, src, out_rate, in_rate)
                    if limit is None:
                        break
                    flows, scale, total, cap = limit
                    share = max(cap, 0.0) / total
                    for f in flows:
                        scale[f.index] *= share
                    changed = True
            if not changed:
                break
        self._sums.clear()
        moved = self._moved
        moved.clear()
        for level in self.levels.values():
            level.in_rate = level.out_rate = level.spill_rate = level.unmet_rate = 0.0
        stable = True
        for flow in self.flows:
            after_src = flow.desired * src[flow.index]
            actual = after_src * dst[flow.index]
            if actual != flow.actual:
                moved.add(("flow", flow.name))
                if flow.tag:
                    moved.add(("tag", flow.tag))
                if stable and abs(actual - flow.actual) > EPS:
                    stable = False
            flow.actual = actual
            if flow.src is not None:
                flow.src.out_rate += flow.actual
                flow.src.unmet_rate += flow.desired - after_src
            if flow.dst is not None:
                flow.dst.in_rate += flow.actual * flow.efficiency
                flow.dst.spill_rate += (after_src - flow.actual) * flow.efficiency
        for belt in self._conveyors:
            level = belt.level
            level.in_rate = level.out_rate = level.spill_rate = level.unmet_rate = 0.0
        return stable


def _model(sd):
    c = create(distribution="Exponential", dist_params={"rate": 0.7})
    draw = block("draw", "TRANSFER", {"level": "Stock", "amount": "3", "mode": "remove",
                                      "wait": True})
    lump = block("lump", "TRANSFER", {"level": "Loose", "amount": "1", "mode": "add",
                                      "wait": False})
    d = dispose()
    doc = make_layout(
        [c, draw, lump, d], chain(c, draw, lump, d),
        levels=[{"name": "Tank", "capacity": 40, "initial": 10},
                {"name": "Sump", "capacity": 50, "initial": 12},
                {"name": "BatA", "capacity": 30, "initial": 0, "max_inflow": 1},
                {"name": "BatB", "capacity": 25, "initial": 0},
                {"name": "Stock", "capacity": 60, "initial": 6},
                {"name": "Decay", "capacity": 100, "initial": 80},
                {"name": "Loose", "capacity": 10, "initial": 0},     # no flow: never solved
                {"name": "NetA", "initial": 5, "min": None},      # unbounded: a biflow's ends
                {"name": "NetB", "initial": 0, "min": None},
                # A conveyor writes `desired` itself, outside `_evaluate`.
                {"name": "Belt", "initial": 0, "conveyor": {"transit_time": 2}}],
        flows=[{"name": "fill", "to": "Tank", "rate": "TS(\"profile\")"},
               {"name": "drain", "from": "Tank", "rate": "1"},
               {"name": "big", "from": "Sump", "rate": "3"},
               {"name": "small", "from": "Sump", "rate": "LEVEL(\"Tank\") / 20", "min_rate": 1},
               {"name": "swap", "from": "NetA", "to": "NetB", "biflow": True,
                "rate": "TS(\"profile\") - 2.5"},
               {"name": "belt_in", "from": "Stock", "to": "Belt", "rate": "2"},
               {"name": "belt_out", "from": "Belt", "to": "Tank", "rate": ""},
               {"name": "refill", "to": "Sump", "rate": "1.5"},
               {"name": "pv", "rate": "TS(\"profile\") + 1", "tag": "generation"},
               {"name": "load", "rate": "2", "tag": "demand"},
               {"name": "charge_a", "to": "BatA", "tag": "storage_in", "priority": 1,
                "efficiency": 0.9,
                "rate": "max(FLOWSUM(\"generation\") - FLOWSUM(\"demand\") "
                        "- FLOWSUM(\"storage_in\", 1), 0)"},
               {"name": "charge_b", "to": "BatB", "tag": "storage_in", "priority": 2,
                "rate": "max(FLOWSUM(\"generation\") - FLOWSUM(\"demand\") "
                        "- FLOWSUM(\"storage_in\", 2), 0)"},
               {"name": "use_b", "from": "BatB", "rate": "0.4"},
               {"name": "restock", "to": "Stock", "rate": "1.2"},
               {"name": "decay", "from": "Decay", "rate": "0.05 * LEVEL(\"Decay\")"}],
        timeseries_inputs=[{"name": "profile", "start": 0, "step": 7,
                            "values": [3.0, 0.0, 5.0, 1.0], "beyond": "repeat"}])
    if sd != "euler":       # a conveyor needs the Euler grid
        doc["levels"] = [lv for lv in doc["levels"] if lv["name"] != "Belt"]
        doc["flows"] = [f for f in doc["flows"] if not f["name"].startswith("belt_")]
    doc["simulation"] = {"time_limit": 120.0, "warmup": 10.0, "replications": 1}
    if sd:
        doc["simulation"]["sd"] = {"dt": 0.25, "method": sd}
    return doc


def _trace(monkeypatch, cls, sd):
    monkeypatch.setattr(levels_mod, "LevelSystem", cls)
    env, _model_ = setup_run(_model(sd), seed=3, run_len=120.0, warmup=10.0)
    system = env.ctx.level_system
    assert type(system) is cls
    rows = []
    t = 0.0
    while t < 120.0:
        t = min(t + 0.37, 120.0)
        step_until(env, t)
        system.sync(t)
        rows.append(tuple(repr(x) for level in system.levels.values() for x in (
            level.level, level.in_rate, level.out_rate, level.spilled, level.unmet,
            level.time_avg(t), level.inflow_total, level.outflow_total)))
        rows.append(tuple(repr(x) for flow in system.flows for x in (
            flow.desired, flow.actual, flow.total, flow.peak, flow.limited_time)))
    return rows, env._eid


@pytest.mark.parametrize("sd", [None, "euler", "rk4"])
def test_group_solve_gives_the_all_levels_time_series_bit_for_bit(monkeypatch, sd):
    new_rows, new_events = _trace(monkeypatch, LevelSystem, sd)
    old_rows, old_events = _trace(monkeypatch, _AllLevels, sd)
    assert new_rows == old_rows
    assert repr(new_events) == repr(old_events)


def test_groups_are_the_flow_connected_levels_and_a_flowless_level_is_in_none(monkeypatch):
    env, _model_ = setup_run(_model(None), seed=0, run_len=1.0, warmup=0.0)
    system = env.ctx.level_system
    groups = sorted(sorted([l.name for l in g.levels] + [f.name for f in g.flows])
                    for g in system._groups)
    assert ["BatA", "charge_a"] in groups               # FLOWSUM is a read, not a link
    assert ["Sump", "big", "refill", "small"] in groups
    assert ["pv"] in groups and ["load"] in groups      # a flow with no level is its own group
    assert not any("Loose" in g for g in groups)
    assert system.levels["Loose"] not in system._flowed
