"""The `stockpiles` section: blocks drawn as piles of slabs in playback (D1111).

Layout only, like `cranes`: the run never reads it. A store block that holds many entities
draws them as a few piles instead of a queue row, so stacking reads on the floor plan:

    "stockpiles": {"source": "Inbound", "keys": ["long", "short", ...], "max_layers": 8,
                   "areas": [{"block": "Stock_A", "label": "Area A", "label_at": [x, y],
                              "colour": "#2a78d6",
                              "piles": [{"key": "long", "rect": [x0, y0, x1, y1], "label": "Long"}]}]}

- `keys` gives each entity its pile key, by creation order at the `source` block (the k-th
  entity created there gets keys[k], so the key follows the entity across scenarios). An
  entity with no key, or a key no pile of its area has, goes on the area's last pile.
- An area may carry its own `keys` (and `source`, default the section's), used for that area
  only (D1146): a ground buffer piles by destination while the warehouse piles by product.
  An area without them uses the section's list, so the single-list form stays valid.
- A pile draws one slab per entity up to `max_layers` (default 8), each offset towards the
  viewer and darker, then only its "xN" label counts. The newest entity flashes as it lands
  and the area's `label` shows its total.

Pure Python; the 2D player (`product/ui/cranes2d.py`) draws it.
"""
from __future__ import annotations

import re

from nagasim.core.problems import problem

__all__ = ["MAX_LAYERS", "validate_stockpiles", "pile_keys", "landings", "stays", "pile_of"]

MAX_LAYERS = 8
_FIELDS = {"source", "keys", "max_layers", "areas"}
_AREA_FIELDS = {"block", "label", "label_at", "colour", "piles", "keys", "source"}
_PILE_FIELDS = {"key", "rect", "label"}
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_stockpiles(layout: dict, problems: list) -> None:
    """Shape of the optional `stockpiles` section (a `core/sections.py` hook)."""
    section = layout.get("stockpiles")
    if section is None:
        return

    def bad(text, path="/stockpiles"):
        problems.append(problem("E_STOCKPILE", f"stockpiles: {text}", path,
                                hint='{"areas": [{"block": "Store", "piles": '
                                     '[{"rect": [x0, y0, x1, y1]}]}]}'))

    if not isinstance(section, dict):
        bad("must be an object")
        return
    for field in sorted(set(section) - _FIELDS):
        bad(f"unknown field '{field}'", f"/stockpiles/{field}")
    if "source" in section and not (isinstance(section["source"], str) and section["source"]):
        bad("source must be a block name", "/stockpiles/source")
    keys = section.get("keys", [])
    if not (isinstance(keys, list) and all(isinstance(k, str) for k in keys)):
        bad("keys must be a list of strings, one per entity created at source", "/stockpiles/keys")
    if keys and not section.get("source"):
        bad("keys need a source block", "/stockpiles/source")
    layers = section.get("max_layers", MAX_LAYERS)
    if not (isinstance(layers, int) and not isinstance(layers, bool) and 1 <= layers <= 50):
        bad("max_layers must be a whole number from 1 to 50", "/stockpiles/max_layers")
    areas = section.get("areas")
    if not isinstance(areas, list):
        bad("areas must be a list", "/stockpiles/areas")
        return
    blocks = set()
    for i, area in enumerate(areas):
        path = f"/stockpiles/areas/{i}"
        if not isinstance(area, dict):
            bad("an area must be an object", path)
            continue
        for field in sorted(set(area) - _AREA_FIELDS):
            bad(f"unknown area field '{field}'", f"{path}/{field}")
        block = area.get("block")
        if not (isinstance(block, str) and block):
            bad("an area needs a block", f"{path}/block")
        elif block in blocks:
            bad(f"block '{block}' has two areas", f"{path}/block")
        blocks.add(block)
        if "source" in area and not (isinstance(area["source"], str) and area["source"]):
            bad("an area's source must be a block name", f"{path}/source")
        own = area.get("keys")
        if own is not None:
            if not (isinstance(own, list) and all(isinstance(k, str) for k in own)):
                bad("an area's keys must be a list of strings, one per entity created at source",
                    f"{path}/keys")
            elif own and not (area.get("source") or section.get("source")):
                bad("an area's keys need a source block (the area's or the section's)",
                    f"{path}/source")
        if "label" in area and not isinstance(area["label"], str):
            bad("label must be a string", f"{path}/label")
        at = area.get("label_at")
        if at is not None and not (isinstance(at, list) and len(at) == 2 and all(map(_num, at))):
            bad("label_at must be [x, y] in metres", f"{path}/label_at")
        if "colour" in area and not (isinstance(area["colour"], str) and _HEX.match(area["colour"])):
            bad("colour must be a \"#RRGGBB\" colour", f"{path}/colour")
        piles = area.get("piles")
        if not (isinstance(piles, list) and piles):
            bad("piles must list at least one pile", f"{path}/piles")
            continue
        for j, pile in enumerate(piles):
            where = f"{path}/piles/{j}"
            if not isinstance(pile, dict):
                bad("a pile must be an object", where)
                continue
            for field in sorted(set(pile) - _PILE_FIELDS):
                bad(f"unknown pile field '{field}'", f"{where}/{field}")
            rect = pile.get("rect")
            if not (isinstance(rect, list) and len(rect) == 4 and all(map(_num, rect))
                    and rect[0] < rect[2] and rect[1] < rect[3]):
                bad("a pile's rect must be [x0, y0, x1, y1] with x0 < x1 and y0 < y1",
                    f"{where}/rect")
            for field in ("key", "label"):
                if field in pile and not isinstance(pile[field], str):
                    bad(f"a pile's {field} must be a string", f"{where}/{field}")


def pile_keys(events, section, area=None) -> dict[int, str]:
    """{entity id: pile key} for the entities created at the `source`, in order: *area*'s own
    keys and source when it has keys (D1146), else the section's."""
    section = section or {}
    if area is not None and area.get("keys") is not None:
        keys, source = list(area["keys"]), area.get("source") or section.get("source")
    else:
        keys, source = list(section.get("keys") or []), section.get("source")
    out: dict[int, str] = {}
    if not keys or not source:
        return out
    k = 0
    for e in sorted(events, key=lambda e: e.t):
        if e.kind == "entity_created" and e.block == source:
            if k < len(keys):
                out[e.entity_id] = keys[k]
            k += 1
    return out


def landings(events, blocks, crane_lifts=None) -> dict[str, list[tuple[float, int]]]:
    """{block: [(t, entity id)] in time order}: when each entity is SEEN to land in a pile
    block. A crane lift's load lands at the end of its "set" phase (cranes.LAND), anything
    else when it enters the block (a move's arrival, or an instant move)."""
    from nagasim.core.cranes import LAND

    blocks = set(blocks)
    lifted = {}
    for lifts in (crane_lifts or {}).values():
        for lf in lifts:
            if lf.dst in blocks:
                lifted[(lf.entity_id, lf.t0)] = lf.s0 + LAND * (lf.s1 - lf.s0)
    out: dict[str, list] = {b: [] for b in blocks}
    for e in events:
        if e.kind == "entity_moved" and e.target in blocks and e.target != e.block:
            t = lifted.get((e.entity_id, e.t), e.t + max(e.duration, 0.0))
            out[e.target].append((t, e.entity_id))
        elif e.kind == "entity_created" and e.block in blocks:
            out[e.block].append((e.t, e.entity_id))
    for rows in out.values():
        rows.sort()
    return out


def stays(events, area: dict, keys: dict, crane_lifts=None) -> list[tuple]:
    """[(entity, pile index, landed, left or None while still there)] of *area*, by landing
    time then entity: an entity is on its pile from when it is SEEN to land (as `landings`)
    until it leaves, a crane's load at its grab (cranes.GRAB). The membership the editor's
    piles count after a lift is drawn (`product/ui/cranes2d.CraneLayer.adjust`), as
    intervals, so the HTML player (Q2) counts and layers a pile by looking a time up."""
    from nagasim.core.cranes import GRAB, LAND

    block = area["block"]
    grab, land = {}, {}
    for lifts in (crane_lifts or {}).values():
        for lf in lifts:
            if lf.src == block and lf.src_slot is not None:
                grab[(lf.entity_id, lf.t0)] = lf.s0 + GRAB * (lf.s1 - lf.s0)
            if lf.dst == block:
                land[(lf.entity_id, lf.t0)] = lf.s0 + LAND * (lf.s1 - lf.s0)
    open_: dict[int, float] = {}
    out = []

    def close(eid, t):
        landed = open_.pop(eid)
        if t > landed:
            out.append((eid, pile_of(area, keys.get(eid)), landed, t))

    for e in sorted(events, key=lambda e: e.t):
        eid = e.entity_id
        if e.kind == "entity_created" and e.block == block:
            open_[eid] = e.t
        elif e.kind == "entity_moved" and e.target and e.target != e.block:
            if e.block == block and eid in open_:
                close(eid, grab.get((eid, e.t), e.t))
            if e.target == block:
                open_[eid] = land.get((eid, e.t), e.t + max(e.duration, 0.0))
        elif e.kind in ("entity_disposed", "entity_absorbed") and e.block == block and eid in open_:
            close(eid, e.t)
    out += [(eid, pile_of(area, keys.get(eid)), landed, None) for eid, landed in open_.items()]
    return sorted(out, key=lambda s: (s[2], s[0]))


def pile_of(area: dict, key: str | None) -> int:
    """The index of the pile an entity with *key* goes on in *area*: its key's pile, else the last."""
    piles = area.get("piles") or []
    for i, pile in enumerate(piles):
        if key is not None and pile.get("key") == key:
            return i
    return max(len(piles) - 1, 0)
