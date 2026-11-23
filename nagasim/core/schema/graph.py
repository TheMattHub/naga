"""Flow-graph validation: reachability, branch counts, DECIDE branch coverage.
Pure dicts, no env, no processes."""
from __future__ import annotations

from nagasim.core import agents
from nagasim.core.arealink import area_pairs
from nagasim.core.beltpick import first_belt_target
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.conveyor_path import belt_exit_targets, exit_names, exit_slot, reverse_chains
from nagasim.core.blocks.decide import FALLBACK_MODES
from nagasim.core.problems import Problem, problem
from nagasim.core.quantities import field_factor, param
from nagasim.core.blocking import BELT_TYPES as BELT_BLOCKS, TRANSPARENT_TYPES
from nagasim.core.blocks.conveyor import SPEC as BELT_SPEC
from nagasim.core.blocks.transfer_piece import turn_angle
from nagasim.core.conveyor_types import TYPES as CONVEYOR_TYPES
from nagasim.core.timeunits import LENGTH_UNITS, UNITS, base_factor, units_of
from nagasim.core.routing import (divert_exits, divert_groups, divert_slots, rule_spec,
                                  split_exit, validate_rule)
from nagasim.core.schema.common import _link_pairs
from nagasim.core.seizing import QUEUEING_BLOCK_TYPES
from nagasim.core.sequences import station_blocks
from nagasim.core.utils import is_number


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
        at, block=block, hint="shorten it or move the detail to the description",
        fields={"case": "long", "n": len(shown)})


def _picks_from(block: dict, belt: str, index: int) -> bool:
    """Whether *block* is a MOTION, or a robot pattern BATCH, whose first move goes to a
    target `{"belt": belt, "divert": index}` (D1343)."""
    kind = str(block.get("type", "")).upper()
    params = block.get("params") or {}
    if kind != "MOTION" and not (kind == "BATCH" and params.get("robot") and params.get("pattern")):
        return False
    first = first_belt_target(params) or {}
    return first.get("belt") == belt and first.get("divert") == index


def _route_warnings(name: str, spec: dict, targets: list, by_name: dict, drawn_links: dict,
                    at: str) -> list[Problem]:
    """W_ROUTE_RULE (D1359) of a usable rule whose exit i leaves to *targets*[i]: a condition
    per exit with no ELSE (R27: a part no condition takes stops the run), and a limit or
    least_loaded over an exit that counts nothing (no target piece on its way, `_walk_end`, and
    no named target)."""
    found = []
    if spec.get("kind") == "condition" and len(spec.get("conditions") or []) == len(spec["exits"]):
        found.append(problem(
            "W_ROUTE_RULE", f"{name}: the routing rule has no ELSE exit, so a part no condition "
            f"takes stops the run", f"{at}/conditions", block=name,
            hint="drop the last exit's condition: that exit then takes the rest",
            fields={"case": "no_else"}))
    if (spec.get("limit") or 0) > 0 or spec.get("kind") == "least_loaded":
        for exit_name, target in zip(spec["exits"], targets):
            if not split_exit(exit_name)[1] and \
                    _walk_end(target, name, by_name, drawn_links) is None:
                found.append(problem(
                    "W_ROUTE_RULE", f"{name}: rule exit {exit_name!r} leads to no CONVEYOR, so "
                    f"no part counts under way there and its limit or load reads 0",
                    f"{at}/exits", block=name,
                    hint="name its target piece as \"name:belt\", or route by another kind",
                    fields={"case": "uncounted", "exit": str(exit_name)}))
    return found


def _reaches(start, goal: str, drawn_links: dict, avoid: str) -> bool:
    """Some way of drawn links leads from block *start* to block *goal*, never through
    *avoid* (the routing block)."""
    todo, seen = [start], {avoid}
    while todo:
        block = todo.pop()
        if block is None or block in seen:
            continue
        if block == goal:
            return True
        seen.add(block)
        todo.extend(drawn_links.get(block, []))
    return False


def _route_target_problems(name: str, spec: dict, targets: list, by_name: dict,
                           drawn_links: dict, code: str, at: str) -> list[Problem]:
    """R34: every exit of a usable rule that names its target piece ("name:target") names a
    CONVEYOR its loads reach (downstream of the block the exit's link goes to): one check for
    a belt's divert group, a CONVEYOR_TRANSFER and a DECIDE, each under its own code."""
    found = []
    for exit_name, target in zip(spec["exits"], targets):
        named = split_exit(exit_name)[1]
        if not named:
            continue
        if _kind(by_name.get(named)) != "CONVEYOR":
            why = "which is not a CONVEYOR"
        elif not _reaches(target, named, drawn_links, name):
            why = "which no load of that exit reaches"
        else:
            continue
        found.append(problem(
            code, f"{name}: rule exit {exit_name!r} names target piece {named!r}, {why}",
            f"{at}/exits", block=name,
            hint="name a CONVEYOR on that exit's way, or give the exit no target: it then "
                 "counts to the last conveyor of its branch",
            fields={"case": "route_target", "exit": str(exit_name), "target": named}))
    return found


def _group_route_warnings(name: str, members: list, outs: list, by_name: dict,
                          drawn_links: dict, at: str) -> list[Problem]:
    """W_ROUTE_RULE of a belt's divert group (D1359): exit i leaves by its divert's link
    (divert k: link k + 1), the exit `out` by link 0 (R28); and its named targets (R34)."""
    rule = members[0][1].get("rule")
    if not isinstance(rule, dict):
        return []
    spec = rule_spec(rule, divert_exits(index for index, _divert in members))
    try:
        validate_rule(spec)
        slots = divert_slots(spec["exits"], len(members))
    except ValueError:
        return []           # reported as E_CONVEYOR_DIVERT
    links = [0 if s is None else members[s][0] + 1 for s in slots]
    targets = [outs[k] if k < len(outs) else "" for k in links]
    where = f"{at}/params/diverts/{members[0][0]}/rule"
    return [*_route_target_problems(name, spec, targets, by_name, drawn_links,
                                    "E_CONVEYOR_DIVERT", where),
            *_route_warnings(name, spec, targets, by_name, drawn_links, where)]


def _divert_link_problems(name: str, diverts: list, outs: list, by_name: dict,
                          at: str, areas: dict) -> list[Problem]:
    """A belt's divert links (D851, D852): one per divert after the end's, and a "pass"
    divert's link straight to a block with places it can read for room."""
    found = []
    if len(outs) < 1 + len(diverts):
        found.append(problem(
            "E_CONVEYOR_DIVERT", f"{name}: {len(diverts)} diverts need {1 + len(diverts)} "
            f"outgoing links, it has {len(outs)}", at, block=name,
            hint="link 0 is the belt's end; divert k leaves by link k + 1",
            fields={"case": "a7_links", "diverts": len(diverts), "needed": 1 + len(diverts), "links": len(outs)}))
    for index, divert in enumerate(diverts):
        over = areas.get((name, outs[index + 1])) if index + 1 < len(outs) else None
        if isinstance(divert, dict) and divert.get("intercept") is True and index + 1 < len(outs):
            if over is not None or not _picks_from(by_name.get(outs[index + 1]) or {}, name, index):
                found.append(problem(
                    "E_CONVEYOR_DIVERT", f"{name}: divert {index} meets a robot, but its link "
                    f"goes to '{outs[index + 1]}', which does not pick from it at that divert",
                    f"{at}/params/diverts/{index}", block=name,
                    hint="link it straight to a MOTION, or a pattern BATCH with a robot, whose "
                         "first move targets {\"belt\": this belt, \"divert\": k}",
                    fields={"case": "a7_robot", "index": index}))
            continue        # a robot is ready or not: no places to read for room
        if not isinstance(divert, dict) or divert.get("when_full") != "pass" \
                or index + 1 >= len(outs):
            continue
        target = by_name.get(outs[index + 1]) or {}
        kind = str(target.get("type", "")).upper()
        params = target.get("params") or {}

        def places(field: str) -> bool:
            value = params.get(field, 0)
            return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0

        if over is not None:        # D1369: over an area link it reads the link's places
            roomy = isinstance(over, int) and not isinstance(over, bool) and over > 0
        else:
            roomy = (kind == "CONVEYOR" or (kind == "BUFFER" and places("capacity"))
                     or (kind in ("SEIZE", "PROCESS", "MOTION") and places("queue_capacity")))
        if not roomy:
            found.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: divert {index} passes when full, but its link "
                f"goes to '{outs[index + 1]}', which has no places to read for room",
                f"{at}/params/diverts/{index}/when_full", block=name,
                hint="link it straight to a CONVEYOR, a BUFFER with capacity > 0 or a "
                     "SEIZE/PROCESS with queue_capacity > 0, give its link a delay and a "
                     "capacity, or use when_full \"wait\"",
                fields={"case": "a7_room", "index": index}))
    return found


def _belt_port_problems(layout: dict, by_name: dict) -> list[Problem]:
    """A link out of a CONVEYOR may name its exit: "out" (the end) or "d<k>" (divert k of the
    belt), each at most once per belt. Any other name, or a name given twice, is refused, since
    a typo would otherwise route parts to an exit nobody chose."""
    found = []
    taken: dict[tuple, int] = {}
    for index, link in enumerate(layout.get("links", [])):
        if not isinstance(link, dict) or link.get("source_port") in (None, ""):
            continue
        source = link.get("source")
        block = by_name.get(source) or {}
        if str(block.get("type", "")).upper() == "CONVEYOR_TRANSFER":
            found.extend(_transfer_port_problem(link, index, source, exit_names(block), taken))
            continue
        if str(block.get("type", "")).upper() != "CONVEYOR":
            continue
        diverts = (block.get("params") or {}).get("diverts")
        count = len(diverts) if isinstance(diverts, list) else 0
        port = link["source_port"]
        names = exit_names(block)
        reversible = names == ["out", "in"]
        # D1368: a reversible belt's exits are "out" (the end) and "in" (the start)
        slot = (names.index(port) if port in names else None) if reversible \
            else exit_slot(port, count)
        if slot is None:
            found.append(problem(
                "E_CONVEYOR_DIVERT", f"{source}: source_port {port!r} is not an exit "
                f"of the belt", f"/links/{index}/source_port", block=source,
                hint="use \"out\" for the end, or \"in\" for the start of a reversible belt"
                if reversible else
                "use \"out\" for the end, or \"d<k>\" for divert k (a belt with "
                f"{count} diverts has {', '.join(f'd{k}' for k in range(count)) or 'none'})",
                fields={"case": "a3_port", "port": str(port), "diverts": count}))
        elif (source, slot) in taken:
            found.append(problem(
                "E_CONVEYOR_DIVERT", f"{source}: exit {port!r} is named by links "
                f"{taken[source, slot]} and {index}", f"/links/{index}/source_port", block=source,
                hint="each exit of a belt takes one link: remove one of the two, or name "
                     "another exit",
                fields={"case": "a3_port_twice", "port": port, "first": taken[source, slot]}))
        else:
            taken[source, slot] = index
    return found


def _feeds(source: str, belt: str, by_name: dict, drawn_links: dict) -> bool:
    """*source*'s links reach *belt* directly or through transparent blocks only."""
    stack, seen = list(drawn_links.get(source, [])), set()
    while stack:
        node = stack.pop()
        if node == belt:
            return True
        if node in seen or _kind(by_name.get(node)) not in TRANSPARENT_TYPES:
            continue
        seen.add(node)
        stack.extend(drawn_links.get(node, []))
    return False


def _chain_member_problems(name: str, params: dict, at: str, chains: dict) -> list[Problem]:
    """D1392: a later piece of a reversible chain hands back to the piece before it, so it names
    no reverse_to (the chain's first piece does); a ring of such pieces has no first piece."""
    before = {after: piece for piece, after in chains.items()}
    head, seen = name, {name}
    while head in before:
        head = before[head]
        if head in seen:
            return [problem(
                "E_CONVEYOR_REVERSE", f"{name}: reversible belts linked end to start in a ring "
                f"have no chain start", at, block=name,
                hint="break the ring with a mechanism or a block between two pieces",
                fields={"case": "ring"})]
        seen.add(head)
    target = params.get("reverse_to", "")
    if isinstance(target, str) and not target.strip():
        return []
    return [problem(
        "E_CONVEYOR_REVERSE", f"{name}: reverse_to belongs to the first piece of its chain "
        f"({head!r}); this piece hands back to {before[name]!r}", f"{at}/params/reverse_to",
        block=name, hint=f"remove reverse_to here and give it to {head!r}",
        fields={"case": "member", "first": head})]


def _reverse_exit_problems(name: str, params: dict, outs: list, at: str, layout: dict,
                           by_name: dict, drawn_links: dict) -> list[Problem]:
    """A reversible CONVEYOR's `reverse_to` (D1368): the exit "in" (a link whose source_port
    is "in", R47) or the target of one of its links, the way a rule's exits name a belt's
    exits; never a CONVEYOR feeding this belt (R48): nothing boards a belt by its end."""
    target = params.get("reverse_to", "")
    if not isinstance(target, str) or not target.strip():
        return [problem(
            "E_CONVEYOR_REVERSE", f"{name}: a reversible belt needs reverse_to, the exit for "
            f"parts leaving at its start", f"{at}/params/reverse_to", block=name,
            hint="name the block a link from this belt goes to, or \"in\" for the link whose "
                 "source_port is \"in\"",
            fields={"case": "no_target"})]
    target = target.strip()
    if target == "in":
        ported = [link.get("target") for link in layout.get("links") or []
                  if isinstance(link, dict) and link.get("source") == name
                  and link.get("source_port") == "in"]
        target = ported[0] if ported else None
        if target is None:
            return [problem(
                "E_CONVEYOR_REVERSE", f"{name}: reverse_to 'in' is not an exit of the belt: no "
                f"link from it has source_port \"in\"", f"{at}/params/reverse_to", block=name,
                hint="give the link for parts leaving at the start source_port \"in\", or name "
                     "its target block",
                fields={"case": "target", "target": "in"})]
    if target in outs:
        if _kind(by_name.get(target)) == "CONVEYOR" and _feeds(target, name, by_name,
                                                               drawn_links):
            return [problem(
                "E_CONVEYOR_REVERSE", f"{name}: reverse_to {target!r} is a conveyor that feeds "
                f"this belt, and nothing boards a belt by its end", f"{at}/params/reverse_to",
                block=name, hint="put a mechanism between the pieces",
                fields={"case": "feeder", "target": target})]
        return []
    return [problem(
        "E_CONVEYOR_REVERSE", f"{name}: reverse_to {target!r} is not an exit of the belt",
        f"{at}/params/reverse_to", block=name,
        hint=f"name the target of one of its links ({', '.join(map(repr, outs)) or 'none'}), "
             f"or \"in\"",
        fields={"case": "target", "target": target})]


def _transfer_port_problem(link: dict, index: int, source: str, names: list,
                           taken: dict) -> list[Problem]:
    """A link out of a CONVEYOR_TRANSFER names one of its exits' ports, each at most once."""
    port = link["source_port"]
    if port not in names:
        return [problem(
            "E_CONVEYOR_DIVERT", f"{source}: source_port {port!r} is not an exit of the "
            f"transfer", f"/links/{index}/source_port", block=source,
            hint=f"use one of its exits' ports: {', '.join(repr(n) for n in names)}",
            fields={"case": "transfer_port", "port": str(port)})]
    slot = names.index(port)
    if (source, slot) in taken:
        return [problem(
            "E_CONVEYOR_DIVERT", f"{source}: exit {port!r} is named by links "
            f"{taken[source, slot]} and {index}", f"/links/{index}/source_port", block=source,
            hint="each exit of a transfer takes one link: remove one of the two, or name "
                 "another exit",
            fields={"case": "transfer_port_twice", "port": port, "first": taken[source, slot]})]
    taken[source, slot] = index
    return []


def _transfer_rule_problems(name: str, block: dict, outs: list, by_name: dict,
                            drawn_links: dict, at: str) -> list[Problem]:
    """A CONVEYOR_TRANSFER's routing rule: usable, one exit per exit of the module (its exits
    default to the ports), and the W_ROUTE_RULE warnings over the links in exit order."""
    rule = (block.get("params") or {}).get("rule")
    if not rule:
        return []
    names = exit_names(block)
    spec = rule_spec(rule, names)
    try:
        validate_rule(spec)
        if len(spec["exits"]) != len(names):
            raise ValueError(f"it has {len(spec['exits'])} exits for the module's {len(names)}")
    except ValueError as exc:
        return [problem(
            "E_CONVEYOR_DIVERT", f"{name}: routing rule: {exc}", f"{at}/params/rule",
            block=name, hint="kind is one of fixed, sequential, cyclic, round_robin, "
                             "least_loaded, condition, attribute, item; exits, if given, one "
                             "per exit of the module",
            fields={"case": "transfer_rule"})]
    if len(outs) != len(names):
        return []           # reported as transfer_links
    return [*_route_target_problems(name, spec, list(outs), by_name, drawn_links,
                                    "E_CONVEYOR_DIVERT", f"{at}/params/rule"),
            *_route_warnings(name, spec, list(outs), by_name, drawn_links,
                             f"{at}/params/rule")]


def _kind(block) -> str:
    return str((block or {}).get("type", "")).upper()


def _belt_base(belt: dict, field: str, value, units) -> float:
    """*value* of CONVEYOR *field* (`length`, `spacing`, `part_length`, `speed`) in the run's
    base, read with its own unit field as the run reads it; a stop's `at` is in the unit of
    `length`."""
    return float(value) * field_factor(param(BELT_SPEC, field), belt.get("params") or {}, units,
                                       BELT_SPEC.params)


def _receiving(name: str, link: int, outs: list, by_name: dict, drawn_links: dict,
               delayed: set) -> tuple:
    """(the CONVEYOR exit *link* of transfer *name* hands to, the transparent block on the
    way that branches, the block that links into that CONVEYOR): the walk the run makes
    (`ConveyorTransfer.receiving_belt`)."""
    source, target, seen = name, outs[link] if link < len(outs) else None, set()
    while target is not None:
        if (source, target) in delayed:
            return None, None, None
        block = by_name.get(target)
        if _kind(block) == "CONVEYOR":
            return target, None, source
        if _kind(block) not in TRANSPARENT_TYPES or target in seen:
            return None, None, None
        nexts = drawn_links.get(target, [])
        if len(nexts) != 1:
            return None, target, None
        seen.add(target)
        source, target = target, nexts[0]
    return None, None, None


def _eyes_seen(belt: dict, source: str) -> list:
    """The eyes of *belt* a load handed to it by *source* passes: past the merge *source*
    joins at, else every eye (R36, `_MergeEntry.has_eye_for`)."""
    params = belt.get("params") or {}
    eyes = [e for e in params.get("eyes") or [] if isinstance(e, dict)]
    for merge in params.get("merges") or []:
        if isinstance(merge, dict) and merge.get("from") == source and is_number(merge.get("at")):
            return [e for e in eyes if is_number(e.get("at")) and e["at"] > merge["at"]]
    return eyes


def _longest_item(belt: dict, layout: dict, units) -> float:
    """The longest item that can ride *belt*, in the run's base length: every sized entity
    type's length, and an unsized item's (part_length, else the type's zone or the spacing)."""
    sizes = [float(e["size"]["length"]) * base_factor("length", units.length, units)
             for e in layout.get("entities") or [] if isinstance(e, dict)
             and isinstance(e.get("size"), dict) and is_number(e["size"].get("length"))]
    params = belt.get("params") or {}
    if is_number(params.get("part_length")) and params["part_length"] > 0:
        sizes.append(_belt_base(belt, "part_length", params["part_length"], units))
    elif params.get("type") in CONVEYOR_TYPES:
        sizes.append(CONVEYOR_TYPES[params["type"]].zone_length)
    elif is_number(params.get("spacing", 1.0)):
        sizes.append(_belt_base(belt, "spacing", params.get("spacing", 1.0), units))
    return max(sizes) if sizes else 0.0


class _Drawn(dict):
    """The drawn links by source ({name: [target, ...]}), plus `areas`: the (source,
    target) pairs that are area links (D1369) with their capacity, where a target-piece walk
    stops as the run's does at the hidden process, and a transfer's handshake ends as at a
    delayed link."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.areas: dict = {}


def _walk_end(start, source: str, by_name: dict, drawn_links: dict):
    """R5's default target piece of an exit of routing block *source* whose link goes to
    *start*: each belt's end to the next CONVEYOR, through a CONVEYOR_TRANSFER with one exit
    (R36), never back to *source* nor, for a DECIDE, to a belt that feeds it (R38), nor across
    an area link (R50: a non-conveyor step); the last CONVEYOR walked, None when there is none.
    The walk the run makes (`RoutePoint._chain_of`)."""
    areas = getattr(drawn_links, "areas", ())
    if (source, start) in areas:
        return None
    seen = {source}
    if _kind(by_name.get(source)) not in BELT_BLOCKS:
        seen |= {name for name, block in by_name.items()
                 if _kind(block) == "CONVEYOR" and source in drawn_links.get(name, [])}
    piece, last = start, None
    while _kind(by_name.get(piece)) in BELT_BLOCKS and piece not in seen:
        seen.add(piece)
        nexts = drawn_links.get(piece, [])
        if _kind(by_name.get(piece)) == "CONVEYOR":
            last = piece
        elif len(nexts) != 1:
            break
        after = nexts[0] if nexts else None
        if (piece, after) in areas:
            break
        piece = after
    return last


def _feed_speeds(name: str, by_name: dict, drawn_links: dict, units) -> set:
    """The speeds of the CONVEYORs linking straight to *name*, in the run's base."""
    return {round(_belt_base(b, "speed", (b.get("params") or {}).get("speed", 1.0), units), 12)
            for feeder, b in by_name.items() if _kind(b) == "CONVEYOR"
            and name in drawn_links.get(feeder, [])
            and is_number((b.get("params") or {}).get("speed", 1.0))}


def _transfer_exit_problems(name: str, block: dict, outs: list, by_name: dict,
                            drawn_links: dict, layout: dict, at: str) -> list[Problem]:
    """What a CONVEYOR_TRANSFER's exits cannot time or count (W_ROUTE_RULE): a branching
    block between it and its belt (no handshake), a belt with no eye and a stop within the
    longest item (the module frees too early), a limit or least_loaded whose count ends at the
    handshake; and transfer_speed 0 with no one feeding speed where a move needs it
    (E_CONVEYOR_PARAM)."""
    units = units_of(layout.get("simulation") or {})
    if units.length not in LENGTH_UNITS or units.time not in UNITS:
        return []       # E_UNIT / E_TIME_UNIT already say it; the lengths below cannot be read
    found = []
    params = block.get("params") or {}
    delayed = {(k.get("source"), k.get("target")) for k in layout.get("links") or []
               if isinstance(k, dict) and k.get("delay")} | set(getattr(drawn_links, "areas", ()))
    names = exit_names(block)
    rule = params.get("rule") if isinstance(params.get("rule"), dict) else {}
    specs = [str(e) for e in (rule.get("exits") or names)]
    counts = (rule.get("limit") or 0) > 0 or rule.get("kind") == "least_loaded"
    for index, port in enumerate(names):
        belt_name, fan, into = _receiving(name, index, outs, by_name, drawn_links, delayed)
        if fan is not None:
            found.append(problem(
                "W_ROUTE_RULE", f"{name}: exit {port!r} reaches its conveyor through "
                f"'{fan}', which branches, so the module frees as the load leaves it, not at "
                f"the next conveyor's eye", f"{at}/params/exits", block=name,
                hint="link each branch's conveyor to its own transfer exit, or branch after "
                     "the receiving conveyor",
                fields={"case": "transfer_fanout", "port": port, "fan": fan}))
        belt = by_name.get(belt_name) if belt_name else None
        if belt is None:
            continue
        belt_params = belt.get("params") or {}
        eyes = _eyes_seen(belt, into)
        stops = [s["at"] for s in belt_params.get("stops") or []
                 if isinstance(s, dict) and is_number(s.get("at"))]
        longest = _longest_item(belt, layout, units)
        if not eyes and any(_belt_base(belt, "length", a, units) < longest - 1e-9
                            for a in stops):
            found.append(problem(
                "W_ROUTE_RULE", f"{name}: exit {port!r} hands to '{belt_name}', which has no "
                f"eye and a stop nearer its entry than the longest item, so the module frees "
                f"while the load still overhangs it", f"{at}/params/exits", block=name,
                hint=f"give '{belt_name}' an eye before its first stop: the module waits for it",
                fields={"case": "transfer_short", "port": port, "belt": belt_name}))
        if counts and eyes and outs[index] == belt_name and index < len(specs):
            exit_name, named = split_exit(specs[index])
            if (named or _walk_end(belt_name, name, by_name, drawn_links)) == belt_name:
                found.append(problem(
                    "W_ROUTE_RULE", f"{name}: exit {port!r} counts a load under way only until "
                    f"the first eye of '{belt_name}', which is the handshake, so its limit or "
                    f"load always reads 0", f"{at}/params/rule", block=name,
                    hint=f"name a conveyor further on as the exit's target: "
                         f"\"{exit_name}:<conveyor>\" in the rule's exits",
                    fields={"case": "transfer_target", "port": port, "belt": belt_name}))
    needs = (is_number(params.get("entry_length")) and params["entry_length"] > 0) or (
        params.get("mechanism", "popup") == "popup"
        and any(isinstance(e, dict) and is_number(e.get("angle"))
                and turn_angle(e["angle"]) > 1e-9 for e in params.get("exits") or []))
    if needs and params.get("transfer_speed") in (None, 0, 0.0):
        speeds = _feed_speeds(name, by_name, drawn_links, units)
        if len(speeds) != 1:
            why = "no CONVEYOR feeds it" if not speeds else "its feeding conveyors differ in speed"
            found.append(problem(
                "E_CONVEYOR_PARAM", f"{name}: transfer_speed is 0, which takes the feeding "
                f"conveyor's speed, but {why}", f"{at}/params/transfer_speed", block=name,
                hint="set transfer_speed, the speed of the module's chains or rollers",
                fields={"case": "transfer_speed"}))
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
    drawn_links = _Drawn({name: list(targets) for name, targets in out_links.items()})
    for name, targets in belt_exit_targets(layout, by_name).items():
        drawn_links[name] = targets
    drawn_links.areas = area_pairs(link for link in layout.get("links") or []
                                   if isinstance(link, dict))
    chains = reverse_chains(layout)         # D1392
    chain_fed = set(chains.values())
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
                                hint="add a CREATE block: it is the only source of entities",
                                fields={"case": "missing"}))
    if not disposes:
        problems.append(problem("E_NO_DISPOSE", "model: no DISPOSE block", "/blocks",
                                hint="add a DISPOSE block and link the end of the flow to it",
                                fields={"case": "missing"}))

    problems.extend(_belt_port_problems(layout, by_name))
    for block in blocks:
        name = block["name"]
        btype = block.get("type", "").upper()
        outs = drawn_links.get(name, [])
        at = f"/blocks/{index_of[name]}"

        if btype == "CREATE" and not outs:
            problems.append(problem("E_CREATE_NO_OUTPUT", f"{name}: CREATE has no outgoing link",
                                    at, block=name,
                                    hint="link this CREATE to the first block of the flow",
                                    fields={"case": "none"}))
        if btype == "CREATE" and _is_endless_zero_gap(block.get("params", {})):
            problems.append(problem("E_CREATE_ZERO_INTERARRIVAL",
                                    f"{name}: interarrival time is a constant 0 with unlimited arrivals",
                                    f"{at}/params/dist_params", block=name,
                                    hint="set a positive interarrival time, or a max_arrivals "
                                         "for a burst at one instant",
                                    fields={"case": "zero"}))
        if btype == "DISPOSE" and outs:
            problems.append(problem("E_DISPOSE_HAS_OUTPUT",
                                    f"{name}: DISPOSE must have no outgoing link", at, block=name,
                                    hint="remove the outgoing link: DISPOSE ends the flow",
                                    fields={"case": "output"}))
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
        if btype == "CONVEYOR" and params.get("reversible") is True and name in chain_fed:
            # D1392: a later chain piece's start hands back to the piece before it
            problems.extend(_chain_member_problems(name, params, at, chains))
        elif btype == "CONVEYOR" and params.get("reversible") is True:
            # D1368: the start is an exit too, by the link `reverse_to` names
            allowed = 2
            problems.extend(_reverse_exit_problems(name, params, outs, at, layout, by_name,
                                                   drawn_links))
        elif btype == "CONVEYOR" and isinstance(params.get("diverts"), list) \
                and params.get("diverts"):
            # D851: divert k leaves by link k + 1, so each needs its link.
            allowed = 1 + len(params["diverts"])
            problems.extend(_divert_link_problems(name, params["diverts"], outs, by_name, at,
                                                  drawn_links.areas))
        if btype == "CONVEYOR_TRANSFER" and len(outs) != len(exit_names(block)):
            # each exit leaves by its own link, so a load is never routed to an unlinked exit
            exits = len(exit_names(block))
            problems.append(problem(
                "E_CONVEYOR_DIVERT", f"{name}: {exits} exits need {exits} links out, has "
                f"{len(outs)}", at, block=name,
                hint="link each exit once (source_port = the exit's port), or remove the exit",
                fields={"case": "transfer_links", "exits": exits, "links": len(outs)}))
        if btype == "CONVEYOR_TRANSFER":
            problems.extend(_transfer_rule_problems(name, block, outs, by_name, drawn_links,
                                                    at))
            problems.extend(_transfer_exit_problems(name, block, outs, by_name, drawn_links,
                                                    layout, at))
        if btype == "CONVEYOR" and isinstance(params.get("diverts"), list):
            for members in divert_groups(params["diverts"]).values():
                problems.extend(_group_route_warnings(name, members, outs, by_name,
                                                      drawn_links, at))
        if btype == "CONVEYOR" and isinstance(params.get("merges"), list):
            for index, merge in enumerate(params["merges"]):
                source = merge.get("from") if isinstance(merge, dict) else None
                if isinstance(source, str) and source.strip() \
                        and name not in drawn_links.get(source, []):
                    problems.append(problem(
                        "E_CONVEYOR_MERGE", f"{name}: merge {index} names '{source}', which "
                        f"does not link to this belt", f"{at}/params/merges/{index}/from",
                        block=name, hint="link that block to the belt, or name the one that "
                                         "does",
                        fields={"case": "a7_source", "index": index, "source": source}))
        if btype == "BATCH" and not str(params.get("carrier_type") or "").strip() and (
                str(params.get("member_time") or "").strip()
                or str(params.get("carrier_capacity") or "").strip()):
            problems.append(problem(
                "E_BATCH_MEMBER_TIME",
                f"{name}: member_time and carrier_capacity apply only to a carrier BATCH", at,
                block=name, hint="set carrier_type to the carrier's entity type (a truck), or "
                                 "clear member_time and carrier_capacity",
                fields={"case": "member"}))
        if not (spec is not None and spec.multi_output) and len(outs) > allowed:
            problems.append(problem(
                "E_TOO_MANY_OUTPUTS",
                f"{name}: {len(outs)} outgoing links, at most {allowed} allowed", at, block=name,
                fields={"n": len(outs), "allowed": allowed},
                hint="only DECIDE and LOGIC may branch: insert a DECIDE, or remove the extra links"))
        if leaves and len(outs) < 2:
            problems.append(problem(
                "E_QUEUE_NO_LEAVE_LINK",
                f"{name}: balk_if/renege_after needs a second outgoing link", at, block=name,
                hint="link this block to where entities that leave without service go "
                     "(a DISPOSE, or a RECORD before one); it is branch 1",
                fields={"case": "none"}))
        elif queueing and not leaves and len(outs) == 2:
            problems.append(problem(
                "W_QUEUE_LEAVE_LINK_UNUSED",
                f"{name}: second outgoing link but no balk_if or renege_after, so "
                f"nothing ever takes it", at, block=name,
                hint="set balk_if or renege_after, or remove the second link",
                fields={"case": "unused"}))

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
                    hint='for the free units use IDLE("name"); for the busy ones NR("name")',
                    fields={"case": "capacity"}))
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
                        hint=f"link {len(probs) - len(outs)} more target(s), or drop the extra probs",
                        fields={"case": "probs", "n": len(probs), "links": len(outs)}))
                total = sum(probs)
                if total > 1 + _PROB_SUM_EPS:
                    problems.append(problem(
                        "E_DECIDE_PROBS", f"{name}: probs sum to {total}, must be <= 1",
                        f"{at}/params/probs", block=name,
                        hint="probabilities are fractions of 1: scale them down",
                        fields={"case": "high", "total": total}))
                elif total < 1 - _PROB_SUM_EPS and len(outs) != len(probs) + 1:
                    problems.append(problem(
                        "E_DECIDE_PROBS",
                        f"{name}: probs sum to {total} < 1, needs one more link than probs (the ELSE)",
                        f"{at}/params/probs", block=name,
                        hint=mode_hint or "add one more outgoing link for the remaining "
                                          "probability, or make the probs sum to 1",
                        fields={"case": "low", "total": total}))
            elif mode == "Condition":
                conditions = params.get("conditions", [])
                if len(conditions) > len(outs):
                    problems.append(problem(
                        "E_DECIDE_BRANCHES",
                        f"{name}: {len(conditions)} conditions but only {len(outs)} outgoing links",
                        f"{at}/params/conditions", block=name,
                        hint="each condition needs its own outgoing link, in the same order",
                        fields={"case": "conditions", "n": len(conditions), "links": len(outs)}))
                elif conditions and len(conditions) == len(outs):
                    problems.append(problem(
                        "W_DECIDE_NO_ELSE",
                        f"{name}: no ELSE link, so an entity no condition takes stops the run",
                        f"{at}/params/conditions", block=name,
                        hint="add one more outgoing link, last, for the entities no condition takes",
                        fields={"case": "else"}))
            elif mode == "Rule":
                # A7 (D1359): a RouteRule whose exits are the outgoing links
                spec = rule_spec(params.get("rule"), list(outs))
                try:
                    validate_rule(spec)
                except ValueError as exc:
                    problems.append(problem(
                        "E_DECIDE_RULE", f"{name}: rule: {exc}", f"{at}/params/rule",
                        block=name, hint="kind is one of fixed, sequential, cyclic, round_robin, "
                                         "least_loaded, condition, attribute, item",
                        fields={"case": "rule"}))
                else:
                    if len(spec["exits"]) != len(outs):
                        problems.append(problem(
                            "E_DECIDE_RULE", f"{name}: rule has {len(spec['exits'])} exits but "
                            f"{len(outs)} outgoing links", f"{at}/params/rule/exits", block=name,
                            hint="give one exit name per outgoing link, in link order, or none",
                            fields={"case": "exits", "n": len(spec["exits"]),
                                    "links": len(outs)}))
                    else:
                        problems.extend(_route_target_problems(
                            name, spec, list(outs), by_name, drawn_links, "E_DECIDE_RULE",
                            f"{at}/params/rule"))
                        problems.extend(_route_warnings(name, spec, list(outs), by_name,
                                                        drawn_links, f"{at}/params/rule"))
            elif mode == "Policy":
                # Actions = outgoing links, whatever their count -- no probs/
                # conditions to match against (ROADMAP 3.7 A).
                if not params.get("policy"):
                    problems.append(problem(
                        "E_DECIDE_POLICY_NAME", f"{name}: mode \"Policy\" needs a non-empty policy",
                        f"{at}/params/policy", block=name,
                        hint="name a decision point, e.g. \"routing\"; several DECIDE blocks may share one",
                        fields={"case": "name"}))
                fallback = params.get("fallback", "first")
                if fallback not in FALLBACK_MODES:
                    problems.append(problem(
                        "E_DECIDE_FALLBACK", f"{name}: unknown fallback '{fallback}'",
                        f"{at}/params/fallback", block=name,
                        hint=f"use one of: {', '.join(FALLBACK_MODES)}",
                        fields={"case": "unknown", "fallback": fallback}))
                elif fallback == "shortest" and len(params.get("queue_exprs", [])) != len(outs):
                    problems.append(problem(
                        "E_DECIDE_QUEUE_EXPRS",
                        f"{name}: fallback \"shortest\" needs {len(outs)} queue_exprs "
                        f"(one per outgoing link), has {len(params.get('queue_exprs', []))}",
                        f"{at}/params/queue_exprs", block=name,
                        hint="give one expression per outgoing link, in branch order",
                        fields={"case": "count", "links": len(outs), "n": len(params.get("queue_exprs", []))}))

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
                at, block=name, hint="add a DISPOSE block or link this block to one",
                fields={"case": "end"}))

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
                                "(a truck) leave by a second link",
                           fields={"case": "carrier", "carrier": carrier}))
    if (params.get("release_order", "pattern") or "pattern") not in ("pattern", "fifo", "lifo"):
        out.append(problem("E_SEPARATE_RELEASE_ORDER", f"{name}: release_order "
                           f"{params.get('release_order')!r} is not \"pattern\", \"fifo\" or "
                           f"\"lifo\"",
                           f"{at}/params/release_order", block=name,
                           hint="\"pattern\": a pattern pallet top layer first; \"fifo\": the "
                                "order they were loaded; \"lifo\": last loaded first",
                           fields={"case": "order", "order": params.get("release_order")}))
    if carrier == "keep" and n_outs != 2:
        out.append(problem("E_SEPARATE_CARRIER_LINK", f"{name}: a SEPARATE that keeps the "
                           f"carrier needs exactly 2 outgoing links, has {n_outs}", at, block=name,
                           hint="link 0 takes the members, link 1 the carrier once it is empty",
                           fields={"case": "links", "n": n_outs}))
    if carrier == "dispose" and str(params.get("member_time") or "").strip():
        out.append(problem("E_SEPARATE_MEMBER_TIME", f"{name}: member_time applies only when "
                           f"the carrier is kept", f"{at}/params/member_time", block=name,
                           hint="set carrier to \"keep\", or clear member_time",
                           fields={"case": "member"}))
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
