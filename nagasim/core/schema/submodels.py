"""Validation of the "submodels" section itself (ROADMAP 2.3 part B, schema
v12), named by definition and block. Problems INSIDE a definition's blocks --
a bad distribution, an undeclared variable -- come from validating the
flattened layout instead (see `prepare`), where the names carry the instance
prefix."""
from __future__ import annotations

from nagasim.core import submodels
from nagasim.core.problems import Problem, problem


# Params whose `$name` value is cloned from a top-level record, and the
# sections that record may live in (ROADMAP 2.3 part B, `$` scoping).
_SCOPED_TEMPLATES = {
    "resource_name": ("resources", "resource_sets"),
    "schedule_name": ("schedules",),
    "capacity_schedule": ("schedules",),
    "level": ("levels",),        # a TRANSFER block inside an asset (PHASE4 D1)
    "fleet": ("fleets",),        # a TRANSPORT block inside an asset (PHASE4 E2)
}


def _check_definition_cycles(definitions: dict, problems: list) -> None:
    """A definition may instantiate other definitions, but never (transitively)
    itself -- flattening would not terminate."""
    state: dict[str, str] = {}

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "open":
            problems.append(problem(
                "E_SUBMODEL_CYCLE",
                f"submodel definition cycle: {' -> '.join(path + [name])}",
                f"/submodels/{name}",
                hint="break the loop: a definition cannot instantiate itself, directly or through "
                     "another definition"))
            return
        state[name] = "open"
        for ref in submodels.definition_references(definitions.get(name) or {}):
            if ref in definitions:
                visit(ref, path + [name])
        state[name] = "done"

    for name in definitions:
        visit(name, [])


def _check_definition_terminals(def_name: str, definition: dict, problems: list) -> None:
    """A definition with no SUB_EXIT is a sink: every flow inside it must end in
    a DISPOSE, or the entities it receives would vanish into a dead end."""
    blocks = definition.get("blocks", [])
    if any(b.get("type", "").upper() == submodels.SUB_EXIT for b in blocks):
        return
    sources = {l.get("source") for l in definition.get("links", []) if isinstance(l, dict)}
    for index, block in enumerate(blocks):
        name = block.get("name", "<unnamed>")
        if name in sources or block.get("type", "").upper() == "DISPOSE":
            continue
        problems.append(problem(
            "E_SUBMODEL_DEAD_END",
            f"submodel '{def_name}': block '{name}' is a dead end: the definition "
            f"has no SUB_EXIT and this block is not a DISPOSE",
            f"/submodels/{def_name}/blocks/{index}", block=name,
            hint="add a SUB_EXIT port and link this block to it, or end the flow in a DISPOSE"))


def _check_scoped_names(def_name: str, definition: dict, layout: dict,
                        problems: list) -> None:
    """A `$name` that flattening clones per instance needs its template declared
    at the top level under the `$`-less name."""
    # Every section `_SCOPED_TEMPLATES` can name, so adding a kind there needs
    # nothing here (DEVLOG #91: "levels" did, and this raised KeyError on any
    # asset with a `$` level in a TRANSFER).
    declared = {section: {str(r.get("name", "")) for r in layout.get(section, []) or []
                          if isinstance(r, dict)}
                for sections in _SCOPED_TEMPLATES.values() for section in sections}
    for index, block in enumerate(definition.get("blocks", [])):
        params = block.get("params", {})
        for key, sections in _SCOPED_TEMPLATES.items():
            value = params.get(key)
            if not isinstance(value, str) or not value.startswith("$"):
                continue
            bare = value[1:]
            if not any(bare in declared[section] for section in sections):
                problems.append(problem(
                    "E_SUBMODEL_SCOPED_NAME",
                    f"submodel '{def_name}': block '{block.get('name', '<unnamed>')}': "
                    f"'{value}' has no top-level template: declare a "
                    f"{' or '.join(s.replace('_', ' ') for s in sections)} named '{bare}'",
                    f"/submodels/{def_name}/blocks/{index}/params/{key}",
                    block=block.get("name"),
                    hint=f"declare a {' or '.join(s.replace('_', ' ') for s in sections)} named "
                         f"'{bare}' at the top level; flattening clones it per instance"))


def _check_structural_arguments(def_name: str, definition: dict, problems: list) -> None:
    """Every `{"$arg": "<parameter>"}` in a definition names one of its own
    parameters (PHASE4 B6, D59). Checked here, on the UNFLATTENED definition, so
    an asset author is told which block carries the typo even when no instance
    of the definition exists yet; flattening checks the same thing again against
    the instance's actual arguments."""
    declared = {str(p.get("name", "")) for p in definition.get("parameters", []) or []}
    known = ", ".join(sorted(declared)) or "(none)"
    sources = [(f"/submodels/{def_name}/blocks/{i}/params", block.get("name"),
                block.get("params", {}))
               for i, block in enumerate(definition.get("blocks", []) or [])]
    sources.append((f"/submodels/{def_name}/scoped", None, definition.get("scoped") or {}))
    for base, block_name, value in sources:
        for name, path in submodels.argument_names(value):
            if name in declared:
                continue
            problems.append(problem(
                "E_SUBMODEL_ARG_UNKNOWN",
                f"submodel '{def_name}': {block_name or 'scoped'}: $arg '{name}' is not a "
                f"parameter of this definition",
                f"{base}{path}", block=block_name,
                hint=f"declare '{name}' in the definition's parameters; it has: {known}"))


def _check_instances(blocks, links, definitions: dict, label: str,
                     problems: list, base: str) -> None:
    """Instances and port-bearing links of ONE level (the model, or a definition)."""
    by_name = {b.get("name"): b for b in blocks}
    known = ", ".join(sorted(definitions)) or "(none)"
    for index, block in enumerate(blocks):
        if block.get("type", "").upper() != submodels.SUBMODEL:
            continue
        at = f"{base}/blocks/{index}/params"
        name = block.get("name", "<unnamed>")
        params = block.get("params", {})
        def_name = str(params.get("definition", ""))
        definition = definitions.get(def_name)
        if definition is None:
            problems.append(problem(
                "E_UNKNOWN_SUBMODEL",
                f"{label}: '{name}': unknown submodel definition '{def_name}'",
                f"{at}/definition", block=name, hint=f"use one of: {known}"))
            continue
        declared = {str(p.get("name", "")): p for p in definition.get("parameters", [])}
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        elif not isinstance(arguments, dict):
            problems.append(problem(
                "E_SUBMODEL_ARGUMENTS", f"{label}: '{name}': arguments must be an object",
                f"{at}/arguments", block=name,
                hint="\"arguments\" maps each declared parameter name to its value"))
            arguments = {}
        for key in arguments:
            if key not in declared:
                problems.append(problem(
                    "E_SUBMODEL_ARGUMENT_UNKNOWN",
                    f"{label}: '{name}': unknown argument '{key}' for submodel '{def_name}'",
                    f"{at}/arguments/{key}", block=name,
                    hint=f"'{def_name}' declares: {', '.join(declared) or '(no parameters)'}"))
        for pname, spec in declared.items():
            if pname not in arguments and "default" not in spec:
                problems.append(problem(
                    "E_SUBMODEL_ARGUMENT_MISSING",
                    f"{label}: '{name}': missing argument '{pname}' for submodel '{def_name}'",
                    f"{at}/arguments/{pname}", block=name,
                    hint=f"pass \"{pname}\" in this instance's arguments, or give the parameter "
                         f"a default in the definition"))

    for index, link in enumerate(links):
        if not isinstance(link, dict):
            continue
        arrow = f"{link.get('source')}->{link.get('target')}"
        for side, port_side in (("source", "out"), ("target", "in")):
            block = by_name.get(link.get(side))
            if block is None or block.get("type", "").upper() != submodels.SUBMODEL:
                continue
            def_name = str(block.get("params", {}).get("definition", ""))
            definition = definitions.get(def_name)
            if definition is None:
                continue  # already reported above
            ports = submodels.definition_ports(definition, port_side)
            port = link.get(f"{side}_port")
            if port is None:
                if len(ports) != 1:
                    problems.append(problem(
                        "E_SUBMODEL_PORT_REQUIRED",
                        f"{label}: link {arrow}: {side}_port is required -- submodel "
                        f"'{def_name}' has {len(ports)} {port_side} ports",
                        f"{base}/links/{index}/{side}_port",
                        hint=f"set \"{side}_port\" to one of: "
                             f"{', '.join(ports) or f'(none: add a {port_side} port to the definition)'}"))
            elif port not in ports:
                problems.append(problem(
                    "E_SUBMODEL_PORT_UNKNOWN",
                    f"{label}: link {arrow}: unknown {port_side} port '{port}' "
                    f"on submodel '{def_name}'",
                    f"{base}/links/{index}/{side}_port",
                    hint=f"use one of: {', '.join(ports) or '(none)'}"))


def validate_submodels_structured(layout: dict) -> list[Problem]:
    """Every problem the "submodels" section itself can have, as coded
    Problems (ROADMAP 3.1 item 1)."""
    # Every rule below reads the EFFECTIVE definitions (`extends` resolved,
    # PHASE6 W7 Q4): a child holding only its one replaced block is checked as
    # the whole definition it stands for.
    definitions, problems = submodels.resolve_extends(layout.get("submodels") or {})
    for def_name in [n for n, d in definitions.items() if not isinstance(d, dict)]:
        problems.append(problem("E_SUBMODEL", f"submodel '{def_name}': a definition must be "
                                              "an object", f"/submodels/{def_name}",
                                hint="give it \"parameters\", \"blocks\" and \"links\", or delete it"))
    definitions = {n: d for n, d in definitions.items() if isinstance(d, dict)}

    for index, block in enumerate(layout.get("blocks", [])):
        btype = block.get("type", "").upper()
        if btype in (submodels.SUB_ENTRY, submodels.SUB_EXIT):
            name = block.get("name", "<unnamed>")
            problems.append(problem(
                "E_SUBMODEL_SCOPE",
                f"{name}: {btype} is only allowed inside a submodel definition",
                f"/blocks/{index}/type", block=name,
                hint="move it into a submodel definition, or delete it"))

    _check_definition_cycles(definitions, problems)

    for def_name, definition in definitions.items():
        label = f"submodel '{def_name}'"
        base = f"/submodels/{def_name}"
        for side in ("in", "out"):
            seen: set[str] = set()
            for port in submodels.definition_ports(definition, side):
                if not port:
                    problems.append(problem("E_SUBMODEL_PORT_NAME",
                                            f"{label}: a {side} port has no name", base,
                                            hint=f"give every SUB_{'ENTRY' if side == 'in' else 'EXIT'}"
                                                 f" block a \"port\" name"))
                elif port in seen:
                    problems.append(problem("E_DUPLICATE_SUBMODEL_PORT",
                                            f"{label}: duplicate {side} port name '{port}'", base,
                                            hint="port names are unique per side: rename one"))
                seen.add(port)
        _check_definition_terminals(def_name, definition, problems)
        _check_scoped_names(def_name, definition, layout, problems)
        _check_structural_arguments(def_name, definition, problems)
        _check_instances(definition.get("blocks", []), definition.get("links", []),
                         definitions, label, problems, base)

    _check_instances(layout.get("blocks", []), layout.get("links", []), definitions,
                     "model", problems, "")
    return problems


def _library_clashes(layout: dict) -> list[Problem]:
    """W_LIBRARY_ITEM_CLASH (D109): a placed asset bound to a model record of
    its name whose content differs from the asset's own copy, kept in the
    definition's `info["clashes"]` -- warned while the two still differ."""
    out = []
    for definition, meta in sorted((layout.get("submodels") or {}).items()):
        if not isinstance(meta, dict):
            continue        # validate_submodels_structured reports it
        for section, records in sorted(((meta.get("info") or {}).get("clashes") or {}).items()):
            model = {r.get("name"): r for r in layout.get(section) or [] if isinstance(r, dict)}
            for name, record in sorted(records.items()):
                if name in model and model[name] != record:
                    out.append(problem(
                        "W_LIBRARY_ITEM_CLASH",
                        f"'{definition}' uses the model's own {section[:-1]} '{name}', "
                        f"which differs from the one its asset ships", f"/{section}",
                        hint="rename one of them if they are meant to be different things"))
    return out
