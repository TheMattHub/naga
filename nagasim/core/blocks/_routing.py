"""A routing rule placed in a run (conveyor system A7, D1359).

A CONVEYOR divert group, a DECIDE in mode "Rule" and a CONVEYOR_TRANSFER each own one
`RoutePoint`: it asks its `RouteRule` (core/routing.py) for an exit, counts the load under way
to that exit's target piece until the load reaches it, leaves its way there by a divert (R5,
R26, `Conveyor.watch`), or is disposed or batched (R34, `end_routes`), and wakes a held load
when a count drops. Every step is an event (D675): a hold waits on `freed()`.
"""
from nagasim.core.arealink import real_target
from nagasim.core.blocking import _is_belt, _is_conveyor
from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.errors import ModelRunError
from nagasim.core.routing import RouteRule, rule_spec, split_exit
from nagasim.core.stats import TimeWeighted

__all__ = ["RoutePoint", "rule_of", "end_routes"]


def _code(block) -> str:
    """The problem code a routing fault of *block* is reported under, as validation does."""
    kind = getattr(getattr(block, "block", None), "block_type", "")
    return "E_DECIDE_RULE" if kind == "DECIDE" else "E_CONVEYOR_DIVERT"


def rule_of(block, rule, default_exits: list, block_kind: str = "") -> RouteRule:
    """The RouteRule of *rule* (`rule_spec`: its exits default to *default_exits*); a bad rule
    stops the run with its block and code named (validation reports it first)."""
    try:
        spec = rule_spec(rule, default_exits, block_kind)
        if not isinstance(spec, dict):
            raise ValueError("a routing rule must be an object")
        return RouteRule.from_dict(spec)
    except ValueError as exc:
        raise ModelRunError(f"Block '{block.name}': routing rule: {exc} ({_code(block)}).") \
            from None


def end_routes(ctx, entity) -> None:
    """R34: *entity* is disposed or batched, so it never reaches a target piece: every rule
    counting it under way lets it go. Free for a model with no counted load."""
    ends = ctx.route_ends
    if ends:
        for done in ends.pop(entity.id, ()):
            done(entity)


class RoutePoint:
    """One rule over exits that leave *block* by its outgoing links *links* (exit i by link
    links[i]). Also the ctx `RouteRule.choose` reads: `eval_condition`, `item_runtime`."""

    def __init__(self, block, rule: RouteRule, links: list):
        self.block, self.rule, self.links = block, rule, list(links)
        self._chains = None             # per exit, its belts, target piece last; built on use
        self._free = None               # fires when an exit's count drops
        self._pending: dict = {}        # entity id -> (its callback, the belts watched)
        # A12 (R35): the hold state `held` keeps, time-weighted: loads held now, and 1 while
        # any is; `holds` counts the holds begun since the warm-up
        now = block.env.now
        self.holding = TimeWeighted(now)
        self.any_held = TimeWeighted(now)
        self.holds = 0
        block.ctx.route_points.append(self)

    def reset_stats(self, t: float) -> None:
        """Warm-up hook (`sim_runner.reset_statistics`): open holds keep their level."""
        self.holding.reset(t)
        self.any_held.reset(t)
        self.holds = 0

    # -- the ctx protocol of RouteRule.choose ------------------------------------
    def eval_condition(self, expr, entity) -> bool:
        return bool(evaluate_expression(self.block, expr, entity))

    @property
    def item_runtime(self):
        item = getattr(self.block, "item", None)
        runtime = self.block.ctx.items.get(item) if item is not None else None
        if runtime is None:
            raise ModelRunError(f"Block '{self.block.name}': an \"item\" routing rule needs the "
                                f"block inside an item with code ({_code(self.block)}).")
        return runtime

    # -- choosing and counting -----------------------------------------------------
    def choose(self, entity):
        try:
            return self.rule.choose(entity, self)
        except ValueError as exc:       # R27: no true condition and no ELSE, a bad attribute
            raise ModelRunError(f"Block '{self.block.name}': routing rule: {exc} "
                                f"({_code(self.block)}).") from None

    def routed(self, entity, index: int) -> None:
        """Count *entity* against exit *index* until it reaches that exit's target piece, leaves
        a belt on its way there by a divert (R5, R26), or is disposed or batched (R34). An exit
        with no target piece (its link leads to no CONVEYOR) counts nothing: no event would ever
        end the count; nor does a rule nothing reads the counts of (no limit, not least_loaded),
        so it puts no watch on any belt."""
        if self._forget(entity) is not None:    # routed again (a loop): the newer route counts
            self.rule.arrived(entity)
            self.wake()
        if not self.rule.counts():
            return
        chain = self.chain(index)
        if not chain:
            return
        self.rule.routed(entity, index)

        def done(_entity):
            self._arrived(entity, done)
        self._pending[entity.id] = (done, chain)
        self.block.ctx.route_ends.setdefault(entity.id, []).append(done)
        chain[-1].watch(entity, done)
        for belt in chain[:-1]:
            belt.watch(entity, done, off_only=True)

    def _forget(self, entity):
        pending = self._pending.pop(entity.id, None)
        if pending is not None:
            done, chain = pending
            for belt in chain:
                belt.unwatch(entity, done)
            ends = self.block.ctx.route_ends.get(entity.id)
            if ends and done in ends:
                ends.remove(done)
                if not ends:
                    del self.block.ctx.route_ends[entity.id]
        return pending

    def _arrived(self, entity, done) -> None:
        pending = self._pending.get(entity.id)
        if pending is None or pending[0] is not done:
            return
        self._forget(entity)
        self.rule.arrived(entity)
        self.wake()

    def freed(self):
        """The event a held load waits on: an exit's count dropped (or `wake`)."""
        if self._free is None or self._free.triggered:
            self._free = self.block.env.event()
        return self._free

    def wake(self) -> None:
        if self._free is not None and not self._free.triggered:
            self._free.succeed()

    def held(self, change: int, since: float | None = None) -> float:
        """A load starts (+1) or ends (-1, *since*: when it started) a hold here: what the
        run-end checks read (`blocking.deadlock_at_end`, `blocking.route_holds_at_end`), each
        open hold by the time it began. Returns that time."""
        holds = self.block.ctx.route_holds.setdefault(self.block.name, [])
        now = self.block.env.now
        if change > 0:
            since = now
            holds.append(since)
            self.holds += 1
        elif since in holds:
            holds.remove(since)
        else:
            return since
        self.holding.add(change, now)
        self.any_held.set(1.0 if self.holding.level > 0 else 0.0, now)
        return since

    # -- target pieces (R5) --------------------------------------------------------
    def chain(self, index: int) -> list:
        """Exit *index*'s belts, its target piece last: from the exit's link, each belt's end
        to the next CONVEYOR, through a CONVEYOR_TRANSFER with one exit (R36), never back to
        this block nor, for a DECIDE, to a belt that hands it loads (M2, R38); ending at the
        last CONVEYOR before any other block (a transfer with several exits too), or at the
        CONVEYOR `name:target` names, which must lie downstream of the exit (R34). [] when
        there is none."""
        if self._chains is None:
            self._chains = [self._chain_of(i) for i in range(len(self.links))]
        return self._chains[index]

    def target(self, index: int):
        chain = self.chain(index)
        return chain[-1] if chain else None

    def _kept_out(self) -> set:
        """Names the walk never enters: this block, and for a DECIDE the belts that feed it
        (a recirculating exit would otherwise end its count as the load leaves that belt)."""
        names = {self.block.name}
        if not _is_belt(self.block):
            names |= {name for name, belt in self.block.ctx.conveyors.items()
                      if any(getattr(real_target(p), "name", None) == self.block.name
                             for p in belt.next_process or [])}
        return names

    def _first_piece(self, index: int):
        nexts = self.block.next_process or []
        link = self.links[index]
        return nexts[link] if link < len(nexts) else None

    def _chain_of(self, index: int) -> list:
        spec = self.rule.exits[index]
        name = split_exit(spec)[1]
        piece = self._first_piece(index)
        walked, seen = [], self._kept_out()
        while _is_belt(piece) and piece.name not in seen:
            seen.add(piece.name)
            nexts = piece.next_process or []
            if _is_conveyor(piece):
                walked.append(piece)
                if piece.name == name:
                    break
            elif len(nexts) != 1:
                break                   # a transfer with several exits ends the branch (R36)
            piece = nexts[0] if nexts else None
        if name:
            if walked and walked[-1].name == name:
                return walked
            belt = self.block.ctx.conveyors.get(name)
            if belt is None:
                raise ModelRunError(f"Block '{self.block.name}': exit '{spec}' names target "
                                    f"piece '{name}', which is not a CONVEYOR "
                                    f"({_code(self.block)}).")
            if not self._reaches(self._first_piece(index), belt):
                raise ModelRunError(f"Block '{self.block.name}': exit '{spec}' names target "
                                    f"piece '{name}', which no load of that exit reaches "
                                    f"({_code(self.block)}).")
            return [belt]
        return walked

    def _reaches(self, start, belt) -> bool:
        """Some way of links leads from *start* to *belt*, never through this block."""
        todo, seen = [start], {self.block.name}
        while todo:
            piece = todo.pop()
            name = getattr(piece, "name", None)
            if piece is None or name in seen:
                continue
            if name == belt.name:
                return True
            seen.add(name)
            todo.extend(piece.next_process or [])
        return False
