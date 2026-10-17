"""Submodel definitions and flattening (ROADMAP 2.3 part B, schema v12).

A submodel is a reusable piece of model stored ONCE, under the layout's
top-level "submodels" section, and dropped on a canvas as many times as wanted
through a SUBMODEL block ("an instance"). The engine never hears about either:
`flatten()` rewrites a layout with instances into an ordinary flat layout, and
`GraphModel.load_from_json` calls it before it builds any process. A submodel
is therefore an EDITING concept with exactly zero run-time cost and no new
semantics to reason about when a run misbehaves.

Shape of the section (schema v12)::

    "submodels": {
      "Cell": {
        "info": {"description": "..."},
        "parameters": [{"name": "rate", "default": 1.0, "description": "..."}],
        "blocks": [...], "links": [...], "annotations": [...]
      }
    }

Two block types exist only inside a definition: SUB_ENTRY (`port`, exactly one
outgoing link, no incoming one) and SUB_EXIT (`port`, incoming links only).
A definition may contain any other block, CREATE and DISPOSE included (so it
can be a self-contained source or sink), and instances of OTHER definitions
(cycles among definitions are a validation error).


Flattening rules, as built
--------------------------

1. NAMES. Every block of an expanded instance is renamed `<instance>/<inner>`;
   nesting composes, so an instance `b` inside instance `a` gives `a/b/inner`.
   Top-level blocks keep their names exactly. KPIs, traces, user statistics and
   the Problems dock all use these flattened names.

2. LINKS. A link may carry `"source_port"` / `"target_port"`; each defaults to
   the definition's single port on that side. An outer link into an instance is
   rewired to whatever the matching SUB_ENTRY points at, and an outer link out
   of an instance is rewired from whatever points at the matching SUB_EXIT --
   both transitively, so a definition that only forwards (entry straight to
   exit) disappears completely. SUB_ENTRY and SUB_EXIT blocks never survive.
   A SUB_ENTRY fed by several outer links and a SUB_EXIT reached from several
   inner blocks both work: every (real source, real target) pair is emitted.

3. LINK DELAYS. A rewired hop keeps the FIRST delay found along the path it
   replaced -- the outer link's own delay when it has one, otherwise the delay
   of the inner hop that carried it.

4. BRANCH ORDER. Links are emitted in layout order, and each rewiring expands
   in port order, so a source's branch indices are preserved; among an
   instance's outgoing links the order is per output port.

5. PARAMETERS. An instance's `arguments` merged over the definition's
   `parameters` defaults become each expanded block's `"locals"` section, which
   `BaseProcess.expression_scope` feeds to the expression engine. An argument
   given as a string is an EXPRESSION, evaluated ONCE here in the scope of the
   enclosing instance's own locals (so at the top level only numeric expressions
   work -- there is no entity, no clock and no context at flatten time).

6. `$` SCOPING. Resources, resource sets, schedules, levels, stores, signals
   and user statistics named inside a definition are GLOBAL by default: every instance
   shares them. A name written with a leading `$` is per-instance instead:
   flattening rewrites `$Machine` to `<instance>/Machine`, everywhere it can
   appear -- a bare param value (`resource_name`, `signal_name`, `stat_name`,
   `schedule_name`, ...) and a quoted string literal inside an expression
   (`NQ("$Machine")`). When the `$`-less name is declared at the top level as a
   resource, a resource set or a schedule, that record is CLONED under the
   scoped name and acts as the per-instance template: a cloned resource brings
   its `capacity_schedule` (cloned too), a cloned set brings its members (each
   cloned as a resource in turn). `schema.validate_model` checks the template
   exists. Stores and signals have no top-level record, so `$hold_1` is simply
   the per-instance name -- which is what a store wants anyway, since it is
   registered under the block's own flattened name.

   A LEVEL (PHASE4 D1) clones the same way, and brings its FLOWS with it:
   every top-level flow whose `from`/`to` names the template level is cloned
   per instance with that endpoint rewritten, exactly as a failure follows its
   resource. That is what lets an asset library package a "Battery" -- one
   level, its charge/discharge flows and their parameters -- as a submodel
   definition with `$Battery` inside it.

   A failure declared at the top level against a template resource is cloned
   onto each per-instance copy too: its `resources` list is rewritten to name
   the clone instead of the template, under a per-instance failure name --
   which also gives each instance's failure its own rng stream (the engine
   seeds a failure's rng from `name + resource_name`, both distinct per clone).

7. SCOPED OVERRIDES. A definition may declare `"scoped": {"resources":
   {"Machine": {"capacity": "servers"}}, "schedules": {...}, "failures": {...}}`
   -- per-field overrides applied to a CLONED `$` record at flatten time, once
   the record exists (so a definition with no `$Machine` reference anywhere is
   unaffected even if it declares an override for one -- unless it has no
   blocks at all, item 9). Each value is a
   number or an expression string, evaluated ONCE in the instance's own
   locals (the same scope PARAMETERS use); a field the record needs as an
   integer (a resource's `capacity`) is cast to int after evaluation.

   A field NAME may be dotted -- `"uptime.dist_params.rate": "1.0 / mtbf"` --
   which walks into the record instead of replacing a whole nested spec; a
   DIGIT step indexes a list, so `"thresholds.0.value"` is how an inventory
   asset's reorder point becomes a parameter (PHASE4 D4). That
   is what makes a per-instance failure's MTBF/MTTR a parameter (PHASE4 B5)
   without making every string inside a duration spec an expression: a
   distribution's NAME is a literal, only its numbers are computed. Failures
   cloned for a `$` resource (item 6) take their overrides under the
   TEMPLATE failure's name.

8. STRUCTURAL ARGUMENTS (PHASE4 B6, ARCHITECTURE D59). Item 5 binds an
   argument into the EXPRESSION scope, which reaches only what an expression
   reads. A param that is NOT an expression -- a record NAME, an integer
   (`BATCH.batch_size`), a string choice (`distribution`, `action`), a bool --
   takes its value from `{"$arg": "<parameter>"}` instead: a JSON OBJECT, so it
   can never collide with a real string value. Flattening replaces it with the
   instance's argument (or the parameter's default), coerced to the type the
   target's ParamSpec declares. It is legal anywhere a definition block param,
   a `scoped` override value or a cloned `$` record field appears, and an inner
   instance may pass one down (`{"$arg": "pool"}` as an argument of a nested
   SUBMODEL).

   A parameter whose declared `type` is a LITERAL one (`name`, `choice`,
   `string`) is NOT evaluated as an expression: its string value is the word
   itself, which is what lets an argument name a resource. `refers_to`
   (resource/resource_set/schedule/table/sequence/entity) additionally checks at
   flatten time that the record exists.

9. CONTINUOUS ASSETS (PHASE4 D3, ARCHITECTURE D62). A purely continuous asset
   -- a PV array, a grid connection -- has NO blocks, so there is no `$`
   reference anywhere to trigger item 6. A level or flow the definition names
   under `scoped` is cloned for the instance for that reason alone:
   `"scoped": {"flows": {"Nrg PV": {}}}` says "this flow is mine", overrides or
   not. Resources and fleets (D615) follow the same rule, so a block-less
   crew pool or vehicle fleet asset owns its record, failures included.
   A cloned flow carries the instance's parameters as `locals`, exactly as
   an expanded block does, so its `rate`/`price`/`emission_factor` expressions
   read them (`TS(profile) * kwp * pr`) and a `$name` inside one is scoped the
   same way it is inside a block param. Threshold signals and variables scope
   with their level.

   A template level or flow that was cloned LEAVES the flattened layout. A
   template resource is merely idle, but a template level would be a second
   live battery beside the instance's, and its flow's rate reads parameters no
   template has. So: a level or flow reached by `$` or by `scoped` is a
   template, not a live record. A hand-written top-level flow must therefore
   name a level no instance clones.

10. PER-INSTANCE VARIABLES (ARCHITECTURE D72). A VARIABLE clones exactly like a
    resource: `$gate` anywhere inside a definition -- an ASSIGN target, a level
    or flow threshold's `variable`, `VAR("$gate")` inside an expression -- is
    rewritten to `<instance>/gate` and the top-level variable of that name is
    copied under it, with `scoped.variables` available for a per-instance
    `initial`. `VAR("name")` is the expression spelling, because the scoped name
    holds a `/` (and often a space) and so can never be a bare identifier. The
    template is LEFT in the layout, like a resource template: an unwritten
    variable drives nothing, and a hand-written model may read the bare name.

11. BINDING TO A RECORD SOMEONE ELSE OWNS (ARCHITECTURE D73). A literal `$name`
    is MINE -- cloned per instance, item 6. A name that arrives through a `name`
    PARAMETER (`{"$arg": "store"}`) is someone ELSE'S: it is bound exactly as
    given and clones nothing, which is what lets two feeder instances draw from
    one shared level, or a conveyor inside one asset stop against the resource
    inside another. The argument may name another instance's scoped record
    (`<instance>/<record>`), so `refers_to` is checked once the whole layout has
    been expanded -- against the top-level records AND every clone -- instead of
    against the unflattened model, where the clone does not exist yet.

12. PER-INSTANCE AGENTS (PHASE6 C1). A POPULATION clones like a level: reached
    by `$People` anywhere (`COUNT("$People", "I")`, `AGENT("$People", i)`, a
    send's `{"population": "$People"}`, `{"spawn": "$People"}`, a SEND block's
    `to`) or named under `scoped.populations`, it becomes `<instance>/People`
    and brings its AGENT TYPE with it (`<instance>/Person`, unless `type` came
    from an argument, item 11), whose own `$` names are scoped in turn. Both
    templates are consumed, like a level's. `scoped.agent_types.<T>` takes
    `"parameters.<p>"` overrides and `scoped.populations.<P>` a `count`, so an
    agent asset is form-editable. Streams: agent i's key is the population's
    FULL name, so two instances draw independently and a clone equals the same
    model written flat under the scoped names, bit for bit.

    A CHARGER (D167) clones only when named under `scoped.chargers`, and its
    template is consumed like a fleet's; `"node": "$bay"` binds it to the
    instance's own geometry node.

    A MOTION PROFILE (D174) clones like a resource, its template left in
    place: reached by a block's `"profile": "$Arm"` or a template fleet's
    `"arm": "$Arm"`, with `scoped.motion_profiles` for its speeds.

13. INHERITANCE (PHASE6 W7 Q4, ARCHITECTURE D127). A definition may declare
    `"extends": "<definition>"` and `"remove": [block names or "src->tgt"
    link keys]`. Before anything else reads the section, `resolve_extends`
    replaces it by its EFFECTIVE definition: the parent's (itself resolved, so
    chains work) with the child merged in by identity -- blocks and parameters
    by `name` (a child record of the same name takes the parent's place, new
    ones are appended), links by (source, target, source_port, target_port)
    (a child link replaces the same-identity parent link, new ones are appended). `remove` drops parent
    blocks and every parent link touching one; a `"src->tgt"` entry drops the
    parent links between those two blocks. `scoped` merges per section and
    record (the child's override dict wins); every other field (`info`,
    `annotations`, ...) is the child's when it has it, else the parent's. A
    cycle is E_EXTENDS_CYCLE, an unknown parent E_EXTENDS_UNKNOWN; flattening
    refuses either with a SubmodelError. `core/submodels.py` cannot reuse
    `modeldiff`'s identity discovery (that module sits ABOVE core), and a
    definition has only three record lists, so the keys are written here.
"""
from __future__ import annotations

import copy
import re

from nagasim.core import sections
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.expressions import LOOKUP_SECTION, ExpressionError, ExpressionScope, compile_expression
from nagasim.core.problems import Problem, problem
from nagasim.core.quantities import SECTION_FIELDS

__all__ = ["SUBMODEL", "SUB_ENTRY", "SUB_EXIT", "PORT_TYPES", "ARG_KEY", "LITERAL_TYPES",
           "REFERS_SECTIONS", "RECORD_SECTIONS", "LOCALS_SECTIONS", "SubmodelError", "flatten",
           "has_submodels", "definition_ports", "port_block_name", "definition_references",
           "argument_names", "merge_definition", "merge_named", "resolve_extends"]

SUBMODEL = "SUBMODEL"
SUB_ENTRY = "SUB_ENTRY"
SUB_EXIT = "SUB_EXIT"
PORT_TYPES = {"in": SUB_ENTRY, "out": SUB_EXIT}

# A structural argument (item 8): {"$arg": "<parameter>"} as a whole param value.
ARG_KEY = "$arg"
# Parameter types whose string value is the WORD, not an expression.
LITERAL_TYPES = frozenset({"name", "choice", "string"})
# `refers_to` kind -> the layout sections a name of that kind may live in.
REFERS_SECTIONS = {
    "resource": ("resources", "resource_sets"),
    "resource_set": ("resource_sets",),
    "schedule": ("schedules",),
    "level": ("levels",),
    "conversion": ("conversions",),
    # PHASE4 D3: a profile an energy asset reads (TS()), picked by name in the
    # instance form exactly as a schedule or a table is.
    "timeseries_input": LOOKUP_SECTION["TS"],
    "table": ("tables",),
    "sequence": ("sequences",),
    "entity": ("entities",),
    # PHASE4 E2: a station asset picks the fleet that carries work away from it.
    "fleet": ("fleets",),
}

# The record sections (a list of NAME-addressable records), the subset cloned
# per submodel instance (item 7/D72) and its continuous subset whose cloned
# templates leave the flattened layout (item 9): rows of core/sections.py.
RECORD_SECTIONS = sections.RECORD_SECTIONS
_CLONABLE_SECTIONS = sections.CLONABLE_SECTIONS
_CONTINUOUS_SECTIONS = sections.CONTINUOUS_SECTIONS

# A quoted `$name` inside an expression: NQ("$Machine") -> NQ("cell_1/Machine").
_QUOTED_SCOPED = re.compile(r"(['\"])\$([^'\"]*)\1")

# The sections a `$name` clones from, tried in this order (a name in two takes the first).
_BY_REFERENCE = ("populations", "agent_types", "resource_sets", "resources", "schedules",
                 "levels", "fleets", "variables", "auxiliaries", "delays", "motion_profiles")
# The sections a definition's `scoped` entry clones, in this order: the flattened record order.
_SCOPED_ORDER = ("levels", "flows", "fixed_costs", "conversions", "chargers", "variables",
                 "auxiliaries", "delays", "agent_types", "populations")
# Records whose expressions read the instance's parameters by name: a clone carries them as
# `locals` (a level only when its `initial` is an expression). The published schema declares
# `locals` on exactly these and on a block (#694).
LOCALS_SECTIONS = ("levels", "flows", "fixed_costs", "conversions", "auxiliaries", "delays")


def _expression_fields(section: str) -> tuple:
    """(child, field) of every expression field of *section*'s records in the field table,
    child "" for the record's own field ("levels.conveyor" gives ("conveyor", "transit_time"))."""
    return tuple((key.partition(".")[2], spec.name) for key, fields in SECTION_FIELDS.items()
                 if key.partition(".")[0] == section
                 for spec in fields if "expression" in spec.field_kinds)


# From the field table (core/quantities.py), so a new field needs no edit here: the fields
# whose `$` names a clone scopes (#695), and the integer fields a bound argument or a scoped
# override is cast to (#696). A LEVEL's capacity is kWh or tonnes: never cast (DEVLOG #92).
_EXPRESSIONS = {section: _expression_fields(section) for section in _CLONABLE_SECTIONS}
_INT_FIELDS = {key: frozenset(spec.name for spec in fields if spec.type is int)
               for key, fields in SECTION_FIELDS.items()}


_BAD = object()     # "this argument does not fit the target type"


def _coerce(value, target):
    """*value* as the type a ParamSpec declares, or `_BAD`. Only widening that
    loses nothing is done: an int where a float is wanted, an integral float
    where an int is. A bool is never a number and a number is never a string --
    both would hide a real mistake in a model file."""
    if target is None:
        return copy.deepcopy(value)
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if target is bool:
        return value if isinstance(value, bool) else _BAD
    if target is int:
        return int(value) if number and int(value) == value else _BAD
    if target is float:
        return float(value) if number else _BAD
    if target is str:
        return value if isinstance(value, str) else _BAD
    return copy.deepcopy(value) if isinstance(value, target) else _BAD


class SubmodelError(ValueError):
    """Flattening refused: the layout's submodel section is not consistent.

    Carries `problems` (the messages, the long-standing contract) and `items`,
    where a check that has its own permanent code kept its `Problem` -- that is
    what lets `schema.validate_model_structured` report a structural-argument
    fault under its own code instead of the generic `E_SUBMODEL`.
    """

    def __init__(self, problems):
        if isinstance(problems, (str, Problem)):
            problems = [problems]
        self.items = list(problems)
        self.problems = [p.message if isinstance(p, Problem) else str(p) for p in self.items]
        super().__init__("; ".join(self.problems) or "invalid submodel")


# -- small readers ----------------------------------------------------------
def _type(block: dict) -> str:
    return str(block.get("type", "")).upper()


def has_submodels(layout: dict) -> bool:
    """True when this layout needs flattening at all. A stray port block counts
    too, so `validate_model` reports it instead of silently ignoring it."""
    return bool(layout.get("submodels")) or any(
        _type(b) in (SUBMODEL, SUB_ENTRY, SUB_EXIT) for b in layout.get("blocks", []))


def definition_ports(definition: dict, side: str) -> list[str]:
    """Port names of *definition* on "in" (SUB_ENTRY) or "out" (SUB_EXIT),
    in block order."""
    want = PORT_TYPES[side]
    return [str(b.get("params", {}).get("port", "")) for b in definition.get("blocks", [])
            if _type(b) == want]


def port_block_name(definition: dict, side: str, port: str | None) -> str | None:
    """Name of the SUB_ENTRY/SUB_EXIT block serving *port*; with *port* None the
    definition's single port on that side, or None when there is not exactly
    one."""
    want = PORT_TYPES[side]
    candidates = [b for b in definition.get("blocks", []) if _type(b) == want]
    if port is None:
        return candidates[0]["name"] if len(candidates) == 1 else None
    for block in candidates:
        if str(block.get("params", {}).get("port", "")) == port:
            return block["name"]
    return None


def definition_references(definition: dict) -> list[str]:
    """Names of the definitions instantiated inside *definition*."""
    return [str(b.get("params", {}).get("definition", "")) for b in definition.get("blocks", [])
            if _type(b) == SUBMODEL]


def argument_names(value, path: str = "") -> list[tuple[str, str]]:
    """Every `{"$arg": "<name>"}` inside *value*, as (parameter, path) pairs --
    what a validator needs to check the names a definition actually uses."""
    if isinstance(value, dict):
        if len(value) == 1 and ARG_KEY in value:
            return [(str(value[ARG_KEY]), path)]
        found = []
        for key, item in value.items():
            found += argument_names(item, f"{path}/{key}")
        return found
    if isinstance(value, list):
        found = []
        for index, item in enumerate(value):
            found += argument_names(item, f"{path}/{index}")
        return found
    return []


def _param_type(block_type: str, key: str):
    """The Python type a block's ParamSpec declares for *key*, or None when the
    block type or the param is unknown (a nested value, a plugin's own key)."""
    spec = BLOCK_REGISTRY.get(block_type)
    if spec is None:
        return None
    for param in spec.params:
        if param.name == key:
            return param.type
    return None


# -- inheritance (item 13) --------------------------------------------------
def merge_named(parent: list, child: list, remove=()) -> list:
    """Records by `name`: parent order, a same-name child record in the
    parent's place, the child's new records appended; parent records named in
    *remove* are dropped (a child record of that name is still added)."""
    mine = {r.get("name"): r for r in child or [] if isinstance(r, dict)}
    out = []
    for record in parent or []:
        name = record.get("name") if isinstance(record, dict) else None
        if name in mine:
            out.append(copy.deepcopy(mine.pop(name)))
        elif name not in remove:
            out.append(copy.deepcopy(record))
    return out + [copy.deepcopy(r) for r in child or []
                  if isinstance(r, dict) and r.get("name") in mine]


def _link_key(link: dict) -> tuple:
    return tuple(link.get(k) for k in ("source", "target", "source_port", "target_port"))


def merge_definition(parent: dict, child: dict) -> dict:
    """The effective definition of *child* extending *parent* (item 13). A
    `remove` entry is a block name, or a link key `"source->target"` that
    drops the parent's links between those two blocks, whatever their ports
    (DEVLOG #220: how a child inserts a step between two kept blocks)."""
    remove = {str(n) for n in child.get("remove") or []}
    removed_links = {tuple(n.split("->", 1)) for n in remove if "->" in n}
    merged_keys = ("blocks", "links", "parameters", "scoped", "extends", "remove")
    out = copy.deepcopy({k: v for k, v in parent.items() if k not in merged_keys})
    out.update(copy.deepcopy({k: v for k, v in child.items() if k not in merged_keys}))
    out["parameters"] = merge_named(parent.get("parameters"), child.get("parameters"))
    out["blocks"] = merge_named(parent.get("blocks"), child.get("blocks"), remove)
    gone = remove - {b.get("name") for b in child.get("blocks") or [] if isinstance(b, dict)}
    child_links = [l for l in child.get("links") or [] if isinstance(l, dict)]
    replaced = {_link_key(l) for l in child_links}
    out["links"] = [copy.deepcopy(l) for l in parent.get("links") or []
                    if isinstance(l, dict) and _link_key(l) not in replaced
                    and not {l.get("source"), l.get("target")} & gone
                    and (l.get("source"), l.get("target")) not in removed_links] \
        + copy.deepcopy(child_links)
    scoped = copy.deepcopy(parent.get("scoped") or {})
    for section, records in (child.get("scoped") or {}).items():
        scoped[section] = {**(scoped.get(section) or {}), **copy.deepcopy(records)}
    if scoped:
        out["scoped"] = scoped
    return out


def resolve_extends(definitions: dict, *, path: str = "/submodels",
                    merge=merge_definition) -> tuple[dict, list]:
    """(effective definitions, Problems): every definition with `extends`
    replaced by its merged form (item 13). One whose chain has a cycle or an
    unknown parent is left as written and reported. With no `extends` anywhere
    the input dict itself comes back, so an existing model pays nothing.
    `libraries.py` passes its own *merge* to resolve asset inheritance with the
    same chain rules."""
    if not any(isinstance(d, dict) and d.get("extends") is not None
               for d in definitions.values()):
        return definitions, []
    out, problems, reported = {}, [], set()
    for name, definition in definitions.items():
        if not isinstance(definition, dict):
            out[name] = definition      # malformed: validation reports it (DEVLOG #222)
            continue
        chain = [name]
        while True:
            parent = definitions[chain[-1]].get("extends")
            if parent is None:
                break
            if not isinstance(parent, str) or not isinstance(definitions.get(parent), dict):
                if chain[-1] not in reported:
                    reported.add(chain[-1])
                    problems.append(problem(
                        "E_EXTENDS_UNKNOWN",
                        f"'{chain[-1]}' extends '{parent}', which does not exist",
                        f"{path}/{chain[-1]}/extends",
                        hint=f"extend one of: {', '.join(sorted(definitions)) or '(none)'}"))
                chain = None
                break
            if parent in chain:
                loop = chain[chain.index(parent):] + [parent]
                if frozenset(loop) not in reported:
                    reported.add(frozenset(loop))
                    problems.append(problem(
                        "E_EXTENDS_CYCLE", f"extends cycle: {' -> '.join(loop)}",
                        f"{path}/{parent}/extends",
                        hint="a definition cannot extend itself, directly or through another"))
                chain = None
                break
            chain.append(parent)
        if chain is None:
            out[name] = definition
            continue
        effective = definitions[chain[-1]]
        for child in reversed(chain[:-1]):
            effective = merge(effective, definitions[child])
        out[name] = effective
    return out, problems


# -- flattening -------------------------------------------------------------
def flatten(layout: dict) -> dict:
    """Return a layout with every SUBMODEL instance expanded and the
    "submodels" section removed. A layout with no submodels is returned
    unchanged (identity, same object) so an existing model pays nothing.

    Raises SubmodelError naming the offending definition/block when the section
    cannot be expanded; `schema.validate_model` reports the same problems
    without raising.
    """
    if not has_submodels(layout):
        return layout
    return _Flattener(layout).run()


class _Flattener:
    def __init__(self, layout: dict):
        self.layout = layout
        self.definitions, extends_problems = resolve_extends(layout.get("submodels") or {})
        # Every clonable section's template records, by name: what a `$name`
        # or a `scoped` entry clones per instance (item 7, D72).
        self.templates = {section: {r.get("name"): r for r in layout.get(section) or []}
                          for section in _CLONABLE_SECTIONS}
        # Names a `refers_to` argument may point at (item 8), by section.
        self.declared = {section: {str(r.get("name", "")) for r in layout.get(section, []) or []
                                   if isinstance(r, dict)}
                         for section in {s for sections in REFERS_SECTIONS.values()
                                         for s in sections}}
        self.blocks: list[dict] = []
        self.links: list[dict] = []
        self.clones: dict[str, dict] = {section: {} for section in _CLONABLE_SECTIONS}
        # Template levels and flows that were cloned per instance: they leave
        # the flattened layout (item 9). A resource template is idle and
        # harmless; a level template would be a SECOND live battery, and its
        # flow template's rate reads parameters that only an instance has.
        self.consumed: dict[str, set] = {section: set() for section in _CONTINUOUS_SECTIONS}
        # An `extends` fault (item 13) is fatal before anything expands.
        self.problems: list = list(extends_problems)
        # `refers_to` arguments, checked once the whole layout is expanded
        # (item 11, D73): an argument may name another instance's scoped
        # record, which does not exist until that instance has been cloned.
        self._references: list[tuple] = []
        # Per-instance "scoped" field overrides (item 7): flattened instance
        # name -> (its definition's "scoped" dict, its own locals to evaluate
        # expression-valued overrides in). Filled in _expand_instance, read by
        # _apply_scoped when a $ record is cloned for that instance's scope.
        self._scoped: dict[str, tuple[dict, dict]] = {}
        # `<instance>/<name>` of every instance whose asset picture follows a state
        # source: a variable among them is marked `trace` (D608).
        self._state_sources: set[str] = set()

    # -- entry point --------------------------------------------------------
    def run(self) -> dict:
        if self.problems:
            raise SubmodelError(self.problems)
        self._expand(self.layout.get("blocks", []), self.layout.get("links", []),
                     prefix="", scope_locals={}, stack=())
        self._check_references()
        if self.problems:
            raise SubmodelError(self.problems)

        links = self._contract()        # also drops the port blocks from self.blocks
        out = copy.deepcopy(self.layout)
        out.pop("submodels", None)
        out["blocks"] = self.blocks
        out["links"] = links
        templates = self._continuous_templates()
        for section in _CLONABLE_SECTIONS:
            kept = [r for r in out.get(section, [])
                    if str(r.get("name", "")) not in templates.get(section, ())]
            out[section] = kept + list(self.clones[section].values())
        for record in out.get("variables") or []:
            if record.get("name") in self._state_sources:
                record["trace"] = True
        return out

    def _continuous_templates(self) -> dict[str, set]:
        """Level and flow names that are TEMPLATES, not live records (item 9).

        Anything a definition declares under `scoped` counts, cloned or not:
        an asset's flow rate reads that asset's parameters, so a copy left at
        the top level would not even compile once every instance is deleted.
        """
        out = {section: set(names) for section, names in self.consumed.items()}
        for definition in self.definitions.values():
            scoped = definition.get("scoped") or {}
            for section in _CONTINUOUS_SECTIONS:
                out[section].update(name for name in (scoped.get(section) or {})
                                    if name in self.templates[section])
        return out

    # -- phase 1: recursive expansion ---------------------------------------
    def _expand(self, blocks, links, prefix: str, scope_locals: dict, stack: tuple) -> None:
        """Emit every block of one level with its names prefixed, recursing into
        instances; emit every link with instance endpoints redirected to the
        matching port block (contracted away in phase 2)."""
        instances = []
        for block in blocks:
            if _type(block) == SUBMODEL:
                instances.append(block)
                continue
            new = copy.deepcopy(block)
            new["name"] = prefix + block["name"]
            params = self._resolve_arguments(block.get("params", {}), scope_locals,
                                             prefix + block["name"], _type(block))
            new["params"] = self._rewrite(params, prefix)
            if scope_locals:
                new["locals"] = dict(scope_locals)
            self.blocks.append(new)

        by_name = {b["name"]: b for b in blocks}
        for index, link in enumerate(links):
            # A top-level link's fault points at the link itself (DEVLOG
            # #216), so a three-way merge can revert that one record.
            at = f"/links/{index}" if not stack else None
            source = self._endpoint(by_name, prefix, link, "source", at)
            target = self._endpoint(by_name, prefix, link, "target", at)
            if source is None or target is None:
                continue        # the problem is already recorded
            entry = {"source": source, "target": target}
            delay = link.get("delay")
            if delay:
                entry["delay"] = self._rewrite(copy.deepcopy(delay), prefix)
            self.links.append(entry)

        # Instances last, so this level's own blocks and links read before the
        # bodies they call into.
        for block in instances:
            self._expand_instance(block, prefix + block["name"], prefix, scope_locals, stack)

    def _expand_instance(self, block, name, prefix, scope_locals, stack) -> None:
        definition_name = str(block.get("params", {}).get("definition", ""))
        definition = self.definitions.get(definition_name)
        if definition is None:
            self.problems.append(f"{name}: unknown submodel definition '{definition_name}'")
            return
        if definition_name in stack:
            self.problems.append(
                f"{name}: submodel definition cycle: {' -> '.join(stack + (definition_name,))}")
            return
        inner_locals = self._arguments(definition, block, name, scope_locals)
        self._scoped[name] = (definition.get("scoped") or {}, inner_locals)
        source = ((definition.get("info") or {}).get("graphics") or {}).get("state_source")
        if source:
            self._state_sources.add(f"{name}/{source}")
        self._clone_scoped_records(name, blockless=not definition.get("blocks"))
        self._expand(definition.get("blocks", []), definition.get("links", []),
                     prefix=f"{name}/", scope_locals=inner_locals,
                     stack=stack + (definition_name,))

    def _endpoint(self, by_name: dict, prefix: str, link: dict, side: str,
                  at: str | None = None) -> str | None:
        """Flattened name a link end refers to: the block itself, or -- for an
        instance -- the port block inside it that the link's `source_port` /
        `target_port` selects. *at* is the link's JSON pointer at top level."""
        raw = link.get(side)
        block = by_name.get(raw)
        if block is None:
            message = f"link {link.get('source')}->{link.get('target')}: unknown block '{raw}'"
            self.problems.append(message if at is None else problem(
                "E_SUBMODEL", message, at, hint="fix the submodel section, then validate again"))
            return None
        if _type(block) != SUBMODEL:
            return prefix + raw
        definition = self.definitions.get(str(block.get("params", {}).get("definition", "")))
        if definition is None:
            return None     # _expand_instance already reported the unknown definition
        port_side = "out" if side == "source" else "in"
        port = link.get(f"{side}_port")
        inner = port_block_name(definition, port_side, port)
        if inner is None:
            self.problems.append(
                f"link {link.get('source')}->{link.get('target')}: "
                f"{side}_port {port!r} is not a {port_side} port of definition "
                f"'{block['params']['definition']}'")
            return None
        return f"{prefix}{raw}/{inner}"

    # -- phase 2: contract the port blocks ----------------------------------
    def _contract(self) -> list[dict]:
        ports = {b["name"] for b in self.blocks if _type(b) in (SUB_ENTRY, SUB_EXIT)}
        if not ports:
            return self.links
        out_edges: dict[str, list[dict]] = {}
        for link in self.links:
            out_edges.setdefault(link["source"], []).append(link)

        def resolve(node: str, delay, seen: frozenset) -> list[tuple[str, dict | None]]:
            if node not in ports:
                return [(node, delay)]
            if node in seen:        # a cycle through ports alone carries no entity
                return []
            found = []
            for hop in out_edges.get(node, []):
                found.extend(resolve(hop["target"], delay if delay else hop.get("delay"),
                                     seen | {node}))
            return found

        final = []
        for link in self.links:
            if link["source"] in ports:
                continue        # reached through the real source that feeds it
            for target, delay in resolve(link["target"], link.get("delay"), frozenset()):
                entry = {"source": link["source"], "target": target}
                if delay:
                    entry["delay"] = copy.deepcopy(delay)
                final.append(entry)

        self.blocks = [b for b in self.blocks if b["name"] not in ports]
        return final

    # -- parameters ---------------------------------------------------------
    def _arguments(self, definition: dict, block: dict, label: str, outer: dict) -> dict:
        """The instance's arguments merged over the definition's defaults.

        A string argument is an EXPRESSION evaluated once in the ENCLOSING
        instance's locals -- unless the parameter declares a LITERAL type
        (`name`, `choice`, `string`, item 8), where the word itself is the
        value. An argument written as `{"$arg": ...}` is this instance's own
        parameter passed down (nesting).
        """
        given = self._resolve_arguments(block.get("params", {}).get("arguments") or {},
                                        outer, label, None)
        values = {}
        for spec in definition.get("parameters", []):
            name = str(spec.get("name", ""))
            if not name:
                continue
            raw = given[name] if name in given else spec.get("default")
            literal = str(spec.get("type", "")) in LITERAL_TYPES
            if isinstance(raw, str) and not literal:
                try:
                    raw = compile_expression(raw).evaluate(ExpressionScope(locals=outer))
                except ExpressionError as exc:
                    self.problems.append(f"{label}: argument '{name}': {exc}")
                    raw = 0
            values[name] = raw
            self._check_reference(spec, raw, label, name)
        return values

    def _check_reference(self, spec: dict, value, label: str, name: str) -> None:
        """Queue a `refers_to` parameter for checking once the whole layout is
        expanded; a `$`-scoped name is per-instance and checked by
        `_check_scoped_names`."""
        kind = str(spec.get("refers_to", ""))
        if kind not in REFERS_SECTIONS or not isinstance(value, str) or not value \
                or value.startswith("$"):
            return
        self._references.append((kind, value, label, name))

    def _check_references(self) -> None:
        """Every queued `refers_to` argument against the FLATTENED model (item
        11, D73): the top-level records PLUS every clone, so an argument may
        bind to another instance's scoped record (`<instance>/<record>`)."""
        for kind, value, label, name in self._references:
            sections = REFERS_SECTIONS[kind]
            known = {n for section in sections
                     for n in (self.declared[section] | set(self.clones.get(section, ())))}
            if value in known:
                continue
            self.problems.append(problem(
                "E_SUBMODEL_ARG_REFERENCE",
                f"{label}: argument '{name}': no {kind.replace('_', ' ')} named '{value}'",
                block=label,
                hint=f"declare it in the model, or pick one of: "
                     f"{', '.join(sorted(known)) or '(none)'}"))

    # -- structural arguments (item 8) --------------------------------------
    def _resolve_arguments(self, value, locals_: dict, label: str, block_type: str | None,
                           key: str = "", target=None):
        """Deep-copy *value* with every `{"$arg": "<parameter>"}` replaced by
        the instance's argument, coerced to the type the target declares."""
        if isinstance(value, dict):
            if len(value) == 1 and ARG_KEY in value:
                return self._argument(str(value[ARG_KEY]), locals_, label, key, target)
            # Only the TOP level of a block's params has a ParamSpec; deeper
            # keys (inside dist_params) recurse with no target to coerce to.
            return {k: self._resolve_arguments(value[k], locals_, label, None, k,
                                               _param_type(block_type, k) if block_type else None)
                    for k in value}
        if isinstance(value, list):
            return [self._resolve_arguments(v, locals_, label, None, key) for v in value]
        return copy.deepcopy(value)

    def _argument(self, name: str, locals_: dict, label: str, key: str, target):
        where = f"param '{key}'" if key else "an argument"
        if name not in locals_:
            self.problems.append(problem(
                "E_SUBMODEL_ARG_UNKNOWN",
                f"{label}: {where}: $arg '{name}' is not a parameter of this submodel",
                block=label,
                hint=f"declare '{name}' in the definition's parameters, or use one of: "
                     f"{', '.join(sorted(locals_)) or '(none)'}"))
            return 0
        value = locals_[name]
        coerced = _coerce(value, target)
        if coerced is _BAD:
            self.problems.append(problem(
                "E_SUBMODEL_ARG_TYPE",
                f"{label}: {where}: $arg '{name}' is {value!r}, not a "
                f"{getattr(target, '__name__', target)}",
                block=label, hint=f"pass a {getattr(target, '__name__', target)} argument"))
            return 0
        return coerced

    # -- `$` scoping --------------------------------------------------------
    def _rewrite(self, value, prefix: str):
        """Deep-copy *value* with every `$name` rewritten to `<instance>/name`,
        cloning the top-level template record when there is one."""
        scope = prefix.rstrip("/")
        if not scope:
            return copy.deepcopy(value)
        found: set[str] = set()
        rewritten = self._walk(value, scope, found)
        for name in sorted(found):
            self._clone(name, scope)
        return rewritten

    def _walk(self, value, scope: str, found: set):
        if isinstance(value, dict):
            return {k: self._walk(v, scope, found) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, scope, found) for v in value]
        if isinstance(value, str):
            return self._scope_string(value, scope, found)
        return value

    def _scope_string(self, text: str, scope: str, found: set) -> str:
        if "$" not in text:
            return text

        def quoted(match):
            found.add(match.group(2))
            return f"{match.group(1)}{scope}/{match.group(2)}{match.group(1)}"

        out = _QUOTED_SCOPED.sub(quoted, text)
        # A bare param value ("$Machine" as resource_name); a string that still
        # holds quotes is an expression, already handled above.
        if out.startswith("$") and "'" not in out and '"' not in out:
            found.add(out[1:])
            out = f"{scope}/{out[1:]}"
        return out

    def _clone(self, name: str, scope: str) -> None:
        section = next((s for s in _BY_REFERENCE if name in self.templates[s]), None)
        # Anything else (a signal, a user statistic, a store/block name) has no
        # top-level record: the scoped name is all it needs.
        if section is not None:
            self._clone_record(section, name, scope)

    def _clone_record(self, section: str, name: str, scope: str) -> str:
        """A per-instance copy `<scope>/<name>` of the template record *name*
        of *section*, made once: the instance's `scoped` overrides and `$arg`s
        applied, its parameters as `locals` where the section reads them, every
        `$` name in an expression field scoped (and cloned in turn), then the
        section's own step from `_AFTER`. A continuous template is consumed:
        it leaves the flattened layout (item 9)."""
        new_name = f"{scope}/{name}"
        if new_name in self.clones[section]:
            return new_name
        record = copy.deepcopy(self.templates[section][name])
        record["name"] = new_name
        self.clones[section][new_name] = record     # before recursing: cycle-safe
        if section in self.consumed:
            self.consumed[section].add(name)
        bound = self._apply_scoped(record, section, name, scope)
        _scoped_defs, locals_ = self._scoped.get(scope, ({}, {}))
        if locals_ and section in LOCALS_SECTIONS:
            record["locals"] = dict(locals_)
        for child, field in _EXPRESSIONS.get(section, ()):
            holders = record.get(child) if child else [record]
            for holder in holders if isinstance(holders, list) else [holders]:
                if isinstance(holder, dict) and isinstance(holder.get(field), str) \
                        and "$" in holder[field]:
                    holder[field] = self._rewrite(holder[field], f"{scope}/")
        after = self._AFTER.get(section)
        if after is not None:
            after(self, record, name, scope, bound)
        return new_name

    # -- the sections' own clone steps ----------------------------------------
    # A field in `bound` came from an argument: a BINDING to a record someone
    # else owns, never a trigger for a clone of its own (item 11, D73).
    def _after_resource(self, record, name, scope, bound) -> None:
        """A resource, or a fleet (a fleet IS a resource, D68): its schedule,
        a fleet's `$` arm profile (D174) and its failures are the instance's."""
        schedule = record.get("capacity_schedule")
        if schedule and schedule in self.templates["schedules"] \
                and "capacity_schedule" not in bound:
            record["capacity_schedule"] = self._clone_record("schedules", schedule, scope)
        arm = record.get("arm")
        if isinstance(arm, str) and arm.startswith("$") \
                and arm[1:] in self.templates["motion_profiles"]:
            record["arm"] = self._clone_record("motion_profiles", arm[1:], scope)
        self._clone_failures_for(name, record["name"], scope)

    def _after_set(self, record, name, scope, bound) -> None:
        record["members"] = [self._clone_record("resources", m, scope)
                             if m in self.templates["resources"] else m
                             for m in record.get("members", [])]

    def _after_charger(self, record, name, scope, bound) -> None:
        """`"node": "$bay"` is the instance's own geometry node (D167)."""
        node = record.get("node")
        if isinstance(node, str) and node.startswith("$"):
            record["node"] = f"{scope}/{node[1:]}"

    def _after_level(self, record, name, scope, bound) -> None:
        """Only an expression `initial` reads the parameters (PHASE5 SD-2); a
        `$` signal or variable on a threshold is per-instance, so two
        warehouses do not share one reorder signal (item 9); and every flow
        touching the level is cloned with it (PHASE4 D1, the failure rule)."""
        if not isinstance(record.get("initial"), str):
            record.pop("locals", None)
        if record.get("thresholds"):
            record["thresholds"] = self._rewrite(record["thresholds"], f"{scope}/")
        for flow_name, entry in self.templates["flows"].items():
            if name in (entry.get("from"), entry.get("to")):
                self._clone_record("flows", flow_name, scope)

    def _after_flow(self, record, name, scope, bound) -> None:
        """A `$` end is scoped; a literal level name is the asset's OWN level and
        clones with it, unless it came through an argument: then it belongs to
        someone else and is bound as given -- two feeders on one store (D73)."""
        for side in ("from", "to"):
            end = record.get(side)
            if not isinstance(end, str) or not end:
                continue
            if end.startswith("$"):
                record[side] = self._rewrite(end, f"{scope}/")
            elif end in self.templates["levels"] and side not in bound:
                record[side] = self._clone_record("levels", end, scope)

    def _after_population(self, record, name, scope, bound) -> None:
        """A population brings its type unless `type` is a binding (PHASE6 C1)."""
        if record.get("type") in self.templates["agent_types"] and "type" not in bound:
            record["type"] = self._clone_record("agent_types", record["type"], scope)
        self._after_agent_type(record, name, scope, bound)

    def _after_agent_type(self, record, name, scope, bound) -> None:
        """Every `$` name, not only the expression fields: statechart actions
        (`{"spawn": "$People"}`) and counts (`COUNT("$People")`) too."""
        record.update(self._rewrite({k: v for k, v in record.items() if k not in bound},
                                    f"{scope}/"))

    _AFTER = {"resources": _after_resource, "fleets": _after_resource,
              "resource_sets": _after_set, "chargers": _after_charger, "levels": _after_level,
              "flows": _after_flow, "populations": _after_population,
              "agent_types": _after_agent_type}

    # -- scoped field overrides (item 7) and failure cloning (item 6) -------
    def _int_field(self, section: str, field: str) -> bool:
        return field in _INT_FIELDS.get(section, ())

    def _resolve_record(self, record: dict, locals_: dict, scope: str, section: str) -> dict:
        """The record's own `{"$arg": ...}` fields, resolved for this instance."""
        out = {}
        for field, value in record.items():
            if isinstance(value, dict) and len(value) == 1 and ARG_KEY in value:
                out[field] = self._argument(str(value[ARG_KEY]), locals_, scope, field,
                                            int if self._int_field(section, field) else None)
        return out

    def _apply_scoped(self, record: dict, section: str, name: str, scope: str) -> set:
        """Apply *scope*'s instance's "scoped.<section>.<name>" field overrides
        (declared on the SUBMODEL's definition) onto a just-cloned record, and
        resolve any `{"$arg": ...}` the TEMPLATE record itself carries (item 8),
        which is per-instance for the same reason the clone is.

        Returns the fields whose value came from an ARGUMENT. Those are
        BINDINGS to a record someone else owns and must never trigger a clone
        of their own (item 11, D73): a flow's `from`/`to`, a cloned resource's
        `capacity_schedule`.
        """
        entry = self._scoped.get(scope)
        if entry is None:
            return set()
        scoped_defs, locals_ = entry
        resolved = self._resolve_record(record, locals_, scope, section)
        record.update(resolved)
        bound = set(resolved)
        overrides = (scoped_defs.get(section) or {}).get(name)
        if not overrides:
            return bound
        for field, value in overrides.items():
            if isinstance(value, dict) and len(value) == 1 and ARG_KEY in value:
                value = self._argument(str(value[ARG_KEY]), locals_, scope, field,
                                       int if self._int_field(section, field) else None)
                bound.add(field)
            elif isinstance(value, str):
                try:
                    value = compile_expression(value).evaluate(ExpressionScope(locals=locals_))
                except ExpressionError as exc:
                    self.problems.append(f"{scope}: scoped {section}.{name}.{field}: {exc}")
                    continue
            # A DOTTED field walks into the record, so a nested numeric field
            # ("uptime.dist_params.rate") is reachable without making every
            # string inside a nested spec an expression -- "Exponential" is a
            # distribution NAME, not something to evaluate (PHASE4 B5).
            *path, leaf = field.split(".")
            target = record
            for part in path:
                if isinstance(target, list):
                    target = target[int(part)] if part.isdigit() and int(part) < len(target) else {}
                    continue
                nxt = target.get(part)
                # A digit step is a LIST index -- "thresholds.0.value" is how a
                # reorder point becomes a parameter (PHASE4 D4).
                if not isinstance(nxt, (dict, list)):
                    nxt = {}
                    target[part] = nxt
                target = nxt
            if isinstance(target, list):
                if leaf.isdigit() and int(leaf) < len(target):
                    target[int(leaf)] = value
            else:
                target[leaf] = int(value) if self._int_field(section, leaf) else value
        return bound

    def _clone_scoped_records(self, scope: str, blockless: bool = False) -> None:
        """Clone the records the definition names under `scoped` (item 9).

        A continuous asset (a PV array, a grid connection) has NO blocks, so
        there is no `$` reference anywhere to trigger item 6. Naming the record
        under `scoped` is that trigger: `"scoped": {"flows": {"Nrg PV": {}}}`
        says "this flow is mine", with or without field overrides.

        Resources and fleets follow the same rule in a *blockless* definition
        only (D615, #684): one with blocks makes its own with `$Machine`, and a
        `scoped` override there must not add a phantom copy of the shared one.
        They come last, so a record a `$` reference above already cloned keeps
        its place in the flattened order.
        """
        scoped_defs, _locals = self._scoped.get(scope, ({}, {}))
        for section in _SCOPED_ORDER + (("resources", "fleets") if blockless else ()):
            for name in scoped_defs.get(section) or {}:
                if name in self.templates[section]:
                    self._clone_record(section, name, scope)

    def _clone_failures_for(self, name: str, new_name: str, scope: str) -> None:
        """Every top-level failure naming the template resource just cloned as
        *new_name* gets its own per-instance clone: the resource list is
        rewritten to the instance's copy, under a distinct per-instance name."""
        for failure_name, entry in self.templates["failures"].items():
            if name in entry.get("resources", []):
                clone = self.clones["failures"][self._clone_record("failures", failure_name,
                                                                   scope)]
                clone["resources"] = [new_name if r == name else r for r in clone["resources"]]
