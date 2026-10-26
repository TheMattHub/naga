"""Storage dispatch as an optimum, not a merit order (Phase 15 M8, D846-D850).

Without this module, storage on a bus is dispatched by rules inside the flow
rates (`FLOWSUM("tag", priority)`, D62). That gives an exact ORDER, but no rule
looks ahead. The model's `dispatch` section adds a ROLLING-HORIZON LP, the
practice of a real energy management system: every `replan_every` it solves
the cheapest dispatch over `horizon` on a grid of `step`, from the measured
levels, and writes each driven storage's setpoint variable at every step
boundary. The level system then runs the physics exactly as before, so a
deviation from the plan (a noisy load) is absorbed by the grid flows and the
saturation rule, and the next replan starts from what really happened.

Why rolling and not the alternatives (D846): an instantaneous LP at each event
is myopic -- in one period stored energy has no value, so it never charges for
later and reduces to `self_consumption`. A whole-horizon LP is the same
mechanism with `horizon` >= the run and `replan_every` = `horizon`.

The plan sees the model only through two child specs (D847): a level's
`storage` (power limits, efficiencies, the setpoint variable, `optimal`) and a
flow's `grid` (`role` import/export, `limit`). The LP's prices are the grid
flows' own `price` expressions, so its objective is the cost the engine
itself accounts.

LP, per storage s and step k (all bus-side, base units):
    0 <= c <= charge_max, 0 <= d <= discharge_max,
    e_k = e_{k-1} + (eta_c c - d / eta_d - loss) dt, min <= e_k <= capacity;
per step: 0 <= i <= import limit, 0 <= x <= export limit, curtailed u >= 0,
unserved v >= 0, sum_s (d - c) + G - D + i - x - u + v = 0; one peak P >= i_k
and >= the realised peak. Minimise sum dt (p_imp i + p_exp x + voll v
+ EPS (c + d)) + peak_price P; with `terminal` "hold", e_K >= the level the
run started with. EPS breaks ties: no needless cycling and no
simultaneous charge and discharge. `voll` exists because the engine prices
unserved energy at 0; without it the LP would shed load.

Engine-side: numpy + scipy (HiGHS through `linprog`), no pandas, no Qt. A model
without the section never imports this module.
"""
from __future__ import annotations

import logging
import math
import time

import numpy as np

from nagasim.core import sd
from nagasim.core.expressions import ExpressionError, ExpressionScope, functions_with
from nagasim.core.problems import problem
from nagasim.core.quantities import SECTION_FIELDS, param, to_base
from nagasim.core.timeunits import ModelUnits, base_factor

log = logging.getLogger(__name__)

__all__ = ["Dispatcher", "build_dispatch", "validate_dispatch", "TERMINALS", "FORECASTS",
           "SUMMARY_COLUMNS"]

TERMINALS = ("hold", "free")
FORECASTS = ("model", "persistence")
SUMMARY_COLUMNS = ["Run", "Dispatch", "Solves", "Fallbacks", "Planned Cost", "Realised Cost"]
_FIELDS = {"name", "supply_tags", "demand_tags", "horizon", "step", "replan_every", "time_unit",
           "terminal", "forecast", "voll", "status_variable"}
# The tie-break cost per unit of throughput (see the module docstring).
EPS = 1e-6
# A plan value this close to 0 is written as 0, so a setpoint never carries
# solver noise into the trace.
_ZERO = 1e-9
# A rate reading model STATE cannot be evaluated at a future instant: its value
# then is not known now. AUX is followed through `sd.reach`, so it is not one.
_STATE_CALLS = frozenset(functions_with("held", "bus")) - {"AUX"}
_HIGHS = {"primal_feasibility_tolerance": 1e-10, "dual_feasibility_tolerance": 1e-10}


def _pinnable(expression, auxes) -> bool:
    if expression is None:
        return True
    calls = frozenset().union(*(e.calls for e in sd.reach(expression, auxes)))
    return not (calls & _STATE_CALLS)


class _Pinned:
    """The run's ctx with the clock pinned at a forecast instant and random
    draws at their mean (D119): every expression function reads the time
    through `ctx.now`, so the expression evaluates as it would at that time."""

    __slots__ = ("_ctx", "now")
    deterministic = True

    def __init__(self, ctx, now: float):
        self._ctx = ctx
        self.now = now

    def __getattr__(self, name):
        return getattr(self._ctx, name)


class _Storage:
    __slots__ = ("level", "setpoint", "cmax", "dmax", "eta_c", "eta_d", "loss", "per", "target")

    def __init__(self, level, spec: dict, rate_scale: float):
        self.level = level
        self.setpoint = str(spec.get("setpoint") or "")
        self.cmax = _cap(spec.get("charge_max"), rate_scale)
        self.dmax = _cap(spec.get("discharge_max"), rate_scale)
        self.eta_c = float(spec.get("charge_efficiency", 1.0))
        self.eta_d = float(spec.get("discharge_efficiency", 1.0))
        self.loss = float(spec.get("loss", 0.0) or 0.0) * rate_scale
        # The setpoint is written in the level's own rate unit, like charge_max.
        self.per = rate_scale
        # "hold" ends every plan at least at the level the RUN started with, not
        # the level this solve started from: anchoring on each replan's start
        # would ratchet a battery charged for tonight up to full for ever.
        self.target = level.level


def _cap(value, scale: float) -> float:
    return math.inf if value is None else float(value) * scale


class Dispatcher:
    """One run's plan: a SimPy process beside the `LevelSystem`."""

    def __init__(self, env, system, settings: dict, storages, imports, exports, supply, demand):
        self.env = env
        self.system = system
        self.name = settings["name"]
        self.step = settings["step"]
        self.horizon = settings["horizon"]
        self.replan = max(1, round(settings["replan_every"] / self.step))
        self.terminal = settings["terminal"]
        self.forecast = settings["forecast"]
        self.voll = settings["voll"]
        self.status = settings["status_variable"]
        self.storages = storages
        self.imports, self.exports = imports, exports        # (flow, base limit) or None
        auxes = {name: expression for name, (expression, _l)
                 in (getattr(env.ctx, "auxiliaries", None) or {}).items()}
        # (flow, pinnable rate, pinnable price) per forecast flow; sign +1 supply, -1 demand.
        self.bus = [(flow, sign, _pinnable(flow.rate_expr, auxes))
                    for sign, flows in ((1.0, supply), (-1.0, demand)) for flow in flows]
        self.grid = [(entry[0], _pinnable(entry[0].price_expr, auxes))
                     for entry in (imports, exports) if entry is not None]
        self.end = None
        self.solves = self.fallbacks = 0
        self.solve_seconds = 0.0
        self.planned_energy = 0.0
        self.planned_peak = 0.0
        self._warned = False

    def start(self, horizon: float | None = None) -> None:
        self.end = float(horizon) if horizon and horizon > 0 else None
        for storage in self.storages:      # a warm start has set the levels by now
            storage.target = storage.level.level
        self.env.process(self._run())

    # -- the process ----------------------------------------------------------
    def _run(self):
        env = self.env
        plan, index = None, 0
        k = 0
        while self.end is None or env.now < self.end - 1e-12:
            if k % self.replan == 0:
                plan, index = self._solve(env.now), 0
            dt = self.step if self.end is None else min(self.step, self.end - env.now)
            if plan is not None and index < len(plan["dt"]):
                self._apply(plan, index, dt)
            index += 1
            k += 1
            yield env.timeout(dt)

    def _apply(self, plan: dict, k: int, dt: float) -> None:
        ctx = self.env.ctx
        for s, storage in enumerate(self.storages):
            value = (plan["c"][s][k] - plan["d"][s][k]) / storage.per
            self._write(ctx, storage.setpoint, 0.0 if abs(value) < _ZERO else float(value))
        # The plan's cost counts over the observed window only, as every KPI does.
        start, warmup = self.env.now, float(getattr(ctx, "warmup", 0.0) or 0.0)
        share = 1.0 if start >= warmup else max(0.0, (start + dt - warmup) / dt)
        if share > 0.0:
            self.planned_energy += share * dt * (plan["p_imp"][k] * plan["i"][k]
                                                 + plan["p_exp"][k] * plan["x"][k])
            self.planned_peak = max(self.planned_peak, plan["i"][k])
        self._settle(ctx)

    def _write(self, ctx, name: str, value: float) -> None:
        ctx.variables[name] = value
        if ctx.watched or ctx.traced_variables:
            ctx.wrote_variable(name)

    def _settle(self, ctx) -> None:
        # A variable write is one of the events that re-evaluate the rates (D1).
        self.system.trigger()
        if getattr(ctx, "agent_system", None) is not None:
            ctx.agent_system.trigger()

    def _fallback(self, why: str) -> None:
        """No plan this time: the storages run their own rule (D849)."""
        self.fallbacks += 1
        log.warning("dispatch '%s' at t=%g: %s; the storages fall back to their rule",
                    self.name, self.env.now, why)
        ctx = self.env.ctx
        for storage in self.storages:
            self._write(ctx, storage.setpoint, 0.0)
        self._write(ctx, self.status, 0.0)
        self._settle(ctx)

    # -- one plan -------------------------------------------------------------
    def _steps(self, t0: float) -> np.ndarray:
        span = self.horizon if self.end is None else min(self.horizon, self.end - t0)
        count = max(1, math.ceil(span / self.step - 1e-9))
        dt = np.full(count, self.step)
        dt[-1] = span - self.step * (count - 1)
        return dt

    def _forecast(self, t0: float, dt: np.ndarray):
        """Net bus load D - G and the grid prices per step, base units."""
        ctx = self.env.ctx
        mids = t0 + np.cumsum(dt) - dt / 2.0
        when = mids if self.forecast == "model" else np.full(len(dt), t0)
        net = np.zeros(len(dt))
        unpinned = []
        for flow, sign, pinnable in self.bus:
            if pinnable:
                values = [self._value(flow.rate_expr, flow, t) for t in when]
            else:
                unpinned.append(flow.name)
                values = [flow.actual] * len(dt)
            net -= sign * np.asarray(values, dtype=float)
        prices = []
        for flow, pinnable in self.grid:
            if pinnable:
                prices.append(np.asarray([self._value(flow.price_expr, flow, t) for t in when]))
            else:
                unpinned.append(flow.name)
                prices.append(np.full(len(dt), flow.price))
        if unpinned and not self._warned:
            self._warned = True
            log.warning("dispatch '%s': %s read model state, so the plan holds their current "
                        "value (W_DISPATCH_FORECAST)", self.name, ", ".join(unpinned))
        p_imp = prices[0] if self.imports is not None else np.zeros(len(dt))
        p_exp = prices[-1] if self.exports is not None else np.zeros(len(dt))
        return net, p_imp, p_exp

    def _value(self, expression, flow, t: float) -> float:
        if expression is None:
            return 0.0
        locals_ = flow.scope.locals if flow.scope is not None else None
        return float(expression.evaluate(ExpressionScope(ctx=_Pinned(self.env.ctx, float(t)),
                                                         locals=locals_)))

    def _solve(self, t0: float):
        self.solves += 1
        self.system.sync()
        dt = self._steps(t0)
        try:
            net, p_imp, p_exp = self._forecast(t0, dt)
        except (ExpressionError, ValueError, ZeroDivisionError) as exc:
            self._fallback(f"forecast failed ({exc})")
            return None
        if not (np.all(np.isfinite(net)) and np.all(np.isfinite(p_imp))
                and np.all(np.isfinite(p_exp))):
            self._fallback("the forecast is not finite")
            return None
        started = time.perf_counter()
        result = self._lp(dt, net, p_imp, p_exp)
        self.solve_seconds += time.perf_counter() - started
        if result is None:
            return None
        self._write(self.env.ctx, self.status, 1.0)
        return result

    def _lp(self, dt, net, p_imp, p_exp):
        from scipy.optimize import linprog
        from scipy.sparse import coo_matrix

        K, S = len(dt), len(self.storages)
        n = 3 * K * S + 4 * K + 1
        g = 3 * K * S                       # first grid variable: i, then x, u, v, then P
        P = n - 1
        cost = np.zeros(n)
        lower, upper = np.zeros(n), np.full(n, np.inf)
        voll = self.voll if self.voll is not None else 100.0 * max(
            float(np.max(np.abs(p_imp))) if len(p_imp) else 0.0, 0.01)
        rows, cols, vals = [], [], []
        b_eq = np.zeros(K + K * S)
        b_eq[:K] = net
        ks = np.arange(K)
        for s, storage in enumerate(self.storages):
            c0, d0, e0 = 3 * K * s, 3 * K * s + K, 3 * K * s + 2 * K
            cost[c0:c0 + K] = cost[d0:d0 + K] = EPS * dt
            upper[c0:c0 + K], upper[d0:d0 + K] = storage.cmax, storage.dmax
            level = storage.level
            low = level.minimum
            high = math.inf if level.capacity is None else level.capacity
            lower[e0:e0 + K], upper[e0:e0 + K] = low, high
            start = level.level
            if self.terminal == "hold":
                lower[e0 + K - 1] = max(low, min(storage.target, high))
            # Balance: + d - c.
            rows += [*ks, *ks]
            cols += [*(d0 + ks), *(c0 + ks)]
            vals += [1.0] * K + [-1.0] * K
            # Energy: e_k - e_{k-1} - eta_c dt c_k + dt / eta_d d_k = -loss dt (+ e_start at k=0).
            r = K + K * s + ks
            rows += [*r, *r[1:], *r, *r]
            cols += [*(e0 + ks), *(e0 + ks[:-1]), *(c0 + ks), *(d0 + ks)]
            vals += ([1.0] * K + [-1.0] * (K - 1) + list(-storage.eta_c * dt)
                     + list(dt / storage.eta_d))
            b_eq[K + K * s: K + K * (s + 1)] = -storage.loss * dt
            b_eq[K + K * s] += start
        i0, x0, u0, v0 = g, g + K, g + 2 * K, g + 3 * K
        rows += [*ks, *ks, *ks, *ks]
        cols += [*(i0 + ks), *(x0 + ks), *(u0 + ks), *(v0 + ks)]
        vals += [1.0] * K + [-1.0] * K + [-1.0] * K + [1.0] * K
        cost[i0:i0 + K] = p_imp * dt
        cost[x0:x0 + K] = p_exp * dt
        cost[v0:v0 + K] = voll * dt
        upper[i0:i0 + K] = self.imports[1] if self.imports is not None else 0.0
        upper[x0:x0 + K] = self.exports[1] if self.exports is not None else 0.0
        peak_flow = self.imports[0] if self.imports is not None else None
        if peak_flow is not None and peak_flow.peak_price:
            cost[P] = peak_flow.peak_price * peak_flow.per
            lower[P] = peak_flow.peak
        a_eq = coo_matrix((vals, (rows, cols)), shape=(K + K * S, n)).tocsr()
        a_ub = coo_matrix(([1.0] * K + [-1.0] * K, ([*ks, *ks], [*(i0 + ks), *([P] * K)])),
                          shape=(K, n)).tocsr()
        res = linprog(cost, A_ub=a_ub, b_ub=np.zeros(K), A_eq=a_eq, b_eq=b_eq,
                      bounds=np.column_stack([lower, upper]), method="highs", options=_HIGHS)
        if res.status != 0 or res.x is None:
            self._fallback(f"the LP did not solve (status {res.status}: {res.message})")
            return None
        x = res.x
        return {"dt": dt, "p_imp": p_imp, "p_exp": p_exp,
                "c": [x[3 * K * s: 3 * K * s + K] for s in range(S)],
                "d": [x[3 * K * s + K: 3 * K * s + 2 * K] for s in range(S)],
                "i": x[i0:i0 + K], "x": x[x0:x0 + K]}

    # -- results --------------------------------------------------------------
    def summary_row(self) -> dict:
        planned = self.planned_energy
        realised = 0.0
        for entry in (self.imports, self.exports):
            if entry is not None:
                realised += entry[0].cost + entry[0].peak_cost
        if self.imports is not None:
            planned += self.imports[0].peak_price * self.imports[0].per * self.planned_peak
        return {"Dispatch": self.name, "Solves": self.solves, "Fallbacks": self.fallbacks,
                "Planned Cost": planned, "Realised Cost": realised}


# -- build ---------------------------------------------------------------------
def settings_of(section: dict, units: ModelUnits) -> dict:
    """The section's fields with defaults, its times in the model's base unit."""
    fields = SECTION_FIELDS["dispatch"]
    out = {name: to_base(param(fields, name), section, units, fields)
           for name in ("horizon", "step", "replan_every")}
    out.update(name=str(section.get("name") or "site"),
               terminal=section.get("terminal", "hold"),
               forecast=section.get("forecast", "model"),
               voll=section.get("voll"),
               status_variable=str(section.get("status_variable") or "dispatch_ok"))
    return out


def build_dispatch(env, layout: dict, system) -> Dispatcher | None:
    """The run's dispatcher, or None without a `dispatch` section."""
    section = layout.get("dispatch")
    if not section or system is None:
        return None
    units = ModelUnits(getattr(env.ctx, "time_unit", "minutes"))
    storages = []
    for entry in layout.get("levels") or []:
        spec = entry.get("storage")
        if isinstance(spec, dict) and spec.get("optimal"):
            scale = base_factor("rate", entry.get("rate_time_unit", "base") or "base", units)
            storages.append(_Storage(system.levels[str(entry["name"])], spec, scale))
    roles = {}
    for entry in layout.get("flows") or []:
        grid = entry.get("grid")
        if isinstance(grid, dict) and grid.get("role") in ("import", "export"):
            scale = base_factor("rate", entry.get("rate_time_unit", "base") or "base", units)
            roles[grid["role"]] = (system.flow_index[str(entry["name"])],
                                   _cap(grid.get("limit"), scale))
    supply = [f for f in system.flows if f.tag and f.tag in set(section.get("supply_tags") or ())]
    demand = [f for f in system.flows if f.tag and f.tag in set(section.get("demand_tags") or ())]
    return Dispatcher(env, system, settings_of(section, units), storages,
                      roles.get("import"), roles.get("export"), supply, demand)


# -- validation ------------------------------------------------------------------
def validate_dispatch(layout: dict, problems: list) -> None:
    """The `dispatch` section and the `storage`/`grid` specs it reads (a
    `core/sections.py` hook, run on the flattened layout)."""
    section = layout.get("dispatch")
    levels = [l for l in layout.get("levels") or [] if isinstance(l, dict)]
    flows = [f for f in layout.get("flows") or [] if isinstance(f, dict)]
    optimal = [l for l in levels if isinstance(l.get("storage"), dict)
               and l["storage"].get("optimal")]

    def bad(text, path="/dispatch", hint=None, code="E_DISPATCH"):
        problems.append(problem(code, f"dispatch: {text}", path, hint=hint))

    if section is None:
        for level in optimal:
            bad(f"level '{level.get('name')}' is marked optimal but the model has no dispatch "
                "section", f"/levels/{level.get('name')}",
                "add a \"dispatch\" section, or pick a rule dispatch for this storage")
        return
    if not isinstance(section, dict):
        bad("must be an object", hint="{\"supply_tags\": [\"generation\"], "
                                      "\"demand_tags\": [\"demand\"]}")
        return
    for field in sorted(set(section) - _FIELDS):
        bad(f"unknown field '{field}'", f"/dispatch/{field}")
    for field in ("supply_tags", "demand_tags"):
        tags = section.get(field)
        if not (isinstance(tags, list) and tags and all(isinstance(t, str) and t for t in tags)):
            bad(f"{field} must be a non-empty list of bus tags", f"/dispatch/{field}")
    numbers = {}
    for field, default in (("horizon", 24.0), ("step", 0.25), ("replan_every", 1.0)):
        value = section.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            bad(f"{field} must be a number > 0", f"/dispatch/{field}")
        else:
            numbers[field] = float(value)
    if len(numbers) == 3:
        if numbers["horizon"] < numbers["step"]:
            bad("horizon is shorter than step", "/dispatch/horizon")
        ratio = numbers["replan_every"] / numbers["step"]
        if abs(ratio - round(ratio)) > 1e-9:
            bad("replan_every must be a multiple of step", "/dispatch/replan_every")
        if numbers["replan_every"] > numbers["horizon"] + 1e-9:
            bad("replan_every is longer than horizon: the plan would run out before the next "
                "one", "/dispatch/replan_every")
    if section.get("terminal", "hold") not in TERMINALS:
        bad(f"unknown terminal '{section.get('terminal')}'", "/dispatch/terminal",
            " or ".join(TERMINALS))
    if section.get("forecast", "model") not in FORECASTS:
        bad(f"unknown forecast '{section.get('forecast')}'", "/dispatch/forecast",
            " or ".join(FORECASTS))
    voll = section.get("voll")
    if voll is not None and (isinstance(voll, bool) or not isinstance(voll, (int, float))
                             or voll < 0):
        bad("voll must be a number >= 0", "/dispatch/voll")
    variables = {v.get("name") for v in layout.get("variables") or [] if isinstance(v, dict)}
    status = section.get("status_variable", "dispatch_ok")
    if status not in variables:
        bad(f"status_variable '{status}' is not a declared variable", "/dispatch/status_variable",
            "declare it with initial 1 (the Energy library's Battery storage does)")
    roles = {}
    for flow in flows:
        grid = flow.get("grid")
        if grid is None:
            continue
        role = grid.get("role") if isinstance(grid, dict) else None
        if role not in ("import", "export"):
            bad(f"flow '{flow.get('name')}' has grid role '{role}'", f"/flows/{flow.get('name')}",
                "import or export")
        elif role in roles:
            bad(f"flows '{roles[role]}' and '{flow.get('name')}' both hold the grid role "
                f"'{role}'", f"/flows/{flow.get('name')}", "one import and one export per bus")
        else:
            roles[role] = flow.get("name")
    if not optimal:
        bad("no storage is marked optimal, so the plan drives nothing",
            hint="set \"dispatch\": \"optimal\" on a Battery storage")
    for level in optimal:
        setpoint = level["storage"].get("setpoint")
        if setpoint not in variables:
            bad(f"level '{level.get('name')}': setpoint '{setpoint}' is not a declared variable",
                f"/levels/{level.get('name')}/storage/setpoint")
    for level in levels:
        spec = level.get("storage")
        if isinstance(spec, dict) and not spec.get("optimal"):
            bad(f"storage '{level.get('name')}' keeps its own rule; the plan does not see it",
                f"/levels/{level.get('name')}", code="W_DISPATCH_MIXED")
    _check_forecasts(layout, section, flows, problems)


def _check_forecasts(layout, section, flows, problems) -> None:
    from nagasim.core.expressions import compile_expression

    tags = set(section.get("supply_tags") or ()) | set(section.get("demand_tags") or ())
    auxes = {}
    for aux in layout.get("auxiliaries") or []:
        try:
            auxes[str(aux["name"])] = compile_expression(str(aux.get("expr", "0")))
        except (ExpressionError, KeyError):
            continue
    for flow in flows:
        if flow.get("tag") not in tags:
            continue
        try:
            expression = compile_expression(str(flow.get("rate") or "0"))
        except ExpressionError:
            continue        # the levels validator reports it
        if not _pinnable(expression, auxes):
            problems.append(problem(
                "W_DISPATCH_FORECAST",
                f"dispatch: flow '{flow.get('name')}' reads model state, so the plan holds its "
                "current rate over the horizon", f"/flows/{flow.get('name')}"))
