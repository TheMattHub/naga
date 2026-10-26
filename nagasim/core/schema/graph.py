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


LONG_NAME_CHARS = 20   # the widest name the canvas draws on one line (Phase 13 D)
_PROB_SUM_EPS = 1e-9 # tolerance for float sums that should equal 1.0 (e.g. 0.3 + 0.7)


def long_name_problem(block: str, shown: str, at: str) -> Problem | None:
    """W_LONG_NAME for a block whose drawn name *shown* is over LONG_NAME_CHARS, else None.
    Shared with prepare, which checks a SUBMODEL instance name before it is flattened away."""
    if len(shown) <= LONG_NAME_CHARS:
        return None
    return problem(
        "W_LONG_NAME",
        f"{block}: name is {len(shown)} characters; the canvas wraps it past {LONG_NAME_CHARS}",
        at, block=block, hint="shorten it or move the detail to the description")


def _divert_link_problems(name: str, diverts: list, outs: list, by_name: dict,
                          at: str) -> list[Problem]:
    """A belt's divert links (D851, D852): one per divert after the end's, and a "pass"
    divert's link straight to a block with places it can read for room."""
    found = []
    if len(outs) < 1 + len(diverts):
        found.append(problem(
            "E_CONVEYOR_DIVERT", f"{name}: {len(diverts)} diverts need {1 + len(diverts)} "
            f"outgoing links, it has {len(outs)}", at, block=name,
            hint="link 0 is the belt's end; divert k leaves by link k + 1"))
    for index, divert in enumerate(diverts):
        if not isinstance(divert, dict) or divert.get("when_full") != "pass" \
                or index + 1 >= len(outs):
            continue
        target = by_name.get(outs[index + 1]) or {}
        kind = str(target.get("type", "")).upper()
        params = target.get("params") or {}

        def places(field: str) -> bool:
            value = params.get(field, 0)
            return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0

        if not (kind == "CONVEYOR" or (kind == "BUFFER" and places("capacity"))
                or (kind in ("SEIZE", "PROCESS", "MOTION") and places("queue_capacity"))):
            found.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {index} passes when full, but its link "
                f"goes to '{outs[index + 1]}', which has no places to read for room",
                f"{at}/params/diverts/{index}/when_full", block=name,
                hint="link it straight to a CONVEYOR, a BUFFER with capacity > 0 or a "
                     "SEIZE/PROCESS with queue_capacity > 0, or use when_full \"wait\""))
    return found


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
        if btype == "SEPARATE":
            problems.extend(_separate_problems(name, params, len(outs), at))
            # D819: a kept carrier leaves by link 1, checked exactly above -- so keep mode's
            # link count is reported once, by E_SEPARATE_CARRIER_LINK (review P7).
            allowed = max(2, len(outs)) if params.get("carrier", "dispose") == "keep" else 1
        if btype == "CONVEYOR" and isinstance(params.get("diverts"), list) \
                and params.get("diverts"):
            # D851: divert k leaves by link k + 1, so each needs its link.
            allowed = 1 + len(params["diverts"])
            problems.extend(_divert_link_problems(name, params["diverts"], outs, by_name, at))
        if btype == "CONVEYOR" and isinstance(params.get("merges"), list):
            for index, merge in enumerate(params["merges"]):
                source = merge.get("from") if isinstance(merge, dict) else None
                if isinstance(source, str) and source.strip() \
                        and name not in drawn_links.get(source, []):
                    problems.append(problem(
                        "E_CONVEYOR_MERGE", f"{name}: merge {index} names '{source}', which "
                        f"does not link to this belt", f"{at}/params/merges/{index}/from",
                        block=name, hint="link that block to the belt, or name the one that "
                                         "does"))
        if btype == "BATCH" and not str(params.get("carrier_type") or "").strip() and (
                str(params.get("member_time") or "").strip()
                or str(params.get("carrier_capacity") or "").strip()):
            problems.append(problem(
                "E_BATCH_MEMBER_TIME",
                f"{name}: member_time and carrier_capacity apply only to a carrier BATCH", at,
                block=name, hint="set carrier_type to the carrier's entity type (a truck), or "
                                 "clear member_time and carrier_capacity"))
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

        # The study's car park task: AVAILABLE() reads as "free units" but is the capacity in
        # force, so a test of it against the occupancy never fires (no error, a wrong answer).
        tests = [("balk_if", params.get("balk_if"))] + [
            (f"conditions/{i}", c) for i, c in enumerate(params.get("conditions") or [])
            if btype == "DECIDE"]
        for key, text in tests:
            if isinstance(text, str) and "AVAILABLE(" in text.replace(" ", "").upper():
                problems.append(problem(
                    "W_AVAILABLE_IS_CAPACITY",
                    f"{name}: AVAILABLE() is the capacity in force, seized units not deducted",
                    f"{at}/params/{key}", block=name,
                    hint='for the free units use IDLE("name"); for the busy ones NR("name")'))
        if btype == "DECIDE":
            params = block.get("params", {})
            mode = params.get("mode", "Chance")
            if mode == "Chance":
                probs = params.get("probs", [])
                # #423: conditions in a Chance DECIDE are never read; the probs hints below
                # would lead the author further from what they meant.
                mode_hint = ('these are conditions: set "mode": "Condition"'
                             if params.get("conditions") and not probs else None)
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
                        hint=mode_hint or "add one more outgoing link for the remaining "
                                          "probability, or make the probs sum to 1"))
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
        # Counted in characters, not pixels: the engine never measures fonts. Checked before
        # the reachability `continue` so an unlinked block warns too. Advisory only.
        # A flattened submodel block is `instance/inner`; the canvas draws only the last segment.
        long_name = long_name_problem(name, name.rsplit("/", 1)[-1], at)
        if long_name:
            problems.append(long_name)
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


def _separate_problems(name: str, params: dict, n_outs: int, at: str) -> list[Problem]:
    """A SEPARATE's carrier mode (Phase 15 M2, D819): its choices, its second link,
    and a member_time only where a carrier lives on."""
    out = []
    carrier = params.get("carrier", "dispose") or "dispose"
    if carrier not in ("dispose", "keep"):
        out.append(problem("E_SEPARATE_CARRIER", f"{name}: carrier {carrier!r} is not "
                           f"\"dispose\" or \"keep\"", f"{at}/params/carrier", block=name,
                           hint="\"dispose\" ends the batch here; \"keep\" lets the carrier "
                                "(a truck) leave by a second link"))
    if (params.get("release_order", "fifo") or "fifo") not in ("fifo", "lifo"):
        out.append(problem("E_SEPARATE_RELEASE_ORDER", f"{name}: release_order "
                           f"{params.get('release_order')!r} is not \"fifo\" or \"lifo\"",
                           f"{at}/params/release_order", block=name,
                           hint="\"fifo\": the order they were loaded; \"lifo\": last loaded first"))
    if carrier == "keep" and n_outs != 2:
        out.append(problem("E_SEPARATE_CARRIER_LINK", f"{name}: a SEPARATE that keeps the "
                           f"carrier needs exactly 2 outgoing links, has {n_outs}", at, block=name,
                           hint="link 0 takes the members, link 1 the carrier once it is empty"))
    if carrier == "dispose" and str(params.get("member_time") or "").strip():
        out.append(problem("E_SEPARATE_MEMBER_TIME", f"{name}: member_time applies only when "
                           f"the carrier is kept", f"{at}/params/member_time", block=name,
                           hint="set carrier to \"keep\", or clear member_time"))
    return out


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
