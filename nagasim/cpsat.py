"""CP-SAT proposer (PHASE6 W4 P4, ARCHITECTURE 5.32, D122).

`propose(layout)` turns the order book into a job shop, lets OR-Tools CP-SAT
sequence it, and hands the answer back as an ordinary scenario: every order
gains `plan` ({station: planned start}) and, where the plan starts it later, a
later `release`; every resource the plan uses gets the "Plan" queue
discipline (`core/resource_manager.py`), which works each station's planned
order. The simulation then executes the plan -- deterministically to check
it, stochastically (`planning.plan_risk`) to score it.

What the solver sees is read off the BUILT engine model in deterministic mode
(D119), so an operation's duration is exactly the mean the engine would use,
expression params evaluated against the order's own attributes and step
values, set where ROUTE sets them. Per sequence step the path runs from the
STATION along first links to the next ROUTE: a PROCESS "Seize Delay Release"
is an operation; a DELAY, a PROCESS "Delay", a link delay or a ROUTE transfer
is a lag. Anything else on that path is REFUSED -- listed in
`Proposal.refused`, left out of the solver model, still executed by the
simulation. Flow after an order's last ROUTE is not planned.

Machines are resources: capacity c is a cumulative constraint, a resource set
is one optional interval per member (exactly one present). A PROCESS setup
(D52) is modelled exactly on a single-unit resource only that block uses: a
circuit over its jobs, the changeover evaluated by the block's own
expressions; elsewhere it is refused. Weights (weighted tardiness) are order
`priority`, higher = more important: absent means 1, negative counts as 0.

Stdlib only here; `ortools` is an optional dependency imported inside
`propose` alone (D122).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param
from nagasim.core.resource_manager import ResourceSet
from nagasim.core.sim_classes import spawn_entity
from nagasim.core.sim_runner import initialize_sim_env, prepare_model
from nagasim.core.timeunits import units_of

__all__ = ["OBJECTIVES", "Proposal", "propose"]

OBJECTIVES = ("makespan", "weighted_tardiness")


@dataclass
class Proposal:
    """`layout` is the model with `overrides` applied; `objective` and
    `makespan` are in the model's base time unit (weighted tardiness: time x
    weight); `status` is CP-SAT's ("OPTIMAL", or "FEASIBLE" when the time
    limit stopped the search first)."""

    layout: dict
    overrides: list
    objective: float
    makespan: float
    status: str
    refused: list = field(default_factory=list)


@dataclass(eq=False)
class _Op:
    block: object
    station: str | None
    duration: float
    lag: float            # between the previous operation's end (or the release) and this start
    members: list         # the resource, or a set's members
    quantity: int
    key: object           # setup key (D52)
    probe: object         # the entity as it stands here, for the setup expression


@dataclass(eq=False)
class _Job:
    order: object
    release: float
    ops: list
    tail: float = 0.0     # lag after the last operation, up to the last ROUTE


def propose(layout, objective: str = "makespan", time_limit_s: float = 10, seed: int = 0) -> Proposal:
    """Propose a plan for *layout*'s order book (a dict or a model path)."""
    try:
        import ortools  # noqa: F401 -- explicit, so a blocked top-level package fails here
        from ortools.sat.python import cp_model
    except ImportError as exc:
        raise ImportError("cpsat.propose needs the optional package 'ortools': "
                          "pip install \"nagasim[plan]\" (or: pip install ortools)") from exc
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {OBJECTIVES}, got {objective!r}")
    # NOT api.load_layout: raw stays in the CALLER's own schema version, since
    # Proposal.layout must round-trip through experiments.apply_overrides back
    # to a layout equal to what the caller passed in (test_cpsat.py).
    raw = layout if isinstance(layout, dict) else json.loads(Path(layout).read_text(encoding="utf-8"))

    from nagasim.plugins import load_plugins
    load_plugins()
    built = copy.deepcopy(raw)
    built["simulation"] = {**(built.get("simulation") or {}), "deterministic": True}
    env = initialize_sim_env(seed)
    model = prepare_model(env, built)
    refused: set[str] = set()
    jobs = _jobs(env.ctx, model, refused)
    solved = _solve(cp_model, jobs, objective, time_limit_s, seed, refused)
    return _proposal(raw, jobs, solved, refused)


# ------------------------------------------------------------ reading the model
def _jobs(ctx, model, refused) -> list[_Job]:
    """One job per entity the order book releases, each op read off the model."""
    jobs = []
    for order in ctx.orders.values():
        create = model.blocks.get(order.block)
        steps = ctx.sequences.get(order.sequence) if order.sequence else None
        if create is None or not steps:
            refused.add(f"order '{order.id}': no sequence or no releasing CREATE (not planned)")
            continue
        for _ in range(order.quantity):
            probe = _probe(ctx, create, order)
            lag = _link_lag(create, probe)
            ops, lag, route = (_path(ctx, create.next_process[0], probe, lag, refused)
                               if create.next_process else ([], lag, None))
            for index, step in enumerate(steps):
                if route is None:
                    refused.add(f"order '{order.id}': its path leaves the routing before step "
                                f"{index} (later steps not planned)")
                    break
                for key, value in step["values"].items():
                    setattr(probe, key, value)
                probe.step = index + 1
                lag += _transfer(route, probe, step["station"])
                more, lag, route = _path(ctx, ctx.stations[step["station"]], probe, lag, refused)
                ops += more
            jobs.append(_Job(order, max(order.release, float(create.start_time)), ops, lag))
        if order.quantity > 1:
            refused.add(f"order '{order.id}': its {order.quantity} entities share one planned "
                        f"start per station (the replay ranks them FIFO)")
    return jobs


def _probe(ctx, create, order):
    """An entity carrying what CREATE's Orders mode gives it (never released)."""
    probe = spawn_entity(ctx, attrs=create.attributes)
    probe.order_id = order.id
    if order.entity_type:
        probe.e_type = order.entity_type
    if order.due is not None:
        probe.due_date = float(order.due)
    if order.priority is not None:
        probe.priority = order.priority
    for key, value in order.attributes.items():
        setattr(probe, key, value)
    probe.sequence, probe.step = order.sequence, 0
    return probe


def _link_lag(block, probe) -> float:
    delay = block.link_delays[0] if block.link_delays else None
    return block.sample_duration(delay, probe) if delay is not None else 0.0


def _transfer(route, probe, station) -> float:
    """ROUTE's own transfer time to *station*, as `Route.self_process` finds it."""
    if route.travel == "network":
        return route._network_travel(probe, station)[0]
    return route.sample_duration(route.delay, probe, extra={
        "from_station": getattr(probe, "station", ""), "to_station": station})


def _path(ctx, block, probe, lag, refused):
    """(ops, lag after the last op, the ROUTE ending the path or None)."""
    ops, seen = [], set()
    while block is not None and block.name not in seen:
        seen.add(block.name)
        kind = block.block.block_type
        if kind == "ROUTE":
            return ops, lag, block
        if kind == "STATION":
            probe.station = block.station_name
        elif kind == "PROCESS" and block.action == "Seize Delay Release" and block.resource_name:
            ops.append(_op(ctx, block, probe, lag, refused))
            lag = 0.0
        elif kind == "DELAY" or (kind == "PROCESS" and block.action == "Delay"):
            lag += block.sample_duration(block.delay, probe)
        elif kind != "DISPOSE":
            refused.add(f"{kind} '{block.name}' (ignored by the solver)")
        if len(block.next_process) > 1:
            refused.add(f"branching at '{block.name}' (the solver follows its first link)")
        if not block.next_process:
            break
        lag += _link_lag(block, probe)
        block = block.next_process[0]
    return ops, lag, None


def _op(ctx, block, probe, lag, refused) -> _Op:
    target = ctx.resource_manager.resolve(block.resource_name)
    if target is None:
        raise ValueError(f"PROCESS '{block.name}': unknown resource '{block.resource_name}'")
    members = list(target.members) if isinstance(target, ResourceSet) else [target]
    for member in members:
        if member.has_controller:
            refused.add(f"capacity schedule or failure on '{member.name}' (the solver sees its "
                        f"full capacity)")
    if block.worker_fleet:
        refused.add(f"worker fleet at '{block.name}' (not modelled)")
    key = evaluate_expression(block, block.setup_key, probe) if block.setup_key else probe.e_type
    return _Op(block, getattr(probe, "station", None), block.sample_duration(block.delay, probe),
               lag, members, int(block.quantity), key, copy.copy(probe))


def _setups(jobs, refused) -> dict:
    """{resource: (ops, {(i, j): changeover})} for each single-unit resource
    whose only user is one PROCESS with a setup; i = -1 is the initial key."""
    by_resource: dict = {}
    for job in jobs:
        for op in job.ops:
            for member in op.members:
                by_resource.setdefault(member, []).append(op)
    out = {}
    for resource, ops in by_resource.items():
        with_setup = {op.block.name for op in ops if op.block.setup}
        if not with_setup:
            continue
        blocks = {op.block.name for op in ops}
        if resource.capacity != 1 or len(blocks) > 1 or any(len(op.members) > 1 for op in ops):
            refused.update(f"setup at '{name}' (resource '{resource.name}' is multi-unit, shared "
                           f"or in a set: the solver uses no setup)" for name in with_setup)
            continue
        block = ops[0].block
        table = {}
        for j, target in enumerate(ops):
            for i, source in [(-1, None)] + list(enumerate(ops)):
                if i == j:
                    continue
                previous = (block.initial_key or None) if source is None else source.key
                table[i, j] = (0.0 if previous is None or previous == target.key else float(
                    evaluate_expression(block, block.setup, target.probe,
                                        extra={"previous_key": previous, "key": target.key})))
        out[resource] = (ops, table)
    return out


# ------------------------------------------------------------ the solver model
def _grid(values, refused) -> int:
    """The smallest power of ten turning every time into an integer (CP-SAT
    is integer-only); 1000 with a rounding note when none up to 10^4 does."""
    for exponent in range(5):
        factor = 10 ** exponent
        if all(abs(v * factor - round(v * factor)) < 1e-6 for v in values):
            return factor
    refused.add("times not on a 0.0001 grid: rounded to 0.001 for the solver")
    return 1000


def _solve(cp_model, jobs, objective, time_limit_s, seed, refused) -> dict:
    setups = _setups(jobs, refused)
    ops = [op for job in jobs for op in job.ops]
    times = ([op.duration for op in ops] + [op.lag for op in ops]
             + [j.release for j in jobs] + [j.tail for j in jobs]
             + [v for _, table in setups.values() for v in table.values()]
             + [j.order.due for j in jobs if j.order.due is not None])
    factor = _grid(times, refused)

    def to_int(value):
        return int(round(value * factor))

    worst_setup = {op: max((to_int(v) for (_, j), v in table.items() if ops_r[j] is op), default=0)
                   for ops_r, table in setups.values() for op in ops_r}
    horizon = (max((to_int(j.release) for j in jobs), default=0)
               + sum(to_int(op.duration) + to_int(op.lag) + worst_setup.get(op, 0) for op in ops)
               + sum(to_int(j.tail) for j in jobs))
    m = cp_model.CpModel()
    usage: dict = {}
    start, end, setup_var = {}, {}, {}
    completion = {}
    for job in jobs:
        ready = to_int(job.release)
        for op in job.ops:
            s = start[op] = m.NewIntVar(0, horizon, "")
            m.Add(s >= ready + to_int(op.lag))
            duration = to_int(op.duration)
            if op in worst_setup:
                su = setup_var[op] = m.NewIntVar(0, worst_setup[op], "")
                e = end[op] = m.NewIntVar(0, horizon, "")
                interval = m.NewIntervalVar(s, su + duration, e, "")
            else:
                e = end[op] = s + duration
                interval = m.NewFixedSizeIntervalVar(s, duration, "")
            if len(op.members) == 1:
                usage.setdefault(op.members[0], []).append((interval, op.quantity))
            else:
                literals = [m.NewBoolVar("") for _ in op.members]
                m.AddExactlyOne(literals)
                for member, literal in zip(op.members, literals):
                    usage.setdefault(member, []).append(
                        (m.NewOptionalFixedSizeIntervalVar(s, duration, literal, ""), op.quantity))
            ready = e
        completion[job] = ready + to_int(job.tail)
    for resource, items in usage.items():
        if any(q > resource.capacity for _, q in items):
            raise ValueError(f"resource '{resource.name}': an operation needs more units than its "
                             f"capacity {resource.capacity}")
        if resource.capacity == 1:
            m.AddNoOverlap([interval for interval, _ in items])
        else:
            m.AddCumulative([i for i, _ in items], [q for _, q in items], resource.capacity)
    for ops_r, table in setups.values():
        arcs = []
        for j, op in enumerate(ops_r):
            for i in [-1] + list(range(len(ops_r))):
                if i == j:
                    continue
                literal = m.NewBoolVar("")
                arcs.append((i + 1, j + 1, literal))
                m.Add(setup_var[op] == to_int(table[i, j])).OnlyEnforceIf(literal)
                if i >= 0:
                    m.Add(start[op] >= end[ops_r[i]]).OnlyEnforceIf(literal)
            arcs.append((j + 1, 0, m.NewBoolVar("")))
        m.AddCircuit(arcs)

    makespan = m.NewIntVar(0, horizon, "")
    m.AddMaxEquality(makespan, list(completion.values()) or [0])
    weight_factor = 1
    if objective == "makespan":
        m.Minimize(makespan)
    else:
        weights = {job: max(1.0 if job.order.priority is None else job.order.priority, 0.0)
                   for job in jobs}
        if any(w != int(w) for w in weights.values()):
            weight_factor = 1000
        terms = []
        for order in {job.order.id: job.order for job in jobs}.values():
            if order.due is None:
                continue
            mine = [job for job in jobs if job.order is order]
            done = m.NewIntVar(0, horizon, "")
            m.AddMaxEquality(done, [completion[job] for job in mine])
            tardy = m.NewIntVar(0, horizon, "")
            m.Add(tardy >= done - to_int(order.due))
            terms.append(int(round(weights[mine[0]] * weight_factor)) * tardy)
        m.Minimize(sum(terms))

    solver = cp_model.CpSolver()
    # One worker and a fixed seed: the same model gives the same plan (D122).
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = int(seed)
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"CP-SAT found no plan ({solver.StatusName(status)}) within "
                           f"{time_limit_s} s")
    return {"status": solver.StatusName(status),
            "start": {op: solver.Value(v) / factor for op, v in start.items()},
            "makespan": solver.Value(makespan) / factor,
            "objective": solver.ObjectiveValue() / factor / weight_factor}


# ------------------------------------------------------------ the scenario
def _proposal(raw, jobs, solved, refused) -> Proposal:
    starts = solved["start"]
    plans: dict = {}
    releases: dict = {}
    used: set = set()
    for job in jobs:
        plan = plans.setdefault(job.order.id, {})
        seen = set()
        for op in job.ops:
            used.update(member.name for member in op.members)
            if op.station is None:
                continue
            if op.station in seen:
                refused.add(f"order '{job.order.id}' visits '{op.station}' again: the plan keeps "
                            f"the first visit")
                continue
            seen.add(op.station)
            plan[op.station] = min(plan.get(op.station, starts[op]), starts[op])
        if job.ops:
            first = starts[job.ops[0]] - job.ops[0].lag
            releases[job.order.id] = min(releases.get(job.order.id, first), first)

    orders = copy.deepcopy(raw.get("orders") or [])
    units, fields = units_of(raw.get("simulation")), SECTION_FIELDS["orders"]
    built = {job.order.id: job.order for job in jobs}
    for entry in orders:
        oid = str(entry.get("id", ""))
        if not plans.get(oid):
            continue
        entry["plan"] = plans[oid]
        if releases.get(oid, 0.0) > built[oid].release + 1e-9:
            # Model time back into the order's own unit (#311).
            entry["release"] = releases[oid] / field_factor(
                param(fields, "release"), entry, units, fields)
    declared = {r.get("name"): r for r in raw.get("resources") or []}
    overrides = [{"path": "orders", "value": orders}]
    base = copy.deepcopy(raw)
    for name in sorted(used):
        if name not in declared:
            refused.add(f"resource '{name}' is not declared under \"resources\": it keeps FIFO")
            continue
        # An override edits a value the model has (experiments._descend), so a
        # resource relying on the FIFO default gets it spelled out first.
        next(r for r in base["resources"] if r.get("name") == name).setdefault("discipline", "FIFO")
        overrides.append({"path": f"resources.{name}.discipline", "value": "Plan"})
    from nagasim.experiments import apply_overrides
    return Proposal(layout=apply_overrides(base, overrides), overrides=overrides,
                    objective=solved["objective"], makespan=solved["makespan"],
                    status=solved["status"], refused=sorted(refused))
