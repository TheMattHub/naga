"""Pure-logic search over a ModelDocument (P2.3a item 3, Ctrl+F). No Qt: the
UI turns a Match into a selection/centre action; this module only finds them.
"""
from __future__ import annotations

import dataclasses

from nagasim.core import sections
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
    stable order (blocks in doc.blocks order, then annotations, submodels, the
    model-data records in section-table order, agent states and transitions, the
    network). Empty/whitespace-only text matches nothing."""
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

    # Every keyed section a client lists as model data (B7, UX review 2026-10-16):
    # read off the section table, so a new section is found without a line here.
    # What Find matches in a record is the key the model names it by.
    for section in sections.SECTIONS:
        if not (section.item and section.tab and section.key):
            continue
        for entry in getattr(doc, section.name, None) or []:
            key = entry.get(section.key, "") if isinstance(entry, dict) else ""
            if _contains(key, needle):
                matches.append(Match(section.item, str(key), "name"))

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
