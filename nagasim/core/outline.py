"""The model outline (6D1 U6): every element of a model as one tree -- blocks
by submodel level, definitions, agent types with their states and
transitions, SD records, data records, the path network and annotations.

Pure logic, no Qt: `ui/outline.py` draws the tree and turns a node's
(kind, name) into navigation. Kinds and names are `core/search.py`'s own, so
a Find match and an outline row for the same thing are interchangeable.
"""
from __future__ import annotations

import dataclasses

from nagasim.core import schema, sd, submodels

__all__ = ["Node", "build_outline", "transition_label", "RECORD_GROUPS", "SECTION_OF_KIND"]

# (group label, document section, node kind), in display order. Table-driven:
# a new named-record section is one line here, not one builder per kind.
RECORD_GROUPS = (
    ("Agent types", "agent_types", "agent_type"),
    ("Populations", "populations", "population"),
    ("Levels", "levels", "level"),
    ("Flows", "flows", "flow"),
    ("Auxiliaries", "auxiliaries", "auxiliary"),
    ("Delays", "delays", "delay"),
    ("Resources", "resources", "resource"),
    ("Resource sets", "resource_sets", "resource_set"),
    ("Schedules", "schedules", "schedule"),
    ("Failures", "failures", "failure"),
    ("Tables", "tables", "table"),
    ("Time series", "timeseries_inputs", "timeseries_input"),
    ("Variables", "variables", "variable"),
    ("Conversions", "conversions", "conversion"),
)
SECTION_OF_KIND = {kind: section for _label, section, kind in RECORD_GROUPS}
_KIND_OF_SECTION = {section: kind for _label, section, kind in RECORD_GROUPS}


@dataclasses.dataclass
class Node:
    kind: str          # "group", "block", "submodel", "definition", a RECORD_GROUPS kind,
                       # "state", "transition", "node", "segment", "annotation"
    name: str          # what navigation needs: a block's instance path ("cell/drill"),
                       # "def/block", "Type/state", "Type/<index>", "a|b" for a segment
    path: str          # unique in the tree: keeps expansion/selection across rebuilds
    label: str
    children: list = dataclasses.field(default_factory=list)
    owner: str | None = None     # the definition that owns this record (D108)
    problem_count: int = 0       # problems on this node or anything under it
    detail: str = ""             # a block's type, a record's one-line summary


def _abbrev(text, limit: int, ellipsis: str = "...") -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit - len(ellipsis)] + ellipsis


def transition_label(tr: dict, ellipsis: str = "...") -> str:
    """"T timeout / r rate / condition / tick / message", abbreviated guard in
    brackets (PHASE5 AB-5, PHASE6 A1): the statechart's edge label, which
    passes its own typographic *ellipsis* (engine strings stay ASCII)."""
    def cut(text, limit):
        return _abbrev(text, limit, ellipsis)

    if "timeout" in tr:
        base = f"T {cut(tr['timeout'], 16)}"
    elif "rate" in tr:
        base = f"r {cut(tr['rate'], 16)}"
    elif "condition" in tr:
        base = cut(tr["condition"], 26)
    elif "tick" in tr:
        base = f"tick {cut(tr['tick'], 14)}" if tr["tick"] else "tick"
    elif "message" in tr:
        base = tr["message"] or "*"
    else:
        base = "?"
    if tr.get("guard"):
        base = f"{base} [{cut(tr['guard'], 14)}]"
    return base


def _block_label(name: str, block) -> str:
    if block.type == submodels.SUBMODEL:
        return f"{name} ({block.params.get('definition', '')})"
    return f"{name} ({block.type})"


def _level_blocks(document, definition: str | None, prefix: str, path: str, seen: tuple) -> list:
    """The blocks of one level; a SUBMODEL instance nests its definition's
    blocks under it, named by instance path. *seen* stops a recursive definition."""
    level = document if definition is None else document.open_definition(definition)
    out = []
    for name, block in level.blocks.items():
        full = f"{prefix}{name}"
        node = Node("block", full, f"{path}/{name}", _block_label(name, block), detail=block.type)
        inner = str(block.params.get("definition", ""))
        if block.type == submodels.SUBMODEL and inner in document.submodels and inner not in seen:
            node.children = _level_blocks(document, inner, f"{full}/", node.path, seen + (inner,))
        out.append(node)
    return out


def _agent_children(entry: dict, path: str) -> list:
    """A composite state's own Node nests its children (PHASE6 K2: a state
    with `parent` sits under it instead of alongside it at the top level);
    a flat type (no `parent` at all) is unaffected -- every state stays a
    top-level sibling in declaration order, same as before K2."""
    tname = entry.get("name", "")
    states = [s for s in entry.get("states", []) if isinstance(s, dict) and s.get("name")]
    nodes = {s["name"]: Node("state", f"{tname}/{s['name']}", f"{path}/states/{s['name']}", s["name"])
            for s in states}
    top = []
    for s in states:
        node = nodes[s["name"]]
        parent = s.get("parent")
        if parent and parent in nodes:
            nodes[parent].children.append(node)
        else:
            top.append(node)
    transitions = [Node("transition", f"{tname}/{i}", f"{path}/transitions/{i}",
                        f"{tr.get('from')} -> {tr.get('to')}: {transition_label(tr)}")
                   for i, tr in enumerate(entry.get("transitions", [])) if isinstance(tr, dict)]
    return top + transitions


def _owners(owned: dict) -> dict:
    """{(section, name): definition}, the reverse of *owned*."""
    out = {}
    for definition, sections in owned.items():
        for section, names in sections.items():
            for name in names:
                out.setdefault((section, name), definition)
    return out


def _record_node(section: str, entry: dict, owners: dict, path: str) -> Node:
    kind, name = _KIND_OF_SECTION[section], entry.get("name", "")
    node = Node(kind, name, path, name, owner=owners.get((section, name)))
    if section == "agent_types":
        node.children = _agent_children(entry, path)
    elif section == "populations":
        node.detail = f"{entry.get('type', '')} x {entry.get('count', '')}"
    elif section == "flows":
        node.detail = f"{entry.get('from') or '(cloud)'} -> {entry.get('to') or '(cloud)'}"
    return node


def build_outline(document) -> list[Node]:
    """The outline's top-level groups, empty ones omitted, with problem
    counts filled in from `schema.validate_model_structured`."""
    # Each definition's `owns` (D108), kept to records that still exist.
    existing = {(section, r.get("name")) for section in _KIND_OF_SECTION
                for r in getattr(document, section, []) or []}
    owned = {definition: {section: [n for n in names if (section, n) in existing]
                          for section, names in (document.definition_owns(definition) or {}).items()
                          if section in _KIND_OF_SECTION}
             for definition in document.submodels}
    owners = _owners(owned)
    groups: list[Node] = []

    def group(label: str, children: list) -> None:
        if children:
            groups.append(Node("group", label, f"/group/{label}", label, children))

    group("Blocks", _level_blocks(document, None, "", "/blocks", ()))

    definitions = []
    for definition in document.submodels:
        count = len(document.instances_of(definition))
        node = Node("definition", definition, f"/submodels/{definition}",
                    f"{definition} ({count} instance{'s' if count != 1 else ''})")
        for name, block in document.open_definition(definition).blocks.items():
            node.children.append(Node("submodel", f"{definition}/{name}",
                                      f"{node.path}/blocks/{name}", _block_label(name, block),
                                      detail=block.type))
        for section, names in owned[definition].items():
            node.children += [Node(_KIND_OF_SECTION[section], name,
                                   f"{node.path}/owns/{section}/{name}", name, owner=definition)
                              for name in names]
        definitions.append(node)
    group("Submodel definitions", definitions)

    for label, section, _kind in RECORD_GROUPS:
        group(label, [_record_node(section, entry, owners, f"/{section}/{entry.get('name', '')}")
                      for entry in getattr(document, section, []) or [] if entry.get("name")])

    network = [Node("node", n["name"], f"/network/nodes/{n['name']}", n["name"])
               for n in document.network_nodes() if n.get("name")]
    network += [Node("segment", f"{s.get('from')}|{s.get('to')}",
                     f"/network/segments/{s.get('from')}|{s.get('to')}",
                     f"{s.get('from')} -> {s.get('to')}")
                 for s in document.network_segments()]
    group("Network", network)

    group("Annotations", [Node("annotation", a.get("id", ""), f"/annotations/{a.get('id', '')}",
                               _abbrev(a.get("title") or a.get("text") or a.get("id", ""), 40),
                               detail=a.get("kind", ""))
                          for a in document.annotations if a.get("id")])

    _count_problems(document, groups)
    return groups


# -- problem badges ------------------------------------------------------
def _validated_layout(layout: dict) -> dict:
    """The layout `validate_model_structured` reports indices against: the
    flattened, SD-expanded one (a problem's "/levels/3" is the fourth level
    THERE). Falls back to *layout* when flattening fails -- those problems
    all carry "/submodels" paths anyway."""
    try:
        return sd.expand(submodels.flatten(layout))
    except Exception:       # a broken section: flatten's errors are already problems
        return layout


def _problem_key(problem, target: dict):
    """The (kind, name) *problem* belongs to, or None when it names nothing
    the outline shows (a model-wide "no CREATE block", a generated record)."""
    parts = problem.path.strip("/").split("/")
    head = parts[0] if parts else ""
    if head == "submodels" and len(parts) > 1:
        if problem.block and len(parts) > 2 and parts[2] == "blocks":
            return "submodel", f"{parts[1]}/{problem.block}"
        return "definition", parts[1]
    if problem.block:
        return "block", problem.block
    if head == "network" and len(parts) > 2 and parts[2].isdigit():
        entries = (target.get("network") or {}).get(parts[1]) or []
        index = int(parts[2])
        if parts[1] not in ("nodes", "segments") or index >= len(entries):
            return None
        entry = entries[index]
        return (("node", entry.get("name", "")) if parts[1] == "nodes"
                else ("segment", f"{entry.get('from')}|{entry.get('to')}"))
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    index = int(parts[1])
    records = target.get(head)
    if not isinstance(records, list) or index >= len(records) or not isinstance(records[index], dict):
        return None
    entry = records[index]
    if head == "blocks":
        return "block", entry.get("name", "")
    if head == "annotations":
        return "annotation", entry.get("id", "")
    if head not in _KIND_OF_SECTION:
        return None
    # A per-instance clone is "<instance path>/<record>" once flattened; its
    # problem is the model-level template's (the record the outline shows).
    name = str(entry.get("name", "")).rpartition("/")[2]
    if head == "agent_types" and len(parts) > 3 and parts[2] == "transitions" and parts[3].isdigit():
        return "transition", f"{name}/{parts[3]}"
    if head == "agent_types" and len(parts) > 3 and parts[2] == "states" and parts[3].isdigit():
        states = entry.get("states") or []
        j = int(parts[3])
        if j < len(states) and isinstance(states[j], dict):
            return "state", f"{name}/{states[j].get('name', '')}"
    return _KIND_OF_SECTION[head], name


def _count_problems(document, groups: list) -> None:
    layout = document.to_layout()
    problems = schema.validate_model_structured(layout)
    if not problems:
        return
    target = _validated_layout(layout)
    counts: dict = {}
    for problem in problems:
        key = _problem_key(problem, target)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1

    def fill(node: Node) -> int:
        node.problem_count = counts.get((node.kind, node.name), 0) + sum(fill(c) for c in node.children)
        return node.problem_count

    for node in groups:
        fill(node)
