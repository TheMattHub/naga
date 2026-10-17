"""Recommend (PHASE7 7.15, D227 to D230, D298 to D310): candidate actions compared by
simulation from the plant's state of now, ranked as advice, and what became of each.

    asked = recommend.load("candidates.json", model)        # the caller's file (D300)
    advisor = recommend.Advisor(asked, model=model, name="line", horizon=480,
                                out_dir="twin_out", sink=sink)      # the caller's sink, or None
    cycle = twin.Cycle(source, horizon=480, out_dir="twin_out", advisor=advisor)

* A CANDIDATE is an action from a closed vocabulary (`VERBS`; built: `BUILT`). It is an
  override on the forecast's run (D298), never an edit of the model: the model's hash, the
  state's and the twin's rolling score are untouched.
* THE COMPARISON runs each candidate on the forecast's own seed: common random numbers ON
  PURPOSE (D301), so the difference a replication is the observation. One row a candidate
  on ONE KPI: the effect with its 95% paired-t interval, the share of replications in which
  the action beat the baseline, and a verdict. An interval that includes "no effect" is
  `no_effect`, not a win (D302).
* WHAT IS CLAIMED: what this model says the action changes, in distribution. Not a causal
  effect in the plant.
* A candidate is not a forecast: it never enters the twin's history or its rolling score.

Pandas and numpy are loaded only when a comparison runs.
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
from pathlib import Path

from nagasim import outbox
from nagasim.core.schema import load_layout
from nagasim.core.problems import Problem, problem
from nagasim.live import iso
from nagasim.plant import flat_layout
from nagasim.core.utils import canonical_json, is_finite_number, sha256_text

__all__ = ["FORMAT", "KIND", "VERBS", "BUILT", "ORDER_VERBS", "LEVEL", "LIMITS", "REASONS",
           "Refused", "load", "check_action", "override_of", "applied", "effects", "RECORD_KIND",
           "LEDGER_FILE", "LEDGER_KIND", "STATES", "STATUSES", "identity", "lifetime", "record_id",
           "check", "Advisor", "reproduce"]

FORMAT = 1
KIND = "naga-candidates"
# Vocabulary 1 (D228). A verb is built by a decision, with its scoring rule (D299). The
# order verbs since the state carries the plant's order book (D486; D407 lifted).
VERBS = ("set_capacity", "resequence", "expedite", "hold", "release", "reroute")
BUILT = VERBS
ORDER_VERBS = ("resequence", "expedite", "hold", "release")
_PRIORITY = re.compile(r"\bentity\.priority\b")
LEVEL = 0.95                    # of the interval on an effect
BETTER = ("higher", "lower")
REASONS = ("no_forecast", "none_better", "kpi", "replications", "failed")
# Ceilings. The caller's file moves a value under its ceiling; nothing else moves one.
LIMITS = {
    "candidates": 20,           # of one file, so of one set: each is one more forecast
    "file_bytes": 2 ** 20,      # a candidates file
    "kpi": 200,                 # characters of the KPI's name
    "value": 10_000,            # units of a capacity
    "name": 200,                # characters of a block a reroute names, or of an order's id
    "priority": 1e9,            # the size of a priority an expedite gives
    "expires": 86400.0,         # seconds a record may live
    "entries": 200,             # records of the ledger, the newest
    "states": 20,               # state documents kept for reproduction, the newest
    "state_bytes": 2 * 2 ** 20,     # one of them
    "ledger_bytes": 4 * 2 ** 20,    # the ledger file
    "day": 1000,                # records delivered to a sink in one UTC day
    "shown": 20,                # standing actions, and effects, of one result
}
RECORD_KIND = "naga-proposal"
LEDGER_FILE = "twin_outbox.json"
LEDGER_KIND = "naga-twin-outbox"
STATES = "states"               # the folder of kept state documents, in the result directory
# Of a record: not delivered (no sink, or tried again), delivered and unanswered, dropped
# at a ceiling, answered, or void by its date.
STATUSES = ("proposed", "delivered", "dropped", "taken", "rejected", "expired")
_REPLY_COUNTS = ("taken", "rejected", "expired", "ended", "unknown", "repeated", "early",
                 "late", "not_standing", "malformed", "gaps")
_RECORD_KEYS = frozenset({"format", "kind", "id", "created", "expires", "action", "expected",
                          "confidence", "rank", "state", "seed", "replications", "horizon",
                          "model", "scenario", "mapping", "engine"})
_EXPECTED_KEYS = frozenset({"kpi", "better", "level", "baseline", "with_action", "effect", "low",
                            "high", "q05", "q95"})
_EFFECTS = ("waiting", "measured", "missed", "lost", "not_observable")
_BUDGET = 5.0                   # seconds of one step spent delivering (D410)
_PULLS = 10                     # of the replies in one step, 100 replies each
_HEX = re.compile(r"^[0-9a-f]{64}$")
_STATE_FILE = re.compile(r"^[0-9a-f]{64}\.json$")
# The one KPI a plant can show (D308): what a counted block let out over the horizon.
_COUNTED = re.compile(r"^timeseries/(block/.+/out)/max$")


class Refused(Exception):
    """A coded refusal of what the caller gave; `.problem` is what a report carries."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint

    @property
    def problem(self) -> Problem:
        return problem(self.code, self.message, "/candidates", hint=self.hint)


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _value_ok(verb, value) -> bool:
    """Whether *value* is of the kind *verb* takes: a capacity, a block's or an order's name,
    a priority, or true (hold and release take nothing more than the order)."""
    if verb in ("reroute", "resequence"):
        return isinstance(value, str) and 0 < len(value) <= LIMITS["name"] and value.isprintable()
    if verb == "expedite":
        return is_finite_number(value) and abs(value) <= LIMITS["priority"]
    if verb in ("hold", "release"):
        return value is True
    return verb == "set_capacity" and _whole(value) and 1 <= value <= LIMITS["value"]


# ---------------------------------------------------------------- the vocabulary
def check_action(action, model: dict) -> dict:
    """*action* as `{"verb", "target", "value"}` when the migrated *model* can answer it,
    else `Refused`. Exactly those three keys: a record has no room for free text (D228)."""
    def refuse(text, hint=None):
        return Refused("E_CANDIDATES", text, hint)

    if not (isinstance(action, dict) and set(action) == {"verb", "target", "value"}
            and isinstance(action["verb"], str) and isinstance(action["target"], str)):
        raise refuse("an action is exactly {\"verb\", \"target\", \"value\"}, the first two "
                     "texts", "no other key is read, and none is kept")
    verb, target, value = action["verb"], action["target"], action["value"]
    if verb not in BUILT:
        raise Refused("E_CANDIDATE_VERB", f"the verb '{verb[:40]}' is no verb",
                      f"built: {', '.join(BUILT)}")
    if not _value_ok(verb, value):
        short = target[:80]
        raise refuse({
            "reroute": f"reroute: the value for '{short}' is the name of a block",
            "resequence": f"resequence: the value for '{short}' is the id of the order it "
                          f"goes before",
            "expedite": f"expedite: the value for '{short}' is the priority it gets, a number "
                        f"of at most {LIMITS['priority']:g} either way",
            "hold": "hold: the value is true", "release": "release: the value is true",
        }.get(verb, f"set_capacity: the value for '{short}' must be a whole number from 1 to "
                    f"{LIMITS['value']}"))
    if len(target) > LIMITS["name"] or not target.isprintable():
        raise refuse(f"the target '{target[:80]}' is over {LIMITS['name']} characters or "
                     f"not plain text", "a record holds no longer name; rename the block")
    action = {"verb": verb, "target": target, "value": value}
    if verb in ORDER_VERBS:
        _orders_answerable(action, model)   # the order itself is the state's (D487)
    else:
        override_of(action, model)      # the model can answer it, or `Refused` says why not
    return action


def override_of(action: dict, model: dict) -> dict:
    """The one override an action is on the migrated (started) *model*, or `Refused` (D298):
    a path of the grammar (`experiments.apply_overrides`), or, for an order verb, the whole
    state with the action applied (path `initial_state`, D486; see `applied`)."""
    if action["verb"] in ORDER_VERBS:
        return _order_verb(action, model)
    if action["verb"] == "reroute":
        return _reroute(action, model)
    return _capacity(action, model)


def applied(model: dict, action: dict) -> dict:
    """A new *model* with *action* applied. An order verb's state replaces the model's
    here and nowhere else: the shared grammar has no whole-state path, so no binding or
    scenario can write one (D486)."""
    from nagasim import experiments

    override = override_of(action, model)
    if override["path"] != "initial_state":
        return experiments.apply_overrides(model, [override])
    new = copy.deepcopy(model)
    new["initial_state"] = override["value"]
    return new


def _orders_answerable(action: dict, model: dict, flat: dict | None = None) -> None:
    """D487, what the model says: an order verb needs a CREATE that releases orders, and
    `expedite` something that reads a job's priority (a ranked queue, a condition), or the
    forecast could not see it. *flat*: the model's `_flat`, when the caller has it."""
    from nagasim.core import orders

    verb, flat = action["verb"], flat_layout(model) if flat is None else flat
    if not orders.orders_mode_creates(flat):
        raise Refused("E_CANDIDATES", f"{verb}: the model releases no orders (no CREATE in "
                      f"\"Orders\" mode)", "an order verb acts on the orders a model releases")
    if verb == "expedite" and not _PRIORITY.search(json.dumps(
            [b.get("params") for b in flat.get("blocks") or [] if isinstance(b, dict)]
            + [r for r in flat.get("resources") or [] if isinstance(r, dict)], default=str)):
        raise Refused("E_CANDIDATES", "expedite: nothing in the model reads entity.priority, "
                      "so the forecast could not see a new priority",
                      "rank a queue on entity.priority (LowestValueFirst or HighestValueFirst)")


def _order_verb(action: dict, model: dict) -> dict:
    """D486: the started *model*'s state with the order verb applied, from the state's own
    values and the action's constant. The book is the state's, else the model's orders read
    to model time (which by itself changes nothing). D488: an effect the state already shows
    is no change."""
    from nagasim.core import calendar as cal, orders

    verb, target, value = action["verb"], action["target"], action["value"]
    short = target[:80]

    def refuse(text, hint=None):
        return Refused("E_CANDIDATES", f"{verb}: {text}", hint)

    flat = flat_layout(model)     # once: migrating and flattening is what a candidate costs
    _orders_answerable(action, model, flat)
    state = copy.deepcopy(model.get("initial_state") or {})
    clock = float(state.get("clock") or 0.0)
    counted = state.get("orders") or {}
    if not counted and "order_book" not in state and any(
            (state.get("arrivals") or {}).get(name, {}).get("count")
            for name in orders.orders_mode_creates(flat)):
        raise refuse("the state counts released orders but does not name them",
                     "take the state again: a state names its released orders (D452)")
    raw = state.get("order_book") if isinstance(state.get("order_book"), list) \
        else model.get("orders") or []
    try:
        calendar = cal.model_calendar(model)
    except cal.CalendarError:
        calendar = None
    built = orders.build_orders({**model, "orders": raw, "initial_state": {}}, calendar)
    book = []
    for entry in raw:
        entry = {k: v for k, v in entry.items() if k != "time_unit"}   # now in model time
        made = built.get(str(entry.get("id", "")))
        if made is not None:
            entry["release"] = made.release
            if made.due is not None:
                entry["due"] = made.due
        book.append(entry)
    by_id = {str(o.get("id")): o for o in book}
    released = {name for name, spec in counted.items() if (spec or {}).get("created", 0) > 0}
    order = by_id.get(target)
    if order is None:
        raise refuse(f"the state holds no order '{short}'", "an order of the plant's book now")
    if verb in ("hold", "release", "resequence") and target in released:
        raise refuse(f"order '{short}' is released: its jobs are in the plant, and the model "
                     f"does not take them off the floor")
    if verb == "hold":
        order["held"] = True
    elif verb == "release":
        order.pop("held", None)
        order["release"] = min(order["release"], clock)
    elif verb == "expedite":
        done = counted.get(target) or {}
        if target in released and done.get("completed", 0) >= done.get("created", 0):
            raise refuse(f"order '{short}' is complete")
        order["priority"] = value
        for entity in state.get("entities") or []:
            attributes = entity.get("attributes") or {}
            if attributes.get("order_id") == target:
                attributes["priority"] = value
    else:
        other = by_id.get(value)
        if other is None or value in released:
            raise refuse(f"the state holds no order '{value[:80]}' not yet released")
        if order.get("held") or other.get("held"):
            raise refuse("a held order is not in the release sequence", "release it first")
        if order["release"] == other["release"] and target > value:
            raise refuse(f"'{short}' and '{value[:80]}' are released at one time, in order of "
                         f"their ids, which no verb changes")
        if order["release"] > other["release"]:
            order["release"], other["release"] = other["release"], order["release"]
    state["order_book"] = book
    return {"path": "initial_state", "value": state}


def _reroute(action: dict, model: dict) -> dict:
    """`reroute` (D406): every item that reaches the DECIDE *target* from now on goes down
    its one link to the block *value*. A DECIDE holds nobody and routes on arrival, so what
    is past it is untouched in the plant and in the run alike. The conditions are our own
    two constants: no text of the action enters an expression."""
    target, short = action["target"], action["target"][:80]

    def refuse(text, hint=None):
        return Refused("E_CANDIDATES", f"reroute: {text}", hint)

    block = next((b for b in model.get("blocks") or [] if isinstance(b, dict)
                  and b.get("name") == target), None)
    if block is None or str(block.get("type", "")).upper() != "DECIDE":
        raise refuse(f"the model declares no DECIDE block '{short}'",
                     "a target is a DECIDE of the model itself, not one inside an asset")
    # The links in the order the block's outlets are wired (`GraphModel.link`).
    outlets = [link.get("target") for link in model.get("links") or []
               if isinstance(link, dict) and link.get("source") == target]
    if len(outlets) < 2:
        raise refuse(f"'{short}' has no alternative: it has {len(outlets)} link(s)")
    if outlets.count(action["value"]) != 1:
        raise refuse(f"'{short}' has " + ("two links" if action["value"] in outlets else "no link")
                     + f" to '{action['value'][:80]}'",
                     "the value is the block one of its links leads to, and only one")
    params = dict(block.get("params") or {})
    params.update(mode="Condition", conditions=["True" if name == action["value"] else "False"
                                                for name in outlets])
    return {"path": f"blocks.{target}.params", "value": params}


def _capacity(action: dict, model: dict) -> dict:
    """`set_capacity`. A resource of the model itself: its capacity. A
    resource of an asset instance, named as it is after flattening (`<instance>/<name>`):
    the instance's own argument that sets its capacity. A capacity that instances bind is
    NOT the template's to set: such an override would be accepted and change no run."""
    from nagasim import experiments

    target, short = action["target"], action["target"][:80]

    def refuse(text, hint=None):
        return Refused("E_CANDIDATES", f"set_capacity: {text}", hint)

    definitions = model.get("submodels") if isinstance(model.get("submodels"), dict) else {}

    def bound(definition, name):
        """What a definition binds that resource's capacity to, or None."""
        scoped = (definitions.get(definition) or {}).get("scoped") or {}
        said = (scoped.get("resources") or {}).get(name)
        return said.get("capacity") if isinstance(said, dict) else None

    resources = {r.get("name"): r for r in model.get("resources") or [] if isinstance(r, dict)}
    path = name = None
    if target in resources:
        if any(bound(definition, target) is not None for definition in definitions):
            raise refuse(f"the capacity of '{short}' is an instance's argument: the resource "
                         f"is a template", "name the instance's resource, <instance>/<name>")
        path, name = f"resources.{target}.capacity", target
    else:
        for block in model.get("blocks") or []:
            params = block.get("params") if isinstance(block.get("params"), dict) else {}
            instance = block.get("name")
            if str(block.get("type", "")).upper() != "SUBMODEL" or not isinstance(instance, str) \
                    or not target.startswith(instance + "/"):
                continue
            name = target[len(instance) + 1:]
            binding = bound(params.get("definition"), name)
            if name not in resources or binding is None:
                continue
            declared = {p.get("name") for p in (definitions[params["definition"]].get("parameters")
                                                or []) if isinstance(p, dict)}
            if binding not in declared:
                raise refuse(f"the capacity of '{short}' is worked out from the instance's "
                             f"arguments, not one of them")
            path = f"blocks.{instance}.params.arguments.{binding}"
            break
    if path is None:
        raise refuse(f"the model declares no resource '{short}'",
                     "a target is a resource of the model by its name, or an asset "
                     "instance's as <instance>/<name>")
    if resources[name].get("capacity_schedule"):
        raise refuse(f"the capacity of '{short}' follows a schedule",
                     "a capacity a schedule sets is not one an override can change")
    override = {"path": path, "value": action["value"]}
    try:
        experiments.apply_overrides(model, [override])
    except Exception as exc:            # the grammar's own refusal: its class only
        raise refuse(f"'{short}' cannot be overridden ({type(exc).__name__}): an instance "
                     f"must set the argument its capacity comes from") from None
    return override


def load(path, model) -> dict:
    """The caller's candidates file, checked against *model*: `{"kpi", "better", "expires"
    (seconds, or None for one horizon), "actions"}`. Raises `Refused`. The file is data: it
    selects no code, no path and no sink."""
    from nagasim import twin

    def refuse(text, hint="see the candidates file in the twin's documentation"):
        return Refused("E_CANDIDATES", text, hint)

    try:
        if Path(path).stat().st_size > LIMITS["file_bytes"]:
            raise refuse(f"the candidates file is over {LIMITS['file_bytes']} bytes")
        doc = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError) as exc:
        raise refuse(f"the candidates file cannot be read ({type(exc).__name__})") from None
    if not (isinstance(doc, dict) and doc.get("format") == FORMAT and doc.get("kind") == KIND):
        raise refuse(f"this is not a candidates file of format {FORMAT} (kind \"{KIND}\")")
    unknown = sorted(set(doc) - {"format", "kind", "kpi", "better", "expires", "candidates"})
    if unknown:
        raise refuse(f"a candidates file has no key '{str(unknown[0])[:40]}'",
                     "its keys: format, kind, kpi, better, expires, candidates")
    kpi, better, listed = doc.get("kpi"), doc.get("better", "higher"), doc.get("candidates")
    if not (isinstance(kpi, str) and 0 < len(kpi) <= LIMITS["kpi"] and kpi.isprintable()):
        raise refuse("\"kpi\" names the one KPI the candidates are compared on",
                     "a KPI of the forecast's table, such as timeseries/block/<name>/out/max")
    if better not in BETTER:
        raise refuse("\"better\" is \"higher\" or \"lower\"")
    expires = None
    if "expires" in doc:
        try:
            expires = twin.parse_interval(doc["expires"])
        except ValueError as exc:
            raise refuse(f"\"expires\": {exc}") from None
        if not 0 < expires <= LIMITS["expires"]:
            raise refuse("\"expires\" is more than nothing and at most one day")
    if not (isinstance(listed, list) and 0 < len(listed) <= LIMITS["candidates"]):
        raise refuse(f"\"candidates\" is a list of 1 to {LIMITS['candidates']} actions",
                     "each candidate is one more forecast at every forecast")
    migrated = load_layout(model)
    actions = [check_action(action, migrated) for action in listed]
    if len({json.dumps(a, sort_keys=True) for a in actions}) < len(actions):
        raise refuse("two candidates are the same action")
    return {"kpi": kpi, "better": better, "expires": expires, "actions": actions}


# ---------------------------------------------------------------- the comparison
def _values(result, kpi: str) -> dict:
    """{replication: the KPI's value} of one forecast result, the finite ones."""
    from nagasim import experiments, forecast

    frames = result.results
    flat = forecast.series_extreme_kpis(frames) if kpi.startswith("timeseries/") \
        else experiments.flatten_kpis(frames)
    rows = flat[flat["KPI"] == kpi]
    return {int(run): float(value) for run, value in zip(rows["Run"], rows["Value"])
            if math.isfinite(value)}


def _row(action: dict, base: dict, mine: dict, better: str) -> dict:
    import numpy as np

    from nagasim.core.stats import paired_diff_ci

    runs = sorted(set(base) & set(mine))
    if len(runs) < 2:
        return {"action": action, "verdict": "failed", "why": "replications"}
    a, b = [mine[r] for r in runs], [base[r] for r in runs]
    interval = paired_diff_ci(a, b, LEVEL)
    sign = 1 if better == "higher" else -1
    low, high = float(interval["lb"]), float(interval["ub"])
    q05, q95 = (float(q) for q in np.quantile(a, [0.05, 0.95]))
    # The interval excludes "no effect" on the better side, on the other, or holds it.
    verdict = "better" if sign * low > 0 and sign * high > 0 \
        else "worse" if sign * low < 0 and sign * high < 0 else "no_effect"
    return {"action": action, "verdict": verdict, "baseline": sum(b) / len(b),
            "with_action": sum(a) / len(a), "effect": float(interval["mean"]), "low": low,
            "high": high, "q05": q05, "q95": q95, "replications": len(runs),
            "confidence": sum(1 for x, y in zip(a, b) if sign * (x - y) > 0) / len(runs)}


def effects(model, actions, *, kpi: str, better: str, horizon: float, replications: int,
            seed: int, workers=1, should_stop=None, runner=None, baseline=None) -> tuple:
    """(one row an action, the reason no action is recommended or None): each of *actions*
    on the started *model* against the baseline, on the SAME seed (D301). *baseline* is a
    forecast of *model* already run on that seed, or None to run it here. A `better` row
    has its `rank`, from 1. None when *should_stop* said so in between."""
    from nagasim import twin

    run = runner or twin.forecast_from_state
    asked = dict(horizon=horizon, replications=replications, seed=seed, workers=workers,
                 should_stop=should_stop)
    base = _values(run(model, **asked) if baseline is None else baseline, kpi)
    if not base:
        return [], "kpi"
    if len(base) < 2:
        return [], "replications"
    rows = []
    for action in actions:
        if should_stop is not None and should_stop():
            return None, None
        try:
            candidate = applied(model, action)
            rows.append(_row(action, base, _values(run(candidate, **asked), kpi), better))
        except Exception as exc:        # the class only: the text may be a model's
            rows.append({"action": action, "verdict": "failed", "why": type(exc).__name__})
    sign = 1 if better == "higher" else -1
    won = sorted((r for r in rows if r["verdict"] == "better"),
                 key=lambda r: (-sign * r["effect"], -r["confidence"],
                                json.dumps(r["action"], sort_keys=True)))
    for rank, row in enumerate(won, 1):
        row["rank"] = rank
    reason = None if won else "failed" if rows and all(
        r["verdict"] == "failed" for r in rows) else "none_better"
    return won + [r for r in rows if r["verdict"] != "better"], reason


# ---------------------------------------------------------------- the record (D228)
def _sha(value) -> str:
    return sha256_text(canonical_json(value))


def identity(mapping) -> str | None:
    """The hash a record names a plant mapping by, or None for none."""
    try:
        return None if mapping is None else _sha(mapping)
    except (TypeError, ValueError):
        return None


def lifetime(asked: dict, horizon: float, time_unit: str | None) -> float:
    """How long a record lives, in seconds: the caller's `expires`, or one horizon."""
    from nagasim.core.timeunits import UNITS

    if asked.get("expires"):
        return float(asked["expires"])
    return min(max(float(horizon) * UNITS.get(time_unit or "", 1), 1.0), LIMITS["expires"])


def record_id(model: str, scenario: str, state: str, seed: int, action: dict, kpi: str,
              horizon: float) -> str:
    """The id names the PROPOSAL, not its computed numbers (D228): the same action from the
    same state and seed, judged on the same KPI over the same horizon, has the same id on
    every machine; judged on another KPI or horizon it is another proposal."""
    return _sha([model, scenario, state, seed, action, kpi, float(horizon)])


def _count(value) -> bool:
    """A count read back: whole, not negative, and one a float holds exactly (#373)."""
    return _whole(value) and 0 <= value <= 2 ** 53


def _hex(value) -> bool:
    return isinstance(value, str) and _HEX.match(value) is not None


def check(record) -> bool:
    """Whether *record* is a proposal of format 1 and nothing more: every key known, every
    text a name, a stamp or a hash, every number finite, the id the content's own. What a
    ledger read back, a delivery and a reproduction all stand on."""
    try:
        if not (isinstance(record, dict) and set(record) == _RECORD_KEYS
                and record["format"] == FORMAT and record["kind"] == RECORD_KIND
                and len(canonical_json(record)) <= outbox.LIMITS["record_bytes"]):
            return False
        action, expected, state, model = (record[k] for k in ("action", "expected", "state",
                                                               "model"))
        return bool(
            isinstance(action, dict) and set(action) == {"verb", "target", "value"}
            and action["verb"] in BUILT and isinstance(action["target"], str)
            and len(action["target"]) <= LIMITS["name"]
            and _value_ok(action["verb"], action["value"])
            and isinstance(expected, dict) and set(expected) == _EXPECTED_KEYS
            and isinstance(expected["kpi"], str) and len(expected["kpi"]) <= LIMITS["kpi"]
            and expected["better"] in BETTER and expected["level"] == LEVEL
            and all(is_finite_number(expected[k]) for k in _EXPECTED_KEYS - {"kpi", "better"})
            and is_finite_number(record["confidence"]) and _whole(record["rank"])
            and isinstance(state, dict) and set(state) == {"hash", "at", "stamp", "cursors"}
            and _hex(state["hash"]) and is_finite_number(state["at"])
            and (state["stamp"] is None or _stamp(state["stamp"]) is not None)
            and isinstance(state["cursors"], dict) and len(state["cursors"]) <= 50
            and all(isinstance(k, str) and isinstance(v, str) and len(k) + len(v) <= 400
                    for k, v in state["cursors"].items())
            and _whole(record["seed"]) and _whole(record["replications"])
            and 2 <= record["replications"] and is_finite_number(record["horizon"])
            and record["horizon"] > 0
            and isinstance(model, dict) and set(model) == {"name", "hash"}
            and isinstance(model["name"], str) and len(model["name"]) <= 200
            and _hex(model["hash"])
            and isinstance(record["scenario"], str) and len(record["scenario"]) <= 400
            and (record["mapping"] is None or _hex(record["mapping"]))
            and isinstance(record["engine"], str) and len(record["engine"]) <= 40
            and all(_stamp(record[k]) is not None for k in ("created", "expires"))
            and record["id"] == record_id(model["hash"], record["scenario"], state["hash"],
                                          record["seed"], action, expected["kpi"],
                                          record["horizon"]))
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
        return False


def _stamp(value) -> float | None:
    from nagasim.live import stamp

    return stamp(value) if isinstance(value, str) and len(value) <= 64 else None


# ---------------------------------------------------------------- the cycle's part
class Advisor:
    """What a `twin.Cycle` recommends with (`Cycle(advisor=...)`): the caller's candidates
    compared after each forecast, the records, their delivery to the caller's sink, the
    replies and the measured effects. Owned by the cycle's thread, but for `compare`, which
    runs in the forecast's and touches nothing of this object but what it was built with.

    *asked* is `load`'s answer; *sink* an `outbox.open_sink` of the caller's own flag, or
    None: recommendations are then shown and nothing is written outside *out_dir* (D227).
    *lifetime* is a record's, in seconds. With no *out_dir* nothing is kept, and so
    nothing is delivered: a delivered record must reproduce (D303)."""

    def __init__(self, asked: dict, *, model, horizon: float, name: str = "", mapping=None,
                 lifetime: float = 3600.0, out_dir=None, sink=None, wall=time.time,
                 monotonic=time.monotonic):
        from nagasim import state

        self.kpi, self.better = asked["kpi"], asked["better"]
        self.actions = [dict(a) for a in asked["actions"]][:LIMITS["candidates"]]
        self.model_hash, self.name = state.model_hash(model), str(name)[:200]
        self.horizon, self.lifetime = float(horizon), float(lifetime)
        self.mapping = identity(mapping)
        self.out_dir = None if out_dir is None else Path(out_dir)
        self.sink = sink
        self._wall, self._monotonic = wall, monotonic
        counted = _COUNTED.match(self.kpi)
        self.series = counted.group(1) if counted else None     # what a plant can count
        self._set: dict | None = None       # the latest set, as the result shows it
        self._entries: list = []            # the ledger, oldest first
        self._cursor: str | None = None     # of the replies
        self._counts = dict.fromkeys(_REPLY_COUNTS, 0)
        self._day = ["", 0]                 # records delivered on this UTC day
        self._state = ("ok", None)          # of the sink: ok | failing | stopped, and why
        self._retry: dict = {}              # id -> (monotonic time of the next try, the wait)
        self._said: list = []               # problems for the next step, each once
        self._once: set = set()             # the lasting conditions already said
        self._unsaved = False
        self._load()

    # ------------------------------------------------------------ the ledger
    def _load(self) -> None:
        from nagasim import twin

        if self.out_dir is None:
            return
        path = self.out_dir / LEDGER_FILE
        doc = twin._read(path, LEDGER_KIND, LIMITS["ledger_bytes"])
        if doc is None:
            if path.exists():
                self._warn("the ledger in this directory cannot be read as one: it starts "
                           "again and nothing of it stands")
            return
        if doc.get("model") != self.model_hash:
            self._warn("the ledger in this directory is of another model: it starts again "
                       "and nothing of it stands")
            return
        counts = doc.get("counts") if isinstance(doc.get("counts"), dict) else {}
        day = doc.get("day") if isinstance(doc.get("day"), list) else ["", 0]
        if not (all(_count(counts.get(k, 0)) for k in _REPLY_COUNTS) and len(day) == 2
                and isinstance(day[0], str) and _count(day[1])):
            self._warn("the ledger in this directory holds a count that is none: it starts "
                       "again and nothing of it stands")
            return
        found = doc.get("entries") if isinstance(doc.get("entries"), list) else []
        self._entries = [e for e in found if _sound(e)][-LIMITS["entries"]:]
        if isinstance(doc.get("cursor"), str) and doc["cursor"].isdigit() \
                and len(doc["cursor"]) <= 16:
            self._cursor = doc["cursor"]
        self._counts.update({k: counts[k] for k in _REPLY_COUNTS if k in counts})
        self._day = [day[0][:10], day[1]]

    def _save(self) -> None:
        from nagasim import twin

        self._unsaved = False
        if self.out_dir is None:
            return
        try:
            twin.write_json(self.out_dir / LEDGER_FILE, {
                "format": FORMAT, "kind": LEDGER_KIND, "model": self.model_hash,
                "cursor": self._cursor, "counts": self._counts, "day": self._day,
                "entries": self._entries})
        except OSError as exc:
            self._unsaved = True            # written again at the next step
            self._say("ledger", problem(
                "E_TWIN_OUTPUT", f"{LEDGER_FILE} could not be written ({exc})", "/recommend",
                hint="check the result directory; a reader must open, read and close it"))
        else:
            self._once.discard("ledger")

    def _warn(self, text: str) -> None:
        self._said.append(problem("W_TWIN_RECOMMEND", text, "/recommend"))

    def _say(self, key: str, found) -> None:
        """A problem of a lasting condition: once, until the condition has ended."""
        if key not in self._once:
            self._once.add(key)
            self._said.append(found)

    def _entry(self, record_id_: str) -> dict | None:
        return next((e for e in self._entries if e["record"]["id"] == record_id_), None)

    def _keep_state(self, doc: dict) -> bool:
        """The state document as `states/<hash>.json`, the newest kept; whether it is there."""
        from nagasim import twin

        if self.out_dir is None or not _hex(doc.get("hash")):
            return False
        folder = self.out_dir / STATES
        path = folder / f"{doc['hash']}.json"
        try:
            text = json.dumps(doc, allow_nan=False)
            if len(text) > LIMITS["state_bytes"]:
                self._warn(f"the state is over {LIMITS['state_bytes']} bytes and is not kept: "
                           f"this set does not reproduce and none of it is delivered")
                return False
            if not path.exists():
                twin.write_text(path, text)
            wanted = []
            for entry in reversed(self._entries):
                digest = entry["record"]["state"]["hash"]
                if digest not in wanted and len(wanted) < LIMITS["states"] - 1:
                    wanted.append(digest)
            for other in folder.iterdir():      # the twin's own folder, its own names only
                if _STATE_FILE.match(other.name) and other.name[:64] not in wanted \
                        and other != path:
                    other.unlink()
        except (OSError, ValueError, TypeError) as exc:
            self._say("states", problem(
                "W_TWIN_RECOMMEND", f"a state could not be kept ({type(exc).__name__}): this "
                f"set does not reproduce and none of it is delivered", "/recommend"))
            return path.exists()
        self._once.discard("states")
        return True

    # ------------------------------------------------------------ one forecast
    def _standing(self) -> list:
        return [{"id": e["record"]["id"], "action": e["record"]["action"],
                 "since": e["reply"]["read"]}
                for e in self._entries if e.get("stands")][-LIMITS["shown"]:]

    def apply(self, model) -> tuple:
        """(the started *model* with the actions that stand applied, those actions): what
        was taken is in the plant, and the model must be told (D307)."""
        stand = []
        for standing in self._standing():
            try:
                model = applied(model, standing["action"])
            except Exception as exc:        # the model's own refusal: the class only
                self._entry(standing["id"])["stands"], self._unsaved = False, True
                self._warn(f"the action taken on '{standing['action']['target'][:80]}' no "
                           f"longer applies to the model ({type(exc).__name__}): it stands "
                           f"no more")
            else:
                stand.append(standing)
        return model, stand

    def compare(self, model, pending: dict, baseline, *, runner, should_stop, workers,
                standing=()) -> dict | None:
        """In the forecast's thread: the candidates against *baseline*, the forecast just
        made of *model* on `pending["seed"]`. None when the cycle was stopped."""
        began = self._monotonic()
        taken = [canonical_json(s["action"]) for s in standing]
        actions = [a for a in self.actions if canonical_json(a) not in taken]
        rows, reason = effects(
            model, actions, kpi=self.kpi, better=self.better, horizon=pending["horizon"],
            replications=pending["replications"], seed=pending["seed"], workers=workers,
            should_stop=should_stop, runner=runner, baseline=baseline)
        if rows is None:
            return None
        ids = {canonical_json(s["action"]): s["id"] for s in standing}
        rows += [{"action": a, "verdict": "standing", "id": ids[canonical_json(a)]}
                 for a in self.actions if canonical_json(a) in taken]
        return {"rows": rows, "reason": reason, "took": round(self._monotonic() - began, 3)}

    def none(self, reason: str = "no_forecast") -> None:
        """This forecast attempt made no set (D302): nothing earlier is shown as current."""
        self._set = {"status": "none", "reason": reason, "items": []}

    def take(self, pending: dict, state_doc: dict, model, compared, *, standing=(),
             cursors=None) -> None:
        """In the cycle's thread: the set of a forecast that ended, its records kept and
        delivered. *compared* is `compare`'s answer, or what it raised."""
        if compared is None or isinstance(compared, Exception):
            self.none("failed")
            if compared is not None:
                self._said.append(problem(
                    "W_TWIN_RECOMMEND", f"the candidates could not be compared "
                    f"({type(compared).__name__})", "/recommend"))
            return
        if compared["reason"] == "kpi":
            self._say("kpi", problem(
                "E_RECOMMEND_KPI", f"the forecast has no KPI '{self.kpi}': nothing is "
                f"compared", "/recommend", hint="name a KPI of the forecast's table; counted "
                "throughput is timeseries/block/<name>/out/max of a block the mapping counts"))
        made = _stamp(pending.get("stamp"))
        made = self._wall() if made is None else made
        scenario = "Baseline" + ("" if not standing else " after " + ",".join(
            sorted(s["id"][:12] for s in standing)))
        counts = list((model.get("simulation") or {}).get("timeseries_counts") or [])
        wins = [r for r in compared["rows"] if r["verdict"] == "better"]
        kept = bool(wins) and self._keep_state(state_doc)
        items = []
        for row in compared["rows"][:LIMITS["candidates"]]:
            item = {k: v for k, v in row.items() if k != "replications"}
            if row["verdict"] == "better":
                record = {
                    "format": FORMAT, "kind": RECORD_KIND, "created": iso(made),
                    "expires": iso(made + self.lifetime), "action": row["action"],
                    "expected": {"kpi": self.kpi, "better": self.better, "level": LEVEL,
                                 **{k: row[k] for k in ("baseline", "with_action", "effect",
                                                        "low", "high", "q05", "q95")}},
                    "confidence": row["confidence"], "rank": row["rank"],
                    "state": {"hash": pending["state"], "at": pending["from"],
                              "stamp": pending.get("stamp"),
                              "cursors": {str(k): str(v) for k, v in (cursors or {}).items()
                                          if v is not None}},
                    "seed": pending["seed"], "replications": row["replications"],
                    "horizon": pending["horizon"],
                    "model": {"name": self.name, "hash": pending["model"]},
                    "scenario": scenario, "mapping": self.mapping,
                    "engine": nagasim_version()}
                record["id"] = record_id(pending["model"], scenario, pending["state"],
                                         pending["seed"], row["action"], self.kpi,
                                         pending["horizon"])
                item["id"] = record["id"]
                if check(record) and self._entry(record["id"]) is None:
                    self._entries.append({
                        "record": record, "status": "proposed", "attempts": 0, "kept": kept,
                        "counts": counts, "standing": [s["action"] for s in standing]})
            items.append(item)
        self._entries = self._entries[-LIMITS["entries"]:]
        self._set = {"status": "ok" if wins else "none", "reason": compared["reason"],
                     "state": pending["state"], "seed": pending["seed"],
                     "replications": pending["replications"], "from": pending["from"],
                     "stamp": pending.get("stamp"), "took": compared["took"],
                     "reproducible": kept if wins else None, "items": items}
        self._unsaved = True

    # ------------------------------------------------------------ every step
    def step(self, seen: dict, wall: float) -> list:
        """Replies, expiries, deliveries and effects of this step; its problems, each said
        once. *seen* is what the source observed; the plant's present is its `stamp`."""
        present = _stamp(seen.get("stamp"))
        present = wall if present is None else present
        self._replies(seen, present)
        for entry in self._entries:
            if entry["status"] in ("proposed", "delivered") \
                    and present >= _stamp(entry["record"]["expires"]):
                entry["status"], self._unsaved = "expired", True
                self._retry.pop(entry["record"]["id"], None)
        self._deliver(wall)
        self._measure(seen)
        if self._unsaved:
            self._save()
        said, self._said = self._said, []
        return said

    # ------------------------------------------------------------ feedback (D229)
    def _replies(self, seen: dict, present: float) -> None:
        """What was answered since the last step, read like a feed: "what changed since"."""
        if self.sink is None:
            return
        for _ in range(_PULLS):
            before = self._cursor
            got = self.sink.changes(outbox.REPLIES, before, 100)
            self._cursor = got.cursor
            self._counts["malformed"] += got.lost
            self._counts["gaps"] += bool(got.gap)
            if got.state != "live":
                for found in got.problems:
                    self._say("replies", found)
                return
            for found in got.problems:          # a drop or a loss while live (D409)
                if found.code in ("W_LIVE_DROPPED", "W_LIVE_LOST"):
                    self._say("replies:" + found.code, found)
            self._once.discard("replies")
            for event in got.events:
                self._reply(event.value, seen, present)
            if got.cursor == before:
                return
            self._unsaved = True

    def _reply(self, reply: dict, seen: dict, present: float) -> None:
        """One reply. It can do one thing: decide a record of ours that was delivered and
        is not void. The first wins; every other case is counted and dropped (D306)."""
        entry = self._entry(reply["id"])
        at = None
        try:
            at = None if reply["at"] is None else iso(reply["at"])
        except (OverflowError, OSError, ValueError):
            pass
        if reply["decision"] == "ended":
            # The one reply a taken record still hears (D307): it stands no more, from the
            # next forecast. For anything that does not stand it is counted and dropped.
            ends = entry is not None and entry.get("stands") is True
            self._counts["ended" if ends else "unknown" if entry is None else "not_standing"] += 1
            self._unsaved = True
            if ends:
                entry["stands"] = False
                entry["ended"] = {"at": at, "by": reply["by"], "note": reply["note"],
                                  "read": iso(present)}
            return
        kind = "unknown" if entry is None else "repeated" if "reply" in entry \
            else "early" if entry["status"] in ("proposed", "dropped") \
            else "late" if entry["status"] == "expired" \
            or present >= _stamp(entry["record"]["expires"]) else reply["decision"]
        self._counts[kind] += 1
        self._unsaved = True
        if kind != reply["decision"]:
            return
        # `by` and `note` are someone's free text: kept here, in the ledger, and in no
        # result, page or tool answer (D309).
        entry["reply"] = {"decision": kind, "at": at, "by": reply["by"], "note": reply["note"],
                          "read": iso(present)}
        entry["status"] = kind
        if kind == "taken":
            action = entry["record"]["action"]
            for other in self._entries:     # a later action on a target replaces the earlier
                if other.get("stands") and all(other["record"]["action"][k] == action[k]
                                               for k in ("verb", "target")):
                    other["stands"] = False
            entry["stands"] = True
            entry["effect"] = self._effect(entry["record"], seen)

    def _effect(self, record: dict, seen: dict) -> dict:
        """The start of a measurement (D308): what the plant's counter shows NOW, the step
        the reply was read at. No count exists for an instant the twin was not there."""
        counted = _COUNTED.match(record["expected"]["kpi"])
        series = counted.group(1) if counted else None
        cumulative = seen.get("cumulative") or {}
        if series is None or series not in cumulative:
            return {"status": "not_observable"}
        value, now = (seen.get("observed") or {}).get(series), seen.get("at")
        if not (is_finite_number(value) and is_finite_number(now)):
            return {"status": "lost"}       # no reading at this step: no start to count from
        return {"status": "waiting", "series": series, "from": float(now),
                "until": float(now) + record["horizon"],
                "base": [float(value), cumulative[series]]}

    def _measure(self, seen: dict) -> None:
        """An effect whose horizon has passed takes what the counter added since its start,
        once: or is missed (the twin was late) or lost (the count was), by name."""
        from nagasim import twin

        now = seen.get("at")
        for entry in self._entries:
            effect = entry.get("effect") or {}
            if effect.get("status") != "waiting" or not is_finite_number(now) or now < effect["until"]:
                continue
            expected = entry["record"]["expected"]
            if now - effect["until"] > entry["record"]["horizon"] / 6:
                effect["status"] = "missed"
            else:
                value = twin._reading({"base": {effect["series"]: effect["base"]}},
                                      effect["series"], seen)
                if value is None:           # not read at this step: at the next
                    continue
                if value is False:
                    effect["status"] = "lost"
                else:
                    effect.update(status="measured", observed=value, at=float(now),
                                  inside_interval=expected["q05"] <= value <= expected["q95"])
            self._unsaved = True

    def _deliver(self, wall: float) -> None:
        if self.sink is None or self._state[0] == "stopped":
            return
        mono, today = self._monotonic(), iso(wall)[:10]
        if self._day[0] != today:
            self._day = [today, 0]
        began = time.monotonic()
        for entry in self._entries:
            ident = entry["record"]["id"]
            if entry["status"] == "proposed" and time.monotonic() - began > _BUDGET:
                break                       # the rest waits for the next step (D410)
            if entry["status"] != "proposed" or not entry.get("kept") \
                    or self._retry.get(ident, (mono, 0))[0] > mono:
                continue
            if self._day[1] >= LIMITS["day"]:
                entry.update(status="dropped", error="ceiling")
                self._unsaved = True
                self._say("day", problem(
                    "W_OUTBOX", f"over {LIMITS['day']} records were delivered today: the rest "
                    f"is dropped until tomorrow (UTC)", "/outbox"))
                continue
            entry["attempts"], self._unsaved = entry["attempts"] + 1, True
            try:
                self.sink.deliver(entry["record"])
            except ValueError:              # no line under the bound: it never will be
                entry.update(status="dropped", error="record")
            except outbox.Full:
                self._turn("stopped", "Full")
                return
            except OSError as exc:
                wait = min(max(self._retry.get(ident, (0, 0.5))[1] * 2, 1.0), 60.0)
                self._retry[ident] = (mono + wait, wait)
                entry["error"] = str(exc)[:40]
                self._turn("failing", str(exc)[:40])
            else:
                entry.update(status="delivered", delivered=iso(wall))
                entry.pop("error", None)
                self._retry.pop(ident, None)
                self._day[1] += 1
                self._once.discard("day")
                self._turn("ok", None)

    def _turn(self, state: str, why: str | None) -> None:
        """The sink's state; a change of it is said, once (a refusal is not said at every
        step for as long as a reader holds the file)."""
        if (state, why) == self._state:
            return
        self._state = (state, why)
        self._said.append(problem(
            "W_OUTBOX", {"ok": "the outbox takes records again",
                         "failing": f"the outbox could not take a record ({why}): it is tried "
                                    f"again until the record expires",
                         "stopped": f"the outbox file is at its ceiling of "
                                    f"{outbox.LIMITS['sink_bytes']} bytes: nothing more is "
                                    f"written to it"}[state], "/outbox",
            hint=None if state == "ok" else "a reader must open, read and close the file; "
                                            "move or empty a file at its ceiling"))

    # ------------------------------------------------------------ the result
    def document(self) -> dict:
        """The `recommend` key of a result: bounded, plain data, no path and no reply text."""
        shown = dict(self._set or {"status": "none", "reason": "no_forecast", "items": []})
        items = []
        for item in shown["items"]:
            entry = self._entry(item["id"]) if "id" in item else None
            items.append(item if entry is None else {
                **item, "status": entry["status"], "expires": entry["record"]["expires"]})
        by_status = dict.fromkeys(STATUSES, 0)
        for entry in self._entries:
            by_status[entry["status"]] += 1
        return {"kpi": self.kpi, "better": self.better, "level": LEVEL, "crn": True,
                "measurable": self.series is not None,
                **shown, "items": items, "standing": self._standing(),
                # What stood and was ended through the outbox (D307), the newest.
                "ended": [{"id": e["record"]["id"], "action": e["record"]["action"],
                           "since": e["reply"]["read"], "ended": e["ended"].get("read")}
                          for e in self._entries
                          if "ended" in e and "reply" in e][-LIMITS["shown"]:],
                "effects": [{"id": e["record"]["id"], "action": e["record"]["action"],
                             "q05": e["record"]["expected"]["q05"],
                             "q95": e["record"]["expected"]["q95"],
                             **{k: e["effect"].get(k) for k in (
                                 "status", "observed", "inside_interval", "from", "until")}}
                            for e in self._entries if "effect" in e][-LIMITS["shown"]:],
                "outbox": {"sink": None if self.sink is None else self.sink.kind,
                           "state": self._state[0], "why": self._state[1],
                           "records": by_status, "replies": dict(self._counts)}}


def _sound(entry) -> bool:
    """Whether a ledger entry read back is one the advisor can walk."""
    return (isinstance(entry, dict) and check(entry.get("record"))
            and entry.get("status") in STATUSES and _count(entry.get("attempts"))
            and isinstance(entry.get("kept"), bool)
            and isinstance(entry.get("counts"), list) and len(entry["counts"]) <= 1000
            and all(isinstance(name, str) for name in entry["counts"])
            and isinstance(entry.get("reply", {}), dict)
            # Only what a reply TOOK can stand, and only while nothing ended it.
            and (entry.get("stands") is not True
                 or (isinstance(entry.get("reply", {}).get("read"), str)
                     and entry["reply"].get("decision") == "taken"
                     and entry["status"] == "taken" and "ended" not in entry))
            and isinstance(entry.get("ended", {}), dict)
            and _sound_effect(entry.get("effect"))
            and isinstance(entry.get("standing"), list) and len(entry["standing"]) <= 50
            and all(isinstance(a, dict) and set(a) == {"verb", "target", "value"}
                    and a["verb"] in BUILT and isinstance(a["target"], str)
                    and _value_ok(a["verb"], a["value"]) for a in entry["standing"]))


def _sound_effect(effect) -> bool:
    if effect is None:
        return True
    if not (isinstance(effect, dict) and effect.get("status") in _EFFECTS):
        return False
    if effect["status"] == "measured" and not (
            is_finite_number(effect.get("observed")) and isinstance(effect.get("inside_interval"), bool)):
        return False
    base = effect.get("base")
    return effect["status"] != "waiting" or (
        isinstance(effect.get("series"), str) and is_finite_number(effect.get("from"))
        and is_finite_number(effect.get("until")) and isinstance(base, list) and len(base) == 2
        and is_finite_number(base[0]))


def nagasim_version() -> str:
    import nagasim

    return str(nagasim.__version__)[:40]


# ---------------------------------------------------------------- reproduction (D303)
def reproduce(directory, model, *, ids=None, workers=1) -> tuple:
    """(lines to print, how many of them are differences): every recommendation stored in
    the twin's *directory* whose state document is still kept, computed again from that
    state, its seed and the model *model*, and set beside what was stored. Row 7.15's exit
    check. *ids* (full ids, or their first characters) narrows it. Nothing is written.

    The same engine on the same numbers gives the same replications: a number differs
    when it is not equal to nine digits. A record of another engine version is still
    compared, and the version is said."""
    from nagasim import state, twin
    from nagasim.score import _same

    directory = Path(directory)
    doc = twin._read(directory / LEDGER_FILE, LEDGER_KIND, LIMITS["ledger_bytes"])
    found = doc.get("entries") if doc else None
    entries = [e for e in found if _sound(e)] if isinstance(found, list) else []
    if ids:
        entries = [e for e in entries
                   if any(isinstance(i, str) and i and e["record"]["id"].startswith(i) for i in ids)]
    if not entries:
        return ["no stored recommendation can be read there"], 1
    lines, differences, groups = [], 0, {}

    def differs(text: str) -> None:
        nonlocal differences
        differences += 1
        lines.append("DIFFERENT " + text)

    # One baseline a set: the records of one state, seed and standing actions.
    for entry in entries:
        record = entry["record"]
        key = canonical_json([record["state"]["hash"], record["seed"], record["replications"],
                          record["horizon"], record["expected"]["kpi"],
                          record["expected"]["better"], entry["standing"], entry["counts"]])
        groups.setdefault(key, []).append(entry)
    version = nagasim_version()
    for group in groups.values():
        first = group[0]["record"]
        said = ", ".join(e["record"]["id"][:12] for e in group)
        path = directory / STATES / f"{first['state']['hash']}.json"    # 64 hex: `check`
        if not path.exists():
            text = f"{said}: the state {first['state']['hash'][:12]} is no longer kept " \
                   f"(the newest {LIMITS['states']} are)"
            differs(text) if ids else lines.append(text)
            continue
        try:                            # the twin's own bound, not a state file's 16 MiB
            large = path.stat().st_size > LIMITS["state_bytes"]
        except OSError:
            large = True
        if large:
            differs(f"{said}: the kept state is over {LIMITS['state_bytes']} bytes or cannot "
                    f"be read: the twin wrote no such file")
            continue
        kept, problems = state.read(path)
        started = None
        if not problems:
            if not isinstance(kept, dict) or kept.get("hash") != first["state"]["hash"]:
                differs(f"{said}: the kept state is not the one the record names")
                continue
            clock = (kept.get("state") or {}).get("clock") if isinstance(
                kept.get("state"), dict) else None
            started, problems = state.start(
                model, kept, until=(clock if is_finite_number(clock) else 0.0) + first["horizon"])
        if started is None:
            differs(f"{said}: {problems[0].code}: {problems[0].message}")
            continue
        # As the twin's source started it: no warm-up, the plant's counted blocks sampled,
        # the actions that stood applied.
        started["simulation"]["warmup"] = 0
        if group[0]["counts"]:
            started["simulation"]["timeseries_counts"] = list(group[0]["counts"])
        try:
            for action in group[0]["standing"]:
                started = applied(started, action)
            rows, _reason = effects(
                started, [e["record"]["action"] for e in group], kpi=first["expected"]["kpi"],
                better=first["expected"]["better"], horizon=first["horizon"],
                replications=first["replications"], seed=first["seed"], workers=workers)
        except Exception as exc:
            differs(f"{said}: it could not be computed again ({type(exc).__name__})")
            continue
        again = {canonical_json(row["action"]): row for row in rows}
        for entry in group:
            record = entry["record"]
            action, row = record["action"], again.get(canonical_json(record["action"]), {})
            name = f"{record['id'][:12]} {action['verb']} {action['target'][:60]} = {action['value']}"
            note = "" if record["engine"] == version else \
                f" (computed by engine {record['engine']}, this is {version})"
            off = [k for k in ("baseline", "with_action", "effect", "low", "high", "q05", "q95")
                   if not _same(float(record["expected"][k]), row.get(k))]
            # Not the rank: it is a place among the rows of its set, which may be gone.
            if not _same(float(record["confidence"]), row.get("confidence")):
                off.append("confidence")
            if row.get("verdict") != "better" or off:
                differs(f"{name}: " + (f"now '{row.get('verdict')}'" if row.get("verdict") != "better"
                                       else ", ".join(f"{k} stored {_stored(record, k):g}, now "
                                                      f"{row[k]:g}" for k in off)) + note)
            else:
                lines.append(f"{name}: reproduced (effect {row['effect']:+g} on "
                             f"{record['expected']['kpi']}, confidence {row['confidence']:g}, "
                             f"seed {record['seed']}, state {record['state']['hash'][:12]})" + note)
    lines.append("every recommendation whose state is kept reproduces from its state and seed"
                 if not differences else f"{differences} differences")
    return lines, differences


def _stored(record: dict, key: str) -> float:
    return float(record[key] if key in ("confidence", "rank") else record["expected"][key])
