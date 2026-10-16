"""What the `dashboard` section means to the engine (D243): the rules of Present
mode that must hold with no GUI.

    resolve_dashboard(layout)        the definition a viewer gets (stored, else generated)
    input_overrides(layout, values)  a viewer's values as overrides, each checked here
    generated_widgets(layout)        the charts shown for a model that declares none

`inputs` are the overrides a person who did not build the model may make, with
their bounds; `results` the KPIs to report first; `limits` what a run on that
person's behalf may cost; `pages` and positions are hints for whoever draws it.
A what-if is a scenario nobody saved: append `input_overrides(...)` as one
scenario and call `experiments.run_scenarios`.

Top-level, like `experiments` (whose path grammar it reads); the section's
shape and ceilings are core's (`core/schema/sections_validation.py`).
"""
from __future__ import annotations

import copy
import math

from nagasim import experiments
from nagasim.core import sections, submodels
from nagasim.core.problems import Problem, problem
from nagasim.core.schema import ModelValidationError
from nagasim.core.schema.sections_validation import (CONNECTION_SOURCE_KINDS, DASHBOARD_LIMITS,
                                                     validate_dashboard_definition)

__all__ = ["default_dashboard", "generated_dashboard", "generated_widgets", "resolve_dashboard",
           "merge_generated",
           "input_overrides", "input_for", "check_inputs", "MAX_GENERATED_INPUTS"]

# The generation rule's numbers (the spec's section 3), one constant each.
MAX_GENERATED_INPUTS = 12
_CHART_LEVELS, _CHART_QUEUES = 4, 8     # series of the generated line and bar charts
_RANGE_STEPS = 50               # a generated non-integer range is cut in about this many
# From 3: a 95% interval from two replications has one degree of freedom.
_DEFAULT_REPLICATIONS, _MAX_REPLICATIONS = (3, 10), 30
_PROCESS_RESULTS = ("system/Avg Throughput Time", "system/Throughput (per time unit)",
                    "system/Avg WIP (Little)", "system/Avg Resource Usage %")
_COST_RESULT = "system/Total Cost"
# A slider control's target -> the override path of the same value before a run.
_CONTROL_PATHS = {"capacity": "resources.{}.capacity", "variable": "variables.{}.initial",
                  "constant": "auxiliaries.{}.expr"}
# merge_generated: (list key, the entry's key, its tag in `hidden`, the activity whose page
# gets a new entry as an item).
_MERGED = (("inputs", "path", "input", "what_if"), ("results", "kpi", "result", "what_if"),
           ("pages", "name", "page", None))


# -- generation (D244) ----------------------------------------------------------
def _as_number(leaf):
    """*leaf* as a finite number: a number, or the text of a number literal (an
    auxiliary's `expr` is a string). None for anything else, a boolean included."""
    if isinstance(leaf, bool):
        return None
    if isinstance(leaf, str):
        try:
            leaf = int(leaf)
        except ValueError:
            try:
                leaf = float(leaf)
            except ValueError:
                return None
    if isinstance(leaf, (int, float)) and abs(leaf) < math.inf:    # an int of any size (#373)
        return leaf
    return None


def _nice_step(span: float) -> float | None:
    """span / 50 rounded down to 1, 2 or 5 times a power of ten; None for a span
    so small that the division underflows to 0 (a denormal initial value)."""
    raw = span / _RANGE_STEPS
    if raw <= 0:
        return None
    exponent = math.floor(math.log10(raw))
    # Through text, so 2 * 10**-2 is 0.02 and not 0.020000000000000004.
    return max(step for step in (float(f"{m}e{exponent}") for m in (1, 2, 5)) if step <= raw)


def _number_input(path: str, label: str, value, capacity: bool = False) -> dict | None:
    if value > 0:
        low, high = (1, max(2 * value, value + 2)) if capacity else (0, 2 * value)
    elif value == 0:
        low, high = 0, 10
    else:
        low, high = 2 * value, 0
    if max(abs(low), abs(high)) > DASHBOARD_LIMITS["magnitude"]:
        return None
    step = 1 if isinstance(value, int) and high - low <= DASHBOARD_LIMITS["steps"] \
        else _nice_step(high - low)
    if step is None:
        return None
    if isinstance(value, int):
        step = int(step)
    return {"path": path, "label": label, "kind": "number", "min": low, "max": high, "step": step}


def _entry(path: str, label: str, value, capacity: bool = False) -> dict | None:
    """The input the generation rule makes of a leaf: a switch for true/false, a
    number with its generated range for a number, None for anything else."""
    if isinstance(value, bool):
        return {"path": path, "label": label, "kind": "switch"}
    number = _as_number(value)
    return None if number is None else _number_input(path, label, number, capacity)


def input_for(layout: dict, path: str) -> dict | None:
    """The input the generation rule would make of *path*, labelled with the
    path: what the designer adds when the author picks a parameter. None when the
    path addresses nothing, or no number or true/false value."""
    try:
        leaf = experiments.value_at(layout, path)
    except (ModelValidationError, KeyError):
        return None
    label = path[:DASHBOARD_LIMITS["label"]] if path.isprintable() else ""
    return _entry(path, label, leaf, path.startswith("resources.") and path.endswith(".capacity"))


def _templates(layout: dict) -> dict[str, set]:
    """{section: names} of the records a submodel definition clones per instance
    (`scoped`, or `info.owns` for a placed asset). The run reads the clones, which
    take their values from each instance's arguments: overriding the top-level
    record changes nothing, so it is no input."""
    found: dict[str, set] = {}
    for definition in (layout.get("submodels") or {}).values():
        if not isinstance(definition, dict):
            continue
        for owned in (definition.get("scoped"), (definition.get("info") or {}).get("owns")):
            for section, names in (owned or {}).items():
                found.setdefault(section, set()).update(names)
    return found


def _generated_inputs(layout: dict, controls: list) -> list[dict]:
    """In file order: the declared variable and constant sliders (their range
    and title win), then every variable with a number or true/false initial
    value, every resource's capacity, every auxiliary that is a number literal,
    then every number or true/false argument a submodel instance sets."""
    found: dict[str, dict] = {}
    templates = _templates(layout)

    def add(path: str, label: str, value, capacity: bool = False) -> None:
        try:
            experiments.value_at(layout, path)
        except (ModelValidationError, KeyError):
            return                          # a name the grammar cannot address (a dot in it)
        if len(path) > DASHBOARD_LIMITS["text"] or not path.isprintable() or path in found:
            return
        label = label[:DASHBOARD_LIMITS["label"]] if label.isprintable() else ""
        entry = _entry(path, label, value, capacity)
        if entry is not None:
            found[path] = entry

    # A capacity slider caps a resource within its declared capacity during a
    # run, and validation holds its range inside that capacity: an input that
    # lowered the capacity would be refused, so the slider is that resource's knob.
    capped = {c["target"].get("capacity") for c in controls if c.get("kind") == "slider"}
    for control in controls:
        if control.get("kind") == "slider" and "capacity" not in control["target"]:
            (kind, name), = control["target"].items()
            path = _CONTROL_PATHS[kind].format(name)
            try:
                leaf = experiments.value_at(layout, path)
            except (ModelValidationError, KeyError):
                continue
            if _as_number(leaf) is not None:    # a slider on an expression is no number input
                add(path, str(control.get("title") or control["name"]), 0)
            if path in found:
                found[path].update(min=control["min"], max=control["max"],
                                   step=control.get("step", 1))
    for section, field, capacity in (("variables", "initial", False),
                                     ("resources", "capacity", True),
                                     ("auxiliaries", "expr", False)):
        for record in layout.get(section) or []:
            name = record.get("name")
            if isinstance(name, str) and name and name not in templates.get(section, ()) \
                    and not (capacity and name in capped):
                add(f"{section}.{name}.{field}", name, record.get(field), capacity)
    for block in layout.get("blocks") or []:
        arguments = (block.get("params") or {}).get("arguments")
        if block.get("type") == "SUBMODEL" and isinstance(arguments, dict):
            for parameter, value in arguments.items():
                add(f"blocks.{block.get('name')}.params.arguments.{parameter}",
                    f"{block.get('name')} {parameter}", value)
    return list(found.values())[:MAX_GENERATED_INPUTS]


def default_dashboard(layout: dict) -> dict:
    """The definition generated from *layout*: deterministic, in file order, pure.

    What the section already stores wins key by key: its widgets and controls
    always, and its `inputs`, `results` and `limits` when present, so an author
    may write those by hand and let the pages follow. Left out of the generated
    inputs: block parameters, distributions, schedules, failures, the network,
    costs, text and expression fields, anything inside a submodel definition.
    No widget is generated. *layout* is a validated one (`check_inputs`)."""
    stored = layout.get("dashboard") or {}
    controls = copy.deepcopy(stored.get("controls") or [])
    widgets = copy.deepcopy(stored.get("widgets") or [])
    inputs = copy.deepcopy(stored["inputs"]) if "inputs" in stored \
        else _generated_inputs(layout, controls)
    if "results" in stored:
        results = copy.deepcopy(stored["results"])
    else:
        keys = _PROCESS_RESULTS if "process" in sections.capabilities(layout) else ()
        if keys and layout.get("fixed_costs"):
            keys += (_COST_RESULT,)
        results = [{"kpi": key, "label": key.partition("/")[2]} for key in keys]
    simulation = layout.get("simulation") or {}
    low, high = _DEFAULT_REPLICATIONS
    limits = {"replications": min(max(int(simulation.get("replications") or 1), low), high),
              "max_replications": _MAX_REPLICATIONS, **copy.deepcopy(stored.get("limits") or {})}
    limits["replications"] = min(limits["replications"], limits["max_replications"])

    pages = []
    if inputs:
        pages.append({"name": "What-if", "activity": "what_if", "items": [
            *({"kind": "input", "ref": entry["path"]} for entry in inputs), {"kind": "table"}]})
    if len(experiments.scenarios_of(layout)) > 1:
        pages.append({"name": "Scenarios", "activity": "scenarios",
                      "items": [{"kind": "scenarios"}, {"kind": "table"}]})
    pages.append({"name": "Play", "activity": "play", "items": [
        {"kind": "view", "mode": "schematic"}, {"kind": "decisions"},
        *({"kind": "control", "ref": c["name"]} for c in controls),
        *({"kind": "widget", "ref": w["name"]} for w in widgets)][:DASHBOARD_LIMITS["items"]]})
    # A model a twin can watch: what a separately started one wrote (D249). A forecast,
    # and the advice made from it, only for a model that can start from a state.
    if any(isinstance(s, dict) and s.get("kind") in CONNECTION_SOURCE_KINDS
           for s in layout.get("data_sources") or []):
        items = [{"kind": "status"}, {"kind": "view", "mode": "schematic"}]
        if layout.get("data_bindings") or layout.get("initial_state"):
            items += [{"kind": "forecast"}, {"kind": "recommendations"}]
        pages.append({"name": "Live", "activity": "live", "items": items})
    return {"widgets": widgets, "controls": controls, "inputs": inputs, "results": results,
            "limits": limits, "pages": pages}


def generated_widgets(layout: dict) -> list[dict]:
    """Rule 3: the charts Present mode shows for a model that declares no widget,
    else none: a line of the first 4 levels and a bar of the queues of the first 8
    resources. Over the FLATTENED names (an asset's level runs as
    `<instance>/<name>`), less the templates an asset clones (`_templates`): the
    names a run reads. Not part of the definition, so nothing is stored or merged:
    the Presenter adds them to its own copy of the model. *layout* is validated."""
    if (layout.get("dashboard") or {}).get("widgets"):
        return []
    sim = layout.get("simulation") or {}
    if not (sim.get("time_limit") or sim.get("timeseries_interval")):
        return []       # a draining run records no samples (sampling is off): the chart would stay empty
    try:
        flat = submodels.flatten(layout)
    except submodels.SubmodelError:
        return []
    templates = _templates(layout)

    def names(section: str, most: int) -> list[str]:
        # A quote or a backslash would end the literal early: such a name gets no series.
        return [r["name"] for r in flat.get(section) or [] if isinstance(r, dict)
                and isinstance(r.get("name"), str) and r["name"] and not r.get("hidden")
                and r["name"] not in templates.get(section, ())
                and not {'"', "\\"} & set(r["name"]) and r["name"].isprintable()][:most]
    widgets = []
    for name, kind, function, found in (
            ("Levels", "line", "LEVEL", names("levels", _CHART_LEVELS)),
            ("Queues", "bar", "NQ", names("resources", _CHART_QUEUES))):
        if found:
            widgets.append({"name": name, "kind": kind, "series": [
                {"label": n, "expr": f'{function}("{n}")'} for n in found]})
    return widgets


def generated_dashboard(layout: dict) -> dict:
    """What the model alone generates, whatever the author stored besides its
    widgets and controls: the side Regenerate merges in and Reset returns to."""
    stored = layout.get("dashboard") or {}
    own = {key: stored[key] for key in ("widgets", "controls") if key in stored}
    return default_dashboard({**layout, "dashboard": own})


def resolve_dashboard(layout: dict) -> dict:
    """The stored definition when it has pages, else the generated one (D244):
    a model nobody customised keeps following the model."""
    stored = layout.get("dashboard") or {}
    return copy.deepcopy(stored) if stored.get("pages") else default_dashboard(layout)


def merge_generated(stored: dict, generated: dict) -> tuple[dict, list[str]]:
    """Regenerate (D245): add every generated input, result and page that
    *stored* neither holds nor lists in `hidden`; never touch a stored entry.
    Returns (the merged definition, the tags added, "input:<path>" style)."""
    merged = copy.deepcopy(stored)
    hidden, added = set(merged.get("hidden") or []), []
    for key, field, tag, activity in _MERGED:
        have = {entry[field] for entry in merged.get(key) or []}
        for entry in generated.get(key) or []:
            name = f"{tag}:{entry[field]}"
            if entry[field] in have or name in hidden:
                continue
            merged.setdefault(key, []).append(copy.deepcopy(entry))
            added.append(name)
            page = next((p for p in merged.get("pages") or []
                         if p.get("activity") == activity), None)
            if page is not None:
                page.setdefault("items", []).append({"kind": tag, "ref": entry[field]})
    return merged, added


# -- a viewer's value (D246) -----------------------------------------------------
def _leaf_problem(layout: dict, entry: dict) -> str | None:
    """Why *entry* cannot be an input of *layout*, or None."""
    try:
        leaf = experiments.value_at(layout, entry["path"])
    except (ModelValidationError, KeyError) as exc:
        return str(exc)
    kind = entry["kind"]
    if kind == "number" and _as_number(leaf) is None:
        return "a number input needs a number there"
    if kind == "switch" and not isinstance(leaf, bool):
        return "a switch needs a true/false value there"
    if kind == "choice" and not all(
            type(c["value"]) is type(leaf)
            or (_as_number(leaf) is not None and not isinstance(leaf, str)
                and not isinstance(c["value"], (bool, str)))
            for c in entry["choices"]):
        return "every choice needs a value of the type found there"
    return None


def check_inputs(layout: dict) -> list[Problem]:
    """Every problem of the stored definition: its shape and ceilings, then
    each input's path and kind against THIS model (the path grammar is
    `experiments`', which core cannot import)."""
    problems = validate_dashboard_definition(layout)
    if problems:
        return problems
    for index, entry in enumerate((layout.get("dashboard") or {}).get("inputs") or []):
        reason = _leaf_problem(layout, entry)
        if reason:
            problems.append(problem(
                "E_DASHBOARD_INPUT", f"dashboard input {index}: {reason[:200]}",
                f"/dashboard/inputs/{index}/path",
                hint="an input is an override path whose value has the input's kind"))
    return problems


def _checked(entry: dict, leaf, value):
    """*value* as the override of *entry*, or ValueError. The viewer's object is
    never passed on as it came: a number is rebuilt in the leaf's own type."""
    shown = repr(value)[:40]
    kind = entry["kind"]
    if kind == "switch":
        if not isinstance(value, bool):
            raise ValueError(f"input '{entry['path']}': {shown} is not true or false")
        return value
    if kind == "choice":
        for choice in entry["choices"]:
            if type(choice["value"]) is type(value) and choice["value"] == value:
                return copy.deepcopy(choice["value"])
        raise ValueError(f"input '{entry['path']}': {shown} is not one of its choices")
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not entry["min"] <= value <= entry["max"]:      # NaN fails the comparison
        raise ValueError(f"input '{entry['path']}': {shown} is not a number within "
                         f"[{entry['min']}, {entry['max']}]")
    whole = float(value).is_integer()
    if isinstance(leaf, int) and not whole:
        raise ValueError(f"input '{entry['path']}': {shown} is not a whole number")
    number = int(value) if whole and not isinstance(leaf, float) else float(value)
    return repr(number) if isinstance(leaf, str) else number


def input_overrides(layout: dict, values: dict) -> list[dict]:
    """{input path: value} as scenario overrides, in the order given. Raises
    ValueError for a definition with a problem, a path that is not an input of
    this model, or a value outside its input: the range a slider shows is
    enforced here, not by the slider."""
    problems = check_inputs(layout)
    if problems:
        raise ValueError("; ".join(p.message for p in problems[:5]))
    inputs = {entry["path"]: entry for entry in resolve_dashboard(layout)["inputs"]}
    overrides = []
    for path, value in values.items():
        entry = inputs.get(path)
        if entry is None:
            raise ValueError(f"{str(path)[:80]!r} is not an input of this model")
        overrides.append({"path": path,
                          "value": _checked(entry, experiments.value_at(layout, path), value)})
    return overrides
