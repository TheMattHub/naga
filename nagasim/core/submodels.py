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
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.problems import Problem, problem

__all__ = ["SUBMODEL", "SUB_ENTRY", "SUB_EXIT", "PORT_TYPES", "ARG_KEY", "LITERAL_TYPES",
           "REFERS_SECTIONS", "RECORD_SECTIONS", "SubmodelError", "flatten", "has_submodels",
           "definition_ports", "port_block_name", "definition_references",
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
    "timeseries_input": ("timeseries_inputs",),
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
        self.resources = {r.get("name"): r for r in layout.get("resources", [])}
        self.sets = {s.get("name"): s for s in layout.get("resource_sets", [])}
        self.schedules = {s.get("name"): s for s in layout.get("schedules", [])}
        self.failures = {f.get("name"): f for f in layout.get("failures", [])}
        # PHASE4 D1: levels clone like resources, and their flows follow them
        # like a failure follows its resource.
        self.levels = {v.get("name"): v for v in layout.get("levels", []) or []}
        self.flows = {f.get("name"): f for f in layout.get("flows", []) or []}
        # PHASE4 D5: a fixed cost clones exactly like a flow -- an asset's
        # capex reads that instance's own sizing parameters.
        self.fixed_costs = {c.get("name"): c for c in layout.get("fixed_costs", []) or []}
        # Bio-1: a digester clones per asset instance exactly as a flow does --
        # two digesters are two independent cohort pipelines.
        self.conversions = {c.get("name"): c for c in layout.get("conversions", []) or []}
        # PHASE4 E2: a fleet clones like a resource -- which is what it IS
        # (D68) -- so a forklift pool and an AGV pool are two instances of one
        # asset with their own counts, speeds and home nodes.
        self.fleets = {f.get("name"): f for f in layout.get("fleets", []) or []}
        # D167: a charger clones like a fleet, at a node of its own instance.
        self.chargers = {c.get("name"): c for c in layout.get("chargers", []) or []}
        # D174: a motion profile clones like a resource, so each robot asset
        # instance has its own speeds.
        self.motion_profiles = {p.get("name"): p
                                for p in layout.get("motion_profiles", []) or []}
        # D72: a variable clones per instance like a resource, so two holders
        # or two batteries each get their own hysteresis gate or setpoint.
        self.variables = {v.get("name"): v for v in layout.get("variables", []) or []}
        # PHASE5 SD-2: an auxiliary clones like a fixed cost -- an SD asset's
        # "birth_rate" reads that instance's own parameters.
        self.auxiliaries = {a.get("name"): a for a in layout.get("auxiliaries", []) or []}
        # DEVLOG #123: a delay clones like an auxiliary. flatten runs before
        # sd.expand, so a clone "cell_1/d" gets stages "cell_1/d~s1..n".
        self.delays = {d.get("name"): d for d in layout.get("delays", []) or []}
        # PHASE6 C1: a population clones like a level and brings its type.
        self.agent_types = {t.get("name"): t for t in layout.get("agent_types", []) or []}
        self.populations = {p.get("name"): p for p in layout.get("populations", []) or []}
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
                                    if name in getattr(self, section))
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
        if name in self.populations:
            self._clone_population(name, scope)
        elif name in self.agent_types:
            self._clone_agent_type(name, scope)
        elif name in self.sets:
            self._clone_set(name, scope)
        elif name in self.resources:
            self._clone_resource(name, scope)
        elif name in self.schedules:
            self._clone_schedule(name, scope)
        elif name in self.levels:
            self._clone_level(name, scope)
        elif name in self.fleets:
            self._clone_fleet(name, scope)
        elif name in self.variables:
            self._clone_variable(name, scope)
        elif name in self.auxiliaries:
            self._clone_auxiliary(name, scope)
        elif name in self.delays:
            self._clone_delay(name, scope)
        elif name in self.motion_profiles:
            self._clone_motion_profile(name, scope)
        # Anything else (a signal, a user statistic, a store/block name) has no
        # top-level record: the scoped name is all it needs.

    def _clone_resource(self, name: str, scope: str) -> str:
        new_name = f"{scope}/{name}"
        if new_name in self.clones["resources"]:
            return new_name
        record = copy.deepcopy(self.resources[name])
        record["name"] = new_name
        self.clones["resources"][new_name] = record        # before recursing: cycle-safe
        bound = self._apply_scoped(record, "resources", name, scope)
        schedule = record.get("capacity_schedule")
        if schedule and schedule in self.schedules and "capacity_schedule" not in bound:
            record["capacity_schedule"] = self._clone_schedule(schedule, scope)
        self._clone_failures_for(name, new_name, scope)
        return new_name

    def _clone_fleet(self, name: str, scope: str) -> str:
        """A per-instance copy of a template FLEET (PHASE4 E2).

        A fleet IS a resource (D68), so this is `_clone_resource` with one
        difference: the TEMPLATE is consumed. An idle resource template costs
        nothing, but a leftover fleet would register vehicles of its own, take
        a row in the resource and fleet frames, and need a home node that only
        an instance knows.
        """
        new_name = f"{scope}/{name}"
        if new_name in self.clones["fleets"]:
            return new_name
        record = copy.deepcopy(self.fleets[name])
        record["name"] = new_name
        self.clones["fleets"][new_name] = record        # before recursing: cycle-safe
        self.consumed["fleets"].add(name)
        bound = self._apply_scoped(record, "fleets", name, scope)
        schedule = record.get("capacity_schedule")
        if schedule and schedule in self.schedules and "capacity_schedule" not in bound:
            record["capacity_schedule"] = self._clone_schedule(schedule, scope)
        arm = record.get("arm")
        if isinstance(arm, str) and arm[1:] in self.motion_profiles and arm.startswith("$"):
            record["arm"] = self._clone_motion_profile(arm[1:], scope)
        self._clone_failures_for(name, new_name, scope)
        return new_name

    def _clone_motion_profile(self, name: str, scope: str) -> str:
        """A per-instance copy of a template MOTION PROFILE (D174). The
        template is left in place, like a resource's: an unused profile is
        inert."""
        new_name = f"{scope}/{name}"
        if new_name not in self.clones["motion_profiles"]:
            record = copy.deepcopy(self.motion_profiles[name])
            record["name"] = new_name
            self.clones["motion_profiles"][new_name] = record
            self._apply_scoped(record, "motion_profiles", name, scope)
        return new_name

    def _clone_charger(self, name: str, scope: str) -> str:
        """A per-instance copy of a template CHARGER (D167).

        Reached only through `scoped.chargers`: no block names a charger. The
        template is consumed, like a fleet's -- left in place it would be a
        real charger at a node only an instance has. `"node": "$bay"` is the
        instance's own geometry node, `<instance>/bay`.
        """
        new_name = f"{scope}/{name}"
        if new_name not in self.clones["chargers"]:
            record = copy.deepcopy(self.chargers[name])
            record["name"] = new_name
            self.clones["chargers"][new_name] = record
            self.consumed["chargers"].add(name)
            self._apply_scoped(record, "chargers", name, scope)
            node = record.get("node")
            if isinstance(node, str) and node.startswith("$"):
                record["node"] = f"{scope}/{node[1:]}"
        return new_name

    def _clone_variable(self, name: str, scope: str) -> str:
        """A per-instance copy of a template VARIABLE (item 10, D72).

        The TEMPLATE is left in place, like a resource template and unlike a
        level: an unwritten variable is inert -- it costs one row and drives
        nothing -- and a hand-written model may still read the bare name.
        """
        new_name = f"{scope}/{name}"
        if new_name not in self.clones["variables"]:
            record = copy.deepcopy(self.variables[name])
            record["name"] = new_name
            self._apply_scoped(record, "variables", name, scope)
            self.clones["variables"][new_name] = record
        return new_name

    def _clone_set(self, name: str, scope: str) -> str:
        new_name = f"{scope}/{name}"
        if new_name in self.clones["resource_sets"]:
            return new_name
        record = copy.deepcopy(self.sets[name])
        record["name"] = new_name
        self.clones["resource_sets"][new_name] = record
        record["members"] = [self._clone_resource(m, scope) if m in self.resources
                             else m for m in record.get("members", [])]
        return new_name

    def _clone_schedule(self, name: str, scope: str) -> str:
        new_name = f"{scope}/{name}"
        if new_name not in self.clones["schedules"]:
            record = copy.deepcopy(self.schedules[name])
            record["name"] = new_name
            self._apply_scoped(record, "schedules", name, scope)
            self.clones["schedules"][new_name] = record
        return new_name

    # -- scoped field overrides (item 7) and failure cloning (item 6) -------
    # Fields a record needs as an integer, cast after evaluation, PER SECTION:
    # a resource's capacity is a unit count, a LEVEL's capacity is kWh or
    # tonnes and casting it truncated a 10.5 kWh battery to 10 (DEVLOG #92).
    _INT_FIELDS = {"resources": frozenset({"capacity"}), "fleets": frozenset({"count"}),
                   "populations": frozenset({"count", "max_count"}),
                   "chargers": frozenset({"points", "spare_packs"})}

    def _int_field(self, section: str, field: str) -> bool:
        return field in self._INT_FIELDS.get(section, ())

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

    def _clone_level(self, name: str, scope: str) -> str:
        """A per-instance copy of a template LEVEL, plus a copy of every flow
        that touches it (PHASE4 D1) -- the failure rule of item 6, applied to
        the continuous side."""
        new_name = f"{scope}/{name}"
        if new_name in self.clones["levels"]:
            return new_name
        record = copy.deepcopy(self.levels[name])
        record["name"] = new_name
        self.clones["levels"][new_name] = record        # before recursing: cycle-safe
        self.consumed["levels"].add(name)
        self._apply_scoped(record, "levels", name, scope)
        if isinstance(record.get("initial"), str):      # an expression (PHASE5 SD-2)
            _scoped_defs, locals_ = self._scoped.get(scope, ({}, {}))
            if locals_:
                record["locals"] = dict(locals_)
            if "$" in record["initial"]:
                record["initial"] = self._rewrite(record["initial"], f"{scope}/")
        if record.get("thresholds"):
            # A `$signal` / `$variable` on a threshold is per-instance too, so
            # two warehouses do not share one reorder signal (item 9).
            record["thresholds"] = self._rewrite(record["thresholds"], f"{scope}/")
        for flow_name, entry in self.flows.items():
            if name in (entry.get("from"), entry.get("to")):
                self._clone_flow(flow_name, scope)
        return new_name

    def _clone_flow(self, name: str, scope: str) -> str:
        """A per-instance copy of a template FLOW (PHASE4 D3, item 9).

        Reached two ways: the flow touches a `$` level (item 6), or the
        definition names it under `scoped.flows` -- which is how a purely
        CONTINUOUS asset, with no blocks at all to carry a `$` reference, still
        gets its own accounting flows. The clone carries the instance's
        parameters as `locals`, so its rate, price and impact expressions read
        them the way an expanded block's expressions do.
        """
        clone_name = f"{scope}/{name}"
        if clone_name in self.clones["flows"]:
            return clone_name
        clone = copy.deepcopy(self.flows[name])
        clone["name"] = clone_name
        self.clones["flows"][clone_name] = clone        # before recursing: cycle-safe
        self.consumed["flows"].add(name)
        bound = self._apply_scoped(clone, "flows", name, scope)
        _scoped_defs, locals_ = self._scoped.get(scope, ({}, {}))
        if locals_:
            clone["locals"] = dict(locals_)
        for field in ("rate", "price", "emission_factor"):
            if isinstance(clone.get(field), str) and "$" in clone[field]:
                clone[field] = self._rewrite(clone[field], f"{scope}/")
        for side in ("from", "to"):
            end = clone.get(side)
            if not isinstance(end, str) or not end:
                continue
            if end.startswith("$"):
                clone[side] = self._rewrite(end, f"{scope}/")
            elif end in self.levels and side not in bound:
                # Not `bound`: a literal level name inside the definition is the
                # asset's OWN level and clones with it. A name that arrived
                # through a `name` parameter belongs to someone else and is
                # bound exactly as given -- two feeders on one store (D73).
                clone[side] = self._clone_level(end, scope)
        return clone_name

    def _clone_fixed_cost(self, name: str, scope: str) -> str:
        """A per-instance copy of a template FIXED COST (PHASE4 D5).

        Reached only through `scoped.fixed_costs`: a capex has no level and no
        block to carry a `$` reference. The clone carries the instance's
        parameters as `locals`, so "capacity_kwh * capex_per_kwh * rate" reads
        that instance's sizing -- the cloned-flow rule, unchanged.
        """
        clone_name = f"{scope}/{name}"
        if clone_name in self.clones["fixed_costs"]:
            return clone_name
        clone = copy.deepcopy(self.fixed_costs[name])
        clone["name"] = clone_name
        self.clones["fixed_costs"][clone_name] = clone
        self.consumed["fixed_costs"].add(name)
        self._apply_scoped(clone, "fixed_costs", name, scope)
        _scoped_defs, locals_ = self._scoped.get(scope, ({}, {}))
        if locals_:
            clone["locals"] = dict(locals_)
        if isinstance(clone.get("amount"), str) and "$" in clone["amount"]:
            clone["amount"] = self._rewrite(clone["amount"], f"{scope}/")
        return clone_name

    def _clone_conversion(self, name: str, scope: str) -> str:
        """A per-instance copy of a template CONVERSION (Bio-1 G1).

        Reached through `scoped.conversions` or a `$` reference, like a fixed
        cost: a conversion has no level and no block of its own. The clone
        carries the instance's parameters as `locals`, so a substrate's feed
        expression reads that digester's own feeder flow and its yield reads
        that instance's catalogue row.
        """
        clone_name = f"{scope}/{name}"
        if clone_name in self.clones["conversions"]:
            return clone_name
        clone = copy.deepcopy(self.conversions[name])
        clone["name"] = clone_name
        self.clones["conversions"][clone_name] = clone
        self.consumed["conversions"].add(name)
        self._apply_scoped(clone, "conversions", name, scope)
        _scoped_defs, locals_ = self._scoped.get(scope, ({}, {}))
        if locals_:
            clone["locals"] = dict(locals_)
        for substrate in clone.get("substrates") or []:
            for field in ("feed",):
                if isinstance(substrate.get(field), str) and "$" in substrate[field]:
                    substrate[field] = self._rewrite(substrate[field], f"{scope}/")
        return clone_name

    def _clone_auxiliary(self, name: str, scope: str) -> str:
        """A per-instance copy of a template AUXILIARY (PHASE5 SD-2), reached
        through `scoped.auxiliaries` or an `AUX("$name")` reference: the fixed
        cost rule -- the clone carries the instance's parameters as `locals`
        and its own `$` references are scoped (and cloned) in turn."""
        clone_name = f"{scope}/{name}"
        if clone_name in self.clones["auxiliaries"]:
            return clone_name
        clone = copy.deepcopy(self.auxiliaries[name])
        clone["name"] = clone_name
        self.clones["auxiliaries"][clone_name] = clone      # before recursing: cycle-safe
        self.consumed["auxiliaries"].add(name)
        self._apply_scoped(clone, "auxiliaries", name, scope)
        _scoped_defs, locals_ = self._scoped.get(scope, ({}, {}))
        if locals_:
            clone["locals"] = dict(locals_)
        if isinstance(clone.get("expr"), str) and "$" in clone["expr"]:
            clone["expr"] = self._rewrite(clone["expr"], f"{scope}/")
        return clone_name

    def _clone_delay(self, name: str, scope: str) -> str:
        """A per-instance copy of a template DELAY (DEVLOG #123): the
        auxiliary rule, over the delay's three expression fields."""
        clone_name = f"{scope}/{name}"
        if clone_name in self.clones["delays"]:
            return clone_name
        clone = copy.deepcopy(self.delays[name])
        clone["name"] = clone_name
        self.clones["delays"][clone_name] = clone           # before recursing: cycle-safe
        self.consumed["delays"].add(name)
        self._apply_scoped(clone, "delays", name, scope)
        _scoped_defs, locals_ = self._scoped.get(scope, ({}, {}))
        if locals_:
            clone["locals"] = dict(locals_)
        for field in ("input", "delay_time", "initial"):
            if isinstance(clone.get(field), str) and "$" in clone[field]:
                clone[field] = self._rewrite(clone[field], f"{scope}/")
        return clone_name

    def _clone_population(self, name: str, scope: str) -> str:
        """A per-instance copy of a template POPULATION (PHASE6 C1, item 12):
        its type is cloned with it unless `type` came from an argument (a
        binding, item 11), and every `$` name in it is scoped."""
        clone_name = f"{scope}/{name}"
        if clone_name in self.clones["populations"]:
            return clone_name
        clone = copy.deepcopy(self.populations[name])
        clone["name"] = clone_name
        self.clones["populations"][clone_name] = clone      # before recursing: cycle-safe
        self.consumed["populations"].add(name)
        bound = self._apply_scoped(clone, "populations", name, scope)
        if clone.get("type") in self.agent_types and "type" not in bound:
            clone["type"] = self._clone_agent_type(clone["type"], scope)
        clone.update(self._rewrite({k: v for k, v in clone.items() if k not in bound},
                                   f"{scope}/"))
        return clone_name

    def _clone_agent_type(self, name: str, scope: str) -> str:
        """A per-instance copy of a template AGENT TYPE (PHASE6 C1): parameters
        take the instance's `scoped` overrides, and every `$` name in its
        expressions and actions (`COUNT("$People")`, `{"spawn": "$People"}`)
        is scoped -- and cloned -- in turn."""
        clone_name = f"{scope}/{name}"
        if clone_name in self.clones["agent_types"]:
            return clone_name
        clone = copy.deepcopy(self.agent_types[name])
        clone["name"] = clone_name
        self.clones["agent_types"][clone_name] = clone      # before recursing: cycle-safe
        self.consumed["agent_types"].add(name)
        bound = self._apply_scoped(clone, "agent_types", name, scope)
        clone.update(self._rewrite({k: v for k, v in clone.items() if k not in bound},
                                   f"{scope}/"))
        return clone_name

    def _clone_scoped_records(self, scope: str, blockless: bool = False) -> None:
        """Clone the records the definition names under `scoped` (item 9).

        A continuous asset (a PV array, a grid connection) has NO blocks, so
        there is no `$` reference anywhere to trigger item 6. Naming the record
        under `scoped` is that trigger: `"scoped": {"flows": {"Nrg PV": {}}}`
        says "this flow is mine", with or without field overrides.

        Resources and fleets follow the same rule in a *blockless* definition
        only (D615, #684): one with blocks makes its own with `$Machine`, and a
        `scoped` override there must not add a phantom copy of the shared one.
        """
        scoped_defs, _locals = self._scoped.get(scope, ({}, {}))
        for name in scoped_defs.get("levels") or {}:
            if name in self.levels:
                self._clone_level(name, scope)
        for name in scoped_defs.get("flows") or {}:
            if name in self.flows:
                self._clone_flow(name, scope)
        for name in scoped_defs.get("fixed_costs") or {}:
            if name in self.fixed_costs:
                self._clone_fixed_cost(name, scope)
        for name in scoped_defs.get("conversions") or {}:
            if name in self.conversions:
                self._clone_conversion(name, scope)
        for name in scoped_defs.get("chargers") or {}:
            if name in self.chargers:
                self._clone_charger(name, scope)
        for name in scoped_defs.get("variables") or {}:
            if name in self.variables:
                self._clone_variable(name, scope)
        for name in scoped_defs.get("auxiliaries") or {}:
            if name in self.auxiliaries:
                self._clone_auxiliary(name, scope)
        for name in scoped_defs.get("delays") or {}:
            if name in self.delays:
                self._clone_delay(name, scope)
        for name in scoped_defs.get("agent_types") or {}:
            if name in self.agent_types:
                self._clone_agent_type(name, scope)
        for name in scoped_defs.get("populations") or {}:
            if name in self.populations:
                self._clone_population(name, scope)
        # D615: a block-less asset (a crew pool, a vehicle fleet) owns its
        # resource or fleet the same way; last, so a record a `$` reference
        # above already cloned keeps its place in the flattened order.
        if not blockless:
            return
        for name in scoped_defs.get("resources") or {}:
            if name in self.resources:
                self._clone_resource(name, scope)
        for name in scoped_defs.get("fleets") or {}:
            if name in self.fleets:
                self._clone_fleet(name, scope)

    def _clone_failures_for(self, name: str, new_name: str, scope: str) -> None:
        """Every top-level failure naming the template resource just cloned as
        *new_name* gets its own per-instance clone: the resource list is
        rewritten to the instance's copy, under a distinct per-instance name."""
        for failure_name, entry in self.failures.items():
            if name not in entry.get("resources", []):
                continue
            clone_name = f"{scope}/{failure_name}"
            clone = self.clones["failures"].get(clone_name)
            if clone is None:
                clone = copy.deepcopy(entry)
                clone["name"] = clone_name
                self.clones["failures"][clone_name] = clone
                self._apply_scoped(clone, "failures", failure_name, scope)
            clone["resources"] = [new_name if r == name else r for r in clone["resources"]]
