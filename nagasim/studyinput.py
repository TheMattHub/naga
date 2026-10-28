"""Study input normaliser: accept near-miss shapes, return the canonical record, say what changed.

Why: an assistant that sends `range: [1, 2]` or `statement` instead of `low/high`/`text` should not
burn a failed call to learn the shape. We map the known near-misses, store only canonical records
(study.json never holds an alias) and return one note per mapping. Anything ambiguous (two readings
of one value) or a field name that is only a typo is refused with a corrected example, never guessed.
Stdlib only; the tool layer (not this module) decides what to do with the notes.
"""
from __future__ import annotations

import copy
import json

from .core.suggest import close as _close
from .study import StudyError

# Canonical fields per record list. Unknown fields without a close match pass through untouched.
_FIELDS = {
    "goals": ("id", "text", "kpi", "low", "high", "precision", "share_above"),
    "target": ("name", "purpose", "blocks"),
    "assumptions": ("id", "text", "value", "source", "check"),
    "sources": ("id", "text", "input", "path"),     # path: the model field the data feeds
    "validation": ("level", "passed", "text"),
    "experiments": ("id", "text", "result"),
}
_TEXT_ALIASES = {"statement": "text", "description": "text", "title": "text"}
_ALIASES = {
    "goals": {**_TEXT_ALIASES, "min": "low", "max": "high", "target_low": "low", "target_high": "high"},
    "validation": {**_TEXT_ALIASES, "kind": "level", "type": "level"},
    "assumptions": _TEXT_ALIASES, "sources": _TEXT_ALIASES, "experiments": _TEXT_ALIASES,
    "target": {},
}
_TOP_KEYS = ("question", "decision", "mode", "size", "plan_approved", "goals", "target", "assumptions",
             "sources", "validation", "experiments", "skipped", "precision")


def _bad(message: str, hint: str = "") -> StudyError:
    return StudyError("E_STUDY_BAD_VALUE", message, hint)


def _label(key: str, i: int, rec: dict) -> str:
    return f"{key}[{rec['id'] if isinstance(rec.get('id'), str) and rec['id'] else i}]"


def _range_ends(rng, label: str) -> tuple:
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in rng)):
        raise _bad(f"{label}: range must be [low, high]", 'Use "low": <number>, "high": <number>.')
    return rng[0], rng[1]


def _apply_aliases(key: str, i: int, rec: dict, notes: list) -> dict:
    label = _label(key, i, rec)
    out = dict(rec)
    if key == "goals" and "range" in out:
        clash = [e for e in ("low", "high", "min", "max", "target_low", "target_high") if e in out]
        if clash:
            lo, hi = _range_ends(out["range"], label)
            raise _bad(f"{label}: both range and {clash[0]} given; they may disagree",
                       f'Send one reading: {{"low": {lo}, "high": {hi}}} (from range) or drop range '
                       f"and keep {clash[0]}.")
        out["low"], out["high"] = _range_ends(out.pop("range"), label)
        notes.append(f"{label}: range -> low/high")
    for alias, canon in _ALIASES[key].items():
        if alias not in out:
            continue
        if canon in out:
            raise _bad(f"{label}: both {alias} and {canon} given; they may disagree",
                       f"Keep {canon} and drop {alias}.")
        out[canon] = out.pop(alias)
        notes.append(f"{label}: {alias} -> {canon}")
    if key == "validation":
        out = _validation_passed(out, label, notes)
    return out


def _validation_passed(rec: dict, label: str, notes: list) -> dict:
    """`result: "passed"|"failed"` and `ok: bool` both mean the boolean `passed`."""
    given = [f for f in ("result", "ok") if f in rec]
    if not given:
        return rec
    if "passed" in rec or len(given) > 1:
        names = given + (["passed"] if "passed" in rec else [])
        raise _bad(f"{label}: {' and '.join(names)} given; they may disagree",
                   'Send only "passed": true or false.')
    field, v = given[0], rec[given[0]]
    flag = v if field == "ok" and isinstance(v, bool) else {"passed": True, "failed": False}.get(v)
    if flag is None:
        raise _bad(f"{label}: {field} {v!r} is not understood", 'Use "passed": true or false.')
    out = {k: x for k, x in rec.items() if k != field}
    out["passed"] = flag
    notes.append(f"{label}: {field} -> passed")
    return out


def _check_fields(key: str, i: int, rec: dict) -> None:
    for f in rec:
        if f in _FIELDS[key]:
            continue
        near = _close(f, _FIELDS[key])
        if near:
            fixed = {(near[0] if k == f else k): v for k, v in rec.items()}
            raise _bad(f"{_label(key, i, rec)}: unknown field {f!r}; did you mean {', '.join(near)}?",
                       f'Example: {{"{key}": [{json.dumps(fixed)}]}}')


def _records(key: str, value, notes: list) -> list:
    if isinstance(value, dict):
        value = [value]
        notes.append(f"{key}: one object -> list")
    if not isinstance(value, list):
        return value                       # the tool's own check names the type error
    out = []
    for i, rec in enumerate(value):
        if key == "target" and isinstance(rec, str):
            notes.append(f"target[{rec}]: string -> {{name}}")
            rec = {"name": rec}
        if isinstance(rec, dict):
            rec = _apply_aliases(key, i, rec, notes)
            _check_fields(key, i, rec)
        out.append(rec)
    return out


def normalise(changes: dict) -> tuple[dict, list[str]]:
    """Return (canonical changes, notes). A canonical input comes back equal, with no notes."""
    if not isinstance(changes, dict):
        return changes, []
    notes: list[str] = []
    out = {}
    for key, value in changes.items():
        if key not in _TOP_KEYS:
            near = _close(key, _TOP_KEYS)
            if near:
                raise _bad(f"unknown key {key!r}; did you mean {', '.join(near)}?",
                           f'Example: {{"{near[0]}": ...}}')
        out[key] = _records(key, copy.deepcopy(value), notes) if key in _FIELDS else value
    return out, notes


def _overrides(value, label: str, notes: list):
    if isinstance(value, dict):
        if set(value) == {"path", "value"}:
            notes.append(f"{label}: one override -> list")
            return [value]
        notes.append(f"{label}: {{path: value}} -> list of {{path, value}}")
        return [{"path": p, "value": v} for p, v in value.items()]
    if isinstance(value, list):
        for j, o in enumerate(value):
            bad = [f for f in o if f not in ("path", "value")] if isinstance(o, dict) else []
            if bad:
                near = _close(bad[0], ("path", "value"))
                raise _bad(f"{label}[{j}]: unknown field {bad[0]!r}"
                           + (f"; did you mean {', '.join(near)}?" if near else ""),
                           'Each override is {"path": "resources.<name>.capacity", "value": 2}.')
    return value


def normalise_layout(layout: dict) -> tuple[dict, list[str]]:
    """Normalise only scenario overrides; the rest of the model is the engine's to validate."""
    scenarios = layout.get("scenarios") if isinstance(layout, dict) else None
    if not isinstance(scenarios, list):
        return layout, []
    notes: list[str] = []
    out = copy.deepcopy(layout)
    for i, sc in enumerate(out["scenarios"]):
        if isinstance(sc, dict) and "overrides" in sc:
            label = f"scenarios[{sc.get('name', i)}].overrides"
            sc["overrides"] = _overrides(sc["overrides"], label, notes)
    return out, notes
