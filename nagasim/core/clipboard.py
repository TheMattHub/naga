"""Copy/paste of a block selection as a JSON-able fragment. Pure logic per
UI_DESIGN.md rule 2 (all model editing goes through ModelDocument) -- no Qt
here. Resource/variable references are kept unchanged: `params` is copied
verbatim (e.g. a `resource_name` string param is carried over, not remapped),
since this module never touches doc.resources/doc.variables.
"""
from __future__ import annotations

import copy

from nagasim.core import submodels
from nagasim.core.document import ModelDocument

__all__ = ["copy_selection", "paste_fragment"]

# Grid size for snapping pasted positions. Matches ui/style.py's GRID, but
# core/ must not import ui, so it is hardcoded here.
_GRID = 20


def _snap(value: float, offset: float) -> float:
    return round((value + offset) / _GRID) * _GRID


def copy_selection(doc: ModelDocument, block_names, annotation_ids=()) -> dict:
    """Build a JSON-able fragment {"blocks": [...], "links": [...],
    "annotations": [...]} from the given block names and (ROADMAP 2.3 part A
    #4) annotation ids. Unknown names/ids are silently skipped. Does not
    mutate doc."""
    names = [n for n in block_names if n in doc.blocks]
    name_set = set(names)
    blocks = []
    for n in names:
        b = doc.blocks[n]
        blocks.append({
            "name": b.name,
            "type": b.type,
            "description": b.description,
            "position": [b.position[0], b.position[1]],
            "params": copy.deepcopy(b.params),
            # A dragged name label travels with its block (D82); absent when
            # the label sits where it always did.
            **({"label_offset": list(b.label_offset)} if b.label_offset else {}),
        })
    links = []
    for link in doc.link_records():
        # Parallel links between one pair are separate records (D77), so both
        # are copied, in order.
        if link.source in name_set and link.target in name_set:
            entry = {"source": link.source, "target": link.target}
            if link.delay:
                entry["delay"] = copy.deepcopy(link.delay)
            links.append(entry)
    ann_set = set(annotation_ids)
    annotations = [copy.deepcopy(a) for a in doc.annotations if a.get("id") in ann_set]
    fragment = {"blocks": blocks, "links": links, "annotations": annotations}
    definitions = _definitions_of(doc, blocks)
    if definitions:
        fragment["definitions"] = definitions
    return fragment


def _definitions_of(doc: ModelDocument, blocks: list[dict]) -> dict:
    """#188: every definition the copied instances use (nested ones too),
    each with the model records it owns (D108), so a paste after a cut --
    whose delete dropped them -- or into another model can put them back."""
    wanted = [b["params"].get("definition") for b in blocks if b["type"] == submodels.SUBMODEL]
    if not wanted:
        return {}
    layout = doc.to_layout()
    sections = layout.get("submodels") or {}
    found: dict = {}
    while wanted:
        name = str(wanted.pop())
        if name in found or name not in sections:
            continue
        owns = doc.definition_owns(name) or {}
        found[name] = {**sections[name], "records": {
            section: [r for r in layout.get(section, []) if r.get("name") in set(names)]
            for section, names in owns.items()}}
        wanted += [b["params"].get("definition") for b in sections[name]["blocks"]
                   if b["type"] == submodels.SUBMODEL]
        if sections[name].get("extends"):       # the parent travels with its child (D127)
            wanted.append(sections[name]["extends"])
    return found


def paste_fragment(doc: ModelDocument, fragment: dict, offset=(40, 40)) -> list[str]:
    """Paste a fragment from copy_selection into doc as one undo step. Blocks
    get new, auto-generated names (unique even for a same-document paste);
    internal links are recreated between the new names. Annotations get new,
    auto-generated ids the same way. Returns the new block names, in
    fragment["blocks"] order (annotations are not included in the return
    value -- callers that need their ids can read doc.annotations after)."""
    new_names = []
    with doc.transaction(label="Paste"):
        # A definition the target lacks comes back whole, with the records it
        # owns that the target lacks (first writer wins, as for a library, D56).
        for name, section in fragment.get("definitions", {}).items():
            if name in doc.submodels:
                continue
            doc.restore_definition(name, section)
            for records_name, records in section.get("records", {}).items():
                current = getattr(doc, records_name)
                have = {r.get("name") for r in current}
                missing = [r for r in records if r.get("name") not in have]
                if missing:
                    getattr(doc, f"set_{records_name}")(current + copy.deepcopy(missing))
        name_map = {}
        for entry in fragment.get("blocks", []):
            x, y = entry["position"]
            new_position = (_snap(x, offset[0]), _snap(y, offset[1]))
            new_name = doc.add_block(entry["type"], new_position)
            name_map[entry["name"]] = new_name
            if "description" in entry:
                doc.set_description(new_name, entry["description"])
            if entry.get("params"):
                doc.set_params(new_name, entry["params"])
            if entry.get("label_offset"):
                doc.set_label_offsets({new_name: entry["label_offset"]})
            new_names.append(new_name)

        for entry in fragment.get("links", []):
            src = name_map.get(entry["source"])
            tgt = name_map.get(entry["target"])
            if src and tgt:
                index = doc.add_link(src, tgt)
                if entry.get("delay"):
                    doc.set_link_delay(src, tgt, entry["delay"], index=index)

        for entry in fragment.get("annotations", []):
            fields = {k: v for k, v in entry.items() if k not in ("id", "kind")}
            kind = entry.get("kind", "text")
            if kind == "text" and "position" in fields:
                x, y = fields["position"]
                fields["position"] = [_snap(x, offset[0]), _snap(y, offset[1])]
            elif kind == "frame" and "rect" in fields:
                x, y, w, h = fields["rect"]
                fields["rect"] = [_snap(x, offset[0]), _snap(y, offset[1]), w, h]
            doc.add_annotation(kind, **fields)

    return new_names
