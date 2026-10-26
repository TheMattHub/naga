"""Helpers shared by more than one block module (ARCHITECTURE 10.3 rule 2:
one block type per file; this module holds what would otherwise be
duplicated across them)."""
from __future__ import annotations

from nagasim.core import tending
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.movers import free_held
from nagasim.core.sim_classes import Entity

__all__ = ["evaluate_expression", "entity_scope", "compiled_closure", "make_batch_entity", "validated_probs", "SEPARATE_EXCLUDED_FIELDS",
           "DUPLICATE_EXCLUDED_FIELDS", "absorb_member", "member_time_of"]


def entity_scope(block, entity):
    """The scope `evaluate_expression` builds, made once so a block with several
    expressions (ASSIGN, DECIDE, RECORD) shares it across them for one entity."""
    return ExpressionScope(entity=entity, ctx=block.ctx, rng=block.rng,
                           locals=getattr(block, "locals", None))


def compiled_closure(expr):
    """The compiled closure for *expr*, or None when it does not compile. The
    caller then falls back to `evaluate_expression`, which raises the original
    error at run time, for that entity, exactly as before the plan existed."""
    try:
        return compile_expression(expr).closure()
    except (ExpressionError, ValueError):
        return None


def evaluate_expression(block, expr, entity=None, extra=None):
    """Evaluate a model-file expression in *block*'s scope (DEVLOG #6, #53).

    The expression engine is an AST allow-list, not eval: see
    `core/expressions.py`. Sampling functions draw from the block's own rng, so
    a condition that calls RAND() stays reproducible per seed.

    *extra* (PHASE4 A2) adds names visible to THIS expression only, merged over
    the block's submodel locals -- a PROCESS setup expression sees
    `previous_key` and `key` that way.
    """
    locals_ = getattr(block, "locals", None)
    if extra:
        locals_ = {**(locals_ or {}), **extra}
    return compile_expression(expr).evaluate(
        ExpressionScope(entity=entity, ctx=block.ctx, rng=block.rng, locals=locals_))


def make_batch_entity(block, members, e_type):
    """Create the batch entity that represents *members* from now on.

    Entity accounting (DEVLOG #29): the members leave `live_entities` -- the
    batch stands for them in WIP -- and carry `in_batch = <batch id>` until a
    Separate splits the batch again. Shared by Batch and Match.
    """
    batch = Entity(next(block.ctx.entity_ids))
    batch.e_type = e_type
    batch.creation_time = block.env.now
    batch.batch_elems = list(members)
    block.ctx.all_entities.append(batch)
    block.ctx.live_entities.append(batch)

    # Permanent batching (ROADMAP 2.2): the members' cost moves into the batch
    # entity, so it is never lost while they are absorbed (and never double
    # counted -- a member with in_batch set is excluded from every KPI sum).
    batch.cost = sum(getattr(m, "cost", 0.0) for m in members)
    batch._auto_cost = sum(getattr(m, "_auto_cost", 0.0) for m in members)
    batch._holding_cost = sum(getattr(m, "_holding_cost", 0.0) for m in members)

    for member in members:
        _absorbed(block, member, batch)

    block.emit("entity_created", entity=batch)
    return batch


def _absorbed(block, member, batch, **traced):
    """The member half of batching: *member* stops being a part of its own."""
    member.in_batch = batch.id
    tending.cobot_of(block, member)      # D169: a cobot staying with it has nothing to wait for
    free_held(block, member)             # D837: nor a vehicle a TRANSPORT kept for it
    if member in block.ctx.live_entities:
        block.ctx.live_entities.remove(member)
    # An absorbed member never passes Dispose: give its planned stations
    # back now, or every job planned after it waits at the gate (#206).
    if block.ctx.plan_gates is not None:
        block.ctx.plan_gates.drop(member)
    # #319: playback has to hear that the member is no longer a part of its
    # own in this block, or it counts it here for the rest of the run.
    block.emit("entity_absorbed", entity=member, **traced)


def absorb_member(block, member, carrier):
    """A carrier BATCH (D819) loads *member* onto an existing *carrier*: the same
    accounting as `make_batch_entity`, but the carrier keeps its identity and no
    entity is created. The member's cost moves into the carrier's totals and stays
    frozen on the member, so a keep-mode SEPARATE can give back exactly that."""
    carrier.batch_elems.append(member)
    carrier.cost += getattr(member, "cost", 0.0)
    carrier._auto_cost += getattr(member, "_auto_cost", 0.0)
    carrier._holding_cost += getattr(member, "_holding_cost", 0.0)
    # No `entity_created` follows to say which batch it went into (D461), so the
    # absorption names the carrier itself: `value` is its id (ids start at 1).
    _absorbed(block, member, carrier, value=float(carrier.id))


def member_time_of(block, text, factor, entity, label="member_time") -> float:
    """A carrier's per-member time (D820): *text* evaluated in the member's scope,
    in base units, never negative. Empty text is 0: everything in one instant."""
    if not text:
        return 0.0
    try:
        value = float(compile_expression(text).evaluate(block.expression_scope(entity)))
    except (ExpressionError, TypeError, ValueError) as exc:
        raise ExpressionError(f"Block '{block.name}': {label}: {exc}") from None
    return max(value * factor, 0.0)


def validated_probs(probs, block_name):
    """Branch probabilities must not over-commit the unit interval (DEVLOG #26)."""
    total = sum(float(p) for p in probs)
    if total > 1 + 1e-9:
        raise ValueError(f"Decide block '{block_name}': branch probabilities sum to {total:.4f} > 1.")
    return probs


# Fields that must stay unique per entity, never copied onto a duplicate (DEVLOG #3);
# a staying cobot (D169) belongs to one part, like a hold.
DUPLICATE_EXCLUDED_FIELDS = {"_id", "name", "req_params", tending._COBOT}

# Identity/lifecycle fields never copied from a batch parent onto a child (DEVLOG #28):
# doing so used to duplicate the parent's id and self-reference batch_elems.
# Cost fields are excluded too (ROADMAP 2.2): separate.py splits them evenly
# across children itself, rather than blanket-copying the parent's total onto
# every child.
SEPARATE_EXCLUDED_FIELDS = {"_id", "name", "batch_elems", "_creation_time", "_deletion_time",
                            "_throughput_time", "req_params", "cost", "_auto_cost", "_holding_cost",
                            tending._COBOT}
