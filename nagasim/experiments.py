"""Scenario manager, precision helper and warm-up helper (ROADMAP 1.6).

Top-level module beside `api.py`, deliberately NOT in `nagasim/core/`: it needs
pandas and it sits above the engine. Dependencies point downward only --
experiments -> api -> core, never the other way.

    from nagasim import experiments
    result = experiments.run_scenarios(layout, replications=20, workers=4)
    table = result.comparison()        # KPI x scenario, with paired-t CIs

A scenario is stored in the model file (schema v9):

    {"name": "One more agent", "description": "...",
     "overrides": [{"path": "resources.Agents.capacity", "value": 6}]}

The implicit scenario "Baseline" (no overrides) always exists.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import argparse
import copy
from contextlib import nullcontext
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

from nagasim import api
from nagasim._cli import setup_cli
from nagasim.core import schema, sections
from nagasim.core.export import concat_runs
from nagasim.core.outputs import LOG_DERIVED_COLUMNS
from nagasim.core.schema import ModelValidationError
from nagasim.core.stats import ci95, paired_diff_ci, welch_diff_ci

__all__ = ["BASELINE", "apply_overrides", "value_at", "list_override_paths", "override_targets",
           "scenarios_of", "flatten_kpis", "needs_event_logs", "KPI_PREFIXES", "ExperimentResult",
           "run_scenarios",
           "record_scenarios",
           "run_until_precision", "suggest_warmup", "WarmupSuggestion", "COMPARISON_COLS", "main"]

BASELINE = "Baseline"

# The sections an override path addresses by entry key (core/sections.py's
# `overridable` rows): by "name" (blocks and most record sections; a block's
# params only), by "id" for orders (PHASE4 A3, "orders.WO-1001.due"), and
# links by "<source>-><target>" -- plus "#1" for the second and further
# PARALLEL links between one pair (D77).
_NAMED_SECTIONS = tuple(s.name for s in sections.SECTIONS if s.overridable and s.key == "name")
_ID_SECTIONS = tuple(s.name for s in sections.SECTIONS if s.overridable and s.key == "id")
_SECTIONS = _NAMED_SECTIONS + _ID_SECTIONS + ("links",)
# ... plus "submodels" (ROADMAP 2.3 part B), a dict keyed by definition name:
# "submodels.<Definition>.blocks.<block>.params.<field>" edits the definition,
# so EVERY instance of it moves together. An instance's own bindings are
# reached the ordinary way: "blocks.<instance>.params.arguments.<parameter>".
_SUBMODEL_SECTION = "submodels"


def _as_layout(layout) -> dict:
    """A migrated layout dict from either a layout dict or a model file path."""
    return api.load_layout(layout)


# ---------------------------------------------------------------- override paths
def _entry_key(section: str, entry: dict) -> str:
    if section == "links":
        return f"{entry.get('source')}->{entry.get('target')}"
    if section == "network.segments":     # PHASE4 E1, same "a->b" shape
        return f"{entry.get('from')}->{entry.get('to')}"
    if section in _ID_SECTIONS:
        return entry.get("id", "")
    return entry.get("name", "")


def _entry_keys(section: str, entries: list) -> list[str]:
    """One override key per entry, in list order. Repeated link pairs get a
    "#<index>" suffix from the second one on (D77), so both parallel links are
    addressable and the first one's path is unchanged."""
    keys = [_entry_key(section, entry) for entry in entries]
    if section != "links":
        return keys
    seen: dict[str, int] = {}
    out = []
    for key in keys:
        index = seen.get(key, 0)
        seen[key] = index + 1
        out.append(key if index == 0 else f"{key}#{index}")
    return out


def _fail(path: str, reason: str) -> None:
    raise ModelValidationError([f"override path '{path}': {reason}"])


def _split_entry(section: str, entries: list, rest: str, path: str) -> tuple[dict, list[str]]:
    """Split "<entry name>[.<field>...]" against the section's ACTUAL names, so a
    resource called "Line 2.a" still resolves -- splitting on every dot would
    not. Longest name first, so "Agents" never shadows "Agents.night"."""
    keyed = sorted(zip(_entry_keys(section, entries), entries), key=lambda pair: -len(pair[0]))
    for key, entry in keyed:
        if not key:
            continue
        if rest == key:
            return entry, []
        if rest.startswith(key + "."):
            return entry, rest[len(key) + 1:].split(".")
    _fail(path, f"no {section} entry named '{rest.split('.')[0]}'")


def _descend(container, fields: list[str], path: str) -> tuple[dict, str]:
    """Walk *fields* through nested dicts and return (parent, last key).

    Every key on the way, the leaf included, must already exist: an override is
    a change to a value the model has, never a way to invent a parameter no
    block reads.
    """
    if not fields:
        _fail(path, "names no field")
    for key in fields[:-1]:
        if not isinstance(container, dict) or key not in container:
            _fail(path, f"no such field '{key}'")
        container = container[key]
    if not isinstance(container, dict) or fields[-1] not in container:
        _fail(path, f"no such field '{fields[-1]}'")
    return container, fields[-1]


def _split_key(names, rest: str, path: str, what: str) -> tuple[str, str]:
    """Split "<name>.<remainder>" against actual *names* (longest first), so a
    definition or block whose name contains a dot still resolves."""
    for key in sorted(names, key=len, reverse=True):
        if rest == key:
            return key, ""
        if rest.startswith(key + "."):
            return key, rest[len(key) + 1:]
    _fail(path, f"no {what} named '{rest.split('.')[0]}'")


def _resolve_submodel(layout: dict, rest: str, path: str) -> tuple[dict, str]:
    """"submodels.<Definition>.blocks.<block>.params.<field>"."""
    definitions = layout.get(_SUBMODEL_SECTION) or {}
    def_name, remainder = _split_key(definitions, rest, path, "submodel definition")
    definition = definitions[def_name]
    inner, _, block_rest = remainder.partition(".")
    if inner != "blocks":
        _fail(path, "only a submodel definition's blocks can be overridden "
                    "(submodels.<name>.blocks.<block>.params....)")
    entry, fields = _split_entry("blocks", definition.get("blocks", []), block_rest, path)
    if fields[:1] != ["params"]:
        _fail(path, "only a block's params can be overridden")
    return _descend(entry, fields, path)


def _resolve_network(layout: dict, rest: str, path: str) -> tuple[dict, str]:
    """"network.nodes.<name>.<field>" / "network.segments.<a>-><b>.<field>"."""
    network = layout.get("network") or {}
    kind, _, remainder = rest.partition(".")
    if kind == "length_factor":
        return _descend(network, ["length_factor"], path)
    if kind not in ("nodes", "segments"):
        _fail(path, "only network.nodes, network.segments and network.length_factor "
                    "can be overridden")
    entry, fields = _split_entry(f"network.{kind}", network.get(kind) or [], remainder, path)
    return _descend(entry, fields, path)


# What a plant can report of a state (PHASE7 7.10, D236), and what each starts from.
_STATE_FIELDS = {"resources": {"down_until": 0.0, "busy": 0},
                 "entities": {"count": 0, "in_service": 0}}


def _resolve_state(layout: dict, rest: str, path: str) -> tuple[dict, str]:
    """"initial_state.variables.<name>", ".levels.<name>", ".resources.<name>.down_until"
    or ".busy", ".entities.<block>.count" or ".in_service".

    The one place a path may address a value the model does not hold yet: a state is what
    a plant reports, and a model file usually stores none. The name is not checked here;
    the section's own validation refuses an unknown one, with its code."""
    state = layout.setdefault("initial_state", {})
    kind, _, remainder = rest.partition(".")
    if kind in ("variables", "levels") and remainder:
        declared = {e.get("name"): e.get("value" if kind == "variables" else "initial", 0)
                    for e in layout.get(kind, []) or []}
        container = state.setdefault(kind, {})
        container.setdefault(remainder, declared.get(remainder, 0))
        return container, remainder
    name, _, leaf = remainder.rpartition(".")
    if kind not in _STATE_FIELDS or not name or leaf not in _STATE_FIELDS[kind]:
        _fail(path, "of a state only variables.<name>, levels.<name>, "
                    "resources.<name>.down_until or .busy and entities.<block>.count or "
                    ".in_service can be written")
    if kind == "resources":
        entry = state.setdefault("resources", {}).setdefault(name, {})
    else:
        entries = state.setdefault("entities", [])
        entry = next((e for e in entries if e.get("block") == name), None)
        if entry is None:
            entry = {"block": name, "count": 0}
            entries.append(entry)
    entry.setdefault(leaf, _STATE_FIELDS[kind][leaf])
    return entry, leaf


def _resolve(layout: dict, path: str) -> tuple[dict, str]:
    """(container, key) the override path addresses inside *layout*."""
    if not path:
        _fail("", "empty path")
    # The one whole-section path (PHASE4 A3): an MES binding replaces the order
    # book outright, which is not a change to a value the model already has and
    # so has no leaf to address. Deliberately only "orders" -- every other
    # section stays leaf-addressable, an override being an edit, not a rewrite.
    if path == "orders":
        layout.setdefault("orders", [])
        return layout, "orders"
    section, _, rest = path.partition(".")
    # The path network (PHASE4 E1) is one OBJECT holding two named lists, so it
    # gets its own two-level split: "network.nodes.<node>.x" moves a dock,
    # "network.segments.<a>-><b>.length" lengthens an aisle. Those are the
    # geometry knobs a layout study actually turns.
    if section == "network":
        return _resolve_network(layout, rest, path)
    if section == "simulation":
        return _descend(layout.get("simulation", {}), rest.split(".") if rest else [], path)
    if section == "initial_state":
        return _resolve_state(layout, rest, path)
    if section == _SUBMODEL_SECTION:
        return _resolve_submodel(layout, rest, path)
    if section not in _SECTIONS:
        _fail(path, f"unknown section '{section}' (expected one of "
                    f"{', '.join(_SECTIONS + ('simulation', 'network', _SUBMODEL_SECTION))})")
    entry, fields = _split_entry(section, layout.get(section, []), rest, path)
    if section == "agent_types" and "python" in fields:
        # PHASE6 A7 (D105): a scenario, a data binding or an agent tool must
        # never rewrite code a caller consented to run.
        _fail(path, "an agent type's python code cannot be overridden")
    if section == "blocks" and fields[:1] != ["params"]:
        _fail(path, "only a block's params can be overridden")
    if section == "links" and fields[:1] != ["delay"]:
        _fail(path, "only a link's delay can be overridden")
    return _descend(entry, fields, path)


def apply_overrides(layout: dict, overrides) -> dict:
    """Return a NEW layout with every override applied; *layout* is untouched.

    Raises ModelValidationError naming the path when it addresses nothing in
    this model. The result is not validated here -- `run_scenarios` does that
    once per scenario, so a caller building a layout by hand can still apply an
    override to an intermediate state.
    """
    new = copy.deepcopy(layout)
    for override in overrides or ():
        container, key = _resolve(new, override.get("path", ""))
        container[key] = copy.deepcopy(override.get("value"))
    return new


def value_at(layout: dict, path: str):
    """The value *path* currently addresses in *layout*.

    The read half of the override grammar: `nagasim.databind` (ROADMAP 3.4a)
    needs it to type-check a binding's result against the field it is about to
    overwrite. Raises ModelValidationError naming the path when it addresses
    nothing, exactly as `apply_overrides` does.
    """
    if path.startswith("initial_state."):
        # Resolving one creates the entry it addresses: on a copy, a read changes nothing.
        layout = {**layout, "initial_state": copy.deepcopy(layout.get("initial_state") or {})}
    container, key = _resolve(layout, path)
    return container[key]


def _walk_override_targets(layout: dict) -> list[tuple[str, str, str, str, object]]:
    """Every leaf the path grammar can address, as (path, section, key, field,
    current value) -- one walk shared by `list_override_paths` (path, value)
    and `override_targets` (path, section, key, field), so both stay in step
    with the same grammar instead of two copies of it.

    A leaf is anything that is not a dict, so a schedule's `points` list
    appears as one addressable value (replacing the whole calendar is the
    normal way to change a shift). An entry's own `name` is left out:
    renaming a resource through an override would silently break every block
    that refers to it. *field* is the leaf's path under the entry's own root
    ("params.capacity" for a block's capacity, "" for a section-level leaf
    such as "simulation.seed" or "network.length_factor").
    """
    out: list[tuple[str, str, str, str, object]] = []

    def walk(root: str, section: str, key: str, field: str, value) -> None:
        if isinstance(value, dict):
            for sub_key, sub in value.items():
                if sub_key not in ("name", "id", "python"):  # "id": an order's key; python: A7
                    walk(root, section, key, f"{field}.{sub_key}" if field else sub_key, sub)
        else:
            out.append((f"{root}.{field}" if field else root, section, key, field, value))

    for section in _SECTIONS:
        entries = layout.get(section, [])
        for key, entry in zip(_entry_keys(section, entries), entries):
            if not key:
                continue
            root = f"{section}.{key}"
            if section == "blocks":
                walk(root, section, key, "params", entry.get("params", {}))
            elif section == "links":
                if entry.get("delay"):
                    walk(root, section, key, "delay", entry["delay"])
            else:
                walk(root, section, key, "", entry)
    # Submodel definitions (ROADMAP 2.3 part B): one path per block param
    # inside each definition. An instance's `arguments` already appear above,
    # under "blocks.<instance>.params.arguments.<parameter>".
    for def_name, definition in (layout.get(_SUBMODEL_SECTION) or {}).items():
        for entry in definition.get("blocks", []):
            if entry.get("name"):
                key = f"{def_name}.blocks.{entry['name']}"
                walk(f"{_SUBMODEL_SECTION}.{key}", _SUBMODEL_SECTION, key, "params",
                     entry.get("params", {}))
    # The path network (PHASE4 E1): its nodes and segments are the geometry a
    # layout study varies, so each one's fields are addressable.
    network = layout.get("network") or {}
    for kind in ("nodes", "segments"):
        section = f"network.{kind}"
        for entry in network.get(kind) or []:
            key = _entry_key(section, entry)
            if key:
                walk(f"{section}.{key}", section, key, "", entry)
    if "length_factor" in network:
        out.append(("network.length_factor", "network", "", "length_factor",
                    network["length_factor"]))
    walk("simulation", "simulation", "", "", layout.get("simulation", {}))
    return out


def list_override_paths(layout: dict) -> list[tuple[str, object]]:
    """Every leaf the path grammar can address, as (path, current value).
    Feeds the UI's path picker."""
    return [(path, value) for path, _section, _key, _field, value
            in _walk_override_targets(layout)]


def override_targets(layout: dict) -> list[tuple[str, str, str, str]]:
    """Every leaf the path grammar can address, as (path, section, key,
    field): the section it belongs to, the entry key inside that section (by
    name, id, or "source->target" for a link; "" for a section-level path
    such as "simulation.seed" or "network.length_factor"), and the field path
    under that entry's root. The public seam UI code (`param_labels` in
    `product/ui/experiments.py`) uses to build parameter labels, instead of
    reaching into `_entry_key`/`_entry_keys`/`_SECTIONS` directly.
    """
    return [(path, section, key, field) for path, section, key, field, _value
            in _walk_override_targets(layout)]


def scenarios_of(layout: dict) -> list[dict]:
    """The model's scenarios with the implicit "Baseline" first (unless the
    model declares one of its own by that name)."""
    stored = [copy.deepcopy(s) for s in layout.get("scenarios", [])]
    if not any(s.get("name") == BASELINE for s in stored):
        stored.insert(0, {"name": BASELINE, "overrides": []})
    return stored


# ---------------------------------------------------------------- KPI flattening
_OBJECT_FRAMES = (("resource_summary", "Resource", "resource"),
                  ("store_summary", "Store", "store"),
                  ("entity_summary", "Entity Type", "entity"),
                  # PHASE4 A3. Both are empty for a model with no orders and no
                  # due dates, so they add no KPI key to an existing model --
                  # and give `order/<id>/Tardiness` and
                  # `duedate/(all)/Service Level` to one that has them, which
                  # is what the paired-t scenario comparison then reads.
                  ("order_summary", "Order", "order"),
                  ("due_date_summary", "Entity Type", "duedate"),
                  # PHASE4 A4, same deal: `queue/<block>/Reneged` and friends
                  # for a model that declares a queue behaviour, nothing for
                  # one that does not.
                  ("queue_summary", "Block", "queue"),
                  # D156: `motion/<block>/<kind>/Mean Time` and friends ("cycle"
                  # for the whole cycle) -- nothing for a model without MOTION.
                  ("motion_summary", "Motion", "motion"),
                  # PHASE4 D1: `level/<name>/Average` and `flow/<name>/Total`
                  # for a model with continuous quantities, nothing for one
                  # without -- so the optimizer and the scenario comparison
                  # address them like any other KPI.
                  ("level_summary", "Level", "level"),
                  ("flow_summary", "Flow", "flow"),
                  # Bio-1: `conversion/<name>/<substrate>/Produced` and
                  # `component/<level>/<component>/Avg Fraction` -- the id
                  # column already carries both halves of the key.
                  ("conversion_summary", "Conversion", "conversion"),
                  ("component_summary", "Component", "component"),
                  # PHASE4 D5: `cost/<item>/Amount`, so the optimizer can trade
                  # a capex against operating cost without a pseudo-flow.
                  ("cost_summary", "Item", "cost"),
                  # PHASE4 E1: `fleet/<name>/Distance Empty`,
                  # `segment/<a<->b>/Traversals` and `conveyor/<block>/Blocked %`
                  # -- nothing at all for a model with no network.
                  ("fleet_summary", "Fleet", "fleet"),
                  ("segment_summary", "Segment", "segment"),
                  ("conveyor_summary", "Conveyor", "conveyor"),
                  # D164-D165: `charger/<name>/Grid kWh` and
                  # `battery/<fleet>/Charging %` -- nothing without batteries.
                  ("charging_summary", "Charger", "charger"),
                  ("battery_summary", "Fleet", "battery"),
                  # D169: `tending/<station>/<fleet>/Avg Loaded Wait` -- two id
                  # columns, joined; nothing for a model no cobot tends in.
                  ("tending_summary", ("Station", "Fleet"), "tending"),
                  # PHASE4 T5: `buffer/<block>/Blocked %` and `.../Avg Content`,
                  # so a buffer-allocation study can optimize on them directly.
                  ("buffer_summary", "Block", "buffer"),
                  # PHASE6 W3 M1 (D112): `energy/<resource>/Total kWh` and
                  # `.../Energy Cost` -- nothing for a model with no "power".
                  ("energy_summary", "Resource", "energy"),
                  # PHASE6 W3 M2 (D113): `bottleneck/<resource>/Sole %` and
                  # friends -- nothing unless the model sets
                  # `outputs.bottleneck`. "material_flow_summary" is NOT here:
                  # its natural key is the (From, To) pair, not one id column,
                  # and a per-link entity count is not the kind of thing a
                  # scenario comparison or the optimizer addresses by name.
                  ("bottleneck_summary", "Resource", "bottleneck"),
                  # PHASE5 AB-1: `agent/<population>/<state>/Avg Count` and
                  # friends -- nothing for a model without populations.
                  ("agent_summary", "Agent State", "agent"),
                  # PHASE6 C2: `agent/<population>/<attribute>/<state>/End Mean`
                  # and friends ("All" for the population-wide row) -- nothing
                  # for a model whose agents declare no attributes.
                  ("agent_attributes", "Agent Attribute", "agent"))
# The first part of every `flatten_kpis` key.
KPI_PREFIXES = ("system", "user", *dict.fromkeys(prefix for _f, _c, prefix in _OBJECT_FRAMES))


def flatten_kpis(results: dict) -> pd.DataFrame:
    """The scalar KPI frames (system/resource/store/entity/user -- everything
    but "timeseries", a curve rather than one number) as one long
    Run / KPI / Value frame, numbers only.

    Stable keys, so a scenario comparison or a precision target can name a KPI
    without knowing which frame it came from:

        system/<Metric>              e.g. system/Avg Throughput Time
        resource/<name>/<column>     e.g. resource/Agents/Usage % (snapshot)
        store/<name>/<column>
        entity/<type>/<column>
        user/<statistic>             the RECORD statistic's own Value
        cost/<item>/Amount           a fixed, per-event or peak cost (PHASE4 D5)
    """
    rows: list[tuple] = []

    sys_df = results.get("system")
    if sys_df is not None and not sys_df.empty:
        values = pd.to_numeric(sys_df["Value"], errors="coerce")
        for run, metric, value in zip(sys_df["Run"], sys_df["Metric"], values):
            if metric != "Run" and pd.notna(value):
                rows.append((run, f"system/{metric}", float(value)))

    for key, obj_col, prefix in _OBJECT_FRAMES:
        df = results.get(key)
        if df is None or df.empty:
            continue
        objects = (df[obj_col] if isinstance(obj_col, str)
                   else df[list(obj_col)].astype(str).agg("/".join, axis=1))
        for column in df.select_dtypes("number").columns:
            if column == "Run":
                continue
            for run, obj, value in zip(df["Run"], objects, df[column]):
                if pd.notna(value):
                    rows.append((run, f"{prefix}/{obj}/{column}", float(value)))

    user = results.get("user_summary")
    if user is not None and not user.empty:
        for run, name, value in zip(user["Run"], user["Statistic"],
                                    pd.to_numeric(user["Value"], errors="coerce")):
            if pd.notna(value):
                rows.append((run, f"user/{name}", float(value)))

    return pd.DataFrame(rows, columns=["Run", "KPI", "Value"])


def needs_event_logs(kpis) -> bool:
    """Whether any of the KPI keys *kpis* is computed from the per-event logs,
    so a study asking for it must not run with `record_logs` off.

    Keys are spelled as `flatten_kpis` writes them, plus the raw log columns a
    calibration target names (`entity_log/Throughput Time`). The column is the
    last `/`-separated part, because an object name may itself contain a `/`.
    """
    frames = {prefix: frame for frame, _col, prefix in _OBJECT_FRAMES}
    for kpi in kpis:
        prefix, _, rest = str(kpi).partition("/")
        if prefix in ("entity_log", "resource_log", "store_log"):
            return True
        if rest.rpartition("/")[2] in LOG_DERIVED_COLUMNS.get(frames.get(prefix), ()):
            return True
    return False


def _series(flat: pd.DataFrame, kpi: str) -> np.ndarray:
    """The KPI's per-replication values ordered by Run, so two scenarios run on
    the same seeds line up element by element for the paired interval."""
    sub = flat[flat["KPI"] == kpi].sort_values("Run")
    return sub["Value"].to_numpy(dtype=float)


# ---------------------------------------------------------------- experiment run
COMPARISON_COLS = ["KPI", "Scenario", "Mean", "LB", "UB",
                   "Diff vs baseline", "Diff LB", "Diff UB", "Significant"]


@dataclass
class ExperimentResult:
    """What `run_scenarios` returns: the eight frames per scenario, plus the
    seeds each one used and whether every scenario finished."""

    results: dict[str, dict] = field(default_factory=dict)
    seeds: dict[str, list[int]] = field(default_factory=dict)
    crn: bool = True
    completed: bool = True

    @property
    def scenarios(self) -> list[str]:
        return list(self.results)

    def kpis(self) -> pd.DataFrame:
        """Every scenario's flattened KPIs in one frame, with a Scenario column."""
        frames = [flatten_kpis(res).assign(Scenario=name) for name, res in self.results.items()]
        return (pd.concat(frames, ignore_index=True) if frames
                else pd.DataFrame(columns=["Run", "KPI", "Value", "Scenario"]))

    def comparison(self, baseline: str = BASELINE, kpis=None,
                   confidence: float = 0.95) -> pd.DataFrame:
        """One row per (KPI, scenario): the scenario's own mean with a Student-t
        CI, and the difference against *baseline* with its own CI.

        With common random numbers the difference interval is the PAIRED-t one
        on the per-replication differences (the shared noise cancels); without
        them the two samples are independent and it is Welch's. "Significant"
        is simply "the difference interval excludes 0", so the baseline's own
        row -- difference exactly 0 -- is never significant.
        """
        flat = {name: flatten_kpis(res) for name, res in self.results.items()}
        if baseline not in flat:
            raise ValueError(f"unknown baseline scenario: {baseline!r}")
        base_flat = flat[baseline]
        wanted = set(kpis) if kpis is not None else None

        names = sorted({k for frame in flat.values() for k in frame["KPI"].unique()})
        if wanted is not None:
            names = [k for k in names if k in wanted]

        diff_ci = paired_diff_ci if self.crn else welch_diff_ci
        rows = []
        for kpi in names:
            base_values = _series(base_flat, kpi)
            for scenario, frame in flat.items():
                values = _series(frame, kpi)
                own = ci95(values, confidence)
                diff = diff_ci(values, base_values, confidence)
                significant = bool(np.isfinite(diff["lb"]) and np.isfinite(diff["ub"])
                                   and (diff["lb"] > 0 or diff["ub"] < 0))
                rows.append({"KPI": kpi, "Scenario": scenario, "Mean": own["mean"],
                             "LB": own["lb"], "UB": own["ub"],
                             "Diff vs baseline": diff["mean"], "Diff LB": diff["lb"],
                             "Diff UB": diff["ub"], "Significant": significant})
        return pd.DataFrame(rows, columns=COMPARISON_COLS)


def _scenario_layouts(base: dict, scenario_names, baseline: bool = True) -> dict[str, dict]:
    """Apply and validate every chosen scenario up front, so a typo in an
    override is an error before the first replication rather than after the
    last one."""
    wanted = None if scenario_names is None else list(scenario_names)
    # A caller's subset always gets the comparison baseline too (its own doc,
    # and the run_scenarios agent tool's, promise it): dropping it here is
    # what used to make ExperimentResult.comparison()'s default lookup raise
    # "unknown baseline scenario" (DEVLOG #275). `baseline=False` is the one
    # caller that holds a baseline already.
    if wanted is not None and baseline and BASELINE not in wanted:
        wanted = [BASELINE] + wanted
    layouts: dict[str, dict] = {}
    for scenario in scenarios_of(base):
        name = scenario.get("name") or "<unnamed>"
        if wanted is not None and name not in wanted:
            continue
        try:
            candidate = apply_overrides(base, scenario.get("overrides", []))
        except ModelValidationError as exc:
            raise ModelValidationError([f"scenario '{name}': {p}" for p in exc.problems]) from None
        fatal = schema.fatal_problems(schema.validate_model_structured(candidate))  # by severity (#247)
        if fatal:
            raise ModelValidationError(
                [f"scenario '{name}': {p}" for p in schema.format_problems(fatal)])
        layouts[name] = candidate
    if wanted is not None:
        missing = [n for n in wanted if n not in layouts]
        if missing:
            raise ModelValidationError([f"unknown scenario: {n}" for n in missing])
    return layouts


def _window(candidate: dict, replications, run_length, warmup) -> tuple:
    """(replications, run length, warm-up) one scenario runs with: its own
    `simulation.time_limit` and `warmup` unless the caller gave one (#315). One
    place, so the recording of a run (`record_scenarios`) has the window of that run."""
    sim = candidate.get("simulation", {})
    replications, warm, length = api.run_settings(
        candidate, replications,
        (sim.get("warmup") or 0.0) if warmup is None else warmup,
        (sim.get("time_limit") or 0.0) if run_length is None else run_length)
    return replications, length, warm


def run_scenarios(layout, scenario_names=None, *, replications: int | None = None, seed: int = 0,
                  run_length: float | None = None, warmup: float | None = None, crn: bool = True,
                  workers: int | str = 1, on_progress=None, should_stop=None,
                  pool=None, baseline: bool = True) -> ExperimentResult:
    """Run every named scenario (all of them by default) and return the results.

    A named subset also runs the baseline, unless *baseline* is False: for a
    caller that keeps the baseline's results from an earlier run (the result then
    has no baseline to compare against until the caller adds it).

    *replications*, *run_length* and *warmup* fall back to the model's own
    "simulation" section, exactly like `api.run_model`; run length and warm-up
    fall back per scenario, so an override of either takes effect (#315).

    With *crn* (the default) replication r of EVERY scenario uses seed
    `seed + r`. Streams are per block name (`core/base_process.py` seeds each
    block from `[seed, hash(name)]`), so a block a scenario did not touch draws
    the very same numbers in both runs and the scenarios stay synchronised --
    the comparison then uses the paired-t interval. With crn=False each
    scenario gets a disjoint seed block and the comparison uses Welch's.

    *workers* == "auto" (ROADMAP 1.7 E) decides once, timing the first
    replication of the first scenario, then reuses that same worker count for
    every scenario -- `api.run_model` is never itself handed "auto" here, so
    it never re-probes per scenario.

    `on_progress(done, total)` counts replications across all scenarios;
    `should_stop()` stops between replications and leaves `completed` False on
    the result.

    *pool*, when given, is a `ProcessPoolExecutor` the CALLER owns and reuses
    across many studies (`api.scenario_pool`), so a Windows spawn is paid once
    instead of once per call. Results are unchanged either way.

    Since PHASE4 T3 the unit of parallel work is (scenario, replication), not
    the replications of one scenario, so a five-scenario set of one replication
    each fills five workers instead of one.
    """
    base = _as_layout(layout)
    if replications is None:
        replications = int(base.get("simulation", {}).get("replications") or 1)
    layouts = _scenario_layouts(base, scenario_names, baseline)
    # Tasks go straight to the pool, so the "model never ends on its own" guard
    # that `run_model` used to apply per scenario is applied here instead --
    # per scenario, each with its OWN simulation.time_limit and warmup unless
    # the caller gave one (#315).
    windows: dict[str, tuple] = {}
    for name, candidate in layouts.items():
        replications, length, warm = _window(candidate, replications, run_length, warmup)
        windows[name] = (length, warm)
    total = len(layouts) * replications
    result = ExperimentResult(crn=crn)

    if workers == "auto":
        # ponytail: this probe replication is thrown away rather than folded
        # into scenario 0's results (that would mean threading a partially-run
        # scenario through api.run_model's own replication loop). Upgrade path:
        # have api.run_model accept a pre-computed first replication if the
        # extra replication ever matters for a real study's compute budget.
        first_layout = next(iter(layouts.values()), None)
        if first_layout is None or (should_stop is not None and should_stop()):
            result.completed = False
            return result
        t0 = time.perf_counter()
        run_length, warmup = next(iter(windows.values()))
        probe = api.run_single_simulation(first_layout, seed=seed, run_len=run_length,
                                          warmup=warmup, should_stop=should_stop)
        elapsed = time.perf_counter() - t0
        if probe is None:
            result.completed = False
            return result
        workers = api.auto_workers(elapsed, max(total - 1, 0))

    def jobs():
        for index, (name, candidate) in enumerate(layouts.items()):
            offset = seed if crn else seed + index * replications
            for r in range(replications):
                yield (name, candidate, offset + r, *windows[name])

    # ONE process pool for the whole call (PHASE4 A4): a fresh pool per scenario
    # cost seconds of interpreter start-up each on Windows spawn. Serial runs
    # (and the "auto" crossover, resolved above) still start no process at all.
    # A caller running MANY studies passes its own `pool` and pays that spawn
    # once for all of them, exactly as `api.run_model` already allows.
    with (nullcontext(pool) if pool is not None else api.scenario_pool(workers)) as pool:
        collected = api.run_jobs(jobs(), pool=pool,
                                 workers=workers if isinstance(workers, int) else 1,
                                 on_progress=on_progress, should_stop=should_stop, total=total)

    for name in layouts:
        pairs = collected.get(name) or []
        if len(pairs) < replications or not pairs:
            result.completed = False        # stopped, or nothing to report
            break
        result.results[name] = concat_runs([kpi for _, kpi in pairs])
        result.seeds[name] = [s for s, _ in pairs]
    return result


def record_scenarios(layout, result: ExperimentResult, *, run_length: float | None = None,
                     warmup: float | None = None) -> dict:
    """`{scenario: TraceRecorder}`: the first replication of every scenario in
    *result*, recorded (PHASE7 7.7, D200).

    `run_scenarios` records nothing: its replications may run in a process pool,
    and a recorder cannot come back from a child. A replication is a function of
    its layout and its seed, so each first one is run AGAIN here, in this process,
    with that seed and a recorder: it is the replication whose numbers *result*
    holds. Pass the *run_length* and *warmup* the study was run with (None: each
    scenario's own settings, as in `run_scenarios`). With common random numbers
    the scenarios share their arrivals, so the recordings compare frame by frame.

    *layout* must be the model the study ran on: a scenario it no longer has is a
    ModelValidationError.
    """
    from nagasim.core.trace import TraceRecorder

    layouts = _scenario_layouts(_as_layout(layout), list(result.results))
    recordings = {}
    for name in result.results:
        _replications, length, warm = _window(layouts[name], 1, run_length, warmup)
        recorder = TraceRecorder()
        api.run_single_simulation(layouts[name], seed=result.seeds[name][0], run_len=length,
                                  warmup=warm, subscribers=(recorder,))
        recordings[name] = recorder
    return recordings


# ---------------------------------------------------------------- precision
def _merge(a: dict, b: dict) -> dict:
    return {key: pd.concat([a[key], b[key]], ignore_index=True) for key in a}


def run_until_precision(layout, kpi: str, *, relative: float | None = 0.05,
                        absolute: float | None = None, confidence: float = 0.95,
                        n0: int = 10, max_replications: int = 200, seed: int = 0,
                        run_length: float | None = None, warmup: float | None = None,
                        workers: int = 1, on_progress=None, should_stop=None):
    """Keep adding replications until the CI half-width on *kpi* is small enough.

    *kpi* is a `flatten_kpis` key (e.g. "system/Avg Throughput Time"). The
    target half-width is *absolute* if given, else *relative* x |mean|. After
    the first n0 replications the needed count is the textbook
    n >= (t * s / h)^2 from the current sample; each step runs at most as many
    replications again as have run so far (never more than doubling), so a
    wildly optimistic first estimate cannot blow the budget in one go.

    Returns (results, report); report carries the replications used, the
    achieved half-width, the target and `met`.
    """
    base = _as_layout(layout)
    sim = base.get("simulation", {})
    if run_length is None:
        run_length = sim.get("time_limit") or 0.0
    if warmup is None:
        warmup = sim.get("warmup") or 0.0

    max_replications = max(2, int(max_replications))
    n = max(2, min(int(n0), max_replications))
    results = api.run_model(base, replications=n, seed=seed, run_length=run_length,
                            warmup=warmup, workers=workers, on_progress=on_progress,
                            should_stop=should_stop)

    half = target = mean = float("nan")
    while results is not None:
        values = _series(flatten_kpis(results), kpi)
        if values.size == 0:
            raise ValueError(f"unknown KPI: {kpi!r}")
        stat = ci95(values, confidence)
        mean, half = stat["mean"], stat["ub"] - stat["mean"]
        target = float(absolute) if absolute is not None else float(relative or 0.0) * abs(mean)
        if half <= target or n >= max_replications or target <= 0:
            break
        if should_stop is not None and should_stop():
            break
        # (t * s / h)^2, capped at doubling and at the replication budget
        t = float(sp_stats.t.ppf(0.5 + confidence / 2.0, n - 1))
        needed = int(math.ceil((t * stat["sd"] / target) ** 2))
        nxt = min(max(needed, n + 1), 2 * n, max_replications)
        extra = api.run_model(base, replications=nxt - n, seed=seed + n, run_length=run_length,
                              warmup=warmup, workers=workers, on_progress=on_progress,
                              should_stop=should_stop)
        if extra is None:
            break
        results = _merge(results, extra)
        n = nxt

    report = {"kpi": kpi, "replications": n, "mean": mean, "half_width": half,
              "target": target, "confidence": confidence,
              "met": bool(np.isfinite(half) and np.isfinite(target) and half <= target)}
    return results, report


# ---------------------------------------------------------------- warm-up (MSER-5)
@dataclass
class WarmupSuggestion:
    """`suggest_warmup`'s answer: a truncation time in model time units, the
    MSER curve it was picked from, and whether that pick can be trusted."""

    warmup: float
    curve: np.ndarray
    reliable: bool
    index: int = 0          # observation index the truncation time comes from


def _throughput_series(source) -> tuple[np.ndarray, np.ndarray]:
    """(exit times, throughput times) averaged across replications.

    Each replication's completed entities are ordered by exit time, then the
    k-th completion of every replication is averaged with the k-th of the
    others -- the across-replication average MSER-5 is meant to be run on.
    Replications are truncated to the shortest one so every averaged point has
    the same number of contributors.
    """
    log_df = source["entity_log"] if isinstance(source, dict) else source
    if log_df is None or len(log_df) == 0:
        return np.array([]), np.array([])
    df = log_df.dropna(subset=["Exited At", "Throughput Time"])
    if "Run" not in df.columns:
        df = df.assign(Run=0)
    runs = [grp.sort_values("Exited At") for _, grp in df.groupby("Run", sort=True)]
    runs = [g for g in runs if len(g)]
    if not runs:
        return np.array([]), np.array([])
    n = min(len(g) for g in runs)
    times = np.mean([g["Exited At"].to_numpy(dtype=float)[:n] for g in runs], axis=0)
    values = np.mean([g["Throughput Time"].to_numpy(dtype=float)[:n] for g in runs], axis=0)
    return times, values


def _mser(values: np.ndarray, batch: int) -> tuple[int, np.ndarray, bool]:
    """MSER-d on a series; returns (batches to drop, curve, reliable)."""
    k = values.size // batch
    if k < 2:
        return 0, np.array([]), False
    means = values[:k * batch].reshape(k, batch).mean(axis=1)
    curve = np.full(k, np.inf)
    for d in range(k - 1):          # at least two batch means must survive
        tail = means[d:]
        curve[d] = float(((tail - tail.mean()) ** 2).sum()) / (tail.size ** 2)
    best = int(np.argmin(curve))
    return best, curve, bool(best < k / 2)


def suggest_warmup(source, *, batch: int = 5) -> WarmupSuggestion:
    """MSER-5 warm-up suggestion from a run's entity log.

    MSER-5 (White 1997) groups the output series into batches of `batch`
    observations and picks the truncation point d that minimises

        MSER(d) = sum_{i>d} (m_i - mean_{i>d})^2 / (k - d)^2

    over the batch means m_1..m_k: the amount of head to drop that leaves the
    most precise estimate of the steady-state mean. The series used here is the
    entity throughput time ordered by exit time and averaged across
    replications (`_throughput_series`), and the returned `warmup` is the exit
    time of the first observation that survives the truncation -- a time in
    model time units, ready for the run dialog's warm-up field.

    `reliable` is False when the minimum falls in the SECOND half of the batch
    means. That is the standard MSER caveat: with few batches left the
    denominator (k - d)^2 dominates, so a still-drifting series always "looks"
    best when truncated near the end. Treat such a suggestion as "the run is
    too short to tell", not as a warm-up.

    *source* is either a results dict from `api.run_model` or an entity-log
    frame with the columns Run / Exited At / Throughput Time.
    """
    times, values = _throughput_series(source)
    best, curve, reliable = _mser(values, batch)
    index = best * batch
    warmup = float(times[index]) if index < times.size and best > 0 else 0.0
    return WarmupSuggestion(warmup=warmup, curve=curve, reliable=reliable, index=index)


# ---------------------------------------------------------------- CLI (ROADMAP 1.7 C)
def _workers_type(value: str):
    return value if value == "auto" else int(value)


def main(argv=None) -> int:
    """`naga scenarios model.json ...`: run named scenarios headless and
    print/export the comparison table. Dispatched from `nagasim.cli`
    without importing Qt."""
    setup_cli()

    parser = argparse.ArgumentParser(prog="naga scenarios",
                                     description="Run named scenarios headless and compare them.")
    parser.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    parser.add_argument("--scenario", action="append", dest="scenarios", default=None,
                        help="scenario name to run (default: all); may repeat")
    parser.add_argument("--replications", type=int, default=None,
                        help="default: the model's own 'simulation.replications'")
    parser.add_argument("--run-length", type=float, default=None,
                        help="default: the model's own 'simulation.time_limit'")
    parser.add_argument("--warmup", type=float, default=None,
                        help="default: the model's own 'simulation.warmup'")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=_workers_type, default=1,
                        help="replications to run in parallel processes, or 'auto' (default: 1)")
    parser.add_argument("--no-crn", action="store_true",
                        help="disjoint seeds per scenario (Welch's interval) instead of paired CRN")
    parser.add_argument("--out", help="write the comparison table here (.xlsx or .csv)")
    parser.add_argument("--report", help="write an HTML report here")
    parser.add_argument("--player", metavar="PATH",
                        help="write a shareable HTML player here: the first replication of "
                             "every scenario, run again with its seed and recorded, in one "
                             "self-contained file with a scenario switch")
    args = parser.parse_args(argv)

    result = run_scenarios(args.model, args.scenarios, replications=args.replications,
                           seed=args.seed, run_length=args.run_length, warmup=args.warmup,
                           crn=not args.no_crn, workers=args.workers)
    comparison = result.comparison() if result.results else None
    if comparison is not None:
        print(comparison.to_string(index=False))
    else:
        print("no scenarios completed")

    if args.out:
        out = Path(args.out)
        if comparison is None:
            print("nothing to write")
        elif out.suffix.lower() == ".csv":
            comparison.to_csv(out, index=False)
            print(f"Comparison written to {out}")
        else:
            from nagasim import investment          # it imports this module
            layout = api.load_layout(args.model)
            with pd.ExcelWriter(out) as writer:
                comparison.to_excel(writer, index=False, sheet_name="Comparison")
                # D172: only for a model with the section whose baseline was run.
                if (layout.get("investment") is not None and
                        (investment.investment_settings(layout)["baseline"] or BASELINE)
                        in result.results):
                    investment.investment_case(result, layout).to_excel(
                        writer, index=False, sheet_name="Investment")
            print(f"Comparison written to {out}")

    if args.report:
        from nagasim.report import build_report
        base_layout = api.load_layout(args.model)
        base_results = result.results.get(BASELINE) or next(iter(result.results.values()), None)
        if base_results is None:
            print("no scenarios completed; report not written")
        else:
            build_report(base_layout, base_results, experiment=result,
                         title=Path(args.model).stem, path=args.report)
            print(f"Report written to {args.report}")

    if args.player:
        if not result.results:
            print("no scenarios completed; player not written")
        else:
            from nagasim.player import build_player
            layout = api.load_layout(args.model)
            recordings = record_scenarios(layout, result, run_length=args.run_length,
                                          warmup=args.warmup)
            try:
                build_player(layout, recordings, experiment=result, title=Path(args.model).stem,
                             path=args.player)
            except ValueError as exc:       # over the size limit
                print(f"--player: {exc}")
                return 1
            print(f"Player written to {args.player}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
