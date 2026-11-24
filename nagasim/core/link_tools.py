"""Pure logic for the link-oriented context-menu actions (P2.3a item 5):
reverse a link's direction, and split a link by inserting a block at its
midpoint. No Qt (UI_DESIGN.md rule 2); a caller applies these through
ModelDocument, same as core/clipboard.py and core/layout_tools.py.
"""
from __future__ import annotations

from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.document import ModelDocument

__all__ = ["can_reverse", "reverse_link", "insert_block_on_link"]

# Matches layout_tools.GRID; core/ must not import ui/, so duplicated here.
_GRID = 20


def _snap(value: float) -> float:
    return round(value / _GRID) * _GRID


def can_reverse(doc: ModelDocument, src: str, tgt: str, index: int = 0) -> tuple[bool, str]:
    """Whether src->tgt can become tgt->src: the new source (the old target)
    must be allowed an outgoing link at all (not DISPOSE) and must not already
    have one unless its block type supports multiple outputs (DECIDE, LOGIC).
    *index* picks one of several parallel links between the pair (D77).
    Returns (True, "") or (False, reason)."""
    if doc.link_count(src, tgt) <= index:
        return False, f"no such link: {src} -> {tgt}"
    new_source = tgt
    new_source_type = doc.blocks[new_source].type
    if new_source_type == "DISPOSE":
        return False, f"{new_source} is a DISPOSE block: it cannot have an outgoing link"
    if _feeds_merge(doc, src, tgt):
        return False, f"{src} feeds a merge on {tgt}, so its link cannot be reversed"
    spec = BLOCK_REGISTRY.get(new_source_type)
    multi_output = spec is not None and spec.multi_output
    if not multi_output and doc.out_links(new_source):
        return False, f"{new_source} already has an outgoing link"
    return True, ""


def reverse_link(doc: ModelDocument, src: str, tgt: str, index: int = 0) -> None:
    """Replace src->tgt with tgt->src as one undo step, carrying over any
    transfer delay. Raises ValueError when can_reverse refuses it."""
    ok, reason = can_reverse(doc, src, tgt, index)
    if not ok:
        raise ValueError(reason)
    delay = doc.link_delay(src, tgt, index=index)
    with doc.transaction():
        doc.remove_link(src, tgt, index=index)
        new_index = doc.add_link(tgt, src)
        if delay:
            doc.set_link_delay(tgt, src, delay, index=new_index)


def insert_block_on_link(doc: ModelDocument, src: str, tgt: str, block_type: str,
                         name: str | None = None, index: int = 0) -> str:
    """Split src->tgt by inserting a new block of *block_type* at the link's
    midpoint (snapped to grid), as one undo step. The new link src->new takes
    the OLD link's branch index among src's outgoing links (so a DECIDE's
    other branches keep their order); any transfer delay on the old link
    stays on the first half (src->new); the second half (new->tgt) is a plain
    link. The old link's `source_port` goes on the first half, its `target_port` on the
    second (#2031). Returns the new block's name.

    A CONVEYOR in a model (not a submodel definition, which has no floor) is a floor piece
    from creation (conveyor plan B, O8): a straight 2 m `path` in the model's length unit,
    its floor pose midway between the two ends' floor places (the one end's when only one has
    a place, else the origin), heading 0."""
    if doc.link_count(src, tgt) <= index:
        raise ValueError(f"no such link: {src} -> {tgt}")
    branch_index = doc.branch_index(src, tgt, index=index)
    delay = doc.link_delay(src, tgt, index=index)
    merges = (doc.blocks[tgt].params or {}).get("merges") if _feeds_merge(doc, src, tgt) else None
    ports = doc.link_ports(src, tgt, index=index)
    sx, sy = doc.blocks[src].position
    tx, ty = doc.blocks[tgt].position
    midpoint = (_snap((sx + tx) / 2), _snap((sy + ty) / 2))

    with doc.transaction():
        if block_type == "CONVEYOR" and isinstance(doc, ModelDocument):
            new_name = _floor_conveyor(doc, src, tgt, midpoint, name)
        else:
            new_name = doc.add_block(block_type, midpoint, name=name)
        doc.remove_link(src, tgt, index=index)
        # #2031: the old link's ports stay where they belong: the port it left by on the first
        # half, the port it entered by on the second.
        doc.add_link(src, new_name, source_port=ports.get("source_port"))
        current_index = doc.branch_index(src, new_name)
        if current_index != branch_index:
            doc.reorder_link(src, current_index, branch_index)
        if delay:
            doc.set_link_delay(src, new_name, delay)
        doc.add_link(new_name, tgt, target_port=ports.get("target_port"))
        if merges is not None and src not in doc.in_links(tgt):
            # The merge's parts now come from the new block: its `from` follows the link
            # (the removal above dropped the entry with its last link, #2030).
            doc.set_params(tgt, {**doc.blocks[tgt].params, "merges": [
                {**m, "from": new_name} if isinstance(m, dict) and m.get("from") == src else m
                for m in merges]})
    return new_name


def _feeds_merge(doc, src: str, tgt: str) -> bool:
    """Whether *src* is the `from` of a merge on CONVEYOR *tgt*."""
    block = doc.blocks.get(tgt)
    if block is None or block.type != "CONVEYOR":
        return False
    merges = (block.params or {}).get("merges")
    return isinstance(merges, list) and any(isinstance(m, dict) and m.get("from") == src
                                            for m in merges)


def _floor_conveyor(doc: ModelDocument, src: str, tgt: str, position, name) -> str:
    """O8: a 2 m straight CONVEYOR on the floor between *src* and *tgt* (see
    `insert_block_on_link`), through the editor's and the agent tools' one write."""
    from nagasim.core.conveyor_path import round_length
    from nagasim.core.floor import add_belt_piece, length_factor
    factor = length_factor(doc, {})
    places = [doc.blocks[n].layout_position for n in (src, tgt)
              if doc.blocks[n].layout_position is not None]
    x = sum(p[0] for p in places) / len(places) if places else 0.0
    y = sum(p[1] for p in places) / len(places) if places else 0.0
    # The editor's rounding (0.01 of the unit or 0.01 m, the finer): 0.002 in a km model.
    length = round_length(2.0 / factor, factor)
    return add_belt_piece(doc, [{"straight": length}], pose=(x, y, 0.0),
                          name=name, position=position)
