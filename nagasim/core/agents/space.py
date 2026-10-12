"""Agent space for a run: the compiled grid (`_Grid`, PHASE6 A4, D102), links
and the initial-network generators (PHASE6 A5, D103), and `_SpaceMixin`, the
`AgentSystem` methods for placement, movement, grid, heading and links."""
from __future__ import annotations

import heapq
import math
from typing import TYPE_CHECKING

import numpy as np
from scipy.spatial import cKDTree

from nagasim.core.agents.common import (_NEIGHBOURHOODS, _NETWORK_TAG, _PATCH_TAG, _norm360,
                                        _number, _table_column, stream_seed)
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.network import point_on_path
from nagasim.core.quantities import SECTION_FIELDS, param, to_base
from nagasim.core.timeunits import ModelUnits

if TYPE_CHECKING:       # annotations only: runtime imports this module
    from nagasim.core.agents.runtime import _Population


class _Grid:
    """A type's grid space compiled for a run (PHASE6 A4, D102): geometry,
    the patches (one (h, w) float array each, row cy, column cx) and the
    patch tick. `front` is the tick-start copy a synchronous tick reads."""

    def __init__(self, space: dict, names, type_name: str, params: dict,
                 units: ModelUnits | None = None):
        self.w, self.h = (int(v) for v in space["size"])
        self.cell = float(space.get("cell", 1))
        self.torus = bool(space.get("torus", False))
        self.offsets = _NEIGHBOURHOODS[space.get("neighbourhood", "moore")]
        self.type_name, self.params = type_name, params
        self.specs = []                 # (name, init, diffuse, evaporate)
        self.grow: dict = {}            # PHASE6 A9: name -> (rate, max or None)
        for p in space.get("patches") or []:
            init = p.get("init", 0)
            if isinstance(init, str):
                init = compile_expression(init, set(names) | {"cx", "cy"}).closure()
            self.specs.append((p["name"], init, float(p.get("diffuse", 0)),
                               float(p.get("evaporate", 0))))
            if p.get("grow"):
                cap = p["grow"].get("max")
                self.grow[p["name"]] = (float(p["grow"]["rate"]),
                                        None if cap is None else float(cap))
        self.patches: dict = {}
        self.capacity: dict = {}        # name -> each cell's max (A9 grow)
        self.front = None
        fields = SECTION_FIELDS["agent_types.space"]
        self.dt = (float(to_base(param(fields, "patch_tick"), space, units or ModelUnits(), fields))
                   if space.get("patch_tick") else None)
        self.k = 0                      # the next patch tick's integer index

    def init_patches(self, ctx, seed: int) -> None:
        """Every patch's initial values: a number, or an expression of cx, cy
        (and TABLE, parameters...) evaluated cell by cell in row-major order,
        drawing (if it samples) from the patch's own stream -- (seed, type,
        patch index, tag), `stream_seed` (D107)."""
        for p, (name, init, _d, _e) in enumerate(self.specs):
            if not callable(init):
                self.patches[name] = np.full((self.h, self.w), float(init))
                self._capacity(name)
                continue
            rng = np.random.default_rng(stream_seed(seed, self.type_name, p, _PATCH_TAG))
            values = np.empty((self.h, self.w))
            scope = ExpressionScope(ctx=ctx, rng=rng, locals=dict(self.params))
            # ponytail: one evaluation per cell; fine to ~1e5 cells, vectorise if it binds.
            for cy in range(self.h):
                for cx in range(self.w):
                    scope.locals["cx"], scope.locals["cy"] = cx, cy
                    values[cy, cx] = _number(init(scope), f"patch '{name}': init")
            self.patches[name] = values
            self._capacity(name)

    def _capacity(self, name: str) -> None:
        """A growing patch's per-cell max: `grow.max`, or by default each
        cell's own initial value -- Sugarscape's capacity landscape."""
        if name in self.grow:
            cap = self.grow[name][1]
            self.capacity[name] = self.patches[name].copy() if cap is None else cap

    def wrap(self, x: int, y: int):
        """Cell (x, y) on the grid -- wrapped on a torus -- or None off it."""
        if self.torus:
            return x % self.w, y % self.h
        return (x, y) if 0 <= x < self.w and 0 <= y < self.h else None

    def around(self, x: int, y: int) -> list:
        """The neighbourhood cells of (x, y), in `offsets` order, each once and
        never (x, y) itself (a torus under 3 cells wide folds offsets together)."""
        cells = []
        for dx, dy in self.offsets:
            c = self.wrap(x + dx, y + dy)
            if c is not None and c != (x, y) and c not in cells:
                cells.append(c)
        return cells

    def tick(self) -> None:
        """One patch tick: each patch diffuses, evaporates, then grows (A9):
        a cell below its max gains `rate`, clipped at the max; one at or above
        it (a deposit, a diffused share) is left alone -- growth never lowers."""
        for name, _init, diffuse, evaporate in self.specs:
            v = self.patches[name]
            if diffuse:
                v = self._diffuse(v, diffuse)
            if evaporate:
                v = v * (1 - evaporate)
            if name in self.grow:
                cap = self.capacity[name]
                v = np.where(v < cap, np.minimum(v + self.grow[name][0], cap), v)
            self.patches[name] = v

    def _diffuse(self, v, rate: float):
        """NetLogo's diffuse: a cell keeps (1 - rate) of its value and hands
        rate / n to each of its n neighbourhood offsets; a share whose cell is
        off a non-torus grid stays home, so the total is conserved."""
        share = v * rate / len(self.offsets)
        out = v * (1 - rate)
        h, w = v.shape
        for dx, dy in self.offsets:
            if self.torus:
                out += np.roll(share, (dy, dx), axis=(0, 1))
                continue
            src = (slice(max(0, -dy), h - max(0, dy)), slice(max(0, -dx), w - max(0, dx)))
            dst = (slice(max(0, dy), h - max(0, -dy)), slice(max(0, dx), w - max(0, -dx)))
            out[dst] += share[src]
            kept = share.copy()
            kept[src] = 0.0
            out += kept
        return out


# ---------------------------------------------------------------- links (PHASE6 A5, D103)
class _Links:
    """One population's links: adjacency sets (the mutable truth; `into` is
    `out` itself when undirected, the reverse sets when directed, for a
    death's cleanup), which live reads sort per row (O(degree), #150). In a
    synchronous tick (D99) the first change snapshots a CSR copy -- `indptr`,
    `indices`, each row's neighbours ascending -- and reads during that tick
    see the snapshot: the tick-start links."""

    def __init__(self, directed: bool = False, on_spawn: bool = False):
        self.directed, self.on_spawn = directed, on_spawn
        self.out: dict = {}
        self.into: dict = {} if directed else self.out
        self.front = None               # (the pop.front it belongs to, its CSR)
        self.builds = 0                 # CSR builds, for the #150 complexity test

    def _changing(self, pop) -> None:
        if pop.front is not None and (self.front is None or self.front[0] is not pop.front):
            self.front = (pop.front, self._build(pop.n))

    def add(self, pop, i: int, j: int) -> bool:
        """Link i -> j (and j -> i undirected); False if they already were."""
        if j in self.out.get(i, ()):
            return False
        self._changing(pop)
        self.out.setdefault(i, set()).add(j)
        self.into.setdefault(j, set()).add(i)
        return True

    def remove(self, pop, i: int, j: int) -> bool:
        if j not in self.out.get(i, ()):
            return False
        self._changing(pop)
        self.out[i].discard(j)
        self.into[j].discard(i)
        return True

    def drop(self, pop, i: int) -> None:
        """Every link to or from agent i (a death, C5)."""
        if not self.out.get(i) and not self.into.get(i):
            return
        self._changing(pop)
        for j in self.out.pop(i, ()):
            self.into[j].discard(i)
        for j in self.into.pop(i, ()) if self.directed else ():
            self.out[j].discard(i)

    def _build(self, n: int) -> tuple:
        """The CSR of the current links over slots 0..n-1."""
        self.builds += 1
        rows = [sorted(self.out.get(i, ())) for i in range(n)]
        indptr = np.zeros(n + 1, dtype=np.int64)
        np.cumsum([len(r) for r in rows], out=indptr[1:])
        return indptr, np.fromiter((j for r in rows for j in r), np.int64, int(indptr[-1]))

    def row(self, pop, i: int, live: bool = False) -> np.ndarray:
        """Agent i's linked neighbours, ascending -- unless *live*, the
        snapshot's in a synchronous tick that changed a link (see the class)."""
        front = self.front
        snap = not live and pop.front is not None and front is not None and front[0] is pop.front
        if not snap:
            return np.array(sorted(self.out.get(i, ())), dtype=np.int64)
        indptr, indices = front[1]
        if i + 1 >= len(indptr):
            return indices[:0]
        return indices[indptr[i]:indptr[i + 1]]


def _erdos_renyi(n: int, p: float, directed: bool, rng) -> list:
    """G(n, p): for i ascending, one uniform per candidate j ascending (j > i;
    directed: every j != i), a link where it is < p."""
    edges = []
    # ponytail: O(n^2) uniforms (1e4 agents: 5e7, about a second); geometric skips if it binds.
    for i in range(n):
        js = np.delete(np.arange(n), i) if directed else np.arange(i + 1, n)
        edges += [(i, int(j)) for j in js[rng.random(len(js)) < p]]
    return edges


def _watts_strogatz(n: int, k: int, beta: float, rng) -> list:
    """Watts-Strogatz: the ring lattice (i linked to i +- 1..k/2), then for j
    = 1..k/2 and u ascending one uniform; below beta, u - (u + j) moves to u -
    w, w drawn uniformly (`integers(n)`) until it is neither u nor already
    linked (networkx's order; a saturated u keeps its link)."""
    adj = [set() for _ in range(n)]
    for u in range(n):
        for j in range(1, k // 2 + 1):
            v = (u + j) % n
            adj[u].add(v)
            adj[v].add(u)
    for j in range(1, k // 2 + 1) if beta > 0 else ():
        for u in range(n):
            if rng.random() >= beta or len(adj[u]) >= n - 1:
                continue
            w = int(rng.integers(n))
            while w == u or w in adj[u]:
                w = int(rng.integers(n))
            v = (u + j) % n
            adj[u].discard(v)
            adj[v].discard(u)
            adj[u].add(w)
            adj[w].add(u)
    return [(u, v) for u in range(n) for v in sorted(adj[u]) if u < v]


def _barabasi_albert(n: int, m: int, rng) -> list:
    """Barabasi-Albert: agents 0..m-1 start unlinked, agent m links to all of
    them, and each later agent to m distinct earlier ones, each drawn
    uniformly (`integers`) from the list holding every agent once per link
    end -- degree-proportional -- redrawn on a repeat: m * (n - m) links."""
    edges, ends, targets = [], [], list(range(m))
    for v in range(m, n):
        if v > m:
            targets = []
            while len(targets) < m:
                t = ends[int(rng.integers(len(ends)))]
                if t not in targets:
                    targets.append(t)
        edges += [(t, v) for t in targets]
        ends += targets + [v] * m
    return edges


class _SpaceMixin:
    """The `AgentSystem` methods for space: placement and movement (AB-3), the
    grid (A4), heading and steering (A6), neighbours and links (A5). Split out
    of `runtime.AgentSystem` only for size: it reads the system's own state."""

    # -- space and movement (PHASE5 AB-3) --------------------------------------
    def _position(self, pop: _Population, i: int, t: float) -> tuple[float, float]:
        """Where agent i of *pop* is at *t*: linear on its current segment, or
        along the real polyline (`point_on_path`) while it has one -- exact
        either way, with no per-tick state (D68)."""
        t0, t1 = float(pop.t0[i]), float(pop.t1[i])
        if t1 <= t0 or t >= t1:
            return float(pop.x1[i]), float(pop.y1[i])
        if t <= t0:
            return float(pop.x0[i]), float(pop.y0[i])
        f = (t - t0) / (t1 - t0)
        path = pop.path.get(i) if pop.path else None
        if path:
            return point_on_path(self._network, path, f)
        return (float(pop.x0[i] + (pop.x1[i] - pop.x0[i]) * f),
                float(pop.y0[i] + (pop.y1[i] - pop.y0[i]) * f))

    def _place(self, pop: _Population, i: int, now: float) -> None:
        """Place agent i (AB-3; a newborn too, C5): `{"kind": "random"}`
        (default) is uniform over the space's own bounds, or a uniform random
        network node, both from the AGENT's own stream (CRN, the same
        convention as an attribute init); `{"kind": "table", "points"|"nodes":
        [...]}` places agent i at entry `i % len(table)`."""
        table_kind = pop.placement.get("kind", "random") == "table"
        grid = pop.type.grid
        if grid is not None:            # A4: a table cell, or one uniform flat-index draw
            table = pop.placement.get("points") if table_kind else None
            if table:
                cx, cy = table[i % len(table)]
            else:
                k = int(pop.rng(i).integers(grid.w * grid.h))
                cx, cy = k % grid.w, k // grid.w
            self._set_cell(pop, i, int(cx), int(cy), now, placed=False)
            return
        if pop.type.space.get("kind") == "network":
            nodes = self._nodes
            table = pop.placement.get("nodes") if table_kind else None
            name = (table[i % len(table)] if table
                    else nodes[int(pop.rng(i).integers(len(nodes)))] if nodes else "")
            self._set_node(pop, i, name, now)
            return
        x0b, y0b, x1b, y1b = pop.placement.get("bounds") or pop.type.space.get("bounds")
        table = pop.placement.get("points") if table_kind else None
        if table:
            x, y = table[i % len(table)]
        else:
            rng = pop.rng(i)
            x = rng.uniform(x0b, x1b) if x1b > x0b else x0b
            y = rng.uniform(y0b, y1b) if y1b > y0b else y0b
        self._set_point(pop, i, float(x), float(y), now)

    def _set_point(self, pop: _Population, i: int, x: float, y: float, t: float) -> None:
        pop.x0[i] = pop.x1[i] = x
        pop.y0[i] = pop.y1[i] = y
        pop.t0[i] = pop.t1[i] = t
        pop.path.pop(i, None)
        self._trace_moved(pop, i, t, x, y, x, y, 0.0, ())

    def _set_node(self, pop: _Population, i: int, name: str, t: float) -> None:
        pop.node[i] = name
        point = self._network.point(name) if name and self._network.has(name) else (0.0, 0.0)
        self._set_point(pop, i, point[0], point[1], t)

    # -- grid space (PHASE6 A4, D102) -----------------------------------------
    def _set_cell(self, pop: _Population, i: int, cx: int, cy: int, now: float,
                  placed: bool = True) -> None:
        """Agent i now sits in cell (cx, cy), at its centre, instantly; the
        per-cell counts follow (*placed*: it was in a cell before)."""
        s, w = pop.state[i], pop.occ.shape[2]
        if placed:
            pop.occ[s, pop.cy[i], pop.cx[i]] -= 1
            pop.cells.get(int(pop.cy[i]) * w + int(pop.cx[i]), set()).discard(i)
        pop.occ[s, cy, cx] += 1
        pop.cells.setdefault(int(cy) * w + int(cx), set()).add(i)
        pop.cx[i], pop.cy[i] = cx, cy
        size = pop.type.grid.cell
        self._set_point(pop, i, (cx + 0.5) * size, (cy + 0.5) * size, now)
        self._tree_cache.pop(pop.index, None)       # positions moved within the instant

    def _grid_move_to(self, pop: _Population, i: int, target) -> None:
        """`move_to` on a grid: a cell [cx, cy] (wrapped on a torus, refused
        off a bounded grid) or another agent's cell; no travel, no "arrived"."""
        grid = pop.type.grid
        if isinstance(target, (tuple, list)) and len(target) == 2 and isinstance(target[0], str):
            other, j = self.deref(target if isinstance(target, tuple) else tuple(target))
            if other.occ is not None:
                cell = int(other.cx[j]), int(other.cy[j])
            else:
                x, y = self._position(other, j, float(self.env.now))
                cell = math.floor(x / grid.cell), math.floor(y / grid.cell)
        elif isinstance(target, (tuple, list)) and len(target) == 2:
            cell = (_integer(target[0], "move_to"), _integer(target[1], "move_to"))
        else:
            raise ExpressionError(f"move_to: {target!r} is not a cell [cx, cy] or an agent "
                                  f"reference")
        wrapped = grid.wrap(*cell)
        if wrapped is None:
            raise ExpressionError(f"move_to: cell {list(cell)} is off the {grid.w} x {grid.h} "
                                  f"grid")
        self._set_cell(pop, i, *wrapped, float(self.env.now))

    def step(self, pop: _Population, i: int, dx, dy) -> None:
        """`step: [dx, dy]`: off the edge of a bounded grid the agent stays."""
        cell = pop.type.grid.wrap(int(pop.cx[i]) + _integer(dx, "step"),
                                  int(pop.cy[i]) + _integer(dy, "step"))
        if cell is not None:
            self._set_cell(pop, i, *cell, float(self.env.now))

    def move_random(self, pop: _Population, i: int, free: bool) -> None:
        """`move_random`: a neighbourhood cell drawn uniformly from the agent's
        own stream ("free": one holding no live agent of its population);
        with no candidate the agent stays and nothing is drawn."""
        cells = pop.type.grid.around(int(pop.cx[i]), int(pop.cy[i]))
        if free:
            cells = [(x, y) for x, y in cells if not pop.occ[:, y, x].any()]
        if cells:
            self._set_cell(pop, i, *cells[int(pop.rng(i).integers(len(cells)))],
                           float(self.env.now))

    def set_patch(self, pop: _Population, i: int, name: str, value) -> None:
        """A `patch.<name>` set: the live patch at agent i's cell."""
        values = pop.type.grid.patches[name]
        values[pop.cy[i], pop.cx[i]] = _number(value, f"patch.{name}")

    def _grid_agent(self, scope, what: str):
        """(population, index, grid) of the agent in hand, which must be on one."""
        if scope is None or scope.agent is None:
            raise ExpressionError(f"{what}: no agent is available in this context")
        pop, i = scope.agent
        if pop.type.grid is None:
            raise ExpressionError(f"{what}: agent type '{pop.type.name}' has no grid space")
        return pop, i, pop.type.grid

    def _grid_pop(self, name, grid: _Grid, what: str) -> _Population:
        pop = self.population(name)
        other = pop.type.grid
        if other is None or (other.w, other.h) != (grid.w, grid.h):
            raise ExpressionError(f"{what}(\"{name}\"): the population is not on a "
                                  f"{grid.w} x {grid.h} grid like self's")
        return pop

    def patch(self, name, dx, dy, scope) -> float:
        """PATCH(name[, dx, dy]): self's cell (+ offset) of its type's patch --
        the tick-start copy in a synchronous tick; 0 off a bounded grid."""
        pop, i, grid = self._grid_agent(scope, "PATCH")
        values = (grid.front or grid.patches).get(name)
        if values is None:
            raise ExpressionError(f"PATCH(\"{name}\"): agent type '{pop.type.name}' has no "
                                  f"such patch")
        cell = grid.wrap(int(pop.cx[i]) + _integer(dx, "PATCH"),
                         int(pop.cy[i]) + _integer(dy, "PATCH"))
        return 0.0 if cell is None else float(values[cell[1], cell[0]])

    def _state_code(self, pop: _Population, state) -> int:
        s = pop.type.index.get(state)
        if s is None:
            raise ExpressionError(f"population '{pop.name}' has no state '{state}'")
        return s

    def neighbours(self, name, state, scope) -> int:
        """NEIGHBOURS(pop[, state]): live agents of *pop* in self's
        neighbourhood cells (self's own cell excluded)."""
        me, i, grid = self._grid_agent(scope, "NEIGHBOURS")
        pop = self._grid_pop(name, grid, "NEIGHBOURS")
        cells = grid.around(int(me.cx[i]), int(me.cy[i]))
        if not cells:
            return 0
        xs, ys = [c[0] for c in cells], [c[1] for c in cells]
        if state is None:
            occ = pop.occ
        else:                           # K1: a composite sums its leaves' rows
            s = self._state_code(pop, state)
            leaves = pop.type.leaves[s]
            occ = pop.occ[s] if leaves is None else pop.occ[leaves]
        return int(occ[..., ys, xs].sum())

    def neighbour_aggregate(self, how: str, name, attr, state, scope) -> float:
        """NEIGHBOUR_SUM/MEAN(pop, attr[, state]): over NEIGHBOURS' agents."""
        me, i, grid = self._grid_agent(scope, f"NEIGHBOUR_{how}")
        pop = self._grid_pop(name, grid, f"NEIGHBOUR_{how}")
        cells = pop.cells       # #155: O(neighbourhood), not a scan of the population
        keys = {y * grid.w + x for x, y in grid.around(int(me.cx[i]), int(me.cy[i]))}
        found = np.array(sorted(j for k in keys for j in cells.get(k, ())), dtype=np.int64)
        if state is not None:
            found = found[pop.type.mask(pop.state[found], self._state_code(pop, state))]
        return self._reduce(pop, attr, how, found)

    def cell_count(self, name, dx, dy, scope) -> int:
        """CELL_COUNT(pop[, dx, dy]): live agents of *pop* in self's cell (+
        offset), self included; 0 off a bounded grid."""
        me, i, grid = self._grid_agent(scope, "CELL_COUNT")
        pop = self._grid_pop(name, grid, "CELL_COUNT")
        cell = grid.wrap(int(me.cx[i]) + _integer(dx, "CELL_COUNT"),
                         int(me.cy[i]) + _integer(dy, "CELL_COUNT"))
        return 0 if cell is None else int(pop.occ[:, cell[1], cell[0]].sum())

    def patch_values(self, type_name: str, name: str) -> np.ndarray:
        """Agent type *type_name*'s patch *name* now, (h, w), row cy (a copy)."""
        grid = next((g for g in self._grids if g.type_name == type_name), None)
        if grid is None or name not in grid.patches:
            raise KeyError(f"no patch '{name}' on agent type '{type_name}'")
        return grid.patches[name].copy()

    def _speed(self, pop: _Population, i: int, scope) -> float:
        speed = float(pop.type.speed_fn(scope))
        if not speed > 0:
            raise ExpressionError(f"agent type '{pop.type.name}': speed evaluated to {speed!r}, "
                                  f"must be > 0")
        return speed

    def _resolve_move_target(self, space_kind: str, target, now: float):
        """*target* (a `move_to` action's evaluated value) -> ((x, y), node
        name or None): a literal `[x, y]` (continuous only), a node name
        (network only), or another agent's current position/node (either)."""
        if isinstance(target, str):
            if space_kind != "network" or not self._network.has(target):
                raise ExpressionError(f"move_to: unknown network node '{target}'")
            return self._network.point(target), target
        if isinstance(target, (tuple, list)) and len(target) == 2:
            a, b = target
            if isinstance(a, str):                          # AGENT("Pop", i)
                other, index = self.deref(target if isinstance(target, tuple) else tuple(target))
                node = other.node[index] if other.node is not None else None
                return self._position(other, index, now), node
            if (isinstance(a, (int, float)) and not isinstance(a, bool)
                    and isinstance(b, (int, float)) and not isinstance(b, bool)):
                if space_kind == "network":
                    raise ExpressionError("move_to: a network space needs a node name or an "
                                          "agent at a node, not a point")
                return (float(a), float(b)), None
        raise ExpressionError(f"move_to: {target!r} is not a point, a node name or an agent "
                              f"reference")

    def move_to(self, pop: _Population, i: int, target, scope) -> None:
        """The `move_to` action (AB-3): resolve *target*, compute the travel
        time and schedule the arrival; a message ("arrived") delivers it,
        reusing D95's machinery instead of a new trigger kind."""
        space = pop.type.space
        if space is None:
            raise ExpressionError(f"agent type '{pop.type.name}' has no space: move_to needs "
                                  f"\"space\"")
        if pop.type.grid is not None:           # A4: instantaneous, cell to cell
            self._grid_move_to(pop, i, target)
            return
        now = float(self.env.now)
        x0, y0 = self._position(pop, i, now)
        (x1, y1), node = self._resolve_move_target(space["kind"], target, now)
        speed = self._speed(pop, i, scope)
        if space["kind"] == "network":
            start = pop.node[i] or None
            if start is None:
                raise ExpressionError(f"move_to: agent {i} of '{pop.name}' is mid-transit; wait "
                                      f"for \"arrived\" before its next move_to")
            path = self._network.path(start, node, speed) if node else []
            if not path:
                raise ExpressionError(f"move_to: no path from '{start}' to '{node}' on the "
                                      f"network")
            travel = self._network.travel_time(path, speed)
            pop.path[i] = tuple(path)
            pop.node[i] = ""                    # mid-transit: set again on arrival
        else:
            travel = math.hypot(x1 - x0, y1 - y0) / speed
            pop.path.pop(i, None)
        t1 = now + travel
        pop.x0[i], pop.y0[i], pop.x1[i], pop.y1[i] = x0, y0, x1, y1
        pop.t0[i], pop.t1[i] = now, t1
        pop.move_epoch[i] += 1
        self._trace_moved(pop, i, now, x0, y0, x1, y1, travel, pop.path.get(i, ()))
        heapq.heappush(self._moves, (t1, pop.index, i, int(pop.move_epoch[i])))

    # -- heading and steering (PHASE6 A6, D104) --------------------------------
    def _heading_target(self, pop: _Population, i: int, distance: float, now: float):
        """The endpoint of a straight `forward`/`random_walk` move of
        *distance* along self's heading, folded into the space's `bounds` by
        its `edge` rule: "none" (default, unbounded -- move_to's own
        convention), "clamp" (stop at the wall), "wrap" (torus, exact modulo)
        or "bounce" (an exact closed-form triangle-wave fold, so any distance
        -- even several widths -- reflects correctly with no per-step
        iteration, D68's own "no ODE solver" spirit). Returns ((x, y), the
        heading to keep -- unchanged except a bounce reflection)."""
        x0, y0 = self._position(pop, i, now)
        heading = math.radians(float(pop.heading[i]))
        x1 = x0 + distance * math.cos(heading)
        y1 = y0 + distance * math.sin(heading)
        edge = pop.type.space.get("edge", "none")
        if edge == "none":
            return (x1, y1), float(pop.heading[i])
        x0b, y0b, x1b, y1b = pop.type.space["bounds"]
        if edge == "clamp":
            return (min(max(x1, x0b), x1b), min(max(y1, y0b), y1b)), float(pop.heading[i])
        if edge == "wrap":
            return (x0b + (x1 - x0b) % (x1b - x0b),
                    y0b + (y1 - y0b) % (y1b - y0b)), float(pop.heading[i])

        def fold(v, lo, span):                  # a triangle wave: exact, any distance
            n, r = divmod(v - lo, span)
            return (lo + r, False) if int(n) % 2 == 0 else (lo + span - r, True)
        x, flip_x = fold(x1, x0b, x1b - x0b)
        y, flip_y = fold(y1, y0b, y1b - y0b)
        dx = math.cos(heading) * (-1 if flip_x else 1)
        dy = math.sin(heading) * (-1 if flip_y else 1)
        return (x, y), _norm360(math.degrees(math.atan2(dy, dx)))

    def forward(self, pop: _Population, i: int, distance: float, scope) -> None:
        """`forward: <distance>` outside on_tick: a TIMED segment along self's
        heading at `speed`, ending in an "arrived" message -- forward outside
        a tick IS a move_to towards the folded endpoint, so it needs no new
        arrival machinery (D95's own)."""
        if pop.heading is None:
            raise ExpressionError(f"agent type '{pop.type.name}' has no continuous space: "
                                  f"forward needs \"space\": {{\"kind\": \"continuous\"}}")
        (x1, y1), heading = self._heading_target(pop, i, float(distance), float(self.env.now))
        pop.heading[i] = heading
        self.move_to(pop, i, [x1, y1], scope)

    def forward_tick(self, pop: _Population, i: int, distance: float) -> None:
        """`forward` inside on_tick (NetLogo style): an instant jump -- the
        tick itself is the model's discrete step, so no travel time and no
        "arrived" message."""
        now = float(self.env.now)
        (x1, y1), heading = self._heading_target(pop, i, float(distance), now)
        pop.heading[i] = heading
        self._set_point(pop, i, x1, y1, now)

    def turn(self, pop: _Population, i: int, degrees: float) -> None:
        """`turn: <degrees>`: rotate self.heading, left positive, instant in
        or out of a tick -- it never touches position."""
        pop.heading[i] = _norm360(float(pop.heading[i]) + float(degrees))

    def face(self, pop: _Population, i: int, target) -> None:
        """`face: <target>`: point self.heading straight at a point `[x, y]`
        or another agent's CURRENT position, instant."""
        now = float(self.env.now)
        x0, y0 = self._position(pop, i, now)
        if isinstance(target, (tuple, list)) and len(target) == 2 and isinstance(target[0], str):
            other, j = self.deref(target if isinstance(target, tuple) else tuple(target))
            x1, y1 = self._position(other, j, now)
        elif isinstance(target, (tuple, list)) and len(target) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in target):
            x1, y1 = float(target[0]), float(target[1])
        else:
            raise ExpressionError(f"face: {target!r} is not a point or an agent reference")
        pop.heading[i] = _norm360(math.degrees(math.atan2(y1 - y0, x1 - x0)))

    def random_walk(self, pop: _Population, i: int, step: float, turn_sd: float, scope) -> None:
        """`random_walk: {step, turn_sd}` outside on_tick: turn by a normal
        draw (mean 0, sd *turn_sd*) from the agent's OWN stream (CRN), then
        forward (timed, "arrived" like any other move_to)."""
        self.turn(pop, i, float(scope.rng.normal(0.0, float(turn_sd))))
        self.forward(pop, i, float(step), scope)

    def random_walk_tick(self, pop: _Population, i: int, step: float, turn_sd: float,
                         scope) -> None:
        """`random_walk` inside on_tick: the same draw and turn, then an
        instant forward jump (NetLogo style)."""
        self.turn(pop, i, float(scope.rng.normal(0.0, float(turn_sd))))
        self.forward_tick(pop, i, float(step))

    def _positions(self, pop: _Population, t: float):
        """(live indices, points array, cKDTree) of *pop* at *t*, a one-slot
        cache keyed by the query instant (ARCHITECTURE 5.25): repeat
        NEAR/NEAREST calls at the same t -- the usual case, several agents
        querying one instant -- rebuild nothing; a birth or a death clears it
        (C5). Tree row k is agent `idx[k]`."""
        cached = self._tree_cache.get(pop.index)
        if cached is not None and cached[0] == t:
            return cached[1:]
        idx = pop.members()
        points = np.array([self._position(pop, int(i), t) for i in idx]).reshape(len(idx), 2)
        tree = cKDTree(points) if len(idx) else None
        self._tree_cache[pop.index] = (t, idx, points, tree)
        return idx, points, tree

    def _agent_point(self, scope) -> tuple[float, float, _Population, int]:
        if scope is None or scope.agent is None:
            raise ExpressionError("NEAR/NEAREST: no agent is available in this context")
        me_pop, me_i = scope.agent
        if me_pop.type.space is None:
            raise ExpressionError(f"agent type '{me_pop.type.name}' has no space")
        x, y = self._position(me_pop, me_i, float(self.env.now))
        return x, y, me_pop, me_i

    def _neighbours(self, name, r, state, scope) -> np.ndarray:
        """Agent indices of *name* within radius r of self, inclusive (AB-3),
        alive, state-filtered, self excluded when *name* is self's own
        population (D68-cached cKDTree, exact -- `query_ball_point`). Shared
        by NEAR and, PHASE6 A3, NEAR_SUM/NEAR_MEAN/NEAR_MIN/NEAR_MAX."""
        pop = self.population(name)
        if pop.type.space is None:
            raise ExpressionError(f"population '{name}' has no space: neighbour queries need "
                                  f"\"space\"")
        x, y, me_pop, me_i = self._agent_point(scope)
        idx, _points, tree = self._positions(pop, float(self.env.now))
        if tree is None:
            return np.empty(0, dtype=int)
        found = idx[tree.query_ball_point((x, y), float(r))]
        if state is not None:
            s = pop.type.index.get(state)
            if s is None:
                raise ExpressionError(f"population '{name}' has no state '{state}'")
            found = found[pop.type.mask(pop.state[found], s)]
        if pop is me_pop:
            found = found[found != me_i]
        return found

    def near(self, name, r, state, scope) -> int:
        """NEAR(pop, r[, state]): how many agents are in `_neighbours`."""
        return len(self._neighbours(name, r, state, scope))

    def _reduce(self, pop: _Population, attr: str, how: str, found: np.ndarray) -> float:
        """SUM/MEAN/MIN/MAX of *pop*'s *attr* column over agent indices
        *found* (PHASE6 A3): a synchronous tick reads its start (A1, same
        `view()` as `aggregate`/`count`). SUM/MEAN of an empty *found* is 0
        (`aggregate`'s existing rule for an empty selection); MIN/MAX has no
        identity element, so an empty *found* is NaN instead."""
        states, attrs = pop.view()
        front = pop.front
        if front is not None:
            # #148: the live index may hold a newborn -- in a grown slot past
            # the snapshot, or in a slot freed this tick (a new generation):
            # invisible to snapshot reads in its birth tick, like COUNT.
            found = found[found < len(states)]
            found = found[(states[found] >= 0) & (front[4][found] == pop.gen[found])]
        column = attrs.get(attr)
        if column is None:
            raise ExpressionError(f"population '{pop.name}' has no attribute '{attr}'")
        values = column[found]
        if how == "SUM":
            return float(values.sum()) if len(values) else 0.0
        if how == "MEAN":
            return float(values.mean()) if len(values) else 0.0
        if how == "MIN":
            return float(values.min()) if len(values) else math.nan
        return float(values.max()) if len(values) else math.nan

    def near_aggregate(self, how: str, name, attr, r, state, scope) -> float:
        """NEAR_SUM/NEAR_MEAN/NEAR_MIN/NEAR_MAX(pop, attr, r[, state])
        (PHASE6 A3): *how* reduced over the same neighbourhood NEAR counts."""
        pop = self.population(name)
        return self._reduce(pop, attr, how, self._neighbours(name, r, state, scope))

    def near_heading(self, name, r, state, scope) -> float:
        """NEAR_HEADING(pop, r[, state]) (PHASE6 A6): the CIRCULAR mean
        heading of the same neighbourhood NEAR counts -- a plain mean would
        average 350 and 10 to 180, not 0. NaN with none (MIN/MAX's own "no
        identity element" rule, A3); composes with NEAR_SUM/NEAR_MEAN
        (existing) for boids-style separation/alignment/cohesion, no
        flocking engine of its own."""
        pop = self.population(name)
        if pop.heading is None:
            raise ExpressionError(f"population '{name}' has no continuous space: NEAR_HEADING "
                                  f"needs \"space\": {{\"kind\": \"continuous\"}}")
        found = self._neighbours(name, r, state, scope)
        if not len(found):
            return math.nan
        radians = np.radians(pop.heading[found])
        return _norm360(math.degrees(math.atan2(np.sin(radians).mean(), np.cos(radians).mean())))

    def knn_aggregate(self, how: str, name, attr, k, state, scope) -> float:
        """KNN_SUM/KNN_MEAN(pop, attr, k[, state]) (PHASE6 A3): *how* reduced
        over the k nearest live agents of *pop* to self (self excluded when
        *pop* is self's own), *state*-filtered BEFORE the k are chosen. Ties
        at the cutoff distance are broken by ascending agent index; that
        needs the exact sorted order, which `cKDTree.query(k=k)` does not
        guarantee at a tie, so this sorts the cached (AB-3) positions with
        `np.lexsort` instead of querying the tree for k."""
        pop = self.population(name)
        if pop.type.space is None:
            raise ExpressionError(f"population '{name}' has no space: neighbour queries need "
                                  f"\"space\"")
        x, y, me_pop, me_i = self._agent_point(scope)
        idx, points, _tree = self._positions(pop, float(self.env.now))
        if state is not None:
            s = pop.type.index.get(state)
            if s is None:
                raise ExpressionError(f"population '{name}' has no state '{state}'")
            mask = pop.type.mask(pop.state[idx], s)
            idx, points = idx[mask], points[mask]
        if pop is me_pop:
            mask = idx != me_i
            idx, points = idx[mask], points[mask]
        k = max(0, min(int(k), len(idx)))
        if k < len(idx):
            d2 = (points[:, 0] - x) ** 2 + (points[:, 1] - y) ** 2
            idx = idx[np.lexsort((idx, d2))[:k]]
        return self._reduce(pop, attr, how, idx)

    def nearest(self, name, state, scope) -> float:
        """NEAREST(pop[, state]): distance to the closest OTHER agent of
        *pop* (`inf` if there is none)."""
        pop = self.population(name)
        if pop.type.space is None:
            raise ExpressionError(f"population '{name}' has no space: NEAREST needs \"space\"")
        x, y, me_pop, me_i = self._agent_point(scope)
        now = float(self.env.now)
        if state is not None:
            s = pop.type.index.get(state)
            if s is None:
                raise ExpressionError(f"population '{name}' has no state '{state}'")
            idx = np.flatnonzero(pop.type.mask(pop.state, s))
            if pop is me_pop:
                idx = idx[idx != me_i]
            if len(idx) == 0:
                return math.inf
            pts = np.array([self._position(pop, int(j), now) for j in idx])
            return float(np.hypot(pts[:, 0] - x, pts[:, 1] - y).min())
        live, _points, tree = self._positions(pop, now)
        k = 2 if pop is me_pop else 1
        if tree is None or len(live) < k:
            return math.inf
        dist, rows = tree.query((x, y), k=k)
        dist, rows = np.atleast_1d(dist), np.atleast_1d(rows)
        if pop is me_pop:
            for d, j in zip(dist, live[rows]):
                if j != me_i:
                    return float(d)
            return math.inf
        return float(dist[0])

    # -- links and networks (PHASE6 A5, D103) ----------------------------------
    def _build_networks(self, now: float) -> None:
        """Each declared network's initial links, drawn from the population's
        own network stream `stream_seed(seed, pop, hash("network"))` (D107)."""
        for pop, spec in self._networks:
            kind, n = spec.get("kind", "none"), pop.n
            links = self._links[pop.index] = _Links(bool(spec.get("directed")),
                                                    bool(spec.get("link_on_spawn")))
            rng = np.random.default_rng(stream_seed(self.seed, pop.name, _NETWORK_TAG))
            for i, j in self._generate(pop, spec, kind, n, rng):
                if i != j and links.add(pop, i, j):
                    self._trace_link(pop, i, j, now, True)

    def _generate(self, pop: _Population, spec: dict, kind: str, n: int, rng) -> list:
        """The (i, j) links of a network spec over agents 0..n-1."""
        what = f"population '{pop.name}': network"
        if kind == "random":
            p = spec["p"] if "p" in spec else spec["mean_degree"] / (n - 1) if n > 1 else 0.0
            if not 0 <= p <= 1:
                raise ExpressionError(f"{what}: mean_degree {spec.get('mean_degree')} exceeds "
                                      f"count - 1 ({n - 1})")
            return _erdos_renyi(n, float(p), bool(spec.get("directed")), rng)
        if kind in ("small_world", "scale_free"):
            size = int(spec["k" if kind == "small_world" else "m"])
            if size >= n:
                raise ExpressionError(f"{what}: {'k' if kind == 'small_world' else 'm'} = "
                                      f"{size} needs more than {size} agents, has {n}")
            return _watts_strogatz(n, size, float(spec.get("beta", 0)), rng) \
                if kind == "small_world" else _barabasi_albert(n, size, rng)
        if kind == "table":
            table = self.tables.get(spec.get("table")) or {}
            ends = [_table_column(table, column)[0] or [] for column in ("from", "to")]
            if any(not (v.is_integer() and 0 <= v < n) for v in ends[0] + ends[1]):
                raise ExpressionError(f"{what}: table '{spec.get('table')}' names an agent "
                                      f"outside 0..{n - 1}")
            return [(int(a), int(b)) for a, b in zip(*ends)]
        return []

    def link(self, pop: _Population, i: int, targets, on: bool) -> None:
        """A `link`/`unlink` action: agent i to or from each target, all of its
        own population (links never cross populations); i itself is skipped."""
        links = self._links.get(pop.index)
        if links is None:
            if not on:
                return
            links = self._links[pop.index] = _Links()
        now = float(self.env.now)
        for other, j in targets:
            if other is not pop:
                raise ExpressionError(f"{'link' if on else 'unlink'}: agent {j} of "
                                      f"'{other.name}': links join agents of one population "
                                      f"('{pop.name}')")
            if j != i and (links.add(pop, i, j) if on else links.remove(pop, i, j)):
                self._trace_link(pop, i, j, now, on)

    def _link_newborn(self, pop: _Population, i: int, parent) -> None:
        """`link_on_spawn`: a newborn starts linked to the agent that spawned
        it, when that agent is of the same population; else unlinked."""
        links = self._links.get(pop.index)
        agent = getattr(parent, "agent", None)
        if links is not None and links.on_spawn and agent is not None and agent[0] is pop \
                and links.add(pop, agent[1], i):
            self._trace_link(pop, agent[1], i, float(self.env.now), True)

    def _linked(self, pop: _Population, i: int, state=None, live: bool = False) -> np.ndarray:
        """Agent i's linked neighbours, ascending, alive and (optionally) in
        *state*. An expression read (not *live*) sees a synchronous tick's
        start (D99: the links' snapshot and `view()`); a send or a link action
        resolves over the live links and agents, as a population send does."""
        links = self._links.get(pop.index)
        if links is None:
            return np.empty(0, dtype=np.int64)
        found = links.row(pop, i, live)
        states = pop.state if live else pop.view()[0]
        found = found[found < len(states)]
        if state is None:
            return found[states[found] >= 0]
        return found[pop.type.mask(states[found], self._state_code(pop, state))]

    def _link_self(self, scope, what: str, name=None) -> tuple:
        if scope is None or scope.agent is None:
            raise ExpressionError(f"{what}: no agent is available in this context")
        pop, i = scope.agent
        if name is not None and self.population(name) is not pop:
            raise ExpressionError(f"{what}(\"{name}\"): links join agents of one population; "
                                  f"self is in '{pop.name}'")
        return pop, i

    def linked(self, scope) -> tuple:
        """`to: "links"`: (self's population, its live linked neighbours)."""
        pop, i = self._link_self(scope, "to: \"links\"")
        return pop, self._linked(pop, i, live=True)

    def link_count(self, name, state, scope) -> int:
        """LINKED(pop[, state]): self's linked neighbours (pop: self's own)."""
        pop, i = self._link_self(scope, "LINKED", name)
        return len(self._linked(pop, i, state))

    def link_aggregate(self, how: str, attr, state, scope) -> float:
        """LINK_SUM/LINK_MEAN(attr[, state]) over self's linked neighbours (0 if none)."""
        pop, i = self._link_self(scope, f"LINK_{how}")
        return self._reduce(pop, attr, how, self._linked(pop, i, state))

    def is_linked(self, ref, scope) -> bool:
        """IS_LINKED(ref): whether self links to that agent (directed: self -> it)."""
        pop, i = self._link_self(scope, "IS_LINKED")
        other, j = self.deref(ref)
        return other is pop and bool((self._linked(pop, i) == j).any())

    def links_of(self, name) -> list:
        """Population *name*'s live links as sorted (i, j) pairs (i < j when
        undirected) -- for tests and reports."""
        pop = self.population(name)
        links = self._links.get(pop.index)
        if links is None:
            return []
        return sorted((i, j) for i, js in links.out.items() for j in js
                      if links.directed or i < j)

    def _trace_link(self, pop: _Population, i: int, j: int, now: float, on: bool) -> None:
        """`agent_linked`/`agent_unlinked` for two traced agents (the D75 cap):
        `index` and `value` carry the ends, ordered when undirected."""
        bus = self.env.ctx.bus
        if max(i, j) >= self.trace_agents or not bus.has_subscribers:
            return
        if not self._links[pop.index].directed and j < i:
            i, j = j, i
        bus.emit("agent_linked" if on else "agent_unlinked", block=None, t=now, pop=pop.name,
                 index=i, value=float(j), e_type=pop.type.name)


def _integer(value, what: str) -> int:
    """A cell coordinate or offset: a whole number (PHASE6 A4)."""
    number = _number(value, what)
    if not (math.isfinite(number) and number == int(number)):
        raise ExpressionError(f"{what}: {value!r} is not a whole number of cells")
    return int(number)
