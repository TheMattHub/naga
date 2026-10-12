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
    link. Returns the new block's name."""
    if doc.link_count(src, tgt) <= index:
        raise ValueError(f"no such link: {src} -> {tgt}")
    branch_index = doc.branch_index(src, tgt, index=index)
    delay = doc.link_delay(src, tgt, index=index)
    sx, sy = doc.blocks[src].position
    tx, ty = doc.blocks[tgt].position
    midpoint = (_snap((sx + tx) / 2), _snap((sy + ty) / 2))

    with doc.transaction():
        new_name = doc.add_block(block_type, midpoint, name=name)
        doc.remove_link(src, tgt, index=index)
        doc.add_link(src, new_name)
        current_index = doc.branch_index(src, new_name)
        if current_index != branch_index:
            doc.reorder_link(src, current_index, branch_index)
        if delay:
            doc.set_link_delay(src, new_name, delay)
        doc.add_link(new_name, tgt)
    return new_name
