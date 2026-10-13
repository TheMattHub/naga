"""A plant as a timeline (PHASE7 7.11, D252 to D259).

A plant says "this key has this value at this time" (`live.Event`). A PLANT MAPPING, a
`*.plantmap.json`, says what each key asserts about a model: which block an item is in, how
many units of a resource are busy, what a level holds. `Mapper` turns the observations into
`TraceEvent`s of the engine's own kinds, and the one `Timeline` (ARCHITECTURE section 6,
rule 13) reduces them, so every view of a run shows the plant with no view code changed:

    mapping, problems = plant.load("line.plantmap.json")
    events, mapper = plant.replay(mapping, model, plant.read_log("line.jsonl")[0])
    Timeline(events).state_at(mapper.now)

* The mapper is STATE-BASED (D253): it holds what each key last asserted and emits the
  minimal events that take the timeline there, so an observation delivered twice emits
  nothing, and delivery at least once is harmless.
* What the plant did not name is SYNTHETIC and marked by construction (D254): an anonymous
  member of a queue has an id below -1, a busy or blocked unit is held by entity -1 from no
  block.
* A contradiction or a hole is a counted, coded Problem with a stated rule (D256), never a
  repair. Every bound has a ceiling in `LIMITS`; what is over one is dropped and counted.
* Nothing from a log is text in an output: an id becomes an integer of the mapper's own, and
  every name comes from the mapping, checked against the model.

A mapping and a log are data: they name no file, host or connection and select no code.
Stdlib and `nagasim.core` only.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from nagasim.core import schema as model_schema, submodels
from nagasim.core.problems import Problem, problem
from nagasim.core.timeunits import UNITS, units_of
from nagasim.core.trace import DEFAULT_MAX_EVENTS, TraceEvent
from nagasim import readers
from nagasim.live import MAX_TEXT, SKEW_SECONDS, Feed, stamp

__all__ = ["FORMAT", "LIMITS", "KINDS", "STATES", "PlantError", "load", "check", "schema",
           "scaffold", "Mapper", "UNTYPED",
           "read_log", "write_log", "in_order", "replay", "Watch", "log_of", "main"]

FORMAT = 1
LIMITS = {
    "map_bytes": 2 ** 20,           # a mapping file
    "signals": 2000,                # signals of a mapping
    "entries": 10_000,              # entries of one table of a signal
    "log_bytes": 256 * 2 ** 20,     # a recorded log
    "line_bytes": 64 * 2 ** 10,     # one line of it
    "lines": 1_000_000,            # observations of it
    "events": DEFAULT_MAX_EVENTS,   # mapped events of a recording: the trace's own cap
    "entities": 100_000,            # entities known at once (`state.MAX_ENTITIES`)
    "keys": 100_000,                # keys a mapper remembers the last stamp of
    "left": 10_000,                 # items that left, remembered so that none comes back (#372)
    "count": 10_000,                # a queue length or a unit count
    "seconds": 100 * 365 * 86400,   # model time, from the epoch
}
# as -> (the field naming what it speaks of, the other fields it may carry)
KINDS = {
    "state": ("resource", ("states",)),
    "capacity": ("resource", ()),
    "busy": ("resource", ()),
    "blocked": ("resource", ()),
    "job": ("resource", ()),
    "queue": ("block", ("type",)),
    "at": ("blocks", ("gone", "merged", "type", "type_field", "types")),
    "seen": ("block", ("leaves", "type")),
    "level": ("level", ("scale",)),
    "counter": ("block", ("rollover",)),
}
STATES = ("busy", "idle", "blocked", "down", "up")
_UNITS_STATES = frozenset({"busy", "idle", "blocked"})     # the ones that hold or free a unit
UNTYPED = "Unknown"             # the type of an entity nothing gives one (as a trace writes it)
_SCALARS = (bool, int, float, str, type(None))
_COMMON = ("source", "key", "field", "as")
_HINT = "see the plant mapping page of the documentation, or start from `naga plant init`"


class PlantError(Exception):
    """A mapping that may not be used; `.problems` says why."""

    def __init__(self, problems):
        super().__init__("; ".join(p.message for p in problems))
        self.problems = list(problems)


def _text(value) -> str:
    """A value as the text a table of the mapping is keyed by: `true`, `false`, `null`, a
    whole number with no fraction (an enumeration read as 2.0 is 2)."""
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return json.dumps(value)


def _flat(layout) -> dict:
    """The model as a run sees it: migrated, every submodel instance expanded."""
    model = model_schema.migrate(model_schema.read_layout(layout))
    return submodels.flatten(model) if submodels.has_submodels(model) else model


def _names(layout) -> dict:
    flat = _flat(layout)

    def of(section):
        return [str(r["name"]) for r in flat.get(section) or [] if r.get("name")]
    types = {str(e).upper() for e in of("entities")}
    types |= {str((b.get("params") or {}).get(key)).upper() for b in flat.get("blocks") or []
              for key in ("e_type", "batch_type") if (b.get("params") or {}).get(key)}
    return {"block": of("blocks"), "resource": of("resources") + of("fleets"),
            "level": of("levels"), "types": sorted(types), "unit": units_of(flat.get("simulation")).time}


# ---------------------------------------------------------------- the mapping file
def load(path) -> tuple:
    """(the mapping in the file *path*, or None; the problems of reading it). Bounded before
    it is parsed; what it holds is then judged by `check`."""
    try:
        if Path(path).stat().st_size > LIMITS["map_bytes"]:
            return None, [problem("E_PLANTMAP_SIZE", f"the mapping file is over "
                                  f"{LIMITS['map_bytes']} bytes", "/", hint=_HINT)]
        return json.loads(Path(path).read_text(encoding="utf-8")), []
    except (OSError, ValueError, RecursionError) as exc:
        return None, [problem("E_PLANTMAP_FORMAT", f"the mapping file cannot be read "
                              f"({type(exc).__name__})", "/", hint=_HINT)]


def _table(value, allowed=None) -> str | None:
    """Why *value* is not a table of text to text (None: it is one)."""
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        return "must be an object of text to text"
    if allowed is not None and not set(value.values()) <= set(allowed):
        return f"may only name {', '.join(allowed)}"
    return None


def check(mapping, layout=None) -> list[Problem]:
    """The problems of *mapping*; with *layout*, also against that model's names. [] when a
    `Mapper` may use it."""
    found: list[Problem] = []

    def bad(message, at="/", code="E_PLANTMAP_FORMAT", hint=_HINT):
        found.append(problem(code, f"mapping: {message}", at, hint=hint))

    if not isinstance(mapping, dict) or mapping.get("format") != FORMAT:
        bad(f"this is not a plant mapping of format {FORMAT}")
        return found
    for key in mapping.keys() - {"format", "description", "time", "signals"}:
        bad(f"unknown key '{str(key)[:80]}'", f"/{str(key)[:80]}")
    time = mapping.get("time")
    epoch = _instant(time["epoch"]) if isinstance(time, dict) \
        and isinstance(time.get("epoch"), str) else None
    if epoch is None or not 0 <= epoch <= _LAST_EPOCH:
        # Bounded: an instant outside what the platform's clock can name would make every
        # stamp of the timeline raise (Windows refuses a date before 1970).
        bad("time.epoch must be an ISO 8601 instant from 1970 to 2200: what model time 0 "
            "stands for", "/time/epoch")
        time = {}
    if time.get("unit", "minutes") not in UNITS:
        bad(f"time.unit must be one of {', '.join(UNITS)}", "/time/unit")
    if time.get("stamp", "source") not in ("source", "received"):
        bad("time.stamp must be \"source\" or \"received\"", "/time/stamp")
    for key in time.keys() - {"epoch", "unit", "stamp"}:
        bad(f"unknown key 'time.{str(key)[:80]}'", "/time")
    signals = mapping.get("signals", [])
    if not isinstance(signals, list):
        bad("signals must be a list", "/signals")
        return found
    if len(signals) > LIMITS["signals"]:
        bad(f"over the bound of {LIMITS['signals']} signals", "/signals", "E_PLANTMAP_SIZE")
        return found
    names = _names(layout) if layout is not None else None
    drivers: dict[str, set] = {}        # resource -> what holds its units
    members: dict[str, set] = {}        # block -> how its content is told
    told: dict[str, int] = {}           # a thing only one signal may tell -> how many do
    for index, signal in enumerate(signals):
        at = f"/signals/{index}"
        kind = signal.get("as") if isinstance(signal, dict) else None
        if kind not in KINDS:
            bad(f"signal {index}: \"as\" must be one of {', '.join(KINDS)}", f"{at}/as")
            continue
        target, extra = KINDS[kind]
        for key in signal.keys() - {*_COMMON, target, *extra}:
            bad(f"signal {index}: unknown key '{str(key)[:80]}' for \"{kind}\"", at)
        key = signal.get("key")
        if not isinstance(key, str) or not key:
            bad(f"signal {index}: key must be a text", f"{at}/key")
            continue
        if len(key.encode("utf-8", "replace")) > MAX_TEXT:
            bad(f"signal {index}: key is over {MAX_TEXT} bytes", f"{at}/key", "E_PLANTMAP_SIZE")
            continue
        if key.count("{id}") != (kind == "at") or key.count("*") > (kind == "job"):
            bad(f"signal {index}: an \"at\" key holds one {{id}}, a \"job\" key may hold one "
                f"*, and no other key holds either", f"{at}/key")
        for name in ("source", "field", "type", "type_field"):
            if name in signal and not (isinstance(signal[name], str) and signal[name]
                                       and len(signal[name]) <= 200):
                bad(f"signal {index}: {name} must be a text of at most 200 characters",
                    f"{at}/{name}")
        tables = {"states": STATES, "blocks": None, "types": None}
        for name, allowed in tables.items():
            if name in signal or (name, kind) in (("states", "state"), ("blocks", "at")):
                reason = _table(signal.get(name), allowed)
                if reason:
                    bad(f"signal {index}: {name} {reason}", f"{at}/{name}")
                elif len(signal[name]) > LIMITS["entries"]:
                    bad(f"signal {index}: {name} is over {LIMITS['entries']} entries",
                        f"{at}/{name}", "E_PLANTMAP_SIZE")
        for name in ("gone", "merged"):
            codes = signal.get(name, [])
            if not isinstance(codes, list) or len(codes) > LIMITS["entries"] or not all(
                    isinstance(c, (str, int, float, bool, type(None))) for c in codes):
                bad(f"signal {index}: {name} must be a list of values", f"{at}/{name}")
        scale = signal.get("scale", 1.0)
        if not _number(scale) or scale == 0:
            bad(f"signal {index}: scale must be a number other than 0", f"{at}/scale")
        roll = signal.get("rollover", 2)
        if isinstance(roll, bool) or not _number(roll) or roll < 2 or not float(roll).is_integer():
            bad(f"signal {index}: rollover must be a whole number of at least 2: the value "
                f"the counter returns to 0 at", f"{at}/rollover")
        if "leaves" in signal and not isinstance(signal["leaves"], bool):
            bad(f"signal {index}: leaves must be true or false", f"{at}/leaves")
        # What it speaks of: one name, a table of them, or (seen) a way out.
        if kind == "at":
            named = list(signal["blocks"].values()) if isinstance(signal.get("blocks"), dict) else []
        elif kind == "seen" and signal.get("leaves"):
            named = []
        else:
            named = [signal.get(target)]
            if not isinstance(named[0], str) or not named[0]:
                bad(f"signal {index}: \"{kind}\" needs \"{target}\"", f"{at}/{target}")
                continue
        group = "block" if target == "blocks" else target
        if names is not None:
            for name in named:
                if isinstance(name, str) and name not in names[group]:
                    bad(f"signal {index}: the model has no {group} '{name[:80]}'", f"{at}/{target}",
                        "E_PLANTMAP_NAME", f"its {group}s: "
                        f"{', '.join(sorted(names[group])[:20]) or '(none)'}")
        if kind in ("job", "busy", "blocked") or (
                kind == "state" and isinstance(signal.get("states"), dict)
                and _UNITS_STATES & set(signal["states"].values())):
            held = ("blocked",) if kind == "blocked" else ("busy",) if kind in ("job", "busy") \
                else ("busy", "blocked")
            for what in held:
                drivers.setdefault(f"{what} units of resource '{named[0]}'", set()).add(kind)
        if kind not in ("job", "at", "seen"):
            # Two keys for one count, state or level would undo each other's events at every
            # change; several jobs, and several ways to see an item, are what a plant has.
            what = f"\"{kind}\" of {group} '{named[0]}'"
            told[what] = told.get(what, 0) + 1
        if group == "block" and kind != "counter":     # a counter tells what left, not what is there
            for name in named:
                if isinstance(name, str):
                    members.setdefault(f"content of block '{name}'", set()).add(
                        "queue" if kind == "queue" else "identities")
    for what, kinds in sorted({**drivers, **members}.items()):
        if len(kinds) > 1:
            bad(f"the {what[:120]} are told twice, by {' and '.join(sorted(kinds))}", "/signals",
                "E_PLANTMAP_DOUBLE", "keep one: the engine does not choose between two reports")
    for what, signals_ in sorted(told.items()):
        if signals_ > 1:
            bad(f"the {what[:120]} is told by {signals_} signals", "/signals",
                "E_PLANTMAP_DOUBLE", "keep one: the engine does not choose between two reports")
    return found


def schema() -> dict:
    """The JSON Schema of a `*.plantmap.json`, for the reference. `check` is the judge: it
    also knows which fields go with which kind, and the model."""
    text = {"type": "string", "minLength": 1}
    table = {"type": "object", "additionalProperties": {"type": "string"}}
    values = {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Naga plant mapping",
        "type": "object", "additionalProperties": False, "required": ["format", "time"],
        "properties": {
            "format": {"const": FORMAT},
            "description": {"type": "string"},
            "time": {
                "type": "object", "additionalProperties": False, "required": ["epoch"],
                "properties": {
                    "epoch": {"type": "string", "description": "ISO 8601 instant that model "
                              "time 0 stands for; with no offset it is UTC."},
                    "unit": {"enum": list(UNITS), "description": "The model's time unit "
                             "(default: the model's own)."},
                    "stamp": {"enum": ["source", "received"], "description": "Which stamp of "
                              "an observation is its time (default: the source's)."}}},
            "signals": {
                "type": "array", "maxItems": LIMITS["signals"],
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["key", "as"],
                    "properties": {
                        "source": {**text, "description": "Only observations of this data source."},
                        "key": {**text, "description": "The observation's key. An \"at\" key "
                                "holds one {id}; a \"job\" key may hold one *."},
                        "field": {**text, "description": "The cell to read when the value is a row."},
                        "as": {"enum": list(KINDS), "description": "What the key asserts."},
                        "resource": {**text, "description": "A resource or a fleet of the model."},
                        "block": {**text, "description": "A block of the flattened model."},
                        "level": {**text, "description": "A level of the model."},
                        "states": {**table, "description": "Plant value -> "
                                   + ", ".join(STATES) + "."},
                        "blocks": {**table, "description": "Plant value -> block."},
                        "gone": {**values, "description": "Values that end the entity (a null always does)."},
                        "merged": {**values, "description": "Values that take it into a batch."},
                        "type": {**text, "description": "The entity type of what this signal names."},
                        "type_field": {**text, "description": "The cell of a row that holds the product."},
                        "types": {**table, "description": "Product -> entity type."},
                        "leaves": {"type": "boolean", "description": "\"seen\": the entity has left."},
                        "scale": {"type": "number", "description": "\"level\": model value = value * scale."},
                        "rollover": {"type": "integer", "minimum": 2, "description": "\"counter\": "
                                     "the value the plant's counter returns to 0 at."}}}}}}


def scaffold(layout, anonymous: bool = False, counters: bool = False) -> dict:
    """The mapping that names every block, resource and level of the model *layout* by its
    own name: a file to start from, and the mapping the exit check inverts (D257).
    *anonymous*: queue lengths and unit counts instead of identities and jobs. *counters*:
    also a running total of what left each block (D288)."""
    names = _names(layout)
    signals = []
    for name in names["resource"]:
        signals += [
            {"key": f"resource/{name}/down", "as": "state", "resource": name,
             "states": {"true": "down", "false": "up"}},
            {"key": f"resource/{name}/capacity", "as": "capacity", "resource": name},
            {"key": f"resource/{name}/blocked", "as": "blocked", "resource": name},
            {"key": f"resource/{name}/busy", "as": "busy", "resource": name} if anonymous
            else {"key": f"resource/{name}/job/*", "as": "job", "resource": name}]
    if anonymous:
        signals += [{"key": f"block/{name}/count", "as": "queue", "block": name}
                    for name in names["block"]]
    elif names["block"]:
        signals.append({"key": "entity/{id}", "field": "at", "as": "at",
                        "blocks": {name: name for name in names["block"]}, "merged": ["+"],
                        "type_field": "type", "types": {name: name for name in names["types"]}})
    signals += [{"key": f"level/{name}", "as": "level", "level": name} for name in names["level"]]
    if counters:
        signals += [{"key": f"block/{name}/out", "as": "counter", "block": name}
                    for name in names["block"]]
    return {"format": FORMAT, "time": {"epoch": "1970-01-01T00:00:00+00:00", "unit": names["unit"]},
            "signals": signals}


# ---------------------------------------------------------------- observations into events
_WHOLE = 2 ** 53        # the largest count a float holds exactly
_READING = ("value", "total", "since", "resets", "at")      # of a counter (`Mapper.readings`)


def _number(value) -> bool:
    """Finite. A JSON integer may be too large for a float: it is then no number (#373)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and abs(value) <= sys.float_info.max


def _count(value) -> int | None:
    """A whole number of items or units within the bound, else None."""
    if _number(value) and float(value).is_integer() and 0 <= value <= LIMITS["count"]:
        return int(value)
    return None


def _fits(value) -> bool:
    return not isinstance(value, str) or len(value.encode("utf-8", "replace")) <= MAX_TEXT


_LAST_EPOCH = 7258118400.0          # 2200-01-01T00:00:00Z


def _instant(value) -> float | None:
    """Seconds since 1970 of a stamp as the feed reads one (`live.stamp`): a number, or ISO
    8601 text of a sane length. None for anything else, a date no clock can name included."""
    if isinstance(value, str) and len(value) > 64:
        return None
    try:
        seconds = stamp(value)
    except (OverflowError, OSError, ValueError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) else None  # a float


def _event(t, kind, entity=-1, e_type="", block="", target=None, **more) -> TraceEvent:
    return TraceEvent(t=t, kind=kind, entity_id=entity, e_type=e_type, block=block, target=target,
                      **more)


class _Entity:
    """A named entity: where it is, whether it is there or inside a batch, since when."""
    __slots__ = ("id", "block", "present", "e_type", "since", "order")

    def __init__(self, ident, block, e_type, since, order):
        self.id, self.block, self.present, self.e_type = ident, block, True, e_type
        self.since, self.order = since, order


# code -> the reasons it reports, with how each reads
_REPORTED = {
    "W_PLANT_DROPPED": {
        "value": "a value its signal cannot read", "field": "a row without the signal's field",
        "type": "a key or a value that is not data", "oversize": "a text over 1024 bytes",
        "stale": "older than what its key already said", "time_range": "a time before the epoch "
        "or past the bound", "no_time": "no usable time", "keys": "over the bound of keys",
        "entities": "over the bound of entities", "ceiling": "after the event ceiling",
        "forgotten": "the key longest silent was forgotten to make room (an old value of it "
        "would now read as new)"},
    "W_PLANT_TIME": {
        "no_stamp": "no source stamp, the receipt time was used", "ahead": "a source stamp "
        "ahead of its receipt, the receipt time was used", "late": "late, applied at the "
        "timeline's last instant"},
    "W_PLANT_SYNTHETIC": {"synthetic": "events for items and units the plant did not name"},
    "W_PLANT_CONTRADICTION": {
        "job_unseen": "a job for an entity seen nowhere: the resource is busy, nobody is in "
        "service", "retyped": "another type for a known entity: the first stands",
        "job_elsewhere": "a job for an entity in a block that does not seize that resource: "
        "shown as said, in service where it is", "over_capacity": "work started on a resource "
        "with no unit free: shown as said", "level_range": "a level outside its "
        "declared bounds: shown as said", "counter_back": "a counter that went backwards "
        "with no rollover declared: nothing was added and it counts on from the new value "
        "(its total is a lower bound)"},
}
_HINTS = {
    "W_PLANT_DROPPED": "check the mapping's tables against what the plant sends",
    "W_PLANT_TIME": "synchronise the clocks; a recorded log is sorted before it is mapped",
    "W_PLANT_SYNTHETIC": "map identities (\"at\", \"seen\", \"job\") where the plant has them",
    "W_PLANT_CONTRADICTION": "the plant says two things that do not agree",
}


class Mapper:
    """Observations in, `TraceEvent`s out, in time order (D253). One per timeline.

    *max_events*: stop after that many events (`truncated`), for a recording; a live
    timeline keeps no log and needs none."""

    def __init__(self, mapping, layout=None, *, max_events: int | None = None):
        refused = check(mapping, layout)
        if refused:
            raise PlantError(refused)
        time = mapping["time"]
        self._epoch = stamp(time["epoch"])
        unit = time.get("unit") or (_names(layout)["unit"] if layout is not None else "minutes")
        self._unit = float(UNITS[unit])
        self._received = time.get("stamp") == "received"
        # With a model: which blocks seize each resource (a set's members with it, a fleet
        # by its TRANSPORT), and each level's declared bounds. Without one, neither is judged.
        self._seizes: dict[str, set] = {}
        self._bounds: dict[str, tuple] = {}
        if layout is not None:
            flat = _flat(layout)
            sets = {str(r.get("name")): r.get("members") or [] for r in flat.get("resource_sets") or []}
            for block in flat.get("blocks") or []:
                for key in ("resource_name", "fleet"):
                    target = (block.get("params") or {}).get(key)
                    for name in [target, *sets.get(target, ())] if target else ():
                        self._seizes.setdefault(name, set()).add(block.get("name"))
            for level in flat.get("levels") or []:
                self._bounds[level.get("name")] = tuple(
                    level.get(key) if _number(level.get(key)) else None for key in ("min", "capacity"))
        self._exact: dict[str, list] = {}
        self._patterns: list = []
        for signal in mapping.get("signals", []):
            key = signal["key"]
            if "{id}" in key or "*" in key:
                # Literal text around one capture: no pattern of the file's is ever compiled.
                text = re.escape(key).replace(re.escape("{id}"), "(.+)").replace(re.escape("*"), ".*")
                self._patterns.append((re.compile(text, re.DOTALL), signal))
            else:
                self._exact.setdefault(key, []).append(signal)
        self._max = max_events
        self.now = 0.0
        self.truncated = False
        self.ids: dict[str, int] = {}               # plant id -> the entity id the events carry
        self._entities: dict[int, _Entity] = {}
        self._anonymous: dict[str, list] = {}       # block -> ids, oldest first
        self._anonymous_type: dict[str, str] = {}
        self._alive = 0                             # anonymous entities
        self._next, self._next_anonymous, self._order = 1, -2, 0
        self._said: dict[tuple, tuple] = {}         # (source, key) -> (its last time, value)
        self._left: dict[tuple, float] = {}         # (source, key) of an item that left -> when
        self._jobs: dict[tuple, tuple] = {}         # (source, key) -> (resource, entity id)
        self._working: dict[str, int] = {}          # resource -> the jobs it holds
        self._merged: dict[int, str] = {}           # entities inside a batch, oldest first
        self._units = {"busy": {}, "blocked": {}}   # resource -> units nobody names
        self._down: dict[str, bool] = {}
        self._capacity: dict[str, float] = {}
        self._levels: dict[str, tuple] = {}         # level -> (time, value) last said
        # block -> [last value, total, since, resets, when it last spoke]
        self._counters: dict[str, list] = {}
        self._carried: dict[str, list] = {}         # the same, of a mapper before this one
        self._carry_age = 0.0
        self._counted = {s["block"] for s in mapping.get("signals", []) if s.get("as") == "counter"}
        self._counts: dict[str, int] = {}
        self._holes: list = []                      # the first 1000, for a report
        self._hole_totals = [0, 0, 0]               # holes, events lost, breaks: exact
        self._emitted = self._observed = 0

    # ------------------------------------------------------------ in
    def feed(self, observations) -> list:
        """The events these observations make, in time order: ready for `Timeline.append`.
        An observation is a `live.Event` or its dict; a `{"lost": n}` or `{"gap": true}`
        dict is a hole of the log."""
        out: list = []
        for observation in observations:
            if isinstance(observation, dict) and "key" not in observation:
                self.hole(observation.get("lost", 0), bool(observation.get("gap")))
                continue
            self._observed += 1
            self._one(*_fields(observation), out)
        self._emitted += len(out)
        return out

    def hole(self, lost=0, gap: bool = False) -> None:
        """The feed dropped *lost* events, or its stream broke (*gap*): recorded with the
        time it happened at. The next assertions put the timeline right again; an entity
        whose leaving was missed stays until it is seen again."""
        lost = int(lost) if _number(lost) and lost > 0 else 0
        if lost or gap:
            for index, more in enumerate((1, lost, bool(gap))):
                self._hole_totals[index] += more
            if len(self._holes) < 1000:
                self._holes.append((self.now, lost, bool(gap)))

    def _drop(self, reason: str) -> None:
        self._counts[reason] = self._counts.get(reason, 0) + 1

    def _full(self, out) -> bool:
        """Whether the event ceiling is reached, here and now: a single queue length may
        ask for thousands of events."""
        if self._max is None or self._emitted + len(out) < self._max:
            return False
        self.truncated = True
        self._drop("ceiling")
        return True

    def _over(self, resource) -> None:
        """A unit started work on a resource with none free: shown as said, counted. Asked
        only when work starts: a capacity that falls under the units still busy is a shift
        ending while jobs finish, which a correct plant and a correct run both do."""
        capacity = self._capacity.get(resource)
        if capacity is not None and capacity < self._units["busy"].get(resource, 0) \
                + self._working.get(resource, 0):
            self._drop("over_capacity")

    def at(self, seconds: float) -> float:
        """The model time of an instant given in seconds since 1970."""
        return (seconds - self._epoch) / self._unit

    def seconds(self, source_time, received) -> tuple:
        """(the instant an observation stands for, in seconds since 1970, or None; why it
        is not the source's own stamp, or None). D255."""
        source_time, received = _instant(source_time), _instant(received)
        if self._received:
            return received, None
        if source_time is None:
            return received, "no_stamp"
        if received is not None and source_time > received + SKEW_SECONDS:
            return received, "ahead"
        return source_time, None

    def _one(self, source, key, value, source_time, received, out) -> None:
        if self._full(out):
            return None
        row = isinstance(value, dict)
        if not isinstance(key, str) or not key or not (
                isinstance(value, _SCALARS) if not row else all(
                    isinstance(k, str) and isinstance(v, _SCALARS) for k, v in value.items())):
            return self._drop("type")
        if not _fits(key) or not all(_fits(v) for v in (value.values() if row else (value,))):
            return self._drop("oversize")
        signals = [(s, None) for s in self._exact.get(key, ())]
        for pattern, signal in self._patterns:
            match = pattern.fullmatch(key)
            if match:
                signals.append((signal, match.group(1) if match.groups() else None))
        signals = [(s, ident) for s, ident in signals if s.get("source", source) == source]
        if not signals:
            return self._drop("unread")
        seconds, why = self.seconds(source_time, received)
        if why:
            self._drop(why)
        if seconds is None:
            return self._drop("no_time")
        t = (seconds - self._epoch) / self._unit
        if not 0 <= t <= LIMITS["seconds"] / self._unit:
            return self._drop("time_range")
        said = self._said.pop((source, key), None)
        if said is None and t <= self._left.get((source, key), -math.inf):
            # What an item said before it left, delivered again (#372): not a new item.
            return self._drop("stale" if t < self._left[(source, key)] else "duplicate")
        if said is not None and (t < said[0] or said == (t, value)):
            # Older than what the key already said, or the same thing again (at least once).
            self._said[(source, key)] = said
            return self._drop("stale" if t < said[0] else "duplicate")
        if said is None and len(self._said) >= LIMITS["keys"]:
            # A watch runs for weeks: the key longest silent makes room, counted. What it
            # holds in the timeline stays; only "is this older than what it said" is lost.
            del self._said[next(iter(self._said))]
            self._drop("forgotten")
        self._said[(source, key)] = (t, value)      # last said, last in the table
        if t < self.now:
            self._drop("late")
            t = self.now
        self.now = t
        for signal, ident in signals:
            if value is None or not ("field" in signal or row):
                cell = value
            elif row and signal.get("field") in value:
                cell = value[signal["field"]]
            else:
                self._drop("field")
                continue
            getattr(self, "_as_" + signal["as"])(signal, (source, key), ident, cell, value, t, out)

    # ------------------------------------------------------------ resources
    def _hold(self, out, t, resource, which, units) -> None:
        """*units* of *resource* are busy or blocked with nobody named (D254)."""
        held = self._units[which].get(resource, 0)
        step = 1 if units > held else -1
        while held != units and not self._full(out):
            if which == "busy":
                out.append(_event(t, "entity_seized" if step > 0 else "entity_released",
                                  target=resource))
            else:
                out.append(_event(t, "resource_state", resource=resource, state="blocked",
                                  value=1.0 if step > 0 else 0.0))
            held += step
            self._drop("synthetic")
        self._units[which][resource] = held         # what was told, also when cut short
        if which == "busy" and step > 0:
            self._over(resource)

    def _as_state(self, signal, _key, _ident, cell, _row, t, out) -> None:
        state, resource = signal["states"].get(_text(cell)), signal["resource"]
        if state is None:
            return self._drop("value")
        if (state == "down") != self._down.get(resource, False):
            self._down[resource] = state == "down"
            out.append(_event(t, "resource_state", resource=resource, state=state if
                              state == "down" else "up", value=1.0 if state == "down" else 0.0))
        if state == "busy":
            self._hold(out, t, resource, "busy", max(1, self._units["busy"].get(resource, 0)))
        if state == "blocked":
            self._hold(out, t, resource, "blocked", max(1, self._units["blocked"].get(resource, 0)))
        if state == "idle":
            self._hold(out, t, resource, "busy", 0)
        if state in ("busy", "idle"):
            self._hold(out, t, resource, "blocked", 0)

    def _as_capacity(self, signal, _key, _ident, cell, _row, t, out) -> None:
        if not _number(cell) or not 0 <= cell <= LIMITS["count"]:
            return self._drop("value")
        if self._capacity.get(signal["resource"]) != cell:
            self._capacity[signal["resource"]] = cell
            out.append(_event(t, "resource_state", resource=signal["resource"], state="capacity",
                              value=float(cell)))

    def _as_busy(self, signal, _key, _ident, cell, _row, t, out) -> None:
        units = _count(cell)
        if units is None:
            return self._drop("value")
        self._hold(out, t, signal["resource"], signal["as"], units)

    _as_blocked = _as_busy

    def _as_job(self, signal, key, _ident, cell, _row, t, out) -> None:
        resource, held = signal["resource"], self._jobs.get(key)
        if cell is None or cell == "":
            entity = None
        elif isinstance(cell, bool) or isinstance(cell, float) and not cell.is_integer():
            return self._drop("value")
        else:
            entity = self.ids.get(_text(cell), -1)
            if entity == -1 and (held is None or held[1] != -1):
                self._drop("job_unseen")
        if (held[1] if held else None) == entity:
            return None                 # the same job, or an idle position saying so again
        if held is None and len(self._jobs) >= LIMITS["keys"]:
            return self._drop("keys")   # a held job is state: none is evicted for a new one
        for kind, who in (("entity_released", held[1] if held else None), ("entity_seized", entity)):
            if who is not None:
                known = self._entities.get(who)
                out.append(_event(t, kind, who, known.e_type if known else "",
                                  known.block if known and known.present else "", resource))
        if held is not None:
            self._working[resource] -= 1
            del self._jobs[key]
        if entity is not None:
            self._jobs[key] = (resource, entity)
            self._working[resource] = self._working.get(resource, 0) + 1
            known = self._entities.get(entity)
            if known is not None and known.present and resource in self._seizes \
                    and known.block not in self._seizes[resource]:
                self._drop("job_elsewhere")
            self._over(resource)

    # ------------------------------------------------------------ entities
    def _as_queue(self, signal, _key, _ident, cell, _row, t, out) -> None:
        length, block = _count(cell), signal["block"]
        if length is None:
            return self._drop("value")
        members = self._anonymous.setdefault(block, [])
        e_type = self._anonymous_type[block] = signal.get("type", UNTYPED)
        while len(members) > length and not self._full(out):
            out.append(_event(t, "entity_disposed", members.pop(0), e_type, block))
            self._alive -= 1
            self._drop("synthetic")
        while len(members) < length and not self._full(out):
            if len(self.ids) + self._alive >= LIMITS["entities"]:
                return self._drop("entities")
            members.append(self._next_anonymous)
            out.append(_event(t, "entity_created", self._next_anonymous, e_type, block))
            self._next_anonymous -= 1
            self._alive += 1
            self._drop("synthetic")

    def _as_at(self, signal, key, ident, cell, row, t, out) -> None:
        code = _text(cell)
        if cell is None or code in map(_text, signal.get("gone", ())):
            # Its key is forgotten with it: a plant makes new ids for ever, and the table of
            # keys must not fill with the dead. The newest of the dead keep the time they
            # left at, so that an old value delivered again is still old (#372).
            said = self._said.pop(key, None)
            if said is not None:
                self._left.pop(key, None)
                self._left[key] = said[0]
                if len(self._left) > LIMITS["left"]:
                    del self._left[next(iter(self._left))]
            return self._gone(ident, t, out)
        entity = self._entities.get(self.ids.get(ident, 0))
        if code in map(_text, signal.get("merged", ())):
            if entity is not None and entity.present:
                entity.present = False
                self._merged[entity.id] = ident
                out.append(_event(t, "entity_absorbed", entity.id, entity.e_type, entity.block))
            return None
        block = signal["blocks"].get(code)
        if block is None:
            return self._drop("value")
        product = row.get(signal.get("type_field")) if isinstance(row, dict) else None
        e_type = signal.get("types", {}).get(_text(product)) if product is not None else None
        self._place(ident, block, e_type or signal.get("type"), t, out)

    def _as_seen(self, signal, _key, _ident, cell, _row, t, out) -> None:
        if cell is None or cell == "":
            return None                 # nobody at the reader: says nothing of who was
        if isinstance(cell, bool) or isinstance(cell, float) and not cell.is_integer():
            return self._drop("value")
        if signal.get("leaves"):
            return self._gone(_text(cell), t, out)
        self._place(_text(cell), signal["block"], signal.get("type"), t, out)

    def _place(self, ident, block, e_type, t, out) -> None:
        """Entity *ident* is in *block*: created there when it is new, moved when it was
        elsewhere, nothing when it is known to be there."""
        entity = self._entities.get(self.ids.get(ident, 0))
        self._order += 1
        if entity is None:
            if len(self.ids) + self._alive >= LIMITS["entities"] and self._merged:
                # A part taken into a batch and never heard of again makes room, oldest first.
                self._entities.pop(self.ids.pop(self._merged.pop(next(iter(self._merged))), 0), None)
            if len(self.ids) + self._alive >= LIMITS["entities"]:
                return self._drop("entities")
            entity = _Entity(self._next, block, e_type or UNTYPED, t, self._order)
            self.ids[ident] = self._next
            self._entities[self._next] = entity
            self._next += 1
            out.append(_event(t, "entity_created", entity.id, entity.e_type, block))
            return None
        if e_type and e_type != entity.e_type:
            self._drop("retyped")
        if entity.present and entity.block == block:
            return None
        # Back out of a batch it comes from no block: a move from the block it is in would
        # read as one inside an instance, which the timeline does not show.
        out.append(_event(t, "entity_moved", entity.id, entity.e_type,
                          entity.block if entity.present else "", block))
        entity.block, entity.present, entity.since, entity.order = block, True, t, self._order
        self._merged.pop(entity.id, None)

    def _gone(self, ident, t, out) -> None:
        entity = self._entities.pop(self.ids.pop(ident, 0), None)
        if entity is not None:
            self._merged.pop(entity.id, None)
        if entity is not None and entity.present:
            out.append(_event(t, "entity_disposed", entity.id, entity.e_type, entity.block))

    # ------------------------------------------------------------ levels
    def _as_level(self, signal, _key, _ident, cell, _row, t, out) -> None:
        if not _number(cell):
            return self._drop("value")
        said = (t, cell * signal.get("scale", 1.0))
        if self._levels.get(signal["level"]) != said:
            self._levels[signal["level"]] = said
            low, high = self._bounds.get(signal["level"], (None, None))
            if (low is not None and said[1] < low) or (high is not None and said[1] > high):
                self._drop("level_range")
            out.append(_event(t, "level_changed", resource=signal["level"], value=float(said[1])))

    # ------------------------------------------------------------ counters
    def _as_counter(self, signal, _key, _ident, cell, _row, t, _out) -> None:
        """The plant's running total of what left a block (D288, D289). The first value is
        a baseline; an increase adds; a decrease wraps with `rollover` and is otherwise a
        restart that adds nothing. No event: a count is not an item."""
        if not _number(cell) or not 0 <= cell <= _WHOLE or not float(cell).is_integer():
            return self._drop("value")      # no count, or one no float holds exactly (#373)
        held = self._counters.get(signal["block"])
        if held is None:
            # The count of a mapper before this one goes on (`carry`) when this is its next
            # reading: not an earlier one (this mapper is told the past itself and counts
            # it) and not one later than the stated age. Judged once, at the first value.
            before = self._carried.pop(signal["block"], None)
            held = self._counters[signal["block"]] = before if before is not None                 and before[4] <= t <= before[4] + self._carry_age else [cell, 0, t, 0, t]
        step, roll = cell - held[0], signal.get("rollover")
        if step < 0 and roll and max(cell, held[0]) < roll:
            step += roll
        elif step < 0:
            step, held[3] = 0, held[3] + 1
            self._drop("counter_back")
        held[0], held[1], held[4] = cell, held[1] + int(step), t

    def carry(self, readings, max_age: float) -> None:
        """Go on from the counts of a mapper before this one (a twin started again, D378).
        *readings* is what its `readings()` gave and is read back from a file: an entry
        that is not a sound reading of a counted block is no reading. A count goes on only
        if its counter next speaks within *max_age* (model time) of what is carried; a
        value under the carried one is then a reset, or a declared rollover, as within
        one mapper. A reset that has counted past the carried value again cannot be told
        from an increase, as between any two readings."""
        self._carry_age = float(max_age) if _number(max_age) and max_age > 0 else 0.0
        self._carried = {}
        for block in self._counted.intersection(readings if isinstance(readings, dict) else ()):
            said = readings[block]
            row = [said.get(key) for key in _READING] if isinstance(said, dict) else [None]
            if all(_number(v) for v in row) and row[2] <= row[4] and all(
                    0 <= row[i] <= _WHOLE and float(row[i]).is_integer() for i in (0, 1, 3)):
                self._carried[block] = [int(row[0]), int(row[1]), float(row[2]), int(row[3]),
                                        float(row[4])]

    def readings(self) -> dict:
        """block -> the last raw reading of its counter and the count it stands in:
        `value`, `total`, `since`, `resets` and `at`, the model time the counter last
        spoke at. What `carry` takes. A carried count whose counter has not spoken yet is
        still given, so a quiet counter is not lost by one mapper that never heard it."""
        return {block: dict(zip(_READING, held))
                for block, held in sorted({**self._carried, **self._counters}.items())}

    def counters(self) -> dict:
        """block -> what its counter has counted since this mapper first saw it: `total`,
        `since` (model time) and `resets` (times it went back and the total lost count)."""
        return {block: {"total": held[1], "since": held[2], "resets": held[3]}
                for block, held in sorted(self._counters.items())}

    # ------------------------------------------------------------ out
    def snapshot(self) -> dict:
        """What the plant looks like now, as plain data: what this mapper has told the
        timeline, under the FLATTENED model's names (a timeline viewed from the top shows an
        asset, not the block inside it). `state.from_plant` makes a state document of it."""
        units = self._units["busy"]
        busy = {name: units.get(name, 0) + self._working.get(name, 0)
                for name in {*units, *self._working}}
        holds: dict[int, list] = {}
        for resource, entity in self._jobs.values():
            holds.setdefault(entity, []).append(resource)
        present = sorted((e for e in self._entities.values() if e.present), key=lambda e: e.order)
        def typed(e_type):
            return None if e_type in (UNTYPED, "", None) else e_type
        entities = [{"block": e.block, "e_type": typed(e.e_type),
                     "since": e.since, "holds": holds.get(e.id, [])} for e in present]
        entities += [{"block": block, "e_type": typed(self._anonymous_type.get(block)),
                      "since": None, "holds": []}
                     for block, members in self._anonymous.items() for _ in members]
        return {"at": self.now, "stamp": self.stamp(), "entities": entities,
                # `units`: the part of `busy` nobody is named for (a count, not a job).
                "resources": {name: {"busy": busy.get(name, 0), "units": units.get(name, 0),
                                     "down": self._down.get(name, False)}
                              for name in sorted({*busy, *self._down, *self._capacity})},
                "levels": {name: said[1] for name, said in self._levels.items()},
                "counters": self.counters()}

    def stamp(self) -> str | None:
        """The instant `now` stands for, ISO 8601 in UTC; None when no clock can name it
        (`check` bounds the epoch and `feed` the time, so only a caller's own `now` can)."""
        try:
            return datetime.fromtimestamp(self._epoch + self.now * self._unit,
                                          timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None

    def report(self) -> dict:
        """Plain counts: what came in, what went out, what was dropped or repaired and why."""
        return {"observations": self._observed, "events": self._emitted, "now": self.now,
                "truncated": self.truncated, "entities": len(self.ids) + self._alive,
                "counts": dict(self._counts), "holes": list(self._holes)}

    def problems(self) -> list[Problem]:
        """One Problem per code that happened, with the count of each reason."""
        found = []
        for code, reasons in _REPORTED.items():
            said = [f"{self._counts[reason]} {text}" for reason, text in reasons.items()
                    if self._counts.get(reason)]
            if said:
                found.append(problem(code, "plant: " + "; ".join(said), "/signals",
                                     hint=_HINTS[code]))
        if self._holes:
            holes, lost, breaks = self._hole_totals
            found.append(problem(
                "W_PLANT_GAP", f"plant: the log has {holes} holes ({lost} events "
                f"lost, {breaks} breaks), the first at t = "
                f"{self._holes[0][0]:g}", "/signals",
                hint="what was missed is not shown; the next values put the timeline right"))
        return found


def _fields(observation) -> tuple:
    """(source, key, value, source time, receipt time) of a `live.Event` or its dict."""
    if isinstance(observation, dict):
        get = observation.get
    else:
        def get(name):
            return getattr(observation, name, None)
    return get("source"), get("key"), get("value"), get("source_time"), get("received")


# ---------------------------------------------------------------- recorded logs
def read_log(path) -> tuple:
    """(the observations and holes of the recorded log *path*, in file order; the problems
    of reading it). The file `naga watch --record` writes: one JSON object a line. Bounded
    as it is read; a line that is no object, or too long, is skipped and counted."""
    observations, skipped = [], 0
    try:
        if Path(path).stat().st_size > LIMITS["log_bytes"]:
            raise ValueError("size")
        with open(path, "rb") as handle:
            while line := handle.readline(LIMITS["line_bytes"] + 1):
                if len(line) > LIMITS["line_bytes"]:
                    while line and not line.endswith(b"\n"):    # the rest of that line
                        line = handle.readline(LIMITS["line_bytes"])
                    skipped += 1
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):
                    record = None
                if not isinstance(record, dict):
                    skipped += bool(line.strip())
                elif len(observations) >= LIMITS["lines"]:
                    raise ValueError("lines")
                else:
                    observations.append(record)
    except (OSError, ValueError) as exc:
        return [], [problem("E_PLANT_LOG", f"the log cannot be read ({type(exc).__name__}), or is "
                            f"over {LIMITS['log_bytes']} bytes or {LIMITS['lines']} lines", "/",
                            hint="record one with `naga watch --record`, or cut it")]
    found = [problem("W_PLANT_DROPPED", f"plant: {skipped} lines of the log are not an "
                     f"observation, or are over {LIMITS['line_bytes']} bytes", "/",
                     hint="one JSON object a line, as `naga watch --record` writes")] if skipped else []
    return observations, found


def write_log(path, observations) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for observation in observations:
            handle.write(json.dumps(observation) + "\n")


def in_order(mapper: "Mapper", observations) -> list:
    """*observations* by the instant each stands for (D255), ties and what has no time in
    the order given: what a recording is sorted by, and one step of several live sources."""
    keyed, last = [], (-math.inf, 0)
    for index, observation in enumerate(observations):
        if not (isinstance(observation, dict) and "key" not in observation):
            seconds = mapper.seconds(*_fields(observation)[3:])[0]
            seq = observation.get("seq") if isinstance(observation, dict) \
                else getattr(observation, "seq", None)
            # Within one instant the feed's own count is the order things happened in.
            last = (last[0] if seconds is None else seconds, seq if _number(seq) else last[1])
        keyed.append((*last, index))
    return [observations[key[2]] for key in sorted(keyed)]


def replay(mapping, layout, observations, *, max_events: int | None = None) -> tuple:
    """(the trace events of a recorded log, the mapper that made them). *max_events*
    defaults to the recorder's own cap; `mapper.truncated` says it was reached."""
    mapper = Mapper(mapping, layout,
                    max_events=LIMITS["events"] if max_events is None else max_events)
    return mapper.feed(in_order(mapper, list(observations))), mapper


# ---------------------------------------------------------------- live
class Watch:
    """A live feed as an observed timeline, one pull at a time (rule 12): `step()` asks the
    feed what changed since and returns the events for a `Timeline(live=True)`, or for the
    editor's `TracePlayer.follow(events, watch.now)`. The feed, with its connections, is the
    caller's; *sources* defaults to every live source of the model."""

    def __init__(self, mapping, layout, feed, sources=None):
        self.mapper = Mapper(mapping, layout)
        self._feed = feed
        if sources is None:
            sources = [s.get("name") for s in _flat(layout).get("data_sources") or []
                       if s.get("kind") in readers.KINDS]
        self._cursors: dict = dict.fromkeys(sources)

    @property
    def now(self) -> float:
        return self.mapper.now

    def step(self) -> list:
        batch: list = []
        for name, cursor in self._cursors.items():
            got = self._feed.changes(name, cursor)
            self._cursors[name] = got.cursor
            self.mapper.hole(got.lost, got.gap)
            batch += got.events
        # Two sources' events of one pull, by their own time: only what is late is clamped.
        return self.mapper.feed(in_order(self.mapper, batch))


# ---------------------------------------------------------------- the inverse (D257)
_VALUES = {"true": True, "false": False, "null": None}


def log_of(events, mapping, source: str = "plant") -> list[dict]:
    """The plant log a recorded run would have left, through *mapping* read backwards: one
    observation, in the feed's event shape, for what each trace event changes. Mapped back
    (`replay`) it gives the run's timeline, but for what a mapping cannot say (the spec's
    section 3). It writes through `at`, `queue`, `job`, `busy`, `blocked`, `capacity`,
    `state`, `level` and `counter`; an event nothing of the mapping speaks of leaves no line."""
    refused = check(mapping)
    if refused:
        raise PlantError(refused)
    epoch = stamp(mapping["time"]["epoch"])
    unit = float(UNITS[mapping["time"].get("unit", "minutes")])
    by: dict[tuple, object] = {}
    for signal in mapping.get("signals", []):
        kind = signal["as"]
        if kind == "at":
            for code, block in signal["blocks"].items():
                by.setdefault(("at", block), (signal, code))
        elif kind == "state":
            for code, state in signal["states"].items():
                by.setdefault((f"state:{state}", signal["resource"]), (signal, code))
        elif kind != "seen":
            by.setdefault((kind, signal[KINDS[kind][0]]), signal)
    lines: list = []
    where: dict[int, str] = {}                      # entity -> the block it is in
    inside: dict[str, int] = {}                     # block -> items, for a queue signal
    slots: dict[str, dict[int, int]] = {}           # resource -> position -> entity
    units = {"busy": {}, "blocked": {}}

    def say(t, signal, key, cell, **row) -> None:
        at = epoch + t * unit
        lines.append({
            "source": signal.get("source", source), "key": key,
            "value": {signal["field"]: cell, **row} if "field" in signal else cell,
            "source_time": at, "received": at})

    def leave(t, entity, ending) -> None:
        block = where.pop(entity, None)
        if ("queue", block) in by:
            inside[block] = max(inside.get(block, 0) - 1, 0)
            say(t, by["queue", block], by["queue", block]["key"], inside[block])
        elif ("at", block) in by and ending is not None:
            signal = by["at", block][0]
            codes = signal.get(ending) or signal.get("gone") or [None]
            say(t, signal, signal["key"].replace("{id}", str(entity)), codes[0])

    def enter(t, entity, e_type, block) -> None:
        old = where.get(entity)
        if ("at", block) not in by:
            # Out of what the identities cover: it is gone for them (and one more in a queue).
            leave(t, entity, "gone" if ("at", old) in by else None)
        elif ("queue", old) in by:
            leave(t, entity, None)
        where[entity] = block
        if ("queue", block) in by:
            inside[block] = inside.get(block, 0) + 1
            say(t, by["queue", block], by["queue", block]["key"], inside[block])
        elif ("at", block) in by:
            signal, code = by["at", block]
            product = next((text for text, name in signal.get("types", {}).items()
                            if name == e_type), None)
            row = {signal["type_field"]: product} if "type_field" in signal and product else {}
            say(t, signal, signal["key"].replace("{id}", str(entity)), code, **row)

    def hold(t, resource, which, change) -> None:
        held = units[which].get(resource, 0)
        units[which][resource] = now = max(held + change, 0)
        if (which, resource) in by:
            say(t, by[which, resource], by[which, resource]["key"], now)
        elif bool(held) != bool(now):       # a plant that says a state, not a count
            state = which if now else "idle" if which == "busy" else "busy"
            if (f"state:{state}", resource) in by:
                signal, code = by[f"state:{state}", resource]
                say(t, signal, signal["key"], _VALUES.get(code, code))

    # In the order a plant would see it. A move over a timed link shows when it ARRIVES (the
    # ride is not mappable now): before whatever else that instant holds, as the timeline
    # has it (#231), and not at all when the entity moved on first (#336).
    steps: list = []
    riding: dict[int, tuple] = {}                   # entity -> (its ride's step, its arrival)
    passed: dict[str, int] = {}                     # block -> what its counter shows
    for (kind, _block), signal in list(by.items()) if events else ():
        if kind == "counter":                       # a counter is there before the first part
            say(events[0].t, signal, signal["key"], 0)
    for n, e in enumerate(events):
        at = e.t
        if ("counter", e.block) in by and (e.kind == "entity_disposed" or (
                e.kind == "entity_moved" and e.target and e.target != e.block)):
            # When it leaves, a ride included: what `block/<name>/out` counts in a run.
            passed[e.block] = passed.get(e.block, 0) + 1
            say(e.t, by["counter", e.block], by["counter", e.block]["key"], passed[e.block])
        if e.kind in ("entity_moved", "entity_disposed", "entity_absorbed"):
            ride = riding.pop(e.entity_id, None)
            if ride is not None and e.t < ride[1]:
                steps[ride[0]] = None
            if e.kind == "entity_moved" and e.duration > 0 and e.target and e.target != e.block:
                at = e.t + e.duration
                riding[e.entity_id] = (len(steps), at)
        steps.append((at, at == e.t, n, e))
    for t, _on_time, _n, e in sorted(step for step in steps if step is not None):
        if e.kind == "entity_created":
            enter(e.t, e.entity_id, e.e_type, e.block)
        elif e.kind == "entity_moved" and e.target and e.target != e.block:
            enter(t, e.entity_id, e.e_type, e.target)
        elif e.kind in ("entity_disposed", "entity_absorbed"):
            leave(e.t, e.entity_id, "gone" if e.kind == "entity_disposed" else "merged")
        elif e.kind == "entity_seized" and ("job", e.target) in by:
            taken = slots.setdefault(e.target, {})
            slot = next(n for n in range(1, len(taken) + 2) if n not in taken)
            taken[slot] = e.entity_id
            say(e.t, by["job", e.target], by["job", e.target]["key"].replace("*", str(slot)),
                str(e.entity_id))
        elif e.kind == "entity_released" and ("job", e.target) in by:
            taken = slots.get(e.target, {})
            slot = next((n for n in sorted(taken) if taken[n] == e.entity_id), None)
            if slot is not None:
                del taken[slot]
                say(e.t, by["job", e.target], by["job", e.target]["key"].replace("*", str(slot)),
                    None)
        elif e.kind in ("entity_seized", "entity_released"):
            hold(e.t, e.target, "busy", 1 if e.kind == "entity_seized" else -1)
        elif e.kind == "resource_state" and e.state == "blocked":
            hold(e.t, e.resource, "blocked", 1 if e.value else -1)
        elif e.kind == "resource_state" and e.state == "capacity" and ("capacity", e.resource) in by:
            say(e.t, by["capacity", e.resource], by["capacity", e.resource]["key"], e.value)
        elif e.kind == "resource_state" and (f"state:{e.state}", e.resource) in by:
            signal, code = by[f"state:{e.state}", e.resource]
            say(e.t, signal, signal["key"], _VALUES.get(code, code))
        elif e.kind == "level_changed" and ("level", e.resource) in by:
            signal = by["level", e.resource]
            say(e.t, signal, signal["key"], e.value / signal.get("scale", 1.0))
    return [{**line, "seq": seq} for seq, line in enumerate(lines, 1)]


# ---------------------------------------------------------------- the CLI
def _say(problems) -> int:
    """Print each problem; 1 when one refuses, 0 for warnings only."""
    for found in problems:
        print(f"{found.code}: {found.message}")
    return 1 if any(found.severity == "error" for found in problems) else 0


def _picture(timeline, t) -> str:
    """One line of what the observed timeline shows at *t*: names of the model, numbers."""
    state = timeline.state_at(t)
    blocks = ", ".join(f"{name} {n}" for name, n in sorted(state["counts"].items()))
    resources = ", ".join(f"{name} {info['state']}" for name, info in sorted(state["resources"].items()))
    return f"t = {t:g}  blocks: {blocks or '-'}  resources: {resources or '-'}"


def _finish(mapper, layout, model_path, state_out) -> int:
    """The summary, the problems, and the state of now when one is asked for."""
    report = mapper.report()
    print(f"{report['observations']} observations, {report['events']} events, "
          f"{report['entities']} entities, now t = {report['now']:g} ({mapper.stamp()})"
          + (", stopped at the event ceiling" if report["truncated"] else ""))
    problems = mapper.problems()
    if state_out:
        from nagasim import state       # the run path: loaded only when a state is asked for

        doc, found = state.from_plant(layout, mapper.snapshot(), name=Path(model_path).stem)
        problems += found
        Path(state_out).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        print(f"state {doc['hash']} written to {state_out}")
    return _say(problems)


def main(argv=None) -> int:
    """`naga plant check | init | mock | replay | watch`."""
    import argparse
    import dataclasses
    import types

    from nagasim import api, netguard
    from nagasim._cli import setup_cli
    from nagasim.core.playback import Timeline
    from nagasim.sources import ReadError

    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga plant",
        description="A plant as a timeline: a plant mapping (*.plantmap.json) says what each key "
                    "a plant sends asserts about a model, and the views of a run then show the "
                    "plant. Read-only: nothing is sent to a plant.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    check_ = verbs.add_parser("check", help="say whether a mapping may be used with a model; "
                                           "exit 1 with the reasons when not")
    init_ = verbs.add_parser("init", help="write a starter mapping that names every block, "
                                         "resource and level of the model by its own name")
    init_.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    init_.add_argument("--anonymous", action="store_true",
                       help="queue lengths and unit counts instead of identities and jobs")
    init_.add_argument("--counters", action="store_true",
                       help="also a counter of what left each block (parts made, scrapped)")
    init_.add_argument("--out", default=None, metavar="FILE", help="default: print it")
    mock_ = verbs.add_parser("mock", help="run the model and write the plant log that run would "
                                         "have left, through the mapping read backwards")
    mock_.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    mock_.add_argument("--map", required=True, metavar="FILE", help="the plant mapping")
    mock_.add_argument("--out", required=True, metavar="FILE", help="the log, one JSON object a line")
    mock_.add_argument("--seed", type=int, default=0)
    replay_ = verbs.add_parser("replay", help="map a recorded log (naga watch --record) into an "
                                             "observed timeline")
    watch_ = verbs.add_parser("watch", help="show a model's live sources as an observed timeline, "
                                           "as they send")
    for verb in (check_, replay_, watch_):
        verb.add_argument("mapping", help="path to the plant mapping")
        verb.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    replay_.add_argument("log", help="path to the recorded log")
    replay_.add_argument("--player", default=None, metavar="FILE",
                         help="write the observed timeline as a self-contained HTML player")
    replay_.add_argument("--trace", default=None, metavar="FILE",
                         help="write the trace events, one JSON object a line")
    watch_.add_argument("--connections", required=True, metavar="FILE",
                        help="your connections file, as for naga watch")
    watch_.add_argument("--source", action="append", default=None, metavar="NAME",
                        help="only this source; may repeat (default: every live source)")
    watch_.add_argument("--for", dest="seconds", type=float, default=30.0, metavar="SECONDS",
                        help="how long to watch (default 30; 0: until Ctrl+C)")
    for verb in (replay_, watch_):
        verb.add_argument("--state", default=None, metavar="FILE",
                          help="write the state of the plant at the last instant, for "
                               "naga run MODEL --state FILE")
    args = parser.parse_args(argv)

    layout = api.load_layout(Path(args.model))
    if args.verb == "init":
        text = json.dumps(scaffold(layout, anonymous=args.anonymous, counters=args.counters),
                          indent=1)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            text = f"mapping written to {args.out}"
        print(text)
        return 0
    mapping, problems = load(args.map if args.verb == "mock" else args.mapping)
    problems = problems or [p for p in check(mapping, layout) if p.severity == "error"]
    if problems:
        return _say(problems)
    if args.verb == "check":
        print(f"the mapping reads {len(mapping.get('signals', []))} signals of this model")
        return 0
    mapping["time"].setdefault("unit", _names(layout)["unit"])
    if args.verb == "mock":
        from nagasim.core.trace import TraceRecorder

        recorder = TraceRecorder()
        api.run_model(layout, replications=1, seed=args.seed, trace=recorder, record_logs=False)
        lines = log_of(recorder.events, mapping)
        write_log(args.out, lines)
        print(f"{len(lines)} observations of a run with seed {args.seed} written to {args.out}")
        return 0
    if args.verb == "replay":
        observations, problems = read_log(args.log)
        if _say(problems):
            return 1
        events, mapper = replay(mapping, layout, observations)
        if events:
            print(_picture(Timeline(events), mapper.now))
        if args.trace:
            with open(args.trace, "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(dataclasses.asdict(event)) + "\n")
        if args.player and events:
            from nagasim import player
            from nagasim.experiments import BASELINE

            player.build_player(layout, {BASELINE: types.SimpleNamespace(
                events=events, truncated=mapper.truncated)}, path=args.player)
            print(f"player written to {args.player}")
        return _finish(mapper, layout, args.model, args.state)

    connections = netguard.load_connections(args.connections)
    specs = [s for s in layout.get("data_sources", []) if s.get("kind") in readers.KINDS
             and (not args.source or s.get("name") in args.source)]
    if not specs:
        print("no live data source to watch")
        return 1
    for name in sorted({s.get("connection") for s in specs} & set(connections)):
        print(netguard.describe_connection(connections[name]))
    failed, timeline, watch = False, Timeline([], live=True), None
    try:
        with Feed(specs, network=netguard.NetworkPolicy(connections=connections)) as feed:
            watch = Watch(mapping, layout, feed, [s["name"] for s in specs])
            deadline = time.monotonic() + (args.seconds or math.inf)
            while not failed and time.monotonic() < deadline:
                try:
                    events = watch.step()
                except (ReadError, netguard.GuardError) as exc:
                    print(f"{exc.code}: {exc.message}")
                    failed = True
                    continue
                if events:
                    timeline.append(events)
                    print(_picture(timeline, watch.now))
                feed.wait(min(1.0, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        pass
    if watch is None:
        return 1
    return _finish(watch.mapper, layout, args.model, args.state) or int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
