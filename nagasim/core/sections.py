"""The model file's top-level sections, one row each (ARCH-REVIEW B1).

The section list used to be hand-kept in `schema.migrate`, `ModelDocument`,
`submodels` and `experiments`, and the copies drifted. They all read this table
now, and so do `schema.validate_layout_structured` (the `validate` hooks) and
`modelschema`'s top-level properties. A new section is one row here (with its
`validate` hook), its JSON Schema `$def`, and the module that reads it.
A client that lists model data (the editor's Model data dock) reads `tab`
from here too, and keeps only its own widget per heading (D185).
Stdlib only: every layer imports it.

Row order is the saved file's key order (`ModelDocument.to_layout`), and so
also the order `migrate` adds missing sections in and the order of
`experiments.list_override_paths`.
"""
from __future__ import annotations

from typing import NamedTuple

__all__ = ["Section", "SECTIONS", "FAMILIES", "MISSING", "RECORD_SECTIONS", "CLONABLE_SECTIONS",
           "CONTINUOUS_SECTIONS", "DOCUMENT_LISTS", "KEY", "CAPABILITIES", "capabilities"]

MISSING = object()      # "migrate adds nothing for this section"


class Section(NamedTuple):
    name: str
    # What `migrate` fills in when a file lacks the section (deep-copied; never
    # mutate it here). MISSING: nothing is added.
    default: object = MISSING
    # The field naming one entry: "name", or "id" for orders.
    key: str | None = None
    # Singular name. Set = ModelDocument keeps the list as a plain attribute
    # and has set_<name>/upsert_<item>/remove_<item>/rename_<item> for it. It
    # is also the KIND of one entry ("level", "agent_type"): the word Find, the
    # outline and a client's navigation name a record by.
    item: str | None = None
    # A list of NAME-addressable records (submodels, libraries, the document).
    # Excludes blocks/links (structural), orders (ID-addressed) and the free
    # collections (scenarios, annotations, the twin sections).
    record: bool = False
    # Cloned per submodel instance like a resource (submodels item 7/D72).
    clonable: bool = False
    # ... and a cloned TEMPLATE leaves the flattened layout (item 9): a level
    # left at the top level would be a second live copy.
    continuous: bool = False
    # Entries addressable by key in a scenario/experiment override path
    # ("resources.Clerk.capacity"); links use "<source>-><target>".
    overridable: bool = False
    # A resolved data binding is written back through the document's setter
    # (`agent_tools.data._apply_override`); only an overridable section can be.
    bindable: bool = False
    # One line for the generated reference (tools/gen_docs.py, schema.md) and
    # the generated setter's docstring.
    doc: str = ""
    # "module:function" called as function(layout, problems) by
    # `schema.validate_layout_structured`, in row order, after the validators
    # it still wires by hand (those need another section's names or run in a
    # fixed order). A dotted name, imported at call time: no import cycles.
    validate: str | None = None
    # The heading a client lists this section's entries under: an editor's
    # tab, a report's chapter. Sections may share one (levels, flows,
    # auxiliaries and delays are all "Stocks"). None: not listed as model data
    # (structure, run settings, view state). Only the name lives here; the
    # order of headings and what draws one are the client's (D202).
    tab: str | None = None
    # The family `tab` belongs to (one of `FAMILIES`, set from `_FAMILY_OF_TAB`
    # below), for a client that orders or folds headings by family (C13); ""
    # where `tab` is not set.
    group: str = ""


def _record(name, item, doc, **flags) -> Section:
    return Section(name, [], "name", item, record=True, doc=doc, **flags)


_LAYOUT = "nagasim.core.schema.layout"
_CLONE = {"clonable": True}
_CONT = {"clonable": True, "continuous": True}

SECTIONS: tuple[Section, ...] = (
    # blocks: only the pre-current-version branch of `migrate` rewrites them.
    Section("blocks", key="name", overridable=True,
            doc="Every block in the model: `name`, `type`, `position` (centre, scene units), "
                "`params`."),
    Section("links", [], overridable=True,
            doc="Ordered list of `{source, target, delay?, source_port?, target_port?}`; array "
                "order is branch order, and several links may join the same pair of blocks (a "
                "link is identified by source, target and which of the parallel links it is)."),
    Section("scenarios", [], "name", "scenario",
            doc="Named parameter-override sets, run and compared by `nagasim.experiments`."),
    Section("orders", [], "id", "order", overridable=True, tab="Orders",
            doc='Released jobs with a due date, emitted by a CREATE block in "Orders" '
                'arrival mode.'),
    _record("resources", "resource",
            "Named resources: capacity, queue discipline, optional ranking expression, capacity "
            "schedule, cost fields.", overridable=True, bindable=True, tab="Resources", **_CLONE),
    _record("resource_sets", "resource_set",
            "Named groups of resources with a selection rule, seized as one pseudo-resource.",
            overridable=True, bindable=True, tab="Resource sets", **_CLONE),
    _record("schedules", "schedule",
            "Piecewise-constant arrival-rate or capacity schedules, optionally repeating.",
            overridable=True, bindable=True, tab="Schedules", **_CLONE),
    _record("failures", "failure",
            "Resource failures: uptime/downtime distribution pairs per named resource group.",
            overridable=True, bindable=True, tab="Failures", **_CLONE),
    _record("tables", "table",
            'Named 1-D/2-D lookup tables read by TABLE("name", k1[, k2]) in any expression.',
            overridable=True, bindable=True, tab="Lookup tables"),
    _record("sequences", "sequence",
            "Named routings: ordered steps through STATION blocks, walked by a ROUTE block.",
            overridable=True, tab="Sequences"),
    _record("variables", "variable",
            "Model-level variables, read and written by expressions; reset to `initial` each "
            "replication.", overridable=True, bindable=True, tab="Variables", **_CLONE),
    # NOT overridable (D145): the only numeric field is holding_cost, which
    # feeds cost KPIs only, never the dynamics -- a what-if is a post-hoc
    # rescale (total holding cost = holding_cost x time-averaged WIP), not a
    # rerun.
    _record("entities", "entity", "Entity types: shape, colour, optional WIP holding cost.",
            tab="Entities"),
    _record("timeseries_inputs", "timeseries_input",
            'Named time-series profiles read by TS("name") in any expression.',
            overridable=True, tab="Time series"),
    _record("levels", "level", "Continuous quantities with bounds (battery energy, tank "
            'volume, an inventory position), read by LEVEL("name").', overridable=True,
            tab="Stocks", **_CONT),
    _record("flows", "flow", "Rate-based transfers between levels, re-evaluated at "
            "events.", overridable=True, tab="Stocks", **_CONT),
    # overridable (D145): yields and curves change the dynamics, so a scenario,
    # a DOE factor or an optimizer parameter may vary them. Entity types stay
    # NOT overridable (see below): their only numeric field, holding_cost,
    # never feeds the dynamics.
    _record("conversions", "conversion", "Delayed conversions with a yield curve: "
            "feed in, product released over the following hours or days.",
            overridable=True, tab="Conversions", **_CONT),
    _record("fixed_costs", "fixed_cost", "Costs of the scenario rather than of the run: "
            "capex (flagged `capex: true` for the investment case), or an annuity over a "
            "sized parameter.", overridable=True, tab="Costs", **_CONT),
    _record("fleets", "fleet", "Pools of vehicles that carry entities on the network; a "
            "fleet is a resource under its own name.", overridable=True, tab="Fleets",
            # The rest of the section is validated by hand (it needs the nodes
            # and schedule kinds); this hook is a fleet's arm and station tending.
            validate="nagasim.core.tending:validate_tending", **_CONT),
    _record("motion_profiles", "motion_profile", "How robots, people and mechanisms move: "
            "speed, acceleration, payload derating and a collaborative speed, named by a "
            "MOTION block's `profile`.", overridable=True, tab="Motion profiles",
            validate="nagasim.core.motion:validate_motion", **_CLONE),
    _record("chargers", "charger", "Chargers and battery-swap stations at network nodes, used "
            "by fleets that declare a `battery`.", overridable=True, tab="Chargers",
            validate="nagasim.core.chargers:validate_chargers", **_CONT),
    # auxiliaries/delays overridable: a DOE factor on an SD model has no block,
    # "auxiliaries.k.expr" IS the parameter (a number there stringifies fine).
    _record("auxiliaries", "auxiliary", 'Named expressions read by AUX("name"), evaluated on '
            'read.', overridable=True, tab="Stocks", **_CONT),
    _record("delays", "delay", 'Delays read by DELAY("name"): input, delay time, kind and '
            'order.', overridable=True, tab="Stocks", **_CONT),
    _record("subscripts", "subscript", "Dimensions: a record with \"dims\" is flattened into one "
            "record per element."),
    _record("agent_types", "agent_type", "Statecharts run for every agent of a population: "
            "parameters, states, transitions.", overridable=True, tab="Agents", **_CONT),
    _record("populations", "population", "Agent populations: a type, a count and an initial "
            "placement.", overridable=True, tab="Agents", **_CONT),
    _record("reality_checks", "reality_check", "Vensim-style reality checks: a test input and a "
            "constraint the model must then satisfy."),
    Section("network", None, doc="Optional path network: nodes at layout coordinates and "
                                 "the segments joining them."),
    Section("annotations", [],
            doc="Text notes and frames drawn behind blocks on the canvas; ignored by the engine.",
            validate=f"{_LAYOUT}:_validate_annotations"),
    Section("submodels", {}, doc="Reusable definitions (parameters, inner blocks/links/ports), "
                                 "flattened before a run."),
    Section("background", None, doc="Optional floor-plan image: path, position, scale, opacity, "
                                    "lock, real-world scale.",
            validate=f"{_LAYOUT}:_validate_background"),
    Section("info", {}, doc="Optional model metadata: description, author, notes; shown in the "
                            "report header."),
    Section("data_sources", [], "name", "data_source", tab="Data sources",
            doc="Declared CSV/Excel/SQLite/SQL/JSON sources model parameters can be read from.",
            # Validates data_bindings too: a binding names a declared source.
            validate="nagasim.core.schema.sections_validation:_validate_data"),
    Section("data_bindings", [], "name", "data_binding", tab="Bindings",
            doc="Which column, filtered and transformed how, feeds which override path."),
    Section("initial_state", {}, doc="Warm start: entities already in blocks, variable values "
                                     "and resource states the run begins with.",
            tab="Initial state",
            validate=f"{_LAYOUT}:_validate_initial_state"),
    # Sub-field defaults (time_unit, currency, energy_price) stay in migrate:
    # they apply to a file that HAS a simulation section too.
    Section("simulation", {"time_limit": None, "warmup": 0, "replications": 1},
            doc="Run settings: time_limit, warmup, replications, time_unit, currency, "
                "entity_log_limit, and the optional real-date calendar.",
            # time_unit only: the calendar and energy_price checks stay wired by
            # hand, first, since other sections' checks read their results.
            validate=f"{_LAYOUT}:_validate_simulation"),
    # Read by the top-level `nagasim.investment` only (post-processing over a
    # scenario run, D172); its hook is the one row naming a module above core/.
    Section("investment", doc="Optional investment case: the baseline scenario, horizon, "
                              "discount rate, operating hours per year, margin per part and "
                              "throughput KPI that `naga invest` compares every scenario with. "
                              "Absent by default.",
            tab="Investment", validate="nagasim.investment:validate_investment"),
    Section("outputs", doc="Optional extra output collection whose cost is opt-in, e.g. "
                           "`bottleneck` for the Roser active-period analyser. "
                           "Absent, none of it runs."),
    Section("assertions", [], doc="Model assertions: a KPI, a comparison "
                                  "operator/value and a replication/confidence budget, checked "
                                  "by `naga check`."),
    # D722: its own section, not keys of reality_checks (those constrain sampled values
    # over time; a path is per entity). No default: absent, a file reads and saves as before.
    Section("path_checks", doc="Entity-path checks: rules on the blocks each entity visits "
                               "(order, never, at most N times, always), checked by "
                               "`naga check` on one traced replication.",
            validate="nagasim.core.schema.sections_validation:_validate_path_checks"),
    Section("dashboard", doc="Dashboard widgets: kpi/gauge/line/bar over the "
                             "model's own expressions, sampled at the same instants as the "
                             "timeseries sampler.",
            tab="Dashboard"),
    Section("view", doc="Editor view state: the canvas tab in front, layout/3D, "
                        "zoom and centre per tab; written on save, ignored by the engine."),
)

# The family of each heading (C13, Matteo 2026-10-16): Flow, Data, Logistics,
# Dynamics, Money. Kept per heading, not per row, so sections sharing a heading
# can never disagree; the order and colour of the families are the client's.
FAMILIES = ("Flow", "Data", "Logistics", "Dynamics", "Money")
_FAMILY_OF_TAB = {
    "Resources": "Flow", "Resource sets": "Flow", "Entities": "Flow", "Schedules": "Flow",
    "Failures": "Flow", "Sequences": "Flow", "Orders": "Flow",
    "Variables": "Data", "Lookup tables": "Data", "Time series": "Data",
    "Data sources": "Data", "Bindings": "Data", "Initial state": "Data", "Dashboard": "Data",
    "Fleets": "Logistics", "Chargers": "Logistics", "Motion profiles": "Logistics",
    "Stocks": "Dynamics", "Conversions": "Dynamics", "Agents": "Dynamics",
    "Costs": "Money", "Investment": "Money",
}
SECTIONS = tuple(s._replace(group=_FAMILY_OF_TAB[s.tab]) if s.tab else s for s in SECTIONS)

KEY = {s.name: s.key for s in SECTIONS if s.key}
RECORD_SECTIONS = tuple(s.name for s in SECTIONS if s.record)
CLONABLE_SECTIONS = tuple(s.name for s in SECTIONS if s.clonable)
CONTINUOUS_SECTIONS = tuple(s.name for s in SECTIONS if s.continuous)
DOCUMENT_LISTS = tuple(s for s in SECTIONS if s.item)


# U2 (D151): what a model uses, derived from which sections hold something --
# the editor shows the tabs, docks and menu items of these capabilities only.
# Never stored in the file: a capability exists once its first record does.
# name -> (one-line description, sections whose content implies it). The
# first section listed that has a `tab` is where a client starts editing the
# capability (D204), so the order of a tuple matters.
CAPABILITIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "process": ("Blocks that entities flow through: arrivals, queues, resources.", ()),
    "system_dynamics": ("Stocks, flows, auxiliaries and delays on a continuous clock.",
                        ("levels", "flows", "auxiliaries", "delays", "conversions")),
    "agents": ("Agent types with statecharts, and their populations.",
               ("agent_types", "populations")),
    "data": ("Data sources, bindings, time series and a warm start: the digital twin.",
             ("data_sources", "data_bindings", "timeseries_inputs", "initial_state")),
    "logistics": ("A path network and vehicle fleets, edited in the layout view.",
                  ("network", "fleets")),
    "planning": ("Orders with due dates and routings through stations.",
                 ("orders", "sequences")),
}


def capabilities(layout: dict) -> set[str]:
    """The capabilities *layout* uses. "process" means a block that is not
    just a SUBMODEL wrapper around blockless records (an SD or agent asset),
    so a whole-model SD asset reads as system dynamics only."""
    found = {name for name, (_doc, owned) in CAPABILITIES.items()
             if any(layout.get(section) for section in owned)}
    if (layout.get("simulation") or {}).get("sd"):
        found.add("system_dynamics")
    # A definition holds blocks, links and annotations only (document.py
    # `_submodels_layout`): the record sections above are top-level.
    definitions = [d for d in (layout.get("submodels") or {}).values() if isinstance(d, dict)]
    if any(b.get("type") != "SUBMODEL" for scope in (layout, *definitions)
           for b in scope.get("blocks") or []):
        found.add("process")
    return found
