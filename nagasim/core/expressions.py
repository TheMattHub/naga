"""Safe expression language for model files (ROADMAP 1.1; closes DEVLOG #53, #54).

A model file is data, not code. Every expression a model can carry -- Assign
targets/values, Decide and Hold conditions, the CustomExpression distribution,
and expression-valued distribution parameters -- is parsed with `ast.parse`,
checked against an allow-list at compile time, then evaluated by walking that
tree. Nothing reaches `eval`/`exec`, so a model file cannot touch the import
system, builtins, or any attribute other than `entity.<name>`.

Grammar (anything else is a compile-time ExpressionError):

    constants      int, float, str, bool, None
    names          entity, TNOW / now, HORIZON, true / false, model variables
    attributes     entity.<name>            (only on `entity`, no leading "_")
                   self.<name>              (the agent in hand, PHASE5 AB-1: its declared
                                             attributes, `state` and `id`)
                   AGENT("<pop>", i).<name> (agent i of a population, PHASE5 AB-2)
    operators      + - * / // % **, unary + - not, and/or, comparisons,
                   `in` / `not in` against a tuple or list literal,
                   `a if c else b`, tuple and list literals
    calls          the functions in FUNCTIONS, positional arguments only

A variable whose name is not an identifier -- the `<instance>/name` submodel
flattening writes for a `$variable` (D72) -- is read with `VAR("<name>")`.

Scope resolution order for a bare name: `entity`, `TNOW`/`now`, `HORIZON`, `true`/`false`,
block locals (submodel parameters, ROADMAP 2.3 part B), then model variables
(`ctx.variables`). Passing `variables=` to `compile_expression` turns an
unknown name into a compile-time error; without it the failure surfaces at run
time.
"""
from __future__ import annotations

import ast
import functools
import math
import operator
from dataclasses import dataclass

__all__ = ["Expression", "ExpressionError", "ExpressionScope", "compile_expression",
           "bind_constants", "literal_problems",
           "parse_target", "is_variable_name", "FUNCTIONS", "FUNCTION_NAMES", "RESERVED_NAMES",
           "SD_MACROS", "SD_VALUE_MACROS", "TRAITS", "functions_with", "LOOKUP_SECTION",
           "lookup_sections"]

# Guards against the two cheap ways to burn memory/CPU inside a single
# expression; everything else is bounded because the grammar has no loops.
MAX_EXPONENT = 1024      # |exponent| for ** -- blocks 9**9**9
MAX_REPEAT = 10_000      # sequence * int -- blocks 'a' * 10**9


class ExpressionError(ValueError):
    """An expression that failed to compile or to evaluate. `col` is the column
    in the source text when the failure has a location."""

    def __init__(self, message: str, col: int | None = None):
        self.message = message
        self.col = col
        super().__init__(message if col is None else f"{message} (col {col})")


@dataclass
class ExpressionScope:
    """What an expression may read: the entity in hand, the simulation context
    (time, variables, resources, stores) and the calling block's rng. A missing
    piece is not an error until an expression actually asks for it.

    `locals` (ROADMAP 2.3 part B) are the submodel parameters bound to the
    block: `submodels.flatten()` writes them onto every block it expands out of
    an instance, so `rate` inside a definition reads that instance's argument.
    They sit between the reserved names and the model variables in the
    resolution order below, so a parameter shadows a variable of the same name
    inside the definition only."""

    entity: object = None
    ctx: object = None
    rng: object = None
    locals: dict | None = None
    # The agent in hand (PHASE5 AB-1): (population, index), read by
    # `self.<attr>` through `population.read`, never `getattr`.
    agent: tuple | None = None


# -- function catalogue -----------------------------------------------------

def _rng(scope):
    if scope.rng is None:
        raise ExpressionError("no random stream available in this context")
    return scope.rng


def _deterministic(scope) -> bool:
    """PHASE6 W4 P1, D119: whether RAND/EXPO/UNIF/NORM/TRIA/DISC should return
    their mean instead of drawing. False (the ordinary, random behaviour)
    whenever the scope carries no ctx at all -- the same "no context, no
    special case" default every other ctx-reading function here already has."""
    return bool(scope.ctx is not None and getattr(scope.ctx, "deterministic", False))


def _ctx(scope):
    if scope.ctx is None:
        raise ExpressionError("no simulation context available in this context")
    return scope.ctx


def _disc(scope, *args):
    """DISC(v1, p1, v2, p2, ...): value vi with probability pi. Deterministic
    mode (D119): the weighted mean sum(vi * pi) -- DISC's own closed form,
    same idea as a distribution's `mean()`."""
    if len(args) % 2:
        raise ExpressionError("DISC takes value/probability pairs: DISC(v1, p1, v2, p2, ...)")
    values, probs = args[0::2], args[1::2]
    if any(p < 0 for p in probs):
        raise ExpressionError("DISC probabilities must be >= 0")
    total = float(sum(probs))
    if abs(total - 1.0) > 1e-9:
        raise ExpressionError(f"DISC probabilities sum to {total:.6g}, must be 1")
    if _deterministic(scope):
        return sum(v * p for v, p in zip(values, probs))
    r = _rng(scope).random()
    cumulative = 0.0
    for value, p in zip(values, probs):
        cumulative += p
        if r < cumulative:
            return value
    return values[-1]


def _resource(scope, name):
    # A resource or a resource set: a block names either, and so does NQ("Tellers") (#392).
    res = _ctx(scope).resource_manager.resolve(name)
    if res is None:
        raise ExpressionError(f"unknown resource '{name}'")
    return res


def _power(scope, name):
    """POWER("name") (D600): the kW a resource, fleet or charger draws now. The
    import is deferred because this module imports nothing from the engine."""
    from nagasim.core.outputs import power_now
    return power_now(_ctx(scope), name)


def _table(scope, name, *keys):
    """TABLE("name", k1[, k2]): a value out of the model's lookup tables
    (PHASE4 A1, `core/tables.py`). The tables are built once per run into
    `ctx.tables`, so this is a dict lookup or a bisect per dimension."""
    table = (getattr(_ctx(scope), "tables", None) or {}).get(name)
    if table is None:
        raise ExpressionError(f"unknown table '{name}'")
    return table.lookup(*keys)


def _series(scope, name, *when):
    """TS("name"[, t]): a time-series input's value now, or at time *t*
    (PHASE4 D2, `core/timeseries_inputs.py`). Built once per run into
    `ctx.series`, so this is one bisect."""
    ctx = _ctx(scope)
    series = (getattr(ctx, "series", None) or {}).get(name)
    if series is None:
        raise ExpressionError(f"unknown time series '{name}'")
    return series.value_at(when[0] if when else ctx.now)


def _level(scope, name):
    """LEVEL("name"): a level's current value (PHASE4 D1). The level engine is
    brought up to now first, so the answer is exact at any instant, not the
    value as of the last engine event."""
    system = getattr(_ctx(scope), "level_system", None)
    level = None if system is None else system.levels.get(name)
    if level is None:
        raise ExpressionError(f"unknown level '{name}'")
    if system._last_t != system.env.now:    # sync's own no-op test, without the call (SD-3)
        if system.peeking:                  # a game-mode snapshot (DEVLOG #229)
            return system.peek(level)
        system.sync()
    return level.level


def _flow(scope, name):
    """FLOW("name"): a flow's rate in effect right now, AFTER saturation -- so
    an accounting flow (a grid import) can be written in terms of what a
    physical one actually managed to move. Per the flow's own `rate_time_unit`
    (#678), like FLOWSUM."""
    system = getattr(_ctx(scope), "level_system", None)
    flow = None if system is None else system.flow_index.get(name)
    if flow is None:
        raise ExpressionError(f"unknown flow '{name}'")
    if system._initialising:        # the level-initial pass (DEVLOG #161)
        return float(flow.rate_expr.evaluate(flow.scope or system._scope)) * flow.per
    return flow.actual * flow.per


def _flowsum(scope, tag, below=None):
    """FLOWSUM("tag"[, priority]): the realised rates of every flow carrying
    *tag*, summed (PHASE4 D3, ARCHITECTURE D62), each per its own
    `rate_time_unit` (#678). That is the site BUS: an asset
    adds its generation or its demand to a named tag, and every other asset
    reads the total without naming a single instance. With a second argument,
    only the flows whose own `priority` is strictly lower are summed -- the
    merit order, so a second battery sees the surplus the first one left.
    An unknown tag is 0, not an error: a site with no generator has none.

    A plant-sized model calls this millions of times a run, so it takes its
    optional argument as a default rather than varargs and reads the context
    directly: `_ctx`'s guard is for expressions evaluated outside a run, and a
    flow rate is never one of those."""
    ctx = scope.ctx
    system = None if ctx is None else getattr(ctx, "level_system", None)
    if system is None:
        return 0.0
    return system.flow_sum(tag, below)


def _conversion(scope, name):
    """CONV("name"): a delayed conversion's product rate right now (Bio-1 G1).

    Piecewise constant between cohort closings and curve-step boundaries, so it
    is an ordinary flow rate: `{"rate": "CONV(\"Digester\")", "to": "Gas holder"}`
    feeds the gas into a level, where saturation and spill handle a full holder.
    Per the conversion's own `time_unit`, as FLOW() is per a flow's.
    """
    system = getattr(_ctx(scope), "level_system", None)
    conversion = None if system is None else system.conversion_index.get(name)
    if conversion is None:
        raise ExpressionError(f"unknown conversion '{name}'")
    return conversion.rate * conversion.per       # per its own time_unit (#678)


def _fraction(scope, name, component):
    """FRACTION("name", "component"): the share of a blended LEVEL's content
    (Bio-1 G2) or of a CONVERSION's product rate (G1) that is *component*.

    One function for both because a rate expression does not care which side of
    the mixing the number came from: `FLOW("gas") * FRACTION("Holder", "ch4")`
    is the methane a CHP is burning.
    """
    system = getattr(_ctx(scope), "level_system", None)
    if system is not None:
        level = system.levels.get(name)
        if level is not None:
            if component not in level.content:
                raise ExpressionError(f"level '{name}' declares no component '{component}'")
            if system.peeking:      # a game-mode snapshot (DEVLOG #229)
                return system.peek_fraction(level, component)
            system.sync()           # the content must be current, exactly as LEVEL() does
            return level.fraction(component)
        conversion = system.conversion_index.get(name)
        if conversion is not None:
            return conversion.fraction(component)
    raise ExpressionError(f"unknown level or conversion '{name}'")


def _distance(scope, a, b):
    """DISTANCE("a", "b"): shortest path along the model's path network
    (PHASE4 E1, `core/network.py`). Either name may be a network node, a
    STATION block or a station alias, so a plain DELAY or a TABLE-driven travel
    time can be written with the names already on the canvas:
    `DISTANCE(entity.station, "Shipping") / 1.4`.

    The network is built once per run and every shortest-path tree is cached, so
    a repeated call is a dict lookup.
    """
    ctx = _ctx(scope)
    network = getattr(ctx, "network", None)
    if network is None or not network:
        raise ExpressionError("the model declares no path network")
    places = getattr(ctx, "places", None) or {}
    try:
        return network.distance(places.get(a, a), places.get(b, b))
    except KeyError as exc:
        raise ExpressionError(str(exc).strip("'")) from None


def _variable(scope, name):
    """VAR("name"): a model variable by NAME (ARCHITECTURE D72).

    A bare name already reads a variable; VAR() exists for the names a bare
    name cannot spell. Submodel flattening rewrites a `$variable` inside a
    definition to `<instance>/variable`, which is not an identifier, so an
    asset that wants one variable PER INSTANCE -- a battery setpoint, a flare
    hysteresis gate -- writes `VAR("$setpoint")` and gets `VAR("bat_1/setpoint")`
    after flattening. A hand-written model may call it with any declared name.
    """
    variables = getattr(_ctx(scope), "variables", None)
    if variables is None or name not in variables:
        raise ExpressionError(f"unknown variable '{name}'")
    return variables[name]


def _aux(scope, name):
    """AUX("name"): an auxiliary -- a named expression evaluated ON READ
    (PHASE5 SD-2). Memoised for one evaluation pass of the level engine
    (`ctx.aux_memo`, a dict only while `LevelSystem._evaluate` runs), so a
    chain of auxiliaries read by many rates costs one evaluation each per pass.
    An auxiliary cloned out of a submodel instance reads its own parameters."""
    ctx = _ctx(scope)
    memo = ctx.aux_memo
    if memo is not None and name in memo:
        return memo[name]
    entry = ctx.auxiliaries.get(name)
    if entry is None:
        raise ExpressionError(f"unknown auxiliary '{name}'")
    expression, own = entry
    if scope.locals is not own:
        scope = ExpressionScope(scope.entity, ctx, scope.rng, own)
    try:
        value = expression.evaluate(scope)
    except ExpressionError as exc:
        raise ExpressionError(f"auxiliary '{name}': {exc.message}") from None
    if memo is not None:
        memo[name] = value
    return value


def _delay(scope, name):
    """DELAY("name"): a delay record's output (PHASE5 SD-2) -- the outflow of
    a material delay, the smoothed value of an information delay, the lagged
    input of a pipeline. `core/levels.py` owns them; each evaluates in its own
    scope, so the caller's entity and parameters never leak in."""
    system = getattr(_ctx(scope), "level_system", None)
    entry = None if system is None else system.delays.get(name)
    if entry is None:
        raise ExpressionError(f"unknown delay '{name}'")
    output, own = entry
    return output.evaluate(own)


# STEP/PULSE/RAMP (PHASE5 SD-2) are functions of the clock. A literal switching
# time is also a change instant of the level engine (`Expression.instants`), so
# the wake-up lands on it -- within float rounding, hence the tolerance.
_TIME_EPS = 1e-9


def _step(scope, height, start):
    return height if _ctx(scope).now >= start - _TIME_EPS else 0.0


def _pulse(scope, height, start, width):
    now = _ctx(scope).now
    return height if start - _TIME_EPS <= now < start + width - _TIME_EPS else 0.0


def _ramp(scope, slope, start, end=None):
    """Rises at *slope* from *start*; holds its value after *end* (Vensim)."""
    now = _ctx(scope).now
    if end is not None and now > end:
        now = end
    return slope * (now - start) if now > start else 0.0


def _sd_macro(name):
    def refuse(*_args):
        raise ExpressionError(f"{name}() is a system-dynamics delay: write it in a flow rate, an "
                              f"auxiliary or a delay input")
    return refuse


def _store(scope, name):
    """Accept either the store's own name or the block's name: GraphModel
    registers a Hold's store as '<block>_hold' and a Batch's as '<block>_batch'."""
    manager = _ctx(scope).store_manager
    for candidate in (name, f"{name}_hold", f"{name}_batch"):
        store = manager.get(candidate)
        if store is not None:
            return store
    raise ExpressionError(f"unknown store '{name}'")


# name -> (callable, needs_scope, min_args, max_args); max_args None = variadic.
FUNCTIONS = {
    "min": (min, False, 1, None),
    "max": (max, False, 1, None),
    "abs": (abs, False, 1, 1),
    "round": (round, False, 1, 2),
    "int": (int, False, 1, 1),
    "float": (float, False, 1, 1),
    "sqrt": (math.sqrt, False, 1, 1),
    "exp": (math.exp, False, 1, 1),
    "log": (math.log, False, 1, 2),
    "log10": (math.log10, False, 1, 1),
    "floor": (math.floor, False, 1, 1),
    "ceil": (math.ceil, False, 1, 1),
    # Sampled from the calling block's own rng, so a model stays reproducible.
    # Deterministic mode (PHASE6 W4 P1, D119): each returns its own mean
    # instead of drawing -- EXPO/NORM already take it as a parameter; RAND's
    # is Uniform(0, 1)'s mean, 0.5.
    "RAND": (lambda s: 0.5 if _deterministic(s) else float(_rng(s).random()), True, 0, 0),
    "EXPO": (lambda s, mean: float(mean) if _deterministic(s) else float(_rng(s).exponential(mean)), True, 1, 1),
    "UNIF": (lambda s, low, high: (low + high) / 2.0 if _deterministic(s)
             else float(_rng(s).uniform(low, high)), True, 2, 2),
    "NORM": (lambda s, mean, sd: float(mean) if _deterministic(s) else float(_rng(s).normal(mean, sd)), True, 2, 2),
    "TRIA": (lambda s, low, mode, high: (low + mode + high) / 3.0 if _deterministic(s)
             else float(_rng(s).triangular(low, mode, high)), True, 3, 3),
    "DISC": (_disc, True, 2, None),
    # Live model state.
    # Entities only: a unit a capacity schedule or a failure holds is out of
    # service, so it shows up in MR (fewer units scheduled), never in NQ or NR.
    "NQ": (lambda s, name: _resource(s, name).waiting, True, 1, 1),
    "NR": (lambda s, name: _resource(s, name).in_service, True, 1, 1),
    "MR": (lambda s, name: _resource(s, name).scheduled_capacity, True, 1, 1),
    # AVAILABLE("name") is MR under the name a CONTINUOUS model wants (Bio-1
    # G3): capacity in force after capacity schedules and failures, with units
    # an entity happens to hold NOT deducted -- a CHP is not seized by anyone.
    # A flow rate reading it is re-evaluated on every state change of that
    # resource (`resource_manager` wakes the level engine), never polled.
    "AVAILABLE": (lambda s, name: float(_resource(s, name).scheduled_capacity), True, 1, 1),
    # Units nothing holds right now: AVAILABLE minus what entities have seized.
    "IDLE": (lambda s, name: float(_resource(s, name).free), True, 1, 1),
    "NS": (lambda s, name: len(_store(s, name).items), True, 1, 1),
    # kW drawn now (D600): what a flow rate reads to price, peak-bill and emit it.
    "POWER": (_power, True, 1, 1),
    # A model variable by name, including a per-instance `<instance>/name` (D72).
    "VAR": (_variable, True, 1, 1),
    # Lookup tables (PHASE4 A1): TABLE("name", k1) or TABLE("name", k1, k2).
    "TABLE": (_table, True, 2, 3),
    # Time-series inputs (PHASE4 D2) and continuous quantities (PHASE4 D1).
    "TS": (_series, True, 1, 2),
    "LEVEL": (_level, True, 1, 1),
    "FLOW": (_flow, True, 1, 1),
    # The site bus (PHASE4 D3): FLOWSUM("tag") or FLOWSUM("tag", priority).
    "FLOWSUM": (_flowsum, True, 1, 2),
    # Delayed conversion and blended quality (Bio-1 G1/G2).
    "CONV": (_conversion, True, 1, 1),
    "FRACTION": (_fraction, True, 2, 2),
    # Geometry (PHASE4 E1): shortest path between two places on the network.
    "DISTANCE": (_distance, True, 2, 2),
    # System dynamics (PHASE5 SD-2). IF is lazy (`_build`): the branch not
    # taken is never evaluated, so IF(x > 0, 1 / x, 0) is safe.
    "IF": (lambda condition, a, b: a if condition else b, False, 3, 3),
    "STEP": (_step, True, 2, 2),
    "PULSE": (_pulse, True, 3, 3),
    "RAMP": (_ramp, True, 2, 3),
    "AUX": (_aux, True, 1, 1),
    "DELAY": (_delay, True, 1, 1),
}
# Inline delay shorthand (PHASE5 SD-2): `core/sd.expand` rewrites each call in a
# flow rate, an auxiliary or a delay input into DELAY("<owner>#k") before
# anything runs. Anywhere else it is E_SD_FUNCTION_CONTEXT, and evaluating one
# is an error. name -> (min args, max args, delay kind, order); the kind and
# order are what `core/sd.py` (`MACROS`) expands it into, None = DELAYN's own
# order argument.
SD_MACROS = {"SMOOTH": (2, 3, "information", 1), "SMOOTH3": (2, 3, "information", 3),
             "DELAY1": (2, 3, "material", 1), "DELAY3": (2, 3, "material", 3),
             "DELAYN": (3, 4, "material", None), "DELAYP": (2, 3, "pipeline", 1)}
FUNCTIONS.update({name: (_sd_macro(name), False, low, high)
                  for name, (low, high, _, _) in SD_MACROS.items()})


# ---------------------------------------------------------------- PHASE6 S2
# ZIDZ/XIDZ/MODULO (Vensim): plain, stateless functions -- no expand() step.
def _zidz(a, b):
    """ZIDZ(a, b): a/b, or 0 when b is 0 ("zero if dividing by zero")."""
    return 0.0 if b == 0 else a / b


def _xidz(a, b, x=0.0):
    """XIDZ(a, b, x): a/b, or x when b is 0 ("x if dividing by zero"). `x`
    defaults to 0 (making a 2-argument call ZIDZ's own behaviour) so XMILE's
    2- or 3-argument `SAFEDIV` maps onto this one function (`sdimport.py`)."""
    return x if b == 0 else a / b


def _modulo(a, b):
    """MODULO(a, b): the remainder of a / b with the SIGN OF A, Vensim's rule
    (DEVLOG #168, SDXorg `rounding`: MODULO(-10, 3) = -1) -- math.fmod, not `%`."""
    return math.fmod(a, b)


FUNCTIONS["ZIDZ"] = (_zidz, False, 2, 2)
FUNCTIONS["XIDZ"] = (_xidz, False, 2, 3)
FUNCTIONS["MODULO"] = (_modulo, False, 2, 2)


def _subscript_only(name):
    """PROD/VMIN/VMAX(x) (PHASE6 S2): the one-argument subscript-reduction
    form `core/sd.expand` unrolls over the "[*]" in x (the #124 star-binding
    rule SUM/MEAN already use) into a plain arithmetic/`min`/`max` expression.
    Reaching here means one was left where no "[*]" could be unrolled --
    E_SUBSCRIPT_AGGREGATE reports it, same as a leftover SUM/MEAN."""
    def refuse(x):
        raise ExpressionError(f"{name}(x) is unrolled over the \"[*]\" elements in x when the "
                              f"model is built; it is not evaluated directly")
    return refuse


FUNCTIONS.update({name: (_subscript_only(name), False, 1, 1) for name in ("PROD", "VMIN", "VMAX")})


def _table_extrapolate(scope, name, *keys):
    """TABLE_EXTRAPOLATE("name", k1[, k2]) (PHASE6 S2): like TABLE, but a
    `linear` axis continues past its ends along the slope of its last segment
    instead of clamping (`core/tables.py`, D50 kept TABLE itself clamping --
    Vensim's own default). A `step`/`exact` axis has no slope to extend, so it
    behaves exactly like TABLE there."""
    table = (getattr(_ctx(scope), "tables", None) or {}).get(name)
    if table is None:
        raise ExpressionError(f"unknown table '{name}'")
    return table.lookup(*keys, extrapolate=True)


FUNCTIONS["TABLE_EXTRAPOLATE"] = (_table_extrapolate, True, 2, 3)


def _sd_value_macro(name):
    """INITIAL/ACTIVE_INITIAL/SAMPLE_IF_TRUE/TREND/FORECAST/NPV/NOISE_NORMAL
    (PHASE6 S2): stateful SD functions, expanded by `core/sd.expand` into
    hidden levels/delays (or, for ACTIVE_INITIAL, a pure text substitution)
    BEFORE anything runs -- the same reason SMOOTH/DELAYn are: a compiled
    expression is cached by its text and cannot hold per-call-site state.
    Reaching here means one was used somewhere `expand` does not rewrite (a
    block expression, a TRANSFER amount, ...) -- E_SD_FUNCTION_CONTEXT."""
    def refuse(*_args):
        raise ExpressionError(f"{name}() is a system-dynamics function: write it in a level's "
                              f"initial value, a flow rate, an auxiliary or a delay field")
    return refuse


# name -> (min args, max args).
SD_VALUE_MACROS = {"INITIAL": (1, 1), "ACTIVE_INITIAL": (2, 2), "SAMPLE_IF_TRUE": (3, 3),
                   "TREND": (2, 3), "FORECAST": (3, 3), "NPV": (4, 4), "NOISE_NORMAL": (4, 4)}
FUNCTIONS.update({name: (_sd_value_macro(name), False, low, high)
                  for name, (low, high) in SD_VALUE_MACROS.items()})


def _agents(scope):
    system = getattr(_ctx(scope), "agent_system", None)
    if system is None:
        raise ExpressionError("the model declares no populations")
    return system


def _aggregate(name):
    def run(scope, population, attribute=None, state=None):
        if attribute is None:
            raise ExpressionError(f"{name}(x) is unrolled over the \"[*]\" elements in x when "
                                  f"the model is built; {name}(population, \"attr\") is the "
                                  f"agent form")
        return _agents(scope).aggregate(name, population, attribute, state)
    return run


# Subscript aggregates (PHASE5 SD-3): `core/sd.expand` unrolls SUM(x)/MEAN(x)
# over the `[*]` in x, so evaluating one means it was left where it cannot be
# (E_SUBSCRIPT_AGGREGATE). The two-argument form is the agent aggregate over a
# population's attribute column (PHASE5 AB-1; a third argument keeps only the
# agents in that state, AB-4), and COUNT(pop[, state]) reads
# the population's size or its O(1) per-state counter.
FUNCTIONS.update({name: (_aggregate(name), True, 1, 3) for name in ("SUM", "MEAN")})
def _count_call(s, population, *rest):
    """COUNT(pop[, state]) (unchanged) or, PHASE6 A2, COUNT(pop, attr, value)
    / COUNT(pop, state, attr, value) -- a category filter, its shape by how
    many args follow *population*, not a fixed position (so "no state" needs
    no placeholder)."""
    if len(rest) == 2:                                    # attr, value: no state
        rest = (None,) + rest
    return _agents(s).count(population, *rest)


FUNCTIONS["COUNT"] = (_count_call, True, 1, 4)
# AGENT("pop", i) (PHASE5 AB-2): a plain (population, index) reference -- what
# `entity.agent` holds and a `send` addresses; `AGENT(...).attr` reads a column.
FUNCTIONS["AGENT"] = (lambda s, population, index: _agents(s).ref(population, index), True, 2, 2)
# ALIVE(ref) (PHASE6 C5): whether a stored reference still names its agent; a
# removed agent's ref (its slot perhaps reused) raises anywhere else (D98).
FUNCTIONS["ALIVE"] = (lambda s, ref: _agents(s).alive(ref), True, 1, 1)
# NEAR/NEAREST (PHASE5 AB-3): self's own position against a cKDTree of
# *population*'s, rebuilt at most once per distinct instant.
FUNCTIONS["NEAR"] = (lambda s, population, r, state=None: _agents(s).near(population, r, state, s),
                     True, 2, 3)
FUNCTIONS["NEAREST"] = (lambda s, population, state=None: _agents(s).nearest(population, state, s),
                        True, 1, 2)
# Neighbour aggregates (PHASE6 A3): SUM/MEAN/MIN/MAX over the agents NEAR
# finds (radius r, inclusive), or KNN_SUM/KNN_MEAN over the k nearest.
FUNCTIONS.update({
    f"NEAR_{how}": (lambda s, population, attribute, r, state=None, how=how:
                    _agents(s).near_aggregate(how, population, attribute, r, state, s), True, 3, 4)
    for how in ("SUM", "MEAN", "MIN", "MAX")})
# NEAR_HEADING (PHASE6 A6): the CIRCULAR mean heading of the same neighbourhood
# NEAR counts -- a plain NEAR_MEAN would average 350 and 10 to 180, not 0 --
# for boids-style alignment; composes with NEAR_SUM/MEAN for separation/cohesion.
FUNCTIONS["NEAR_HEADING"] = (lambda s, population, r, state=None:
                             _agents(s).near_heading(population, r, state, s), True, 2, 3)
FUNCTIONS.update({
    f"KNN_{how}": (lambda s, population, attribute, k, state=None, how=how:
                   _agents(s).knn_aggregate(how, population, attribute, k, state, s), True, 3, 4)
    for how in ("SUM", "MEAN")})
# Grid space (PHASE6 A4, D102): self's CELL against a grid population's per-cell
# counts and its type's patches; every one needs the agent in hand.
FUNCTIONS["PATCH"] = (lambda s, name, dx=0, dy=0: _agents(s).patch(name, dx, dy, s), True, 1, 3)
FUNCTIONS["NEIGHBOURS"] = (lambda s, population, state=None:
                           _agents(s).neighbours(population, state, s), True, 1, 2)
FUNCTIONS.update({
    f"NEIGHBOUR_{how}": (lambda s, population, attribute, state=None, how=how:
                         _agents(s).neighbour_aggregate(how, population, attribute, state, s),
                         True, 2, 3)
    for how in ("SUM", "MEAN")})
FUNCTIONS["CELL_COUNT"] = (lambda s, population, dx=0, dy=0:
                           _agents(s).cell_count(population, dx, dy, s), True, 1, 3)
# Links (PHASE6 A5, D103): self's linked neighbours, all of its own population.
FUNCTIONS["LINKED"] = (lambda s, population, state=None:
                       _agents(s).link_count(population, state, s), True, 1, 2)
FUNCTIONS.update({
    f"LINK_{how}": (lambda s, attribute, state=None, how=how:
                    _agents(s).link_aggregate(how, attribute, state, s), True, 1, 2)
    for how in ("SUM", "MEAN")})
FUNCTIONS["IS_LINKED"] = (lambda s, ref: _agents(s).is_linked(ref, s), True, 1, 1)

# ---------------------------------------------------------------- PHASE6 A9
# Trigonometry in RADIANS, Python's and numpy's convention; DEG/RAD convert, so
# an agent heading (degrees, A6) reads COS(RAD(self.heading)). SQRT is the
# upper-case twin of `sqrt`, beside SIN/COS. `core/agents/` vectorises these
# with the SAME math functions per element, so numpy and the loop agree bit for bit.
FUNCTIONS.update({
    "SIN": (math.sin, False, 1, 1), "COS": (math.cos, False, 1, 1),
    "TAN": (math.tan, False, 1, 1), "ATAN2": (math.atan2, False, 2, 2),
    "SQRT": (math.sqrt, False, 1, 1), "PI": (lambda: math.pi, False, 0, 0),
    "DEG": (math.degrees, False, 1, 1), "RAD": (math.radians, False, 1, 1)})
# Inverse and hyperbolic trigonometry (Vensim/XMILE ARCSIN/ARCCOS/ARCTAN/SINH/COSH/TANH).
FUNCTIONS.update({name: (getattr(math, name.lower()), False, 1, 1)
                  for name in ("ASIN", "ACOS", "ATAN", "SINH", "COSH", "TANH")})

FUNCTION_NAMES = frozenset(FUNCTIONS)

# Function traits (ARCH-REVIEW B2): what the engine needs to know about a
# function beyond how to call it; every per-module name set is
# `functions_with(<trait>)`. A parallel table rather than a fifth FUNCTIONS
# field, because callers unpack FUNCTIONS as 4-tuples. Row order is kept by
# `functions_with`, and `_compile` fills `refs` in that order.
#   named_lookup    1st argument names a declared record, checked at compile (PHASE4 D1/D2)
#   population      1st argument names a population (schema's E_POPULATION_UNKNOWN)
#   resource_state  live state of a named resource; store_state: of a named store (O4)
#   sampler         draws from the block's rng: a rate re-evaluates every pass (levels)
#   bus             reads the flow bus, which moves between passes (levels)
#   dynamic         moves between events: re-evaluated on the SD grid (levels, D91)
#   held            non-agent model state, held between agent events (agents)
#   near/grid/link  reads self's position/cell/links: per agent, never shared (agents)
#   pure            same value for the same arguments: allowed in an OWN rate (agents, D94)
#   vectorised      applied element-wise with its FUNCTIONS callable (agents, A9)
#   steady          cannot change while nothing happens (HOLD condition, O4)
#   unit_lookup     unit is the named record's declared unit (units)
_NEAR_ROW = ("dynamic", "named_lookup", "population", "near")
_GRID_ROW = ("named_lookup", "population", "grid")
TRAITS = {
    "TS": ("dynamic", "named_lookup"),
    "LEVEL": ("dynamic", "held", "named_lookup", "unit_lookup"),
    "FLOW": ("bus", "held", "named_lookup"),
    "CONV": ("held", "named_lookup"),
    "VAR": ("named_lookup",),
    "AUX": ("held", "named_lookup", "unit_lookup"),
    "DELAY": ("dynamic", "held", "named_lookup", "unit_lookup"),
    **dict.fromkeys(("COUNT", "SUM", "MEAN"), ("dynamic", "named_lookup", "population")),
    "AGENT": ("named_lookup", "population"),
    **dict.fromkeys(("NEAR", "NEAREST", "NEAR_SUM", "NEAR_MEAN", "NEAR_MIN", "NEAR_MAX",
                     "NEAR_HEADING", "KNN_SUM", "KNN_MEAN"), _NEAR_ROW),
    **dict.fromkeys(("NEIGHBOURS", "NEIGHBOUR_SUM", "NEIGHBOUR_MEAN", "CELL_COUNT"), _GRID_ROW),
    "LINKED": ("named_lookup", "population", "link"),
    **dict.fromkeys(("NQ", "NR", "MR", "AVAILABLE", "IDLE"), ("held", "resource_state")),
    "NS": ("held", "store_state"),
    # Not resource_state: a HOLD would wait on ("res", name), which a charger
    # never touches, so a condition reading POWER keeps its scan.
    "POWER": ("held", "named_lookup"),
    "FLOWSUM": ("bus", "held"),
    "FRACTION": ("dynamic", "held"),
    "RAMP": ("dynamic",),
    **dict.fromkeys(("RAND", "EXPO", "UNIF", "NORM", "TRIA", "DISC"), ("sampler",)),
    "PATCH": ("grid",),
    **dict.fromkeys(("LINK_SUM", "LINK_MEAN", "IS_LINKED"), ("link",)),
    **dict.fromkeys(("TABLE", "DISTANCE"), ("steady",)),
    **dict.fromkeys(("min", "max", "round", "int", "float", "exp", "log", "log10", "floor", "ceil"),
                    ("pure", "steady")),
    **dict.fromkeys(("abs", "sqrt"), ("pure", "steady", "vectorised")),
    **dict.fromkeys(("IF", "PI"), ("pure",)),
    **dict.fromkeys(("SIN", "COS", "TAN", "ATAN2", "SQRT", "DEG", "RAD"), ("pure", "vectorised")),
}


def functions_with(*traits: str) -> tuple:
    """The functions tagged with any of *traits*, in TRAITS order."""
    return tuple(name for name, tags in TRAITS.items() if any(t in tags for t in traits))


# Which layout section a named-lookup function's first argument names --
# LEVEL("x") reads "levels", TS("x") reads "timeseries_inputs" (ARCH-REVIEW
# wave E3). One source for what `core/schema/layout.py` (`refs`, a literal
# LEVEL()/FLOW()/... call checked at compile time), `core/sd.py` and
# `core/sd_loops.py` (a subscript owner / a dependency-graph edge's origin
# section) and `ui/sdview.py` (an SD-diagram influence arrow's origin) used to
# hand-map three separate times over.
LOOKUP_SECTION = {"TS": ("timeseries_inputs",), "LEVEL": ("levels",), "FLOW": ("flows",),
                  "FRACTION": ("levels",), "CONV": ("conversions",), "VAR": ("variables",),
                  "DISTANCE": ("network",), "AUX": ("auxiliaries",), "DELAY": ("delays",),
                  "POWER": ("resources", "fleets", "chargers")}


def lookup_sections(*names: str) -> dict:
    """`LOOKUP_SECTION` filtered to *names*, each to its one section -- the
    subset a caller treats as an origin (not every named-lookup function is
    one, e.g. a population lookup resolves against "populations" generically,
    not this map)."""
    return {name: LOOKUP_SECTION[name][0] for name in names}


def name_sections(function: str) -> tuple:
    """The layout sections whose records the first argument of *function* names,
    () when it names none or the registry does not say (DISTANCE: a place is a
    block or a network node). For a completer; validation has its own maps."""
    tags = TRAITS.get(function, ())
    if "resource_state" in tags:
        return ("resources", "resource_sets")
    if "store_state" in tags:
        return ("blocks",)          # a Hold's or a Batch's store, under the block's name
    if "population" in tags:
        return ("populations",)
    if function in ("TABLE", "TABLE_EXTRAPOLATE"):
        return ("tables",)
    sections = LOOKUP_SECTION.get(function, ())
    return () if sections == ("network",) else sections


_NAMED_LOOKUPS = functions_with("named_lookup")
# Recorded in `refs` the same way so an event-driven HOLD condition knows which
# resource or store it reads (PHASE5 O4).
_STATE_LOOKUPS = functions_with("resource_state", "store_state")
# Names every expression resolves on its own. `self` is the agent in hand (AB-1).
_BUILTIN_NAMES = (frozenset({"entity", "self", "now", "TNOW", "HORIZON", "true", "false"})
                  | FUNCTION_NAMES)
# Names a model variable may not shadow: the built-ins, plus `payload`, which
# only a message transition binds (AB-2, DEVLOG #137) -- so it is NOT a
# built-in, and compiling it anywhere else still says "unknown name" -- and
# `sender`, which only a send/remove target's `where` binds (PHASE6 A9).
RESERVED_NAMES = _BUILTIN_NAMES | {"payload", "sender"}

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_COMPARE_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


# -- compile ----------------------------------------------------------------

def _check(node, variables):
    """Raise ExpressionError unless *node* is in the allow-list."""
    if isinstance(node, ast.Constant):
        if node.value is not None and not isinstance(node.value, (int, float, str, bool)):
            raise ExpressionError(f"unsupported constant of type {type(node.value).__name__}",
                                  node.col_offset)
        return

    if isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            raise ExpressionError("assignment is not allowed in an expression", node.col_offset)
        if node.id in ("self", "sender"):   # `self.<attr>` returns early below; a bare one lands here
            raise ExpressionError(f"'{node.id}' is only usable as {node.id}.<attribute>",
                                  node.col_offset)
        if variables is not None and node.id not in variables and node.id not in _BUILTIN_NAMES:
            raise ExpressionError(f"unknown name '{node.id}'", node.col_offset)
        return

    if isinstance(node, ast.Attribute):
        if _is_agent_call(node.value):
            _check(node.value, variables)
        elif _is_entity_agent(node.value):
            pass            # H1: entity.agent.<attr>, exactly two hops
        elif isinstance(node.value, ast.Name) and node.value.id == "sender":
            # PHASE6 A9: the sending agent, bound only in a send/remove target's
            # `where` -- validation compiles that one with "sender" among the names.
            if variables is not None and "sender" not in variables:
                raise ExpressionError("sender.<attribute> is readable only in a send or remove "
                                      "target's where", node.col_offset)
        elif not (isinstance(node.value, ast.Name) and node.value.id in ("entity", "self")):
            raise ExpressionError("only entity.<attribute>, entity.agent.<attribute>, "
                                  "self.<attribute> and AGENT(...).<attribute> access is allowed",
                                  node.col_offset)
        if node.attr.startswith("_"):
            raise ExpressionError(f"attribute '{node.attr}' is not accessible", node.col_offset)
        return

    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ExpressionError(f"operator {type(node.op).__name__} is not allowed", node.col_offset)
        _check(node.left, variables)
        _check(node.right, variables)
        return

    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise ExpressionError(f"operator {type(node.op).__name__} is not allowed", node.col_offset)
        _check(node.operand, variables)
        return

    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _check(value, variables)
        return

    if isinstance(node, ast.Compare):
        for op, comparator in zip(node.ops, node.comparators):
            if type(op) not in _COMPARE_OPS:
                raise ExpressionError(f"comparison {type(op).__name__} is not allowed", node.col_offset)
            if isinstance(op, (ast.In, ast.NotIn)) and not isinstance(comparator, (ast.Tuple, ast.List)):
                raise ExpressionError("'in' is only allowed against a tuple or list literal",
                                      node.col_offset)
            _check(comparator, variables)
        _check(node.left, variables)
        return

    if isinstance(node, ast.IfExp):
        for part in (node.test, node.body, node.orelse):
            _check(part, variables)
        return

    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            _check(element, variables)
        return

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("only calls to the built-in functions are allowed", node.col_offset)
        spec = FUNCTIONS.get(node.func.id)
        if spec is None:
            raise ExpressionError(f"unknown function '{node.func.id}'", node.col_offset)
        if node.keywords:
            raise ExpressionError("keyword arguments are not allowed", node.col_offset)
        _, _, low, high = spec
        count = len(node.args)
        if count < low or (high is not None and count > high):
            expected = str(low) if high == low else f"{low}..{'n' if high is None else high}"
            raise ExpressionError(f"{node.func.id}() takes {expected} argument(s), got {count}",
                                  node.col_offset)
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                raise ExpressionError("argument unpacking is not allowed", node.col_offset)
            _check(argument, variables)
        return

    raise ExpressionError(f"{type(node).__name__} is not allowed in an expression",
                          getattr(node, "col_offset", None))


def _is_agent_call(node) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "AGENT")


def _is_entity_agent(node) -> bool:
    """`entity.agent`: the reference an agent's `enter` gives its entity."""
    return (isinstance(node, ast.Attribute) and node.attr == "agent"
            and isinstance(node.value, ast.Name) and node.value.id == "entity")


def entity_agent(scope, col=None) -> tuple:
    """(population, index) of the entity's agent (H1); ExpressionError when
    the entity has none, or its agent was removed (D98)."""
    if scope.entity is None:
        raise ExpressionError("no entity is available in this context", col)
    ref = getattr(scope.entity, "agent", None)
    if ref is None:
        raise ExpressionError("entity.agent: this entity has no agent (only an agent's "
                              "enter action gives it one)", col)
    return _agents(scope).deref(ref)


class Expression:
    """A compiled, validated expression. Immutable and shareable: all run-time
    state arrives through the ExpressionScope passed to `evaluate`.

    `evaluate` calls a closure tree built once by `_build` (ROADMAP 2.5):
    profiling tools/bench.py's expression_heavy benchmark showed the previous
    design -- walk the AST and `isinstance`-dispatch every node on every
    `evaluate()` call -- as the single largest cost for an expression-heavy
    model, about a quarter of total wall time. `_compile` below is still the
    one-time cost (cached by `functools.lru_cache`, same as before); only the
    per-evaluation dispatch changed, from a chain of `isinstance` checks to a
    plain function call per node, decided once at compile time instead of
    every time.
    """

    __slots__ = ("text", "names", "calls", "tables", "refs", "computed_refs", "bus_reads",
                 "instants", "_eval_fn")

    def __init__(self, text: str, eval_fn, names: frozenset, tables=(), refs=None, calls=(),
                 bus_reads=None, computed_refs=frozenset(), instants=()):
        self.text = text
        self.names = names      # referenced bare names, function names excluded
        # The FUNCTION names this expression calls. `core/levels.py` reads it to
        # tell a rate that can change within one re-evaluation (it reads the bus,
        # or samples) from one that cannot -- see LevelSystem._recompute.
        self.calls = frozenset(calls)
        # TABLE("name", ...) calls with a literal name, as (name, key count):
        # what `core/schema/` checks against the declared tables (PHASE4 A1).
        # A computed name is invisible here and fails at run time instead.
        self.tables = tuple(tables)
        # Same idea for the one-name lookups: {"TS"|"LEVEL"|"FLOW": (name, ...)}
        # with a literal first argument (PHASE4 D1/D2).
        self.refs = refs or {}
        # The lookups (keys of `refs`) called at least once with a COMPUTED
        # name, which `refs` cannot list: a reader that needs every name (an
        # event-driven HOLD, PHASE5 O4) must treat these as unknown.
        self.computed_refs = computed_refs
        # What this expression reads off the FLOW BUS, as ("flow", name) and
        # ("tag", tag) keys -- or None when it reads something it cannot name
        # (a computed FLOW()/FLOWSUM() argument). `core/levels.py` uses it to
        # re-evaluate a rate only when something it actually reads has moved.
        self.bus_reads = bus_reads
        # The literal times at which a STEP/PULSE/RAMP here switches (PHASE5
        # SD-2), sorted -- change instants for the level engine -- or None
        # when a STEP/PULSE time is computed and so cannot be known ahead.
        self.instants = instants
        self._eval_fn = eval_fn

    def evaluate(self, scope: ExpressionScope):
        return self._eval_fn(scope)

    def closure(self):
        """The compiled closure itself, so a hot loop can bind it ONCE instead
        of paying this method's own call on every evaluation. `core/levels.py`
        binds the rate of every flow this way at build time."""
        return self._eval_fn

    def __repr__(self):
        return f"<Expression {self.text!r}>"


@functools.lru_cache(maxsize=1024)
def _compile(text: str, variables: frozenset | None) -> Expression:
    # The parser, the checker and the closure builder each recurse once per nesting level,
    # so a deep enough text fails in whichever runs out first: one answer for all of them.
    # The parser reports its own stack as a MemoryError.
    try:
        return _compile_tree(text, variables)
    except (RecursionError, MemoryError):
        raise ExpressionError("expression is nested too deeply") from None


def _compile_tree(text: str, variables: frozenset | None) -> Expression:
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"syntax error: {exc.msg}", exc.offset) from None
    return _expression(text, tree, variables)


def _expression(text: str, tree, variables: frozenset | None) -> Expression:
    """Validate a parsed tree and build its Expression."""
    _check(tree.body, variables)
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - called
    tables = [(node.args[0].value, len(node.args) - 1)
              for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("TABLE", "TABLE_EXTRAPOLATE") and node.args
              and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)]
    refs: dict[str, tuple] = {}
    computed = set()
    # FLOWSUM's tags too, so a unit check reads the folded literals (#690).
    for func in _NAMED_LOOKUPS + _STATE_LOOKUPS + ("FLOWSUM",):
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == func]
        found = tuple(node.args[0].value for node in calls
                      if node.args and isinstance(node.args[0], ast.Constant)
                      and isinstance(node.args[0].value, str))
        if found:
            refs[func] = found
        if len(found) < len(calls):
            computed.add(func)
    # DISTANCE("a", "b") names a place in BOTH arguments (PHASE4 E1), so both
    # literals go under the one key and are checked the same way.
    places = tuple(arg.value for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "DISTANCE"
                   for arg in node.args
                   if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
    if places:
        refs["DISTANCE"] = places
    return Expression(text, _build(tree.body), frozenset(names), tables, refs, called,
                      _bus_reads(tree), frozenset(computed), _instants(tree))


def _instants(tree):
    """Literal switching times of STEP(h, t0), PULSE(h, t0, w) and RAMP(s, t0[,
    t1]), or None when a STEP/PULSE time is computed (PHASE5 SD-2). A computed
    RAMP time costs nothing: RAMP is re-evaluated on the SD grid anyway."""
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("STEP", "PULSE", "RAMP")):
            continue
        times = [arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float))
                 and not isinstance(arg.value, bool) else None for arg in node.args[1:]]
        if node.func.id == "PULSE" and None not in times and len(times) == 2:
            times = [times[0], times[0] + times[1]]
        if None in times and node.func.id != "RAMP":
            return None
        found.update(float(t) for t in times if t is not None)
    return tuple(sorted(found))


def _bus_reads(tree):
    """The flow-bus keys this expression reads, or None if it reads one it
    cannot name. `FLOW("x")` is ("flow", "x") and `FLOWSUM("t"[, p])` is
    ("tag", "t") -- the priority argument is deliberately ignored, which makes
    the answer conservative (a flow of that tag moving counts even when it
    sits behind *p* in the merit order) and therefore always safe."""
    keys = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        kind = {"FLOW": "flow", "FLOWSUM": "tag"}.get(node.func.id)
        if kind is None:
            continue
        target = node.args[0] if node.args else None
        if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
            return None
        keys.add((kind, target.value))
    return frozenset(keys)


def compile_expression(text, variables=None) -> Expression:
    """Compile *text*, raising ExpressionError on anything outside the grammar.
    Pass *variables* (the model's variable names) to make an unknown name a
    compile-time error. Results are cached, so re-compiling is free."""
    return _compile(str(text), frozenset(variables) if variables is not None else None)


def _literal_number(node):
    """The value of a numeric literal, a unary minus or plus allowed; None otherwise.
    A bool is not a number here (`True` is an int to Python)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _literal_number(node.operand)
        return None if value is None else (-value if isinstance(node.op, ast.USub) else value)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    return None


def literal_problems(text) -> list[str]:
    """What a DISC or TRIA call with only literal arguments would raise or get wrong
    on every draw (D912), found before the run: DISC's pairs and probabilities as
    `_disc` checks them, TRIA's low <= mode <= high. A call with any computed
    argument is left to run time; text that does not parse is reported elsewhere."""
    try:
        tree = ast.parse(str(text), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("DISC", "TRIA") and node.args and not node.keywords):
            continue
        args = [_literal_number(arg) for arg in node.args]
        if any(arg is None for arg in args):
            continue
        if node.func.id == "TRIA":
            if len(args) == 3 and not args[0] <= args[1] <= args[2]:
                found.append(f"TRIA({args[0]}, {args[1]}, {args[2]}) needs low <= mode <= high")
        elif len(args) % 2:
            found.append("DISC takes value/probability pairs: DISC(v1, p1, v2, p2, ...)")
        elif any(p < 0 for p in args[1::2]):
            found.append("DISC probabilities must be >= 0")
        elif abs(float(sum(args[1::2])) - 1.0) > 1e-9:      # the tolerance of `_disc`
            found.append(f"DISC probabilities sum to {float(sum(args[1::2])):.6g}, must be 1")
    return found


# What `bind_constants` folds: plain values only, by exact type, so nothing a
# model file can put in a parameter reaches the tree except what `_check`
# accepts as a literal anyway.
_FOLDABLE = (int, float, str, bool)
# Arithmetic safe to do once: `**` and `*` on a sequence keep their run-time
# size guards (MAX_EXPONENT, MAX_REPEAT), so they are never folded.
_FOLD_NUMERIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)


def bind_constants(text, variables, constants) -> Expression:
    """`compile_expression`, with every name in *constants* (a flow's submodel
    parameters, fixed for the whole run) written in as its value.

    A parameter then costs nothing per evaluation, and a bus name built from
    one -- `FLOWSUM("biogas" + circuit)` -- becomes the literal it always was,
    so `bus_reads` can name it and the level engine re-evaluates the rate only
    when that tag moves instead of on every pass (ARCHITECTURE 5.19).

    The result is cached by text AND by the values folded in, never stored on
    the shared compiled expression: two instances of one submodel, or two
    models in one process, with different arguments get two closures."""
    variables = frozenset(variables) if variables is not None else None
    base = _compile(str(text), variables)
    # `repr` sits in the key because 1, 1.0 and True (and 0.0, -0.0) are equal
    # and hash alike, yet do not fold to the same expression.
    items = tuple(sorted((name, repr(constants[name]), constants[name]) for name in base.names
                         if name in constants and name not in RESERVED_NAMES
                         and type(constants[name]) in _FOLDABLE))
    return _bind(base.text, variables, items) if items else base


@functools.lru_cache(maxsize=4096)
def _bind(text: str, variables: frozenset | None, items: tuple) -> Expression:
    try:
        tree = ast.parse(text, mode="eval")     # `bind_constants` compiled it: it parses
        tree.body = _fold(tree.body, {name: value for name, _repr, value in items})
        # Checked again like any other tree: `_build` only ever sees a validated one.
        return _expression(text, tree, variables)
    except (RecursionError, MemoryError):
        raise ExpressionError("expression is nested too deeply") from None


def _fold(node, constants: dict):
    """*node* with the names in *constants* replaced by their values, and the
    arithmetic left between two literals done. Same operator on the same
    operands as the closure would run, so the value is bit-identical; one that
    raises is left in place to fail at evaluation, with its usual message."""
    if isinstance(node, ast.Name):
        if node.id in constants:
            return ast.copy_location(ast.Constant(constants[node.id]), node)
        return node
    if isinstance(node, ast.Attribute):
        return node         # entity.<attr>, self.<attr>: that Name is not a value
    if isinstance(node, ast.Call):
        node.args = [_fold(argument, constants) for argument in node.args]
        return node         # node.func is the function's name, not a parameter
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            setattr(node, field, _fold(value, constants))
        elif isinstance(value, list):
            setattr(node, field, [_fold(v, constants) if isinstance(v, ast.AST) else v
                                  for v in value])
    if (isinstance(node, ast.BinOp) and isinstance(node.left, ast.Constant)
            and isinstance(node.right, ast.Constant)):
        left, right = node.left.value, node.right.value
        kinds = (type(left), type(right))
        if (kinds == (str, str) and isinstance(node.op, ast.Add)) or (
                isinstance(node.op, _FOLD_NUMERIC) and all(k in (int, float) for k in kinds)):
            try:
                return ast.copy_location(ast.Constant(_BIN_OPS[type(node.op)](left, right)), node)
            except Exception:       # 1 / 0: the model's error, reported when it is evaluated
                return node
    return node


def is_variable_name(text) -> bool:
    """True for a declared variable's name: an identifier, or the scoped
    `<instance>/<identifier>` that submodel flattening writes for a `$variable`
    (ARCHITECTURE D72). An instance name may hold spaces, so only the LAST
    segment has to be an identifier -- it is the name the definition wrote."""
    text = str(text)
    head, sep, leaf = text.rpartition("/")
    return leaf.isidentifier() and (not sep or bool(head))


def parse_target(text) -> tuple[str, str]:
    """Split an assignment target into ("entity", attr), ("self", attr) (an
    agent's own attribute, PHASE5 AB-2; only an agent action accepts it),
    ("agent", attr) (`entity.agent.<attr>`, H1; only ASSIGN accepts it) or
    ("variable", name)."""
    text = str(text).strip()
    if text.startswith("entity.agent."):
        attr = text[len("entity.agent."):]
        if not attr.isidentifier() or attr.startswith("_"):
            raise ExpressionError(f"invalid agent attribute: '{text}'")
        if attr in ("state", "id"):
            raise ExpressionError(f"'{text}' is read-only (an agent changes state only "
                                  f"through its own transitions)")
        return "agent", attr
    for kind in ("entity", "self"):
        if text.startswith(kind + "."):
            attr = text[len(kind) + 1:]
            if not attr.isidentifier() or attr.startswith("_"):
                raise ExpressionError(f"invalid {kind} attribute: '{text}'")
            if text == "entity.agent":      # #225: set only by CREATE population / enter
                raise ExpressionError("'entity.agent' is read-only (set by CREATE population "
                                      "or an agent's enter action)")
            return kind, attr
    if not is_variable_name(text):
        raise ExpressionError(f"invalid assignment target: '{text}'")
    if text in RESERVED_NAMES:
        raise ExpressionError(f"'{text}' is a reserved name")
    return "variable", text


# -- evaluate ---------------------------------------------------------------

def _name_value(ident: str, scope: ExpressionScope):
    if ident == "entity":
        if scope.entity is None:
            raise ExpressionError("no entity is available in this context")
        return scope.entity
    if ident in ("TNOW", "now"):
        return _ctx(scope).now
    if ident == "HORIZON":
        # The run length asked for, in the model's base time unit, or 0 for a
        # run with no time limit (PHASE4 D5). Known before the first event, so
        # a fixed cost can prorate an annuity over the window it is compared
        # against: "capex * annual_rate * HORIZON / 8760" on an hourly model.
        return float(getattr(_ctx(scope), "sim_horizon", None) or 0.0)
    if ident == "true":
        return True
    if ident == "false":
        return False
    if scope.locals is not None and ident in scope.locals:
        return scope.locals[ident]      # submodel parameter (ROADMAP 2.3 part B)
    variables = getattr(scope.ctx, "variables", None)
    if variables is not None and ident in variables:
        return variables[ident]
    raise ExpressionError(f"unknown name '{ident}'")


def _apply(func, col, *args):
    """Run an operator or function, turning its failure into an ExpressionError
    so a bad model reports a location instead of a bare TypeError.

    The hot closures below (binary operators, calls of up to three arguments)
    inline this instead of calling it -- a continuous model evaluates tens of
    millions of nodes in a run, and a wrapper call per node was a fifth of the
    engine's time (Bio-4). The cold ones still call it.
    """
    try:
        return func(*args)
    except ExpressionError:
        raise
    except Exception as exc:
        raise ExpressionError(str(exc) or type(exc).__name__, col) from None


# -- build: AST -> closure tree (ROADMAP 2.5) --------------------------------
#
# `_check` already proved *node* is in the allow-list, so `_build` never
# raises ExpressionError itself -- every closure below only ever runs at
# evaluation time, on an already-validated tree, one per distinct expression
# text (cached alongside the rest of `_compile`'s work). What used to be a
# fresh `isinstance` dispatch over every node on every `evaluate()` call is
# now a plain function call per node, decided once here.
#
# Anything decidable at COMPILE time is decided here and never again: which
# reserved name a `Name` node is, how many arguments a call takes, whether a
# `*` can possibly be a sequence repetition.

_SEQUENCES = (str, list, tuple)


def _build(node):
    """Compile *node* into a zero-argument-but-`scope` closure."""
    if isinstance(node, ast.Constant):
        value = node.value
        return lambda scope: value

    if isinstance(node, ast.Name):
        ident = node.id
        if ident in ("TNOW", "now"):
            return lambda scope: _ctx(scope).now
        if ident == "true":
            return lambda scope: True
        if ident == "false":
            return lambda scope: False
        if ident in ("entity", "HORIZON"):
            return lambda scope: _name_value(ident, scope)

        def _lookup(scope):          # a submodel parameter, or a model variable
            local = scope.locals
            if local is not None and ident in local:
                return local[ident]
            variables = getattr(scope.ctx, "variables", None)
            if variables is not None and ident in variables:
                return variables[ident]
            raise ExpressionError(f"unknown name '{ident}'")
        return _lookup

    if isinstance(node, ast.Attribute):
        attr, col = node.attr, node.col_offset
        if _is_agent_call(node.value):
            # Another agent's column (PHASE5 AB-2), by name through the
            # population, never getattr -- the same rule as self.<attr>.
            ref = _build(node.value)

            def _agent_attr(scope):
                population, index = _agents(scope).deref(ref(scope))   # stale: refused (C5)
                return population.read(attr, index)
            return _agent_attr
        if _is_entity_agent(node.value):
            # H1: the entity's own agent, read the same way; `state` reads the
            # state NAME, so `entity.agent.state == "Busy"` needs no special case.
            def _entity_agent_attr(scope):
                population, index = entity_agent(scope, col)
                return population.read(attr, index)
            return _entity_agent_attr
        if node.value.id == "self":
            # An agent's column, read by name (PHASE5 AB-1): never getattr, so
            # the grammar cannot reach anything but declared attributes.
            def _self_attr(scope):
                if scope.agent is None:
                    raise ExpressionError("no agent is available in this context", col)
                population, index = scope.agent
                return population.read(attr, index)
            return _self_attr
        if node.value.id == "sender":
            # PHASE6 A9: the sending agent, a scope local of the `where` probe
            # (as `payload` is of a message transition), read the same way.
            def _sender_attr(scope):
                sender = scope.locals.get("sender") if scope.locals is not None else None
                if sender is None:
                    raise ExpressionError("sender.<attribute> is readable only in an agent's "
                                          "send or remove target's where", col)
                population, index = sender
                return population.read(attr, index)
            return _sender_attr

        def _attr(scope):
            if scope.entity is None:
                raise ExpressionError("no entity is available in this context", col)
            try:
                return getattr(scope.entity, attr)
            except AttributeError:
                raise ExpressionError(f"entity has no attribute '{attr}'", col) from None
        return _attr

    if isinstance(node, ast.BinOp):
        left, right = _build(node.left), _build(node.right)
        op_type = type(node.op)
        op, col = _BIN_OPS[op_type], node.col_offset

        if op_type is ast.Pow:
            def _power(scope):
                lv, rv = left(scope), right(scope)
                if isinstance(rv, (int, float)) and abs(rv) > MAX_EXPONENT:
                    raise ExpressionError(f"exponent {rv} exceeds the limit of {MAX_EXPONENT}", col)
                try:
                    return op(lv, rv)
                except ExpressionError:
                    raise
                except Exception as exc:
                    raise ExpressionError(str(exc) or type(exc).__name__, col) from None
            return _power

        if op_type is ast.Mult:
            def _multiply(scope):
                lv, rv = left(scope), right(scope)
                # Sequence repetition is the only way `*` can blow up memory,
                # and it needs a str/list/tuple operand -- so the arithmetic
                # case pays two isinstance tests and nothing else.
                if isinstance(lv, _SEQUENCES) or isinstance(rv, _SEQUENCES):
                    for sequence, count in ((lv, rv), (rv, lv)):
                        if isinstance(sequence, _SEQUENCES) and isinstance(count, int) \
                                and count > MAX_REPEAT:
                            raise ExpressionError(
                                f"repetition of {count} exceeds the limit of {MAX_REPEAT}", col)
                try:
                    return lv * rv
                except ExpressionError:
                    raise
                except Exception as exc:
                    raise ExpressionError(str(exc) or type(exc).__name__, col) from None
            return _multiply

        def _binop(scope):
            try:
                return op(left(scope), right(scope))
            except ExpressionError:
                raise
            except Exception as exc:
                raise ExpressionError(str(exc) or type(exc).__name__, col) from None
        return _binop

    if isinstance(node, ast.UnaryOp):
        operand = _build(node.operand)
        op, col = _UNARY_OPS[type(node.op)], node.col_offset
        return lambda scope: _apply(op, col, operand(scope))

    if isinstance(node, ast.BoolOp):
        children = [_build(v) for v in node.values]
        if isinstance(node.op, ast.And):
            def _and(scope):
                value = True
                for child in children:      # short-circuits like Python's own `and`
                    value = child(scope)
                    if not value:
                        return value
                return value
            return _and

        def _or(scope):
            value = False
            for child in children:
                value = child(scope)
                if value:
                    return value
            return value
        return _or

    if isinstance(node, ast.Compare):
        left = _build(node.left)
        pairs = [(_COMPARE_OPS[type(op)], _build(comparator))
                 for op, comparator in zip(node.ops, node.comparators)]
        col = node.col_offset

        def _compare(scope):
            lv = left(scope)
            for op, right in pairs:
                rv = right(scope)
                if not _apply(op, col, lv, rv):
                    return False
                lv = rv
            return True
        return _compare

    if isinstance(node, ast.IfExp):
        test, body, orelse = _build(node.test), _build(node.body), _build(node.orelse)
        return lambda scope: body(scope) if test(scope) else orelse(scope)

    if isinstance(node, ast.Tuple):
        elements = [_build(e) for e in node.elts]
        return lambda scope: tuple(e(scope) for e in elements)

    if isinstance(node, ast.List):
        elements = [_build(e) for e in node.elts]
        return lambda scope: [e(scope) for e in elements]

    if isinstance(node, ast.Call) and node.func.id == "IF":
        test, body, orelse = (_build(a) for a in node.args)
        return lambda scope: body(scope) if test(scope) else orelse(scope)

    if isinstance(node, ast.Call):
        func, needs_scope, _, _ = FUNCTIONS[node.func.id]
        args = [_build(a) for a in node.args]
        col = node.col_offset
        # A lookup by a literal name -- TS("price"), FLOWSUM("biogas", 2),
        # FRACTION("Holder", "ch4") -- has nothing left to evaluate, so its
        # arguments are folded here and the call is one function call.
        if node.args and all(isinstance(a, ast.Constant) for a in node.args):
            fixed = tuple(a.value for a in node.args)

            def _call_fixed(scope):
                try:
                    return func(scope, *fixed) if needs_scope else func(*fixed)
                except ExpressionError:
                    raise
                except Exception as exc:
                    raise ExpressionError(str(exc) or type(exc).__name__, col) from None
            if node.func.id != "FLOWSUM" or not isinstance(fixed[0], str):
                return _call_fixed
            # FLOWSUM with a literal tag is the most called node of a plant
            # model and usually answers from the pass memo: read it here, with
            # the key built once, and take the three calls only on a miss. The
            # memo is the RUN's (`LevelSystem._sums`), found through the scope,
            # so nothing about a run is kept on this shared closure.
            key = (fixed[0], fixed[1] if len(fixed) > 1 else None)

            def _flowsum_fixed(scope):
                system = getattr(scope.ctx, "level_system", None)
                if system is not None:
                    total = system._sums.get(key)
                    if total is not None:
                        return total
                return _call_fixed(scope)
            return _flowsum_fixed

        # Arity is known here, so the common calls skip both the argument
        # generator and the `_apply` wrapper. TS/LEVEL/FLOW/FLOWSUM/CONV/
        # FRACTION are all one or two arguments, and a continuous model calls
        # them millions of times per run.
        if len(args) <= 3:
            a0 = args[0] if args else None
            a1 = args[1] if len(args) > 1 else None
            a2 = args[2] if len(args) > 2 else None

            def _call(scope):
                try:
                    if a0 is None:
                        return func(scope) if needs_scope else func()
                    if a1 is None:
                        return func(scope, a0(scope)) if needs_scope else func(a0(scope))
                    if a2 is None:
                        return (func(scope, a0(scope), a1(scope)) if needs_scope
                                else func(a0(scope), a1(scope)))
                    return (func(scope, a0(scope), a1(scope), a2(scope)) if needs_scope
                            else func(a0(scope), a1(scope), a2(scope)))
                except ExpressionError:
                    raise
                except Exception as exc:
                    raise ExpressionError(str(exc) or type(exc).__name__, col) from None
            return _call

        if needs_scope:
            return lambda scope: _apply(func, col, scope, *(a(scope) for a in args))
        return lambda scope: _apply(func, col, *(a(scope) for a in args))

    # _check ran first, so this is a bug in this module, not in the model.
    raise ExpressionError(f"cannot evaluate {type(node).__name__}", getattr(node, "col_offset", None))


# One line per function: what it reads or returns. The expression reference and the agent
# tools' format guide both print it, so a function's meaning is written once, here.
FUNCTION_NOTES: dict[str, str] = {
    "min": "variadic, plain Python", "max": "variadic, plain Python",
    "abs": "plain Python", "round": "1-2 args, plain Python",
    "int": "plain Python", "float": "plain Python",
    "sqrt": "math.sqrt", "exp": "math.exp", "log": "1-2 args, math.log",
    "log10": "math.log10", "floor": "math.floor", "ceil": "math.ceil",
    "RAND": "uniform [0, 1), sampled from the calling block's own rng",
    "EXPO": "exponential(mean), sampled from the calling block's own rng",
    "UNIF": "uniform(low, high), sampled from the calling block's own rng",
    "NORM": "normal(mean, sd), sampled from the calling block's own rng",
    "TRIA": "triangular(low, mode, high), sampled from the calling block's own rng",
    "DISC": "value/prob pairs (>= 1 pair), probs must sum to 1",
    "NQ": "entities waiting for resource/set NAME",
    "NR": "entities/units currently in service at resource/set NAME",
    "MR": "units currently scheduled at resource/set NAME (net of capacity-schedule/failure blockers)",
    "AVAILABLE": "capacity in force at resource NAME, seized units NOT deducted (free units: IDLE)",
    "IDLE": "free units of resource NAME: nothing holds them now (AVAILABLE minus the seized)",
    "POWER": "POWER(\"name\"): kW resource, fleet or charger NAME draws now, from its \"power\" per state; a flow it feeds takes rate_time_unit hours",
    "NS": "items currently in store NAME (a Hold/Batch/Match store, by block or store name)",
    "VAR": "VAR(\"name\"): model variable NAME, for a name a bare name cannot spell",
    "TS": "TS(\"name\"[, t]): time-series input NAME now, or at time t",
    "LEVEL": "LEVEL(\"name\"): the current value of level NAME",
    "FLOW": "FLOW(\"name\"): the rate flow NAME moves right now, after saturation, per its rate_time_unit",
    "FLOWSUM": "FLOWSUM(\"tag\"[, p]): the rates of every flow tagged TAG (priority below p), per their rate_time_unit",
    "MODULO": "MODULO(a, b): the remainder of a / b with the sign of a",
    "ZIDZ": "ZIDZ(a, b): a / b, or 0 when b is 0",
    "XIDZ": "XIDZ(a, b[, x]): a / b, or x (default 0) when b is 0",
    "TABLE": "a value from the model's lookup table NAME, by 1 or 2 keys",
    "TABLE_EXTRAPOLATE": "TABLE_EXTRAPOLATE(\"name\", k1[, k2]): like TABLE, but a linear axis "
                         "continues past its ends along its last slope instead of clamping",
    "CONV": "CONV(\"name\"): the product rate of delayed conversion NAME right now, per its time_unit",
    "FRACTION": "FRACTION(\"name\", \"component\"): the share of blended level (or conversion) NAME "
                "that is COMPONENT",
    "DISTANCE": "DISTANCE(\"a\", \"b\"): shortest path along the path network between two nodes, "
                "stations or aliases",
    # System dynamics (PHASE5 SD-2, ARCHITECTURE 5.24).
    "AUX": "auxiliary NAME, evaluated on read",
    "DELAY": "output of delay NAME",
    "IF": "IF(c, a, b): a when c else b; only the branch taken is evaluated",
    "STEP": "STEP(h, t0): h from t0 on, else 0",
    "PULSE": "PULSE(h, t0, w): h on [t0, t0 + w), else 0",
    "RAMP": "RAMP(s, t0[, t1]): s * (TNOW - t0) after t0, held after t1",
    "SMOOTH": "SMOOTH(x, tau[, init]): first-order information delay (init defaults to x at t0); "
             "needs simulation.sd",
    "SMOOTH3": "SMOOTH3(x, tau[, init]): third-order information delay (init defaults to x at t0); "
             "needs simulation.sd",
    "DELAY1": "DELAY1(x, tau[, init]): first-order material delay (init defaults to x at t0); "
             "needs simulation.sd",
    "DELAY3": "DELAY3(x, tau[, init]): third-order material delay (init defaults to x at t0); "
             "needs simulation.sd",
    "DELAYN": "DELAYN(x, tau, n[, init]): n-th order material delay, n a literal integer "
              "(init defaults to x at t0)",
    "DELAYP": "DELAYP(x, tau[, init]): x exactly tau later (pipeline); no grid needed",
    "SUM": "SUM(x): x summed over every element its \"[*]\" (or subrange \"[North*]\") "
           "indexes run over (subscripts); "
           "SUM(\"pop\", \"attr\"[, \"state\"]): an attribute summed over a population's agents "
           "(in that state)",
    "MEAN": "MEAN(x): SUM(x) divided by the element count (subscripts); MEAN(\"pop\", "
            "\"attr\"[, \"state\"]): an attribute's mean over a population (in that state; 0 if none)",
    "PROD": "PROD(x): x multiplied over every element its \"[*]\" index runs over (subscripts)",
    "VMIN": "VMIN(x): the smallest x over every element its \"[*]\" index runs over (subscripts)",
    "VMAX": "VMAX(x): the largest x over every element its \"[*]\" index runs over (subscripts)",
    "INITIAL": "INITIAL(x): the value x had at t=0, held for the whole run",
    "ACTIVE_INITIAL": "ACTIVE_INITIAL(x, init): x while running, init when initial values are set up",
    "SAMPLE_IF_TRUE": "SAMPLE_IF_TRUE(cond, x, init): the last x read while cond was true, init before "
                      "the first true reading",
    "TREND": "TREND(x, avg_time[, init]): fractional rate of change of x against its "
             "avg_time-smoothed value (init: the starting trend, default 0)",
    "FORECAST": "FORECAST(x, avg_time, horizon): x extrapolated horizon ahead along its TREND",
    "NPV": "NPV(stream, rate, init, factor): present value of a stream discounted at rate, from "
           "init, times factor; needs simulation.sd for the step",
    "NOISE_NORMAL": "NOISE_NORMAL(mean, sd, lo, hi): a normal draw clipped to [lo, hi], redrawn at "
                    "each re-evaluation, fixed per call site and seed",
    # Agents (PHASE5 AB-1, ARCHITECTURE 5.25).
    "COUNT": "COUNT(\"pop\"[, \"state\"][, \"attr\", \"value\"]): a population's live agents, "
             "those in a state (O(1)), or those whose categorical attribute holds that "
             "category",
    "ALIVE": "ALIVE(ref): whether a stored agent reference (AGENT(...), entity.agent) still names "
             "its agent; using one whose agent was removed raises",
    "AGENT": "AGENT(\"pop\", i): a reference to agent i (what entity.agent holds and a send takes); AGENT(\"pop\", i).attr reads its attribute",
    "NEAR": "NEAR(\"pop\", r[, \"state\"]): how many of pop's agents are within r of self "
            "(inclusive), self excluded when pop is self's own population",
    "NEAREST": "NEAREST(\"pop\"[, \"state\"]): distance to the closest OTHER agent of pop "
               "(inf if there is none)",
    # Neighbour aggregates (PHASE6 A3): the same neighbourhood as NEAR/a
    # k-nearest search, reduced over an attribute.
    "NEAR_SUM": "NEAR_SUM(\"pop\", \"attr\", r[, \"state\"]): attr summed over the agents NEAR "
                "counts; 0 if none",
    "NEAR_MEAN": "NEAR_MEAN(\"pop\", \"attr\", r[, \"state\"]): attr's mean over the agents NEAR "
                 "counts; 0 if none",
    "NEAR_MIN": "NEAR_MIN(\"pop\", \"attr\", r[, \"state\"]): attr's minimum over the agents "
                "NEAR counts; NaN if none",
    "NEAR_MAX": "NEAR_MAX(\"pop\", \"attr\", r[, \"state\"]): attr's maximum over the agents "
                "NEAR counts; NaN if none",
    "KNN_SUM": "KNN_SUM(\"pop\", \"attr\", k[, \"state\"]): attr summed over the k nearest "
               "agents of pop to self (state-filtered first, ties by ascending index); 0 if none",
    "KNN_MEAN": "KNN_MEAN(\"pop\", \"attr\", k[, \"state\"]): attr's mean over the k nearest "
                "agents of pop to self (state-filtered first, ties by ascending index); 0 if none",
    "NEAR_HEADING": "NEAR_HEADING(\"pop\", r[, \"state\"]): the circular mean heading of the agents "
                    "NEAR counts; NaN if none",
    # Grid space (PHASE6 A4): self's cell on a grid space.
    "PATCH": "PATCH(\"name\"[, dx, dy]): the patch value at self's grid cell (or an offset "
             "from it); 0 off a bounded grid",
    "NEIGHBOURS": "NEIGHBOURS(\"pop\"[, \"state\"]): pop's agents in the Moore/von Neumann "
                  "cells around self's (own cell excluded)",
    "NEIGHBOUR_SUM": "NEIGHBOUR_SUM(\"pop\", \"attr\"[, \"state\"]): attr summed over the "
                     "agents NEIGHBOURS counts; 0 if none",
    "NEIGHBOUR_MEAN": "NEIGHBOUR_MEAN(\"pop\", \"attr\"[, \"state\"]): attr's mean over the "
                      "agents NEIGHBOURS counts; 0 if none",
    "CELL_COUNT": "CELL_COUNT(\"pop\"[, dx, dy]): pop's agents in self's grid cell (or an "
                  "offset from it), self included; 0 off a bounded grid",
    # Links (PHASE6 A5): self's linked neighbours, all of its own population.
    "LINKED": "LINKED(\"pop\"[, \"state\"]): how many agents self is linked to (pop is "
              "self's own population; directed: its out-links)",
    "LINK_SUM": "LINK_SUM(\"attr\"[, \"state\"]): attr summed over self's linked "
                "neighbours; 0 if none",
    "LINK_MEAN": "LINK_MEAN(\"attr\"[, \"state\"]): attr's mean over self's linked "
                 "neighbours; 0 if none",
    "IS_LINKED": "IS_LINKED(ref): whether self is linked to that agent (directed: self -> it)",
    # Trigonometry (PHASE6 A9): radians, Python's math functions.
    "SIN": "math.sin, radians", "COS": "math.cos, radians",
    "TAN": "math.tan, radians", "ATAN2": "ATAN2(y, x): math.atan2, radians in (-pi, pi]",
    "SQRT": "math.sqrt (the upper-case twin of sqrt)", "PI": "PI(): 3.14159...",
    "DEG": "radians to degrees", "RAD": "degrees to radians: COS(RAD(self.heading))",
    **{name: f"math.{name.lower()}, radians" for name in
       ("ASIN", "ACOS", "ATAN", "SINH", "COSH", "TANH")},
}
