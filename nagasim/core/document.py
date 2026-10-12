"""ModelDocument: the single source of truth for an in-memory model, edited only
through this API (UI_DESIGN.md rule 2). Every mutation pushes one undo snapshot,
clears redo, marks the document dirty, and notifies subscribers. Stdlib + schema
+ block_registry only -- no Qt, no pandas, no simpy (UI_DESIGN.md section 2).

Editing LEVELS (ROADMAP 2.3 part B): a document holds the model's own canvas
plus one canvas per submodel definition. Every block/link/annotation method
takes a `scope` (default "" = the model itself, a definition name otherwise),
and `open_definition(name)` returns a `DefinitionView` that binds that scope --
the same API, the same single undo stack, so the canvas and the properties dock
are reused unchanged for editing inside a definition.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import logging
import os
import re
from pathlib import Path

from nagasim.core import network, schema, sections, submodels
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.expressions import ExpressionError, compile_expression
from nagasim.core.quantities import SECTION_FIELDS, fill_units, time_expressions
from nagasim.core.timeunits import convert, units_of

log = logging.getLogger(__name__)

__all__ = ["ModelDocument", "BlockData", "LinkData", "DefinitionView"]

_MAX_UNDO_DEPTH = 100
# Every simulation default `schema.migrate` sets, so a new model saves what a
# migrated one holds (DEVLOG #215).
_DEFAULT_SIMULATION = {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes",
                       "length_unit": "meters",
                       "currency": "EUR", "energy_price": 0}
# Definition fields kept verbatim for `extends` (core/submodels.py item 13).
_INHERIT_KEYS = ("extends", "remove")
# Undo-label words for a keyed-record section: plural ("agent types") and
# singular ("agent type") -- identifiers read as words, not as names (#291).
_SECTION_WORDS = {s.name: s.name.replace("_", " ") for s in sections.DOCUMENT_LISTS}
_SECTION_ITEM = {s.name: s.item.replace("_", " ") for s in sections.DOCUMENT_LISTS}

# D108: `fn(document, definition) -> {section: [names]} | None`, what a library
# definition owns when its file predates `info["owns"]`. `product.libraries`
# installs it (the installed asset's `requires`); the engine knows no library
# (D56), so without one an old definition's records are left alone.
owns_fallback = None


@dataclasses.dataclass
class BlockData:
    name: str
    type: str
    description: str
    position: tuple[float, float]
    params: dict
    # Where the block sits on the FLOOR PLAN (PHASE4 C3), in layout units, or
    # None when it has none. Separate from `position`, which is the schematic
    # canvas: the two views never fight over where anything is. A block
    # attached to a network node needs neither -- the node is its place.
    layout_position: tuple[float, float] | None = None
    # How the asset is turned on the FLOOR PLAN (PHASE4 T2, D74): degrees
    # clockwise as seen on screen (+x east, +y south), normalised to [0, 360).
    # Layout view only -- the schematic is never rotated.
    layout_rotation: float = 0.0
    # Where the block's NAME label is dragged to (PHASE4 G2, D82), in scene
    # units relative to its default anchor, in the UNROTATED frame. None (the
    # usual case) means the default position.
    label_offset: tuple[float, float] | None = None


@dataclasses.dataclass
class LinkData:
    """One drawn connection. Its transfer delay and its submodel ports live ON
    the record (PHASE4 T4, D77): they used to sit in dicts keyed by
    (source, target), which is what made two links between the same pair
    impossible. Identity is (source, target, index-among-that-pair), derived
    from list order -- the same order that already IS the branch order."""

    source: str
    target: str
    delay: dict | None = None
    # ROADMAP 2.3 part B: {"source_port"/"target_port"} when an endpoint is a
    # SUBMODEL instance; empty otherwise.
    ports: dict = dataclasses.field(default_factory=dict)


def _link_key(key) -> tuple[str, str, int]:
    """(source, target) or (source, target, index) -> a full link key (D77)."""
    src, tgt, *rest = key
    return src, tgt, int(rest[0]) if rest else 0


def _link_entry(link: LinkData) -> dict:
    """A LinkData as the layout dict the file and the engine read."""
    entry = {"source": link.source, "target": link.target}
    if link.delay:
        entry["delay"] = copy.deepcopy(link.delay)
    entry.update(copy.deepcopy(link.ports))
    return entry


class _Level:
    """One editing level's canvas content: the model (scope "") or one submodel
    definition. Plain data -- ModelDocument owns every mutation."""

    __slots__ = ("blocks", "links", "annotations")

    def __init__(self):
        self.blocks: dict[str, BlockData] = {}
        self.links: list[LinkData] = []
        self.annotations: list[dict] = []


class ModelDocument:
    def __init__(self, layout: dict | None = None):
        self.path: Path | None = None
        self.dirty = False
        self._listeners: list = []
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []
        # One label per snapshot, same index (U3c): what undo_labels()/
        # redo_labels() show and what the toolbar's step menu lists.
        self._undo_labels: list[str] = []
        self._redo_labels: list[str] = []
        self._tx_depth = 0            # >0 while inside transaction()
        self._tx_pre_state: dict | None = None
        self._tx_label: str | None = None
        self._tx_events: list[tuple[str, dict]] = []
        # A new model is an empty layout: _reset_from_layout's defaults ARE
        # the new-model values.
        self._reset_from_layout({} if layout is None else layout)

    # -- persistence ----------------------------------------------------
    @classmethod
    def load(cls, path) -> "ModelDocument":
        doc = cls(schema.load_layout(path))   # utf-8 (#253), migrated
        doc.path = Path(path)
        doc.dirty = False
        return doc

    def save(self, path=None) -> None:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("ModelDocument.save: no path given and the document has no path")
        # D159: a saved file names every unit; what a session left "base" is
        # written as the model unit it means now (load fills the same way).
        target.write_text(json.dumps(fill_units(self.to_layout()), indent=2), encoding="utf-8")
        self.path = target
        self.dirty = False

    def to_layout(self) -> dict:
        return {
            "schema_version": schema.CURRENT_VERSION,
            "blocks": self._blocks_layout(""),
            "links": self._links_layout(""),
            "scenarios": copy.deepcopy(self.scenarios),
            "orders": copy.deepcopy(self.orders),
            **{section: copy.deepcopy(getattr(self, section))
               for section in sections.RECORD_SECTIONS},
            # PHASE4 E1: the path network the layout view draws and the travel
            # blocks measure on.
            "network": copy.deepcopy(self.network) if self.network else None,
            "annotations": copy.deepcopy(self.annotations),
            "submodels": self._submodels_layout(),
            "background": copy.deepcopy(self.background) if self.background else None,
            "info": copy.deepcopy(self.info),
            # ROADMAP 3.4: the twin sections, edited by ui/twin.py.
            "data_sources": copy.deepcopy(self.data_sources),
            "data_bindings": copy.deepcopy(self.data_bindings),
            "initial_state": copy.deepcopy(self.initial_state),
            "simulation": copy.deepcopy(self.simulation),
            # PHASE6 W3 M2, ARCHITECTURE D113: opt-in extra output collection
            # ("bottleneck" so far). Empty for every model that predates it.
            "outputs": copy.deepcopy(self.outputs),
            # D172: the investment case's settings; written only when present.
            **({"investment": copy.deepcopy(self.investment)}
               if self.investment is not None else {}),
            # PHASE6 W7 Q3 (D126) assertions; written only when present so a
            # model without any saves byte-identical to before (#226).
            **({"assertions": copy.deepcopy(self.assertions)} if self.assertions else {}),
            # PHASE6 G1 (D132) dashboard; same rule -- written only when a key of it
            # holds something (the Present keys included, D242), so a model that
            # never added one saves byte-identical.
            **({"dashboard": copy.deepcopy(self.dashboard)}
               if any(self.dashboard.values()) else {}),
            # U3d (D155): the editor's view state, set by the editor on save
            # only (never an edit: no undo step, no dirty mark); written only
            # when present, so a model never saved by the editor is unchanged.
            **({"view": copy.deepcopy(self.view)} if self.view else {}),
        }

    def _blocks_layout(self, scope: str) -> list[dict]:
        return [{"name": b.name, "description": b.description, "type": b.type,
                 "position": [b.position[0], b.position[1]],
                 # Written only when the block HAS one, so a model that never
                 # saw the layout view is byte-identical to before (PHASE4 C3).
                 **({"layout_position": [b.layout_position[0], b.layout_position[1]]}
                    if b.layout_position else {}),
                 # Same rule for the angle (D74): written only when turned, so a
                 # model that never rotated anything is byte-identical to before.
                 **({"layout_rotation": b.layout_rotation} if b.layout_rotation else {}),
                 # Same rule for a dragged label (D82): absent = default place.
                 **({"label_offset": [b.label_offset[0], b.label_offset[1]]}
                    if b.label_offset else {}),
                 "params": copy.deepcopy(b.params)}
                for b in self._level(scope).blocks.values()]

    def _links_layout(self, scope: str) -> list[dict]:
        return [_link_entry(link) for link in self._level(scope).links]

    def _submodels_layout(self) -> dict:
        return {
            name: {
                "info": copy.deepcopy(meta.get("info", {})),
                "parameters": copy.deepcopy(meta.get("parameters", [])),
                # Per-instance field overrides (submodels.py item 7); omitted
                # entirely when unused so an untouched definition's layout is
                # byte-identical to before this field existed.
                **({"scoped": copy.deepcopy(meta["scoped"])} if meta.get("scoped") else {}),
                # Inheritance (PHASE6 W7 Q4, D127): the same omit-when-unused rule.
                **{k: copy.deepcopy(meta[k]) for k in _INHERIT_KEYS if meta.get(k) is not None},
                "blocks": self._blocks_layout(name),
                "links": self._links_layout(name),
                "annotations": copy.deepcopy(self._level(name).annotations),
            }
            for name, meta in self.submodels.items()
        }

    def _reset_from_layout(self, layout: dict) -> None:
        self._levels: dict[str, _Level] = {"": self._build_level(layout)}
        self.submodels: dict[str, dict] = {}
        for name, definition in (layout.get("submodels") or {}).items():
            self._levels[name] = self._build_level(definition)
            self.submodels[name] = self._definition_meta(definition)
        # Every keyed list (records, scenarios, orders, the twin sources and
        # bindings) is a plain attribute of the same name.
        for section in sections.DOCUMENT_LISTS:
            setattr(self, section.name, copy.deepcopy(layout.get(section.name, [])))
        self.network: dict | None = copy.deepcopy(layout.get("network")) or None
        self.background: dict | None = copy.deepcopy(layout.get("background")) or None
        self.info: dict = copy.deepcopy(layout.get("info", {}))
        self.initial_state: dict = copy.deepcopy(layout.get("initial_state", {}))
        self.simulation = copy.deepcopy(layout.get("simulation", dict(_DEFAULT_SIMULATION)))
        self.outputs: dict = copy.deepcopy(layout.get("outputs", {}))
        self.investment: dict | None = copy.deepcopy(layout.get("investment"))   # D172
        self.assertions: list = copy.deepcopy(layout.get("assertions", []))      # #226
        self.dashboard: dict = copy.deepcopy(layout.get("dashboard") or {"widgets": []})  # G1
        self.view: dict = copy.deepcopy(layout.get("view") or {})      # U3d

    @staticmethod
    def _definition_meta(definition: dict) -> dict:
        return {
            "info": copy.deepcopy(definition.get("info", {})),
            "parameters": copy.deepcopy(definition.get("parameters", [])),
            **({"scoped": copy.deepcopy(definition["scoped"])} if definition.get("scoped") else {}),
            **{k: copy.deepcopy(definition[k]) for k in _INHERIT_KEYS
               if definition.get(k) is not None},
        }

    @staticmethod
    def _build_level(section: dict) -> _Level:
        level = _Level()
        for b in section.get("blocks", []):
            x, y = b.get("position", [0, 0])
            level.blocks[b["name"]] = BlockData(
                name=b["name"], type=b.get("type", ""), description=b.get("description", b["name"]),
                position=(float(x), float(y)), params=copy.deepcopy(b.get("params", {})),
                layout_position=(tuple(float(v) for v in b["layout_position"])
                                 if b.get("layout_position") else None),
                layout_rotation=network.normalise_rotation(b.get("layout_rotation")),
                label_offset=(tuple(float(v) for v in b["label_offset"])
                              if b.get("label_offset") else None),
            )
        for link in section.get("links", []):
            if isinstance(link, dict):
                level.links.append(LinkData(
                    source=link.get("source"), target=link.get("target"),
                    delay=copy.deepcopy(link["delay"]) if link.get("delay") else None,
                    ports={k: link[k] for k in ("source_port", "target_port") if link.get(k)}))
            else:       # the pre-v8 `[src, tgt]` pair, kept readable here
                level.links.append(LinkData(source=link[0], target=link[1]))
        level.annotations = copy.deepcopy(section.get("annotations", []))
        return level

    # -- levels -------------------------------------------------------------
    def _level(self, scope: str = "") -> _Level:
        level = self._levels.get(scope)
        if level is None:
            raise ValueError(f"unknown submodel definition: {scope}")
        return level

    @property
    def blocks(self) -> dict[str, BlockData]:
        return self._levels[""].blocks

    @property
    def links(self) -> list[tuple[str, str]]:
        """Every link as a (source, target) pair, in branch order. Two parallel
        links between one pair appear twice (D77); `link_records` tells them
        apart."""
        return [(link.source, link.target) for link in self._levels[""].links]

    @property
    def annotations(self) -> list[dict]:
        return self._levels[""].annotations

    # -- read access ------------------------------------------------------
    def link_records(self, *, scope: str = "") -> list[LinkData]:
        """Every link of *scope* as a LinkData copy, in branch order."""
        return copy.deepcopy(self._level(scope).links)

    def out_links(self, name: str, *, scope: str = "") -> list[str]:
        return [link.target for link in self._level(scope).links if link.source == name]

    def in_links(self, name: str, *, scope: str = "") -> list[str]:
        return [link.source for link in self._level(scope).links if link.target == name]

    def link_count(self, src: str, tgt: str, *, scope: str = "") -> int:
        """How many parallel links join this pair (0, 1 or more since D77)."""
        return sum(1 for link in self._level(scope).links
                   if (link.source, link.target) == (src, tgt))

    def _link_at(self, src: str, tgt: str, index: int, scope: str) -> int:
        """Position in `level.links` of the *index*-th link of this pair, or -1."""
        matches = [i for i, link in enumerate(self._level(scope).links)
                   if (link.source, link.target) == (src, tgt)]
        return matches[index] if 0 <= index < len(matches) else -1

    def branch_index(self, src: str, tgt: str, *, index: int = 0, scope: str = "") -> int:
        """Which OUTGOING branch of *src* this link is -- the number a DECIDE's
        `probs[n]`/`conditions[n]` matches. -1 when the link does not exist."""
        position = self._link_at(src, tgt, index, scope)
        if position < 0:
            return -1
        return sum(1 for link in self._level(scope).links[:position] if link.source == src)

    def link_delay(self, src: str, tgt: str, *, scope: str = "", index: int = 0) -> dict | None:
        """The link's transfer delay spec (ROADMAP 1.5), or None for an
        instant transfer -- today's behaviour."""
        position = self._link_at(src, tgt, index, scope)
        if position < 0:
            return None
        return copy.deepcopy(self._level(scope).links[position].delay) or None

    def set_link_delay(self, src: str, tgt: str, spec: dict | None, *, scope: str = "",
                       index: int = 0) -> None:
        """Set (spec) or clear (None) a link's transfer delay: one undo step."""
        link = self._require_link("set_link_delay", src, tgt, index, scope)
        if link.delay == spec or (not link.delay and not spec):
            return
        self._push_undo(f"Edit link delay {src} -> {tgt}")
        link.delay = copy.deepcopy(spec) if spec else None
        self.dirty = True
        self._notify("links", action="delay", src=src, tgt=tgt, index=index, scope=scope)

    def link_ports(self, src: str, tgt: str, *, scope: str = "", index: int = 0) -> dict:
        """`{"source_port"?, "target_port"?}` of a link into/out of a SUBMODEL
        instance (ROADMAP 2.3 part B); empty when the link needs no port."""
        position = self._link_at(src, tgt, index, scope)
        return copy.deepcopy(self._level(scope).links[position].ports) if position >= 0 else {}

    def set_link_ports(self, src: str, tgt: str, *, scope: str = "", index: int = 0,
                       **ports) -> None:
        """Set (or clear, with a falsy value) `source_port`/`target_port`."""
        link = self._require_link("set_link_ports", src, tgt, index, scope)
        updated = {k: v for k, v in {**link.ports, **ports}.items() if v}
        if updated == link.ports:
            return
        self._push_undo(f"Edit link ports {src} -> {tgt}")
        link.ports = updated
        self.dirty = True
        self._notify("links", action="ports", src=src, tgt=tgt, index=index, scope=scope)

    def _require_link(self, caller: str, src: str, tgt: str, index: int, scope: str) -> LinkData:
        position = self._link_at(src, tgt, index, scope)
        if position < 0:
            raise ValueError(f"{caller}: no such link: {src} -> {tgt} (index {index})")
        return self._level(scope).links[position]

    def problems(self) -> list[schema.Problem]:
        """Every Problem the document has (F3): coded, with severity, not the
        string form -- callers wanting text (CLI, ModelValidationError) go
        through `schema.format_problems`. The background-image check lives
        here (not `core/schema/`) because it reads the filesystem next to the
        model file, not the layout dict."""
        probs = schema.validate_model_structured(self.to_layout())
        if self.background:
            path = self.background_abs_path()
            if path is not None and not path.exists():
                probs.append(schema.problem(
                    "W_BACKGROUND_MISSING",
                    f"background image not found: {self.background.get('path')}"))
        return probs

    # -- grouped undo -------------------------------------------------------
    @contextlib.contextmanager
    def transaction(self, label: str | None = None):
        """Group every mutation inside the block into ONE undo snapshot and ONE
        batch of listener notifications, delivered when the outermost
        transaction exits. Nested use (a transaction opened while one is
        already active) is a no-op inner scope: only the outermost call
        snapshots, labels and commits -- an inner call's own *label* is
        ignored, exactly like its own snapshot would be. An exception inside
        rolls the document back to its pre-transaction state (no undo
        snapshot, no notifications) and re-raises.
        """
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return

        self._tx_depth = 1
        self._tx_pre_state = self.to_layout()
        self._tx_label = label
        self._tx_events = []
        try:
            yield
        except BaseException:
            self._reset_from_layout(self._tx_pre_state)
            raise
        else:
            if self._tx_events:  # at least one real mutation happened
                self._commit_undo(self._tx_pre_state, self._tx_label)
                for kind, info in self._tx_events:
                    for fn in list(self._listeners):
                        fn(kind, **info)
        finally:
            self._tx_depth = 0
            self._tx_pre_state = None
            self._tx_label = None
            self._tx_events = []

    # -- undo/redo --------------------------------------------------------
    def _commit_undo(self, snapshot: dict, label: str | None) -> None:
        """Push one undo entry (label default "Edit"), cap the stack, clear redo."""
        self._undo_stack.append(snapshot)
        self._undo_labels.append(label or "Edit")
        del self._undo_stack[: -_MAX_UNDO_DEPTH]   # cap at _MAX_UNDO_DEPTH, drop oldest first
        del self._undo_labels[: -_MAX_UNDO_DEPTH]
        self._redo_stack.clear()
        self._redo_labels.clear()

    def _push_undo(self, label: str | None = None) -> None:
        if self._tx_depth > 0:
            return  # the transaction already snapshotted the pre-transaction state
        self._commit_undo(self.to_layout(), label)

    def undo(self, steps: int = 1) -> str | None:
        """Undo up to *steps* steps (fewer if the stack is shallower). Returns
        the label of the LAST step undone, or None if nothing moved."""
        label = None
        for _ in range(max(steps, 0)):
            if not self._undo_stack:
                break
            self._redo_stack.append(self.to_layout())
            label = self._undo_labels.pop()
            self._redo_labels.append(label)
            self._reset_from_layout(self._undo_stack.pop())
        if label is not None:
            self.dirty = True
            self._notify("reset")
        return label

    def redo(self, steps: int = 1) -> str | None:
        """Redo up to *steps* steps (fewer if the stack is shallower). Returns
        the label of the LAST step redone, or None if nothing moved."""
        label = None
        for _ in range(max(steps, 0)):
            if not self._redo_stack:
                break
            self._undo_stack.append(self.to_layout())
            label = self._redo_labels.pop()
            self._undo_labels.append(label)
            self._reset_from_layout(self._redo_stack.pop())
        if label is not None:
            self.dirty = True
            self._notify("reset")
        return label

    def undo_labels(self) -> list[str]:
        """Every undo step's label, newest (the next `undo()`) first -- what
        the toolbar's drop-down menu lists."""
        return list(reversed(self._undo_labels))

    def redo_labels(self) -> list[str]:
        return list(reversed(self._redo_labels))

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    # -- listeners ----------------------------------------------------------
    def subscribe(self, fn, *, first: bool = False) -> None:
        """*first*: call *fn* before every other listener -- for one that must
        fix up shared state (the editor leaving a removed level, #187) before
        the others read it."""
        if first:
            self._listeners.insert(0, fn)
        else:
            self._listeners.append(fn)

    def unsubscribe(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self, kind: str, **info) -> None:
        if self._tx_depth > 0:
            self._tx_events.append((kind, info))
            return
        for fn in list(self._listeners):
            fn(kind, **info)

    # -- block mutations ------------------------------------------------
    def add_block(self, type_name: str, position: tuple[float, float], name: str | None = None,
                  *, scope: str = "") -> str:
        type_name = type_name.upper()
        if type_name not in BLOCK_REGISTRY:
            raise ValueError(f"unknown block type: {type_name}")
        level = self._level(scope)

        if name is None:
            prefix = type_name.lower()
            pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
            used = {int(m.group(1)) for existing in level.blocks if (m := pattern.match(existing))}
            n = 1
            while n in used:
                n += 1
            name = f"{prefix}_{n}"
        elif not name:
            raise ValueError("block name must not be empty")
        elif name in level.blocks:
            raise ValueError(f"block name already exists: {name}")

        params = {p.name: copy.deepcopy(p.default) for p in BLOCK_REGISTRY[type_name].params}
        self._push_undo("Add block")
        level.blocks[name] = BlockData(name=name, type=type_name, description=name,
                                       position=(float(position[0]), float(position[1])),
                                       params=params)
        self.dirty = True
        self._notify("blocks", action="add", name=name, scope=scope)
        return name

    def remove_blocks(self, names, *, scope: str = "") -> None:
        self.remove_items(blocks=names, scope=scope)

    def remove_items(self, blocks=(), links=(), *, scope: str = "") -> None:
        """Remove the given links and blocks (and the blocks' own links) as ONE
        undo step. Either argument may be empty; unknown names/links are ignored.

        A link key is (source, target) -- the first link of that pair -- or
        (source, target, index) for one parallel link in particular (D77)."""
        level = self._level(scope)
        names = [n for n in blocks if n in level.blocks]
        name_set = set(names)
        link_keys, positions = [], set()
        for key in links:
            src, tgt, index = _link_key(key)
            position = self._link_at(src, tgt, index, scope)
            if position >= 0 and src not in name_set and tgt not in name_set:
                link_keys.append((src, tgt, index))
                positions.add(position)
        if not names and not link_keys:
            return
        definitions = {str(level.blocks[n].params.get("definition", "")) for n in names
                       if level.blocks[n].type == submodels.SUBMODEL}
        count = len(names) + len(link_keys)
        default_label = (f"Delete {names[0]}" if count == 1 and names
                         else "Delete link" if count == 1 else f"Delete {count} items")
        with self.transaction(label=default_label):
            self._push_undo()
            for n in names:
                del level.blocks[n]
            level.links = [link for i, link in enumerate(level.links)
                           if i not in positions
                           and link.source not in name_set and link.target not in name_set]
            self.dirty = True
            if link_keys:
                self._notify("links", action="remove", keys=link_keys, scope=scope)
            if names:
                self._notify("blocks", action="remove", names=names, scope=scope)
            if not scope:
                self._drop_owned_nodes(name_set)
            self._drop_unused_library_definitions(definitions)

    def rename_block(self, old: str, new: str, *, scope: str = "") -> None:
        level = self._level(scope)
        if old not in level.blocks:
            raise ValueError(f"unknown block: {old}")
        if not new:
            raise ValueError("block name must not be empty")
        if new == old:
            return
        if new in level.blocks:
            raise ValueError(f"block name already exists: {new}")

        self._push_undo(f"Rename {new}")
        level.blocks = {
            (new if k == old else k): (dataclasses.replace(v, name=new) if k == old else v)
            for k, v in level.blocks.items()
        }
        for link in level.links:
            if link.source == old:
                link.source = new
            if link.target == old:
                link.target = new
        self.dirty = True
        self._notify("blocks", action="rename", old=old, new=new, scope=scope)

    def move_blocks(self, positions: dict, *, scope: str = "", label: str | None = None) -> None:
        level = self._level(scope)
        changes = {}
        for name, pos in positions.items():
            if name not in level.blocks:
                continue
            pos = (float(pos[0]), float(pos[1]))
            if level.blocks[name].position != pos:
                changes[name] = pos
        if not changes:
            return
        self._push_undo(label or (f"Move {next(iter(changes))}" if len(changes) == 1
                                  else f"Move {len(changes)} blocks"))
        for name, pos in changes.items():
            level.blocks[name] = dataclasses.replace(level.blocks[name], position=pos)
        self.dirty = True
        self._notify("blocks", action="move", names=list(changes), scope=scope)

    def set_layout_positions(self, positions: dict, *, scope: str = "") -> None:
        """Move blocks on the FLOOR PLAN (PHASE4 C3), one undo step for the
        whole drag -- the layout twin of `move_blocks`. A value of None drops
        the block's layout position, which is what attaching it to a network
        node does.

        An asset that brought its own geometry (ARCHITECTURE D69) owns the
        nodes named `<block>/<node>`, and they move WITH it: the whole drag,
        block and nodes, is one undo step."""
        level = self._level(scope)
        changed = {name: value for name, value in positions.items()
                   if name in level.blocks
                   and (level.blocks[name].layout_position or None)
                   != (tuple(value) if value else None)}
        if not changed:
            return
        deltas = {}
        for name, value in changed.items():
            before = level.blocks[name].layout_position
            if before and value:
                deltas[name] = (float(value[0]) - before[0], float(value[1]) - before[1])
        label = (f"Move {next(iter(changed))}" if len(changed) == 1
                else f"Move {len(changed)} blocks")
        with self.transaction(label=label):
            self._push_undo()
            for name, value in changed.items():
                block = level.blocks[name]
                level.blocks[name] = dataclasses.replace(
                    block, layout_position=(float(value[0]), float(value[1])) if value else None)
            self.dirty = True
            self._notify("blocks", action="layout", names=list(changed), scope=scope)
            if deltas and not scope:
                self._shift_owned_nodes(deltas)

    def set_label_offsets(self, offsets: dict, *, scope: str = "") -> None:
        """Move block NAME labels (PHASE4 G2, D82), one undo step for the whole
        drag -- the label twin of `move_blocks`. The value is (dx, dy) in scene
        units from the label's default anchor, in the UNROTATED frame; None
        puts the label back where it started."""
        level = self._level(scope)
        changed = {name: (float(value[0]), float(value[1])) if value else None
                   for name, value in offsets.items() if name in level.blocks}
        changed = {name: value for name, value in changed.items()
                   if (level.blocks[name].label_offset or None) != value}
        if not changed:
            return
        self._push_undo(f"Move {next(iter(changed))} label" if len(changed) == 1
                        else f"Move {len(changed)} labels")
        for name, value in changed.items():
            level.blocks[name] = dataclasses.replace(level.blocks[name], label_offset=value)
        self.dirty = True
        self._notify("blocks", action="label", names=list(changed), scope=scope)

    def set_layout_rotations(self, rotations: dict, *, scope: str = "") -> None:
        """Turn blocks on the FLOOR PLAN (PHASE4 T2, D74), one undo step for the
        whole selection. Degrees clockwise on screen; anything is accepted and
        folded into [0, 360), but the UI offers quarter turns.

        An asset that brought its own geometry (D69) has its `<block>/<node>`
        positions rewritten here, because the `network` holds ABSOLUTE positions
        and a model has to stay self-contained. They are recomputed from the
        fragment offsets stored with the definition every time -- never by
        rotating the current positions -- so rotating back and forth lands
        exactly where it started."""
        level = self._level(scope)
        changed = {name: network.normalise_rotation(value)
                   for name, value in rotations.items() if name in level.blocks}
        changed = {name: value for name, value in changed.items()
                   if level.blocks[name].layout_rotation != value}
        if not changed:
            return
        label = (f"Rotate {next(iter(changed))}" if len(changed) == 1
                else f"Rotate {len(changed)} blocks")
        with self.transaction(label=label):
            self._push_undo()
            for name, value in changed.items():
                level.blocks[name] = dataclasses.replace(level.blocks[name],
                                                         layout_rotation=value)
            self.dirty = True
            self._notify("blocks", action="layout", names=list(changed), scope=scope)
            if not scope:
                self._place_owned_nodes(changed)

    def _place_owned_nodes(self, blocks: dict) -> None:
        """Put each block's own nodes back at `layout_position + R(angle).offset`
        (D74), reading the offsets from the geometry fragment its definition
        carries. Blocks with no fragment, or no place on the floor, are left
        alone."""
        offsets: dict[str, tuple[float, float]] = {}
        for block_name, angle in blocks.items():
            block = self._level("").blocks.get(block_name)
            if block is None or not block.layout_position:
                continue
            fragment = self.definition_info(str(block.params.get("definition", "")))
            nodes = (fragment.get("geometry") or {}).get("nodes") or []
            for node in nodes:
                dx, dy = network.rotate_offset(float(node.get("dx", 0.0) or 0.0),
                                               float(node.get("dy", 0.0) or 0.0), angle)
                offsets[f"{block_name}/{node.get('name')}"] = (
                    round(block.layout_position[0] + dx, 3),
                    round(block.layout_position[1] + dy, 3))
        if not offsets:
            return
        placed = [{**node, "x": offsets[node["name"]][0], "y": offsets[node["name"]][1]}
                  if node.get("name") in offsets else node
                  for node in self.network_nodes()]
        self.set_network({**(self.network or {}), "nodes": placed,
                          "segments": self.network_segments()})

    def set_params(self, name: str, params: dict, *, scope: str = "") -> None:
        level = self._level(scope)
        if name not in level.blocks:
            raise ValueError(f"unknown block: {name}")
        if level.blocks[name].params == params:
            return
        self._push_undo(f"Edit {name}")
        level.blocks[name] = dataclasses.replace(level.blocks[name], params=copy.deepcopy(params))
        self.dirty = True
        self._notify("params", name=name, scope=scope)

    def set_description(self, name: str, text: str, *, scope: str = "") -> None:
        level = self._level(scope)
        if name not in level.blocks:
            raise ValueError(f"unknown block: {name}")
        if level.blocks[name].description == text:
            return
        self._push_undo(f"Edit {name} description")
        level.blocks[name] = dataclasses.replace(level.blocks[name], description=text)
        self.dirty = True
        self._notify("params", name=name, scope=scope)

    # -- link mutations -----------------------------------------------------
    def add_link(self, src: str, tgt: str, *, scope: str = "",
                 source_port: str | None = None, target_port: str | None = None) -> int:
        """Draw a link and return its index among the links of that pair (D77):
        0 for the first, 1 for a second parallel one.

        A second link between the same pair is allowed when the source can
        branch -- a DECIDE with two branches to one target, a SEIZE whose
        "left without service" exit meets its normal one. For a single-output
        block the very same connection twice can only be a double drag, so it
        is refused."""
        level = self._level(scope)
        if src not in level.blocks or tgt not in level.blocks:
            raise ValueError("add_link: unknown block")
        if src == tgt:
            raise ValueError("add_link: self-links are not allowed")
        ports = {k: v for k, v in (("source_port", source_port),
                                   ("target_port", target_port)) if v}
        existing = [link for link in level.links
                    if (link.source, link.target) == (src, tgt) and link.ports == ports]
        if existing and not self._can_branch(level.blocks[src].type):
            raise ValueError(f"add_link: link already exists: {src} -> {tgt}")
        self._push_undo(f"Add link {src} -> {tgt}")
        # Appended at the end = last branch (section 5.5).
        level.links.append(LinkData(source=src, target=tgt, ports=ports))
        index = self.link_count(src, tgt, scope=scope) - 1
        self.dirty = True
        self._notify("links", action="add", src=src, tgt=tgt, index=index, scope=scope)
        return index

    @staticmethod
    def _can_branch(block_type: str) -> bool:
        """Whether a block may have several outgoing links at all: DECIDE,
        LOGIC and any multi_output plugin, plus the queueing blocks' second
        "left without service" output (ARCHITECTURE D54)."""
        spec = BLOCK_REGISTRY.get(block_type)
        return ((spec is not None and spec.multi_output)
                or block_type in schema.QUEUEING_BLOCK_TYPES)

    def remove_link(self, src: str, tgt: str, *, scope: str = "", index: int = 0) -> None:
        level = self._level(scope)
        position = self._link_at(src, tgt, index, scope)
        if position < 0:
            return
        self._push_undo(f"Delete link {src} -> {tgt}")
        del level.links[position]
        self.dirty = True
        self._notify("links", action="remove", src=src, tgt=tgt, index=index, scope=scope)

    def reorder_link(self, src: str, old_index: int, new_index: int, *, scope: str = "") -> None:
        """Move the old_index-th outgoing link of *src* to new_index, both
        indices within out_links(src). Other blocks' links are untouched."""
        if old_index == new_index:
            return
        level = self._level(scope)
        positions = [i for i, link in enumerate(level.links) if link.source == src]
        group = [level.links[i] for i in positions]
        item = group.pop(old_index)  # IndexError on an out-of-range index, like list
        group.insert(new_index, item)
        self._push_undo(f"Reorder {src} links")
        for pos, link in zip(positions, group):
            level.links[pos] = link
        self.dirty = True
        self._notify("links", action="reorder", src=src, scope=scope)

    # -- keyed record lists ---------------------------------------------------
    # One set/upsert/remove/rename for every core/sections.py row with an
    # `item`; the per-section names (set_resources, upsert_table, ...) are
    # generated aliases (see _record_aliases below). One undo step each.
    def set_records(self, section: str, records: list, *, label: str | None = None) -> None:
        if records == getattr(self, section):
            return
        self._push_undo(label or f"Edit {_SECTION_WORDS.get(section, section)}")
        setattr(self, section, copy.deepcopy(records))
        self.dirty = True
        self._notify("tables", table=section)

    def upsert_record(self, section: str, entry, *, label: str | None = None) -> None:
        """Add *entry*, or replace the one with the same key (in place). A
        record object with `to_dict` (core/sd.py, core/agents/) is accepted."""
        entry = entry.to_dict() if hasattr(entry, "to_dict") else entry
        key = sections.KEY[section]
        records = copy.deepcopy(getattr(self, section))
        index = next((i for i, r in enumerate(records) if r.get(key) == entry.get(key)), None)
        if index is None:
            records.append(copy.deepcopy(entry))
        else:
            records[index] = copy.deepcopy(entry)
        self.set_records(section, records, label=label or f"Edit {_SECTION_ITEM.get(section, section)}")

    def remove_record(self, section: str, key_value: str, *, label: str | None = None) -> None:
        key = sections.KEY[section]
        self.set_records(section, [r for r in getattr(self, section) if r.get(key) != key_value],
                         label=label or f"Delete {key_value}")

    def rename_record(self, section: str, old: str, new: str, *, label: str | None = None) -> None:
        """Rename one entry. References to it elsewhere (TABLE("old"), a flow's
        `from`, a CREATE's sequence, ...) are NOT rewritten -- no section
        rewrites a name it does not own -- so validation reports them."""
        if not new or new == old:
            return
        key = sections.KEY[section]
        self.set_records(section, [{**r, key: new} if r.get(key) == old else r
                                   for r in getattr(self, section)],
                         label=label or f"Rename {new}")

    # -- path network and mover fleets (PHASE4 E1) ---------------------------
    # `network` is ONE object ({"nodes", "segments", "length_factor"}) rather
    # than a list, because a node and the segments joining it are edited
    # together; `fleets` is an ordinary named-record list, so it reuses the
    # four helpers above unchanged.
    def set_network(self, network: dict | None, *, label: str | None = None) -> None:
        network = copy.deepcopy(network) or None
        if network == self.network:
            return
        self._push_undo(label or "Edit network")
        self.network = network
        self.dirty = True
        self._notify("tables", table="network")

    def network_nodes(self) -> list:
        return list((self.network or {}).get("nodes") or [])

    def network_segments(self) -> list:
        return list((self.network or {}).get("segments") or [])

    def upsert_node(self, entry: dict) -> None:
        """Add a node, or move/replace the one with the same name."""
        nodes = copy.deepcopy(self.network_nodes())
        name = entry.get("name")
        index = next((i for i, n in enumerate(nodes) if n.get("name") == name), None)
        if index is None:
            nodes.append(copy.deepcopy(entry))
        else:
            nodes[index] = copy.deepcopy(entry)
        self.set_network({**(self.network or {}), "nodes": nodes,
                          "segments": self.network_segments()}, label=f"Edit node {name}")

    def remove_node(self, name: str) -> None:
        """Remove a node AND every segment that used it -- a segment with a
        dangling end is not a thing the model can mean."""
        nodes = [n for n in self.network_nodes() if n.get("name") != name]
        segments = [s for s in self.network_segments()
                    if s.get("from") != name and s.get("to") != name]
        self.set_network({**(self.network or {}), "nodes": nodes, "segments": segments},
                         label=f"Delete node {name}")

    def rename_node(self, old: str, new: str) -> None:
        """Rename a node, rewriting the segments and the blocks attached to it:
        unlike a table or a sequence name, a node name is pure geometry, so
        there is no expression to leave pointing at the old one."""
        if not new or new == old or old == new:
            return
        with self.transaction(label=f"Rename node {new}"):
            nodes = [{**n, "name": new} if n.get("name") == old else n
                     for n in self.network_nodes()]
            segments = [{**s,
                         **({"from": new} if s.get("from") == old else {}),
                         **({"to": new} if s.get("to") == old else {})}
                        for s in self.network_segments()]
            self.set_network({**(self.network or {}), "nodes": nodes, "segments": segments})
            for block in list(self.blocks.values()):
                params = block.params
                touched = {key: new for key in ("node", "from_node", "to_node")
                           if params.get(key) == old}
                if touched:
                    self.set_params(block.name, {**params, **touched})
            self.set_fleets([{**f, "home": new} if f.get("home") == old else f
                             for f in self.fleets])

    def upsert_segment(self, entry: dict) -> None:
        """Add a segment, or replace the one joining the same two nodes."""
        segments = copy.deepcopy(self.network_segments())
        key = (entry.get("from"), entry.get("to"))
        index = next((i for i, s in enumerate(segments)
                      if (s.get("from"), s.get("to")) == key), None)
        if index is None:
            segments.append(copy.deepcopy(entry))
        else:
            segments[index] = copy.deepcopy(entry)
        self.set_network({**(self.network or {}), "nodes": self.network_nodes(),
                          "segments": segments},
                         label=f"Edit segment {key[0]} -> {key[1]}")

    def owned_nodes(self, block: str) -> list[str]:
        """The network nodes a block brought with it (ARCHITECTURE D69).

        Ownership is the NAME: an asset's geometry fragment is merged under
        `<instance block>/<node>`, the same scoping `$` records already use, so
        nothing extra has to be stored in the model file to know whose node a
        node is."""
        prefix = f"{block}/"
        return [str(n.get("name", "")) for n in self.network_nodes()
                if str(n.get("name", "")).startswith(prefix)]

    def _shift_owned_nodes(self, deltas: dict) -> None:
        nodes, touched = [], False
        for node in self.network_nodes():
            owner, sep, _ = str(node.get("name", "")).partition("/")
            delta = deltas.get(owner) if sep else None
            if delta is None:
                nodes.append(node)
                continue
            touched = True
            nodes.append({**node, "x": round(float(node.get("x", 0.0)) + delta[0], 3),
                          "y": round(float(node.get("y", 0.0)) + delta[1], 3)})
        if touched:
            self.set_network({**(self.network or {}), "nodes": nodes,
                              "segments": self.network_segments()})

    def _drop_owned_nodes(self, blocks: set) -> None:
        gone = {name for block in blocks for name in self.owned_nodes(block)}
        if not gone:
            return
        self.set_network({
            **(self.network or {}),
            "nodes": [n for n in self.network_nodes() if n.get("name") not in gone],
            "segments": [s for s in self.network_segments()
                         if s.get("from") not in gone and s.get("to") not in gone]})

    def remove_segment(self, source: str, target: str) -> None:
        self.set_network({**(self.network or {}), "nodes": self.network_nodes(),
                          "segments": [s for s in self.network_segments()
                                       if (s.get("from"), s.get("to")) != (source, target)]},
                         label=f"Delete segment {source} -> {target}")

    # -- digital twin sections (ROADMAP 3.4) ---------------------------------
    def set_initial_state(self, state: dict) -> None:
        """The warm start (ROADMAP 3.4b): entities already in the system,
        variable values and resource states the run begins with."""
        state = state or {}
        if state == self.initial_state:
            return
        self._push_undo("Edit initial state")
        self.initial_state = copy.deepcopy(state)
        self.dirty = True
        self._notify("tables", table="initial_state")

    def set_simulation(self, **kwargs) -> None:
        updated = {**self.simulation, **kwargs}
        if updated == self.simulation:
            return
        self._push_undo("Edit simulation settings")
        self.simulation = updated
        self.dirty = True
        self._notify("simulation", **kwargs)

    def set_model_units(self, *, time_unit: str | None = None, length_unit: str | None = None,
                        currency: str | None = None) -> list[str]:
        """Model settings (D160), one undo step. Every unit a field leaves
        missing or "base" is first pinned to the CURRENT model unit, so no field
        changes meaning; then the run length, warm-up and sampling interval are
        converted and the new units written. Results come out in the new unit.
        Returns the expressions that read time in the model unit when the time
        unit changes (`quantities.time_expressions`): they are NOT converted and
        do change meaning -- the dialog asks before calling this."""
        old = units_of(self.simulation)
        new = old._replace(**{k: v for k, v in (("time", time_unit), ("length", length_unit),
                                                ("currency", currency)) if v})
        if new == old:
            return []
        layout = self.to_layout()
        found = time_expressions(layout) if new.time != old.time else []
        if found:
            log.warning("model time unit %s -> %s: %d expression(s) read time in the model "
                        "unit and change meaning: %s", old.time, new.time, len(found),
                        "; ".join(found))
        filled = fill_units(copy.deepcopy(layout))
        # A submodel definition is a template that fill_units leaves alone at
        # load; here its units are pinned like the top level's (#302).
        scopes = [("", filled)] + [
            (name, fill_units({**copy.deepcopy(definition), "simulation": layout["simulation"]}))
            for name, definition in (layout.get("submodels") or {}).items()]
        with self.transaction(label="Model settings"):
            for scope, after in scopes:
                params = {b["name"]: b["params"] for b in after["blocks"]}
                for name, block in list(self._level(scope).blocks.items()):
                    if params.get(name, block.params) != block.params:
                        self.set_params(name, params[name], scope=scope)
                seen: dict = {}
                for record, link in zip(after["links"], self.link_records(scope=scope)):
                    pair = (link.source, link.target)
                    index = seen[pair] = seen.get(pair, -1) + 1
                    if record.get("delay") != link.delay:
                        self.set_link_delay(link.source, link.target, record.get("delay"),
                                            scope=scope, index=index)
            for section in (*sections.RECORD_SECTIONS, "orders", "scenarios"):
                if filled.get(section) != layout.get(section):
                    self.set_records(section, filled[section])
            if filled.get("network") != layout.get("network"):
                self.set_network(filled["network"])
            if filled.get("initial_state") != layout.get("initial_state"):
                self.set_initial_state(filled["initial_state"])
            sim = dict(filled["simulation"])
            for spec in SECTION_FIELDS["simulation"]:
                value = sim.get(spec.name)
                if (spec.quantity == "time" and isinstance(value, (int, float))
                        and not isinstance(value, bool)):
                    sim[spec.name] = convert(value, "time", old.time, new.time)
            sim.update(time_unit=new.time, length_unit=new.length, currency=new.currency)
            self.set_simulation(**sim)
        return found

    def set_sd_settings(self, sd) -> None:
        """`simulation.sd` (PHASE5 SD-1): a dict or an `sd.SDSettings`, or None
        to drop the key -- absent, not null, so the model is exactly a v20 one."""
        if sd is not None and hasattr(sd, "to_dict"):
            sd = sd.to_dict()
        updated = {k: v for k, v in self.simulation.items() if k != "sd"}
        if sd is not None:
            updated["sd"] = dict(sd)
        if updated == self.simulation:
            return
        self._push_undo("Edit system dynamics settings")
        self.simulation = updated
        self.dirty = True
        self._notify("simulation", sd=sd)

    def set_bottleneck_output(self, enabled: bool) -> None:
        """`outputs.bottleneck` (PHASE6 W3 M2, ARCHITECTURE D113): True adds the
        key, False drops the whole "outputs" section when nothing else is in
        it -- absent, not false, so a model that never turned this on is
        byte-identical to before the feature existed (the `sd` precedent)."""
        updated = dict(self.outputs)
        if enabled:
            updated["bottleneck"] = True
        else:
            updated.pop("bottleneck", None)
        if updated == self.outputs:
            return
        self._push_undo("Enable bottleneck output" if enabled else "Disable bottleneck output")
        self.outputs = updated
        self.dirty = True
        self._notify("outputs", bottleneck=enabled)

    def set_schedule_output(self, enabled: bool) -> None:
        """`outputs.schedule` (PHASE6 W4 P2, ARCHITECTURE D120): same shape as
        `set_bottleneck_output` above -- True adds the key, False drops the
        whole "outputs" section when nothing else is in it. Deterministic mode
        (`simulation.deterministic`) forces schedule collection on regardless
        of this flag, but does not set it: the two stay independent, so
        turning determinism back off does not silently lose an explicit
        "always collect the schedule" choice."""
        updated = dict(self.outputs)
        if enabled:
            updated["schedule"] = True
        else:
            updated.pop("schedule", None)
        if updated == self.outputs:
            return
        self._push_undo("Enable schedule output" if enabled else "Disable schedule output")
        self.outputs = updated
        self.dirty = True
        self._notify("outputs", schedule=enabled)

    def set_investment(self, record: dict | None) -> None:
        """The `investment` section (D172): a dict (empty = every default), or
        None to remove it -- absent, not empty, so a model without an
        investment case saves as before."""
        record = None if record is None else copy.deepcopy(record)
        if record == self.investment:
            return
        self._push_undo("Edit investment case" if record is not None
                        else "Remove investment case")
        self.investment = record
        self.dirty = True
        self._notify("investment")

    # -- annotations (ROADMAP 2.3 part A) ------------------------------------
    # Text notes and frames drawn behind blocks; the engine ignores this
    # section entirely (core/schema/ checks shape only). Each entry carries its
    # own "id" (auto-named like a block: "<kind>_<n>", smallest free number)
    # and "kind" ("text" or "frame"); the remaining keys are free-form fields
    # the UI reads/writes (position/text/font_size/color for text,
    # rect/title/color/opacity for frame).
    def _next_annotation_id(self, kind: str, scope: str = "") -> str:
        pattern = re.compile(rf"^{re.escape(kind)}_(\d+)$")
        used = {int(m.group(1)) for a in self._level(scope).annotations
                if (m := pattern.match(a.get("id", "")))}
        n = 1
        while n in used:
            n += 1
        return f"{kind}_{n}"

    def add_annotation(self, kind: str, *, scope: str = "", **fields) -> str:
        if kind not in ("text", "frame"):
            raise ValueError(f"unknown annotation kind: {kind}")
        level = self._level(scope)
        ann_id = self._next_annotation_id(kind, scope)
        entry = {"id": ann_id, "kind": kind, **fields}
        self._push_undo(f"Add {kind}")
        level.annotations.append(entry)
        self.dirty = True
        self._notify("annotations", action="add", id=ann_id, scope=scope)
        return ann_id

    def update_annotation(self, ann_id: str, *, scope: str = "", **fields) -> None:
        level = self._level(scope)
        index = next((i for i, a in enumerate(level.annotations) if a.get("id") == ann_id), None)
        if index is None:
            raise ValueError(f"unknown annotation: {ann_id}")
        current = level.annotations[index]
        if all(current.get(k) == v for k, v in fields.items()):
            return
        self._push_undo(f"Edit {current.get('kind', 'annotation')}")
        level.annotations[index] = {**current, **fields}
        self.dirty = True
        self._notify("annotations", action="update", id=ann_id, scope=scope)

    def remove_annotations(self, ids, *, scope: str = "") -> None:
        level = self._level(scope)
        id_set = set(ids)
        kept = [a for a in level.annotations if a.get("id") not in id_set]
        if len(kept) == len(level.annotations):
            return
        label = (f"Delete {next(iter(id_set))}" if len(id_set) == 1
                else f"Delete {len(id_set)} annotations")
        self._push_undo(label)
        level.annotations = kept
        self.dirty = True
        self._notify("annotations", action="remove", ids=list(id_set), scope=scope)

    # -- submodel definitions (ROADMAP 2.3 part B) ---------------------------
    def open_definition(self, name: str) -> "DefinitionView":
        """A scoped editing handle for definition *name*: the same block/link/
        annotation API as this document, on the same undo stack."""
        self._level(name)       # raises for an unknown definition
        return DefinitionView(self, name)

    def add_definition(self, name: str, parameters: list | None = None,
                       description: str = "", scoped: dict | None = None) -> str:
        if not name or not str(name).strip():
            raise ValueError("submodel definition name must not be empty")
        if name in self._levels:
            raise ValueError(f"submodel definition already exists: {name}")
        self._push_undo(f"Add submodel {name}")
        self._levels[name] = _Level()
        self.submodels[name] = {"info": {"description": description} if description else {},
                                "parameters": copy.deepcopy(parameters or [])}
        if scoped:
            self.submodels[name]["scoped"] = copy.deepcopy(scoped)
        self.dirty = True
        self._notify("submodels", action="add", name=name)
        return name

    def restore_definition(self, name: str, section: dict) -> None:
        """Add definition *name* whole from its `to_layout()["submodels"]`
        section (a pasted asset instance brings its definition, #188)."""
        if name in self._levels:
            raise ValueError(f"submodel definition already exists: {name}")
        self._push_undo(f"Add submodel {name}")
        self._levels[name] = self._build_level(section)
        self.submodels[name] = self._definition_meta(section)
        self.dirty = True
        self._notify("submodels", action="add", name=name)

    def rename_definition(self, old: str, new: str) -> None:
        self._level(old)
        if not new:
            raise ValueError("submodel definition name must not be empty")
        if new == old:
            return
        if new in self._levels:
            raise ValueError(f"submodel definition already exists: {new}")
        self._push_undo(f"Rename submodel {new}")
        self._levels = {(new if k == old else k): v for k, v in self._levels.items()}
        self.submodels = {(new if k == old else k): v for k, v in self.submodels.items()}
        for scope, level in self._levels.items():
            for block in level.blocks.values():
                if block.type == submodels.SUBMODEL and block.params.get("definition") == old:
                    block.params["definition"] = new
        for meta in self.submodels.values():
            if meta.get("extends") == old:
                meta["extends"] = new
        self.dirty = True
        self._notify("submodels", action="rename", old=old, new=new)

    def instances_of(self, name: str) -> list[tuple[str, str]]:
        """Every instance of definition *name* as (scope, block name)."""
        return [(scope, block.name)
                for scope, level in self._levels.items()
                for block in level.blocks.values()
                if block.type == submodels.SUBMODEL and block.params.get("definition") == name]

    def remove_definition(self, name: str) -> None:
        self._level(name)
        used = self.instances_of(name)
        if used:
            where = ", ".join(b if s == "" else f"{s}/{b}" for s, b in used)
            raise ValueError(f"submodel '{name}' is still instantiated by: {where}")
        children = sorted(n for n, meta in self.submodels.items() if meta.get("extends") == name)
        if children:
            raise ValueError(f"submodel '{name}' is still extended by: {', '.join(children)}")
        self._push_undo(f"Delete submodel {name}")
        del self._levels[name]
        self.submodels.pop(name, None)
        self.dirty = True
        self._notify("submodels", action="remove", name=name)

    # -- library ownership (D108) --------------------------------------------
    def definition_owns(self, name: str) -> dict | None:
        """`{section: [names]}`: the model-level records definition *name*
        added when it was placed from a library, or None when that is unknown
        (a file older than `info["owns"]` with no library to ask)."""
        info = self.submodels.get(name, {}).get("info", {})
        if "owns" in info:
            return copy.deepcopy(info["owns"])
        return owns_fallback(self, name) if owns_fallback and info.get("library") else None

    def drop_owned_records(self, definitions) -> None:
        """Remove the records *definitions* own, except those another
        definition also owns. Records nobody owns (the user's own) stay."""
        definitions = set(definitions)
        shared: dict[str, set] = {}
        for other in self.submodels:
            if other not in definitions:
                for section, names in (self.definition_owns(other) or {}).items():
                    shared.setdefault(section, set()).update(names)
        gone: dict[str, set] = {}
        for name in definitions:
            for section, names in (self.definition_owns(name) or {}).items():
                gone.setdefault(section, set()).update(set(names) - shared.get(section, set()))
        for section in sections.RECORD_SECTIONS:
            if gone.get(section):
                getattr(self, f"set_{section}")(
                    [r for r in getattr(self, section) if r.get("name") not in gone[section]])

    def _drop_unused_library_definitions(self, candidates: set) -> None:
        """A library definition whose last instance just went goes too, with
        the records it owns; so does a definition only it instantiated."""
        while candidates:
            unused = {name for name in candidates
                      if name in self.submodels and not self.instances_of(name)
                      and self.submodels[name].get("info", {}).get("library")
                      and self.definition_owns(name) is not None}
            if not unused:
                return
            candidates = {str(block.params.get("definition", ""))
                          for name in unused for block in self._level(name).blocks.values()
                          if block.type == submodels.SUBMODEL} - unused
            self.drop_owned_records(unused)
            for name in sorted(unused):
                self.remove_definition(name)    # its inner instances go with it

    def set_definition_parameters(self, name: str, parameters: list) -> None:
        """Replace a definition's parameter list ([{"name", "default",
        "description"?}]): one undo step."""
        meta = self.submodels.get(name)
        if meta is None:
            raise ValueError(f"unknown submodel definition: {name}")
        if meta.get("parameters") == parameters:
            return
        self._push_undo(f"Edit {name} parameters")
        meta["parameters"] = copy.deepcopy(parameters)
        self.dirty = True
        self._notify("submodels", action="parameters", name=name)

    def definition_parameters(self, name: str) -> list:
        return copy.deepcopy(self.submodels.get(name, {}).get("parameters", []))

    def effective_parameters(self, name: str) -> list:
        """The parameters an instance of *name* takes: its own plus those it
        inherits through `extends` (D127) -- what the instance form offers.
        `definition_parameters` stays the definition's OWN list, the one its
        parameter dialog edits."""
        if self.submodels.get(name, {}).get("extends") is None:
            return self.definition_parameters(name)
        return copy.deepcopy(self._effective_definition(name).get("parameters", []))

    def _effective_definition(self, name: str) -> dict:
        """Definition *name* as flattening sees it (`extends` resolved)."""
        return submodels.resolve_extends(self._submodels_layout())[0].get(name) or {}

    def set_definition_info(self, name: str, info: dict) -> None:
        """Replace a definition's free-form `info` block: one undo step.

        `info` holds the description plus, for a definition placed from an
        asset library (PHASE4 B1), its `library` provenance and its `graphics`
        -- both carried in the model file so it stays self-contained on a
        machine with no library installed."""
        meta = self.submodels.get(name)
        if meta is None:
            raise ValueError(f"unknown submodel definition: {name}")
        if meta.get("info") == info:
            return
        self._push_undo(f"Edit {name} info")
        meta["info"] = copy.deepcopy(info)
        self.dirty = True
        self._notify("submodels", action="info", name=name)

    def definition_info(self, name: str) -> dict:
        return copy.deepcopy(self.submodels.get(name, {}).get("info", {}))

    def definition_ports(self, name: str, side: str) -> list[str]:
        """Port names of definition *name* on "in" (SUB_ENTRY blocks) or "out"
        (SUB_EXIT), in block order; empty for an unknown definition."""
        level = self._levels.get(name)
        if level is None:
            return []
        if self.submodels.get(name, {}).get("extends") is not None:
            # An inheriting definition's ports may all come from its parent.
            return submodels.definition_ports(self._effective_definition(name), side)
        want = submodels.PORT_TYPES[side]
        return [str(b.params.get("port", "")) for b in level.blocks.values() if b.type == want]

    # -- selection <-> submodel (ROADMAP 2.3 part B item 6) ------------------
    def create_submodel_from_selection(self, block_names, def_name: str, *, scope: str = "",
                                       instance_name: str | None = None) -> str:
        """Move *block_names* into a new definition *def_name* and replace them
        with one instance, as ONE undo step. Every link crossing the selection
        boundary becomes a port named after the block on the OUTSIDE.

        Returns the new instance's block name. Refuses when one outside block
        would need two links to the instance: a port is named after the block
        on the outside, so two of them would collide. Names the blocks, so the
        selection can be widened to include the fan-out point.
        """
        level = self._level(scope)
        names = [n for n in block_names if n in level.blocks]
        if not names:
            raise ValueError("create_submodel_from_selection: nothing selected")
        if def_name in self._levels:
            raise ValueError(f"submodel definition already exists: {def_name}")
        inside = set(names)
        incoming = [(k.source, k.target) for k in level.links
                    if k.target in inside and k.source not in inside]
        outgoing = [(k.source, k.target) for k in level.links
                    if k.source in inside and k.target not in inside]
        internal = [k for k in level.links if k.source in inside and k.target in inside]
        for label, boundary, index in (("into", incoming, 0), ("out of", outgoing, 1)):
            outside = [link[index] for link in boundary]
            duplicated = {n for n in outside if outside.count(n) > 1}
            if duplicated:
                raise ValueError(
                    f"create_submodel_from_selection: '{sorted(duplicated)[0]}' has several links "
                    f"{label} the selection; include the block it branches at in the selection")

        positions = [level.blocks[n].position for n in names]
        centre = (sum(p[0] for p in positions) / len(positions),
                  sum(p[1] for p in positions) / len(positions))
        left = min(p[0] for p in positions) - 200.0
        right = max(p[0] for p in positions) + 200.0

        with self.transaction(label=f"Create submodel {def_name}"):
            self.add_definition(def_name, parameters=[])
            for n in names:
                block = level.blocks[n]
                self.add_block(block.type, block.position, name=n, scope=def_name)
                self.set_description(n, block.description, scope=def_name)
                self.set_params(n, copy.deepcopy(block.params), scope=def_name)
            for link in internal:
                index = self.add_link(link.source, link.target, scope=def_name)
                if link.delay:
                    self.set_link_delay(link.source, link.target, copy.deepcopy(link.delay),
                                        scope=def_name, index=index)

            in_ports, out_ports = {}, {}
            for i, (src, tgt) in enumerate(incoming):
                port = self.add_block("SUB_ENTRY", (left, 100.0 * i), scope=def_name)
                self.set_params(port, {"port": src}, scope=def_name)
                self.set_description(port, src, scope=def_name)
                self.add_link(port, tgt, scope=def_name)
                in_ports[(src, tgt)] = src
            for i, (src, tgt) in enumerate(outgoing):
                port = self.add_block("SUB_EXIT", (right, 100.0 * i), scope=def_name)
                self.set_params(port, {"port": tgt}, scope=def_name)
                self.set_description(port, tgt, scope=def_name)
                self.add_link(src, port, scope=def_name)
                out_ports[(src, tgt)] = tgt

            self.remove_blocks(names, scope=scope)
            instance = self.add_block("SUBMODEL", centre, name=instance_name, scope=scope)
            self.set_params(instance, {"definition": def_name, "arguments": {}}, scope=scope)
            self.set_description(instance, def_name, scope=scope)
            for (src, _tgt), port in in_ports.items():
                self.add_link(src, instance, scope=scope, target_port=port)
            for (_src, tgt), port in out_ports.items():
                self.add_link(instance, tgt, scope=scope, source_port=port)
        return instance

    def expand_instance(self, name: str, *, scope: str = "") -> dict:
        """Inline instance *name*: replace it with a copy of its definition,
        blocks named `<instance>/<inner>`, as ONE undo step.

        Reuses `submodels.flatten` for the expansion itself, so the inlined
        copy is exactly what a run would have built -- `$`-scoped names and
        their cloned resource/set/schedule records included.

        Parameters are SUBSTITUTED where the expression is nothing but the
        parameter name (`"rate"` -> `1.2`); anywhere else the parameter would
        still have to resolve at run time, so it becomes a model variable named
        `<instance>_<param>` and the occurrences are renamed to it. Returns
        `{"blocks", "substituted", "variables"}` saying which was chosen.
        """
        level = self._level(scope)
        block = level.blocks.get(name)
        if block is None or block.type != submodels.SUBMODEL:
            raise ValueError(f"expand_instance: '{name}' is not a submodel instance")

        # One STUB block per link touching the instance, not one per neighbour
        # block: two links may now join the same pair (D77), and each has to be
        # replaced by its own inner wiring. The stub names are translated back
        # to the real neighbours once flatten has done its work.
        touching = [(i, link) for i, link in enumerate(level.links)
                    if name in (link.source, link.target)]
        stubs = {f"__neighbour_{i}": (link.source if link.target == name else link.target)
                 for i, link in touching}
        mini_links = []
        for stub, (i, link) in zip(stubs, touching):
            entry = _link_entry(link)
            entry["source" if link.target == name else "target"] = stub
            mini_links.append(entry)
        mini = {
            "blocks": [{"name": name, "type": "SUBMODEL", "position": list(block.position),
                        "params": copy.deepcopy(block.params)}]
                     + [{"name": n, "type": "DELAY", "position": [0, 0], "params": {}}
                        for n in stubs],
            "links": mini_links,
            # Every record section, so the instance's own clones -- levels,
            # flows, agent types, not only resources -- survive inlining (D108).
            **{section: copy.deepcopy(getattr(self, section))
               for section in sections.RECORD_SECTIONS},
            "submodels": self._submodels_layout(),
        }
        flat = submodels.flatten(mini)
        inner = [b for b in flat["blocks"] if b["name"].startswith(f"{name}/")]
        inner_names = {b["name"] for b in inner}

        substituted: dict[str, object] = {}
        new_variables: dict[str, object] = {}
        for entry in inner:
            entry["params"] = _substitute_locals(entry["params"], entry.get("locals") or {},
                                                 name, substituted, new_variables)
        cx, cy = _centre([b["position"] for b in inner])
        dx, dy = block.position[0] - cx, block.position[1] - cy

        # What each link touching the instance becomes. Rewiring in place (not
        # remove + append) is what keeps the outer source's BRANCH ORDER: a
        # DECIDE feeding the instance on branch 0 must still feed the inlined
        # copy on branch 0.
        # Keyed by the POSITION of the link in level.links it replaces, so two
        # parallel links touching the instance keep their own wiring (D77).
        replacements: dict[int, list[dict]] = {}
        internal = []
        for link in flat["links"]:
            src, tgt = link["source"], link["target"]
            if src in inner_names and tgt in inner_names:
                internal.append(link)
            elif tgt in inner_names and src in stubs:
                replacements.setdefault(int(src.rsplit("_", 1)[1]), []).append(
                    {**link, "source": stubs[src]})
            elif src in inner_names and tgt in stubs:
                replacements.setdefault(int(tgt.rsplit("_", 1)[1]), []).append(
                    {**link, "target": stubs[tgt]})

        with self.transaction(label=f"Expand {name}"):
            for entry in inner:
                new_name = self.add_block(entry["type"],
                                          (entry["position"][0] + dx, entry["position"][1] + dy),
                                          name=entry["name"], scope=scope)
                self.set_description(new_name, entry.get("description", new_name), scope=scope)
                self.set_params(new_name, entry["params"], scope=scope)
            self._rewire_expanded(level, name, replacements, internal, scope)
            for section in sections.RECORD_SECTIONS:
                extra = [r for r in flat.get(section, [])
                         if r.get("name") not in {x.get("name") for x in getattr(self, section)}]
                if extra:
                    getattr(self, f"set_{section}")(getattr(self, section) + extra)
            if new_variables:
                declared = {v.get("name") for v in self.variables}
                self.set_variables(self.variables + [
                    {"name": k, "initial": v} for k, v in new_variables.items()
                    if k not in declared])
        return {"blocks": sorted(inner_names), "substituted": substituted,
                "variables": new_variables}

    def _rewire_expanded(self, level: _Level, instance: str, replacements: dict,
                         internal: list, scope: str = "") -> None:
        """expand_instance's link surgery: drop the instance, splice each link
        that touched it into the links that replace it (in place, so branch
        order survives), then append the definition's own internal links.
        *replacements* is keyed by the link's POSITION in level.links."""
        links = []
        for position, old in enumerate(level.links):
            if instance in (old.source, old.target):
                for link in replacements.get(position, []):
                    links.append(LinkData(source=link["source"], target=link["target"],
                                          delay=copy.deepcopy(link.get("delay") or old.delay)))
                continue
            links.append(old)
        for link in internal:
            links.append(LinkData(source=link["source"], target=link["target"],
                                  delay=copy.deepcopy(link.get("delay"))))
        level.blocks.pop(instance, None)
        level.links = links
        self.dirty = True
        self._notify("blocks", action="remove", names=[instance], scope=scope)
        self._notify("links", action="reset", scope=scope)

    # -- background image (P2.3a item 1) -------------------------------------
    # {"path", "position": [x, y], "scale": float, "opacity": 0..1, "locked":
    # bool, "units_per_metre": float|None}; core/schema/ checks shape only, the
    # engine never reads this key. A missing file is a Problems-dock WARNING
    # (see problems() above), never an error.
    def resolve_relative_path(self, path) -> Path:
        """*path* resolved to an absolute filesystem path, relative to the
        model file's own folder (or cwd with no file yet) when it is not
        already absolute. Shared by the background image and an entity's
        icon file (ROADMAP 2.3 part C item 2), both stored the same way."""
        p = Path(path)
        if p.is_absolute():
            return p
        base = self.path.parent if self.path is not None else Path.cwd()
        return base / p

    def normalize_relative_path(self, path) -> str:
        """Relative to the model's own folder when *path* lives under it (so a
        shared project folder stays portable as a whole); absolute otherwise,
        or when the document has no path yet."""
        p = Path(path)
        if self.path is not None and p.is_absolute():
            try:
                rel = os.path.relpath(p, self.path.parent)
            except ValueError:
                rel = None  # different drive on Windows: no relative path exists
            if rel is not None and not rel.startswith(".."):
                return rel.replace(os.sep, "/")
        return str(p)

    def background_abs_path(self) -> Path | None:
        """The background image's path resolved to an absolute filesystem
        path (it may be stored relative to the model file); None when there
        is no background or no path in it."""
        if not self.background or not self.background.get("path"):
            return None
        return self.resolve_relative_path(self.background["path"])

    def _normalize_background_path(self, path) -> str:
        return self.normalize_relative_path(path)

    def set_background(self, spec: dict | None) -> None:
        """Set (dict) or clear (None) the floor-plan background image: one
        undo step, notification kind "background"."""
        normalized = None
        if spec is not None:
            normalized = dict(spec)
            if normalized.get("path"):
                normalized["path"] = self._normalize_background_path(normalized["path"])
        if normalized == (self.background or None):
            return
        self._push_undo("Edit background")
        self.background = normalized
        self.dirty = True
        self._notify("background")

    # -- dashboard (PHASE6 G1, D132) -----------------------------------------
    # {"widgets": [{"name", "kind", "expr"|"series", ...}]}; `core/timeseries.py`
    # samples it, `nagasim.core.outputs.summarize_dashboard` reports it. One
    # object, like `network` above, since the editor's dashboard panel (G2)
    # edits the whole widget list as one form.
    def set_dashboard(self, dashboard: dict | None) -> None:
        dashboard = copy.deepcopy(dashboard) or {"widgets": []}
        if dashboard == self.dashboard:
            return
        self._push_undo("Edit dashboard")
        self.dashboard = dashboard
        self.dirty = True
        self._notify("tables", table="dashboard")

    # -- model information (P2.3a item 2) ------------------------------------
    # {"description", "author", "notes"}, every key optional; shown in the
    # report header and File > "Model information...".
    def set_info(self, **kwargs) -> None:
        updated = {**self.info, **kwargs}
        if updated == self.info:
            return
        self._push_undo("Edit model info")
        self.info = updated
        self.dirty = True
        self._notify("info", **kwargs)

    def set_description_text(self, text: str) -> None:
        self.set_info(description=text)

    @property
    def description_text(self) -> str:
        return self.info.get("description", "")


def _record_aliases(section: sections.Section) -> None:
    """Attach set_<section>/upsert_<item>/remove_<item>/rename_<item> to
    ModelDocument: the names callers, tests and the UI's
    `getattr(doc, f"set_{section}")` have always used, one row each."""
    name, item = section.name, section.item

    def set_(self, records: list, *, label: str | None = None) -> None:
        self.set_records(name, records, label=label)

    def upsert(self, entry, *, label: str | None = None) -> None:
        self.upsert_record(name, entry, label=label)

    def remove(self, key: str, *, label: str | None = None) -> None:
        self.remove_record(name, key, label=label)

    def rename(self, old: str, new: str, *, label: str | None = None) -> None:
        self.rename_record(name, old, new, label=label)

    set_.__doc__ = section.doc or None
    for method, attr in ((set_, f"set_{name}"), (upsert, f"upsert_{item}"),
                         (remove, f"remove_{item}"), (rename, f"rename_{item}")):
        method.__name__, method.__qualname__ = attr, f"ModelDocument.{attr}"
        setattr(ModelDocument, attr, method)


for _section in sections.DOCUMENT_LISTS:
    _record_aliases(_section)


def _centre(positions) -> tuple[float, float]:
    if not positions:
        return (0.0, 0.0)
    return (sum(p[0] for p in positions) / len(positions),
            sum(p[1] for p in positions) / len(positions))


def _substitute_locals(value, locals_: dict, instance: str, substituted: dict, variables: dict):
    """Replace submodel parameter references in *value* (a params tree) by their
    bound values where the whole expression IS the parameter, and by a model
    variable `<instance>_<param>` anywhere else (see expand_instance)."""
    if isinstance(value, dict):
        return {k: _substitute_locals(v, locals_, instance, substituted, variables)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_locals(v, locals_, instance, substituted, variables) for v in value]
    if not isinstance(value, str) or not locals_:
        return value
    text = value.strip()
    if text in locals_:
        substituted[text] = locals_[text]
        return locals_[text]
    # ponytail: an identifier scan, not a rewrite of the parsed tree -- it only
    # fires for a string that actually compiles as an expression AND names a
    # parameter, which no plain string param ever does in practice. Upgrade
    # path: rebuild the text from the AST if a real model ever trips on it.
    try:
        referenced = compile_expression(value).names & set(locals_)
    except ExpressionError:
        return value
    for name in referenced:
        variable = f"{instance}_{name}"
        variables[variable] = locals_[name]
        value = re.sub(rf"\b{re.escape(name)}\b", variable, value)
    return value


class DefinitionView:
    """Editing handle for one submodel definition, with the same block/link/
    annotation API as ModelDocument (so `ui/canvas.py` and `ui/properties.py`
    bind to it unchanged). Everything else -- resources, variables, undo,
    transactions, subscriptions -- is the document's own, reached by
    attribute delegation.
    """

    def __init__(self, document: ModelDocument, name: str):
        self.document = document
        self.name = name

    def __repr__(self):
        return f"<DefinitionView {self.name!r}>"

    def __getattr__(self, item):        # only for what is not defined below
        return getattr(self.document, item)

    @property
    def blocks(self):
        return self.document._level(self.name).blocks

    @property
    def links(self):
        return [(link.source, link.target) for link in self.document._level(self.name).links]

    def link_records(self):
        return self.document.link_records(scope=self.name)

    def link_count(self, src, tgt):
        return self.document.link_count(src, tgt, scope=self.name)

    def branch_index(self, src, tgt, index=0):
        return self.document.branch_index(src, tgt, index=index, scope=self.name)

    @property
    def annotations(self):
        return self.document._level(self.name).annotations

    @property
    def parameters(self) -> list:
        return self.document.definition_parameters(self.name)

    # -- scoped forwarding ---------------------------------------------------
    def out_links(self, name):
        return self.document.out_links(name, scope=self.name)

    def in_links(self, name):
        return self.document.in_links(name, scope=self.name)

    def link_delay(self, src, tgt, index=0):
        return self.document.link_delay(src, tgt, scope=self.name, index=index)

    def set_link_delay(self, src, tgt, spec, index=0):
        self.document.set_link_delay(src, tgt, spec, scope=self.name, index=index)

    def link_ports(self, src, tgt, index=0):
        return self.document.link_ports(src, tgt, scope=self.name, index=index)

    def set_link_ports(self, src, tgt, index=0, **ports):
        self.document.set_link_ports(src, tgt, scope=self.name, index=index, **ports)

    def add_block(self, type_name, position, name=None):
        return self.document.add_block(type_name, position, name, scope=self.name)

    def remove_blocks(self, names):
        self.document.remove_blocks(names, scope=self.name)

    def remove_items(self, blocks=(), links=()):
        self.document.remove_items(blocks=blocks, links=links, scope=self.name)

    def rename_block(self, old, new):
        self.document.rename_block(old, new, scope=self.name)

    def move_blocks(self, positions, *, label=None):
        self.document.move_blocks(positions, scope=self.name, label=label)

    def set_label_offsets(self, offsets):
        self.document.set_label_offsets(offsets, scope=self.name)

    def set_params(self, name, params):
        self.document.set_params(name, params, scope=self.name)

    def set_description(self, name, text):
        self.document.set_description(name, text, scope=self.name)

    def add_link(self, src, tgt, source_port=None, target_port=None):
        return self.document.add_link(src, tgt, scope=self.name,
                                      source_port=source_port, target_port=target_port)

    def remove_link(self, src, tgt, index=0):
        self.document.remove_link(src, tgt, scope=self.name, index=index)

    def reorder_link(self, src, old_index, new_index):
        self.document.reorder_link(src, old_index, new_index, scope=self.name)

    def add_annotation(self, kind, **fields):
        return self.document.add_annotation(kind, scope=self.name, **fields)

    def update_annotation(self, ann_id, **fields):
        self.document.update_annotation(ann_id, scope=self.name, **fields)

    def remove_annotations(self, ids):
        self.document.remove_annotations(ids, scope=self.name)

    def expand_instance(self, name):
        return self.document.expand_instance(name, scope=self.name)

    def create_submodel_from_selection(self, block_names, def_name, instance_name=None):
        return self.document.create_submodel_from_selection(
            block_names, def_name, scope=self.name, instance_name=instance_name)

    # A definition has no floor plan of its own; the canvas must not paint the
    # model's behind it.
    @property
    def background(self):
        return None

    def background_abs_path(self):
        return None
