"""Pure layout math for the canvas (ROADMAP 2.3 part A #2-#3): align, distribute,
snap-to-grid, and a deterministic layered auto-layout. No Qt (UI_DESIGN.md rule 2):
a caller applies the returned `{name: (x, y)}` through `ModelDocument.move_blocks`,
which is one undo step regardless of how many blocks moved.
"""
from __future__ import annotations

from nagasim.core.document import ModelDocument

__all__ = ["align", "distribute", "snap_positions", "auto_layout",
           "ALIGN_EDGES", "DISTRIBUTE_AXES"]

# Matches ui/style.py's GRID/BLOCK_W/BLOCK_H; duplicated here (not imported)
# because core/ must never import ui/ (UI_DESIGN.md rule 1).
GRID = 20
DEFAULT_SIZE = (120.0, 60.0)

# Auto-layout spacing, in scene units; both multiples of GRID so a laid-out
# model needs no further snapping.
LAYER_SPACING_X = 200.0
LAYER_SPACING_Y = 100.0
COMPONENT_GAP_Y = 160.0

ALIGN_EDGES = ("left", "right", "top", "bottom", "center_h", "center_v")
DISTRIBUTE_AXES = ("horizontal", "vertical")

Positions = dict  # {name: (x, y)}


def _snap(value: float, grid: int = GRID) -> float:
    return round(value / grid) * grid


def snap_positions(positions: Positions, grid: int = GRID) -> Positions:
    """Every position rounded to the nearest *grid* unit."""
    return {name: (_snap(x, grid), _snap(y, grid)) for name, (x, y) in positions.items()}


def align(positions: Positions, edge: str) -> Positions:
    """Move every block in *positions* onto a common line. Block size is
    uniform across the app (every block is BLOCK_W x BLOCK_H), so aligning
    edges reduces to aligning centres: "left"/"right" set every x to the
    min/max x already present; "center_h" sets it to the midpoint of the
    bounding box; "top"/"bottom"/"center_v" do the same on y. Fewer than 2
    blocks has nothing to align against, so positions pass through unchanged."""
    if edge not in ALIGN_EDGES:
        raise ValueError(f"unknown align edge: {edge}")
    if len(positions) < 2:
        return dict(positions)
    xs = [p[0] for p in positions.values()]
    ys = [p[1] for p in positions.values()]
    if edge == "left":
        target = min(xs)
        return {n: (target, y) for n, (x, y) in positions.items()}
    if edge == "right":
        target = max(xs)
        return {n: (target, y) for n, (x, y) in positions.items()}
    if edge == "center_h":
        target = (min(xs) + max(xs)) / 2
        return {n: (target, y) for n, (x, y) in positions.items()}
    if edge == "top":
        target = min(ys)
        return {n: (x, target) for n, (x, y) in positions.items()}
    if edge == "bottom":
        target = max(ys)
        return {n: (x, target) for n, (x, y) in positions.items()}
    target = (min(ys) + max(ys)) / 2  # center_v
    return {n: (x, target) for n, (x, y) in positions.items()}


def distribute(positions: Positions, axis: str, sizes: dict | None = None) -> Positions:
    """Equalize the gaps between block EDGES along *axis*, keeping the two
    extreme blocks (by current centre) fixed -- the standard "distribute"
    meaning, which reduces to equal centre-spacing when every block is the
    same size. *sizes* maps name -> (w, h) for a block whose footprint isn't
    the default BLOCK_W x BLOCK_H (every built-in block is today, but the
    hook is here for a future variable-size block or a plugin one). Fewer
    than 3 blocks has nothing meaningful to distribute (2 blocks are already
    "evenly spaced" between themselves)."""
    if axis not in DISTRIBUTE_AXES:
        raise ValueError(f"unknown distribute axis: {axis}")
    if len(positions) < 3:
        return dict(positions)
    idx = 0 if axis == "horizontal" else 1
    size_idx = idx

    def size_of(name: str) -> float:
        return (sizes or {}).get(name, DEFAULT_SIZE)[size_idx]

    ordered = sorted(positions.items(), key=lambda kv: kv[1][idx])
    names = [n for n, _ in ordered]
    widths = [size_of(n) for n in names]
    first_c, last_c = ordered[0][1][idx], ordered[-1][1][idx]
    span = (last_c + widths[-1] / 2) - (first_c - widths[0] / 2)
    gap = (span - sum(widths)) / (len(names) - 1)

    result = dict(positions)
    left = first_c - widths[0] / 2
    for name, w in zip(names, widths):
        centre = left + w / 2
        pos = list(positions[name])
        pos[idx] = centre
        result[name] = tuple(pos)
        left += w + gap
    return result


# -- auto-layout (#3) ---------------------------------------------------

def _forward_edges(names_order: list[str], out_links: dict[str, list[str]]) -> set[tuple[str, str]]:
    """Every link that is not a back edge (one that closes a cycle), found by
    an iterative DFS over *names_order* (deterministic: DFS always starts
    from the earliest-undiscovered node in that order). A "back edge" here is
    an edge to a node currently on the DFS stack -- the standard definition
    used to break cycles for layering (rework loops stay in the model, they
    just don't count toward a block's layer)."""
    back_edges: set[tuple[str, str]] = set()
    visited: set[str] = set()
    on_stack: set[str] = set()

    def visit(start: str) -> None:
        stack = [(start, iter(out_links.get(start, [])))]
        on_stack.add(start)
        visited.add(start)
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child in on_stack:
                    back_edges.add((node, child))
                    continue
                if child not in visited:
                    visited.add(child)
                    on_stack.add(child)
                    stack.append((child, iter(out_links.get(child, []))))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                on_stack.discard(node)

    for name in names_order:
        if name not in visited:
            visit(name)

    all_edges = {(s, t) for s, targets in out_links.items() for t in targets}
    return all_edges - back_edges


def _connected_components(names_order: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    """Weakly-connected components (undirected reachability over *edges*),
    each internally ordered like *names_order*; components ordered by the
    earliest name (in *names_order*) they contain."""
    undirected: dict[str, set[str]] = {n: set() for n in names_order}
    for s, t in edges:
        undirected.setdefault(s, set()).add(t)
        undirected.setdefault(t, set()).add(s)

    seen: set[str] = set()
    components: list[list[str]] = []
    for start in names_order:
        if start in seen or start not in undirected:
            continue
        stack, members = [start], []
        seen.add(start)
        while stack:
            node = stack.pop()
            members.append(node)
            for nxt in undirected.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        order_index = {n: i for i, n in enumerate(names_order)}
        members.sort(key=lambda n: order_index[n])
        components.append(members)
    return components


def _layer_component(members: list[str], forward: set[tuple[str, str]]) -> dict[str, int]:
    """Longest-path layering (Kahn's algorithm over the forward-edge DAG,
    restricted to *members*): a node with no forward predecessor is layer 0
    (a CREATE block never has one, so it always lands there); every other
    node's layer is 1 + the max layer of its forward predecessors."""
    member_set = set(members)
    preds: dict[str, list[str]] = {n: [] for n in members}
    succs: dict[str, list[str]] = {n: [] for n in members}
    indegree: dict[str, int] = {n: 0 for n in members}
    for s, t in forward:
        if s in member_set and t in member_set:
            preds[t].append(s)
            succs[s].append(t)
            indegree[t] += 1

    layer: dict[str, int] = {}
    queue = [n for n in members if indegree[n] == 0]
    qi = 0
    remaining = dict(indegree)
    while qi < len(queue):
        node = queue[qi]
        qi += 1
        layer[node] = max((layer[p] + 1 for p in preds[node]), default=0)
        for nxt in succs[node]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                queue.append(nxt)
    # A node never reached (only possible if `forward` still contained a
    # cycle, which _forward_edges guarantees it doesn't) falls back to 0.
    for n in members:
        layer.setdefault(n, 0)
    return layer


def _order_within_layers(members: list[str], layer: dict[str, int],
                         forward: set[tuple[str, str]]) -> dict[str, int]:
    """Barycentre ordering (a couple of sweeps) to reduce edge crossings
    between adjacent layers. Layer 0 keeps the deterministic order it was
    given (component-member order, itself derived from doc.blocks insertion
    order); every later layer sorts by the mean order-index of its forward
    predecessors, ties broken by the previous order (stable sort) so the
    result never depends on set/dict iteration order."""
    by_layer: dict[int, list[str]] = {}
    for n in members:
        by_layer.setdefault(layer[n], []).append(n)
    max_layer = max(by_layer, default=0)

    order_index: dict[str, int] = {n: i for i, n in enumerate(members)} if members else {}
    preds: dict[str, list[str]] = {n: [] for n in members}
    succs: dict[str, list[str]] = {n: [] for n in members}
    for s, t in forward:
        if s in preds and t in preds:
            succs[s].append(t)
            preds[t].append(s)

    def resort(layer_index: int, neighbours: dict[str, list[str]]) -> None:
        nodes = by_layer.get(layer_index)
        if not nodes:
            return

        def key(n: str):
            refs = [order_index[p] for p in neighbours[n] if p in order_index]
            bary = sum(refs) / len(refs) if refs else order_index[n]
            return (bary, order_index[n])

        nodes.sort(key=key)
        for i, n in enumerate(nodes):
            order_index[n] = i

    for _sweep in range(2):
        for li in range(1, max_layer + 1):
            resort(li, preds)
        for li in range(max_layer - 1, -1, -1):
            resort(li, succs)

    return order_index


def auto_layout(doc: ModelDocument, block_names: list[str] | None = None) -> Positions:
    """Layered left-to-right layout of *block_names* (default: every block in
    *doc*): longest-path layering from the roots of the forward-edge graph
    (a CREATE block always has no incoming edge, so it is always a root),
    ignoring back edges (rework loops); barycentre ordering within each layer
    to reduce crossings; disconnected components stacked vertically below
    each other. Deterministic -- every tie breaks by doc.blocks' insertion
    order, never by set/dict iteration order. The result is translated so its
    bounding box starts at the selection's own original top-left corner
    (laying out the whole model keeps it roughly where it was; laying out a
    handful of selected blocks doesn't fling them off to the origin)."""
    names = list(block_names) if block_names is not None else list(doc.blocks)
    names = [n for n in names if n in doc.blocks]
    if not names:
        return {}

    names_order = [n for n in doc.blocks if n in set(names)]  # doc's own insertion order
    name_set = set(names_order)
    out_links: dict[str, list[str]] = {n: [] for n in names_order}
    for s, t in doc.links:
        if s in name_set and t in name_set:
            out_links[s].append(t)

    forward = _forward_edges(names_order, out_links)
    components = _connected_components(names_order, forward | {(s, t) for s, targets in out_links.items()
                                                                for t in targets})
    # A block with no links at all is its own component too.
    in_a_component = {n for comp in components for n in comp}
    for n in names_order:
        if n not in in_a_component:
            components.append([n])

    raw: Positions = {}
    y_offset = 0.0
    for members in components:
        layer = _layer_component(members, forward)
        order_index = _order_within_layers(members, layer, forward)
        max_slot = max(order_index.values(), default=0)
        for n in members:
            x = layer[n] * LAYER_SPACING_X
            y = y_offset + order_index[n] * LAYER_SPACING_Y
            raw[n] = (x, y)
        y_offset += (max_slot + 1) * LAYER_SPACING_Y + COMPONENT_GAP_Y

    # Anchor the new layout's top-left to the original selection's top-left.
    orig_xs = [doc.blocks[n].position[0] for n in names_order]
    orig_ys = [doc.blocks[n].position[1] for n in names_order]
    raw_xs = [p[0] for p in raw.values()]
    raw_ys = [p[1] for p in raw.values()]
    dx = min(orig_xs) - min(raw_xs)
    dy = min(orig_ys) - min(raw_ys)
    translated = {n: (x + dx, y + dy) for n, (x, y) in raw.items()}
    return snap_positions(translated)
