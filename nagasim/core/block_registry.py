"""Single source of truth for constructible block types: class, factory, and
param schema. BLOCK_REGISTRY is assembled from each block module's own SPEC
under nagasim/core/blocks/ (ARCHITECTURE 10.3 rule 2, D11)."""

from nagasim.core.block_spec import BlockSpec, DIST_PARAMS, ParamSpec  # re-exported
from nagasim.core import blocks

import logging

log = logging.getLogger(__name__)

__all__ = ["ParamSpec", "BlockSpec", "DIST_PARAMS", "BLOCK_REGISTRY", "validate_params",
           "duration_types", "holding_types", "service_types", "transparent_types",
           "queueing_types", "resource_block_types"]


BLOCK_REGISTRY: dict[str, BlockSpec] = {spec.type_name: spec for spec in blocks.ALL_SPECS}


def _types_where(flag: str) -> tuple[str, ...]:
    """Type names whose BlockSpec has `flag` set (ARCH-REVIEW E2): the single
    source of truth every hand-kept per-type-name tuple used to duplicate."""
    return tuple(name for name, spec in BLOCK_REGISTRY.items() if getattr(spec, flag))


def duration_types() -> tuple[str, ...]:
    """Block types whose "distribution" param is sampled as a SimPy timeout."""
    return _types_where("duration")


def holding_types() -> tuple[str, ...]:
    """Block types that can hold an entity handed to them out of nowhere."""
    return _types_where("holding")


def service_types() -> tuple[str, ...]:
    """Of holding_types(), the ones where in_service/remaining mean anything."""
    return _types_where("service")


def transparent_types() -> tuple[str, ...]:
    """Block types an entity crosses without ever leaving the machine that
    holds it."""
    return _types_where("transparent")


def queueing_types() -> tuple[str, ...]:
    """The block types that queue for a resource and carry queue behaviours."""
    return _types_where("queueing")


def resource_block_types() -> tuple[str, ...]:
    """Block types whose `resource_name` param follows a resource rename."""
    return _types_where("uses_resource")

def _type_ok(value, expected):
    """int is accepted where float is declared; bool is accepted only where bool
    is declared (bool is an int subclass in Python, so it needs its own check)."""
    if expected is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if expected is float and isinstance(value, int):
        return True
    return isinstance(value, expected)


def validate_params(type_name: str, params: dict) -> list[str]:
    """Check a param dict against the registered schema for `type_name`.
    Returns a list of problem strings; empty means OK. Never raises."""
    try:
        spec = BLOCK_REGISTRY.get(type_name)
        if spec is None:
            return [f"unknown block type: {type_name}"]

        problems = []
        schema = {p.name: p for p in spec.params}

        for key, value in params.items():
            if key not in schema:
                problems.append(f"unknown key: {key}")
                continue
            expected = schema[key].type
            if not _type_ok(value, expected):
                problems.append(
                    f"wrong type for key '{key}': expected {expected.__name__}, "
                    f"got {type(value).__name__}"
                )

        for p in spec.params:
            if p.required and p.name not in params:
                problems.append(f"missing required key: {p.name}")

        return problems
    except Exception as e:  # validate_params must never raise (load-time check only)
        return [f"validation error: {e}"]
