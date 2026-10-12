"""Pure-logic search over a ModelDocument (P2.3a item 3, Ctrl+F). No Qt: the
UI turns a Match into a selection/centre action; this module only finds them.
"""
from __future__ import annotations

import dataclasses

from nagasim.core.document import ModelDocument
from nagasim.core.outline import transition_label

__all__ = ["Match", "find_matches"]


@dataclasses.dataclass(frozen=True)
class Match:
    kind: str   # "block", "annotation", "submodel" (ROADMAP 2.3 part B), a named-record
                # kind ("resource", "level", "agent_type", ...), "state", "transition",
                # "node", "segment" -- the same kinds `core.outline` gives its nodes
    name: str   # block name / annotation id / record name, "<definition>/<block>" for a
                # block inside a definition, "Type/state", "Type/<index>", "a|b"
    field: str  # what matched: "name", "description", "type", "text", "title", or "param:<path>"


def _contains(value, needle: str) -> bool:
    return needle in str(value).lower()


def _flatten_params(params: dict, prefix: str = ""):
    """Yield (path, leaf value) for every scalar in a block's params dict,
    descending into nested dicts (dist_params) and lists of dicts
    (assignments, conditions) -- covers expression text and resource names
    wherever a block stores them."""
    for key, value in params.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _flatten_params(value, f"{path}.")
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    yield from _flatten_params(item, f"{path}[{i}].")
                else:
                    yield f"{path}[{i}]", item
        else:
            yield path, value


def find_matches(doc: ModelDocument, text: str) -> list[Match]:
    """Every case-insensitive substring match of *text* in the model, in a
    stable order (blocks in doc.blocks order, then annotations, resources,
    resource sets, schedules, variables). Empty/whitespace-only text matches
    nothing."""
    needle = text.strip().lower()
    if not needle:
        return []
    matches: list[Match] = []

    for name, block in doc.blocks.items():
        if _contains(name, needle):
            matches.append(Match("block", name, "name"))
        if _contains(block.description, needle):
            matches.append(Match("block", name, "description"))
        if _contains(block.type, needle):
            matches.append(Match("block", name, "type"))
        for path, value in _flatten_params(block.params):
            if _contains(value, needle):
                matches.append(Match("block", name, f"param:{path}"))

    for entry in doc.annotations:
        ann_id = entry.get("id", "")
        for field in ("text", "title"):
            if field in entry and _contains(entry[field], needle):
                matches.append(Match("annotation", ann_id, field))

    # Submodel definitions and the blocks inside them (ROADMAP 2.3 part B):
    # the UI opens the definition and selects the block, same as a canvas hit.
    for def_name in getattr(doc, "submodels", {}):
        if _contains(def_name, needle):
            matches.append(Match("submodel", def_name, "name"))
        for name, block in doc.open_definition(def_name).blocks.items():
            path = f"{def_name}/{name}"
            if _contains(name, needle):
                matches.append(Match("submodel", path, "name"))
            if _contains(block.description, needle):
                matches.append(Match("submodel", path, "description"))
            if _contains(block.type, needle):
                matches.append(Match("submodel", path, "type"))
            for field, value in _flatten_params(block.params):
                if _contains(value, needle):
                    matches.append(Match("submodel", path, f"param:{field}"))

    for r in doc.resources:
        if _contains(r.get("name", ""), needle):
            matches.append(Match("resource", r.get("name", ""), "name"))
    for s in doc.resource_sets:
        if _contains(s.get("name", ""), needle):
            matches.append(Match("resource_set", s.get("name", ""), "name"))
    for s in doc.schedules:
        if _contains(s.get("name", ""), needle):
            matches.append(Match("schedule", s.get("name", ""), "name"))
    for v in doc.variables:
        if _contains(v.get("name", ""), needle):
            matches.append(Match("variable", v.get("name", ""), "name"))
    # The plain named-record sections (PHASE4 A1, D1/D2): one loop, since the
    # only thing Find can match in them is the name the model refers to them by.
    for section, kind in (("tables", "table"), ("timeseries_inputs", "timeseries_input"),
                          ("levels", "level"), ("flows", "flow"),
                          ("conversions", "conversion"), ("failures", "failure"),
                          ("auxiliaries", "auxiliary"), ("delays", "delay"),
                          ("agent_types", "agent_type"), ("populations", "population")):
        for entry in getattr(doc, section, []):
            if _contains(entry.get("name", ""), needle):
                matches.append(Match(kind, entry.get("name", ""), "name"))

    # 6D1 U6: what the Outline shows below an agent type, and the path
    # network, named the Outline's way ("Type/state", "Type/<index>", "a|b").
    for entry in doc.agent_types:
        tname = entry.get("name", "")
        for state in entry.get("states", []):
            if isinstance(state, dict) and _contains(state.get("name", ""), needle):
                matches.append(Match("state", f"{tname}/{state['name']}", "name"))
        for i, tr in enumerate(entry.get("transitions", [])):
            if isinstance(tr, dict) and _contains(transition_label(tr), needle):
                matches.append(Match("transition", f"{tname}/{i}", "text"))
    for node in doc.network_nodes():
        if _contains(node.get("name", ""), needle):
            matches.append(Match("node", node.get("name", ""), "name"))
    for seg in doc.network_segments():
        if _contains(f"{seg.get('from')} {seg.get('to')}", needle):
            matches.append(Match("segment", f"{seg.get('from')}|{seg.get('to')}", "name"))

    return matches
