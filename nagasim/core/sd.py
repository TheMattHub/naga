"""System dynamics: authoring classes and the delay expansion (PHASE5 SD-1/SD-2,
decision 6: Python API first).

Each class is a plain record with `to_dict()` (non-default keys only) and
`from_dict()`, and a `SECTION` naming where its JSON goes; `add(layout,
*records)` appends them. The engine never sees these classes: it runs the JSON
(`core/levels.py` owns the runtime `Level`/`Flow`/`LevelSystem`), so a script
and the editor produce the same file.

    from nagasim.api import run_model
    from nagasim.core import sd
    run_model(sd.add({"simulation": {"time_limit": 50}}, sd.SDSettings(0.25, "rk4"),
                     sd.Stock("P", 10),
                     sd.Flow("growth", 'r*LEVEL("P")*(1-LEVEL("P")/K)', target="P")))

DELAYS (SD-2, D92). A `delays` record `{name, input, delay_time, kind, order,
initial}` is read as `DELAY("name")`. `expand(layout)` turns a material or
information delay into HIDDEN levels and flows before anything runs, so the
ordinary level engine integrates it:

* material, order n: stages `<stem>~s1..n`, each drained at `S_i / (tau/n)`
  into the next; `<stem>~f1` feeds the input, `<stem>~out` is the outflow and
  the delay's output; each stage starts at `initial * tau/n`;
* information (SMOOTH), order n: unbounded stages moved by the signed flows
  `<stem>~f<i>` at `(in_i - S_i) / (tau/n)`; the output is the last stage;
* pipeline (DELAYP): no stages -- an exact `(t, value)` history read back at
  `t - tau` by `levels._Pipeline`, so it needs no grid.

`<stem>` is the delay's name, with a trailing `#1` dropped (so a user delay
"f" beside flow f's first inline call is E_DELAY_NAME, DEVLOG #130). The inline
shorthand SMOOTH/SMOOTH3/DELAY1/DELAY3/DELAYN/DELAYP in a flow rate, an
auxiliary or a delay input is rewritten (through `ast` offsets) into
`DELAY("<owner>#k")` plus a generated hidden record, because a compiled
expression is cached by its text and cannot hold per-call-site state: a flow
"f" with one SMOOTH gets delay "f#1" and stages "f~s1..n". An inline call's
`initial` defaults to its input's value at t0, Vensim's rule (DEVLOG #167); a
`delays` record's to 0; DELAYP's to 0 (Vensim's DELAY FIXED always names one).

SUBSCRIPTS (SD-3). `subscripts: [{name, elements}]` declares a dimension; a
level, flow, auxiliary or delay with `dims: [d, ...]` is FLATTENED here into
one scalar record per element, `Pop[north]` (`Pop[north,young]` for two dims),
the D36 precedent: everything downstream sees ordinary records. Per element,
a literal name in LEVEL/FLOW/AUX/DELAY/FRACTION whose index names one of the
owner's dims takes the owner's element (`LEVEL("Pop[region]")`), a flow's
`from`/`to` naming a subscripted level maps element-wise (`"Pop"` or
`"Pop[region]"`), and a one-argument SUM(x)/MEAN(x) is unrolled over every
`[*]` in x outside a nested SUM/MEAN (a `[*]` binds to its innermost one; a
pairwise sum, so 1000 terms nest ten deep, not a thousand).
Same `ast`-offset rewrite as the delay macros. Order in `expand`: subscripts
first, then the inline macros (so `f[north]` gets its own `f[north]#1`), then
the delay stages.

SUBRANGES, MAPPINGS, PER-ELEMENT EQUATIONS (PHASE6 S3). A subscript with
`parent: "Region"` is a subrange (its elements a subset of Region's); one with
`map_to: {dim, elements?}` corresponds element by element to another
dimension (positionally when `elements` is absent, so the sizes must match).
Both only widen the owner's BINDING (`_Flat.bind`): an owner at element e of
dim D also binds D's parent to e, every subrange holding e to e, D's map target
to the mapped element, and a dim mapping onto D to its element when exactly one
maps there -- transitively, the owner's own dims winning. So `Pop[North]`
inside an owner over North, `Pop[Region]` inside one over North, and `X[Zone]`
inside one over Region (Region mapped to Zone) all resolve with no new rewrite.
`[North*]` is a `[*]` over North's elements (usable on a record over the parent:
`SUM(LEVEL("Pop[North*]"))` sums the subset); `[Region*]` equals `[*]`. A
record's `elements: {"n1": expr, "a,x": expr}` replaces its first expression
field (a level's `initial`, a flow's `rate`, an auxiliary's `expr`, a delay's
`input`) for those elements before the rewrite; keys naming no element are left
on the first copy for `E_SUBSCRIPT_ELEMENT`.

Stdlib + `core/expressions` only.
"""
from __future__ import annotations

import ast
import json
import math
from itertools import product
from graphlib import CycleError, TopologicalSorter

from nagasim.core.expressions import (SD_MACROS, SD_VALUE_MACROS, ExpressionError,
                                      compile_expression, lookup_sections)
from nagasim.core.problems import problem
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, to_base
from nagasim.core.timeunits import units_of

__all__ = ["SDSettings", "Stock", "Flow", "Aux", "Delay", "Subscript", "add", "expand",
           "MACROS", "VALUE_MACROS", "AGGREGATES", "DELAY_KINDS", "stem", "output_text",
           "build_auxiliaries", "delay_reads", "reach", "validate_structured", "split_name",
           "subscripts_of", "subscript_links", "related_dims", "leftover_aggregates"]

DELAY_KINDS = ("material", "information", "pipeline", "sample", "noise")
# PHASE6 S2: extra required fields per kind, beyond the shared "name"/"kind" --
# "sample" (SAMPLE_IF_TRUE) and "noise" (NOISE_NORMAL) have no delay_time/order
# (no stages), so they get their own field list instead of "input"/"delay_time".
_DELAY_FIELDS = {"material": ("input", "delay_time"), "information": ("input", "delay_time"),
                 "pipeline": ("input", "delay_time"), "sample": ("condition", "input"),
                 "noise": ("mean", "sd", "lo", "hi")}
# Delay kinds with no stages to integrate: no order, no E_SD_REQUIRES_DT.
_STAGELESS_KINDS = ("pipeline", "sample", "noise")
# Inline shorthand -> (kind, order); DELAYN takes its order as an argument.
MACROS = {name: (kind, order) for name, (_, _, kind, order) in SD_MACROS.items()}
# (section, field) whose expression may hold the inline shorthand.
_OWNERS = (("flows", "rate"), ("auxiliaries", "expr"), ("delays", "input"))
# Subscripts (SD-3): the sections that take `dims`, their expression fields,
# and the lookup -> section a literal name is resolved in.
_EXPR_FIELDS = {"levels": ("initial",), "flows": ("rate", "price", "emission_factor"),
                "auxiliaries": ("expr",), "delays": ("input", "delay_time", "initial")}
_LOOKUP_SECTION = lookup_sections("LEVEL", "FRACTION", "FLOW", "AUX", "DELAY")
AGGREGATES = ("SUM", "MEAN", "PROD", "VMIN", "VMAX")
# PHASE6 S2: INITIAL/ACTIVE_INITIAL/SAMPLE_IF_TRUE/TREND/FORECAST/NPV/
# NOISE_NORMAL -- a BROADER owner set than the delay shorthand's `_OWNERS`
# (a level's own "initial" too, since INITIAL/ACTIVE_INITIAL matter most
# there): every (section, field) `_EXPR_FIELDS` already lists.
VALUE_MACROS = SD_VALUE_MACROS
_VALUE_OWNERS = tuple((section, field) for section, fields in _EXPR_FIELDS.items()
                      for field in fields)


class _Record:
    SECTION = ""
    # (attribute, JSON key, default): what `to_dict` writes when it differs.
    _FIELDS: tuple = ()

    def to_dict(self) -> dict:
        out = {key: getattr(self, attr) for attr, key, default in self._FIELDS
               if getattr(self, attr) != default or key == "name"}
        out.update(getattr(self, "extra", {}))
        return out

    @classmethod
    def from_dict(cls, data: dict):
        data = dict(data)
        kwargs = {attr: data.pop(key) for attr, key, _default in cls._FIELDS if key in data}
        return cls(**kwargs, **data)

    def __eq__(self, other):
        return type(self) is type(other) and self.to_dict() == other.to_dict()

    def __repr__(self):
        return f"{type(self).__name__}({self.to_dict()!r})"


class SDSettings(_Record):
    """`simulation.sd`: the step grid (D89) and the integration method."""

    SECTION = "simulation"
    _FIELDS = (("dt", "dt", None), ("method", "method", "euler"),
               ("time_unit", "time_unit", "base"))

    def __init__(self, dt: float, method: str = "euler", time_unit: str = "base"):
        self.dt, self.method, self.time_unit = dt, method, time_unit


class Stock(_Record):
    """A stock is a level (`levels` section). `min=None` is unbounded below;
    `initial` may be an expression (SD-2); `conveyor={"transit_time": ...}`
    makes it a Stella conveyor (PHASE6 C1); any other level key
    (`thresholds`, `max_outflow`, ...) passes through."""

    SECTION = "levels"
    _FIELDS = (("name", "name", None), ("initial", "initial", 0.0),
               ("capacity", "capacity", None), ("min", "min", 0.0), ("unit", "unit", ""),
               ("dims", "dims", []), ("conveyor", "conveyor", None))

    def __init__(self, name: str, initial: float | str = 0.0, capacity: float | None = None,
                 min: float | None = 0.0, unit: str = "", dims=(), conveyor: dict | None = None,
                 **level_fields):
        self.name, self.initial, self.capacity, self.min, self.unit = (
            name, initial, capacity, min, unit)
        self.dims = list(dims)
        self.conveyor = conveyor
        self.extra = level_fields


class Flow(_Record):
    """A rate between stocks (`flows` section); None for `source`/`target` is a
    cloud outside the model. `biflow=True` lets the rate go negative. Out of a
    conveyor (PHASE6 C1) the rate is "" and `leak={"fraction": ..., "zone":
    [a, b]}` marks a leakage flow; the one flow without it is the primary."""

    SECTION = "flows"
    _FIELDS = (("name", "name", None), ("rate", "rate", None), ("source", "from", None),
               ("target", "to", None), ("biflow", "biflow", False), ("dims", "dims", []),
               ("leak", "leak", None))

    def __init__(self, name: str, rate: str, source: str | None = None,
                 target: str | None = None, biflow: bool = False, dims=(),
                 leak: dict | None = None, **flow_fields):
        self.name, self.rate, self.source, self.target, self.biflow = (
            name, str(rate), source, target, biflow)
        self.dims = list(dims)
        self.leak = leak
        self.extra = flow_fields


class Aux(_Record):
    """An auxiliary (`auxiliaries` section, SD-2): a named expression read as
    `AUX("name")`, evaluated on read. Other keys (`diagram`, ...) pass through."""

    SECTION = "auxiliaries"
    _FIELDS = (("name", "name", None), ("expr", "expr", None), ("unit", "unit", ""),
               ("dims", "dims", []))

    def __init__(self, name: str, expr: str, unit: str = "", dims=(), **aux_fields):
        self.name, self.expr, self.unit = name, str(expr), unit
        self.dims = list(dims)
        self.extra = aux_fields


class Delay(_Record):
    """A delay (`delays` section, SD-2, PHASE6 S2), read as `DELAY("name")`:
    `kind` is material, information (SMOOTH), pipeline (DELAYP), sample
    (SAMPLE_IF_TRUE -- pass `condition` and `input`, no `delay_time`) or noise
    (NOISE_NORMAL -- pass `mean`/`sd`/`lo`/`hi` as extra fields, no `input`/
    `delay_time`); `order` the number of stages; `initial` the output at t=0
    (0 by default, ignored by "noise", which has none)."""

    SECTION = "delays"
    _FIELDS = (("name", "name", None), ("input", "input", None),
               ("delay_time", "delay_time", None), ("kind", "kind", "material"),
               ("order", "order", 1), ("initial", "initial", 0.0), ("unit", "unit", ""),
               ("dims", "dims", []))

    def __init__(self, name: str, input: str | None = None, delay_time: float | str | None = None,
                 kind: str = "material", order: int = 1, initial: float | str = 0.0,
                 unit: str = "", dims=(), **delay_fields):
        self.name = name
        self.input = None if input is None else str(input)
        self.delay_time = delay_time
        self.kind, self.order, self.initial, self.unit = kind, order, initial, unit
        self.dims = list(dims)
        self.extra = delay_fields


class Subscript(_Record):
    """A dimension (`subscripts` section, SD-3): records with `dims=[name]`
    are flattened into one scalar record per element, `Pop[north]`.
    `parent` makes it a subrange; `map_to` ("Zone" or {"dim": "Zone",
    "elements": [...]}) maps it element-wise onto another dim (PHASE6 S3)."""

    SECTION = "subscripts"
    _FIELDS = (("name", "name", None), ("elements", "elements", None),
               ("parent", "parent", None), ("map_to", "map_to", None))

    def __init__(self, name: str, elements, parent: str | None = None, map_to=None, **fields):
        self.name, self.elements, self.parent = name, [str(e) for e in elements], parent
        self.map_to = {"dim": map_to} if isinstance(map_to, str) else map_to
        self.extra = fields


def add(layout: dict, *records) -> dict:
    """Append *records* to their sections of *layout* (in place) and return it,
    so a script can write `run_model(sd.add({...}, ...))`."""
    for record in records:
        if isinstance(record, SDSettings):
            layout.setdefault("simulation", {})["sd"] = record.to_dict()
        else:
            layout.setdefault(record.SECTION, []).append(record.to_dict())
    return layout


# ---------------------------------------------------------------- expansion
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _has_macro(text) -> bool:
    return isinstance(text, str) and any(name in text for name in MACROS)


def _has_value_macro(text) -> bool:
    return isinstance(text, str) and any(name in text for name in VALUE_MACROS)


def stem(name: str) -> str:
    """What a delay's hidden records are named after: its name, with a
    trailing `#1` dropped, so the first inline macro of flow "f" has stages
    `f~s1..n` and a second one `f#2~s1..n`."""
    return name[:-2] if name.endswith("#1") else name


def _q(name: str) -> str:
    return json.dumps(name)


def _parts(delay: dict):
    """(stage names, `(tau/n)` text) of a material/information delay, or None
    when the record is not expandable (validation reports why)."""
    kind, order = delay.get("kind", "material"), delay.get("order", 1)
    if kind not in ("material", "information") or not _is_int(order) or order < 1:
        return None
    if delay.get("input") in (None, "") or delay.get("delay_time") in (None, ""):
        return None
    base = stem(str(delay.get("name", "")))
    return ([f"{base}~s{j}" for j in range(1, order + 1)],
            f"(({delay['delay_time']}) / {order})")


def output_text(delay: dict) -> str | None:
    """The expression DELAY("name") reads for a material/information delay."""
    parts = _parts(delay)
    if parts is None:
        return None
    stages, step = parts
    if delay.get("kind", "material") == "material":
        return f"LEVEL({_q(stages[-1])}) / {step}"
    return f"LEVEL({_q(stages[-1])})"


def _stage_records(delay: dict):
    """The hidden levels and flows of a material/information delay."""
    stages, step = _parts(delay)
    extra = {"hidden": True}
    if delay.get("locals"):
        extra["locals"] = dict(delay["locals"])
    source = f"({delay['input']})"
    initial = delay.get("initial", 0.0)
    number = isinstance(initial, (int, float)) and not isinstance(initial, bool)
    if delay.get("kind", "material") == "material":
        # Unbounded, two-way stages: a negative input passes through (DEVLOG #169).
        start = 0.0 if number and initial == 0 else f"({initial}) * {step}"
        levels = [{"name": s, "initial": start, "min": None, **extra} for s in stages]
        extra = {**extra, "biflow": True}
        flows = [{"name": f"{stem(delay['name'])}~f1", "rate": source, "to": stages[0], **extra}]
        flows += [{"name": f"{stem(delay['name'])}~f{j + 1}",
                   "rate": f"LEVEL({_q(stages[j - 1])}) / {step}",
                   "from": stages[j - 1], "to": stages[j], **extra} for j in range(1, len(stages))]
        flows.append({"name": f"{stem(delay['name'])}~out",
                      "rate": f"LEVEL({_q(stages[-1])}) / {step}", "from": stages[-1], **extra})
        return levels, flows
    start = float(initial) if number else f"({initial})"
    levels = [{"name": s, "initial": start, "min": None, **extra} for s in stages]
    flows = []
    for j, stage in enumerate(stages):
        upstream = source if j == 0 else f"LEVEL({_q(stages[j - 1])})"
        flows.append({"name": f"{stem(delay['name'])}~f{j + 1}", "biflow": True, "to": stage,
                      "rate": f"({upstream} - LEVEL({_q(stage)})) / {step}", **extra})
    return levels, flows


def _macro_record(node, text: str, name: str, locals_) -> dict:
    kind, order = MACROS[node.func.id]
    args = [ast.get_source_segment(text, arg) for arg in node.args]
    record = {"name": name, "input": args[0], "delay_time": args[1], "kind": kind,
              "hidden": True}
    rest = args[2:]
    if node.func.id == "DELAYN":
        literal = node.args[2]
        value = literal.value if isinstance(literal, ast.Constant) else None
        if isinstance(value, float) and value.is_integer():
            value = int(value)          # "3.0": the importers write every number as a float
        order = value if _is_int(value) else rest[0]  # else E_SD_DELAY_ORDER says so
        rest = rest[1:]
    if kind != "pipeline":
        record["order"] = order
    if rest:
        record["initial"] = rest[0]
    elif kind != "pipeline":
        # DEVLOG #167: Vensim SMOOTH/SMOOTH3/DELAY1/DELAY3 and XMILE SMTH1/SMTH3/
        # DELAY1/DELAY3/DELAYN start in equilibrium, at the input's t0 value --
        # the level-initial pass evaluates it (#160/#161 machinery).
        record["initial"] = args[0]
    if locals_:
        record["locals"] = dict(locals_)
    return record


def _inline(text: str, owner: str, locals_, counter: dict, delays: list) -> str:
    """*text* with every outermost inline delay call replaced by
    `DELAY("<owner>#k")`, each appending its generated record to *delays*.
    A nested call stays inside the generated record's input and is expanded
    when that record's turn comes."""
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return text     # E_EXPRESSION reports it
    calls = []

    def visit(node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in MACROS and not node.keywords):
            low, high, _, _ = SD_MACROS[node.func.id]
            if low <= len(node.args) <= high:       # a wrong arity is E_EXPRESSION's
                calls.append(node)
                return
        for child in ast.iter_child_nodes(node):
            visit(child)
    visit(tree.body)
    edits = []
    for node in sorted(calls, key=lambda n: (n.lineno, n.col_offset)):
        name = _next_name(owner, counter)
        delays.append(_macro_record(node, text, name, locals_))
        edits.append((node, f"DELAY({_q(name)})"))
    return _splice(text, edits)


def _next_name(owner: str, counter: dict) -> str:
    """The next `<owner>#k` hidden-record name, k counted per owner (shared
    across the value-macro and the delay-shorthand passes below, so a flow
    that uses both never collides)."""
    counter[owner] = counter.get(owner, 0) + 1
    return f"{owner}#{counter[owner]}"


def _value_macro(name: str, args: list, locals_, owner: str, counter: dict, delays: list,
                 levels: list, flows: list, in_level_initial: bool, dt: float = 0.0) -> str:
    """One VALUE_MACROS call's replacement text, appending whatever hidden
    record(s) it needs (PHASE6 S2). *args* are the call's own argument SOURCE
    TEXT (already extracted by `_inline_values`), so nesting one of these
    macros inside another's own argument is NOT expanded here -- only inside a
    delay-shorthand macro's argument, or the reverse, which the shared
    *delays* list and the existing re-scan in `expand` still catch. Put an
    inner VALUE_MACROS call in an AUX first if that nesting is needed."""
    extra = {"hidden": True, **({"locals": dict(locals_)} if locals_ else {})}

    if name == "ACTIVE_INITIAL":
        # Vensim: X while the model runs, INIT during initialisation. Our own
        # "initialisation" IS a level's `initial` topological-order pass
        # (SD-2) -- everywhere else (a flow rate, an auxiliary, a delay field)
        # runs "during the model", so this is a pure substitution, no record.
        x, init = args
        return f"({init if in_level_initial else x})"

    if name == "INITIAL":
        # The value of x at t=0, held: a hidden level whose OWN initial is x,
        # with no flow ever touching it -- `_start_initials` (SD-2) evaluates
        # it once, in the same dependency order as any other level, and it
        # then never changes. Exact whenever x itself is (any level initial
        # expression's own rule: E_LEVEL_INITIAL_CYCLE if it depends on itself).
        (x,) = args
        level_name = _next_name(owner, counter)
        levels.append({"name": level_name, "initial": x, "min": None, **extra})
        return f'LEVEL({_q(level_name)})'

    if name == "SAMPLE_IF_TRUE":
        # Holds the last x read while cond was true, init before the first
        # true reading: a "sample" delay (`core/levels.py: _Sampler`), updated
        # at every level-engine re-evaluation -- event, or grid point with
        # `simulation.sd` -- exactly like a pipeline's `record()`. EXACT AT
        # those instants; cond's truth between them is a zero-order hold, the
        # same granularity a self-discharge LEVEL() rate already lives with.
        cond, x, init = args
        delay_name = _next_name(owner, counter)
        delays.append({"name": delay_name, "kind": "sample", "condition": cond, "input": x,
                       "initial": init, **extra})
        return f'DELAY({_q(delay_name)})'

    if name in ("TREND", "FORECAST"):
        # Vensim: TREND(x, t, init) = (x - SMOOTH(x, t)) / (t * SMOOTH(x, t)),
        # SMOOTH's own initial value chosen so TREND's initial reading is
        # `init` (0 when FORECAST calls this, since it exposes no init_trend
        # of its own): smooth_initial = x / (1 + init*t). Reuses the ordinary
        # information-delay (SMOOTH) machinery -- no new runtime code.
        x, avg_time = args[0], args[1]
        init_trend = args[2] if name == "TREND" and len(args) > 2 else "0"
        delay_name = _next_name(owner, counter)
        delays.append({"name": delay_name, "kind": "information", "order": 1, "input": x,
                       "delay_time": avg_time,
                       "initial": f"({x}) / (1 + ({init_trend}) * ({avg_time}))", **extra})
        smooth = f'DELAY({_q(delay_name)})'
        trend = f"(({x}) - {smooth}) / (({avg_time}) * {smooth})"
        if name == "TREND":
            return f"({trend})"
        horizon = args[2]                          # FORECAST(x, avg_time, horizon)
        return f"(({x}) * (1 + ({trend}) * ({horizon})))"

    if name == "NPV":
        # Vensim's present value (DEVLOG #159), as two hidden levels: DF
        # decays from 1 at rate/(1 + rate*dt); CUM accumulates stream*DF
        # from `init`; NPV = (CUM + stream*dt*DF) * factor, dt the SD grid
        # step (0 without `simulation.sd`: the continuous limit). Like any
        # LEVEL()-driven rate, exact at events and on the grid.
        stream, rate, init, factor = args
        base = _next_name(owner, counter)
        df_name, cum_name = f"{base}~df", f"{base}~cum"
        levels.append({"name": df_name, "initial": 1.0, "min": None, **extra})
        levels.append({"name": cum_name, "initial": init, "min": None, **extra})
        flows.append({"name": f"{base}~df_decay", "from": df_name,
                      "rate": f'LEVEL({_q(df_name)}) * ({rate}) / (1 + ({rate}) * {dt!r})',
                      **extra})
        flows.append({"name": f"{base}~cum_acc", "to": cum_name, "biflow": True,
                      "rate": f'({stream}) * LEVEL({_q(df_name)})', **extra})
        return (f'((LEVEL({_q(cum_name)}) + ({stream}) * {dt!r} * LEVEL({_q(df_name)}))'
                f' * ({factor}))')

    if name == "NOISE_NORMAL":
        # A normal draw clipped to [lo, hi], redrawn once per level-engine
        # re-evaluation from a stream seeded once per call site (a "noise"
        # delay, `core/levels.py: _Noise`) -- CRN-safe (the same model and
        # seed draw the same noise regardless of what else runs). RK4 refuses
        # it (E_SD_RK4_RANDOM precedent): four stage re-evaluations would draw
        # four times per step. Deferred: coloured/pink noise (white only).
        mean, sd_expr, lo, hi = args
        delay_name = _next_name(owner, counter)
        delays.append({"name": delay_name, "kind": "noise", "mean": mean, "sd": sd_expr,
                       "lo": lo, "hi": hi, **extra})
        return f'DELAY({_q(delay_name)})'

    raise AssertionError(f"unhandled value macro {name!r}")   # VALUE_MACROS is exhaustive


def _inline_values(text: str, owner: str, locals_, counter: dict, delays: list, levels: list,
                   flows: list, in_level_initial: bool, dt: float = 0.0) -> str:
    """*text* with every OUTERMOST VALUE_MACROS call replaced (PHASE6 S2):
    mirrors `_inline`'s AST scan/splice, but each macro builds its own
    replacement text and hidden record(s) (`_value_macro`) instead of a
    uniform DELAY() rewrite."""
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return text     # E_EXPRESSION reports it
    calls = []

    def visit(node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in VALUE_MACROS and not node.keywords):
            low, high = VALUE_MACROS[node.func.id]
            if low <= len(node.args) <= high:        # a wrong arity is E_EXPRESSION's
                calls.append(node)
                return
        for child in ast.iter_child_nodes(node):
            visit(child)
    visit(tree.body)
    edits = []
    for node in sorted(calls, key=lambda n: (n.lineno, n.col_offset)):
        args = [ast.get_source_segment(text, arg) for arg in node.args]
        replacement = _value_macro(node.func.id, args, locals_, owner, counter, delays, levels,
                                   flows, in_level_initial, dt)
        edits.append((node, replacement))
    return _splice(text, edits)


def _splice(text: str, edits) -> str:
    """*text* with each (ast node, replacement) put in place of the node's
    source; the nodes must not overlap. ast offsets are UTF-8 byte columns."""
    if not edits:
        return text
    raw = text.encode("utf-8")
    starts = [0]
    for line in raw.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    pieces, cursor = [], 0
    for node, replacement in sorted(edits, key=lambda e: (e[0].lineno, e[0].col_offset)):
        pieces += [raw[cursor:starts[node.lineno - 1] + node.col_offset],
                   replacement.encode("utf-8")]
        cursor = starts[node.end_lineno - 1] + node.end_col_offset
    pieces.append(raw[cursor:])
    return b"".join(pieces).decode("utf-8")


# ---------------------------------------------------------------- subscripts
def split_name(name):
    """("Pop", ["north", "young"]) for "Pop[north, young]"; (name, None) for a
    name without an index."""
    if not (isinstance(name, str) and name.endswith("]") and "[" in name):
        return name, None
    base, _, index = name[:-1].partition("[")
    return base, [token.strip() for token in index.split(",")]


def _element_name(base: str, elements) -> str:
    return f"{base}[{','.join(elements)}]"


def subscripts_of(layout: dict) -> dict:
    """{dimension: [elements]} of the well-formed `subscripts` entries (the
    first of a duplicate wins; validation reports the rest)."""
    out = {}
    for entry in layout.get("subscripts") or []:
        if not isinstance(entry, dict):
            continue
        name, elements = entry.get("name"), entry.get("elements")
        if isinstance(name, str) and name not in out and isinstance(elements, list) and elements:
            out[name] = [str(e) for e in elements]
    return out


def _map_target(entry: dict, subs: dict):
    """(target dim, [target element per own element]) of a well-formed
    `map_to`, else None (validation says why)."""
    spec, name = entry.get("map_to"), entry.get("name")
    dim = spec.get("dim") if isinstance(spec, dict) else None
    if not isinstance(dim, str) or dim not in subs or dim == name:
        return None
    given = spec.get("elements")
    targets = [str(e) for e in given] if isinstance(given, list) else list(subs[dim])
    if len(targets) != len(subs[name]) or not set(targets) <= set(subs[dim]):
        return None
    return dim, targets


def subscript_links(layout: dict) -> tuple[dict, dict]:
    """({subrange: parent}, {dim: (target dim, [target element per element])})
    of the well-formed `parent`/`map_to` entries (PHASE6 S3)."""
    subs = subscripts_of(layout)
    parent, maps = {}, {}
    for entry in layout.get("subscripts") or []:
        if not isinstance(entry, dict) or entry.get("name") not in subs:
            continue
        name, up = entry["name"], entry.get("parent")
        if isinstance(up, str) and up in subs and up != name and set(subs[name]) <= set(subs[up]):
            parent[name] = up
        target = _map_target(entry, subs)
        if target:
            maps[name] = target
    return parent, maps


def related_dims(layout: dict, dims) -> set:
    """*dims* plus every dim a subrange/parent/mapping link reaches from them,
    transitively: the dims an owner over *dims* MAY bind (a subrange only for
    the elements it holds -- `_Flat.bind` decides per element). The importers'
    static check (PHASE6 S3)."""
    parent, maps = subscript_links(layout)
    edges = [(a, b) for a, b in parent.items()] + [(a, t) for a, (t, _) in maps.items()]
    out, queue = set(dims), list(dims)
    for dim in queue:
        for a, b in edges:
            for other in (b if a == dim else None, a if b == dim else None):
                if other and other not in out:
                    out.add(other)
                    queue.append(other)
    return out


# DEVLOG #163: the most records one subscripted record may flatten into --
# 1000x1000 took 8.6 s and 367 MB; a cap keeps a hostile file from exhausting memory.
MAX_SUBSCRIPT_ELEMENTS = 200_000


def _too_big(dims, subs: dict) -> bool:
    return math.prod(len(subs[d]) for d in dims) > MAX_SUBSCRIPT_ELEMENTS


def _dims_ok(dims, subs: dict) -> bool:
    return (isinstance(dims, list) and bool(dims)
            and all(isinstance(d, str) and d in subs for d in dims)
            and len(set(dims)) == len(dims))


def _lookup(node):
    """The section a literal-name lookup call reads (LEVEL("x") -> levels)."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return _LOOKUP_SECTION.get(node.func.id)
    return None


def _is_aggregate(node) -> bool:
    """A one-argument SUM(x)/MEAN(x): the form `_Flat.rewrite` unrolls."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in AGGREGATES and len(node.args) == 1 and not node.keywords)


def _pairwise(parts: list, op: str = "+") -> str:
    """Pairwise-reduced text with *op* ("+" for SUM, "*" for PROD): depth
    log2(n), so a long SUM/PROD stays far inside the recursion limit;
    left-associative up to three terms, like `a + b + c`."""
    if len(parts) == 1:
        return f"({parts[0]})"
    half = (len(parts) + 1) // 2
    return f"({_pairwise(parts[:half], op)} {op} {_pairwise(parts[half:], op)})"


class _Flat:
    """One flattening pass: the dimensions, and per section the dims of each
    subscripted base name."""

    def __init__(self, layout: dict):
        self.subs = subscripts_of(layout)
        self.parent, self.maps = subscript_links(layout)
        self.dims = {section: {r["name"]: r["dims"] for r in layout.get(section) or []
                               if isinstance(r, dict) and isinstance(r.get("name"), str)
                               and _dims_ok(r.get("dims"), self.subs)}
                     for section in _EXPR_FIELDS}

    def bind(self, dims, combo) -> dict:
        """The owner's binding for one element *combo* of *dims*, widened
        through the subrange and mapping links (PHASE6 S3, module docstring);
        the first binding of a dim wins, so the owner's own dims always do."""
        binding = dict(zip(dims, combo))
        if not (self.parent or self.maps):
            return binding
        queue = list(binding.items())
        for dim, element in queue:          # grows while it is walked
            found = [(sub, element) for sub, up in self.parent.items()
                     if up == dim and element in self.subs[sub]]
            if dim in self.parent:
                found.append((self.parent[dim], element))
            if dim in self.maps:
                target, targets = self.maps[dim]
                found.append((target, targets[self.subs[dim].index(element)]))
            found += [(source, self.subs[source][targets.index(element)])
                      for source, (target, targets) in self.maps.items()
                      if target == dim and targets.count(element) == 1]
            for other, value in found:
                if other not in binding:
                    binding[other] = value
                    queue.append((other, value))
        return binding

    def _star_dim(self, token: str, dims, position: int):
        """The dim a `*` (the position's own dim) or `D*` (D's elements) runs
        over, or None."""
        if token == "*":
            return dims[position] if dims and position < len(dims) else None
        if token.endswith("*") and token[:-1] in self.subs:
            return token[:-1]
        return None

    def resolve(self, name: str, section: str, binding: dict, star=None,
                implicit=False) -> str:
        """*name* with each index naming a bound dimension replaced by its
        element, and each `*` by the element *star* binds for that position's
        dimension. With *implicit*, a bare subscripted base maps element-wise
        (a flow's `from`/`to`). Anything unresolved is left for validation."""
        base, tokens = split_name(name)
        dims = self.dims[section].get(base)
        if tokens is None:
            if implicit and dims and all(d in binding for d in dims):
                return _element_name(base, [binding[d] for d in dims])
            return name
        out = []
        for position, token in enumerate(tokens):
            over = self._star_dim(token, dims, position)
            if token in binding:
                token = binding[token]
            elif star and over in star:
                token = star[over]
            out.append(token)
        return name if out == tokens else _element_name(base, out)

    def starred(self, node) -> list:
        """The dimensions the `[*]` indexes under *node* run over, in order. A
        nested SUM/MEAN is not entered: its `[*]` are its own, and `rewrite`
        unrolls it per outer term (DEVLOG #124)."""
        found, queue = [], [node]
        for call in queue:          # breadth first, the order ast.walk gave
            if _is_aggregate(call):
                continue
            queue.extend(ast.iter_child_nodes(call))
            section = _lookup(call)
            if section is None:
                continue
            base, tokens = split_name(call.args[0].value)
            dims = self.dims[section].get(base) or ()
            for position, token in enumerate(tokens or ()):
                over = self._star_dim(token, dims, position)
                if over and over not in found:
                    found.append(over)
        return found

    def rewrite(self, text: str, binding: dict, star=None) -> str:
        """*text* for one element: literal names resolved, a one-argument
        SUM/MEAN over `[*]` unrolled. A syntax error is left to E_EXPRESSION."""
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError:
            return text
        edits = []

        def visit(node):
            if _is_aggregate(node):
                over = self.starred(node.args[0])
                if over:
                    arg = ast.get_source_segment(text, node.args[0])
                    parts = [self.rewrite(arg, binding, {**(star or {}), **dict(zip(over, combo))})
                             for combo in product(*(self.subs[d] for d in over))]
                    # SUM/PROD: a pairwise +/* text (depth log2(n)); MEAN divides
                    # the sum; VMIN/VMAX: one variadic min()/max() call -- flat,
                    # not nested, so it has no recursion-depth concern at all.
                    name = node.func.id
                    if name in ("VMIN", "VMAX"):
                        fn = "min" if name == "VMIN" else "max"
                        edits.append((node, f"{fn}({', '.join(parts)})"))
                    elif name == "PROD":
                        edits.append((node, _pairwise(parts, "*")))
                    else:
                        total = _pairwise(parts)
                        edits.append((node, total if name == "SUM" else f"({total} / {len(parts)})"))
                    return
            section = _lookup(node)
            if section is not None:
                name = node.args[0].value
                new = self.resolve(name, section, binding, star)
                if new != name:
                    edits.append((node.args[0], _q(new)))
            for child in ast.iter_child_nodes(node):
                visit(child)
        visit(tree.body)
        return _splice(text, edits)

    def element(self, record: dict, section: str, binding: dict, elements=None) -> dict:
        """One scalar copy of *record* for *binding* (a scalar record is
        rewritten too: it may hold a SUM over `[*]`)."""
        new = {key: value for key, value in record.items() if key != "dims"}
        if elements is not None:
            new["name"] = _element_name(str(record.get("name", "")), elements)
        for field in _EXPR_FIELDS[section]:
            if isinstance(new.get(field), str):
                new[field] = self.rewrite(new[field], binding)
        if section == "flows":
            if isinstance(new.get("components"), dict):
                new["components"] = {key: self.rewrite(value, binding) if isinstance(value, str)
                                     else value for key, value in new["components"].items()}
            for side in ("from", "to"):
                if isinstance(new.get(side), str) and new[side]:
                    new[side] = self.resolve(new[side], "levels", binding, implicit=True)
        return new


def _flatten(layout: dict) -> dict:
    """The layout with every subscripted record replaced by its elements, in
    element order (the first dimension slowest). Pure; a record with unknown
    dims is kept as it is, for `validate_structured` to report."""
    flat = _Flat(layout)
    out = dict(layout)
    for section in _EXPR_FIELDS:
        if section not in layout:
            continue
        records = []
        for record in layout.get(section) or []:
            if not isinstance(record, dict):
                records.append(record)
                continue
            dims = record.get("dims")
            if not dims:
                records.append(flat.element(record, section, {}))
            elif section == "levels" and record.get("conveyor"):
                # C2: a conveyor's own fields (transit_time, capacity, ...) are
                # never rewritten per element, so per-element copies would be
                # identical belts sharing one unrewritten expression -- kept
                # unflattened, "dims" left on, for E_CONVEYOR_WRITE to catch.
                records.append(record)
            elif not _dims_ok(dims, flat.subs) or _too_big(dims, flat.subs):
                records.append(record)          # E_SUBSCRIPT_UNKNOWN/_DIM/_SIZE
            else:
                records += _per_element(flat, record, section, dims)
        out[section] = records
    return out


def _per_element(flat: _Flat, record: dict, section: str, dims) -> list:
    """One copy per element of *dims*; an `elements` override (PHASE6 S3)
    replaces the section's first expression field for its element. Keys that
    match no element stay on the first copy for E_SUBSCRIPT_ELEMENT."""
    overrides = record.get("elements")
    if "elements" in record:
        record = {key: value for key, value in record.items() if key != "elements"}
    leftover = overrides
    if isinstance(overrides, dict):
        leftover = {",".join(t.strip() for t in str(key).split(",")): value
                    for key, value in overrides.items()}
    field = _EXPR_FIELDS[section][0]
    copies = []
    for combo in product(*(flat.subs[d] for d in dims)):
        key, source = ",".join(combo), record
        if isinstance(leftover, dict) and key in leftover:
            source = {**record, field: leftover.pop(key)}
        copies.append(flat.element(source, section, flat.bind(dims, combo), combo))
    if leftover:
        copies[0]["elements"] = leftover
    return copies


def leftover_aggregates(text: str) -> list:
    """(name, argument count) of every SUM/MEAN call still in *text* after
    `expand`: one outside the SD sections, one without a `[*]`, or the
    two-argument agent form (Phase 5B)."""
    try:
        tree = ast.parse(str(text), mode="eval")
    except SyntaxError:
        return []
    return [(node.func.id, len(node.args)) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in AGGREGATES]


def _needs_expand(layout: dict) -> bool:
    """Whether `expand` has anything to do: an existing delay, or a delay-
    shorthand/VALUE_MACROS call sitting in a field either pass reads."""
    if layout.get("delays"):
        return True
    owners = _OWNERS[:2] + _VALUE_OWNERS
    return any(isinstance(record, dict) and (_has_macro(record.get(field))
                                             or _has_value_macro(record.get(field)))
              for section, field in owners for record in layout.get(section) or [])


def _grid_dt(layout: dict) -> float:
    """`simulation.sd.dt` in the model's base unit (NPV's TIME STEP), 0.0
    without a grid or when the settings are malformed (validation's)."""
    sim = layout.get("simulation") or {}
    spec = sim.get("sd") or {}
    try:
        fields = SECTION_FIELDS["simulation.sd"]
        return float(spec["dt"]) * field_factor(param(fields, "dt"), spec, units_of(sim), fields)
    except (KeyError, TypeError, ValueError):
        return 0.0


def expand(layout: dict) -> dict:
    """The layout with every delay and SD function turned into what the engine
    runs (SD-2, PHASE6 S2): inline shorthand rewritten into generated `delays`
    records, INITIAL/ACTIVE_INITIAL/SAMPLE_IF_TRUE/TREND/FORECAST/NPV/
    NOISE_NORMAL rewritten into hidden levels/delays (`_value_macro`), and each
    material/information delay's hidden levels and flows appended.

    Subscripted records are flattened first (SD-3, `_flatten`). Order: value
    macros (a BROADER owner set -- a level's own "initial" too), then the
    delay shorthand (`_OWNERS`), then delay staging -- so a delay-shorthand
    call left inside a value macro's own argument (say TREND's `x`) is still
    caught, since it stays in the SAME field text the delay-shorthand pass
    re-scans; the reverse nesting, or one value macro inside another's own
    argument, is NOT expanded in one pass (put the inner call in an AUX
    first) -- see `_value_macro`.

    Pure (the input is not modified) and idempotent; a layout with no delay,
    no subscript and no inline call is returned unchanged -- the same object
    -- so every non-SD model pays one scan. Runs after `submodels.flatten`."""
    if layout.get("subscripts"):
        layout = _flatten(layout)
    if not _needs_expand(layout):
        return layout
    out = dict(layout)
    counter: dict = {}
    delays = [dict(d) for d in layout.get("delays") or [] if isinstance(d, dict)]
    # Units spec (D159): a delay_time in its own unit converts to base once, here.
    units, fields = units_of(out.get("simulation")), SECTION_FIELDS["delays"]
    for record in delays:
        if record.get("delay_time") not in (None, ""):
            record["delay_time"] = to_base(param(fields, "delay_time"), record, units, fields)
    extra_levels: list = []
    extra_flows: list = []
    dt = _grid_dt(layout)
    for section, field in _VALUE_OWNERS:
        records = delays if section == "delays" else (out.get(section) or [])
        new_records = []
        for record in records:
            if isinstance(record, dict) and _has_value_macro(record.get(field)):
                if section == "auxiliaries" and "ACTIVE_INITIAL" in record[field]:
                    # DEVLOG #160: the aux's `init` twin, read by the
                    # level-initial pass (`levels._start_initials`) instead of expr.
                    record = {**record, "initial": _inline_values(
                        record[field], str(record.get("name", "")), record.get("locals"),
                        counter, delays, extra_levels, extra_flows, True, dt)}
                record = {**record, field: _inline_values(
                    record[field], str(record.get("name", "")), record.get("locals"), counter,
                    delays, extra_levels, extra_flows,
                    section == "levels" and field == "initial", dt)}
            new_records.append(record)
        if section == "delays":
            delays[:] = new_records
        else:
            out[section] = new_records
    for section, field in _OWNERS[:2]:
        records = []
        for record in out.get(section) or []:
            if isinstance(record, dict) and _has_macro(record.get(field)):
                record = {**record, field: _inline(record[field], str(record.get("name", "")),
                                                   record.get("locals"), counter, delays)}
            records.append(record)
        out[section] = records
    index = 0
    while index < len(delays):          # generated inputs may nest further calls
        delay = delays[index]
        if _has_macro(delay.get("input")):
            text = _inline(delay["input"], str(delay.get("name", "")), delay.get("locals"),
                           counter, delays)
            delays[index] = {**delay, "input": text}
            if delay.get("initial") == delay["input"]:      # #167's default follows the input
                delays[index]["initial"] = text
        index += 1
    out["delays"] = delays
    levels = list(out.get("levels") or []) + extra_levels
    flows = list(out.get("flows") or []) + extra_flows
    taken = {level.get("name") for level in levels if isinstance(level, dict)}
    for delay in delays:
        parts = _parts(delay)
        if parts is None or parts[0][0] in taken:       # pipeline, invalid, or expanded already
            continue
        new_levels, new_flows = _stage_records(delay)
        taken.update(level["name"] for level in new_levels)     # a stem clash: E_DELAY_NAME
        levels += new_levels
        flows += new_flows
    out["levels"], out["flows"] = levels, flows
    return out


# ---------------------------------------------------------------- reading
def build_auxiliaries(layout: dict) -> dict:
    """{name: (compiled expression, locals or None)} -- what AUX() reads
    (`ctx.auxiliaries`). Validation has already compiled every expression."""
    return {str(entry["name"]): (compile_expression(str(entry.get("expr", "0"))),
                                 dict(entry["locals"]) if entry.get("locals") else None)
            for entry in layout.get("auxiliaries") or []}


def delay_reads(delays) -> dict:
    """{delay name: [the expressions its output reads]}, for dependency
    walks (`reach`): the last stage of a material/information delay, the input
    and initial of a pipeline, the condition/input/initial of a "sample"
    (SAMPLE_IF_TRUE, PHASE6 S2) or the mean/sd/lo/hi of a "noise"
    (NOISE_NORMAL) -- none of these have an `output_text` stage to read
    instead. Unparseable text is skipped (validation)."""
    out = {}
    for delay in delays or []:
        if not isinstance(delay, dict):
            continue
        kind = delay.get("kind", "material")
        if kind == "pipeline":
            texts = [delay.get("input"), delay.get("initial")]
        elif kind == "sample":
            texts = [delay.get("condition"), delay.get("input"), delay.get("initial")]
        elif kind == "noise":
            texts = [delay.get("mean"), delay.get("sd"), delay.get("lo"), delay.get("hi")]
        else:
            texts = [output_text(delay)]
        found = []
        for text in texts:
            if text in (None, ""):
                continue
            try:
                found.append(compile_expression(str(text)))
            except ExpressionError:
                pass
        out[str(delay.get("name", ""))] = found
    return out


def reach(expression, auxes: dict, delays: dict | None = None,
          flows: dict | None = None) -> list:
    """*expression* plus every auxiliary (and, with *delays*/*flows*, delay
    output or flow rate) expression it reads, transitively: an AUX that reads LEVEL makes its reader dynamic
    (D91), and a level's initial expression depends on what its AUX reads. A
    computed AUX()/DELAY() name reaches all of them. Cycle-safe."""
    out, stack, seen = [], [expression], set()
    while stack:
        current = stack.pop()
        out.append(current)
        for func, table in (("AUX", auxes), ("DELAY", delays), ("FLOW", flows)):
            if not table:
                continue
            names = table if func in current.computed_refs else current.refs.get(func, ())
            for name in names:
                if (func, name) in seen or name not in table:
                    continue
                seen.add((func, name))
                found = table[name]
                stack.extend(found if isinstance(found, list) else [found])
    return out


# ---------------------------------------------------------------- validation
def _compiled(text):
    try:
        return compile_expression(str(text))
    except ExpressionError:
        return None     # E_EXPRESSION, from schema's expression checks


def _cycle(graph: dict):
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        return exc.args[1]
    return None


def _subscript_problems(layout: dict) -> list:
    """The `subscripts` section, and what flattening left unresolved (SD-3):
    `dims` naming no subscript, a `[dim]`/`[*]` index no element replaced, a
    flow still joining a subscripted base."""
    problems: list = []
    subs = subscripts_of(layout)
    seen, elements_seen = set(), {}
    for index, entry in enumerate(layout.get("subscripts") or []):
        at = f"/subscripts/{index}"
        name = entry.get("name") if isinstance(entry, dict) else None
        hint = "{\"name\": \"region\", \"elements\": [\"north\", \"south\"]}"
        if not isinstance(name, str) or not name.isidentifier():
            problems.append(problem("E_SUBSCRIPT_NAME", f"subscript: name must be an "
                                    f"identifier, got {name!r}", f"{at}/name", hint=hint))
            continue
        if name in seen:
            problems.append(problem("E_SUBSCRIPT_NAME", f"duplicate subscript name: {name}",
                                    f"{at}/name", hint="rename one of the two"))
        seen.add(name)
        elements = entry.get("elements")
        if not isinstance(elements, list) or not elements:
            problems.append(problem("E_SUBSCRIPT_NAME", f"subscript '{name}': \"elements\" "
                                    f"must be a non-empty list", f"{at}/elements", hint=hint))
            continue
        texts = [str(e) for e in elements]
        bad = [e for e, text in zip(elements, texts)
               if isinstance(e, bool) or not isinstance(e, (str, int)) or not text.strip()
               or text != text.strip() or any(c in text for c in "[],*")]
        if bad or len(set(texts)) < len(texts):
            problems.append(problem(
                "E_SUBSCRIPT_NAME", f"subscript '{name}': elements must be unique, non-empty "
                f"and free of [ ] , * and surrounding spaces", f"{at}/elements", hint=hint))
        for text in texts:
            elements_seen.setdefault(text, at)
        up = entry.get("parent")
        if up is not None and (up == name or up not in subs
                               or not set(texts) <= set(subs[up])):
            problems.append(problem(
                "E_SUBSCRIPT_RANGE", f"subscript '{name}': parent {up!r} is not another "
                f"declared subscript holding all of its elements", f"{at}/parent",
                hint="{\"name\": \"North\", \"elements\": [\"n1\"], \"parent\": \"Region\"}"))
        if entry.get("map_to") is not None and name in subs and _map_target(entry, subs) is None:
            problems.append(problem(
                "E_SUBSCRIPT_MAP", f"subscript '{name}': map_to must name another declared "
                f"subscript, with one of its elements per element of '{name}' (or the same "
                f"size, mapped by position)", f"{at}/map_to",
                hint="{\"dim\": \"Zone\"} or {\"dim\": \"Zone\", \"elements\": [\"z2\", \"z1\"]}"))
    for text in sorted(set(elements_seen) & set(subs)):
        problems.append(problem(
            "E_SUBSCRIPT_NAME", f"'{text}' is both a subscript and an element: an index "
            f"naming it would be ambiguous", f"{elements_seen[text]}/elements",
            hint="rename the element or the subscript"))

    level_names = {entry.get("name") for entry in layout.get("levels") or []
                   if isinstance(entry, dict)}
    bases = {split_name(name)[0] for name in level_names if split_name(name)[1] is not None}
    for section, fields in _EXPR_FIELDS.items():
        for index, entry in enumerate(layout.get(section) or []):
            if not isinstance(entry, dict) or entry.get("hidden"):
                continue
            at, who = f"/{section}/{index}", f"{section[:-1]} '{entry.get('name', '')}'"
            dims = entry.get("dims")
            if "elements" in entry and not dims:     # what `_per_element` could not place
                found = entry["elements"]
                problems.append(problem(
                    "E_SUBSCRIPT_ELEMENT", f"{who}: \"elements\" "
                    + (f"{', '.join(map(str, found))} match no element of its \"dims\""
                       if isinstance(found, dict) else "must be an object"),
                    f"{at}/elements",
                    hint="give the record \"dims\" and key each override by an element: "
                         "{\"north\": \"2\", \"a,x\": \"3\"}"))
            if dims and _dims_ok(dims, subs) and _too_big(dims, subs):
                problems.append(problem(
                    "E_SUBSCRIPT_SIZE", f"{who}: {math.prod(len(subs[d]) for d in dims):,} "
                    f"elements, over the {MAX_SUBSCRIPT_ELEMENTS:,} cap", f"{at}/dims",
                    hint="use fewer or smaller dimensions"))
            if dims and not _dims_ok(dims, subs):
                if (isinstance(dims, list) and all(isinstance(d, str) and d in subs for d in dims)):
                    problems.append(problem("E_SUBSCRIPT_DIM", f"{who}: \"dims\" repeats a "
                                            f"subscript", f"{at}/dims",
                                            hint="each dimension once"))
                else:
                    unknown = [d for d in dims if d not in subs] if isinstance(dims, list) else [dims]
                    problems.append(problem(
                        "E_SUBSCRIPT_UNKNOWN", f"{who}: unknown subscript(s) "
                        f"{', '.join(map(str, unknown))}", f"{at}/dims",
                        hint=f"declare it under \"subscripts\"; declared: "
                             f"{', '.join(sorted(subs)) or '(none)'}"))
            if not subs:
                continue
            for field in fields:
                text = entry.get(field)
                expression = _compiled(text) if isinstance(text, str) and text else None
                for func in ("LEVEL", "FLOW", "AUX", "DELAY"):
                    for name in (expression.refs.get(func, ()) if expression else ()):
                        tokens = split_name(name)[1] or ()
                        stars = [t for t in tokens if t == "*" or (t[-1:] == "*" and t[:-1] in subs)]
                        loose = stars + [t for t in tokens if t in subs]
                        if loose:
                            problems.append(problem(
                                "E_SUBSCRIPT_DIM", f"{who}: {func}(\"{name}\"): "
                                f"{', '.join(loose)} " + ("sits outside SUM/MEAN" if loose == stars
                                                          else "is not a dimension of this record"),
                                f"{at}/{field}",
                                hint="give the record the same \"dims\", or name one element: "
                                     "LEVEL(\"Pop[north]\")"))
            if section != "flows":
                continue
            for side in ("from", "to"):
                end = entry.get(side)
                if not isinstance(end, str) or end in level_names:
                    continue
                base, tokens = split_name(end)
                if (tokens is None and end in bases) or any(t.endswith("*") or t in subs
                                                            for t in tokens or ()):
                    problems.append(problem(
                        "E_SUBSCRIPT_DIM", f"{who}: \"{side}\" {end} is subscripted, the flow "
                        f"is not (or not over the same dims)", f"{at}/{side}",
                        hint="give the flow the level's \"dims\" to map it element by element"))
    return problems


def validate_structured(layout: dict) -> tuple[list, set, set]:
    """The `subscripts` (SD-3), `auxiliaries` and `delays` sections and the
    level-initial order (SD-2), on the EXPANDED layout. Returns (problems, aux names, delay
    names); the expressions themselves are compiled by `core/schema/`."""
    problems: list = _subscript_problems(layout)
    auxes: dict = {}
    for index, entry in enumerate(layout.get("auxiliaries") or []):
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_AUX_NAME", "auxiliary: name must not be empty",
                                    f"/auxiliaries/{index}/name",
                                    hint="{\"name\": \"birth_rate\", \"expr\": \"0.02\"}"))
            continue
        if name in auxes:
            problems.append(problem("E_AUX_NAME", f"duplicate auxiliary name: {name}",
                                    f"/auxiliaries/{index}/name", hint="rename one of the two"))
        auxes[name] = _compiled(entry.get("expr", ""))
    graph = {name: {ref for ref in expr.refs.get("AUX", ()) if ref in auxes}
             for name, expr in auxes.items() if expr is not None}
    cycle = _cycle(graph)
    if cycle:
        problems.append(problem("E_AUX_CYCLE", f"auxiliaries read each other in a cycle: "
                                f"{' -> '.join(cycle)}", "/auxiliaries",
                                hint="an auxiliary is evaluated on read; break the loop with a "
                                     "level (a stock) or a delay"))

    delays: set = set()
    stems: dict = {}
    has_dt = bool((layout.get("simulation") or {}).get("sd"))
    for index, entry in enumerate(layout.get("delays") or []):
        at = f"/delays/{index}"
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_DELAY_NAME", "delay: name must not be empty", f"{at}/name",
                                    hint="give every delay a unique name"))
            continue
        if name in delays:
            problems.append(problem("E_DELAY_NAME", f"duplicate delay name: {name}",
                                    f"{at}/name", hint="rename one of the two"))
        elif _parts(entry) is not None and stem(name) in stems:     # "f" and inline "f#1", #130
            problems.append(problem(
                "E_DELAY_NAME", f"delays '{stems[stem(name)]}' and '{name}' would both name "
                f"their hidden stages '{stem(name)}~s1..'", f"{at}/name",
                hint="rename the delay: 'f#1' is the first inline SMOOTH/DELAY call in 'f'"))
        delays.add(name)
        if _parts(entry) is not None:       # a staged (material/information) delay
            stems.setdefault(stem(name), name)
        kind = entry.get("kind", "material")
        if kind not in DELAY_KINDS:
            problems.append(problem("E_DELAY_SPEC", f"delay '{name}': unknown kind '{kind}'",
                                    f"{at}/kind", hint=f"use one of: {', '.join(DELAY_KINDS)}"))
            continue
        for field in _DELAY_FIELDS[kind]:
            if entry.get(field) in (None, ""):
                problems.append(problem("E_DELAY_SPEC", f"delay '{name}': \"{field}\" is missing",
                                        f"{at}/{field}", hint="a number or an expression"))
        tau = entry.get("delay_time")
        if isinstance(tau, (int, float)) and not isinstance(tau, bool) and tau <= 0:
            problems.append(problem("E_DELAY_SPEC", f"delay '{name}': delay_time must be > 0",
                                    f"{at}/delay_time", hint="in the model's base time unit"))
        if kind in _STAGELESS_KINDS:
            continue
        order = entry.get("order", 1)
        if not _is_int(order) or order < 1:
            problems.append(problem(
                "E_SD_DELAY_ORDER", f"delay '{name}': order must be a literal integer >= 1, "
                f"got {order!r}", f"{at}/order",
                hint="the number of stages is fixed when the model is built: DELAYN(x, tau, 3)"))
        if not has_dt:
            problems.append(problem(
                "E_SD_REQUIRES_DT", f"delay '{name}': a {kind} delay integrates hidden stocks and "
                f"needs the system-dynamics grid", at,
                hint="add \"simulation\": {\"sd\": {\"dt\": 0.25}}, or use a pipeline delay "
                     "(DELAYP), which is exact without one"))

    # Level initial expressions are evaluated once, in dependency order.
    initials = {str(entry.get("name")): _compiled(entry["initial"])
                for entry in layout.get("levels") or []
                if isinstance(entry, dict) and isinstance(entry.get("initial"), str)}
    if initials:
        exprs = {name: expr for name, expr in auxes.items() if expr is not None}
        reads = delay_reads(layout.get("delays"))
        graph = {name: {ref for found in reach(expr, exprs, reads)
                        for ref in found.refs.get("LEVEL", ()) if ref in initials}
                 for name, expr in initials.items() if expr is not None}
        cycle = _cycle(graph)
        if cycle:
            problems.append(problem(
                "E_LEVEL_INITIAL_CYCLE",
                f"level initial values depend on each other in a cycle: {' -> '.join(cycle)}",
                "/levels", hint="give one of them a number"))
    return problems, set(auxes), delays

