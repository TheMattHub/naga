"""The agent runtime (PHASE5 AB-1..AB-5, D93): `AgentSystem`, the one process
that runs every population, its columnar `_Population`s, the integrated-hazard
`_Hazard` heaps and `build_agent_system`. Space, grid, heading and links are
`AgentSystem` methods kept in `space._SpaceMixin`."""
from __future__ import annotations

import heapq
import math
from bisect import bisect_right
from collections import deque

import numpy as np

from nagasim.core.agents.common import (MAX_FIRINGS_PER_INSTANT, _RUNAWAY, _TICK_TAG, _norm360,
                                        _number, _resolve_count, _table_column, stream_seed,
                                        timing)
from nagasim.core.agents.compile import _Transition, _Type, _run_vec, _tick_in_base
from nagasim.core.agents.space import _SpaceMixin
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.network import build_network
from nagasim.core.quantities import scaled
from nagasim.core.sim_classes import spawn_entity
from nagasim.core.utils import timeout_at


class _Stream:
    """Agent i's stream, resolved per draw (AB-4, D107): each attribute read
    is one `_Population.rng(i)` call, so a draw depends on (seed, population,
    i, that agent's calls so far) alone."""

    __slots__ = ("pop", "i")

    def __init__(self, pop, i: int):
        self.pop, self.i = pop, i

    def __getattr__(self, name):
        return getattr(self.pop.rng(self.i), name)


class AgentRef(tuple):
    """A stored agent reference (PHASE6 C5, D98): still the `(population,
    index)` pair -- unpacking, `==` with a plain pair and every AB-2 use are
    unchanged -- tagged with the slot's generation, so `AgentSystem.deref`
    refuses a ref that outlived its agent instead of handing over whoever
    took the slot since."""

    def __new__(cls, population: str, index: int, gen: int = 0):
        ref = super().__new__(cls, (population, index))
        ref.gen = gen
        return ref

    def __getnewargs__(self):       # pickle/copy rebuild it with its generation
        return (self[0], self[1], self.gen)


class _Population:
    """Columnar storage and per-state statistics of one population. C5: the
    columns are a capacity; `n` slots are in use, `live` of them alive
    (`state >= 0`), the dead ones on the `free` heap."""

    def __init__(self, system, index: int, entry: dict, kind: _Type):
        self.system, self.index, self.name, self.type = system, index, entry["name"], kind
        # count (C3): {"table": name[, "column": col]} resolves against the
        # run's tables -- validation already refused a bad one, so a leftover
        # error here (a scenario override gone bad) resolves to 0 rather than
        # raising deep inside construction.
        raw_count = entry.get("count", 0)
        if isinstance(raw_count, dict):
            resolved, _error = _resolve_count(raw_count, system.tables)
            count = resolved if resolved is not None else 0
        else:
            count = raw_count
        self.n = self.live = self.initial_count = n = int(count)
        self.max_count = int(entry.get("max_count", n))
        self.state = np.full(n, kind.initial, dtype=np.int16)
        self.entered = np.zeros(n)
        # One epoch per depth (K1, D137): a pending trigger of a state at depth
        # d stays live until some state at depth d is exited. A flat type has
        # one row, the old single epoch.
        self.epoch = np.zeros((kind.levels, n), dtype=np.int32)
        self.epochs = list(self.epoch)      # row views: 1D indexing on the hot paths
        # K1: the entry time of the active state at each depth (composite
        # sojourns), and per remembered composite its (last child, last leaf)
        # per agent, -1 = no record. Empty for a flat type.
        self.since = np.zeros((kind.levels, n)) if kind.nested else None
        self.hist = {c: np.full((2, n), -1, dtype=np.int16) for c in kind.remember}
        self.gen = np.zeros(n, dtype=np.int32)      # bumped by each removal (C5)
        self.free: list = []                        # dead slots, a heap: lowest reused first
        self.births = 0                             # the birth serial (D98)
        self._keys: dict = {}                       # slot -> stream key of a newborn
        self.ids = np.arange(n)
        self.attrs = {name: np.zeros(n) for name in kind.attr_names}
        # PHASE6 C2: each NUMERIC attribute's running sum over the ALIVE
        # population and its exact time integral -- the same treatment
        # `counts`/`area` already give a per-state count, extended to a value.
        # A categorical attribute (A2) has no mean, so it keeps neither: every
        # `for name in pop.attr_total` loop below skips it for free. Seeded
        # once attributes get their initial values (AgentSystem.start), kept
        # current by every set action, birth and death; empty (zero cost) for
        # a type with none.
        self.attr_total = {name: 0.0 for name in kind.attr_names if name not in kind.attr_cat}
        self.attr_area = {name: 0.0 for name in kind.attr_names if name not in kind.attr_cat}
        # PHASE6 A2: which categorical attribute, if any, playback colours a
        # token by; read straight off the population entry (a pass-through
        # field, validated by `_check_color_by`).
        self.color_by = entry.get("color_by")
        # Data-driven attribute columns (C3): row i of the population's own
        # table for agent i, straight from the raw data -- no draw, no
        # expression, so CRN and every other agent's draw order are untouched.
        # Only a row-per-agent table (count = {"table": ...}, no "column")
        # qualifies; _check_population_data refuses the rest.
        if isinstance(raw_count, dict) and raw_count.get("column") is None:
            table = system.tables.get(raw_count.get("table"))
            if table is not None:
                for aname, column in zip(kind.attr_names, kind.attr_columns):
                    if column is None:
                        continue
                    values, _error = _table_column(table, column, kind.attr_cat.get(aname))
                    if values is not None:
                        self.attrs[aname][:min(n, len(values))] = values[:n]
        placed = 0
        for item in entry.get("initial") or []:
            count = int(item.get("count", 0))
            self.state[placed:placed + count] = kind.leaf[kind.index[item["state"]]]
            placed += count
        self.draws = np.zeros(n, dtype=np.uint32)   # D107: stream calls so far, per slot
        self._bits = None                           # the population's Philox, keyed on first use
        states = len(kind.states)
        self.counts = [int(c) for c in np.bincount(self.state, minlength=states)]
        for c in kind.composites:       # K1: a composite counts its leaves' agents
            self.counts[c] = sum(self.counts[x] for x in kind.leaves[c].tolist())
        self.minimum, self.maximum = list(self.counts), list(self.counts)
        self.area = [0.0] * states
        self.entries, self.exits = [0] * states, [0] * states
        self.sojourn = [0.0] * states
        self.dropped = [0] * states     # messages that matched nothing, per state (AB-2)
        self.t_last = self.t_start = float(system.env.now)
        self.shared_scope = ExpressionScope(ctx=system.env.ctx, locals=kind.params)
        # Position (AB-3): a linear motion segment per agent, x0,y0 at t0 to
        # x1,y1 at t1 -- exact at any t with no per-tick update (D68's own
        # trick, `core/network.point_on_path`, reused for a network mover's
        # bends via `path`). Allocated only for a population whose type has a
        # space, so a model with none pays nothing extra.
        self.placement = entry.get("placement") or {}
        if kind.space is not None:
            self.x0, self.y0 = np.zeros(n), np.zeros(n)
            self.x1, self.y1 = np.zeros(n), np.zeros(n)
            self.t0, self.t1 = np.zeros(n), np.zeros(n)
            self.move_epoch = np.zeros(n, dtype=np.int32)
            self.node = [""] * n if kind.space.get("kind") == "network" else None
            self.path: dict = {}        # i -> node tuple, only while in transit
            # Heading (PHASE6 A6, D104): degrees, 0 = east, counter-clockwise;
            # continuous space only -- a grid or network move has no free
            # direction to steer, so this pays nothing there.
            self.heading = np.zeros(n) if kind.space.get("kind") == "continuous" else None
        else:
            self.node = self.path = self.heading = None
        # Grid (PHASE6 A4): each agent's cell, and how many live agents of each
        # state sit in each cell -- kept by every placement, move, state
        # change and death, so NEIGHBOURS/CELL_COUNT/"free" are O(cells).
        grid = kind.grid
        if grid is not None:
            self.cx, self.cy = np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
            self.occ = np.zeros((states, grid.h, grid.w), dtype=np.int64)
            self.cells: dict = {}       # cy * w + cx -> its live agents (#155)
        else:
            self.occ = None
        # Ticks (A1): the population's keys override the type's; no dt, no ticks.
        units = system.env.ctx.units
        tick = {**_tick_in_base(kind.tick, units), **_tick_in_base(entry.get("tick") or {}, units)}
        self.tick_dt = float(tick["dt"]) if tick.get("dt") else None
        self.tick_mode = tick.get("activation", "synchronous")
        self.tick_phase = float(tick.get("phase", 0))
        self.tick_k = 0             # the next tick's integer index
        # During a synchronous tick, the tick-start (state, attrs, counts,
        # live, gen) every expression reads; None otherwise (D99).
        self.front = None
        self.lets = None            # A9: i -> its let values, during a synchronous tick

    def view(self):
        """(state, attrs) as expressions read them: the synchronous-tick
        snapshot while one runs (A1), else the live columns."""
        front = self.front
        return (self.state, self.attrs) if front is None else front[:2]

    def rng(self, i: int):
        """Agent i's stream for ONE call (D107): the population's Philox, key
        `stream_seed(seed, pop)`, set to counter `(0, n, key_i, 0)` with an
        empty buffer -- n this agent's calls so far, key_i its birth serial or
        slot (D98). Draws are a pure function of (seed, pop, key_i, n): no
        other agent, nor event order, moves them; 4 bytes per agent. Draw
        from it before the next `rng()` of this population."""
        if self._bits is None:
            key = stream_seed(self.system.seed, self.name).generate_state(2, np.uint64)
            self._bits = np.random.Philox(key=key)
            self._gen = np.random.Generator(self._bits)
            self._ctr = np.zeros(4, dtype=np.uint64)
            self._state = {"bit_generator": "Philox", "state": {"counter": self._ctr, "key": key},
                           "buffer": np.zeros(4, dtype=np.uint64), "buffer_pos": 4,
                           "has_uint32": 0, "uinteger": 0}
        n = int(self.draws[i])
        self.draws[i] = n + 1
        self._ctr[1], self._ctr[2] = n, self._keys.get(i, i)
        self._bits.state = self._state
        return self._gen

    def members(self):
        """The live agents' indices, ascending: the order every scan uses (C5)."""
        if self.live == self.n:
            return self.ids if len(self.ids) == self.n else self.ids[:self.n]
        return np.flatnonzero(self.state[:self.n] >= 0)

    def grow(self) -> None:
        """Double the columns, up to `max_count` (C5); new slots are dead."""
        cap = len(self.state)
        extra = min(self.max_count, max(2 * cap, 8)) - cap

        def pad(column, fill=0):
            return np.concatenate([column, np.full(extra, fill, dtype=column.dtype)])

        def pad2(rows, fill=0):         # K1: the per-depth / history columns
            return np.concatenate([rows, np.full((len(rows), extra), fill, dtype=rows.dtype)],
                                  axis=1)
        self.state = pad(self.state, -1)
        self.entered, self.epoch, self.gen = pad(self.entered), pad2(self.epoch), pad(self.gen)
        self.epochs = list(self.epoch)
        if self.since is not None:
            self.since = pad2(self.since)
        self.hist = {c: pad2(rows, -1) for c, rows in self.hist.items()}
        self.draws = pad(self.draws)
        self.attrs = {name: pad(column) for name, column in self.attrs.items()}
        if self.type.space is not None:
            for name in ("x0", "y0", "x1", "y1", "t0", "t1", "move_epoch"):
                setattr(self, name, pad(getattr(self, name)))
            if self.node is not None:
                self.node += [""] * extra
            if self.heading is not None:             # PHASE6 A6
                self.heading = pad(self.heading)
        if self.occ is not None:                    # PHASE6 A4
            self.cx, self.cy = pad(self.cx), pad(self.cy)
        self.ids = np.arange(cap + extra)

    def scope(self, i: int, locals_=None) -> ExpressionScope:
        return ExpressionScope(ctx=self.system.env.ctx, rng=_Stream(self, i),
                               locals=self.type.params if locals_ is None else locals_,
                               agent=(self, i))

    def read(self, attr: str, i: int):
        """`self.<attr>`: a declared column, the state name or the index --
        a categorical column (PHASE6 A2) reads back its category NAME, so a
        plain `self.attr == "red"` works through the general evaluator with no
        extra grammar, exactly as `self.state == "<name>"` already does. In a
        synchronous tick (A1) an agent alive at its start reads the snapshot."""
        state, attrs, front = self.state, self.attrs, self.front
        if front is not None and i < len(front[4]) and front[0][i] >= 0 \
                and (front[4][i] == self.gen[i] or self.state[i] < 0):     # #152: removed since
            state, attrs = front[0], front[1]
        if attr == "state":
            return self.type.states[state[i]]
        if attr == "id":
            return int(i)
        if attr == "heading" and self.heading is not None:      # PHASE6 A6: live, like position
            return float(self.heading[i])
        column = attrs.get(attr)
        if column is None:
            if attr in ("x", "y") and self.type.space is not None:     # PHASE6 A9: live
                point = self.system._position(self, i, float(self.system.env.now))
                return point[0] if attr == "x" else point[1]
            raise ExpressionError(f"agent type '{self.type.name}' has no attribute '{attr}'")
        categories = self.type.attr_cat.get(attr)
        if categories is not None:
            return categories[int(column[i])]
        return float(column[i])

    def accrue(self, t: float) -> None:
        dt = t - self.t_last
        if dt > 0:
            for s, count in enumerate(self.counts):
                if count:
                    self.area[s] += count * dt
            if self.live:                            # PHASE6 C2
                for name, total in self.attr_total.items():
                    self.attr_area[name] += (total / self.live) * dt
            self.t_last = t

    def move(self, i: int, dst: int, now: float) -> None:
        src = int(self.state[i])
        self.accrue(now)
        self.exits[src] += 1
        self.sojourn[src] += now - self.entered[i]
        self.entries[dst] += 1
        if src != dst:      # a self-loop re-enters without ever leaving the count
            if self.occ is not None:                # PHASE6 A4: the cell's per-state count
                self.occ[src, self.cy[i], self.cx[i]] -= 1
                self.occ[dst, self.cy[i], self.cx[i]] += 1
            self.counts[src] -= 1
            self.minimum[src] = min(self.minimum[src], self.counts[src])
            self.counts[dst] += 1
            self.maximum[dst] = max(self.maximum[dst], self.counts[dst])
        self.state[i] = dst
        self.entered[i] = now
        self.epochs[0][i] += 1

    def shift(self, i: int, exited: tuple, entered: tuple, now: float) -> None:
        """K1 `move`: agent i leaves *exited* (active leaf first) and enters
        *entered* (outermost first). Entries/exits count every boundary
        crossed; a state in both (re-entered) keeps its count, as a flat
        self-loop does; every exited depth's epoch moves on."""
        kind, since = self.type, self.since
        self.accrue(now)
        for x in exited:
            self.exits[x] += 1
            self.sojourn[x] += now - since[kind.depth[x], i]
        for y in entered:
            self.entries[y] += 1
        src, dst = exited[0], entered[-1]
        if src != dst and self.occ is not None:     # PHASE6 A4: leaf rows only
            self.occ[src, self.cy[i], self.cx[i]] -= 1
            self.occ[dst, self.cy[i], self.cx[i]] += 1
        for x in exited:
            if x not in entered:
                self.counts[x] -= 1
                self.minimum[x] = min(self.minimum[x], self.counts[x])
        for y in entered:
            if y not in exited:
                self.counts[y] += 1
                self.maximum[y] = max(self.maximum[y], self.counts[y])
        self.state[i] = dst
        self.entered[i] = now
        for y in entered:
            since[kind.depth[y], i] = now
        for x in exited:
            self.epochs[kind.depth[x]][i] += 1

    def born(self, i: int, s: int, now: float) -> None:
        """Slot i holds a newborn in leaf *s* (C5): counted (with its
        composites, K1), not an Entry (like the initial placement); a
        recycled slot's history records are cleared."""
        self.accrue(now)
        self.state[i], self.entered[i] = s, now
        self.live += 1
        for x in self.type.path[s]:
            self.counts[x] += 1
            self.maximum[x] = max(self.maximum[x], self.counts[x])
        if self.since is not None:
            self.since[:, i] = now
        for rows in self.hist.values():
            rows[:, i] = -1

    def died(self, i: int, now: float) -> int:
        """Agent i leaves the population (C5): its stay counts as an exit (Avg
        Sojourn) of its leaf and every composite around it (K1); epochs and
        generation move on, so every pending entry and every stored ref to it
        goes stale. Returns the leaf it left."""
        s = int(self.state[i])
        self.accrue(now)
        for x in self.type.path[s]:
            self.exits[x] += 1
            self.sojourn[x] += now - (self.entered[i] if x == s
                                      else self.since[self.type.depth[x], i])
            self.counts[x] -= 1
            self.minimum[x] = min(self.minimum[x], self.counts[x])
        for name in self.attr_total:                 # PHASE6 C2: leaves attr_total too
            self.attr_total[name] -= self.attrs[name][i]
        if self.occ is not None:                     # PHASE6 A4: leaves its cell
            self.occ[s, self.cy[i], self.cx[i]] -= 1
            key = int(self.cy[i]) * self.occ.shape[2] + int(self.cx[i])
            self.cells.get(key, set()).discard(i)
        self.state[i] = -1
        self.epoch[:, i] += 1
        self.gen[i] += 1
        self.live -= 1
        heapq.heappush(self.free, i)
        self.draws[i] = 0
        self._keys.pop(i, None)
        return s

    def reset_stats(self, t: float) -> None:
        self.accrue(t)
        self.t_start = t
        self.area = [0.0] * len(self.counts)
        self.minimum, self.maximum = list(self.counts), list(self.counts)
        self.entries, self.exits = [0] * len(self.counts), [0] * len(self.counts)
        self.sojourn = [0.0] * len(self.counts)
        self.dropped = [0] * len(self.counts)
        self.attr_area = {name: 0.0 for name in self.attr_area}     # PHASE6 C2

    def summary(self, t1: float) -> list[dict]:
        """One row per state, over [t_start, t1]; "Dropped Messages" counts
        the messages that reached an agent in that state and matched nothing."""
        self.accrue(t1)
        span = t1 - self.t_start
        return [{"Population": self.name, "State": state,
                 "Avg Count": self.area[s] / span if span > 0 else float(self.counts[s]),
                 "Min": self.minimum[s], "Max": self.maximum[s], "Final": self.counts[s],
                 "Entries": self.entries[s],
                 "Avg Sojourn": self.sojourn[s] / self.exits[s] if self.exits[s] else math.nan,
                 "Dropped Messages": self.dropped[s]}
                for s, state in enumerate(self.type.states)]

    def attr_time_avg(self, name: str, t1: float) -> float:
        """PHASE6 C2: the exact time average of self.<name>'s mean over the
        ALIVE population over [t_start, t1] -- `summary()`'s own treatment of
        Avg Count, extended from a count to a value; piecewise constant
        between agent events, so no sampling is needed. NaN if never alive."""
        self.accrue(t1)
        span = t1 - self.t_start
        if span > 0:
            return self.attr_area[name] / span
        return self.attr_total[name] / self.live if self.live else math.nan


class _Hazard:
    """The integrated-hazard heap of one rate transition (D93): entries
    (key, index, epoch at its source's depth, K1); `big` is Lambda at `t0`,
    `lam` the shared rate since."""

    __slots__ = ("pop", "tr", "heap", "lam", "big", "t0", "depth")

    def __init__(self, pop: _Population, tr: _Transition):
        self.pop, self.tr, self.heap, self.depth = pop, tr, [], tr.depth    # K1
        # An own-only rate has shared part 1, so Lambda(t) = t and a key IS
        # the absolute firing time.
        self.lam, self.big, self.t0 = (0.0 if tr.shared else 1.0), 0.0, 0.0

    def integral(self, t: float) -> float:
        return self.big + self.lam * (t - self.t0)

    def time_of(self, key: float) -> float:
        return self.t0 + (key - self.big) / self.lam if self.lam > 0 else math.inf

    def top(self):
        """The smallest live entry, dropping stale ones (lazy deletion)."""
        heap, epoch = self.heap, self.pop.epochs[self.depth]
        while heap and epoch[heap[0][1]] != heap[0][2]:
            heapq.heappop(heap)
        return heap[0] if heap else None


class AgentSystem(_SpaceMixin):
    """The one process that runs every population (D93). `trigger()` wakes it
    when something a rate or a condition reads changed outside it (an ASSIGN
    writing a variable); `count`/`aggregate` answer COUNT/SUM/MEAN."""

    def __init__(self, env, types: list, populations: list, tables: dict | None = None,
                 network_section=None, trace_agents: int = 200):
        self.env, self.seed = env, env.ctx.seed
        # AB-3: built independently of `ctx.network` (graph_model.py builds
        # THAT after the agent system, PHASE4 E1 order); geometry is stdlib
        # and cheap, so a second Network costs nothing a model with no
        # network-space agents pays even that for.
        self._network = build_network(network_section, env.ctx.units)
        self.trace_agents = int(trace_agents) if trace_agents is not None else 200
        # Data tables (C3): the raw "tables" section entries, by name, so a
        # population's count/attribute inits can read them at construction
        # below -- built once here rather than per population.
        self.tables = tables or {}
        variables = set(getattr(env.ctx, "variables", {}) or {})
        compiled = {entry["name"]: _Type(entry, variables, env.ctx.units) for entry in types}
        self.populations = [_Population(self, p, entry, compiled[entry["type"]])
                            for p, entry in enumerate(populations)]
        self.by_name = {pop.name: pop for pop in self.populations}
        self._nested = any(pop.type.nested for pop in self.populations)     # K1
        self.cap = max(MAX_FIRINGS_PER_INSTANT,
                       100 * sum(pop.max_count for pop in self.populations))
        self._nodes = sorted(self._network.nodes)       # random network placement
        self._starting = False      # inside start(): births and deaths wait for the run (C5)
        self.hazards = [_Hazard(pop, tr) for pop in self.populations
                        for tr in pop.type.transitions if tr.kind == "rate"]
        self._hazard_of = {(h.pop.index, h.tr.k): h for h in self.hazards}
        self.conditions = [(pop, tr) for pop in self.populations
                           for tr in pop.type.transitions if tr.kind == "condition"]
        self._timeouts: list = []           # (time, population, index, transition, epoch)
        # Change instants, TS knots and scan grids of every rate and condition
        # ("message"'s text is a NAME, not an expression -- skipped like
        # "timeout", PHASE5 AB-2).
        instants, series = set(), set()
        self._scans = []
        for pop in self.populations:
            for tr in pop.type.transitions:
                if tr.kind in ("timeout", "message", "tick"):
                    continue
                instants |= timing(tr.text)[0] or set()
                compiled = compile_expression(tr.text)
                series |= (set(env.ctx.series) if "TS" in compiled.computed_refs
                           else set(compiled.refs.get("TS", ())))
                if tr.scan:
                    self._scans.append((pop, tr, float(tr.scan)))
        self._instants = tuple(sorted(instants))
        self._series = tuple(env.ctx.series[name] for name in sorted(series)
                             if name in env.ctx.series)
        self._wake = env.event()
        self.horizon = None
        self.firings = 0
        # Messages (D95): one FIFO of (population, index, name, payload),
        # drained by `_drain` right after the firing or block that sent them.
        self._messages: deque = deque()
        self._busy = False          # inside start()/_settle: a send only queues
        self._cap_t, self._cap_n = None, 0      # firings at the current instant
        self._blocks: dict = {}     # `enter` targets, filled in start()
        # Movement (AB-3): scheduled arrivals on their own absolute-time heap,
        # the `_timeouts` shape -- (time, population, index, epoch), lazy
        # deletion via `pop.move_epoch`. `_tree_cache[pop.index] = (t, tree)`:
        # a one-slot cKDTree cache, rebuilt only when the query instant moves.
        self._moves: list = []
        self._tree_cache: dict = {}
        self._push = heapq.heappush     # list.append while start() arms (AB-4)
        self._tick_pops = [pop for pop in self.populations if pop.tick_dt]     # A1
        self.ticks = 0                  # tick batches run, over every population
        # Grids with populations (A4), once per type: patches live on the type.
        self._grids = list({id(pop.type): pop.type.grid for pop in self.populations
                            if pop.type.grid is not None}.values())
        # Links (A5): pop.index -> _Links, built in start() from each declared
        # network, else on a population's first link action (undirected).
        self._links: dict = {}
        self._networks = [(pop, entry["network"]) for pop, entry in zip(self.populations,
                                                                         populations)
                          if isinstance(entry.get("network"), dict)]

    # -- expression surface --------------------------------------------------
    def population(self, name) -> _Population:
        pop = self.by_name.get(name) if isinstance(name, str) else None
        if pop is None:
            raise ExpressionError(f"unknown population '{name}'")
        return pop

    def ref(self, name, index) -> AgentRef:
        """AGENT(name, index): a checked reference to a LIVE agent, tagged with
        its slot's generation (C5)."""
        pop = self.population(name)
        if isinstance(index, bool) or not isinstance(index, (int, float, np.integer)) \
                or not 0 <= index < pop.n or index != int(index):     # NaN fails the range
            raise ExpressionError(f"AGENT(\"{name}\", {index!r}): no such agent "
                                  f"(indices 0..{pop.n - 1})")
        i = int(index)
        state, gen = self._tick_start(pop, i)
        if state < 0:
            raise ExpressionError(f"AGENT(\"{name}\", {i}): agent {i} was removed")
        return AgentRef(pop.name, i, gen)

    @staticmethod
    def _tick_start(pop, i: int) -> tuple:
        """(state, gen) of slot i as reads see it: in a synchronous tick an
        agent alive at its start stays readable though removed since (#152)."""
        front = pop.front
        if front is not None and i < len(front[0]) and front[0][i] >= 0:
            return int(front[0][i]), int(front[4][i])
        return int(pop.state[i]), int(pop.gen[i])

    def _slot(self, ref) -> tuple:
        """(population, index, current) of a stored reference: current is
        False once its agent was removed, even if the slot was reused (C5)."""
        if not (isinstance(ref, tuple) and len(ref) == 2):
            raise ExpressionError(f"{ref!r} is not an agent reference; write AGENT(\"population\","
                                  f" i) or entity.agent")
        pop, index = self.population(ref[0]), ref[1]
        if isinstance(index, bool) or not isinstance(index, (int, float, np.integer)) \
                or not 0 <= index < pop.n or index != int(index):
            raise ExpressionError(f"{ref!r}: no such agent (indices 0..{pop.n - 1})")
        i = int(index)
        gen = getattr(ref, "gen", None)     # a plain pair: liveness only
        state, now = self._tick_start(pop, i)
        if gen is not None and gen != now and now != pop.gen[i]:
            state, now = int(pop.state[i]), int(pop.gen[i])     # a newborn's ref (#152)
        return pop, i, bool(state >= 0 and (gen is None or now == gen))

    def deref(self, ref) -> tuple:
        """A stored reference -> (population, index); ExpressionError when its
        agent was removed -- a stale read has no right value (D98)."""
        pop, i, current = self._slot(ref)
        if not current:
            raise ExpressionError(f"AGENT(\"{pop.name}\", {i}): that agent was removed (the "
                                  f"reference outlived it); test ALIVE(ref) first")
        return pop, i

    def alive(self, ref) -> bool:
        """ALIVE(ref) (C5): whether a stored reference still names its agent."""
        return self._slot(ref)[2]

    @property
    def dropped(self) -> int:
        """Messages dropped over every population and state (D95)."""
        return sum(sum(pop.dropped) for pop in self.populations)

    def count(self, name, state=None, attr=None, value=None) -> int:
        """COUNT(pop[, state][, attr, value]) (PHASE6 A2): *attr*/*value* filter
        by a categorical attribute's category, on top of the existing O(1)
        per-state counter -- both given, or neither."""
        pop = self.population(name)
        front = pop.front           # a synchronous tick reads its start (A1)
        if attr is not None or value is not None:
            if attr is None or value is None:
                raise ExpressionError(f"COUNT(\"{name}\", ...): a category filter needs both "
                                      f"\"attr\" and \"value\"")
            cats = pop.type.attr_cat.get(attr)
            if cats is None:
                raise ExpressionError(f"population '{name}' has no category attribute '{attr}'")
            code = cats.index(value) if value in cats else -1
            states_arr, attrs = pop.view()
            column = attrs[attr]
            if state is not None:
                s = pop.type.index.get(state)
                if s is None:
                    raise ExpressionError(f"population '{name}' has no state '{state}'")
                column = column[pop.type.mask(states_arr, s)]
            elif front is not None:
                column = column[states_arr >= 0]
            elif pop.live != len(column):
                column = column[pop.members()]
            return int((column == code).sum())
        if state is None:
            return pop.live if front is None else front[3]
        s = pop.type.index.get(state)
        if s is None:
            raise ExpressionError(f"population '{name}' has no state '{state}'")
        return pop.counts[s] if front is None else front[2][s]

    def aggregate(self, how: str, name, attr, state=None) -> float:
        """SUM/MEAN(pop, attr[, state]) over the numpy column, optionally only
        the agents in *state* (AB-4); an empty selection's MEAN is 0."""
        pop = self.population(name)
        states, attrs = pop.view()          # a synchronous tick reads its start (A1)
        column = attrs.get(attr)
        if column is None:
            raise ExpressionError(f"population '{name}' has no attribute '{attr}'")
        if state is not None:
            s = pop.type.index.get(state)
            if s is None:
                raise ExpressionError(f"population '{name}' has no state '{state}'")
            column = column[pop.type.mask(states, s)]
        elif pop.front is not None:
            column = column[states >= 0]
        elif pop.live != len(column):       # dead or unused slots (C5)
            column = column[pop.members()]
        if how == "SUM":
            return float(column.sum())
        return float(column.mean()) if len(column) else 0.0

    def set_attr(self, pop: _Population, i: int, name: str, value) -> None:
        """self.<name> = *value* for agent i: a set action's path and agent
        Python's (A7) -- checked, kept in attr_total (C2), re-traced when it
        colours playback (A2). Writes go to the live columns (D99)."""
        if name == "heading" and pop.heading is not None:          # PHASE6 A6
            pop.heading[i] = _norm360(_number(value, "self.heading"))
            return
        if name not in pop.attrs:
            raise ExpressionError(f"agent type '{pop.type.name}' has no attribute '{name}' "
                                  f"(state and id are read-only)")
        new_value = _coerce_attr(value, pop.type.attr_cat.get(name), f"self.{name}")
        if name in pop.attr_total:                    # PHASE6 C2: numeric only
            pop.attr_total[name] += new_value - pop.attrs[name][i]
        pop.attrs[name][i] = new_value
        if name == pop.color_by:            # PHASE6 A2: playback re-colours it
            self._trace_state(pop, i, self.env.now)

    # -- DES/SD coupling (PHASE5 AB-2) -----------------------------------------
    def send(self, message: str, recipients, payload=None) -> None:
        """Queue *message* for every (population, index) in *recipients*. From
        a DES block (SEND) the instant settles right away (D129), so the next
        block sees what the message caused; from inside the process `_settle`
        drains after the sending firing."""
        payload = 0 if payload is None else payload     # an omitted payload reads 0 (#135)
        self._messages.extend((pop, i, message, payload) for pop, i in recipients)
        if len(self._messages) > self.cap:
            raise RuntimeError(f"more than {self.cap} agent messages queued at "
                               f"t={self.env.now:g}: {_RUNAWAY}")
        if not self._busy:
            self.settle()

    def from_block(self, change, *args):
        """Run a birth or death from a DES block (CREATE `population`, DISPOSE
        `remove_agent`, D130) under the busy guard -- as a spawn/remove action
        runs inside `_settle`, so entry/exit actions only queue their sends --
        then settle the instant (#224)."""
        busy, self._busy = self._busy, True
        try:
            result = change(*args)
        finally:
            self._busy = busy
        self.settle()
        return result

    def settle(self) -> None:
        """Settle the instant from a DES block (an ASSIGN to entity.agent.<attr>,
        H1, D129): the next block in the same zero-time chain sees the agent's
        post-transition state, as SEND's synchronous drain does (D95)."""
        if not self._busy:
            self._settle()
        self.trigger()

    def spawn(self, block_name: str, e_type, pop: _Population, i: int) -> None:
        """`enter`: a new entity at *block_name*, `entity.agent` = the agent."""
        block = self._blocks.get(block_name)
        if block is None:
            raise ExpressionError(f"enter: no block named '{block_name}'")
        entity = spawn_entity(self.env.ctx, e_type,
                              {"agent": AgentRef(pop.name, int(i), int(pop.gen[i]))})
        block.emit("entity_created", entity=entity)
        block.self_run(entity)

    # -- life cycle ------------------------------------------------------------
    def start(self, horizon: float | None = None) -> None:
        """Initialise every agent (attributes, entry actions, transitions armed)
        and start the process; messages sent meanwhile wait for its first pass."""
        self.horizon = float(horizon) if horizon and horizon > 0 else None
        self._blocks = {p.name: p for p in self.env.ctx.process_list}
        now = float(self.env.now)
        self._busy = self._starting = True
        for grid in self._grids:        # A4: before any agent init or entry action reads one
            grid.init_patches(self.env.ctx, self.seed)
            if grid.dt:
                grid.k = _first_tick(now, 0.0, grid.dt)
        for pop in self.populations:
            if pop.type.space is not None:
                for i in range(pop.n):
                    self._place(pop, i, now)
        self._build_networks(now)       # A5: after placement (traced), before any init reads one
        # Arming appends; each heap is built once below (AB-4). Its entries are
        # unique tuples, so the pop order is the same as after one push each.
        self._push = list.append
        for pop in self.populations:
            kind = pop.type
            if kind.columns:            # attribute by attribute (see _Type)
                for name, init, vec, column in zip(kind.attr_names, kind.attr_inits,
                                                    kind.attr_vecs, kind.attr_columns):
                    if column is not None:
                        continue        # set from the table at construction already (C3)
                    values = _run_vec(vec, pop, pop.ids, float) if vec and pop.n else None
                    if values is not None:
                        pop.attrs[name][:] = values
                        continue
                    col, cats = pop.attrs[name], kind.attr_cat.get(name)
                    for i in range(pop.n):
                        col[i] = _coerce_attr(init.evaluate(pop.scope(i)), cats, f"self.{name}")
            for i in range(pop.n):
                scope = pop.scope(i)
                if not kind.columns:
                    for name, init, column in zip(kind.attr_names, kind.attr_inits,
                                                   kind.attr_columns):
                        if column is None:
                            pop.attrs[name][i] = _coerce_attr(init.evaluate(scope),
                                                              kind.attr_cat.get(name), f"self.{name}")
                # PHASE6 C2: seed the running sum with agent i's own initial
                # value (from an expression, a vectorised column or a table),
                # before any entry action below can adjust it by a delta.
                for name in pop.attr_total:
                    pop.attr_total[name] += pop.attrs[name][i]
                self._activate(pop, i, kind.path[int(pop.state[i])], now, scope)
        self._push = heapq.heappush
        heapq.heapify(self._timeouts)
        for hazard in self.hazards:
            heapq.heapify(hazard.heap)
        for pop in self._tick_pops:     # A1: the first k with phase + k * dt > now
            pop.tick_k = _first_tick(now, pop.tick_phase, pop.tick_dt)
        self._busy = self._starting = False
        self.env.process(self._run())

    def trigger(self) -> None:
        if not self._wake.triggered:
            self._wake.succeed()

    def reset_stats(self, t: float) -> None:
        for pop in self.populations:
            pop.reset_stats(t)

    # -- the process -----------------------------------------------------------
    def _run(self):
        env = self.env
        while True:
            self._settle()
            t = self._next_time()
            if t == math.inf:
                yield self._wake
            else:
                yield self._timeout_at(t) | self._wake
            if self._wake.triggered:
                self._wake = env.event()

    def _timeout_at(self, t: float):
        return timeout_at(self.env, t)       # exact instant: agent times are CRN

    def _next_time(self) -> float:
        heap, best, pops = self._timeouts, math.inf, self.populations
        while heap:
            _t, p, i, k, epoch = heap[0]
            pop = pops[p]
            if pop.epochs[pop.type.transitions[k].depth][i] == epoch:   # K1: per depth
                break
            heapq.heappop(heap)
        if heap:
            best = heap[0][0]
        for hazard in self.hazards:
            top = hazard.top()
            if top is not None:
                best = min(best, hazard.time_of(top[0]))
        now = self.env.now
        j = bisect_right(self._instants, now)
        if j < len(self._instants):
            best = min(best, self._instants[j])
        for series in self._series:
            change = series.next_change(now)
            if change is not None:
                best = min(best, change)
        for pop, tr, scan in self._scans:
            if pop.counts[tr.src]:
                k = math.floor(now / scan) + 1
                best = min(best, k * scan if k * scan > now else (k + 1) * scan)
        moves = self._moves
        while moves and self.populations[moves[0][1]].move_epoch[moves[0][2]] != moves[0][3]:
            heapq.heappop(moves)
        if moves:
            best = min(best, moves[0][0])
        for pop in self._tick_pops:     # A1: from the integer k, never accumulated
            best = min(best, pop.tick_phase + pop.tick_k * pop.tick_dt)
        for grid in self._grids:        # A4: patch ticks, the same integer-k rule
            if grid.dt:
                best = min(best, grid.k * grid.dt)
        # Keep a wake-up at the horizon (the LevelSystem rule): the statistics
        # cover the whole run even when every agent is absorbed early.
        if self.horizon is not None and self.env.now < self.horizon:
            best = min(best, self.horizon)
        return best

    def _settle(self) -> None:
        """Fire everything due now: timeouts and rates, then (once nothing
        else is due) the shared rates are re-evaluated and the conditions
        checked, until the instant is quiet. The messages a firing sends are
        delivered before the next firing (D95). Once the instant is quiet, the
        ticks due now run and the instant settles again (A1, D99)."""
        now = self.env.now
        self._busy = True
        start, ticks = self.firings, self.ticks
        try:
            self._drain(now)        # sent at start() or before this pass
            while True:
                batch = self._due(now)
                arrivals = self._due_moves(now)
                conditions = not batch and not arrivals
                if conditions:
                    self._refresh(now)
                    batch = self._due_conditions()
                    if not batch:
                        if self._run_ticks(now):
                            continue
                        break
                for pop, i in arrivals:
                    self._arrive(pop, i, now)
                self._drain(now)    # "arrived" messages go before the tied firings (#144)
                before = self.firings
                if conditions and self._nested:
                    self._fire_innermost(batch, now)
                else:
                    batch.sort()    # ties in (population, index, transition) order
                    for p, i, k, epoch in batch:
                        self._fire(self.populations[p], i, k, epoch, now)
                        self._drain(now)
                if conditions and self.firings == before:
                    if self._run_ticks(now):
                        continue
                    break           # every guard false: re-checking is idempotent (#139)
        finally:
            self._busy = False
        # An SD rate may read COUNT/SUM/MEAN of the agents (#140): re-evaluate
        # at the agent event (or tick) itself, grid or no grid.
        levels = self.env.ctx.level_system
        if (self.firings != start or self.ticks != ticks) and levels is not None:
            levels.trigger()

    def _fire_innermost(self, batch: list, now: float) -> None:
        """A condition batch with composite states (K1, D138): per agent the
        innermost source wins (`rank`), and once one of its transitions is
        taken the rest wait for the next pass -- what a flat agent's epoch
        already does (rank == k there, so flat populations fire as before)."""
        pops, taken = self.populations, set()
        batch.sort(key=lambda e: (e[0], e[1], pops[e[0]].type.rank[e[2]]))
        for p, i, k, epoch in batch:
            if (p, i) in taken:
                continue
            fired = self.firings
            self._fire(pops[p], i, k, epoch, now)
            if self.firings != fired:
                taken.add((p, i))
            self._drain(now)

    # -- ticks (PHASE6 A1, D99) -------------------------------------------------
    def _run_ticks(self, now: float) -> bool:
        """Run every population's tick due at *now*, in population order;
        whether any ran (the caller then settles the instant again)."""
        ran = False
        for pop in self._tick_pops:
            k = pop.tick_k
            if pop.tick_phase + k * pop.tick_dt <= now:
                pop.tick_k = k + 1
                self.ticks += 1
                ran = True
                if pop.type.ticking and pop.live:
                    self._tick(pop, k, now)
        for grid in self._grids:        # A4: after the population ticks of the instant
            if grid.dt and grid.k * grid.dt <= now:
                grid.k += 1
                grid.tick()
                ran = True
        return ran

    def _tick(self, pop: _Population, k: int, now: float) -> None:
        """Tick k of *pop*: every agent alive at its start acts once, in the
        activation's order; one removed meanwhile is skipped (its generation
        moved, C5), a newborn waits for the next tick."""
        kind = pop.type
        if pop.attr_total:
            pop.accrue(now)         # C2: flush with the values in force before any set
        idx = pop.members()
        gens = pop.gen[idx]
        if pop.tick_mode == "random":
            rng = np.random.default_rng(stream_seed(self.seed, pop.name, k, _TICK_TAG))
            order = rng.permutation(len(idx))
            idx, gens = idx[order], gens[order]
        agents = list(zip(idx.tolist(), gens.tolist()))
        if pop.tick_mode != "synchronous":
            for i, gen in agents:
                if pop.gen[i] == gen:
                    self._tick_agent(pop, i, now, True, True)
            return
        pop.front = (pop.state.copy(), {name: col.copy() for name, col in pop.attrs.items()},
                     list(pop.counts), pop.live, pop.gen.copy())
        pop.lets = {} if kind.lets else None        # A9: shared by the two passes
        grid = kind.grid                # A4: patches are read at the tick start too
        if grid is not None:
            grid.front = {name: values.copy() for name, values in grid.patches.items()}
        try:
            if kind.on_tick and not self._tick_columns(pop, idx):
                for i, gen in agents:
                    if pop.gen[i] == gen:
                        self._tick_agent(pop, i, now, True, False)
            if any(kind.tick_out):
                for i, gen in agents:
                    if pop.gen[i] == gen:
                        self._tick_agent(pop, i, now, False, True)
        finally:
            pop.front = pop.lets = None     # the commit: writes went to the live columns
            if grid is not None:
                grid.front = None

    def _tick_agent(self, pop: _Population, i: int, now: float, actions: bool,
                    transitions: bool) -> None:
        """Agent i's on_tick actions, then its first "tick" transition whose
        condition and guard hold; the messages it sent are delivered after."""
        scope = pop.scope(i)
        if pop.type.lets:
            scope.locals = self._lets(pop, i, scope)
        if not actions or self._act(pop.type.on_tick, pop, i, scope):
            if transitions:
                for tr in pop.type.tick_out[pop.state[i]]:
                    if (tr.value is None or tr.value(scope)) and \
                            (tr.guard is None or tr.guard(scope)):
                        self._take(pop, i, tr, scope, now)
                        break
        self._drain(now)

    def _lets(self, pop: _Population, i: int, scope) -> dict:
        """Agent i's `let` values for this tick (PHASE6 A9): evaluated once, in
        order, in its own scope (a draw uses its own stream, once); kept for
        the synchronous tick's transition pass in `pop.lets`, else fresh."""
        values = pop.lets.get(i) if pop.lets is not None else None
        if values is None:
            values = scope.locals = dict(pop.type.params)
            for name, fn in pop.type.lets:
                values[name] = fn(scope)
            if pop.lets is not None:
                pop.lets[i] = values
        return values

    def _tick_columns(self, pop: _Population, idx) -> bool:
        """A synchronous on_tick of vectorisable self sets, column-wise: every
        value is computed from the snapshot first, then written. False (the
        loop decides, AB-4) if the type does not qualify or numpy refuses."""
        for vec in pop.type.let_vecs:
            if _run_vec(vec, pop, idx, float) is None:
                return False
        values = []
        for name, vec in pop.type.tick_vecs:
            value = _run_vec(vec, pop, idx, float)
            if value is None:
                return False
            values.append((name, value))
        for name, value in values:
            column = pop.attrs[name]
            pop.attr_total[name] += float((value - column[idx]).sum())
            column[idx] = value
        return bool(values)

    def _drain(self, now: float) -> None:
        """Deliver the queued messages, FIFO; what a delivery sends queues
        behind the rest. A message fires the first matching "message"
        transition out of the recipient's current state (K1: its leaf, then
        each enclosing composite) whose guard holds; otherwise it is dropped
        and counted against that (leaf) state."""
        queue = self._messages
        while queue:
            pop, i, name, payload = queue.popleft()
            s = int(pop.state[i])
            if s < 0:           # removed since it was sent (#152: AGENT reads the tick start)
                continue
            scope = None
            for tr in pop.type.live_out[s]:
                if tr.kind != "message" or tr.value not in ("", name):
                    continue
                if scope is None:
                    scope = pop.scope(i, {**pop.type.params, "payload": payload})
                if tr.guard is None or tr.guard(scope):
                    self._take(pop, i, tr, scope, now)
                    break
            else:
                pop.dropped[s] += 1

    def _due(self, now: float) -> list:
        batch = []
        heap = self._timeouts
        while heap and heap[0][0] <= now:
            _t, p, i, k, epoch = heapq.heappop(heap)
            batch.append((p, i, k, epoch))
        for hazard in self.hazards:
            while True:
                top = hazard.top()
                if top is None or hazard.time_of(top[0]) > now:
                    break
                heapq.heappop(hazard.heap)
                batch.append((hazard.pop.index, top[1], hazard.tr.k, top[2]))
        return batch

    def _due_moves(self, now: float) -> list:
        """Arrivals due now (AB-3), lazy-deleted like `_due`'s other heaps."""
        batch, heap = [], self._moves
        while heap and heap[0][0] <= now:
            _t, p, i, epoch = heapq.heappop(heap)
            pop = self.populations[p]
            if pop.move_epoch[i] == epoch:      # else: overtaken by a later move_to
                batch.append((pop, i))
        return batch

    def _arrive(self, pop: _Population, i: int, now: float) -> None:
        """A `move_to` reaches its target: the segment collapses to a point at
        *now* and an "arrived" message is queued (D95's own delivery, no new
        trigger kind -- the agent's current state catches it like any other
        message)."""
        pop.x0[i], pop.y0[i] = pop.x1[i], pop.y1[i]
        pop.t0[i] = pop.t1[i] = now
        path = pop.path.pop(i, None)
        if pop.node is not None and path:
            pop.node[i] = path[-1]
        self.send("arrived", [(pop, i)])

    def _refresh(self, now: float) -> None:
        """Re-evaluate every shared rate; Lambda is re-anchored only when one
        changed, so an unrelated event leaves every key's firing time alone."""
        for hazard in self.hazards:
            shared = hazard.tr.shared
            if not shared or not hazard.pop.counts[hazard.tr.src]:
                continue        # nobody waits: the integral so far is nobody's
            lam = float(shared(hazard.pop.shared_scope))
            if not lam >= 0 or lam == math.inf:
                raise ExpressionError(f"population '{hazard.pop.name}': rate of transition "
                                      f"{hazard.tr.k} evaluated to {lam}")
            if lam != hazard.lam:
                hazard.big, hazard.t0, hazard.lam = hazard.integral(now), now, lam

    def _due_conditions(self) -> list:
        batch = []
        for pop, tr in self.conditions:
            if not pop.counts[tr.src]:
                continue
            members = np.flatnonzero(pop.state == tr.src if tr.leaves is None
                                     else np.isin(pop.state, tr.leaves))    # K1
            if not tr.own:
                if not tr.value(pop.shared_scope):
                    continue
            else:
                hit = _run_vec(tr.vec, pop, members, bool) if tr.vec else None
                if hit is not None:
                    members = members[hit]
                else:
                    # ponytail: one evaluation per agent in the state for an own
                    # condition outside the numpy subset (and/or/if/not/in, a draw).
                    members = np.array([i for i in members if tr.value(pop.scope(int(i)))],
                                       dtype=np.intp)
            batch += [(pop.index, i, tr.k, e)
                      for i, e in zip(members.tolist(), pop.epochs[tr.depth][members].tolist())]
        return batch

    def _fire(self, pop: _Population, i: int, k: int, epoch: int, now: float) -> None:
        tr = pop.type.transitions[k]
        if pop.epochs[tr.depth][i] != epoch:
            return                  # moved earlier in this batch
        scope = pop.scope(i)
        if tr.guard is not None and not tr.guard(scope):
            if tr.kind == "rate":   # memoryless: a fresh draw from now
                self._arm_rate(pop, i, tr, now, scope)
            return
        self._take(pop, i, tr, scope, now)

    def _take(self, pop: _Population, i: int, tr: _Transition, scope, now: float) -> None:
        """Agent i takes *tr*: exit actions, the transition's, the move, entry
        actions, then the new state's transitions are armed."""
        if pop.attr_total:
            # PHASE6 C2: flush attr_area up to *now* (using the values still in
            # force) before any action below can change them -- exit/transition
            # actions run before `pop.move`'s own accrue would otherwise catch
            # this. A no-op (dt=0) for a type with no attributes.
            pop.accrue(now)
        if now != self._cap_t:
            self._cap_t, self._cap_n = now, 0
        self._cap_n += 1
        if self._cap_n > self.cap:
            raise RuntimeError(f"more than {self.cap} agent transitions at t={now:g}: {_RUNAWAY}")
        kind = pop.type
        # An action that removes the agent ends its firing there (C5).
        if kind.nested:
            self._take_nested(pop, i, tr, scope, now)
        elif self._act(kind.exit[tr.src], pop, i, scope) and self._act(tr.actions, pop, i, scope):
            pop.move(i, tr.dst, now)
            self._activate(pop, i, kind.path[tr.dst], now, scope)
        self.firings += 1

    def _take_nested(self, pop: _Population, i: int, tr: _Transition, scope, now: float) -> None:
        """K1 (D137): exit actions from the active leaf up to (excluding) the
        LCA, innermost first, each exited composite's history recorded; the
        transition's actions; then entry down to a leaf through `initial`s or
        the recorded history."""
        kind = pop.type
        path = kind.path[int(pop.state[i])]
        exited = path[tr.keep:][::-1]
        for x in exited:
            if not self._act(kind.exit[x], pop, i, scope):
                return
            rows = pop.hist.get(x)
            if rows is not None:
                rows[0, i], rows[1, i] = path[kind.depth[x] + 1], path[-1]
        if not self._act(tr.actions, pop, i, scope):
            return
        dst = tr.dst
        rows = pop.hist.get(dst) if tr.history else None
        leaf = kind.leaf[dst]
        if rows is not None and rows[0, i] >= 0:
            leaf = int(rows[1, i]) if tr.history == "deep" else kind.leaf[int(rows[0, i])]
        entered = tr.enter + kind.path[leaf][kind.depth[dst] + 1:]
        pop.shift(i, exited, entered, now)
        self._activate(pop, i, entered, now, scope)

    def _activate(self, pop: _Population, i: int, states: tuple, now: float, scope) -> None:
        """Agent i has entered *states* (outermost first; one for a flat
        type): per state its entry actions, then its transitions armed (the
        draw order, K1); once all are entered, the leaf is traced. An entry
        action that removes the agent stops there."""
        for s in states:
            if not self._act(pop.type.entry[s], pop, i, scope):
                return
            self._arm(pop, i, s, now, scope)
        self._trace_state(pop, i, now)

    def _act(self, actions: list, pop: _Population, i: int, scope) -> bool:
        """Run *actions* for agent i; False as soon as one removed it (its
        generation moved, C5), skipping the rest."""
        if actions:
            gen = pop.gen[i]
            for run in actions:
                run(self, pop, i, scope)
                if pop.gen[i] != gen:
                    return False
        return True

    # -- births and deaths (PHASE6 C5, D98) ------------------------------------
    def add_agent(self, pop: _Population, state=None, inits=(), parent=None) -> int:
        """A birth: the lowest free slot (else a new one, the columns doubling
        up to max_count), keyed to the stream `count + birth serial`; placed,
        its attribute inits evaluated in its own scope, then each `(attr,
        fn)` of *inits* in the spawner's scope *parent*, then entry actions
        and arming -- an initial agent's order. Returns the slot."""
        if self._starting:
            raise ExpressionError(f"spawn \"{pop.name}\": births start with the run, not in an "
                                  f"entry action run while the populations initialise")
        if pop.live >= pop.max_count:
            raise ExpressionError(f"E_POPULATION_FULL: spawn \"{pop.name}\": the population is at "
                                  f"its max_count ({pop.max_count}); raise \"max_count\"")
        now = float(self.env.now)
        if pop.free:
            i = heapq.heappop(pop.free)
        else:
            i = pop.n
            if i == len(pop.state):
                pop.grow()
            pop.n += 1
        pop._keys[i] = pop.initial_count + pop.births
        pop.births += 1
        kind = pop.type
        s = kind.initial if state is None else kind.leaf[kind.index[state]]
        pop.born(i, s, now)
        self._tree_cache.pop(pop.index, None)
        scope = pop.scope(i)
        if kind.space is not None:
            self._place(pop, i, now)
        for name, init in zip(kind.attr_names, kind.attr_inits):
            # A data-column attribute (C3, init is None) has no table row of
            # its own to be born into: 0, same as any other attribute's
            # default, unless the spawn's own `inits` overrides it below.
            pop.attrs[name][i] = _coerce_attr(init.evaluate(scope), kind.attr_cat.get(name),
                                              f"self.{name}") if init is not None else 0.0
        for name, value in inits:
            pop.attrs[name][i] = _coerce_attr(value(parent), kind.attr_cat.get(name),
                                              f"spawn \"{pop.name}\": init {name}")
        for name in pop.attr_total:                  # PHASE6 C2: the newborn's own value
            pop.attr_total[name] += pop.attrs[name][i]
        self._link_newborn(pop, i, parent)          # A5: to its parent, if link_on_spawn
        # ponytail: a newborn whose entry action spawns again recurses; max_count
        # (or Python's recursion limit) stops a runaway -- queue births if it binds.
        self._activate(pop, i, kind.path[s], now, scope)
        return i

    def remove_agent(self, pop: _Population, i: int) -> None:
        """A death: agent i leaves its population (statistics as an exit), its
        slot goes stale everywhere (epochs, generation) and back on the free
        list, and the messages queued to it are dropped against its state."""
        if self._starting:
            raise ExpressionError(f"remove: agent {i} of \"{pop.name}\": deaths start with the "
                                  f"run, not in an entry action run while the populations "
                                  f"initialise")
        now = float(self.env.now)
        s = pop.died(i, now)
        if pop.type.space is not None:
            pop.move_epoch[i] += 1              # a pending arrival goes stale
            pop.path.pop(i, None)
            if pop.node is not None:
                pop.node[i] = ""
        self._tree_cache.pop(pop.index, None)
        if pop.index in self._links:            # A5: its links go with it
            self._links[pop.index].drop(pop, i)
        queue = self._messages
        if queue:
            # ponytail: O(queue) per removal; the queue holds one firing's sends.
            kept = [m for m in queue if m[0] is not pop or m[1] != i]
            pop.dropped[s] += len(queue) - len(kept)
            queue.clear()
            queue.extend(kept)
        bus = self.env.ctx.bus
        if i < self.trace_agents and bus.has_subscribers:
            bus.emit("agent_removed", block=None, t=now, pop=pop.name, index=i,
                     e_type=pop.type.name)

    def _arm(self, pop: _Population, i: int, s: int, now: float, scope) -> None:
        """Arm state *s*'s transitions for agent i, in declaration order (the
        order its stream is drawn in)."""
        for tr in pop.type.out[s]:
            if tr.kind == "timeout":
                delay = _number(tr.value(scope), f"population '{pop.name}': timeout of "
                                                 f"transition {tr.k}")
                if delay != delay:
                    raise ExpressionError(f"population '{pop.name}': timeout of transition "
                                          f"{tr.k} evaluated to NaN")
                self._push(self._timeouts, (now + max(delay, 0.0), pop.index, i, tr.k,
                                            int(pop.epochs[tr.depth][i])))
            elif tr.kind == "rate":
                self._arm_rate(pop, i, tr, now, scope)

    def _arm_rate(self, pop: _Population, i: int, tr: _Transition, now: float, scope) -> None:
        draw = pop.rng(i).standard_exponential()
        weight = scaled(float(tr.own(scope)) if tr.own else 1.0, tr.rate_factor)
        if weight > 0:
            hazard = self._hazard_of[(pop.index, tr.k)]
            self._push(hazard.heap, (hazard.integral(now) + draw / weight, i,
                                     int(pop.epochs[tr.depth][i])))

    # -- trace (PHASE5 AB-3, D75-style budget) ---------------------------------
    def _trace_state(self, pop: _Population, i: int, now: float) -> None:
        """Called on every firing AND, PHASE6 A2, every set of the
        population's own `color_by` attribute -- the token's colour can
        change with no state transition at all."""
        bus = self.env.ctx.bus
        if i >= self.trace_agents or not bus.has_subscribers:
            return
        color = pop.read(pop.color_by, i) if pop.color_by else None
        bus.emit("agent_state", block=None, t=now, pop=pop.name, index=i,
                 e_type=pop.type.name, state=pop.type.states[int(pop.state[i])], color=color)

    def _trace_moved(self, pop: _Population, i: int, now: float, x0: float, y0: float,
                     x1: float, y1: float, duration: float, path: tuple) -> None:
        bus = self.env.ctx.bus
        if i >= self.trace_agents or not bus.has_subscribers:
            return
        bus.emit("agent_moved", block=None, t=now, pop=pop.name, index=i,
                 x0=x0, y0=y0, x1=x1, y1=y1, duration=duration, path=path)


def _first_tick(now: float, phase: float, dt: float) -> int:
    """The first integer k with phase + k * dt strictly after *now* (A1)."""
    k = max(0, math.floor((now - phase) / dt) + 1)
    while phase + k * dt <= now:
        k += 1
    while k > 0 and phase + (k - 1) * dt > now:
        k -= 1
    return k


def _coerce_attr(value, categories, what: str) -> float:
    """A numeric attribute's `_number` check; a categorical one's (PHASE6 A2)
    is that *value* is one of its declared *categories* -- an unknown literal
    or an arithmetic result (never a string) both raise here, whether from an
    init, a set action or a spawn's own init."""
    if categories is None:
        return _number(value, what)
    if not isinstance(value, str) or value not in categories:
        raise ExpressionError(f"{what} = {value!r}, not one of the declared categories "
                              f"({', '.join(categories)})")
    return float(categories.index(value))


def build_agent_system(env, layout: dict) -> AgentSystem | None:
    """The run's AgentSystem, or None for a model without populations (so a
    model without agents pays one `is None` test per use). Trace cap (AB-3,
    D75): `simulation.agents.trace_agents`, default 200."""
    populations = layout.get("populations") or []
    if not populations:
        return None
    trace_agents = ((layout.get("simulation") or {}).get("agents") or {}).get("trace_agents", 200)
    tables = {t.get("name"): t for t in layout.get("tables") or [] if isinstance(t, dict)}
    return AgentSystem(env, layout.get("agent_types") or [], populations, tables,
                       layout.get("network"), trace_agents=trace_agents)
