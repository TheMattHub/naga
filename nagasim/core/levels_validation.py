"""Structural validation for the "levels"/"flows" sections and the settings
that ride along with them (`simulation.sd`, conveyors, thresholds, components).
Split out of `core/levels.py` (wave E3, PHASE6) so the runtime module (levels,
flows, saturation, the SimPy process) and the schema-time checks are two
files; `core/schema/layout.py` calls `validate_levels_structured` long before
any of this runs.
"""
from __future__ import annotations

import math

from nagasim.core import sd
from nagasim.core.expressions import ExpressionError, bind_constants, compile_expression
from nagasim.core.levels import (DEFAULT_BLEND_TOLERANCE, SD_METHODS, THRESHOLD_DIRECTIONS,
                                  _BUS_CALLS, _CLOCK_NAMES, _SAMPLER_CALLS, _half_up, is_dynamic)
from nagasim.core.problems import problem
from nagasim.core.timeunits import UNITS, ModelUnits, base_factor
from nagasim.core.utils import is_number as _is_number

__all__ = ["validate_levels_structured", "LEAK_KINDS"]


def validate_levels_structured(layout: dict) -> tuple[list, set, set]:
    """Check the "levels" and "flows" sections. Returns (list[Problem],
    {level names}, {flow names}) -- the names `core/schema/` checks every
    literal `LEVEL()`/`FLOW()` call and every TRANSFER block against."""
    problems: list = []
    levels: set = set()
    unbounded: set = set()      # levels a biflow may join (SD-1)
    for index, entry in enumerate(layout.get("levels", []) or []):
        at = f"/levels/{index}"
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_LEVEL_NAME", "level: name must not be empty",
                                    f"{at}/name", hint="give every level a unique name"))
            continue
        if name in levels:
            problems.append(problem("E_LEVEL_NAME", f"duplicate level name: {name}",
                                    f"{at}/name", hint="rename one of the two levels"))
        levels.add(name)
        if all(entry.get(key) is None
               for key in ("min", "capacity", "max_inflow", "max_outflow")) and "min" in entry:
            unbounded.add(name)

        capacity, minimum = entry.get("capacity"), entry.get("min", 0.0)
        if minimum is None:
            minimum = -math.inf
        initial = entry.get("initial", 0.0) or 0.0
        for field in ("capacity", "min", "initial", "max_inflow", "max_outflow"):
            value = entry.get(field)
            if field == "initial" and isinstance(value, str):
                continue        # an expression (SD-2), compiled by core/schema/
            if value is not None and not _is_number(value):
                problems.append(problem("E_LEVEL_BOUNDS",
                                        f"level '{name}': \"{field}\" must be a number",
                                        f"{at}/{field}", hint="leave it out for no limit"))
        if capacity is not None and _is_number(capacity) and capacity < minimum:
            problems.append(problem("E_LEVEL_BOUNDS",
                                    f"level '{name}': capacity {capacity} is below min {minimum}",
                                    f"{at}/capacity", hint="capacity is the upper bound"))
        elif _is_number(initial) and (initial < minimum
                                      or (capacity is not None and _is_number(capacity)
                                          and initial > capacity)):
            problems.append(problem(
                "E_LEVEL_BOUNDS",
                f"level '{name}': initial {initial} is outside [{minimum}, "
                f"{'inf' if capacity is None else capacity}]",
                f"{at}/initial", hint="a run starts inside the level's own bounds"))
        _check_thresholds(problems, f"level '{name}'", entry.get("thresholds"),
                          f"{at}/thresholds", "E_LEVEL_THRESHOLD")
        _check_components(problems, name, entry, at)

    flows: set = set()
    for index, entry in enumerate(layout.get("flows", []) or []):
        at = f"/flows/{index}"
        name = entry.get("name", "")
        if not isinstance(name, str) or not name.strip():
            problems.append(problem("E_FLOW_NAME", "flow: name must not be empty",
                                    f"{at}/name", hint="give every flow a unique name"))
            continue
        if name in flows:
            problems.append(problem("E_FLOW_NAME", f"duplicate flow name: {name}",
                                    f"{at}/name", hint="rename one of the two flows"))
        flows.add(name)
        for side in ("from", "to"):
            end = entry.get(side)
            if end not in (None, "") and str(end) not in levels:
                problems.append(problem(
                    "E_FLOW_LEVEL", f"flow '{name}': \"{side}\" names no level: {end}",
                    f"{at}/{side}",
                    hint=f"declared levels: {', '.join(sorted(levels)) or '(none)'}; "
                         f"leave it out for a source or sink outside the model"))
        biflow = entry.get("biflow", False)
        if not isinstance(biflow, bool):
            problems.append(problem("E_FLOW_BIFLOW", f"flow '{name}': \"biflow\" must be true "
                                    f"or false", f"{at}/biflow"))
        elif biflow:
            bounded = [str(end) for end in (entry.get("from"), entry.get("to"))
                       if end not in (None, "") and str(end) in levels
                       and str(end) not in unbounded]
            if bounded:
                problems.append(problem(
                    "E_FLOW_BIFLOW",
                    f"flow '{name}': a bi-directional flow needs unbounded ends, but "
                    f"{', '.join(bounded)} has a bound",
                    f"{at}/biflow",
                    hint="give that level \"min\": null and no capacity or power limit, or "
                         "split the flow into two one-way flows"))
        if entry.get("tag") is not None and not isinstance(entry.get("tag"), str):
            problems.append(problem("E_FLOW_TAG", f"flow '{name}': \"tag\" must be a string",
                                    f"{at}/tag",
                                    hint="flows sharing a tag are summed by FLOWSUM(\"tag\")"))
        if entry.get("priority") is not None and not _is_number(entry.get("priority")):
            problems.append(problem("E_FLOW_TAG", f"flow '{name}': \"priority\" must be a number",
                                    f"{at}/priority",
                                    hint="lower dispatches first; FLOWSUM(\"tag\", p) sums the "
                                         "flows ahead of priority p"))
        peak_price = entry.get("peak_price")
        if peak_price is not None and not _is_number(peak_price):
            problems.append(problem("E_FLOW_PEAK_PRICE",
                                    f"flow '{name}': \"peak_price\" must be a number",
                                    f"{at}/peak_price",
                                    hint="money per unit of Peak Rate, billed once at the end "
                                         "of the run; a time-varying tariff is \"price\""))
        efficiency = entry.get("efficiency", 1.0)
        if efficiency is not None and (not _is_number(efficiency)
                                       or not 0 < float(efficiency) <= 1):
            problems.append(problem("E_FLOW_EFFICIENCY",
                                    f"flow '{name}': efficiency must be in (0, 1]",
                                    f"{at}/efficiency",
                                    hint="out = in * efficiency; 1 means lossless"))
        min_rate = entry.get("min_rate")
        if min_rate is not None and (not _is_number(min_rate) or float(min_rate) < 0):
            problems.append(problem("E_FLOW_MIN_RATE",
                                    f"flow '{name}': \"min_rate\" must be a number >= 0",
                                    f"{at}/min_rate",
                                    hint="semi-continuous equipment: below this rate the flow is "
                                         "0, never something in between"))
        components = entry.get("components")
        if components is not None and not isinstance(components, dict):
            problems.append(problem(
                "E_FLOW_COMPONENT",
                f"flow '{name}': \"components\" must be an object of component -> share",
                f"{at}/components",
                hint="{\"ch4\": 0.52} or {\"ch4\": \"FRACTION(\\\"Digester\\\", \\\"ch4\\\")\"}; "
                     "leave it out to carry the source level's own blend"))
        _check_thresholds(problems, f"flow '{name}'", entry.get("thresholds"),
                          f"{at}/thresholds", "E_FLOW_THRESHOLD")
    _check_sd(problems, layout)
    _check_clock_rates(problems, layout)
    _check_conveyors(problems, layout)
    _check_rate_units(problems, layout)
    return problems, levels, flows


def _check_rate_units(problems: list, layout: dict) -> None:
    """FLOW(), FLOWSUM() and CONV() give a rate per the flow's or conversion's own
    time unit (#678), so a tag must have one unit, and a rate or feed that reads
    one in another unit takes it unconverted: an error and a warning."""
    units = ModelUnits((layout.get("simulation") or {}).get("time_unit") or "minutes")

    def per(unit):
        try:
            return base_factor("time", unit or "base", units)
        except (ValueError, KeyError):
            return None                 # an unknown unit is reported elsewhere

    hours = per("hours")
    flows = [e for e in layout.get("flows") or [] if isinstance(e, dict)]
    flow_per = {str(e.get("name")): per(e.get("rate_time_unit")) for e in flows}
    conv_per = {str(e.get("name")): per(e.get("time_unit"))
                for e in layout.get("conversions") or [] if isinstance(e, dict)}
    tags: dict = {}
    for index, entry in enumerate(flows):
        if entry.get("tag") and isinstance(entry.get("tag"), str):
            tags.setdefault(entry["tag"], []).append((index, entry))
    tag_per = {}
    for tag, members in tags.items():
        pers = {flow_per[str(e.get("name"))] for _, e in members} - {None}
        if len(pers) == 1:
            tag_per[tag] = pers.pop()
        if len(pers) < 2:
            continue
        first = flow_per[str(members[0][1].get("name"))]
        odd = next(i for i, e in members if flow_per[str(e.get("name"))] != first)
        listed = ", ".join(f"{e.get('name')}: {e.get('rate_time_unit') or 'base'}"
                           for _, e in members)
        problems.append(problem(
            "E_FLOW_TAG", f"flows tagged '{tag}' have different rate_time_units ({listed})",
            f"/flows/{odd}/rate_time_unit",
            hint=f"set the same \"rate_time_unit\" on every flow tagged '{tag}' (the Energy "
                 f"library uses \"hours\")"))

    def check(text, own, who, at, constants=None):
        if own is None:
            return
        try:
            # A `$`-instance's parameters fold in, as the run does (`bind_constants`):
            # FLOWSUM("bio_feed" + circuit) reads the literal tag it always was (#690).
            expression = bind_constants(str(text), None,
                                        constants if isinstance(constants, dict) else {})
        except ExpressionError:
            return                      # reported as E_EXPRESSION by the expression checks
        reads = [(f'FLOW("{n}")', flow_per.get(n)) for n in expression.refs.get("FLOW", ())]
        reads += [(f'CONV("{n}")', conv_per.get(n)) for n in expression.refs.get("CONV", ())]
        reads += [(f'FLOWSUM("{n}")', tag_per.get(n)) for n in expression.refs.get("FLOWSUM", ())]
        computed = [(f"{call}()", pers) for call, pers in (
            ("FLOW", flow_per), ("CONV", conv_per), ("FLOWSUM", tag_per))
            if call in expression.computed_refs]
        if "POWER" in expression.calls:         # kW: a rate per hour
            reads.append(("POWER()", hours))
        # D606: a computed name may read any record of its kind. One time unit
        # among them is checked as a literal; several that differ from this rate's
        # cannot be checked, which is said -- conservatively: the name may only
        # ever read records in this rate's unit.
        for call, pers in computed:
            units_read = set(pers.values()) - {None}
            if len(units_read) == 1:
                unit = units_read.pop()
                if unit not in {other for _call, other in reads}:     # said once
                    reads.append((f"{call} (a computed name)", unit))
            elif units_read - {own}:
                problems.append(problem(
                    "W_RATE_UNIT_UNCHECKED",
                    f"{who}: {call} reads a computed name, and those it may read are per "
                    f"different time units, so the read cannot be unit-checked", at,
                    hint="write the name as a literal, or give every one the same time unit"))
        for call, other in reads:
            if other is not None and other != own:
                problems.append(problem(
                    "W_RATE_UNIT_READ",
                    f"{who}: {call} is per another time unit than this rate, and is read "
                    f"unconverted", at,
                    hint="give both the same time unit, or convert in the expression"))

    # #683: a 0.10.0 change notice. A model from 0.9.x whose rates are written per another
    # unit than the base one now prices, thresholds and reports them per that unit. A
    # submodel instance's record (`<instance>/name`) is left out: a library asset wrote its
    # numbers per its own unit, which is how 0.10.0 now reads them.
    base = (layout.get("simulation") or {}).get("time_unit") or "minutes"
    for index, entry in enumerate(flows):
        unit = entry.get("rate_time_unit")
        if "/" not in str(entry.get("name")) and flow_per[str(entry.get("name"))] not in (
                None, 1.0) and (
                entry.get("peak_price") or entry.get("thresholds")):     # 0 bills nothing
            problems.append(problem(
                "W_RATE_UNIT_CHANGED",
                f"flow '{entry.get('name')}': since 0.10.0 this flow's peak_price/thresholds "
                f"and reported rates are per {unit}, not per {base}; check the numbers",
                f"/flows/{index}/rate_time_unit",
                hint="a 0.9.x model gave them per the base time unit"))
    for index, entry in enumerate(layout.get("conversions") or []):
        if isinstance(entry, dict) and "/" not in str(entry.get("name"))                 and conv_per.get(str(entry.get("name"))) not in (None, 1.0):
            problems.append(problem(
                "W_RATE_UNIT_CHANGED",
                f"conversion '{entry.get('name')}': since 0.10.0 its CONV() and reported "
                f"rates are per {entry.get('time_unit')}, not per {base}; check the numbers",
                f"/conversions/{index}/time_unit",
                hint="a 0.9.x model gave them per the base time unit"))
    for index, entry in enumerate(flows):
        check(entry.get("rate") or "0", flow_per[str(entry.get("name"))],
              f"flow '{entry.get('name')}'", f"/flows/{index}/rate", entry.get("locals"))
    for c_index, entry in enumerate(layout.get("conversions") or []):
        if not isinstance(entry, dict):
            continue
        for s_index, spec in enumerate(entry.get("substrates") or []):
            if isinstance(spec, dict):
                check(spec.get("feed", "0"), per(spec.get("feed_time_unit")),
                      f"conversion '{entry.get('name')}', substrate '{spec.get('name')}'",
                      f"/conversions/{c_index}/substrates/{s_index}/feed", entry.get("locals"))


LEAK_KINDS = ("linear", "exponential")


def _check_clock_rates(problems: list, layout: dict) -> None:
    """DEVLOG #410: a rate is re-evaluated only at events, so one that reads TNOW changes
    between them only on the SD grid. Without one the clock is read at t=0 and a timed
    pump never switches (no error, a plausible wrong answer). LEVEL() reads are fine: a
    level change is itself an event."""
    if (layout.get("simulation") or {}).get("sd") is not None:
        return
    auxes = {}      # no grid, so no auxiliaries to follow (they need one)
    for index, entry in enumerate(layout.get("flows", []) or []):
        try:
            expression = compile_expression(str(entry.get("rate", "0")), None)
        except ExpressionError:
            continue        # reported as E_EXPRESSION by the expression checks
        if any(_CLOCK_NAMES & e.names for e in sd.reach(expression, auxes)):
            problems.append(problem(
                "W_FLOW_CLOCK_NO_GRID",
                f"flow '{entry.get('name', '')}': the rate reads TNOW but the model has no "
                f"step grid, so it is evaluated only at events and the clock is stale between "
                f"them", f"/flows/{index}/rate",
                hint="a rate that switches on a timetable is exact read from a profile: "
                     "TS(\"name\") of a timeseries_inputs entry with interpolation \"step\" "
                     "and beyond \"repeat\". Otherwise add \"simulation\": {\"sd\": "
                     "{\"dt\": ...}} (set_sd_settings): each switch is then late by up to dt"))


def _check_conveyors(problems: list, layout: dict) -> None:
    """Conveyor stocks and their leak flows (C1): the SD grid under Euler,
    a transit time, exactly one primary outflow with no rate of its own, and
    leaks only on a conveyor's outflows with a zone inside [0, 1]. C2: a
    subscripted conveyor is refused (`_flatten` leaves "dims" on it, since it
    never rewrites a conveyor's own fields per element)."""
    belts = {}
    for index, entry in enumerate(layout.get("levels") or []):
        if isinstance(entry, dict) and entry.get("conveyor") is not None:
            name = str(entry.get("name", ""))
            belts[name] = (index, entry["conveyor"])
            if entry.get("dims"):
                problems.append(problem(
                    "E_CONVEYOR_WRITE", f"conveyor '{name}': a subscripted conveyor is not "
                    f"supported (its transit_time/capacity/in_limit/arrest are never rewritten "
                    f"per element)", f"/levels/{index}/dims",
                    hint="give each element its own conveyor level instead of a \"dims\" array"))
            # DEVLOG #242: `build_level_system` drops these on a conveyor. A
            # min of 0 is kept quiet: the editor grid writes it, and it holds.
            ignored = [key for key in ("capacity", "min", "max_inflow", "max_outflow")
                       if entry.get(key) not in (None, "") and not (key == "min" and entry[key] == 0)]
            if ignored:
                problems.append(problem(
                    "W_CONVEYOR_LEVEL_KEY", f"conveyor '{name}': "
                    f"{', '.join(ignored)} is ignored on a conveyor", f"/levels/{index}/{ignored[0]}",
                    hint="use conveyor.capacity (content) and conveyor.in_limit (inflow per "
                         "time unit) instead"))
    spec = (layout.get("simulation") or {}).get("sd")
    slats = {}      # belt -> slat count, for a literal transit only
    for name, (index, belt) in belts.items():
        at = f"/levels/{index}/conveyor"
        if not isinstance(belt, dict) or belt.get("transit_time") in (None, ""):
            problems.append(problem("E_CONVEYOR_SPEC", f"conveyor '{name}': needs a "
                                    f"\"transit_time\"", at,
                                    hint="{\"transit_time\": 5} -- a number or an expression"))
            continue
        if belt.get("leak_kind", "linear") not in LEAK_KINDS:
            problems.append(problem("E_CONVEYOR_SPEC", f"conveyor '{name}': unknown leak_kind "
                                    f"'{belt.get('leak_kind')}'", f"{at}/leak_kind",
                                    hint=f"use one of: {', '.join(LEAK_KINDS)}"))
        if not isinstance(spec, dict):
            problems.append(problem("E_CONVEYOR_GRID", f"conveyor '{name}': needs the "
                                    f"system-dynamics grid", at,
                                    hint="add \"simulation\": {\"sd\": {\"dt\": 0.25}}"))
            continue
        if spec.get("method", "euler") == "rk4":
            problems.append(problem("E_CONVEYOR_METHOD", f"conveyor '{name}': runs one slat per "
                                    f"DT, so it needs method \"euler\"", "/simulation/sd/method",
                                    hint="use \"method\": \"euler\""))
        transit, dt = belt.get("transit_time"), spec.get("dt")
        try:
            transit = float(transit)
        except (TypeError, ValueError):
            continue        # an expression: latched per batch at run time
        if _is_number(dt) and dt > 0:
            slats[name] = max(1, _half_up(transit / dt))
        if _is_number(dt) and dt > 0 and abs(transit / dt - round(transit / dt)) > 1e-9:
            problems.append(problem(
                "W_CONVEYOR_TRANSIT", f"conveyor '{name}': transit_time {transit:g} is "
                f"not a multiple of dt {dt:g}; the belt has round(T/dt) slats", f"{at}/transit_time",
                hint="pick a dt that divides the transit time"))

    primaries: dict = {name: [] for name in belts}
    for index, entry in enumerate(layout.get("flows") or []):
        if not isinstance(entry, dict):
            continue
        at, name = f"/flows/{index}", entry.get("name", "")
        source, leak = str(entry.get("from") or ""), entry.get("leak")
        if leak is not None:
            zone = leak.get("zone", [0, 1]) if isinstance(leak, dict) else None
            if source not in belts:
                problems.append(problem("E_CONVEYOR_LEAK", f"flow '{name}': a leak must flow out "
                                        f"of a conveyor", f"{at}/leak",
                                        hint="set \"from\" to a conveyor stock, or drop "
                                             "\"leak\""))
            elif not isinstance(leak, dict) or leak.get("fraction") in (None, ""):
                problems.append(problem("E_CONVEYOR_LEAK", f"flow '{name}': a leak needs a "
                                        f"\"fraction\"", f"{at}/leak",
                                        hint="{\"fraction\": 0.1, \"zone\": [0, 1]}"))
            elif not (isinstance(zone, (list, tuple)) and len(zone) == 2
                      and all(_is_number(z) for z in zone) and 0 <= zone[0] < zone[1] <= 1):
                problems.append(problem("E_CONVEYOR_LEAK", f"flow '{name}': leak zone must be "
                                        f"[start, end] with 0 <= start < end <= 1",
                                        f"{at}/leak/zone",
                                        hint="fractions of the belt from the inflowing side"))
            elif source in slats and (_half_up(zone[0] * slats[source])
                                      == _half_up(zone[1] * slats[source])):
                problems.append(problem(
                    "W_CONVEYOR_ZONE", f"flow '{name}': leak zone {zone[0]:g}-{zone[1]:g} "
                    f"covers no slat of the {slats[source]}-slat belt, so nothing leaks",
                    f"{at}/leak/zone", hint="widen the zone or use a smaller dt"))
        elif source in belts:
            primaries[source].append((index, entry))
    for name, found in primaries.items():
        if len(found) != 1:
            problems.append(problem(
                "E_CONVEYOR_OUTFLOW", f"conveyor '{name}': needs exactly one primary outflow, "
                f"has {len(found)}", f"/levels/{belts[name][0]}/conveyor",
                hint="mark the other outflows as leaks with \"leak\": {\"fraction\": ...}"))
        for index, entry in found:
            if str(entry.get("rate") or "").strip():
                problems.append(problem(
                    "E_CONVEYOR_OUTFLOW", f"flow '{entry.get('name', '')}': a conveyor's primary "
                    f"outflow is what leaves the belt and takes no rate", f"/flows/{index}/rate",
                    hint="set \"rate\": \"\""))


def _check_sd(problems: list, layout: dict) -> None:
    """`simulation.sd` and what RK4 cannot do (SD-1, D89/D90)."""
    spec = (layout.get("simulation") or {}).get("sd")
    if spec is None:
        return
    at = "/simulation/sd"
    hint = "{\"dt\": 0.25, \"method\": \"euler\"} -- dt in the model's base unit, > 0"
    if not isinstance(spec, dict):
        problems.append(problem("E_SD_SETTINGS", "simulation.sd must be an object", at, hint=hint))
        return
    dt = spec.get("dt")
    if not _is_number(dt) or not dt > 0:
        problems.append(problem("E_SD_SETTINGS", "simulation.sd: \"dt\" must be a number > 0",
                                f"{at}/dt", hint=hint))
    method = spec.get("method", "euler")
    if method not in SD_METHODS:
        problems.append(problem("E_SD_SETTINGS", f"simulation.sd: unknown method '{method}'",
                                f"{at}/method", hint=f"use one of: {', '.join(SD_METHODS)}"))
    unit = spec.get("time_unit", "base")
    if unit != "base" and unit not in UNITS:
        problems.append(problem("E_SD_SETTINGS", f"simulation.sd: unknown time_unit '{unit}'",
                                f"{at}/time_unit",
                                hint=f"use \"base\" or one of: {', '.join(UNITS)}"))
    unknown = sorted(set(spec) - {"dt", "method", "time_unit"})
    if unknown:
        problems.append(problem("E_SD_SETTINGS", f"simulation.sd: unknown key(s) "
                                f"{', '.join(unknown)}", at, hint=hint))
    if method != "rk4":
        return
    auxes = {}
    for entry in layout.get("auxiliaries") or []:
        try:
            auxes[str(entry.get("name"))] = compile_expression(str(entry.get("expr", "0")))
        except ExpressionError:
            pass
    # A dynamic rate that reads a "noise" (NOISE_NORMAL) delay redraws four
    # times per RK4 step, same as calling NORM() directly (PHASE6 S2).
    noisy = {str(d.get("name")) for d in layout.get("delays") or []
             if isinstance(d, dict) and d.get("kind") == "noise"}
    reads = sd.delay_reads(layout.get("delays"))
    for index, entry in enumerate(layout.get("flows", []) or []):
        try:
            expression = compile_expression(str(entry.get("rate", "0")), None)
        except ExpressionError:
            continue        # reported as E_EXPRESSION by the expression checks
        if not is_dynamic(expression, auxes):
            continue
        name = entry.get("name", "")
        reached = sd.reach(expression, auxes, reads)
        calls = frozenset().union(*(e.calls for e in reached))
        delay_names = {n for e in reached for n in e.refs.get("DELAY", ())}
        if (_SAMPLER_CALLS & calls) or (delay_names & noisy):
            problems.append(problem(
                "E_SD_RK4_RANDOM",
                f"flow '{name}': RK4 re-evaluates a dynamic rate four times per step, so it "
                f"cannot draw random numbers", f"/flows/{index}/rate",
                hint="use \"method\": \"euler\", or move the draw into a variable an ASSIGN "
                     "sets"))
        if _BUS_CALLS & calls:
            problems.append(problem(
                "W_SD_RK4_BUS",
                f"flow '{name}': RK4 stages ignore saturation, so FLOW()/FLOWSUM() "
                f"read the rates of the step's start", f"/flows/{index}/rate",
                hint="exact under Euler; under RK4 the read lags by one stage"))


def _check_thresholds(problems: list, who: str, thresholds, at: str, code: str) -> None:
    """A level's or a flow's threshold list (Bio-1 G3 added the flow half)."""
    for index, threshold in enumerate(thresholds or []):
        t_at = f"{at}/{index}"
        if not isinstance(threshold, dict) or not _is_number(threshold.get("value")):
            problems.append(problem(code, f"{who}: a threshold needs a numeric \"value\"", t_at,
                                    hint="{\"value\": 20, \"direction\": \"falling\", "
                                         "\"signal\": \"low\"}"))
            continue
        if threshold.get("direction", "falling") not in THRESHOLD_DIRECTIONS:
            problems.append(problem(
                code, f"{who}: unknown threshold direction '{threshold.get('direction')}'",
                f"{t_at}/direction", hint=f"use one of: {', '.join(THRESHOLD_DIRECTIONS)}"))
        cost = threshold.get("cost")
        if cost is not None and not _is_number(cost):
            problems.append(problem(
                code, f"{who}: a threshold's \"cost\" must be a number", f"{t_at}/cost",
                hint="one lump charged per crossing -- a start cost; it lands in cost_summary"))


def _check_components(problems: list, name: str, entry: dict, at: str) -> None:
    """A level's declared components and its blend tolerance (Bio-1 G2)."""
    seen: set = set()
    for index, component in enumerate(entry.get("components") or []):
        c_at = f"{at}/components/{index}"
        label = "" if not isinstance(component, dict) else str(component.get("name", ""))
        if not label.strip():
            problems.append(problem("E_LEVEL_COMPONENT",
                                    f"level '{name}': a component needs a name", f"{c_at}/name",
                                    hint="{\"name\": \"ch4\", \"initial_fraction\": 0.5}"))
            continue
        if label in seen:
            problems.append(problem("E_LEVEL_COMPONENT",
                                    f"level '{name}': duplicate component '{label}'",
                                    f"{c_at}/name", hint="rename one of the two"))
        seen.add(label)
        fraction = component.get("initial_fraction", 0.0) or 0.0
        if not _is_number(fraction) or not 0.0 <= float(fraction) <= 1.0:
            problems.append(problem(
                "E_LEVEL_COMPONENT",
                f"level '{name}': component '{label}' initial_fraction must be in [0, 1]",
                f"{c_at}/initial_fraction", hint="the share of the level's INITIAL content"))
    tolerance = entry.get("blend_tolerance")
    if tolerance is not None and (not _is_number(tolerance) or float(tolerance) <= 0):
        problems.append(problem(
            "E_LEVEL_COMPONENT", f"level '{name}': \"blend_tolerance\" must be a positive number",
            f"{at}/blend_tolerance",
            hint=f"how far the fraction may drift before the outflows re-read it "
                 f"(default {DEFAULT_BLEND_TOLERANCE})"))
