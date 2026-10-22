"""Validators of the sections whose meaning another module owns but which
cannot live next to it: scenarios, reality checks and assertions (owned by the
top-level `experiments`/`modelcheck`), the dashboard (sampled by
`core/timeseries.py`, but checked with the shared expression check), data
sources/bindings (read by the top-level `databind`) and the travel blocks
(ROUTE/TRANSPORT/CONVEYOR over `core/network.py`; a move there would cycle
through the block package). core/ never imports a top-level module, so their
file-format names (`DATA_SOURCE_KINDS`, ...) are declared here."""
from __future__ import annotations

import re
from dataclasses import replace

from nagasim.core.blocks.route import TRAVEL_MODES
from nagasim.core.expressions import (ExpressionError, ExpressionScope, compile_expression,
                                      functions_with)
from nagasim.core.network import block_nodes, unreachable_pairs
from nagasim.core.problems import problem
from nagasim.core.schema.common import _check_expression, _link_pairs
from nagasim.core.schema.migrate import CONSENT_KEYS
from nagasim.core.sequences import station_blocks
from nagasim.core.utils import is_finite_number


# ROADMAP 3.4a. "csv"/"xlsx"/"sqlite" read a file beside the model; "sql" needs
# a SQLAlchemy URL and "json_url" an http(s) URL, and both are refused unless
# the CALLER allows them (`nagasim.netguard.NetworkPolicy`, set only by a CLI
# flag, a server start argument or the editor's consent dialog). Consent is
# never read from the model file: see `_strip_consent`.
DATA_SOURCE_KINDS = ("csv", "xlsx", "sqlite", "sql", "json_url", "rest", "mqtt", "opcua",
                     "modbus")
REMOTE_SOURCE_KINDS = ("sql", "json_url")
# PHASE7 7.8 (D215). A LIVE source names a `connection` the caller defined and never a
# host: its location is a path, a topic or a node id, and its options are the reader's
# own list below (never passed through to a library). The readers themselves live in
# `nagasim/readers/`, which core does not import; a test keeps the two lists equal.
CONNECTION_SOURCE_KINDS = ("rest", "mqtt", "opcua", "modbus")
LIVE_SOURCE_OPTIONS = {"rest": ("records", "key", "time", "poll"),
                       "mqtt": ("payload", "time", "qos", "settle"),
                       "opcua": ("interval_ms", "settle"), "modbus": ("poll",)}


def _validate_scenarios(layout: dict, problems: list) -> None:
    """Shape only (ROADMAP 1.6): unique non-empty names and well-formed
    overrides. Whether a `path` actually addresses something in THIS model is
    checked by experiments.apply_overrides -- core/ must not import the
    pandas-side experiment module."""
    seen: set[str] = set()
    for index, entry in enumerate(layout.get("scenarios", [])):
        at = f"/scenarios/{index}"
        name = entry.get("name", "")
        if not name:
            problems.append(problem("E_SCENARIO_NAME", "scenario: name must not be empty",
                                    f"{at}/name", hint="give every scenario a unique name"))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_SCENARIO", f"duplicate scenario name: {name}",
                                    f"{at}/name", hint="rename one of the two scenarios"))
        seen.add(name)
        goal = entry.get("goal", "")       # D577/D578: optional, text only
        if not isinstance(goal, str) or len(goal) > 500:
            problems.append(problem(
                "E_SCENARIO_GOAL", f"scenario '{name}': goal must be text of at most 500 characters",
                f"{at}/goal", hint="write the question the scenario answers in one sentence"))
        for j, override in enumerate(entry.get("overrides", [])):
            where = f"{at}/overrides/{j}"
            if not override.get("path"):
                problems.append(problem(
                    "E_SCENARIO_OVERRIDE", f"scenario '{name}': an override has no path",
                    f"{where}/path",
                    hint="an override is {\"path\": \"resources.Server.capacity\", \"value\": 2}"))
            elif "value" not in override:
                problems.append(problem(
                    "E_SCENARIO_OVERRIDE",
                    f"scenario '{name}': override '{override['path']}' has no value",
                    f"{where}/value", hint="add the \"value\" this override sets"))


_INF = float("inf")
_WHEN_NUMBER = r"(-?\d+(?:\.\d+)?)"
_WHEN_WINDOW = re.compile(
    rf"after {_WHEN_NUMBER}|before {_WHEN_NUMBER}|between {_WHEN_NUMBER} and {_WHEN_NUMBER}")
WHEN_FORMS = ('"always", "final", "after <t>", "before <t>", "between <t1> and <t2>", or '
              '"eventually" alone or followed by one of the three windows')


def parse_when(when) -> tuple[str, float, float]:
    """A reality check's "when" as (mode, lo, hi) (D677): *mode* is "always" (the constraint
    must hold at every sample in [lo, hi]), "final" (at the last sample) or "eventually" (at
    least once in [lo, hi]); a window with no start or end has -inf or inf there. The one
    parser for the validator, the sample mask and the live probe. ValueError on a text outside
    the grammar and on t1 > t2."""
    text = (" ".join(when.split()) or "always") if isinstance(when, str) else ""
    if text in ("always", "final", "eventually"):
        return text, -_INF, _INF
    mode, window = (("eventually", text[len("eventually "):]) if text.startswith("eventually ")
                    else ("always", text))
    found = _WHEN_WINDOW.fullmatch(window)
    if found is None:
        raise ValueError(f'unknown "when" {when!r}; use {WHEN_FORMS}')
    after, before, start, end = found.groups()
    if after is not None:
        return mode, float(after), _INF
    if before is not None:
        return mode, -_INF, float(before)
    if float(start) > float(end):
        raise ValueError(f'"when" {when!r} ends before it starts (t1 is later than t2)')
    return mode, float(start), float(end)


def _validate_reality_checks(layout: dict, problems: list, variables: set[str],
                             tables: dict, refs: dict) -> None:
    """Shape only, like `_validate_scenarios` (PHASE6 S8): unique non-empty
    names, an "inputs" map of non-empty override paths (whether a path
    actually addresses something in THIS model is `experiments.apply_overrides`'s
    job, same reasoning as a scenario override), a "constraint" that compiles
    against the same LEVEL/AUX/FLOW/TNOW names a flow or auxiliary can use, and
    a "when" `parse_when` accepts."""
    seen: set[str] = set()
    for index, entry in enumerate(layout.get("reality_checks", [])):
        at = f"/reality_checks/{index}"
        name = entry.get("name", "")
        if not name:
            problems.append(problem("E_REALITY_CHECK_NAME", "reality check: name must not be "
                                    "empty", f"{at}/name",
                                    hint="give every reality check a unique name"))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_REALITY_CHECK",
                                    f"duplicate reality check name: {name}", f"{at}/name",
                                    hint="rename one of the two reality checks"))
        seen.add(name)
        inputs = entry.get("inputs", {})
        if not isinstance(inputs, dict) or not all(isinstance(p, str) and p for p in inputs):
            problems.append(problem(
                "E_REALITY_CHECK_INPUT",
                f"reality check '{name}': \"inputs\" must be an object of non-empty override "
                f"path: value", f"{at}/inputs",
                hint="{\"auxiliaries.Workforce.expr\": \"0\"}, same paths a scenario uses"))
        try:
            parse_when(entry.get("when", "always"))
        except ValueError as exc:
            problems.append(problem("E_REALITY_CHECK_WHEN", f"reality check '{name}': {exc}",
                                    f"{at}/when"))
        constraint = entry.get("constraint", "")
        if not constraint:
            problems.append(problem(
                "E_EXPRESSION", f"reality check '{name}': constraint must not be empty",
                f"{at}/constraint", hint="a boolean expression, e.g. AUX(\"Production\") <= 0"))
        else:
            _check_expression(problems, f"reality check '{name}'", constraint, variables,
                              f"{at}/constraint", tables=tables, refs=refs)


_ASSERTION_OPS = ("<", "<=", ">", ">=", "==")


def _validate_assertions(layout: dict, problems: list) -> None:
    """Shape only, like `_validate_scenarios` (PHASE6 W7 Q3): unique non-empty
    names, a non-empty "kpi" (whether it names a KPI this model actually
    produces is `nagasim.modelcheck`'s job at run time -- the KPI key space
    depends on which frames a run fills, e.g. per-resource columns, which
    `core/` cannot see), one of the five comparison operators, "replications"
    at least 1 and "confidence" strictly between 0 and 1."""
    seen: set[str] = set()
    for index, entry in enumerate(layout.get("assertions", [])):
        at = f"/assertions/{index}"
        name = entry.get("name", "")
        if not name:
            problems.append(problem("E_ASSERTION_NAME", "assertion: name must not be empty",
                                    f"{at}/name", hint="give every assertion a unique name"))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_ASSERTION", f"duplicate assertion name: {name}",
                                    f"{at}/name", hint="rename one of the two assertions"))
        seen.add(name)
        if not entry.get("kpi"):
            problems.append(problem("E_ASSERTION_KPI", f"assertion '{name}': \"kpi\" must not "
                                    "be empty", f"{at}/kpi",
                                    hint="a KPI key from experiments.flatten_kpis, e.g. "
                                        "\"system/Throughput (per time unit)\""))
        op = entry.get("op")
        if op not in _ASSERTION_OPS:
            problems.append(problem("E_ASSERTION_OP", f"assertion '{name}': unknown \"op\" "
                                    f"{op!r}", f"{at}/op",
                                    hint=f"use one of {', '.join(_ASSERTION_OPS)}"))
        replications = entry.get("replications", 5)
        if not isinstance(replications, int) or isinstance(replications, bool) or replications < 1:
            problems.append(problem("E_ASSERTION_REPLICATIONS",
                                    f"assertion '{name}': \"replications\" must be an integer >= 1",
                                    f"{at}/replications"))
        confidence = entry.get("confidence", 0.95)
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
                or not 0 < confidence < 1:
            problems.append(problem("E_ASSERTION_CONFIDENCE",
                                    f"assertion '{name}': \"confidence\" must be between 0 and 1",
                                    f"{at}/confidence"))


_DASHBOARD_KINDS = ("kpi", "gauge", "line", "bar")
_DASHBOARD_SERIES_LIMITS = {"line": 4, "bar": 8}
_SAMPLER_CALLS = frozenset(functions_with("sampler"))


def _check_dashboard_expression(problems: list, who: str, text, variables: set[str], path: str,
                                tables: dict, refs: dict, resources: set) -> None:
    """The same checks a block expression gets (`_check_expression`): compiles
    against the model's variables, and every TS/LEVEL/FLOW/AUX/VAR literal name
    is checked against its own section (their codes, e.g. E_LEVEL_UNKNOWN,
    are left as is). A bare compile failure or unknown name -- `_check_
    expression`'s generic E_EXPRESSION -- is relabelled E_DASHBOARD_EXPR, the
    widget's own code, so a dashboard-editing tool can act on it directly.

    On top of that, what could never evaluate in the sampler (DEVLOG #228)
    is E_DASHBOARD_EXPR too: a literal resource name *resources* lacks, a
    random draw (the sampler has no stream) and a constant that is not a
    number. A read that fails only now and then stays a NaN at run time."""
    before = len(problems)
    _check_expression(problems, who, text, variables, path, tables=tables, refs=refs)
    for i in range(before, len(problems)):
        if problems[i].code == "E_EXPRESSION":
            problems[i] = replace(problems[i], code="E_DASHBOARD_EXPR")
    try:
        expression = compile_expression(text, variables)
    except ExpressionError:
        return                  # reported just above

    def bad(message: str, hint: str | None = None) -> None:
        problems.append(problem("E_DASHBOARD_EXPR", f"{who}: {text}: {message}", path, hint=hint))
    for func in functions_with("resource_state"):
        for name in expression.refs.get(func, ()):
            if name not in resources:
                bad(f"unknown resource '{name}'", "a resource set is read through its members; "
                    f"declared resources: {', '.join(sorted(resources)) or '(none)'}")
    for func in sorted(expression.calls & _SAMPLER_CALLS):
        bad(f"{func}() draws a random number, and the dashboard sampler has no random stream")
    if not expression.names and not expression.calls:
        try:
            float(expression.evaluate(ExpressionScope()))
        except (ExpressionError, TypeError, ValueError, ArithmeticError):
            bad("a constant that is not a number")


def _validate_dashboard_gauge(widget: dict, problems: list, name: str, at: str) -> None:
    """A gauge's "min"/"max"/"thresholds" (PHASE6 G1): max > min, and every
    threshold a number inside [min, max] in ascending order."""
    minimum = widget.get("min", 0)
    maximum = widget.get("max")
    numeric = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if not numeric(maximum):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': gauge needs a numeric "
                                f"\"max\"", f"{at}/max"))
        return
    if not numeric(minimum) or maximum <= minimum:
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': \"max\" must be greater "
                                f"than \"min\"", f"{at}/max"))
        return
    thresholds = widget.get("thresholds") or []
    if not all(numeric(t) for t in thresholds):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': thresholds must be "
                                f"numbers", f"{at}/thresholds"))
    elif thresholds != sorted(thresholds):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': thresholds must be "
                                f"ascending", f"{at}/thresholds"))
    elif any(t < minimum or t > maximum for t in thresholds):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': thresholds must lie "
                                f"within [min, max]", f"{at}/thresholds"))


# -- Present mode (D242, D251): the dashboard section's `inputs`, `results`,
# `limits`, `pages` and `hidden`. A model file is data and may be hostile, so
# every count, range and length has a ceiling here; `modelschema.py` reads the
# same numbers, and `nagasim/dashboard.py` holds the rules that need the
# override grammar (core/ must not import the experiment module).
DASHBOARD_LIMITS = {"widgets": 64, "controls": 64, "inputs": 32, "results": 32, "pages": 8,
                    "hidden": 200, "items": 40, "choices": 20, "names": 64, "label": 80,
                    "text": 200, "steps": 10_000, "magnitude": 1e12, "replications": 1000,
                    "grid": 100}
DASHBOARD_INPUT_KINDS = ("number", "switch", "choice")
DASHBOARD_VIEW_MODES = ("schematic", "layout", "3d")
# activity -> the item kinds a page of it may hold. "live" (2026-10-16) reads what a
# separately started twin wrote (`twin.json`, D249): its state and sources, the plant
# on the view, the forecast with its accuracy, the recommendations. No "events": a
# twin read from another process hands over none (D279). An addition to v29, not a
# new version: every v29 file means what it meant, and an older build refuses a live
# page by name rather than misreading it.
DASHBOARD_ACTIVITIES = {
    "what_if": ("input", "result", "widget", "table", "view"),
    "scenarios": ("scenarios", "result", "widget", "table", "view"),
    "play": ("view", "decisions", "control", "widget", "result", "table"),
    "live": ("status", "view", "forecast", "recommendations"),
}
# The run settings a viewer's results are labelled with (D248): never inputs.
_DASHBOARD_FORBIDDEN_INPUTS = ("simulation.seed", "simulation.replications")
_DASHBOARD_ENTRY_KEYS = {
    "inputs": {"path", "label", "kind", "min", "max", "step", "unit", "choices"},
    "results": {"kpi", "label", "unit", "better"},
    "pages": {"name", "activity", "items"},
    "items": {"kind", "ref", "row", "col", "span", "mode", "names"},
}


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value) -> bool:
    """A finite number within the ceiling."""
    return is_finite_number(value) and abs(value) <= DASHBOARD_LIMITS["magnitude"]


def _text(value, limit: str = "text", empty: bool = False) -> bool:
    """A printable single-line string within its ceiling. Markup is not refused:
    it is data, and whoever draws it draws plain text (D251)."""
    return (isinstance(value, str) and len(value) <= DASHBOARD_LIMITS[limit]
            and value.isprintable() and (empty or bool(value)))


def validate_dashboard_definition(layout: dict) -> list:
    """Shape and ceilings of the whole `dashboard` section, on a layout that may
    hold anything (DEVLOG #360). Whether an input's path addresses something in
    THIS model is `nagasim.dashboard.check_inputs`, the split scenarios have."""
    problems: list = []
    section = layout.get("dashboard")
    if section is None:
        return problems

    def bad(message: str, path: str = "") -> None:
        problems.append(problem("E_DASHBOARD_DEFINITION", f"dashboard: {message}",
                                f"/dashboard{path}", hint="see the schema reference: dashboard"))
    if not isinstance(section, dict):
        bad("must be an object")
        return problems
    known = {"widgets", "controls", "inputs", "results", "limits", "pages", "hidden"}
    if set(section) - known:
        bad(f"unknown key {sorted(set(section) - known)[0][:40]!r}")
    lists: dict[str, list] = {}
    for key in ("widgets", "controls", "inputs", "results", "pages", "hidden"):
        entries, limit = section.get(key, []), DASHBOARD_LIMITS[key]
        if not isinstance(entries, list) or len(entries) > limit:
            bad(f"\"{key}\" must be a list of at most {limit} entries", f"/{key}")
        elif key != "hidden" and not all(isinstance(e, dict) for e in entries):
            bad(f"every entry of \"{key}\" must be an object", f"/{key}")
        else:
            lists[key] = entries
            if any(set(e) - _DASHBOARD_ENTRY_KEYS[key] for e in entries
                   if key in _DASHBOARD_ENTRY_KEYS):
                bad(f"an entry of \"{key}\" has an unknown key", f"/{key}")

    def labels(entry: dict, at: str) -> None:
        for key in ("label", "unit"):
            if key in entry and not _text(entry[key], "label", empty=True):
                bad(f"\"{key}\" must be printable text of at most "
                    f"{DASHBOARD_LIMITS['label']} characters", f"{at}/{key}")

    def unique(key: str, field: str) -> set:
        seen: set = set()
        for index, entry in enumerate(lists.get(key, [])):
            value = entry.get(field)
            if not _text(value) or value in seen:
                bad(f"\"{field}\" must be unique printable text of at most "
                    f"{DASHBOARD_LIMITS['text']} characters", f"/{key}/{index}/{field}")
            else:
                seen.add(value)
        return seen

    paths = unique("inputs", "path")
    for index, entry in enumerate(lists.get("inputs", [])):
        at = f"/inputs/{index}"
        labels(entry, at)
        kind = entry.get("kind")
        if entry.get("path") in _DASHBOARD_FORBIDDEN_INPUTS:
            bad("the seed and the replications cannot be inputs", f"{at}/path")
        if kind == "number":
            low, high, step = entry.get("min"), entry.get("max"), entry.get("step", 1)
            if not (_number(low) and _number(high) and _number(step) and low < high and step > 0
                    and (high - low) / step <= DASHBOARD_LIMITS["steps"]):
                bad(f"a number input needs finite min < max within "
                    f"{DASHBOARD_LIMITS['magnitude']:g} and a step > 0 giving at most "
                    f"{DASHBOARD_LIMITS['steps']} steps", f"{at}/min")
        elif kind == "choice":
            choices = entry.get("choices")
            if not (isinstance(choices, list) and 2 <= len(choices) <= DASHBOARD_LIMITS["choices"]
                    and all(isinstance(c, dict) and set(c) == {"label", "value"}
                            and _text(c["label"], "label")
                            and (_number(c["value"]) or isinstance(c["value"], bool)
                                 or _text(c["value"])) for c in choices)):
                bad(f"a choice input needs 2 to {DASHBOARD_LIMITS['choices']} choices, each a "
                    f"{{\"label\", \"value\"}} whose value is a number, true/false or text",
                    f"{at}/choices")
        elif kind != "switch":
            bad(f"an input's kind is one of {', '.join(DASHBOARD_INPUT_KINDS)}", f"{at}/kind")
    kpis = unique("results", "kpi")
    for index, entry in enumerate(lists.get("results", [])):
        labels(entry, f"/results/{index}")
        if entry.get("better", "lower") not in ("lower", "higher"):
            bad("\"better\" is \"lower\" or \"higher\"", f"/results/{index}/better")

    limits = section.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - {"replications", "max_replications"}:
        bad("\"limits\" holds \"replications\" and \"max_replications\" only", "/limits")
    else:
        default, most = limits.get("replications", 2), limits.get("max_replications", 30)
        if not (_whole(default) and _whole(most)
                and 2 <= default <= most <= DASHBOARD_LIMITS["replications"]):
            bad(f"limits need whole numbers with 2 <= replications <= max_replications <= "
                f"{DASHBOARD_LIMITS['replications']}", "/limits")

    refs = {"input": paths, "result": kpis,
            "widget": {w.get("name") for w in lists.get("widgets", [])},
            "control": {c.get("name") for c in lists.get("controls", [])}}
    unique("pages", "name")
    for index, page in enumerate(lists.get("pages", [])):
        at = f"/pages/{index}"
        activity, items = page.get("activity"), page.get("items", [])
        allowed = DASHBOARD_ACTIVITIES.get(activity) if isinstance(activity, str) else None
        if allowed is None:
            bad(f"a page's activity is one of {', '.join(DASHBOARD_ACTIVITIES)}", f"{at}/activity")
        elif not (isinstance(items, list) and len(items) <= DASHBOARD_LIMITS["items"]
                  and all(isinstance(i, dict) and not set(i) - _DASHBOARD_ENTRY_KEYS["items"]
                          for i in items)):
            bad(f"a page holds at most {DASHBOARD_LIMITS['items']} items, each an object of "
                f"known keys", f"{at}/items")
        else:
            for j, item in enumerate(items):
                _validate_dashboard_item(item, allowed, refs, bad, f"{at}/items/{j}")
    if not all(_text(tag) for tag in lists.get("hidden", [])):
        bad(f"\"hidden\" holds printable text of at most {DASHBOARD_LIMITS['text']} characters",
            "/hidden")
    return problems


def _validate_dashboard_item(item: dict, allowed: tuple, refs: dict, bad, at: str) -> None:
    kind = item.get("kind")
    if not isinstance(kind, str) or kind not in allowed:
        bad(f"this page takes the item kinds {', '.join(allowed)}", f"{at}/kind")
        return
    if kind in refs and (not isinstance(item.get("ref"), str) or item["ref"] not in refs[kind]):
        bad(f"\"ref\" names no {kind} of this dashboard", f"{at}/ref")
    grid = DASHBOARD_LIMITS["grid"]
    for key, least in (("row", 0), ("col", 0), ("span", 1)):
        if not (_whole(item.get(key, least)) and least <= item.get(key, least) <= grid):
            bad(f"\"{key}\" is a whole number from {least} to {grid}", f"{at}/{key}")
    if item.get("mode", "schematic") not in DASHBOARD_VIEW_MODES:
        bad(f"a view's mode is one of {', '.join(DASHBOARD_VIEW_MODES)}", f"{at}/mode")
    names = item.get("names", [])
    if not (isinstance(names, list) and len(names) <= DASHBOARD_LIMITS["names"]
            and all(_text(n) for n in names)):
        bad(f"\"names\" holds at most {DASHBOARD_LIMITS['names']} scenario names", f"{at}/names")


def _validate_dashboard(layout: dict, problems: list, variables: set[str],
                        tables: dict, refs: dict) -> None:
    """The top-level "dashboard" section (PHASE6 G1, D132, schema v26): named
    widgets sampled at the same instants as the timeseries sampler
    (`core/timeseries.py`) -- kpi/gauge read one expression, line/bar read
    1-4/1-8 labelled series. Shape and expression checks only; "row"/"col"/
    "span" are pure layout hints for the G2 panel, bounds-checked by the JSON
    Schema alone."""
    problems.extend(validate_dashboard_definition(layout))
    section = layout.get("dashboard")
    # The checks below read widgets and controls as lists of objects (#360).
    if section is not None and not (isinstance(section, dict) and all(
            isinstance(section.get(key, []), list)
            and all(isinstance(e, dict) for e in section.get(key, []))
            for key in ("widgets", "controls"))):
        return
    # What NQ/NR/MR/AVAILABLE/IDLE resolve at run time: declared resources,
    # fleets and a block's undeclared resource_name (registered with capacity
    # 1, DEVLOG #17) -- never a resource set.
    sets = {s.get("name") for s in layout.get("resource_sets", []) or []}
    resources = ({r.get("name") for r in layout.get("resources", []) or []}
                 | {f.get("name") for f in layout.get("fleets", []) or [] if isinstance(f, dict)}
                 | {(b.get("params") or {}).get("resource_name") for b in layout.get("blocks", [])}
                 ) - sets - {None, ""}
    seen: set[str] = set()
    for index, widget in enumerate((layout.get("dashboard") or {}).get("widgets") or []):
        at = f"/dashboard/widgets/{index}"
        name = widget.get("name", "")
        if not name:
            problems.append(problem("E_DASHBOARD_NAME", "dashboard widget: name must not be "
                                    "empty", f"{at}/name", hint="give every widget a unique name"))
            continue
        if name in seen:
            problems.append(problem("E_DASHBOARD_NAME",
                                    f"duplicate dashboard widget name: {name}", f"{at}/name",
                                    hint="rename one of the two widgets"))
        seen.add(name)
        kind = widget.get("kind")
        if kind not in _DASHBOARD_KINDS:
            problems.append(problem("E_DASHBOARD_KIND", f"widget '{name}': unknown kind "
                                    f"{kind!r}", f"{at}/kind",
                                    hint=f"use one of {', '.join(_DASHBOARD_KINDS)}"))
            continue
        if kind in ("kpi", "gauge"):
            if "series" in widget:
                problems.append(problem("E_DASHBOARD_SERIES",
                                        f"widget '{name}': a {kind} widget takes \"expr\", not "
                                        f"\"series\"", f"{at}/series",
                                        hint="one expression per kpi/gauge widget"))
            expr = widget.get("expr", "")
            if not expr:
                problems.append(problem("E_DASHBOARD_EXPR", f"widget '{name}': \"expr\" must "
                                        "not be empty", f"{at}/expr"))
            else:
                _check_dashboard_expression(problems, f"dashboard widget '{name}'", expr,
                                            variables, f"{at}/expr", tables, refs, resources)
            if kind == "gauge":
                _validate_dashboard_gauge(widget, problems, name, at)
            continue
        # line/bar: 1-4 / 1-8 labelled series, unique non-empty labels.
        if "expr" in widget:
            problems.append(problem("E_DASHBOARD_SERIES",
                                    f"widget '{name}': a {kind} widget takes \"series\", not "
                                    f"\"expr\"", f"{at}/expr",
                                    hint="one or more {\"label\", \"expr\"} series"))
        series = widget.get("series") or []
        limit = _DASHBOARD_SERIES_LIMITS[kind]
        if not 1 <= len(series) <= limit:
            problems.append(problem("E_DASHBOARD_SERIES",
                                    f"widget '{name}': a {kind} widget takes 1-{limit} series, "
                                    f"got {len(series)}", f"{at}/series",
                                    hint=f"add between 1 and {limit} "
                                         "{\"label\", \"expr\"} entries"))
        labels_seen: set[str] = set()
        for s_index, entry in enumerate(series):
            s_at = f"{at}/series/{s_index}"
            label = entry.get("label", "")
            if not label:
                problems.append(problem("E_DASHBOARD_SERIES",
                                        f"widget '{name}': series {s_index}: label must not be "
                                        f"empty", f"{s_at}/label"))
            elif label in labels_seen:
                problems.append(problem("E_DASHBOARD_SERIES",
                                        f"widget '{name}': duplicate series label '{label}'",
                                        f"{s_at}/label", hint="rename one of the two series"))
            labels_seen.add(label)
            expr = entry.get("expr", "")
            if not expr:
                problems.append(problem("E_DASHBOARD_EXPR",
                                        f"widget '{name}': series '{label}': \"expr\" must not "
                                        f"be empty", f"{s_at}/expr"))
            else:
                _check_dashboard_expression(problems, f"dashboard widget '{name}' series "
                                            f"'{label}'", expr, variables, f"{s_at}/expr",
                                            tables, refs, resources)
    _validate_dashboard_controls(layout, problems, variables, refs["AUX"][0])


def _validate_dashboard_controls(layout: dict, problems: list, variables: set[str],
                                 aux_names: set[str]) -> None:
    """The dashboard's game-mode controls (PHASE6 G4, D134): a slider sets a
    resource's capacity (within 0..declared capacity), a variable or an SD
    auxiliary over [min, max]; a decision answers a Policy-mode DECIDE, one
    option label per outgoing link. Everything is E_DASHBOARD_CONTROL."""
    numeric = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    capacities = {r.get("name"): r.get("capacity", 1) for r in layout.get("resources", []) or []}
    outs: dict[str, int] = {}
    for src, _tgt in _link_pairs(layout):
        outs[src] = outs.get(src, 0) + 1
    policies: dict[str, set[int]] = {}
    for block in layout.get("blocks", []):
        params = block.get("params") or {}
        if block.get("type", "").upper() == "DECIDE" and params.get("mode") == "Policy":
            policies.setdefault(params.get("policy") or "", set()).add(outs.get(block["name"], 0))
    seen: set[str] = set()
    for index, control in enumerate((layout.get("dashboard") or {}).get("controls") or []):
        at = f"/dashboard/controls/{index}"
        name = control.get("name", "")

        def bad(message: str, path: str = "", hint: str | None = None) -> None:
            problems.append(problem("E_DASHBOARD_CONTROL", f"dashboard control '{name}': {message}",
                                    at + path, hint=hint))
        if not name:
            bad("name must not be empty", "/name", "give every control a unique name")
            continue
        if name in seen:
            bad("duplicate control name", "/name", "rename one of the two controls")
        seen.add(name)
        kind = control.get("kind")
        if kind == "decision":
            policy, options = control.get("policy", ""), control.get("options") or []
            counts = policies.get(policy) if policy else None
            if counts is None:
                bad(f"no Policy-mode DECIDE uses policy '{policy}'", "/policy",
                    f"declared policies: {', '.join(sorted(p for p in policies if p)) or '(none)'}")
            elif any(n != len(options) for n in counts):
                bad(f"{len(options)} options for a DECIDE with "
                    f"{'/'.join(str(n) for n in sorted(counts))} outgoing links", "/options",
                    "one option label per outgoing link, in link order")
            continue
        if kind != "slider":
            bad(f"unknown kind {kind!r}", "/kind", "use slider or decision")
            continue
        target = control.get("target")
        target_kind = target_name = None
        if isinstance(target, dict) and len(target) == 1:
            (target_kind, target_name), = target.items()
        known = {"capacity": capacities, "variable": variables,
                 "constant": aux_names}.get(target_kind)
        if known is None:
            bad("\"target\" must be one of {\"capacity\"|\"variable\"|\"constant\": name}",
                "/target")
        elif target_name not in known:
            bad(f"unknown {'resource' if target_kind == 'capacity' else target_kind} "
                f"'{target_name}'", "/target")
        low, high, step = control.get("min"), control.get("max"), control.get("step", 1)
        if not (numeric(low) and numeric(high) and numeric(step)):
            bad("\"min\", \"max\" and \"step\" must be numbers", "/min")
        elif low >= high:
            bad("\"max\" must be greater than \"min\"", "/max")
        elif step <= 0:
            bad("\"step\" must be greater than 0", "/step")
        elif target_kind == "capacity" and target_name in capacities and (
                low < 0 or not numeric(capacities[target_name])
                or high > capacities[target_name]):
            bad(f"capacity range must lie within 0..{capacities[target_name]}, the resource's "
                f"declared capacity", "/max")


# ROADMAP 3.4a. Exact names plus three `<name>:<argument>` families; the
# transforms themselves live in `nagasim/databind.py` (they need pandas/scipy),
# but their NAMES are part of the file format, so they are validated here.
DATA_TRANSFORMS = ("mean", "median", "last", "sum", "count", "value", "fit", "empirical",
                   "orders")   # "orders" replaces the whole order book (PHASE4 A3)
DATA_TRANSFORM_PREFIXES = ("quantile:", "rate_per:", "arrival_schedule:", "table:",
                           "series:")   # a whole time-series input (PHASE4 D2)
_FILTER_OPS = ("==", "!=", ">=", "<=", ">", "<")


def consent_problems(layout: dict) -> list:
    """Warnings for a model file that tries to grant itself a permission.

    Checked against the RAW file, since `migrate` strips the key -- the point
    is to tell the human "this was in your file and it did nothing", which is
    only visible before the strip.
    """
    found = []
    for index, entry in enumerate(layout.get("data_sources") or []):
        if not isinstance(entry, dict):
            continue
        for key in CONSENT_KEYS:
            if key in entry:
                found.append(problem(
                    "W_DATA_ALLOW_NETWORK_IGNORED",
                    f"data source '{entry.get('name', '')}': \"{key}\" in a model "
                    f"file is ignored",
                    f"/data_sources/{index}/{key}",
                    hint="network access is granted by whoever RUNS the model "
                         "(--allow-network, or the editor's consent dialog), never by the "
                         "file itself; the key is dropped on load"))
    return found


def _validate_data_sources(layout: dict, problems: list) -> set[str]:
    """The "data_sources" section (ROADMAP 3.4a): shape, known kind, and the
    two hard security rules -- a literal password in a SQL URL is an error, so
    a model file never carries a credential (`${NAGASIM_DATA_<NAME>}` is how a URL
    that needs one is written; `databind` expands it at resolve time), and a source
    may not grant itself network access (`consent_problems`)."""
    names: set[str] = set()
    problems.extend(consent_problems(layout))
    for index, entry in enumerate(layout.get("data_sources", [])):
        at = f"/data_sources/{index}"
        name = entry.get("name", "")
        if not name:
            problems.append(problem("E_DATA_SOURCE_NAME", "data source: name must not be empty",
                                    f"{at}/name", hint="give every data source a unique name"))
        elif name in names:
            problems.append(problem("E_DATA_SOURCE_NAME", f"duplicate data source name: {name}",
                                    f"{at}/name", hint="rename one of the two sources"))
        names.add(name)

        kind = entry.get("kind")
        if kind not in DATA_SOURCE_KINDS:
            problems.append(problem(
                "E_DATA_SOURCE_KIND",
                f"data source '{name}': unknown kind '{kind}' "
                f"(expected one of {', '.join(DATA_SOURCE_KINDS)})",
                f"{at}/kind", hint=f"use one of: {', '.join(DATA_SOURCE_KINDS)}"))
            continue

        location = entry.get("location") or ""
        if kind in CONNECTION_SOURCE_KINDS:
            _validate_live_source(entry, name, kind, at, problems)
        if not location:
            problems.append(problem("E_DATA_SOURCE_LOCATION",
                                    f"data source '{name}': location must not be empty",
                                    f"{at}/location",
                                    hint="a path relative to the model file, or a URL"))
        elif kind in ("sqlite", "sql") and not entry.get("query"):
            problems.append(problem("E_DATA_SOURCE_LOCATION",
                                    f"data source '{name}': a {kind} source needs a query",
                                    f"{at}/query", hint="add the SELECT this source reads"))
        if password_in_url(location):
            problems.append(problem(
                "E_DATA_CREDENTIALS",
                f"data source '{name}': the location embeds a password",
                f"{at}/location",
                hint="never store a credential in a model file: write the whole URL as "
                     "\"${NAGASIM_DATA_DB_URL}\" and set that environment variable"))
    return names


def _validate_live_source(entry: dict, name: str, kind: str, at: str, problems: list) -> None:
    """A live source (PHASE7 7.8): a connection NAME, a location that names no host, and
    only the options its reader knows. Whether the caller defined that connection is not
    this file's to say: `netguard.connection_for` answers when the source is read."""
    if not entry.get("connection") or not isinstance(entry.get("connection"), str):
        problems.append(problem(
            "E_DATA_CONNECTION", f"data source '{name}': a {kind} source needs a connection",
            f"{at}/connection",
            hint="name a connection; the person who runs the model defines it in their "
                 "connections file (--connections), never in the model"))
    location = str(entry.get("location") or "")
    if "://" in location or location.startswith("//"):
        problems.append(problem(
            "E_DATA_SOURCE_LOCATION",
            f"data source '{name}': a {kind} location names no host",
            f"{at}/location",
            hint="the address is the connection's; write only the path, topic or node here"))
    options = entry.get("options") or {}
    for key in (options if isinstance(options, dict) else {}):
        if key not in LIVE_SOURCE_OPTIONS[kind]:
            problems.append(problem(
                "E_DATA_SOURCE_OPTION",
                f"data source '{name}': unknown option '{key}' for a {kind} source",
                f"{at}/options/{key}",
                hint=f"its options: {', '.join(LIVE_SOURCE_OPTIONS[kind])}"))


def password_in_url(location: str) -> bool:
    """True for a `scheme://user:secret@host` URL whose password is a literal.
    A `${NAGASIM_DATA_<NAME>}` reference is exactly what this rule pushes people towards,
    so it never counts. Checked at validation time AND again in `databind`
    right before a connection is opened -- a layout built in memory never went
    through validation."""
    _scheme, sep, rest = str(location).partition("://")
    if not sep:
        return False
    authority = rest.split("/", 1)[0]
    if "@" not in authority:
        return False
    userinfo = authority.rsplit("@", 1)[0]
    if ":" not in userinfo:
        return False
    secret = userinfo.split(":", 1)[1]
    return bool(secret) and not (secret.startswith("${") and secret.endswith("}"))


def _validate_data_bindings(layout: dict, sources: set[str], problems: list) -> None:
    """The "data_bindings" section (ROADMAP 3.4a): shape, a declared source, a
    known transform and a simple filter. Whether the `target` addresses
    anything in THIS model is checked by `databind.resolve_bindings` -- core/
    must not import the experiments module that owns the path grammar (same
    split as the scenarios section)."""
    names: set[str] = set()
    for index, entry in enumerate(layout.get("data_bindings", [])):
        at = f"/data_bindings/{index}"
        name = entry.get("name", "")
        if not name:
            problems.append(problem("E_DATA_BINDING_NAME", "data binding: name must not be empty",
                                    f"{at}/name", hint="give every binding a unique name"))
        elif name in names:
            problems.append(problem("E_DATA_BINDING_NAME", f"duplicate data binding name: {name}",
                                    f"{at}/name", hint="rename one of the two bindings"))
        names.add(name)

        source = entry.get("source")
        if source not in sources:
            problems.append(problem(
                "E_DATA_BINDING_SOURCE", f"binding '{name}': unknown data source '{source}'",
                f"{at}/source",
                hint=f"declare it under \"data_sources\"; declared: "
                     f"{', '.join(sorted(sources)) or '(none)'}"))

        transform = entry.get("transform", "")
        if transform not in DATA_TRANSFORMS and not transform.startswith(DATA_TRANSFORM_PREFIXES):
            problems.append(problem(
                "E_DATA_BINDING_TRANSFORM", f"binding '{name}': unknown transform '{transform}'",
                f"{at}/transform",
                hint=f"use one of: {', '.join(DATA_TRANSFORMS)}, or "
                     f"{', '.join(p + '<argument>' for p in DATA_TRANSFORM_PREFIXES)}"))

        if not entry.get("target"):
            problems.append(problem("E_DATA_BINDING_TARGET",
                                    f"binding '{name}': target must not be empty",
                                    f"{at}/target",
                                    hint="an override path, e.g. \"resources.Nurse.capacity\""))
        # "count" needs no column, and "orders" reads several by its own `map`.
        if not entry.get("column") and transform not in ("count", "orders"):
            problems.append(problem("E_DATA_BINDING_TARGET",
                                    f"binding '{name}': column must not be empty",
                                    f"{at}/column",
                                    hint="name the column of the source this binding reads"))

        filters = entry.get("filter")
        if filters is not None and not isinstance(filters, dict):
            problems.append(problem(
                "E_DATA_FILTER", f"binding '{name}': filter must be an object",
                f"{at}/filter",
                hint="a filter is {\"column\": value} or {\"column >=\": value} -- data, not code"))
        elif filters:
            for key in filters:
                column = str(key).rsplit(" ", 1)[0] if " " in str(key) else str(key)
                op = str(key).rsplit(" ", 1)[1] if " " in str(key) else "=="
                if not column or op not in _FILTER_OPS:
                    problems.append(problem(
                        "E_DATA_FILTER", f"binding '{name}': bad filter key '{key}'",
                        f"{at}/filter",
                        hint=f"write \"<column>\" or \"<column> <op>\" with op one of: "
                             f"{', '.join(_FILTER_OPS)}"))


def _validate_data(layout: dict, problems: list) -> None:
    """The "data_sources" section hook (core/sections.py): sources, then the
    bindings that must name one of them."""
    _validate_data_bindings(layout, _validate_data_sources(layout, problems), problems)


def _validate_travel(layout: dict, problems: list, nodes: set, fleets: set) -> None:
    """The travel blocks (PHASE4 E1): TRANSPORT, CONVEYOR and a ROUTE in
    network mode, plus the connectivity every declared trip needs.

    Reachability is checked on what the model actually asks to travel BETWEEN --
    consecutive steps of each sequence and every literal TRANSPORT destination.
    A pair whose two ends both sit on the network and have no path is an error
    naming both, because at run time it is a crash halfway through a run.

    A gantry fleet (W3-M7, D118) needs no segment between two nodes -- it
    travels directly, by coordinates alone -- so it is exempt here: a
    TRANSPORT's own (home, destination) pair is skipped for a gantry fleet, and
    a model that declares ANY gantry fleet skips the sequence-step pairs
    altogether (a sequence step is not tied to one fleet, so the check cannot
    tell which leg a gantry actually serves; a mixed model that also NEEDS a
    walked or driven network path between the same two stations is not caught
    here, a ceiling -- see ARCHITECTURE 5.21).
    """
    stations = station_blocks(layout)
    places = block_nodes(layout)
    known_nodes = ", ".join(sorted(nodes)) or "(none)"
    known_fleets = ", ".join(sorted(fleets)) or "(none)"
    resources = {str(r.get("name", "")) for r in layout.get("resources", []) or []}
    resources |= {str(s.get("name", "")) for s in layout.get("resource_sets", []) or []} | fleets
    pairs: list[tuple[str, str]] = []
    gantry_fleets = {str(f.get("name", "")).strip() for f in layout.get("fleets", []) or []
                     if isinstance(f, dict)
                     and str(f.get("motion", "network") or "network") == "gantry"}

    for index, block in enumerate(layout.get("blocks", []) or []):
        name = block.get("name", "<unnamed>")
        btype = str(block.get("type", "")).upper()
        params = block.get("params") or {}
        at = f"/blocks/{index}/params"

        if btype == "ROUTE":
            mode = params.get("travel", "time") or "time"
            if mode not in TRAVEL_MODES:
                problems.append(problem(
                    "E_TRAVEL_MODE", f"{name}: unknown travel mode '{mode}'", f"{at}/travel",
                    block=name, hint=f"one of: {', '.join(TRAVEL_MODES)}"))
            elif mode == "network" and not nodes:
                problems.append(problem(
                    "E_TRAVEL_MODE", f"{name}: travel mode \"network\" needs a path network",
                    "/network", block=name,
                    hint="add a \"network\" section, and a `node` to every STATION involved"))

        elif btype == "TRANSPORT":
            fleet = str(params.get("fleet", "") or "").strip()
            if not fleet or fleet not in fleets:
                problems.append(problem(
                    "E_FLEET_UNKNOWN", f"{name}: unknown mover fleet '{fleet}'", f"{at}/fleet",
                    block=name, hint=f"declare it under \"fleets\"; declared: {known_fleets}"))
            destination = str(params.get("destination", "") or "").strip()
            if destination and destination not in stations:
                problems.append(problem(
                    "E_SEQUENCE_STATION_UNKNOWN",
                    f"{name}: unknown destination station '{destination}'", f"{at}/destination",
                    block=name,
                    hint="name a STATION block, or leave it empty to follow the routing"))
            elif destination and destination not in places:
                problems.append(problem(
                    "E_NETWORK_NODE_UNKNOWN",
                    f"{name}: destination station '{destination}' sits on no network node",
                    f"{at}/destination", block=name,
                    hint=f"set that STATION's `node` param; declared nodes: {known_nodes}"))
            # A vehicle must at least be able to GET there from where it parks
            # (a gantry needs no such path, D118).
            if destination:
                pairs.extend((str(f.get("home", "") or ""), destination)
                             for f in layout.get("fleets", []) or []
                             if isinstance(f, dict) and str(f.get("name", "")).strip()
                             not in gantry_fleets)

        elif btype == "CONVEYOR":
            for field in ("speed", "spacing"):
                value = params.get(field, 1.0)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    problems.append(problem(
                        "E_CONVEYOR_PARAM", f"{name}: {field} must be a number > 0",
                        f"{at}/{field}", block=name,
                        hint="speed is belt length per time unit; spacing is the belt one "
                             "entity takes"))
            length = params.get("length", 0.0)
            if not isinstance(length, (int, float)) or isinstance(length, bool) or length < 0:
                problems.append(problem(
                    "E_CONVEYOR_PARAM", f"{name}: length must be a number >= 0", f"{at}/length",
                    block=name, hint="0 takes the network distance between the two nodes"))
            ends = [str(params.get(f, "") or "").strip() for f in ("from_node", "to_node")]
            for field, node in zip(("from_node", "to_node"), ends):
                if node and node not in nodes:
                    problems.append(problem(
                        "E_CONVEYOR_PARAM", f"{name}: unknown network node '{node}'",
                        f"{at}/{field}", block=name, hint=f"declared nodes: {known_nodes}"))
            if (not length) and not all(ends):
                problems.append(problem(
                    "E_CONVEYOR_PARAM",
                    f"{name}: no length: set `length`, or both `from_node` and `to_node`",
                    f"{at}/length", block=name,
                    hint="a belt needs a length before it can have a capacity or a travel time"))
            exit_resource = str(params.get("exit_resource", "") or "").strip()
            if exit_resource and exit_resource not in resources:
                problems.append(problem(
                    "E_CONVEYOR_PARAM", f"{name}: unknown resource '{exit_resource}'",
                    f"{at}/exit_resource", block=name,
                    hint="the end stop is an ordinary resource; declare it under \"resources\""))

    if not gantry_fleets:
        for entry in layout.get("sequences", []) or []:
            steps = [str(s.get("station", "")) for s in entry.get("steps") or []
                    if isinstance(s, dict)]
            pairs.extend(zip(steps, steps[1:]))

    seen = set()
    for a, b in unreachable_pairs(layout, pairs):
        if (a, b) in seen:
            continue
        seen.add((a, b))
        problems.append(problem(
            "E_NETWORK_UNREACHABLE", f"network: no path from '{a}' to '{b}'", "/network/segments",
            hint="add a segment joining them, or make the segment between them two-way"))
