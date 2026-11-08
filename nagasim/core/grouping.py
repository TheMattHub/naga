"""Who is inside what, read from a trace (D1147, D1148): display only, like `cranes` and
`stockpiles`; the run never reads it.

A BATCH or MATCH absorbs its members and then creates the batch, in the same block and instant
(`blocks/_common.make_batch_entity`; the plant log reads the same rule, D461). A carrier BATCH
(D819) loads a member onto a carrier that keeps its identity and names it in the absorption's
`value`. A member stays inside its group until its own next event (a SEPARATE forwards it; in
keep mode one member at a time) or until the group ends. Groups nest: a pallet loaded on a
truck is inside the pallet and the pallet inside the truck.

`Grouping(events)` answers, at any time t:

- `path(entity, t)`: the groups holding *entity*, innermost first, each `(group, slot)` with
  slot the 1-based load position. A follow ring moves to the outermost group drawn and marks
  the slot; it comes back when the entity is separated (an empty path).
- `load(group, t)`: the members inside, in load order.
- `carrier_load(group, t, layout)`: the members with their unload order, FIFO or LIFO by the
  SEPARATE that unloads the group (its `release_order`, D819), as plain JSON-ready data, so
  the editor's carrier view and the HTML player (a later package) show the same thing.

Pure Python over `TraceEvent`-like objects (t, kind, entity_id, e_type, block, target, value).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

__all__ = ["Grouping", "Stay"]

_INF = float("inf")


@dataclass
class Stay:
    """One member's time inside one group."""
    entity: int
    e_type: str
    group: int
    slot: int           # 1-based load position in the group
    t0: float
    t1: float = _INF


class Grouping:
    def __init__(self, events):
        events = sorted(events, key=lambda e: e.t)       # stable: emit order within an instant
        into: dict[int, int] = {}                       # index of an absorption -> its group
        gathering: list = []
        for n, e in enumerate(events):
            if e.kind == "entity_absorbed" and (getattr(e, "value", 0.0) or 0.0) > 0:
                into[n] = int(e.value)
            elif e.kind == "entity_absorbed":
                gathering.append((n, e.block, e.t))
            elif e.kind == "entity_created":
                into.update((m, e.entity_id) for m, block, t in gathering if (block, t) == (e.block, e.t))
                gathering = []
        self.stays: dict[int, list[Stay]] = {}          # entity -> its stays, in time order
        self.members: dict[int, list[Stay]] = {}        # group -> its members' stays, by load
        self.blocks: dict[int, list[tuple[float, str]]] = {}   # entity -> [(t, block entered)]
        open_: dict[int, Stay] = {}
        inside: dict[int, list[Stay]] = {}              # group -> members inside now
        for n, e in enumerate(events):
            if e.kind not in ("entity_created", "entity_moved", "entity_disposed", "entity_absorbed"):
                continue
            eid = e.entity_id
            if e.kind == "entity_created":
                self.blocks.setdefault(eid, []).append((e.t, e.block))
            elif e.kind == "entity_moved" and e.target:
                self.blocks.setdefault(eid, []).append((e.t, e.target))
            stay = open_.pop(eid, None)
            if stay is not None:                        # its own event: it is out again
                stay.t1 = e.t
                inside[stay.group].remove(stay)
            ends = e.kind == "entity_disposed" or (e.kind == "entity_absorbed" and n not in into)
            if ends:                                    # the group is gone: so are its members
                for member in inside.pop(eid, []):
                    member.t1 = e.t
                    open_.pop(member.entity, None)
            if n in into:
                group = into[n]
                now = inside.setdefault(group, [])
                used = {m.slot for m in now}            # a carrier reloaded after unloading
                slot = next(k for k in range(1, len(now) + 2) if k not in used)
                stay = Stay(eid, e.e_type, group, slot, e.t)
                now.append(stay)
                open_[eid] = stay
                self.stays.setdefault(eid, []).append(stay)
                self.members.setdefault(group, []).append(stay)
        self._starts = {eid: [s.t0 for s in stays] for eid, stays in self.stays.items()}

    def stay_at(self, entity: int, t: float) -> Stay | None:
        stays = self.stays.get(entity)
        if not stays:
            return None
        i = bisect.bisect_right(self._starts[entity], t)
        for stay in reversed(stays[:i]):        # an empty stay (in and out in one instant) never is
            if stay.t0 <= t < stay.t1:
                return stay
        return None

    def path(self, entity: int, t: float) -> list[tuple[int, int]]:
        """[(group, slot)] holding *entity* at *t*, innermost first; [] when it is on its own."""
        out, seen = [], {entity}
        stay = self.stay_at(entity, t)
        while stay is not None and stay.group not in seen:
            out.append((stay.group, stay.slot))
            seen.add(stay.group)
            stay = self.stay_at(stay.group, t)
        return out

    def load(self, group: int, t: float) -> list[Stay]:
        """The members inside *group* at *t*, in load order."""
        return [s for s in self.members.get(group, ()) if s.t0 <= t < s.t1]

    def block_at(self, entity: int, t: float) -> str | None:
        """The block *entity* last entered at or before *t* (None before its creation)."""
        rows = self.blocks.get(entity) or []
        i = bisect.bisect_right([r[0] for r in rows], t) - 1
        return rows[i][1] if i >= 0 else None

    def unload_order(self, group: int, t: float, layout: dict | None) -> str:
        """"fifo" or "lifo": the release order of the SEPARATE that unloads *group* (the one
        it is in at *t*, else the next one it enters); "fifo", the engine's default, when the
        trace shows none or no layout is given."""
        separates = {}
        for b in (layout or {}).get("blocks") or []:
            if isinstance(b, dict) and str(b.get("type", "")).upper() == "SEPARATE":
                separates[b.get("name")] = str((b.get("params") or {}).get("release_order") or "fifo")
        rows = self.blocks.get(group) or []
        i = max(bisect.bisect_right([r[0] for r in rows], t) - 1, 0)
        for _t, block in rows[i:]:
            if block in separates:
                return "lifo" if separates[block] == "lifo" else "fifo"
        return "fifo"

    def carrier_load(self, group: int, t: float, layout: dict | None = None) -> dict:
        """What *group* holds at *t*, JSON-ready: {"group", "t", "order", "slots": [{"slot",
        "entity", "type", "loaded", "unloads"}]}, slots in load order and `unloads` 1 for the
        member that comes off first. Nested groups list their own members under "inside"."""
        order = self.unload_order(group, t, layout)
        members = self.load(group, t)
        rank = list(range(1, len(members) + 1))
        if order == "lifo":
            rank.reverse()
        slots = []
        for stay, r in zip(members, rank):
            row = {"slot": stay.slot, "entity": stay.entity, "type": stay.e_type,
                   "loaded": stay.t0, "unloads": r}
            nested = self.load(stay.entity, t)
            if nested:
                row["inside"] = [{"slot": s.slot, "entity": s.entity, "type": s.e_type} for s in nested]
            slots.append(row)
        return {"group": group, "t": t, "order": order, "slots": slots}
