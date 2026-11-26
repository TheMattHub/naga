"""Validators of the sections whose meaning another module owns but which
cannot live next to it: scenarios, reality checks and assertions (owned by the
top-level `experiments`/`modelcheck`), the dashboard (sampled by
`core/timeseries.py`, but checked with the shared expression check), data
sources/bindings (read by the top-level `databind`) and the travel blocks
(ROUTE/TRANSPORT/CONVEYOR over `core/network.py`; a move there would cycle
through the block package). core/ never imports a top-level module, so their
file-format names (`DATA_SOURCE_KINDS`, ...) are declared here."""
from __future__ import annotations

import math
import re
from dataclasses import replace

from nagasim.core.blocks.route import TRAVEL_MODES
from nagasim.core.blocks.transfer_piece import MECHANISMS as TRANSFER_MECHANISMS
from nagasim.core import conveyor_look
from nagasim.core.conveyor_path import parse_path, path_length
from nagasim.core.conveyor_types import TYPES as CONVEYOR_TYPES
from nagasim.core.expressions import (ExpressionError, ExpressionScope, compile_expression,
                                      functions_with)
from nagasim.core.network import block_nodes, unreachable_pairs
from nagasim.core.problems import problem
from nagasim.core.routing import (RouteRule, divert_exits, divert_groups, divert_slots, rule_spec,
                                  validate_rule)
from nagasim.core.schema.common import _check_expression, _link_pairs
from nagasim.core.schema.migrate import CONSENT_KEYS
from nagasim.core.sequences import station_blocks
from nagasim.core.timeunits import UNITS as TIME_UNITS
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
                                    f"{at}/name", hint="give every scenario a unique name",
                                    fields={"case": "empty"}))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_SCENARIO", f"duplicate scenario name: {name}",
                                    f"{at}/name", hint="rename one of the two scenarios",
                                    fields={"case": "twice", "name": name}))
        seen.add(name)
        goal = entry.get("goal", "")       # D577/D578: optional, text only
        if not isinstance(goal, str) or len(goal) > 500:
            problems.append(problem(
                "E_SCENARIO_GOAL", f"scenario '{name}': goal must be text of at most 500 characters",
                f"{at}/goal", hint="write the question the scenario answers in one sentence",
                fields={"case": "long", "name": name}))
        for j, override in enumerate(entry.get("overrides", [])):
            where = f"{at}/overrides/{j}"
            if not override.get("path"):
                problems.append(problem(
                    "E_SCENARIO_OVERRIDE", f"scenario '{name}': an override has no path",
                    f"{where}/path",
                    hint="an override is {\"path\": \"resources.Server.capacity\", \"value\": 2}",
                    fields={"case": "a2_no_path", "name": name}))
            elif "value" not in override:
                problems.append(problem(
                    "E_SCENARIO_OVERRIDE",
                    f"scenario '{name}': override '{override['path']}' has no value",
                    f"{where}/value", hint="add the \"value\" this override sets",
                    fields={"case": "a2_no_value", "name": name, "path": override["path"]}))


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
                                    hint="give every reality check a unique name",
                                    fields={"case": "empty"}))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_REALITY_CHECK",
                                    f"duplicate reality check name: {name}", f"{at}/name",
                                    hint="rename one of the two reality checks",
                                    fields={"case": "twice", "name": name}))
        seen.add(name)
        inputs = entry.get("inputs", {})
        if not isinstance(inputs, dict) or not all(isinstance(p, str) and p for p in inputs):
            problems.append(problem(
                "E_REALITY_CHECK_INPUT",
                f"reality check '{name}': \"inputs\" must be an object of non-empty override "
                f"path: value", f"{at}/inputs",
                hint="{\"auxiliaries.Workforce.expr\": \"0\"}, same paths a scenario uses",
                fields={"case": "inputs", "name": name}))
        try:
            parse_when(entry.get("when", "always"))
        except ValueError as exc:
            problems.append(problem("E_REALITY_CHECK_WHEN", f"reality check '{name}': {exc}",
                                    f"{at}/when", fields={"name": name}))
        constraint = entry.get("constraint", "")
        if not constraint:
            problems.append(problem(
                "E_EXPRESSION", f"reality check '{name}': constraint must not be empty",
                f"{at}/constraint", hint="a boolean expression, e.g. AUX(\"Production\") <= 0",
                fields={"case": "a2_constraint", "name": name}))
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
                                    f"{at}/name", hint="give every assertion a unique name",
                                    fields={"case": "empty"}))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_ASSERTION", f"duplicate assertion name: {name}",
                                    f"{at}/name", hint="rename one of the two assertions",
                                    fields={"case": "twice", "name": name}))
        seen.add(name)
        if not entry.get("kpi"):
            problems.append(problem("E_ASSERTION_KPI", f"assertion '{name}': \"kpi\" must not "
                                    "be empty", f"{at}/kpi",
                                    hint="a KPI key from experiments.flatten_kpis, e.g. "
                                        "\"system/Throughput (per time unit)\"",
                                    fields={"case": "empty", "name": name}))
        op = entry.get("op")
        if op not in _ASSERTION_OPS:
            problems.append(problem("E_ASSERTION_OP", f"assertion '{name}': unknown \"op\" "
                                    f"{op!r}", f"{at}/op",
                                    hint=f"use one of {', '.join(_ASSERTION_OPS)}",
                                    fields={"case": "unknown", "name": name, "op": op}))
        replications = entry.get("replications", 5)
        if not isinstance(replications, int) or isinstance(replications, bool) or replications < 1:
            problems.append(problem("E_ASSERTION_REPLICATIONS",
                                    f"assertion '{name}': \"replications\" must be an integer >= 1",
                                    f"{at}/replications",
                                    fields={"case": "count", "name": name}))
        confidence = entry.get("confidence", 0.95)
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
                or not 0 < confidence < 1:
            problems.append(problem("E_ASSERTION_CONFIDENCE",
                                    f"assertion '{name}': \"confidence\" must be between 0 and 1",
                                    f"{at}/confidence",
                                    fields={"case": "range", "name": name}))


_PATH_RULES = ("order", "never", "at_most", "always")


def _validate_path_checks(layout: dict, problems: list) -> None:
    """The "path_checks" section (D722): unique non-empty names, a known rule, the block
    names the rule needs and, for "at_most", a whole "count" >= 0. Block names are checked
    against the model's blocks unless it holds a submodel instance: then a rule names a
    flattened `instance/block`, judged when the flattened model is validated."""
    entries = layout.get("path_checks")
    if entries is None:
        return
    if not isinstance(entries, list):
        problems.append(problem("E_PATH_CHECK_RULE", "path_checks must be a list of rules",
                                "/path_checks",
                                hint='[{"name": "no_scrap", "rule": "never", "block": "Scrap"}]',
                                fields={"case": "list"}))
        return
    blocks = [b for b in layout.get("blocks") or [] if isinstance(b, dict)]
    names = {b.get("name") for b in blocks}
    nested = any(str(b.get("type", "")).upper() == "SUBMODEL" for b in blocks)
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        at = f"/path_checks/{index}"
        if not isinstance(entry, dict):
            problems.append(problem("E_PATH_CHECK_RULE", "a path check must be an object",
                                    at, hint=f"one of the rules {', '.join(_PATH_RULES)}",
                                    fields={"case": "object"}))
            continue
        name = entry.get("name", "")
        if not isinstance(name, str) or not name:
            problems.append(problem("E_PATH_CHECK_NAME", "path check: name must not be empty",
                                    f"{at}/name", hint="give every path check a unique name",
                                    fields={"case": "empty"}))
            continue
        if name in seen:
            problems.append(problem("E_DUPLICATE_PATH_CHECK", f"duplicate path check name: "
                                    f"{name}", f"{at}/name",
                                    hint="rename one of the two path checks",
                                    fields={"case": "twice", "name": name}))
        seen.add(name)
        rule = entry.get("rule")
        if rule not in _PATH_RULES:
            problems.append(problem("E_PATH_CHECK_RULE", f"path check '{name}': unknown rule "
                                    f"{rule!r}", f"{at}/rule",
                                    hint=f"use one of {', '.join(_PATH_RULES)}",
                                    fields={"case": "unknown", "name": name, "rule": rule}))
            continue
        e_type = entry.get("e_type")
        if e_type is not None and not isinstance(e_type, str):
            problems.append(problem("E_PATH_CHECK_RULE", f"path check '{name}': \"e_type\" "
                                    "must be an entity type name", f"{at}/e_type",
                                    fields={"case": "etype", "name": name}))
        keys = ("first", "then") if rule == "order" else ("block",)
        # A key the rule does not read is most likely a typo ("block" on an order rule).
        stray = sorted(set(entry) - {"name", "description", "rule", "e_type", *keys}
                       - ({"count"} if rule == "at_most" else set()))
        if stray:
            problems.append(problem("E_PATH_CHECK_RULE", f"path check '{name}': a {rule} rule "
                                    f"does not take {', '.join(map(repr, stray))[:120]}",
                                    f"{at}/{stray[0]}",
                                    hint=f"a {rule} rule takes {', '.join(keys)}"
                                         + (" and count" if rule == "at_most" else ""),
                                    fields={"case": "stray", "name": name, "rule": rule}))
        for key in keys:
            block = entry.get(key)
            if not isinstance(block, str) or not block:
                problems.append(problem("E_PATH_CHECK_BLOCK", f"path check '{name}': "
                                        f"\"{key}\" must name a block", f"{at}/{key}",
                                        fields={"case": "unnamed", "name": name, "key": key}))
            elif not nested and block not in names:
                problems.append(problem(
                    "E_PATH_CHECK_BLOCK", f"path check '{name}': no block named "
                    f"'{block[:80]}'", f"{at}/{key}",
                    hint=f"its blocks: {', '.join(sorted(map(str, names))[:20]) or '(none)'}",
                    fields={"case": "unknown", "name": name, "target": block}))
        if rule == "order" and entry.get("first") and entry.get("first") == entry.get("then"):
            problems.append(problem("E_PATH_CHECK_BLOCK", f"path check '{name}': \"first\" "
                                    "and \"then\" name the same block", f"{at}/then",
                                    fields={"case": "same", "name": name}))
        if rule == "at_most":
            count = entry.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                problems.append(problem("E_PATH_CHECK_COUNT", f"path check '{name}': "
                                        "\"count\" must be a whole number >= 0", f"{at}/count",
                                        hint="the most visits an entity may make to the block",
                                        fields={"case": "count", "name": name}))


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

    def bad(message: str, hint: str | None = None, fields=None) -> None:
        problems.append(problem("E_DASHBOARD_EXPR", f"{who}: {text}: {message}", path, hint=hint,
                                fields={"who": who, **(fields or {})}))
    for func in functions_with("resource_state"):
        for name in expression.refs.get(func, ()):
            if name not in resources:
                bad(f"unknown resource '{name}'", "a resource set is read through its members; "
                    f"declared resources: {', '.join(sorted(resources)) or '(none)'}",
                    fields={"case": "resource", "name": name})
    for func in sorted(expression.calls & _SAMPLER_CALLS):
        bad(f"{func}() draws a random number, and the dashboard sampler has no random stream",
            fields={"case": "draw", "func": func})
    if not expression.names and not expression.calls:
        try:
            float(expression.evaluate(ExpressionScope()))
        except (ExpressionError, TypeError, ValueError, ArithmeticError):
            bad("a constant that is not a number", fields={"case": "constant"})


def _validate_dashboard_gauge(widget: dict, problems: list, name: str, at: str) -> None:
    """A gauge's "min"/"max"/"thresholds" (PHASE6 G1): max > min, and every
    threshold a number inside [min, max] in ascending order."""
    minimum = widget.get("min", 0)
    maximum = widget.get("max")
    numeric = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if not numeric(maximum):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': gauge needs a numeric "
                                f"\"max\"", f"{at}/max",
                                fields={"case": "max", "name": name}))
        return
    if not numeric(minimum) or maximum <= minimum:
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': \"max\" must be greater "
                                f"than \"min\"", f"{at}/max",
                                fields={"case": "range", "name": name}))
        return
    thresholds = widget.get("thresholds") or []
    if not all(numeric(t) for t in thresholds):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': thresholds must be "
                                f"numbers", f"{at}/thresholds",
                                fields={"case": "numbers", "name": name}))
    elif thresholds != sorted(thresholds):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': thresholds must be "
                                f"ascending", f"{at}/thresholds",
                                fields={"case": "order", "name": name}))
    elif any(t < minimum or t > maximum for t in thresholds):
        problems.append(problem("E_DASHBOARD_GAUGE", f"widget '{name}': thresholds must lie "
                                f"within [min, max]", f"{at}/thresholds",
                                fields={"case": "within", "name": name}))


# -- Present mode (D242, D251): the dashboard section's `inputs`, `results`,
# `limits`, `pages` and `hidden`. A model file is data and may be hostile, so
# every count, range and length has a ceiling here; `modelschema.py` reads the
# same numbers, and `nagasim/dashboard.py` holds the rules that need the
# override grammar (core/ must not import the experiment module).
DASHBOARD_LIMITS = {"widgets": 64, "controls": 64, "inputs": 32, "results": 32, "pages": 8,
                    "hidden": 200, "items": 40, "choices": 20, "names": 64, "label": 80,
                    "text": 200, "steps": 10_000, "magnitude": 1e12, "replications": 1000,
                    "grid": 100, "note": 60}
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
    "inputs": {"path", "label", "kind", "min", "max", "step", "unit", "choices", "note"},
    "results": {"kpi", "label", "unit", "better", "target"},
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
    known = {"widgets", "controls", "inputs", "results", "limits", "pages", "hidden", "question"}
    if set(section) - known:
        bad(f"unknown key {sorted(set(section) - known)[0][:40]!r}")
    if "question" in section and not _text(section["question"]):
        bad(f"\"question\" must be printable text of at most {DASHBOARD_LIMITS['text']} characters",
            "/question")
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
        if "note" in entry and not _text(entry["note"], "note"):
            bad(f"\"note\" must be printable text of at most {DASHBOARD_LIMITS['note']} characters",
                f"{at}/note")
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
        if "target" in entry and not _number(entry["target"]):
            bad("\"target\" must be a finite number", f"/results/{index}/target")

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
                                    "empty", f"{at}/name", hint="give every widget a unique name",
                                    fields={"case": "empty"}))
            continue
        if name in seen:
            problems.append(problem("E_DASHBOARD_NAME",
                                    f"duplicate dashboard widget name: {name}", f"{at}/name",
                                    hint="rename one of the two widgets",
                                    fields={"case": "twice", "name": name}))
        seen.add(name)
        kind = widget.get("kind")
        if kind not in _DASHBOARD_KINDS:
            problems.append(problem("E_DASHBOARD_KIND", f"widget '{name}': unknown kind "
                                    f"{kind!r}", f"{at}/kind",
                                    hint=f"use one of {', '.join(_DASHBOARD_KINDS)}",
                                    fields={"case": "unknown", "name": name, "kind": kind}))
            continue
        if kind in ("kpi", "gauge"):
            if "series" in widget:
                problems.append(problem("E_DASHBOARD_SERIES",
                                        f"widget '{name}': a {kind} widget takes \"expr\", not "
                                        f"\"series\"", f"{at}/series",
                                        hint="one expression per kpi/gauge widget",
                                        fields={"case": "expr_only", "name": name, "kind": kind}))
            expr = widget.get("expr", "")
            if not expr:
                problems.append(problem("E_DASHBOARD_EXPR", f"widget '{name}': \"expr\" must "
                                        "not be empty", f"{at}/expr",
                                        fields={"case": "empty", "name": name}))
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
                                    hint="one or more {\"label\", \"expr\"} series",
                                    fields={"case": "series_only", "name": name, "kind": kind}))
        series = widget.get("series") or []
        limit = _DASHBOARD_SERIES_LIMITS[kind]
        if not 1 <= len(series) <= limit:
            problems.append(problem("E_DASHBOARD_SERIES",
                                    f"widget '{name}': a {kind} widget takes 1-{limit} series, "
                                    f"got {len(series)}", f"{at}/series",
                                    hint=f"add between 1 and {limit} "
                                         "{\"label\", \"expr\"} entries",
                                    fields={"case": "count", "name": name, "kind": kind, "limit": limit, "got": len(series)}))
        labels_seen: set[str] = set()
        for s_index, entry in enumerate(series):
            s_at = f"{at}/series/{s_index}"
            label = entry.get("label", "")
            if not label:
                problems.append(problem("E_DASHBOARD_SERIES",
                                        f"widget '{name}': series {s_index}: label must not be "
                                        f"empty", f"{s_at}/label",
                                        fields={"case": "label_empty", "name": name, "number": s_index}))
            elif label in labels_seen:
                problems.append(problem("E_DASHBOARD_SERIES",
                                        f"widget '{name}': duplicate series label '{label}'",
                                        f"{s_at}/label", hint="rename one of the two series",
                                        fields={"case": "label_twice", "name": name, "label": label}))
            labels_seen.add(label)
            expr = entry.get("expr", "")
            if not expr:
                problems.append(problem("E_DASHBOARD_EXPR",
                                        f"widget '{name}': series '{label}': \"expr\" must not "
                                        f"be empty", f"{s_at}/expr",
                                        fields={"case": "series_empty", "name": name,
                                                "label": label}))
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
                                    at + path, hint=hint, fields={"name": name}))
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
                         "file itself; the key is dropped on load",
                    fields={"case": "ignored", "name": entry.get("name", ""), "key": key}))
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
                                    f"{at}/name", hint="give every data source a unique name",
                                    fields={"case": "empty"}))
        elif name in names:
            problems.append(problem("E_DATA_SOURCE_NAME", f"duplicate data source name: {name}",
                                    f"{at}/name", hint="rename one of the two sources",
                                    fields={"case": "twice", "name": name}))
        names.add(name)

        kind = entry.get("kind")
        if kind not in DATA_SOURCE_KINDS:
            problems.append(problem(
                "E_DATA_SOURCE_KIND",
                f"data source '{name}': unknown kind '{kind}' "
                f"(expected one of {', '.join(DATA_SOURCE_KINDS)})",
                f"{at}/kind", hint=f"use one of: {', '.join(DATA_SOURCE_KINDS)}",
                fields={"case": "unknown", "name": name, "kind": kind}))
            continue

        location = entry.get("location") or ""
        if kind in CONNECTION_SOURCE_KINDS:
            _validate_live_source(entry, name, kind, at, problems)
        if not location:
            problems.append(problem("E_DATA_SOURCE_LOCATION",
                                    f"data source '{name}': location must not be empty",
                                    f"{at}/location",
                                    hint="a path relative to the model file, or a URL",
                                    fields={"case": "empty", "name": name}))
        elif kind in ("sqlite", "sql") and not entry.get("query"):
            problems.append(problem("E_DATA_SOURCE_LOCATION",
                                    f"data source '{name}': a {kind} source needs a query",
                                    f"{at}/query", hint="add the SELECT this source reads",
                                    fields={"case": "query", "name": name, "kind": kind}))
        if password_in_url(location):
            problems.append(problem(
                "E_DATA_CREDENTIALS",
                f"data source '{name}': the location embeds a password",
                f"{at}/location",
                hint="never store a credential in a model file: write the whole URL as "
                     "\"${NAGASIM_DATA_DB_URL}\" and set that environment variable",
                fields={"case": "password", "name": name}))
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
                 "connections file (--connections), never in the model",
            fields={"case": "missing", "name": name, "kind": kind}))
    location = str(entry.get("location") or "")
    if "://" in location or location.startswith("//"):
        problems.append(problem(
            "E_DATA_SOURCE_LOCATION",
            f"data source '{name}': a {kind} location names no host",
            f"{at}/location",
            hint="the address is the connection's; write only the path, topic or node here",
            fields={"case": "host", "name": name}))
    options = entry.get("options") or {}
    for key in (options if isinstance(options, dict) else {}):
        if key not in LIVE_SOURCE_OPTIONS[kind]:
            problems.append(problem(
                "E_DATA_SOURCE_OPTION",
                f"data source '{name}': unknown option '{key}' for a {kind} source",
                f"{at}/options/{key}",
                hint=f"its options: {', '.join(LIVE_SOURCE_OPTIONS[kind])}",
                fields={"case": "unknown", "name": name, "key": key}))


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
                                    f"{at}/name", hint="give every binding a unique name",
                                    fields={"case": "empty"}))
        elif name in names:
            problems.append(problem("E_DATA_BINDING_NAME", f"duplicate data binding name: {name}",
                                    f"{at}/name", hint="rename one of the two bindings",
                                    fields={"case": "twice", "name": name}))
        names.add(name)

        source = entry.get("source")
        if source not in sources:
            problems.append(problem(
                "E_DATA_BINDING_SOURCE", f"binding '{name}': unknown data source '{source}'",
                f"{at}/source",
                hint=f"declare it under \"data_sources\"; declared: "
                     f"{', '.join(sorted(sources)) or '(none)'}",
                fields={"case": "unknown", "name": name, "source": source}))

        transform = entry.get("transform", "")
        if transform not in DATA_TRANSFORMS and not transform.startswith(DATA_TRANSFORM_PREFIXES):
            problems.append(problem(
                "E_DATA_BINDING_TRANSFORM", f"binding '{name}': unknown transform '{transform}'",
                f"{at}/transform",
                hint=f"use one of: {', '.join(DATA_TRANSFORMS)}, or "
                     f"{', '.join(p + '<argument>' for p in DATA_TRANSFORM_PREFIXES)}",
                fields={"case": "unknown", "name": name, "transform": transform}))

        if not entry.get("target"):
            problems.append(problem("E_DATA_BINDING_TARGET",
                                    f"binding '{name}': target must not be empty",
                                    f"{at}/target",
                                    hint="an override path, e.g. \"resources.Nurse.capacity\"",
                                    fields={"case": "target", "name": name}))
        # "count" needs no column, and "orders" reads several by its own `map`.
        if not entry.get("column") and transform not in ("count", "orders"):
            problems.append(problem("E_DATA_BINDING_TARGET",
                                    f"binding '{name}': column must not be empty",
                                    f"{at}/column",
                                    hint="name the column of the source this binding reads",
                                    fields={"case": "column", "name": name}))

        filters = entry.get("filter")
        if filters is not None and not isinstance(filters, dict):
            problems.append(problem(
                "E_DATA_FILTER", f"binding '{name}': filter must be an object",
                f"{at}/filter",
                hint="a filter is {\"column\": value} or {\"column >=\": value} -- data, not code",
                fields={"case": "object", "name": name}))
        elif filters:
            for key in filters:
                column = str(key).rsplit(" ", 1)[0] if " " in str(key) else str(key)
                op = str(key).rsplit(" ", 1)[1] if " " in str(key) else "=="
                if not column or op not in _FILTER_OPS:
                    problems.append(problem(
                        "E_DATA_FILTER", f"binding '{name}': bad filter key '{key}'",
                        f"{at}/filter",
                        hint=f"write \"<column>\" or \"<column> <op>\" with op one of: "
                             f"{', '.join(_FILTER_OPS)}",
                        fields={"case": "key", "name": name, "key": key}))


def _validate_data(layout: dict, problems: list) -> None:
    """The "data_sources" section hook (core/sections.py): sources, then the
    bindings that must name one of them."""
    _validate_data_bindings(layout, _validate_data_sources(layout, problems), problems)


def _check_belt_stops(problems: list, name: str, params: dict, at: str, resources: set) -> None:
    """A CONVEYOR's `stops` (D732): each a point strictly on the belt with a time of 0 or
    more (an expression is checked with the others) and, when named, a declared resource.
    A belt whose length comes from the network has its `at` checked when it is built."""
    stops = params.get("stops") or []
    if not isinstance(stops, list):
        stops = [stops]
    length = params.get("length", 0.0)
    known = isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0

    def number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(value)

    for index, stop in enumerate(stops):
        where = f"{at}/stops/{index}"
        if not isinstance(stop, dict) or set(stop) - {"at", "time", "resource"}:
            problems.append(problem(
                "E_CONVEYOR_STOP", f"{name}: stop {index} must be {{\"at\", \"time\", "
                f"\"resource\"?}}", where, block=name,
                hint="at is a distance from the belt's entry; time a number or an expression",
                fields={"case": "shape", "index": index}))
            continue
        place = stop.get("at")
        if not number(place) or place <= 0 or (known and place >= length):
            problems.append(problem(
                "E_CONVEYOR_STOP", f"{name}: stop {index} at {place!r} is not on the belt",
                f"{where}/at", block=name,
                hint="at is between 0 and the belt's length, both excluded: the end is the "
                     "belt's own stop (exit_resource)",
                fields={"case": "off", "index": index}))
        time = stop.get("time", 0)
        if not (isinstance(time, str) and time.strip()) and not (number(time) and time >= 0):
            problems.append(problem(
                "E_CONVEYOR_STOP", f"{name}: stop {index} time must be a number >= 0 or an "
                f"expression", f"{where}/time", block=name,
                hint="the time the part halts there, in the model's time unit",
                fields={"case": "time", "index": index}))
        resource = stop.get("resource", "")
        if resource and (not isinstance(resource, str) or resource.strip() not in resources):
            problems.append(problem(
                "E_CONVEYOR_STOP", f"{name}: stop {index}: unknown resource {resource!r}",
                f"{where}/resource", block=name,
                hint="the stop seizes an ordinary resource or set; declare it under "
                     "\"resources\"",
                fields={"case": "resource", "index": index, "resource": resource}))


def _belt_places(params: dict, *fields: str) -> list:
    """The `at` of every stop, divert or merge already on the belt (the fields named)."""
    return [p.get("at") for field in fields
            for p in (params.get(field) if isinstance(params.get(field), list) else [])
            if isinstance(p, dict) and is_finite_number(p.get("at"))]


def _check_belt_at(problems: list, name: str, params: dict, code: str, label: str, index: int,
                   item: dict, where: str, taken: list, off_hint: str, taken_hint: str) -> None:
    """The `at` of a divert or merge: strictly on the belt and not on another point (D851,
    D854). Records the point in *taken* when it is fine."""
    length = params.get("length", 0.0)
    known = isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0
    place = item.get("at")
    if not is_finite_number(place) or place <= 0 or (known and place >= length):
        problems.append(problem(
            code, f"{name}: {label} {index} at {place!r} is not on the belt",
            f"{where}/at", block=name, hint=off_hint,
            fields={"case": "a2_off", "index": index}))
    elif any(abs(place - other) <= 1e-9 for other in taken):
        problems.append(problem(
            code, f"{name}: {label} {index} at {place!r} is on another point of the belt",
            f"{where}/at", block=name, hint=taken_hint,
            fields={"case": "a2_taken", "index": index}))
    else:
        taken.append(place)


def _check_points_on_path(problems: list, name: str, params: dict, at: str, total: float) -> None:
    """A11 (D1371): a path belt has no `length`, so the point checks bound `at` by 0 only; each
    stop, divert, merge and eye must also lie before the path's end (both in length_unit)."""
    for field, label in (("stops", "stop"), ("diverts", "divert"), ("merges", "merge"),
                         ("eyes", "eye")):
        points = params.get(field)
        for index, point in enumerate(points if isinstance(points, list) else []):
            place = point.get("at") if isinstance(point, dict) else None
            if is_finite_number(place) and place >= total:
                problems.append(problem(
                    "E_CONVEYOR_PARAM", f"{name}: {label} {index} at {place!r} is past the end "
                    f"of the belt's path ({total:g})", f"{at}/{field}/{index}/at", block=name,
                    hint="at is a distance from the belt's entry, less than the path's length",
                    fields={"case": "path_at", "what": f"{label} {index}"}))


def _check_belt_path(problems: list, name: str, params: dict, at: str, path, length,
                     ends: list) -> None:
    """A CONVEYOR's `path` (conveyor system A): well-formed pieces, and the only source of the
    belt's length, so a length or end nodes beside it are two answers to one question."""
    try:
        total = path_length(parse_path(path))
    except ValueError as exc:
        total = None
        problems.append(problem(
            "E_CONVEYOR_PATH", f"{name}: {exc}", f"{at}/path", block=name,
            hint="a piece is {\"straight\": length} or {\"arc\": {\"radius\", \"angle\", \"turn\"}}",
            fields={"case": "piece"}))
    if total is not None:
        _check_points_on_path(problems, name, params, at, total)
    if ((isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0)
            or any(ends)):
        problems.append(problem(
            "E_CONVEYOR_PATH", f"{name}: a belt with a path takes its length from the path",
            f"{at}/path", block=name,
            hint="set `length` to 0 and clear from_node and to_node, or empty the path",
            fields={"case": "both"}))


def _check_belt_diverts(problems: list, name: str, params: dict, at: str) -> None:
    """A CONVEYOR's `diverts` (D851, D852): each a point strictly on the belt, not on another
    point, with a condition, a push time of 0 or more (expressions are checked with the
    others) and a when_full of "wait" or "pass". Its links are checked with the graph."""
    diverts = params.get("diverts") or []
    if not isinstance(diverts, list):
        diverts = [diverts]
    taken = _belt_places(params, "stops")
    for index, divert in enumerate(diverts):
        where = f"{at}/diverts/{index}"
        if not isinstance(divert, dict) or set(divert) - {
                "at", "condition", "time", "when_full", "intercept", "on_late", "side",
                "angle", "rule", "rule_group"} \
                or not isinstance(divert.get("intercept", False), bool) \
                or ("on_late" in divert and not divert.get("intercept")):
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {index} must be {{\"at\", \"condition\" or "
                f"\"rule\"/\"rule_group\", \"time\"?, \"when_full\"?, \"intercept\"?, "
                f"\"on_late\"?, \"side\"?, \"angle\"?}}", where,
                block=name,
                hint="at is a distance from the belt's entry; condition an expression; "
                     "on_late only with intercept",
                fields={"case": "a2_shape", "index": index}))
            continue
        _check_belt_at(problems, name, params, "E_CONVEYOR_DIVERT", "divert", index, divert,
                       where, taken,
                       "at is between 0 and the belt's length, both excluded: the end is link 0",
                       "move it, or the stop or divert already there")
        condition = divert.get("condition", "")
        routed = "rule" in divert or "rule_group" in divert       # A7: routed by its rule
        if not routed and not (isinstance(condition, bool)
                               or (isinstance(condition, str) and condition.strip())):
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {index} needs a condition",
                f"{where}/condition", block=name,
                hint="an expression read as the part reaches it, e.g. entity.lane == 2",
                fields={"case": "a2_condition", "index": index}))
        time = divert.get("time", 0)
        if (not (isinstance(time, str) and time.strip())
                and not (is_finite_number(time) and time >= 0)):
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {index} time must be a number >= 0 or "
                f"an expression", f"{where}/time", block=name,
                hint="the push time, in the model's time unit",
                fields={"case": "a2_time", "index": index}))
        if divert.get("when_full", "wait") not in ("wait", "pass"):
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {index} when_full must be \"wait\" or "
                f"\"pass\"", f"{where}/when_full", block=name,
                hint="wait: the part stays until its target has room; pass: it rides on",
                fields={"case": "a2_full", "index": index}))
        if divert.get("side", "left") not in ("left", "right"):
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {index} side must be \"left\" or "
                f"\"right\"", f"{where}/side", block=name,
                hint="the side of the belt's heading its exit port faces on the floor plan",
                fields={"case": "a3_side", "index": index}))
        angle = divert.get("angle", 90)
        if not (is_finite_number(angle) and 0 < angle < 180):
            problems.append(problem(
                "E_CONVEYOR_PARAM", f"{name}: divert {index} angle must be a number above 0 "
                f"and below 180", f"{where}/angle", block=name,
                hint="degrees from the belt's travel to the branch: 90 square, 45 leaning "
                     "forward",
                fields={"case": "divert_angle", "index": index}))
        if divert.get("intercept"):
            _check_belt_intercept(problems, name, divert, index, where)
    _check_belt_routes(problems, name, diverts, at)
    _check_merges_in_groups(problems, name, diverts, params.get("merges"), at)


def _group_rule(members: list) -> RouteRule | None:
    """The RouteRule of a divert group, its exits the diverts' ports when it names none; None
    when the group or its rule is not usable (reported by `_check_belt_routes`)."""
    rule = members[0][1].get("rule")
    if not isinstance(rule, dict):
        return None
    spec = rule_spec(rule, divert_exits(index for index, _divert in members))
    try:
        return RouteRule.from_dict(spec)
    except ValueError:
        return None


def _check_merges_in_groups(problems: list, name: str, diverts: list, merges, at: str) -> None:
    """R38: a part that joins at a merge between a rule group's first and last divert never
    meets the group's rule, which runs at its first divert, so it rides past every divert of
    the group: a warning."""
    if not isinstance(merges, list):
        return
    for key, members in divert_groups(diverts).items():
        ats = [d.get("at") for _index, d in members if is_finite_number(d.get("at"))]
        if len(ats) < 2:
            continue
        for index, merge in enumerate(merges):
            point = merge.get("at") if isinstance(merge, dict) else None
            if is_finite_number(point) and min(ats) < point < max(ats):
                problems.append(problem(
                    "W_ROUTE_RULE", f"{name}: merge {index} joins inside rule group "
                    f"{key!r}, past its first divert, so its parts are never routed",
                    f"{at}/merges/{index}/at", block=name,
                    hint="move the merge before the group's first divert, or after its last",
                    fields={"case": "merge_in_group", "index": index, "group": key}))


def _check_belt_routes(problems: list, name: str, diverts: list, at: str) -> None:
    """A CONVEYOR's routed diverts (A7, D1359): a `rule` on the first divert of its group
    along the belt and only there, a usable RouteRule with one exit per divert of the group,
    and no condition, robot pick or "pass" beside it."""
    for key, members in divert_groups(diverts).items():
        first, divert = members[0]
        where = f"{at}/diverts/{first}"
        for index, member in members:
            group = member.get("rule_group", "")
            condition = member.get("condition", "")
            if not isinstance(group, str) or ("rule_group" in member and not group.strip()):
                problems.append(problem(
                    "E_CONVEYOR_DIVERT", f"{name}: divert {index} rule_group must be a name",
                    f"{at}/diverts/{index}/rule_group", block=name,
                    hint="the diverts that share one rule name the same group",
                    fields={"case": "route_group", "index": index}))
            if (condition not in ("", None)) or member.get("intercept") \
                    or member.get("when_full", "wait") == "pass":
                problems.append(problem(
                    "E_CONVEYOR_DIVERT", f"{name}: divert {index} is routed by a rule, so it "
                    f"takes no condition, robot pick or when_full \"pass\"",
                    f"{at}/diverts/{index}", block=name,
                    hint="remove the condition, intercept or when_full: the rule picks the exit",
                    fields={"case": "route_mix", "index": index}))
        if "rule" not in divert or any("rule" in member for _i, member in members[1:]):
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {first} is the first of rule group "
                f"{key!r} along the belt, so it carries the rule, and only it",
                where, block=name,
                hint="give the rule once, on the group's first divert; the others name the group",
                fields={"case": "route_group", "index": first}))
            continue
        spec = rule_spec(divert["rule"], divert_exits(index for index, _member in members))
        try:
            validate_rule(spec)
        except ValueError as exc:
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {first} rule: {exc}", f"{where}/rule",
                block=name, hint="kind is one of fixed, sequential, cyclic, round_robin, "
                                 "least_loaded, condition, attribute, item",
                fields={"case": "route_rule", "index": first}))
            continue
        try:
            divert_slots(spec["exits"], len(members))
        except ValueError as exc:
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {first} rule: {exc}", f"{where}/rule/exits",
                block=name, hint="one exit per divert of the group, in belt order, and at most "
                                 "one exit named out (the belt's end)",
                fields={"case": "route_rule", "index": first}))


def _check_belt_intercept(problems: list, name: str, divert: dict, index: int,
                          where: str) -> None:
    """An intercept divert (D1343) hands the part to a robot that meets it on the move: it
    has no push time and no places to be full (the robot's readiness is its room), and its
    on_late is "wait", "pass" or an expression."""
    time = divert.get("time", 0)
    timed = not (isinstance(time, (int, float)) and not isinstance(time, bool) and time == 0)
    if "when_full" in divert or timed:
        field = "when_full" if "when_full" in divert else "time"
        problems.append(problem(
            "E_CONVEYOR_DIVERT", f"{name}: divert {index} meets a robot, so it takes no push "
            f"time and no when_full", f"{where}/{field}", block=name,
            hint="remove time and when_full: the robot's readiness is the room",
            fields={"case": "a2_intercept", "index": index}))
    late = divert.get("on_late", "wait")
    if not isinstance(late, str) or not late.strip():
        problems.append(problem(
            "E_CONVEYOR_DIVERT", f"{name}: divert {index} on_late must be wait, pass or an "
            f"expression", f"{where}/on_late", block=name,
            hint="wait: the part stands for the robot; pass: it rides on; else an expression "
                 "read per part, true to wait",
            fields={"case": "a2_late", "index": index}))


def _check_belt_eyes(problems: list, name: str, params: dict, at: str) -> None:
    """A CONVEYOR's `eyes` (D1347): each {at, edge?, assign?, signal?} on the belt, not on
    another point (a back eye at its `at` plus the part length), and `part_length` no longer
    than the spacing. Expressions of the assign rows are checked with the others."""
    length = params.get("length", 0.0)
    known = is_finite_number(length) and length > 0
    spacing = params.get("spacing", 1.0)
    part = params.get("part_length", 0.0)
    # Lengths are compared only in one unit; the engine checks the rest at build.
    units = {k: params.get(k + "_unit", "base") for k in ("length", "spacing", "part_length")}
    if is_finite_number(part) and part > 0:
        same_unit = units["part_length"] == units["length"]
        with_spacing = units["part_length"] == units["spacing"]
    else:
        same_unit = with_spacing = units["spacing"] == units["length"]
    # A typed belt ignores `spacing`; the engine checks its cell at build.
    if with_spacing and not params.get("type") and is_finite_number(part) \
            and is_finite_number(spacing) and part > spacing + 1e-9:
        problems.append(problem(
            "E_CONVEYOR_EYE", f"{name}: part_length {part!r} is longer than the spacing "
            f"{spacing!r}", f"{at}/part_length", block=name,
            hint="a part is no longer than the distance between two parts",
            fields={"case": "a2_length"}))
    eyes = params.get("eyes") or []
    if not isinstance(eyes, list):
        eyes = [eyes]
    taken = _belt_places(params, "stops", "diverts", "merges")
    back = part if is_finite_number(part) and part > 0 else spacing
    for index, eye in enumerate(eyes):
        where = f"{at}/eyes/{index}"
        if not isinstance(eye, dict) or set(eye) - {"at", "edge", "assign", "signal"}:
            problems.append(problem(
                "E_CONVEYOR_EYE", f"{name}: eye {index} must be {{\"at\", \"edge\"?, "
                f"\"assign\"?, \"signal\"?}}", where, block=name,
                hint="at is a distance from the belt's entry",
                fields={"case": "a2_shape", "index": index}))
            continue
        edge = eye.get("edge", "front")
        if edge not in ("front", "back"):
            problems.append(problem(
                "E_CONVEYOR_EYE", f"{name}: eye {index} edge {edge!r} is not front or back",
                f"{where}/edge", block=name,
                hint="front: as the part's front reaches `at`; back: as its back clears it",
                fields={"case": "a2_edge", "index": index}))
        place = eye.get("at")
        on_belt = is_finite_number(place) and place > 0 and (not known or place < length)
        # A typed belt's item lengths come from the entity types, which the engine checks.
        if (edge == "back" and on_belt and same_unit and is_finite_number(back)
                and not params.get("type")):
            place += back       # the front's place when the back clears `at`
            if known and place >= length - 1e-9:
                problems.append(problem(
                    "E_CONVEYOR_EYE", f"{name}: eye {index}'s back edge is at {place:g}, past "
                    f"the end", f"{where}/at", block=name,
                    hint="a back eye needs a part length of room before the end",
                    fields={"case": "a2_back", "index": index}))
                continue
        # A typed back eye is placed per item length at build: only its sensor is checked here.
        clear = [] if edge == "back" and params.get("type") else taken
        _check_belt_at(problems, name, params, "E_CONVEYOR_EYE", "eye", index, {"at": place},
                       where, clear, "at is between 0 and the belt's length, both excluded",
                       "move it, or the point already there")
        assign = eye.get("assign", [])
        for row, entry in enumerate(assign if isinstance(assign, list) else [None]):
            target = entry.get("target") if isinstance(entry, dict) else None
            expr = entry.get("expr") if isinstance(entry, dict) else None
            ok = isinstance(target, str) and bool(target.strip()) and not isinstance(expr, bool) \
                and ((isinstance(expr, str) and bool(expr.strip())) or is_finite_number(expr)) \
                and not set(entry) - {"target", "expr"}
            if not ok:
                problems.append(problem(
                    "E_CONVEYOR_EYE", f"{name}: eye {index} assignment {row} needs a target "
                    f"and an expression", f"{where}/assign/{row}", block=name,
                    hint="a row is {\"target\": \"entity.x\", \"expr\": \"1\"}",
                    fields={"case": "a2_assign", "index": index, "row": row}))
        if not isinstance(eye.get("signal", ""), str):
            problems.append(problem(
                "E_CONVEYOR_EYE", f"{name}: eye {index} signal must be a name",
                f"{where}/signal", block=name, hint="the name of the signal the eye raises",
                fields={"case": "a2_signal", "index": index}))


# A CONVEYOR_TRANSFER's numbers: 0 is a real value for these, not for a speed or a rate
_TRANSFER_AT_LEAST_0 = ("lift_time", "transfer_speed", "cross_length", "entry_length",
                        "turn_ramp", "lift_height")
_TRANSFER_ABOVE_0 = ("turn_rate", "lift_speed")


def _check_transfer(problems: list, name: str, params: dict, at: str) -> None:
    """A CONVEYOR_TRANSFER's mechanism, its exits {port, angle} with distinct ports, and its
    times, lengths and speeds (conveyor system A8)."""
    mechanism = params.get("mechanism", "popup")
    if mechanism not in TRANSFER_MECHANISMS:
        problems.append(problem(
            "E_CONVEYOR_PARAM", f"{name}: mechanism {mechanism!r} is not one of "
            f"{', '.join(TRANSFER_MECHANISMS)}", f"{at}/mechanism", block=name,
            hint="a pop-up transfer, a turntable or a lift",
            fields={"case": "transfer_mechanism", "mechanism": str(mechanism)}))
    exits = params.get("exits", [])
    seen = set()
    for index, spec in enumerate(exits if isinstance(exits, list) else [None]):
        port = spec.get("port") if isinstance(spec, dict) else None
        if not (isinstance(port, str) and port.strip() and is_finite_number(spec.get("angle"))
                and not set(spec) - {"port", "angle"}):
            problems.append(problem(
                "E_CONVEYOR_PARAM", f"{name}: exit {index} needs a port name and an angle",
                f"{at}/exits/{index}", block=name,
                hint="each exit is {\"port\": <name>, \"angle\": <degrees from the entry "
                     "heading>}",
                fields={"case": "transfer_exit", "index": index}))
        elif ":" in port:
            problems.append(problem(
                "E_CONVEYOR_PARAM", f"{name}: exit {index} port {port!r} has a ':'",
                f"{at}/exits/{index}/port", block=name,
                hint="a routing rule reads \"name:target\" in an exit name; use another "
                     "character in the port",
                fields={"case": "transfer_port_colon", "index": index, "port": port}))
        elif port.strip() in seen:
            problems.append(problem(
                "E_CONVEYOR_PARAM", f"{name}: two exits share the port {port!r}",
                f"{at}/exits/{index}/port", block=name,
                hint="give each exit its own port name: links name their exit by it",
                fields={"case": "transfer_exit_twice", "port": port}))
        else:
            seen.add(port.strip())
    for field in _TRANSFER_AT_LEAST_0 + _TRANSFER_ABOVE_0:
        value = params.get(field)
        if value is None:
            continue
        hint = "times, lengths and speeds of the module are in the units next to them"
        if field in _TRANSFER_ABOVE_0:
            if not is_finite_number(value) or value <= 0:
                problems.append(problem(
                    "E_CONVEYOR_PARAM", f"{name}: {field} must be a number > 0",
                    f"{at}/{field}", block=name, hint=hint,
                    fields={"case": "number", "field": field}))
        elif not is_finite_number(value) or value < 0:
            problems.append(problem(
                "E_CONVEYOR_PARAM", f"{name}: {field} must be a number >= 0",
                f"{at}/{field}", block=name, hint=hint,
                fields={"case": "transfer_number", "field": field}))


def _check_belt_zones(problems: list, name: str, params: dict, at: str) -> None:
    """A CONVEYOR's `zones` (D1356): {"count": a whole number >= 1, "release_delay"?: a
    number >= 0, "time_unit"?}, on an accumulating belt with no stop or merge (R24: the
    cascade runs at the exit only); {} is none."""
    zones = params.get("zones")
    if zones in (None, {}):
        return
    count = zones.get("count") if isinstance(zones, dict) else None
    delay = zones.get("release_delay", 0) if isinstance(zones, dict) else None
    unit = zones.get("time_unit", "base") if isinstance(zones, dict) else None
    if not isinstance(zones, dict) or set(zones) - {"count", "release_delay", "time_unit"} \
            or not isinstance(count, int) or isinstance(count, bool) or count < 1 \
            or not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0 \
            or unit not in ("base", *TIME_UNITS):
        problems.append(problem(
            "E_CONVEYOR_PARAM", f"{name}: zones must be {{\"count\": <whole number >= 1>, "
            f"\"release_delay\"?: <number >= 0>, \"time_unit\"?}}", f"{at}/zones",
            block=name, hint="count splits the belt into equal zones, zone 1 at the exit",
            fields={"case": "zones"}))
    elif params.get("accumulating", True) is False:
        problems.append(problem(
            "E_CONVEYOR_PARAM", f"{name}: zones need an accumulating belt", f"{at}/zones",
            block=name, hint="set accumulating to true, or remove the zones",
            fields={"case": "zones_belt"}))
    elif params.get("stops") or params.get("merges"):
        problems.append(problem(
            "E_CONVEYOR_PARAM", f"{name}: zones on a belt with stops or merges are not "
            f"supported yet", f"{at}/zones", block=name,
            hint="split the belt at the stop or merge, or remove the zones",
            fields={"case": "zones_points"}))
    elif any(rule is not None and rule.can_hold() for rule in map(
            _group_rule, divert_groups(params.get("diverts") or []).values())):
        # R24 (A7, D1359): a held part would stand inside a zone's cascade
        problems.append(problem(
            "E_CONVEYOR_PARAM", f"{name}: zones on a belt with a routing rule that can hold a "
            f"part are not supported yet", f"{at}/zones", block=name,
            hint="give the rule no limit, or put the diverts on a belt without zones",
            fields={"case": "zones_hold"}))
    if params.get("diverts") and params.get("accumulating", True) is not False:
        # R40: a divert bounds a section (and its eyes stay points), so the cascade runs only
        # past the last divert or eye; allowed, but its zone times are not modelled
        problems.append(problem(
            "W_ROUTE_RULE", f"{name}: zones on a belt with diverts: zone timing is not "
            f"modelled behind the last divert or eye", f"{at}/diverts", block=name,
            hint="put the diverts on a belt without zones, before or after the zoned one",
            fields={"case": "zones_divert"}))


def _release_rule_ok(form, rule) -> bool:
    """One `release` form's settings (D1357): metering {gap} or {interval}, singulation
    {gap}, slug {count, timeout, time_unit?}; lengths and the timeout numbers >= 0."""
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) \
            and math.isfinite(value) and value >= 0
    if not isinstance(rule, dict):
        return False
    if form == "metering":
        return len(rule) == 1 and set(rule) <= {"gap", "interval"} \
            and number(next(iter(rule.values())))
    if form == "singulation":
        return set(rule) == {"gap"} and number(rule["gap"])
    if form == "slug":
        count = rule.get("count")
        return {"count", "timeout"} <= set(rule) <= {"count", "timeout", "time_unit"} \
            and isinstance(count, int) and not isinstance(count, bool) and count >= 1 \
            and number(rule.get("timeout")) \
            and rule.get("time_unit", "base") in ("base", *TIME_UNITS)
    return False


def _check_belt_release(problems: list, name: str, params: dict, at: str) -> None:
    """A CONVEYOR's `release` (D1357): exactly one form with its settings, on an accumulating
    belt without zones; {} is none."""
    release = params.get("release")
    if release in (None, {}):
        return
    if not isinstance(release, dict) or len(release) != 1 \
            or not _release_rule_ok(*next(iter(release.items()))):
        problems.append(problem(
            "E_CONVEYOR_RELEASE", f"{name}: release must be one of {{\"metering\": {{\"gap\" or "
            f"\"interval\": <length>}}}}, {{\"singulation\": {{\"gap\": <length>}}}} or "
            f"{{\"slug\": {{\"count\": <whole number >= 1>, \"timeout\": <time>}}}}",
            f"{at}/release", block=name, hint="give one form; lengths and the timeout are >= 0",
            fields={"case": "shape"}))
    elif params.get("accumulating", True) is False:
        problems.append(problem(
            "E_CONVEYOR_RELEASE", f"{name}: a release rule needs an accumulating belt",
            f"{at}/release", block=name, hint="set accumulating to true, or remove the rule",
            fields={"case": "belt"}))
    elif params.get("zones"):
        problems.append(problem(
            "E_CONVEYOR_RELEASE", f"{name}: a release rule and zones on one belt are not "
            f"supported", f"{at}/release", block=name,
            hint="put the rule on its own belt after the zoned one, or remove the zones",
            fields={"case": "zones"}))


# D1368, R44: what a reversible belt may not have, (case, field, words)
_REVERSE_REFUSED = (("stops", "stops", "stops"), ("diverts", "diverts", "diverts"),
                    ("picks", "diverts", "robot picks"), ("merges", "merges", "merges"),
                    ("eyes", "eyes", "photo-eyes"), ("zones", "zones", "zones"),
                    ("release", "release", "a release rule"))


def _check_belt_reverse(problems: list, name: str, params: dict, at: str) -> None:
    """A reversible CONVEYOR (D1368) has no stops, diverts, robot picks, merges, photo-eyes,
    zones or release rule: their meaning in the other direction is not defined. Two-way lines
    are plain reversible pieces between mechanisms. Its exits are checked with the graph."""
    flag = params.get("reversible", False)
    if not isinstance(flag, bool):
        problems.append(problem(
            "E_CONVEYOR_REVERSE", f"{name}: reversible must be true or false, not {flag!r}",
            f"{at}/reversible", block=name, hint="write true or false, without quotes",
            fields={"case": "flag"}))
        return
    if not flag:
        return
    diverts = [d for d in params.get("diverts") or [] if isinstance(d, dict)] \
        if isinstance(params.get("diverts"), list) else []
    picks = any(d.get("intercept") is True for d in diverts)
    has = {"stops": params.get("stops"), "picks": picks,
           "diverts": params.get("diverts") and not picks, "merges": params.get("merges"),
           "eyes": params.get("eyes"), "zones": params.get("zones"),
           "release": params.get("release")}
    for case, field, words in _REVERSE_REFUSED:
        if has[case]:
            problems.append(problem(
                "E_CONVEYOR_REVERSE", f"{name}: a reversible belt cannot have {words}",
                f"{at}/{field}", block=name,
                hint=f"put the {words} on a belt of its own before or after this one, or set "
                     f"reversible to false",
                fields={"case": "mechanism", "what": words}))


def _check_belt_merges(problems: list, name: str, params: dict, at: str) -> None:
    """A CONVEYOR's `merges` (D854): each a point strictly on the belt, not on another
    point, naming its source block once, with a priority of "belt" or "merge", on an
    accumulating belt. That the source links to the belt is checked with the graph."""
    merges = params.get("merges") or []
    if not isinstance(merges, list):
        merges = [merges]
    if not merges:
        return
    if params.get("accumulating", True) is False:
        problems.append(problem(
            "E_CONVEYOR_MERGE", f"{name}: merges need an accumulating belt", f"{at}/merges",
            block=name, hint="set accumulating to true, or feed the belt at its entry",
            fields={"case": "a2_belt"}))
    taken = _belt_places(params, "stops", "diverts")
    sources = set()
    for index, merge in enumerate(merges):
        where = f"{at}/merges/{index}"
        if not isinstance(merge, dict) or set(merge) - {"at", "from", "priority"}:
            problems.append(problem(
                "E_CONVEYOR_MERGE", f"{name}: merge {index} must be {{\"at\", \"from\", "
                f"\"priority\"?}}", where, block=name,
                hint="at is a distance from the belt's entry; from the block whose parts join",
                fields={"case": "a2_shape", "index": index}))
            continue
        _check_belt_at(problems, name, params, "E_CONVEYOR_MERGE", "merge", index, merge,
                       where, taken,
                       "at is between 0 and the belt's length, both excluded: the entry is 0",
                       "move it, or the stop, divert or merge already there")
        source = merge.get("from")
        if not isinstance(source, str) or not source.strip() or source in sources:
            problems.append(problem(
                "E_CONVEYOR_MERGE", f"{name}: merge {index} needs its own `from` block",
                f"{where}/from", block=name,
                hint="the block linking to this belt whose parts join here, one merge each",
                fields={"case": "a2_from", "index": index}))
        else:
            sources.add(source)
        if merge.get("priority", "belt") not in ("belt", "merge"):
            problems.append(problem(
                "E_CONVEYOR_MERGE", f"{name}: merge {index} priority must be \"belt\" or "
                f"\"merge\"", f"{where}/priority", block=name,
                hint="belt: merging parts wait for a gap; merge: the belt makes room",
                fields={"case": "a2_priority", "index": index}))


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
                    block=name, hint=f"one of: {', '.join(TRAVEL_MODES)}",
                    fields={"case": "unknown", "mode": mode}))
            elif mode == "network" and not nodes:
                problems.append(problem(
                    "E_TRAVEL_MODE", f"{name}: travel mode \"network\" needs a path network",
                    "/network", block=name,
                    hint="add a \"network\" section, and a `node` to every STATION involved",
                    fields={"case": "network"}))

        elif btype == "TRANSPORT":
            fleet = str(params.get("fleet", "") or "").strip()
            if not fleet or fleet not in fleets:
                problems.append(problem(
                    "E_FLEET_UNKNOWN", f"{name}: unknown mover fleet '{fleet}'", f"{at}/fleet",
                    block=name, hint=f"declare it under \"fleets\"; declared: {known_fleets}",
                    fields={"case": "unknown", "fleet": fleet}))
            destination = str(params.get("destination", "") or "").strip()
            if destination and destination not in stations:
                problems.append(problem(
                    "E_SEQUENCE_STATION_UNKNOWN",
                    f"{name}: unknown destination station '{destination}'", f"{at}/destination",
                    block=name,
                    hint="name a STATION block, or leave it empty to follow the routing",
                    fields={"case": "a2_destination", "destination": destination}))
            elif destination and destination not in places:
                problems.append(problem(
                    "E_NETWORK_NODE_UNKNOWN",
                    f"{name}: destination station '{destination}' sits on no network node",
                    f"{at}/destination", block=name,
                    hint=f"set that STATION's `node` param; declared nodes: {known_nodes}",
                    fields={"case": "a2_destination", "destination": destination}))
            # A vehicle must at least be able to GET there from where it parks
            # (a gantry needs no such path, D118).
            if destination:
                pairs.extend((str(f.get("home", "") or ""), destination)
                             for f in layout.get("fleets", []) or []
                             if isinstance(f, dict) and str(f.get("name", "")).strip()
                             not in gantry_fleets)

        elif btype == "CONVEYOR_TRANSFER":
            _check_transfer(problems, name, params, at)

        elif btype == "CONVEYOR":
            for field in ("speed", "spacing"):
                value = params.get(field, 1.0)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    problems.append(problem(
                        "E_CONVEYOR_PARAM", f"{name}: {field} must be a number > 0",
                        f"{at}/{field}", block=name,
                        hint="speed is belt length per time unit; spacing is the belt one "
                             "entity takes",
                        fields={"case": "number", "field": field}))
            for field in ("gap", "cell"):
                value = params.get(field, 0.0)
                if not is_finite_number(value) or value < 0:
                    problems.append(problem(
                        "E_CONVEYOR_PARAM", f"{name}: {field} must be a number >= 0",
                        f"{at}/{field}", block=name,
                        hint="gap is a typed belt's free length per item; cell its cell "
                             "length, 0 for 1 cm",
                        fields={"case": "at_least_0", "field": field}))
            belt_type = params.get("type", "")
            if belt_type and belt_type not in CONVEYOR_TYPES:
                problems.append(problem(
                    "E_CONVEYOR_PARAM", f"{name}: unknown conveyor type {belt_type!r}",
                    f"{at}/type", block=name,
                    hint=f"a type is one of {', '.join(CONVEYOR_TYPES)}, or empty for an "
                         "untyped belt",
                    fields={"case": "type", "type": str(belt_type)}))
            length = params.get("length", 0.0)
            if not isinstance(length, (int, float)) or isinstance(length, bool) or length < 0:
                problems.append(problem(
                    "E_CONVEYOR_PARAM", f"{name}: length must be a number >= 0", f"{at}/length",
                    block=name, hint="0 takes the network distance between the two nodes",
                    fields={"case": "length"}))
            ends = [str(params.get(f, "") or "").strip() for f in ("from_node", "to_node")]
            for field, node in zip(("from_node", "to_node"), ends):
                if node and node not in nodes:
                    problems.append(problem(
                        "E_CONVEYOR_PARAM", f"{name}: unknown network node '{node}'",
                        f"{at}/{field}", block=name, hint=f"declared nodes: {known_nodes}",
                        fields={"case": "node", "node": node}))
            path = params.get("path")
            has_path = bool(path)
            if has_path:
                _check_belt_path(problems, name, params, at, path, length, ends)
            if (not length) and not all(ends) and not has_path:
                problems.append(problem(
                    "E_CONVEYOR_PARAM",
                    f"{name}: no length: set `length`, or both `from_node` and `to_node`",
                    f"{at}/length", block=name,
                    hint="a belt needs a length before it can have a capacity or a travel time",
                    fields={"case": "no_length"}))
            exit_resource = str(params.get("exit_resource", "") or "").strip()
            if exit_resource and exit_resource not in resources:
                problems.append(problem(
                    "E_CONVEYOR_PARAM", f"{name}: unknown resource '{exit_resource}'",
                    f"{at}/exit_resource", block=name,
                    hint="the end stop is an ordinary resource; declare it under \"resources\"",
                    fields={"case": "resource", "resource": exit_resource}))
            _check_belt_stops(problems, name, params, at, resources)
            _check_belt_diverts(problems, name, params, at)
            _check_belt_eyes(problems, name, params, at)
            _check_belt_merges(problems, name, params, at)
            _check_belt_zones(problems, name, params, at)
            _check_belt_release(problems, name, params, at)
            _check_belt_reverse(problems, name, params, at)
            problems.extend(conveyor_look.look_problems(name, params, at))

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
            hint="add a segment joining them, or make the segment between them two-way",
            fields={"case": "none", "a": a, "b": b}))
