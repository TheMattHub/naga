"""Flow-graph validation: reachability, branch counts, DECIDE branch coverage.
Pure dicts, no env, no processes."""
from __future__ import annotations

from nagasim.core import agents
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.blocks.decide import FALLBACK_MODES
from nagasim.core.problems import Problem, problem
from nagasim.core.schema.common import _link_pairs
from nagasim.core.seizing import QUEUEING_BLOCK_TYPES
from nagasim.core.sequences import station_blocks


_PROB_SUM_EPS = 1e-9  # tolerance for float sums that should equal 1.0 (e.g. 0.3 + 0.7)


def validate_graph_structured(layout: dict) -> list[Problem]:
    """Pure-dict flow-graph checks as coded Problems (ROADMAP 3.1 item 1):
    reachability, branch counts, DECIDE branch coverage. An iterative DFS, so
    rework loops (cycles) never recurse; W_ problems are advisory (unreachable
    blocks), everything else is a real problem."""
    problems: list[Problem] = []
    blocks = layout.get("blocks", [])
    by_name = {b["name"]: b for b in blocks}
    index_of = {b["name"]: i for i, b in enumerate(blocks)}
    out_links: dict[str, list[str]] = {name: [] for name in by_name}
    for src, tgt in _link_pairs(layout):
        out_links.setdefault(src, []).append(tgt)
    # Route jumps are edges too (PHASE4 A2). A ROUTE block reaches a station
    # only at run time, through whatever sequence the entity in hand carries,
    # so the static graph has to assume it can reach ANY station a sequence
    # names -- otherwise a routed job shop, whose stations have no incoming
    # link at all, would report every one of them unreachable. `out_links` is
    # what reachability walks; `drawn_links` below keeps the per-block
    # branch-count rules on the DRAWN links only, since a jump is not a branch.
    drawn_links = {name: list(targets) for name, targets in out_links.items()}
    # A step may name a station by its block name or by its `station` alias
    # (PHASE4 B6), so the edge goes to the BLOCK either one resolves to.
    serves = station_blocks(layout)
    routed = [serves[station] for entry in layout.get("sequences", []) or []
              for step in entry.get("steps") or []
              if isinstance(step, dict) and (station := str(step.get("station", ""))) in serves]
    for block in blocks:
        kind = block.get("type", "").upper()
        # TRANSPORT jumps the same way, by the same sequence (PHASE4 E1).
        if routed and kind in ("ROUTE", "TRANSPORT"):
            out_links.setdefault(block["name"], []).extend(routed)
        # #318: with a `destination` it delivers to that STATION whatever the
        # sequences say, so the station and all it leads to are reachable.
        destination = str((block.get("params") or {}).get("destination") or "")
        if kind == "TRANSPORT" and destination in serves:
            out_links.setdefault(block["name"], []).append(serves[destination])

    creates = [b for b in blocks if b.get("type", "").upper() == "CREATE"]
    disposes = [b for b in blocks if b.get("type", "").upper() == "DISPOSE"]
    # A purely CONTINUOUS model has no entities at all -- a PV array, a battery
    # and a grid connection are levels and flows, not a queue (PHASE4 D1) -- so
    # a model with flows and no blocks is complete, not broken. One with SOME
    # blocks still needs both ends: a half-drawn entity flow is still a mistake.
    # A pure system-dynamics model (PHASE5 SD-1) is levels and flows only, too.
    # #185: a brand-new model (no blocks, no continuous or agent sections at
    # all) is not "half-drawn" -- ProblemsPanel shows an "Empty model"
    # placeholder instead of E_NO_CREATE/E_NO_DISPOSE.
    if not blocks and not (layout.get("flows") or layout.get("conversions")
                           or layout.get("levels") or layout.get("auxiliaries")
                           or layout.get("delays") or layout.get("populations")
                           or layout.get("agent_types")):
        return problems
    if not blocks and (layout.get("flows") or layout.get("conversions")
                       or layout.get("levels") or layout.get("auxiliaries")
                       or layout.get("delays") or layout.get("populations")):
        return problems
    # An agent's `enter` action is an entity source too (PHASE5 AB-2).
    entered = agents.entered_blocks(layout)
    if not creates and not entered:
        problems.append(problem("E_NO_CREATE", "model: no CREATE block", "/blocks",
                                hint="add a CREATE block: it is the only source of entities"))
    if not disposes:
        problems.append(problem("E_NO_DISPOSE", "model: no DISPOSE block", "/blocks",
                                hint="add a DISPOSE block and link the end of the flow to it"))

    for block in blocks:
        name = block["name"]
        btype = block.get("type", "").upper()
        outs = drawn_links.get(name, [])
        at = f"/blocks/{index_of[name]}"

        if btype == "CREATE" and not outs:
            problems.append(problem("E_CREATE_NO_OUTPUT", f"{name}: CREATE has no outgoing link",
                                    at, block=name,
                                    hint="link this CREATE to the first block of the flow"))
        if btype == "CREATE" and _is_endless_zero_gap(block.get("params", {})):
            problems.append(problem("E_CREATE_ZERO_INTERARRIVAL",
                                    f"{name}: interarrival time is a constant 0 with unlimited arrivals",
                                    f"{at}/params/dist_params", block=name,
                                    hint="set a positive interarrival time, or a max_arrivals "
                                         "for a burst at one instant"))
        if btype == "DISPOSE" and outs:
            problems.append(problem("E_DISPOSE_HAS_OUTPUT",
                                    f"{name}: DISPOSE must have no outgoing link", at, block=name,
                                    hint="remove the outgoing link: DISPOSE ends the flow"))
        # DECIDE/LOGIC route by an index chosen inside self_process; a plugin
        # block with the same shape declares BlockSpec(multi_output=True)
        # instead of needing an engine-side change (ROADMAP 2.4 B).
        spec = BLOCK_REGISTRY.get(btype)
        # PHASE4 A4: a queueing block with balk_if/renege_after gets a SECOND
        # output, index 1 = "left without service" (ARCHITECTURE D54).
        params = block.get("params", {})
        queueing = btype in QUEUEING_BLOCK_TYPES
        leaves = queueing and bool(params.get("balk_if") or params.get("renege_after"))
        # A queueing block may always be DRAWN with two outputs; the second one
        # is an error only past two, and a warning when nothing takes it.
        allowed = 2 if queueing else 1
        if not (spec is not None and spec.multi_output) and len(outs) > allowed:
            problems.append(problem(
                "E_TOO_MANY_OUTPUTS",
                f"{name}: {len(outs)} outgoing links, at most {allowed} allowed", at, block=name,
                hint="only DECIDE and LOGIC may branch: insert a DECIDE, or remove the extra links"))
        if leaves and len(outs) < 2:
            problems.append(problem(
                "E_QUEUE_NO_LEAVE_LINK",
                f"{name}: balk_if/renege_after needs a second outgoing link", at, block=name,
                hint="link this block to where entities that leave without service go "
                     "(a DISPOSE, or a RECORD before one); it is branch 1"))
        elif queueing and not leaves and len(outs) == 2:
            problems.append(problem(
                "W_QUEUE_LEAVE_LINK_UNUSED",
                f"{name}: second outgoing link but no balk_if or renege_after, so "
                f"nothing ever takes it", at, block=name,
                hint="set balk_if or renege_after, or remove the second link"))

        if btype == "DECIDE":
            params = block.get("params", {})
            mode = params.get("mode", "Chance")
            if mode == "Chance":
                probs = params.get("probs", [])
                if len(probs) > len(outs):
                    problems.append(problem(
                        "E_DECIDE_BRANCHES",
                        f"{name}: {len(probs)} probs but only {len(outs)} outgoing links",
                        f"{at}/params/probs", block=name,
                        hint=f"link {len(probs) - len(outs)} more target(s), or drop the extra probs"))
                total = sum(probs)
                if total > 1 + _PROB_SUM_EPS:
                    problems.append(problem(
                        "E_DECIDE_PROBS", f"{name}: probs sum to {total}, must be <= 1",
                        f"{at}/params/probs", block=name,
                        hint="probabilities are fractions of 1: scale them down"))
                elif total < 1 - _PROB_SUM_EPS and len(outs) != len(probs) + 1:
                    problems.append(problem(
                        "E_DECIDE_PROBS",
                        f"{name}: probs sum to {total} < 1, needs one more link than probs (the ELSE)",
                        f"{at}/params/probs", block=name,
                        hint="add one more outgoing link for the remaining probability, or make "
                             "the probs sum to 1"))
            elif mode == "Condition":
                conditions = params.get("conditions", [])
                if len(conditions) > len(outs):
                    problems.append(problem(
                        "E_DECIDE_BRANCHES",
                        f"{name}: {len(conditions)} conditions but only {len(outs)} outgoing links",
                        f"{at}/params/conditions", block=name,
                        hint="each condition needs its own outgoing link, in the same order"))
            elif mode == "Policy":
                # Actions = outgoing links, whatever their count -- no probs/
                # conditions to match against (ROADMAP 3.7 A).
                if not params.get("policy"):
                    problems.append(problem(
                        "E_DECIDE_POLICY_NAME", f"{name}: mode \"Policy\" needs a non-empty policy",
                        f"{at}/params/policy", block=name,
                        hint="name a decision point, e.g. \"routing\"; several DECIDE blocks may share one"))
                fallback = params.get("fallback", "first")
                if fallback not in FALLBACK_MODES:
                    problems.append(problem(
                        "E_DECIDE_FALLBACK", f"{name}: unknown fallback '{fallback}'",
                        f"{at}/params/fallback", block=name,
                        hint=f"use one of: {', '.join(FALLBACK_MODES)}"))
                elif fallback == "shortest" and len(params.get("queue_exprs", [])) != len(outs):
                    problems.append(problem(
                        "E_DECIDE_QUEUE_EXPRS",
                        f"{name}: fallback \"shortest\" needs {len(outs)} queue_exprs "
                        f"(one per outgoing link), has {len(params.get('queue_exprs', []))}",
                        f"{at}/params/queue_exprs", block=name,
                        hint="give one expression per outgoing link, in branch order"))

    # Reachability from every CREATE; iterative DFS, cycles legal (DEVLOG #1 -- the
    # legacy graph_model.dfs recurses and blows the stack on a rework loop).
    visited: set[str] = set()
    stack = [b["name"] for b in creates] + sorted(entered)
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        stack.extend(out_links.get(current, []))

    for block in blocks:
        name = block["name"]
        btype = block.get("type", "").upper()
        at = f"/blocks/{index_of[name]}"
        if name not in visited:
            problems.append(problem("W_UNREACHABLE", f"{name} is unreachable from any CREATE",
                                    at, block=name,
                                    hint="link it into the flow, or delete it"))
            continue
        # DRAWN links, not the route jumps: a ROUTE still needs its own outgoing
        # link for the entity whose sequence has finished (PHASE4 A2).
        if not drawn_links.get(name, []) and btype != "DISPOSE":
            problems.append(problem(
                "E_TERMINAL_NOT_DISPOSE",
                f"{name}: terminal block reachable from a CREATE is not DISPOSE",
                at, block=name, hint="add a DISPOSE block or link this block to one"))

    return problems


def _is_endless_zero_gap(params: dict) -> bool:
    """A CREATE whose gap is the literal constant 0 and that never stops
    (DEVLOG #88). An expression or a random gap is the engine guard's job
    (`sim_runner.STALL_EVENT_LIMIT`)."""
    if params.get("arrival_mode", "Interarrival") != "Interarrival":
        return False
    if params.get("distribution", "Constant") != "Constant" or params.get("max_arrivals", 0):
        return False
    given = params.get("dist_params")       # not a mapping: reported as E_PARAM_TYPE (#407)
    value = given.get("value", 1.0) if isinstance(given, dict) else 1.0
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0
