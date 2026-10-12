"""Layout-level structured validation (ROADMAP 3.1): `validate_layout_structured`
runs every section validator -- the owners' own (`validate_<section>_structured`
in their modules) and this package's -- then checks every block's params,
expressions, distributions and time units, and the links' endpoints and delays."""
from __future__ import annotations

from importlib import import_module

from nagasim.core import agents, blocking, sd, submodels, units
from nagasim.core.block_registry import BLOCK_REGISTRY, duration_types, validate_params
from nagasim.core.blocks.create import ARRIVAL_MODES
from nagasim.core.calendar import run_horizon, validate_calendar_structured
from nagasim.core.conversions import (validate_conversion_expressions,
                                      validate_conversions_structured)
from nagasim.core.costs import validate_energy_price_structured, validate_fixed_costs_structured
from nagasim.core.expressions import (LOOKUP_SECTION, RESERVED_NAMES, ExpressionError, functions_with,
                                      is_variable_name, parse_target)
from nagasim.core.initial_state import validate_initial_state_structured
from nagasim.core.levels import TRANSFER_MODES
from nagasim.core.levels_validation import validate_levels_structured
from nagasim.core.movers import validate_fleets_structured, validate_workers_structured
from nagasim.core.network import block_nodes, validate_network_structured
from nagasim.core.orders import validate_orders_structured
from nagasim.core.problems import Problem, problem
from nagasim.core.resource_manager import validate_resources_structured
from nagasim.core.schedules import validate_schedules_structured
from nagasim.core.schema.common import (_check_deterministic_mean, _check_expression, _dist_hint,
                                        _known_distributions, _link_pairs)
from nagasim.core.schema.sections_validation import (_validate_assertions, _validate_dashboard,
                                                     _validate_reality_checks,
                                                     _validate_scenarios, _validate_travel)
from nagasim.core.sections import SECTIONS
from nagasim.core.seizing import ON_PREEMPT_MODES, QUEUEING_BLOCK_TYPES
from nagasim.core.sequences import validate_sequences_structured
from nagasim.core.stats import RECORD_KINDS
from nagasim.core.tables import validate_tables_structured
from nagasim.core.timeseries_inputs import validate_timeseries_inputs_structured
from nagasim.core.quantities import check_units
from nagasim.core.timeunits import LENGTH_UNITS, UNITS


def _validate_variables(layout: dict, problems: list) -> set[str]:
    """Check the "variables" section and return the declared names."""
    declared: set[str] = set()
    for index, entry in enumerate(layout.get("variables", [])):
        at = f"/variables/{index}"
        name = entry.get("name", "")
        # A scoped `<instance>/name` is legal too: it is what flattening writes
        # for an asset's `$variable` (D72), and only an expression's VAR() or a
        # threshold can name it.
        if not isinstance(name, str) or not is_variable_name(name):
            problems.append(problem("E_VARIABLE_NAME", f"variable '{name}': not a valid identifier",
                                    f"{at}/name",
                                    hint="use a Python identifier: letters, digits and _, not "
                                         "starting with a digit"))
        elif name in RESERVED_NAMES:
            problems.append(problem("E_VARIABLE_RESERVED", f"variable '{name}': reserved name",
                                    f"{at}/name",
                                    hint=f"pick a name that is not one of: "
                                         f"{', '.join(sorted(RESERVED_NAMES))}"))
        elif name in declared:
            problems.append(problem("E_DUPLICATE_VARIABLE", f"duplicate variable name: {name}",
                                    f"{at}/name", hint="rename one of the two variables"))
        else:
            declared.add(name)
        if not isinstance(entry.get("initial", 0), (int, float, str, bool)):
            problems.append(problem(
                "E_VARIABLE_INITIAL",
                f"variable '{name}': initial value must be a number, string or boolean",
                f"{at}/initial", hint="give the variable a scalar initial value, e.g. 0"))
    return declared


_ANNOTATION_KINDS = ("text", "frame")      # a tuple: the message lists them (#263)


def _validate_annotations(layout: dict, problems: list) -> None:
    """Shape only (ROADMAP 2.3 part A #4): the engine never reads this
    section, so there is nothing to cross-check against blocks/links -- just
    that each entry has a unique id, a known kind, and that kind's required
    fields."""
    seen: set[str] = set()
    for index, entry in enumerate(layout.get("annotations", [])):
        at = f"/annotations/{index}"
        ann_id = entry.get("id", "")
        if not ann_id:
            problems.append(problem("E_ANNOTATION_ID", "annotation: id must not be empty",
                                    f"{at}/id", hint="give every annotation a unique id"))
        elif ann_id in seen:
            problems.append(problem("E_DUPLICATE_ANNOTATION", f"duplicate annotation id: {ann_id}",
                                    f"{at}/id", hint="rename one of the two annotations"))
        seen.add(ann_id)
        kind = entry.get("kind")
        if kind not in _ANNOTATION_KINDS:
            problems.append(problem(
                "E_ANNOTATION_KIND",
                f"annotation '{ann_id}': unknown kind '{kind}' "
                f"(expected one of {', '.join(_ANNOTATION_KINDS)})",
                f"{at}/kind", hint=f"use one of: {', '.join(_ANNOTATION_KINDS)}"))
            continue
        if kind == "text":
            pos = entry.get("position")
            if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
                problems.append(problem(
                    "E_ANNOTATION_SHAPE", f"annotation '{ann_id}': text needs a 2-element position",
                    f"{at}/position", hint="set \"position\": [x, y]"))
        else:  # frame
            rect = entry.get("rect")
            if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
                problems.append(problem(
                    "E_ANNOTATION_SHAPE",
                    f"annotation '{ann_id}': frame needs a 4-element rect [x, y, w, h]",
                    f"{at}/rect", hint="set \"rect\": [x, y, width, height]"))


def _validate_background(layout: dict, problems: list) -> None:
    """Shape only (P2.3a item 1): an optional top-level "background" key, no
    version bump (absent/None == "no background", exactly today's behaviour).
    The engine never reads this key; existence of the image file is a
    Problems-dock WARNING raised by ModelDocument.problems(), not here (this
    function has no filesystem access, by design -- core/schema/ works on
    plain dicts)."""
    bg = layout.get("background")
    if bg is None:
        return
    if not isinstance(bg, dict):
        problems.append(problem("E_BACKGROUND", "background: must be an object", "/background",
                                hint="use null for no background, or an object with a \"path\""))
        return
    if not bg.get("path"):
        problems.append(problem("E_BACKGROUND", "background: path must not be empty",
                                "/background/path", hint="point \"path\" at the floor-plan image"))
    position = bg.get("position", [0, 0])
    if not (isinstance(position, (list, tuple)) and len(position) == 2):
        problems.append(problem("E_BACKGROUND", "background: position must be a 2-element [x, y]",
                                "/background/position", hint="set \"position\": [x, y]"))
    scale = bg.get("scale", 1.0)
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        problems.append(problem("E_BACKGROUND", "background: scale must be a number > 0",
                                "/background/scale", hint="use 1.0 for the image's natural size"))
    opacity = bg.get("opacity", 1.0)
    if not isinstance(opacity, (int, float)) or isinstance(opacity, bool) or not (0 <= opacity <= 1):
        problems.append(problem("E_BACKGROUND", "background: opacity must be a number in [0, 1]",
                                "/background/opacity", hint="0 is invisible, 1 fully opaque"))
    if "locked" in bg and not isinstance(bg["locked"], bool):
        problems.append(problem("E_BACKGROUND", "background: locked must be a boolean",
                                "/background/locked", hint="use true or false"))
    units_per_metre = bg.get("units_per_metre")
    if units_per_metre is not None and (
            not isinstance(units_per_metre, (int, float)) or isinstance(units_per_metre, bool)
            or units_per_metre <= 0):
        problems.append(problem("E_BACKGROUND",
                                "background: units_per_metre must be a number > 0 or null",
                                "/background/units_per_metre",
                                hint="scene units per real-world metre, or null for no scale"))


def _validate_initial_state(layout: dict, problems: list) -> None:
    problems.extend(validate_initial_state_structured(layout))


def _validate_simulation(layout: dict, problems: list) -> None:
    # The model's own units (D160) name a real unit, never "base" (#306).
    sim = layout.get("simulation", {})
    for key, table, code in (("time_unit", UNITS, "E_TIME_UNIT"),
                             ("length_unit", LENGTH_UNITS, "E_UNIT")):
        unit = sim.get(key)
        if unit is not None and unit not in table:
            problems.append(problem(code, f"simulation: unknown {key} '{unit}'",
                                    f"/simulation/{key}", hint=f"one of: {', '.join(table)}"))
    # The blocks whose departures are sampled (PHASE7 7.14, D290): names of blocks there
    # are. An authored model with submodels is judged again flattened, and its counted
    # names are the flattened ones: here they are only judged as a list.
    counted = sim.get("timeseries_counts")
    if counted is not None:
        names = {b.get("name") for b in layout.get("blocks", [])}
        nested = any(str(b.get("type", "")).upper() == "SUBMODEL" for b in layout.get("blocks", []))
        if not isinstance(counted, list) or len(counted) > 1000 or len(set(map(str, counted))) \
                != len(counted) or not all(isinstance(name, str) and name for name in counted):
            problems.append(problem(
                "E_TIMESERIES_COUNTS", "simulation: timeseries_counts must be a list of at most "
                "1000 different block names", "/simulation/timeseries_counts",
                hint="for example [\"Drill\", \"Pack\"]: the series block/<name>/out"))
        elif not nested:
            for name in counted:
                if name not in names:
                    problems.append(problem(
                        "E_TIMESERIES_COUNTS", f"simulation: timeseries_counts names '{name[:80]}'"
                        f", which is no block of the model", "/simulation/timeseries_counts",
                        hint=f"its blocks: {', '.join(sorted(map(str, names))[:20]) or '(none)'}"))
    # Every other field's unit, from the metadata (units spec): blocks, links,
    # failures and each section's own unit keys, once per layout.
    check_units(layout, problems)


def _validate_link_delays(layout: dict, problems: list) -> None:
    """A link's optional transfer delay (ROADMAP 1.5): a known distribution
    and a known time_unit, same shape as any other duration spec."""
    deterministic = bool(layout.get("simulation", {}).get("deterministic"))
    for index, link in enumerate(layout.get("links", [])):
        if not isinstance(link, dict):
            continue
        delay = link.get("delay")
        if not delay:
            continue
        at = f"/links/{index}/delay"
        label = f"link {link.get('source', '?')}->{link.get('target', '?')}"
        dist = delay.get("distribution")
        if dist is None:
            problems.append(problem("E_DISTRIBUTION_MISSING", f"{label}: delay has no distribution",
                                    f"{at}/distribution",
                                    hint="drop the delay, or name a distribution: " + _dist_hint()))
        elif dist not in _known_distributions():
            problems.append(problem("E_UNKNOWN_DISTRIBUTION",
                                    f"{label}: unknown delay distribution '{dist}'",
                                    f"{at}/distribution", hint=_dist_hint()))
        _check_deterministic_mean(problems, deterministic, dist, delay.get("dist_params", {}),
                                  label, f"{at}/dist_params")


def _validate_failures(layout: dict, problems: list) -> None:
    """Failures (ROADMAP 1.4): named resources must exist, both uptime and
    downtime must name a known distribution (and a known time_unit, ROADMAP 1.5)."""
    deterministic = bool(layout.get("simulation", {}).get("deterministic"))
    # A mover fleet IS a resource under its own name (PHASE4 E1), so a failure
    # may break down a forklift pool exactly as it breaks down a machine.
    resources = {r.get("name") for r in layout.get("resources", [])}
    resources |= {f.get("name") for f in layout.get("fleets", []) or [] if isinstance(f, dict)}
    seen: set[str] = set()
    for index, entry in enumerate(layout.get("failures", [])):
        at = f"/failures/{index}"
        name = entry.get("name", "")
        if not name:
            problems.append(problem("E_FAILURE_NAME", "failure: name must not be empty",
                                    f"{at}/name", hint="give every failure a unique name"))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_FAILURE", f"duplicate failure name: {name}",
                                    f"{at}/name", hint="rename one of the two failures"))
        seen.add(name)
        if not entry.get("resources"):
            problems.append(problem("E_FAILURE_RESOURCES", f"failure '{name}': no resources",
                                    f"{at}/resources",
                                    hint="list the resources this failure takes down"))
        for resource in entry.get("resources", []):
            if resource not in resources:
                problems.append(problem(
                    "E_FAILURE_RESOURCE_UNKNOWN", f"failure '{name}': unknown resource '{resource}'",
                    f"{at}/resources",
                    hint=f"declare the resource first; declared: "
                         f"{', '.join(sorted(str(r) for r in resources)) or '(none)'}"))
        for key in ("uptime", "downtime"):
            dist = entry.get(key, {}).get("distribution")
            if dist is None:
                problems.append(problem("E_DISTRIBUTION_MISSING",
                                        f"failure '{name}': {key} has no distribution",
                                        f"{at}/{key}/distribution",
                                        hint="uptime and downtime are both duration specs: "
                                             + _dist_hint()))
            elif dist not in _known_distributions():
                problems.append(problem("E_UNKNOWN_DISTRIBUTION",
                                        f"failure '{name}': unknown {key} distribution '{dist}'",
                                        f"{at}/{key}/distribution", hint=_dist_hint()))
            _check_deterministic_mean(problems, deterministic, dist, entry.get(key, {}).get("dist_params", {}),
                                      f"failure '{name}': {key}", f"{at}/{key}/dist_params")


def _validate_entities(layout: dict, problems: list) -> None:
    """Entity types (ROADMAP 2.2): `holding_cost` (WIP cost per model base time
    unit) must be a non-negative number when declared."""
    for index, entry in enumerate(layout.get("entities", [])):
        name = entry.get("name", "")
        cost = entry.get("holding_cost", 0.0)
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
            problems.append(problem("E_ENTITY_COST",
                                    f"entity '{name}': holding_cost must be a number >= 0",
                                    f"/entities/{index}/holding_cost",
                                    hint="use 0 when WIP costs nothing to hold"))


def _validate_record_stats(layout: dict, problems: list) -> None:
    """RECORD blocks (ROADMAP 1.3): `stat_name` non-empty, `kind` known, and
    every RECORD sharing a `stat_name` agrees on `kind` (they share one
    collector at run time, `stats.UserStat`)."""
    kinds: dict[str, str] = {}
    for index, block in enumerate(layout.get("blocks", [])):
        if block.get("type", "").upper() != "RECORD":
            continue
        at = f"/blocks/{index}/params"
        name = block.get("name", "<unnamed>")
        params = block.get("params", {})
        stat_name = params.get("stat_name", "")
        kind = params.get("kind", "count")
        if not stat_name:
            problems.append(problem("E_RECORD_STAT_NAME", f"{name}: stat_name must not be empty",
                                    f"{at}/stat_name", block=name,
                                    hint="name the statistic this RECORD collects"))
            continue
        if kind not in RECORD_KINDS:
            problems.append(problem(
                "E_RECORD_KIND",
                f"{name}: unknown kind '{kind}' (expected one of {', '.join(RECORD_KINDS)})",
                f"{at}/kind", block=name, hint=f"use one of: {', '.join(RECORD_KINDS)}"))
            continue
        prior = kinds.get(stat_name)
        if prior is not None and prior != kind:
            problems.append(problem(
                "E_RECORD_KIND_CONFLICT",
                f"{name}: statistic '{stat_name}' is already kind '{prior}', "
                f"cannot also be '{kind}'",
                f"{at}/kind", block=name,
                hint=f"use kind '{prior}' here, or record into a different stat_name"))
        else:
            kinds[stat_name] = kind


def _check_arrival_mode(problems: list, name: str, params: dict,
                        schedule_kinds: dict, at: str) -> None:
    """CREATE's `arrival_mode` (ROADMAP 1.4): "Schedule" needs a declared
    arrival schedule; "Interarrival" keeps using `distribution`."""
    mode = params.get("arrival_mode", "Interarrival")
    if mode not in ARRIVAL_MODES:
        problems.append(problem(
            "E_ARRIVAL_MODE",
            f"{name}: unknown arrival_mode '{mode}' (expected one of {', '.join(ARRIVAL_MODES)})",
            f"{at}/arrival_mode", block=name, hint=f"use one of: {', '.join(ARRIVAL_MODES)}"))
        return
    if mode != "Schedule":
        return
    arrivals = [n for n, k in schedule_kinds.items() if k == "arrival"]
    schedule_name = params.get("schedule_name", "")
    if not schedule_name:
        problems.append(problem(
            "E_ARRIVAL_SCHEDULE", f"{name}: arrival_mode 'Schedule' needs a schedule_name",
            f"{at}/schedule_name", block=name,
            hint=f"name a declared arrival schedule: {', '.join(arrivals) or '(none declared)'}"))
    elif schedule_kinds.get(schedule_name) != "arrival":
        problems.append(problem(
            "E_ARRIVAL_SCHEDULE",
            f"{name}: schedule_name '{schedule_name}' is not a declared "
            f"schedule of kind 'arrival'",
            f"{at}/schedule_name", block=name,
            hint=f"declare it under \"schedules\" with \"kind\": \"arrival\"; declared: "
                 f"{', '.join(arrivals) or '(none)'}"))


def _check_sequence_names(problems: list, name: str, btype: str, params: dict,
                          sequences: dict, at: str) -> None:
    """A LITERAL sequence name must be one the model declares (PHASE4 A2).

    CREATE's `sequence` is always a literal. An ASSIGN to `entity.sequence` is a
    full expression, so only a plain string constant can be checked here -- a
    computed one (`"Widget" if ... else "Gadget"`) fails at run time instead,
    the same rule literal TABLE() names follow.
    """
    known = ", ".join(sorted(sequences)) or "(none)"

    def check(value, path):
        if value and value not in sequences:
            problems.append(problem(
                "E_SEQUENCE_UNKNOWN", f"{name}: unknown sequence '{value}'", path, block=name,
                hint=f"declare it under \"sequences\"; declared: {known}"))

    if btype == "CREATE":
        check(params.get("sequence", ""), f"{at}/sequence")
    elif btype == "ASSIGN":
        for index, assignment in enumerate(params.get("assignments", [])):
            if str(assignment.get("target", "")).strip() != "entity.sequence":
                continue
            text = str(assignment.get("expr", "")).strip()
            if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"" and text[0] not in text[1:-1]:
                check(text[1:-1], f"{at}/assignments/{index}/expr")


# Names a block makes visible to ONE of its own expressions and nothing else
# (PHASE4 A2): ROUTE's transfer time sees where the entity is coming from and
# going to, a PROCESS setup sees the two keys it is changing over between.
# Mirrors what `BaseProcess.expression_scope(extra=...)` supplies at run time.
_EXTRA_SCOPE = {"ROUTE": frozenset({"from_station", "to_station"})}
_SETUP_SCOPE = frozenset({"previous_key", "key"})
# `balk_if` sees the queue the entity would join (PHASE4 A4).
_BALK_SCOPE = frozenset({"queue_length"})


def _check_block_expressions(problems: list, name: str, btype: str, params: dict,
                             variables: set[str], at: str, tables: dict | None = None,
                             refs: dict | None = None) -> None:
    """Compile every expression a block carries, so a typo is a load-time
    problem in the Problems dock, never a crash mid-run. *tables* is
    {name: dimensions}, checked against every literal TABLE() call."""
    if btype in blocking.PROCESS_TYPES:
        # The setup expression (PHASE4 A2) is the one place `previous_key` and
        # `key` are legal names; `setup_key` is an ordinary expression.
        if params.get("setup"):
            _check_expression(problems, name, params["setup"], variables | _SETUP_SCOPE,
                              f"{at}/setup", tables=tables, refs=refs)
        # PHASE4 T3: the changeover COST sees the same two keys as the duration.
        if params.get("setup_cost"):
            _check_expression(problems, name, params["setup_cost"], variables | _SETUP_SCOPE,
                              f"{at}/setup_cost", tables=tables, refs=refs)
        if params.get("setup_key"):
            _check_expression(problems, name, params["setup_key"], variables,
                              f"{at}/setup_key", tables=tables, refs=refs)
    # D156: a segment's `time` is an ordinary expression (TRIA(...), a submodel
    # parameter); a move has none. A STATION's tending cycles (D169) are MOTION
    # segments too.
    cycles = ({"segments": params.get("segments")} if btype == "MOTION"
              else {f"tending/{w}": (params.get("tending") or {}).get(w) for w in ("place", "pick")}
              if btype == "STATION" and isinstance(params.get("tending"), dict) else {})
    for key, segments in cycles.items():
        for index, segment in enumerate(segments or []):
            text = segment.get("time") if isinstance(segment, dict) else None
            if isinstance(text, str) and text.strip() and segment.get("kind") != "move":
                _check_expression(problems, name, text, variables,
                                  f"{at}/{key}/{index}/time", tables=tables, refs=refs)
    if btype in QUEUEING_BLOCK_TYPES:
        # PHASE4 A4: balk_if is the one place `queue_length` is a legal name.
        if params.get("balk_if"):
            _check_expression(problems, name, params["balk_if"], variables | _BALK_SCOPE,
                              f"{at}/balk_if", tables=tables, refs=refs)
        if params.get("renege_after"):
            _check_expression(problems, name, params["renege_after"], variables,
                              f"{at}/renege_after", tables=tables, refs=refs)
    if btype == "BATCH" and params.get("max_wait"):     # PHASE4 B6
        _check_expression(problems, name, params["max_wait"], variables, f"{at}/max_wait",
                          tables=tables, refs=refs)
    if btype == "ASSIGN":
        for index, assignment in enumerate(params.get("assignments", [])):
            where = f"{at}/assignments/{index}"
            target = assignment.get("target", "")
            try:
                kind, target_name = parse_target(target)
                if kind == "self":      # an agent action's target (PHASE5 AB-2)
                    raise ExpressionError("self.<attribute> is only valid in an agent action")
                if kind == "variable" and target_name not in variables:
                    problems.append(problem(
                        "E_UNDECLARED_VARIABLE", f"{name}: {target}: undeclared variable",
                        f"{where}/target", block=name,
                        hint=f"add '{target_name}' to the model's \"variables\", or assign to "
                             f"entity.{target_name} instead"))
            except ExpressionError as exc:
                problems.append(problem("E_ASSIGN_TARGET", f"{name}: {target}: {exc}",
                                        f"{where}/target", block=name,
                                        hint="a target is `entity.<attr>` or a declared variable name"))
            _check_expression(problems, name, assignment.get("expr", ""), variables,
                              f"{where}/expr", tables=tables, refs=refs)
    elif btype in ("DECIDE", "HOLD") and params.get("mode") == "Condition":
        for index, condition in enumerate(params.get("conditions", [])):
            _check_expression(problems, name, condition, variables, f"{at}/conditions/{index}", tables=tables, refs=refs)
    elif btype == "DECIDE" and params.get("mode") == "Policy":
        for index, expr in enumerate(params.get("observation", [])):
            _check_expression(problems, name, expr, variables, f"{at}/observation/{index}", tables=tables, refs=refs)
        if params.get("fallback", "first") == "shortest":
            for index, expr in enumerate(params.get("queue_exprs", [])):
                _check_expression(problems, name, expr, variables,
                                  f"{at}/queue_exprs/{index}", tables=tables, refs=refs)
    elif btype == "RECORD" and params.get("kind", "count") != "time_between":
        _check_expression(problems, name, params.get("expr", "1"), variables, f"{at}/expr", tables=tables, refs=refs)
    elif btype == "ROUTE" and params.get("speed"):       # PHASE4 E1
        _check_expression(problems, name, params["speed"], variables, f"{at}/speed",
                          tables=tables, refs=refs)

    in_scope = variables | _EXTRA_SCOPE.get(btype, frozenset())
    for key, value in params.get("dist_params", {}).items():
        if isinstance(value, str):      # an expression-valued parameter
            _check_expression(problems, name, value, in_scope, f"{at}/dist_params/{key}", tables=tables, refs=refs)


# validate_params problem strings -> a stable code plus a hint (ROADMAP 3.1).
def _param_problem(text: str, name: str, btype: str, at: str) -> Problem:
    if text.startswith("unknown block type: "):
        return problem("E_UNKNOWN_BLOCK_TYPE", f"{name}: {text}", f"/blocks/{at}/type", block=name,
                       hint=f"use one of: {', '.join(sorted(BLOCK_REGISTRY))}")
    spec = BLOCK_REGISTRY.get(btype)
    declared = ", ".join(p.name for p in spec.params) if spec else ""
    if text.startswith("unknown key: "):
        key = text.split(": ", 1)[1]
        return problem("E_PARAM_UNKNOWN", f"{name}: {text}", f"/blocks/{at}/params/{key}",
                       block=name, hint=f"{btype} takes: {declared or '(no parameters)'}")
    if text.startswith("missing required key: "):
        key = text.split(": ", 1)[1]
        return problem("E_PARAM_MISSING", f"{name}: {text}", f"/blocks/{at}/params/{key}",
                       block=name, hint=f"set \"{key}\" on this block")
    if text.startswith("wrong type for key "):
        key = text.split("'", 2)[1]
        return problem("E_PARAM_TYPE", f"{name}: {text}", f"/blocks/{at}/params/{key}",
                       block=name, hint=f"{btype} takes: {declared or '(no parameters)'}")
    return problem("E_PARAM_TYPE", f"{name}: {text}", f"/blocks/{at}/params", block=name)


def _service_owner(block: dict) -> bool:
    """Whether *block* holds its resource inside one block, start to finish.

    Only such a block can be preempted safely: the SimPy process that made the
    request is still alive to receive the interrupt, and there is a remaining
    service time to give back. A SEIZE (or a "Seize Delay" PROCESS) hands the
    hold to a later RELEASE, with arbitrary blocks in between (ARCHITECTURE D55).
    """
    return (block.get("type", "").upper() in blocking.PROCESS_TYPES
            and block.get("params", {}).get("action", "Seize Delay Release") == "Seize Delay Release")


_RESOURCE_NAME_BLOCK_TYPES = (*QUEUEING_BLOCK_TYPES, "RELEASE")


def _validate_resource_refs(layout: dict, problems: list) -> None:
    """#284: a SEIZE/PROCESS/RELEASE naming a resource that does not exist used
    to produce no validation Problem -- only the fail-soft runtime warning in
    core/blocks/{seize,process,release}.py, which auto-registers a capacity-1
    resource so the run does not crash. That fallback stays (a model already
    running on it keeps working); this only makes the mistake visible before
    a run, like E_FAILURE_RESOURCE_UNKNOWN does for failures.

    A fleet (PHASE6 W3-M3) registers itself as a resource of the same name at
    run time (`movers.Fleet.__init__`), so RELEASE naming a fleet to give a
    worker back (there is no separate `worker_fleet` param on RELEASE) is
    equally valid -- fleet names count as declared too."""
    declared = ({r.get("name") for r in layout.get("resources", []) or []}
                | {s.get("name") for s in layout.get("resource_sets", []) or []}
                | {f.get("name") for f in layout.get("fleets", []) or []})
    for index, block in enumerate(layout.get("blocks", [])):
        if block.get("type", "").upper() not in _RESOURCE_NAME_BLOCK_TYPES:
            continue
        name = (block.get("params") or {}).get("resource_name")
        if name and name not in declared:
            problems.append(problem(
                "E_RESOURCE_UNKNOWN",
                f"{block.get('name')}: resource_name '{name}' names no declared resource or "
                f"resource set",
                f"/blocks/{index}/params/resource_name", block=block.get("name"),
                hint="declare it under \"resources\"/\"resource_sets\", or fix the typo"))


def _validate_queue_behaviour(layout: dict, problems: list) -> None:
    """Preemption's scope rules (PHASE4 A4); the link rules live in
    validate_graph_structured, which is where outgoing links are known."""
    blocks = layout.get("blocks", [])
    sets = {s.get("name") for s in layout.get("resource_sets", []) or []}
    seizers: dict[str, list[dict]] = {}
    for block in blocks:
        params = block.get("params", {})
        if block.get("type", "").upper() in QUEUEING_BLOCK_TYPES and params.get("resource_name"):
            if block.get("type", "").upper() == "SEIZE" or params.get("action", "Seize Delay Release").startswith("Seize"):
                seizers.setdefault(params["resource_name"], []).append(block)

    for index, block in enumerate(blocks):
        params = block.get("params", {})
        name = block.get("name", "<unnamed>")
        at = f"/blocks/{index}"
        # `on_preempt` belongs to the block whose job is INTERRUPTED, so it is
        # checked on every block that sets it, not only on preempting ones.
        mode = params.get("on_preempt") or ON_PREEMPT_MODES[0]
        if mode not in ON_PREEMPT_MODES:
            problems.append(problem("E_ON_PREEMPT", f"{name}: unknown on_preempt '{mode}'",
                                    f"{at}/params/on_preempt", block=name,
                                    hint=f"use one of: {', '.join(ON_PREEMPT_MODES)}"))
        if not params.get("preempt"):
            continue
        if not _service_owner(block):
            problems.append(problem(
                "E_PREEMPT_SCOPE",
                f"{name}: preempt needs action \"Seize Delay Release\"",
                f"{at}/params/preempt", block=name,
                hint="only a PROCESS that seizes, serves and releases in one block can hand the "
                     "interrupted job its remaining service back; use that action, or drop preempt"))
            continue
        resource = params.get("resource_name", "")
        if resource in sets:
            problems.append(problem(
                "E_PREEMPT_RESOURCE_SET",
                f"{name}: preempt on resource set '{resource}'",
                f"{at}/params/resource_name", block=name,
                hint="preemption interrupts a job on ONE resource: name a member instead of the set"))
            continue
        others = [b for b in seizers.get(resource, []) if b is not block]
        across = [b.get("name") for b in others if not _service_owner(b)]
        if across:
            problems.append(problem(
                "E_PREEMPT_SCOPE",
                f"{name}: preempt on '{resource}', which {across[0]} holds across blocks",
                f"{at}/params/preempt", block=name,
                hint=f"a job seized in {across[0]} and released elsewhere cannot be interrupted "
                     f"safely: make it a PROCESS \"Seize Delay Release\", or drop preempt"))
        own = params.get("priority", 0)
        if not any(b.get("params", {}).get("priority", 0) != own for b in others):
            problems.append(problem(
                "W_PREEMPT_NO_PRIORITY",
                f"{name}: preempt is set but every block seizing '{resource}' uses "
                f"priority {own}, so nothing is ever preempted",
                f"{at}/params/preempt", block=name,
                hint="give the interrupting block a LOWER priority number than the jobs it should "
                     "interrupt"))


def _validate_match(layout: dict, problems: list) -> None:
    """MATCH by attribute (PHASE5 O4): `match_on` must name an attribute."""
    for index, block in enumerate(layout.get("blocks", [])):
        if block.get("type", "").upper() != "MATCH":
            continue
        value = (block.get("params") or {}).get("match_on", "")
        attr = value[len("entity."):] if isinstance(value, str) and value.startswith("entity.") else value
        if not isinstance(attr, str) or (attr and (not attr.isidentifier() or attr.startswith("_"))):
            name = block.get("name")
            problems.append(problem(
                "E_MATCH_ATTRIBUTE", f"{name}: match_on {value!r} is not an attribute name",
                f"/blocks/{index}/params/match_on", block=name,
                hint="write the attribute's name, e.g. \"order\" (or \"entity.order\"); empty "
                     "matches by type alone"))


def _validate_blocking(layout: dict, problems: list, levels: set) -> None:
    """Finite buffers and blocking after service (PHASE4 T5, ARCHITECTURE D78).

    Three checks: a capacity that is not a count, a BUFFER's mirror level, and
    the one that matters -- a closed LOOP of finite buffers, where every
    station could end up holding a part with nowhere to put it. The loop is a
    warning, not an error, because a big enough buffer may make it unreachable;
    at run time a loop that really closes ends the run with an error.
    """
    blocks = layout.get("blocks", [])
    conveyors = {l.get("name") for l in layout.get("levels", []) or []
                 if isinstance(l, dict) and l.get("conveyor") is not None}
    for index, block in enumerate(blocks):
        name = block.get("name", "<unnamed>")
        btype = block.get("type", "").upper()
        params = block.get("params", {}) or {}
        at = f"/blocks/{index}"
        key = "capacity" if btype == "BUFFER" else "queue_capacity"
        if btype == "BUFFER" or btype in QUEUEING_BLOCK_TYPES:
            value = params.get(key, 0)
            if isinstance(value, bool) or (isinstance(value, int) and value < 0):
                problems.append(problem(
                    "E_BUFFER_CAPACITY", f"{name}: {key} must be a count (0 or more), got {value!r}",
                    f"{at}/params/{key}", block=name,
                    hint="0 places is a legal buffer (pure blocking after service); for an "
                         "unlimited queue leave queue_capacity at 0 and draw no BUFFER"))
        if btype == "BUFFER" and params.get("level") and params["level"] not in levels:
            problems.append(problem(
                "E_BUFFER_LEVEL", f"{name}: no level named '{params['level']}'",
                f"{at}/params/level", block=name,
                hint="declare the level in the \"levels\" section, or clear the field"))
        elif btype == "BUFFER" and params.get("level") in conveyors:     # DEVLOG #240
            problems.append(problem(
                "E_CONVEYOR_WRITE", f"{name}: '{params['level']}' is a conveyor; a BUFFER "
                f"cannot mirror its content into it", f"{at}/params/level", block=name,
                hint="mirror into an ordinary level"))

    out_links: dict[str, list[str]] = {}
    for src, tgt in _link_pairs(layout):
        out_links.setdefault(src, []).append(tgt)
    edges, spaces = blocking.layout_blocking_graph(blocks, out_links)
    index_of = {b.get("name"): i for i, b in enumerate(blocks)}

    for loop in blocking.cycles(edges):
        chain = " -> ".join(loop) + f" -> {loop[0]}"
        head = loop[0]
        problems.append(problem(
            "W_BLOCKING_LOOP",
            f"finite buffers form a closed loop: {chain}; if every one fills, nothing "
            f"in the loop can move again", f"/blocks/{index_of.get(head, 0)}", block=head,
            hint="give one buffer in the loop enough places to never fill, or send one station's "
                 "output somewhere with an unlimited queue"))

    # Blocking BEFORE service (PHASE5 O4) reserves a place in a real space.
    by_name = {b.get("name"): b for b in blocks}
    for index, block in enumerate(blocks):
        params = block.get("params", {}) or {}
        if block.get("type", "").upper() not in QUEUEING_BLOCK_TYPES or not params.get("hold_until_space"):
            continue
        name = block.get("name")
        target = blocking.hold_target(str(params.get("space_of") or ""), out_links.get(name, []))
        other = by_name.get(target) or {}
        kind = other.get("type", "").upper()
        places = (other.get("params") or {}).get("capacity" if kind == "BUFFER" else "queue_capacity",
                  1 if kind == "BUFFER" else 0)
        if kind not in ("BUFFER", *QUEUEING_BLOCK_TYPES) or not isinstance(places, int) or places <= 0:
            problems.append(problem(
                "E_HOLD_SPACE_TARGET",
                f"{name}: hold_until_space reserves a place in '{target or '(no link)'}', which has "
                f"no places", f"/blocks/{index}/params/hold_until_space", block=name,
                hint="link this block to (or name in space_of) a BUFFER with capacity > 0 or a "
                     "SEIZE/PROCESS with queue_capacity > 0"))

    # A part that is blocked is finished: there is no remaining service to hand
    # back, so it must not be preempted (ARCHITECTURE D55 and D78).
    blockers = {src for src, targets in edges.items()
                if src not in spaces and any(t in spaces for t in targets)}
    resources_held = {(b.get("params", {}) or {}).get("resource_name")
                      for b in blocks if b.get("name") in blockers}
    for index, block in enumerate(blocks):
        params = block.get("params", {}) or {}
        if params.get("preempt") and params.get("resource_name") in resources_held:
            problems.append(problem(
                "E_PREEMPT_BLOCKING",
                f"{block.get('name')}: preempt on '{params.get('resource_name')}', which a station "
                f"holds while blocked after service",
                f"/blocks/{index}/params/preempt", block=block.get("name"),
                hint="a finished part waiting for room has no service left to give back: drop "
                     "preempt, or give the downstream buffer enough places that nothing blocks"))


def _validate_flow_expressions(layout: dict, problems: list, variables: set[str],
                               tables: dict, refs: dict) -> None:
    """Every expression the "flows" section carries -- rate, price and the
    impact factor (PHASE4 D1). No `entity` is in scope: a flow is not on an
    entity's path, so its rate reads variables, TS(), TABLE(), LEVEL() and
    FLOW() and nothing else."""
    for index, entry in enumerate(layout.get("flows", []) or []):
        name = entry.get("name", "")
        if entry.get("hidden"):
            continue        # a delay stage (SD-2): its delay's own fields are checked
        # A `$`-cloned flow carries its instance's submodel parameters (PHASE4
        # D3), which are in scope for its own expressions and nothing else --
        # the flow twin of an expanded block's "locals".
        in_scope = variables | set(entry.get("locals") or {})
        for field in ("rate", "price", "emission_factor"):
            text = entry.get(field)
            if text not in (None, ""):
                _check_expression(problems, f"flow '{name}'", text, in_scope,
                                  f"/flows/{index}/{field}", tables=tables, refs=refs)
        # A component share may be an expression too (Bio-1 G2): a conversion's
        # live blend, a level's own fraction, a variable.
        components = entry.get("components")
        for key, text in (components.items() if isinstance(components, dict) else ()):
            if isinstance(text, str) and text:
                _check_expression(problems, f"flow '{name}' component '{key}'", text, in_scope,
                                  f"/flows/{index}/components/{key}", tables=tables, refs=refs)
        leak = entry.get("leak")        # a conveyor's leak fraction (C1)
        if isinstance(leak, dict) and isinstance(leak.get("fraction"), str) and leak["fraction"]:
            _check_expression(problems, f"flow '{name}' leak", leak["fraction"], in_scope,
                              f"/flows/{index}/leak/fraction", tables=tables, refs=refs)


def _validate_sd_expressions(layout: dict, problems: list, variables: set[str],
                             tables: dict, refs: dict) -> None:
    """The expressions of the SD sections (PHASE5 SD-2): each auxiliary, each
    delay's input/delay_time/initial, each level's `initial` expression. Like a
    flow, none of them has an entity in scope."""
    def check(who, text, entry, path):
        if isinstance(text, str) and text:
            _check_expression(problems, who, text, variables | set(entry.get("locals") or {}),
                              path, tables=tables, refs=refs)
    for index, entry in enumerate(layout.get("auxiliaries") or []):
        check(f"auxiliary '{entry.get('name', '')}'", entry.get("expr", ""), entry,
              f"/auxiliaries/{index}/expr")
    for index, entry in enumerate(layout.get("delays") or []):
        for field in ("input", "delay_time", "initial"):
            check(f"delay '{entry.get('name', '')}'", entry.get(field), entry,
                  f"/delays/{index}/{field}")
    for index, entry in enumerate(layout.get("levels") or []):
        if not entry.get("hidden"):
            check(f"level '{entry.get('name', '')}'", entry.get("initial"), entry,
                  f"/levels/{index}/initial")
        belt = entry.get("conveyor")        # C1
        for field in ("transit_time", "capacity", "in_limit", "arrest"):
            if isinstance(belt, dict):
                check(f"conveyor '{entry.get('name', '')}' {field}", belt.get(field), entry,
                      f"/levels/{index}/conveyor/{field}")


def _check_send(problems: list, name: str, params: dict, layout: dict, variables: set[str],
                tables, refs, at: str) -> None:
    """A SEND block's recipients and payload (PHASE5 AB-2)."""
    types = {e.get("name"): e for e in layout.get("agent_types") or [] if isinstance(e, dict)}
    pops = {p.get("name"): types.get(p.get("type")) for p in layout.get("populations") or []
            if isinstance(p, dict)}
    if not pops:
        problems.append(problem("E_SEND_TARGET", f"{name}: the model declares no populations",
                                f"{at}/to", block=name, hint="declare a population to send to"))
        return

    def check(text, path):
        _check_expression(problems, name, text, variables, path, tables=tables, refs=refs)

    def check_where(who, text, names, path):      # self = the recipient (DEVLOG #133)
        _check_expression(problems, who, text, names, path, tables=tables, refs=refs,
                          allow_self=True)
    problems.extend(agents.target_problems(params.get("to", "entity.agent"), name, f"{at}/to",
                                           pops, variables, False, check, check_where))
    if params.get("payload"):
        check(params["payload"], f"{at}/payload")


def _check_transfer(problems: list, name: str, params: dict, levels: set, conveyors: set,
                    variables: set[str], tables: dict, refs: dict, at: str) -> None:
    """A TRANSFER block's level, amount expression and mode (PHASE4 D1); C2:
    a conveyor's own value is the sum of its cohorts, so a TRANSFER writing it
    would be silently overwritten at the next grid step -- refused instead."""
    level = params.get("level", "")
    if level in conveyors:
        problems.append(problem(
            "E_CONVEYOR_WRITE", f"{name}: '{level}' is a conveyor; a TRANSFER cannot move "
            f"material into or out of it", f"{at}/level", block=name,
            hint="TRANSFER only ordinary levels; a conveyor's inflow/outflow are its own flows"))
    elif not level or level not in levels:
        problems.append(problem(
            "E_LEVEL_UNKNOWN", f"{name}: unknown level '{level}'", f"{at}/level", block=name,
            hint=f"declare it under \"levels\"; declared: {', '.join(sorted(levels)) or '(none)'}"))
    if params.get("mode", "remove") not in TRANSFER_MODES:
        problems.append(problem(
            "E_TRANSFER_MODE", f"{name}: unknown mode '{params.get('mode')}'", f"{at}/mode",
            block=name, hint=f"use one of: {', '.join(TRANSFER_MODES)}"))
    _check_expression(problems, name, params.get("amount", "1"), variables, f"{at}/amount",
                      tables=tables, refs=refs)


def _without_stray_structural_args(layout: dict, problems: list) -> dict:
    """A `{"$arg": ...}` is legal only inside a submodel definition, where
    flattening replaces it. One that reaches this validator (a top-level block,
    a hand-edited or agent-written file) has no parameter to read: report it
    and drop the key so the checks below see the param's default instead of a
    dict they would choke on (DEVLOG #101)."""
    blocks = layout.get("blocks", [])
    if not any(isinstance(v, dict) and len(v) == 1 and submodels.ARG_KEY in v
               for b in blocks for v in (b.get("params") or {}).values()):
        return layout
    cleaned = []
    for index, block in enumerate(blocks):
        params = dict(block.get("params") or {})
        for key, value in list(params.items()):
            if isinstance(value, dict) and len(value) == 1 and submodels.ARG_KEY in value:
                problems.append(problem(
                    "E_SUBMODEL_ARG_UNKNOWN",
                    f"{block.get('name')}: '{key}' is bound to parameter "
                    f"'{value[submodels.ARG_KEY]}', but this block is not inside a submodel definition",
                    f"/blocks/{index}/params/{key}", block=block.get("name"),
                    hint="give the param a plain value, or move the block into a definition "
                         "that declares the parameter"))
                del params[key]
        cleaned.append({**block, "params": params})
    return {**layout, "blocks": cleaned}


def validate_layout_structured(layout: dict) -> list[Problem]:
    """Check every block's params against its registry schema, distribution
    names against the catalogue, links against declared block names, every
    section and every expression in the model, as coded Problems (ROADMAP 3.1
    item 1)."""
    problems: list[Problem] = []
    layout = _without_stray_structural_args(layout, problems)
    blocks = layout.get("blocks", [])
    names = {b["name"] for b in blocks}
    variables = _validate_variables(layout, problems)
    # The calendar first (PHASE4 T10): every dated field below resolves through
    # it, and a model without one skips all of that at zero cost.
    calendar_problems, calendar = validate_calendar_structured(layout)
    problems.extend(calendar_problems)
    horizon = run_horizon(layout, calendar) if calendar is not None else None
    schedule_problems, schedule_kinds = validate_schedules_structured(layout, calendar, horizon)
    problems.extend(schedule_problems)
    table_problems, table_dims = validate_tables_structured(layout)
    problems.extend(table_problems)
    series_problems, series_names = validate_timeseries_inputs_structured(layout, calendar, horizon)
    problems.extend(series_problems)
    level_problems, level_names, flow_names = validate_levels_structured(layout)
    problems.extend(level_problems)
    # C2: TRANSFER and an agent transfer action may not target a conveyor --
    # its value is the sum of its own cohorts, silently overwritten otherwise.
    conveyor_names = {l.get("name") for l in layout.get("levels", []) or []
                      if isinstance(l, dict) and l.get("conveyor")}
    sd_problems, aux_names, delay_names = sd.validate_structured(layout)     # PHASE5 SD-2
    problems.extend(sd_problems)
    problems.extend(units.check(layout))       # PHASE5 SD-3/PHASE6 S9: warn by default, off/strict
    conversion_problems, conversion_names, _components = validate_conversions_structured(
        layout)                                                             # Bio-1 G1
    problems.extend(conversion_problems)
    problems.extend(validate_fixed_costs_structured(layout))   # PHASE4 D5
    problems.extend(validate_energy_price_structured(layout, variables))   # PHASE6 W3 M1
    # What a literal TS()/LEVEL()/FLOW() call in any expression is checked
    # against (PHASE4 D1/D2), in the same shape as `table_dims` above.
    network_problems, node_names = validate_network_structured(layout)     # PHASE4 E1
    problems.extend(network_problems)
    fleet_problems, fleet_names = validate_fleets_structured(layout, node_names,
                                                            schedule_kinds)
    problems.extend(fleet_problems)
    places = set(block_nodes(layout))
    refs = {"TS": (series_names, "E_SERIES_UNKNOWN", LOOKUP_SECTION["TS"], "time series"),
            "LEVEL": (level_names, "E_LEVEL_UNKNOWN", LOOKUP_SECTION["LEVEL"], "level"),
            "FLOW": (flow_names, "E_FLOW_UNKNOWN", LOOKUP_SECTION["FLOW"], "flow"),
            "CONV": (conversion_names, "E_CONVERSION_UNKNOWN", LOOKUP_SECTION["CONV"], "conversion"),
            # VAR("name") reads a variable the bare name cannot spell (D72).
            "VAR": (variables, "E_VARIABLE_UNKNOWN", LOOKUP_SECTION["VAR"], "variable"),
            # DISTANCE("a", "b") names a station, an alias or a node (PHASE4 E1).
            "DISTANCE": (places, "E_DISTANCE_UNKNOWN", LOOKUP_SECTION["DISTANCE"], "place"),
            "AUX": (aux_names, "E_AUX_UNKNOWN", LOOKUP_SECTION["AUX"], "auxiliary"),     # PHASE5 SD-2
            "DELAY": (delay_names, "E_DELAY_UNKNOWN", LOOKUP_SECTION["DELAY"], "delay")}
    # COUNT/SUM/MEAN("population", ...) (PHASE5 AB-1): population -> its type
    # entry, so `_check_expression` also checks a literal state/attribute (#134).
    agent_types = {e.get("name"): e for e in layout.get("agent_types") or [] if isinstance(e, dict)}
    population_names = {p.get("name"): agent_types.get(p.get("type"))
                        for p in layout.get("populations") or [] if isinstance(p, dict)}
    for func in functions_with("population"):
        refs[func] = (population_names, "E_POPULATION_UNKNOWN", "populations", "population")
    problems.extend(agents.validate_structured(
        layout, variables,
        lambda who, text, names, path: _check_expression(problems, who, text, names, path,
                                                         tables=table_dims, refs=refs,
                                                         allow_self=True)))
    _validate_travel(layout, problems, node_names, fleet_names)            # PHASE4 E1
    problems.extend(validate_workers_structured(layout))                   # PHASE6 W3-M3
    _validate_flow_expressions(layout, problems, variables, table_dims, refs)
    _validate_sd_expressions(layout, problems, variables, table_dims, refs)
    validate_conversion_expressions(
        layout, variables,
        lambda who, text, names, path: _check_expression(problems, who, text, names, path,
                                                         tables=table_dims, refs=refs))
    sequence_problems, sequence_steps = validate_sequences_structured(layout)
    problems.extend(sequence_problems)
    problems.extend(validate_orders_structured(layout, sequence_steps, calendar))
    problems.extend(validate_resources_structured(layout, schedule_kinds))
    _validate_entities(layout, problems)
    _validate_failures(layout, problems)
    _validate_record_stats(layout, problems)
    _validate_resource_refs(layout, problems)                             # #284
    _validate_queue_behaviour(layout, problems)
    _validate_blocking(layout, problems, level_names)                     # PHASE4 T5
    _validate_match(layout, problems)                                     # PHASE5 O4
    _validate_scenarios(layout, problems)
    _validate_reality_checks(layout, problems, variables, table_dims, refs)   # PHASE6 S8
    _validate_assertions(layout, problems)                                   # PHASE6 W7 Q3
    _validate_dashboard(layout, problems, variables, table_dims, refs)       # PHASE6 G1
    # The sections' own hooks (core/sections.py), in row order. Everything
    # above stays wired by hand: those validators read other sections' names
    # (variables, calendar, schedule kinds, tables, refs, ...), and moving them
    # into row order would change the order problems are reported in.
    for section in SECTIONS:
        if section.validate:
            module, _, function = section.validate.partition(":")
            getattr(import_module(module), function)(layout, problems)
    deterministic = bool(layout.get("simulation", {}).get("deterministic"))   # PHASE6 W4 P1, D119

    seen_names = set()
    for index, block in enumerate(blocks):
        name = block.get("name", "<unnamed>")
        btype = block.get("type", "").upper()
        params = block.get("params", {})
        at = f"/blocks/{index}"

        for text in validate_params(btype, params):
            problems.append(_param_problem(text, name, btype, str(index)))
        # A block expanded out of a submodel instance carries its parameters in
        # "locals" (ROADMAP 2.3 part B); they are legal names in its expressions.
        in_scope = variables | set(block.get("locals") or {})
        _check_block_expressions(problems, name, btype, params, in_scope, f"{at}/params",
                                 tables=table_dims, refs=refs)
        if btype == "TRANSFER":
            _check_transfer(problems, name, params, level_names, conveyor_names, in_scope,
                            table_dims, refs, f"{at}/params")
        elif btype == "SEND":                                               # PHASE5 AB-2
            _check_send(problems, name, params, layout, in_scope, table_dims, refs,
                        f"{at}/params")

        if btype == "CREATE":
            _check_arrival_mode(problems, name, params, schedule_kinds, f"{at}/params")
            pop_name = params.get("population", "")           # H2, D130
            if pop_name and pop_name not in population_names:
                problems.append(problem(
                    "E_POPULATION_UNKNOWN", f"{name}: unknown population '{pop_name}'",
                    f"{at}/params/population", block=name,
                    hint=f"declared: {', '.join(sorted(map(str, population_names))) or '(none)'}"))
        _check_sequence_names(problems, name, btype, params, sequence_steps, f"{at}/params")

        dist = params.get("distribution")
        if dist is not None and dist not in _known_distributions():
            problems.append(problem("E_UNKNOWN_DISTRIBUTION", f"{name}: unknown distribution '{dist}'",
                                    f"{at}/params/distribution", block=name, hint=_dist_hint()))
        if btype in _DURATION_BLOCK_TYPES and dist in _UNBOUNDED_BELOW_DISTRIBUTIONS:
            problems.append(problem(
                "W_UNBOUNDED_DURATION",
                f"{name}: {dist} can produce negative durations; they are truncated to 0",
                f"{at}/params/distribution", block=name,
                hint="use BoundedNormal, Lognormal or Gamma for a strictly positive duration"))
        if btype in _DURATION_BLOCK_TYPES and dist is not None:
            _check_deterministic_mean(problems, deterministic, dist, params.get("dist_params", {}),
                                      name, f"{at}/params/dist_params", block=name)
        if dist == "EmpiricalContinuous":
            dp = params.get("dist_params", {})
            values, probs = dp.get("values", []), dp.get("probs", [])
            if values and probs and len(values) == len(probs):
                problems.append(problem(
                    "W_EMPIRICAL_CONTINUOUS",
                    f"{name}: EmpiricalContinuous with len(values) == len(probs) samples "
                    f"discrete points (pre-1.8 behaviour); use EmpiricalDiscrete, or give "
                    f"len(values) == len(probs) + 1 bin edges for true continuous sampling",
                    f"{at}/params/dist_params", block=name,
                    hint="switch to EmpiricalDiscrete, or add one more bin edge to \"values\""))

        if name in seen_names:
            problems.append(problem("E_DUPLICATE_BLOCK", f"duplicate block name: {name}",
                                    f"{at}/name", block=name,
                                    hint="block names are the model's identifiers: rename one"))
        seen_names.add(name)

    for index, (src, tgt) in enumerate(_link_pairs(layout)):
        for side, other in (("source", src), ("target", tgt)):
            if other not in names:
                problems.append(problem(
                    "E_LINK_UNKNOWN_BLOCK", f"link references unknown block: {other}",
                    f"/links/{index}/{side}",
                    hint=f"add a block named '{other}', or point the link at an existing one: "
                         f"{', '.join(sorted(names)) or '(no blocks)'}"))

    _validate_link_delays(layout, problems)

    return problems


# Block types whose "distribution" param is sampled as a SimPy timeout
# (BaseProcess.sample_duration truncates a negative sample to 0 at run time).
_DURATION_BLOCK_TYPES = set(duration_types())   # from each SPEC.duration flag (E2)
# Distributions that can sample below 0 -- a duration warning is advisory, not
# a hard error, since a mean far enough above 0 rarely truncates in practice.
_UNBOUNDED_BELOW_DISTRIBUTIONS = {"Normal", "Cauchy", "JohnsonSU"}
