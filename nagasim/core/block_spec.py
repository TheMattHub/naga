"""Value types for a block's registry entry: ParamSpec, BlockSpec, DIST_PARAMS.

Kept in their own leaf module (no dependency on `blocks/` or `block_registry`)
so that every block module in `nagasim/core/blocks/` can build its own
module-level SPEC without a circular import against `block_registry`, which in
turn imports the assembled blocks to build BLOCK_REGISTRY. `block_registry.py`
re-exports these three names, so `nagasim.core.block_registry.ParamSpec` (etc.)
keeps working for every existing importer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from nagasim.core.timeunits import UNIT_QUANTITIES

__all__ = ["ParamSpec", "BlockSpec", "DIST_PARAMS", "QUEUE_PARAMS", "HOLD_SPACE_PARAMS",
           "WORKER_PARAMS", "WORKER_BLOCKED_MODES", "WORKER_WHEN_BLOCKED", "KINDS"]

KINDS = ("number", "integer", "distribution", "expression", "table", "text", "bool", "date",
         "choice")
_TYPE_KINDS = {bool: ("bool",), int: ("integer",), float: ("number",), str: ("text",),
               list: ("table",), dict: ("table",)}


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: type            # int, float, str, bool, list, dict
    default: object
    required: bool = False
    description: str = ""
    # Units spec (D159): what the value measures (timeunits.QUANTITIES; None =
    # not declared, which the registry test refuses for a built-in block), the
    # kinds it accepts (empty = derived from `type`), numeric bounds, and the
    # key holding its unit (None = "<name>_unit"; "" = fixed in the engine
    # base; a dotted "uptime.time_unit" lives inside the value's own dict).
    quantity: str | None = None
    kinds: tuple = ()
    min: float | None = None
    max: float | None = None
    unit_key: str | None = None

    @property
    def field_kinds(self) -> tuple:
        return self.kinds or _TYPE_KINDS.get(self.type, ("text",))

    @property
    def unit_field(self) -> str | None:
        if self.quantity not in UNIT_QUANTITIES:
            return None
        return f"{self.name}_unit" if self.unit_key is None else self.unit_key


@dataclass(frozen=True)
class BlockSpec:
    type_name: str        # "CREATE", ...
    cls: type              # the BaseProcess subclass
    params: tuple          # tuple[ParamSpec, ...]
    factory: Callable      # (env, block) -> BaseProcess
    # Optional palette/canvas metadata for a plugin block (ROADMAP 2.4 D):
    # a nagasim.plugins.BlockUi, read by ui/style.py and ui/palette.py. Kept
    # as a plain `object` (not imported here) so this leaf module still has
    # no dependency on anything UI-related; None (every built-in block) falls
    # back to ui/style.py's existing per-type lookup.
    ui: object = None
    # True for a block whose branch index is a routing decision made inside
    # self_process (DECIDE, LOGIC, and a plugin block with the same shape,
    # e.g. an inspection block routing pass/fail) -- schema.validate_graph_structured
    # reads this instead of a hardcoded type-name tuple (ROADMAP 2.4 B), so a
    # plugin block never needs an engine-side change to have more than one
    # outgoing link.
    multi_output: bool = False
    # ARCH-REVIEW B3: the per-type facts ui/style.py used to keep in its own
    # BLOCK_FAMILY/_SHAPES tables, now declared once with the type itself.
    # `family` picks the palette fill (ui/style.FAMILY_FILLS); "" (a plugin
    # that only sets `ui` above, or forgets both) falls back to "container"
    # there rather than raising a KeyError. `shape` is the outline
    # (ui/style.block_path); "" means "rect", the plain block outline.
    family: str = ""
    shape: str = ""

    # ARCH-REVIEW E2: behaviour flags a few call sites used to keep as their
    # own hand-maintained per-type-name tuple (drifting whenever a block was
    # added or its behaviour changed). Each is now declared once with the
    # type itself; nagasim.core.block_registry derives the equivalent tuple
    # from BLOCK_REGISTRY so there is one source of truth. Every flag
    # defaults to False, so a plugin BlockSpec that doesn't set it behaves
    # exactly as before (never crashes, never opts in by accident).
    duration: bool = False       # "distribution"/"dist_params" is sampled as a SimPy timeout
    holding: bool = False        # can hold an entity handed to it out of nowhere (initial_state)
    service: bool = False        # of the holding types, in_service/remaining mean something
    transparent: bool = False    # an entity crosses it without leaving the machine (blocking)
    queueing: bool = False       # queues for a resource, carries queue behaviours (seizing)
    uses_resource: bool = False  # has a resource_name param that follows a resource rename


# Shared by every delay/timeout-bearing block. `time_unit` "base" (or absent)
# means the model's unit; migration (v28) writes it out.
DIST_PARAMS = (
    ParamSpec("distribution", str, "Constant",
              description="Name of a registered distribution (see the distribution reference).",
              quantity="none", kinds=("choice",)),
    ParamSpec("dist_params", dict, {"value": 0.0},
              description="Parameters for `distribution`, by name (e.g. {\"rate\": 0.5} for Exponential).",
              quantity="time", kinds=("number", "distribution", "expression"), unit_key="time_unit"),
    ParamSpec("time_unit", str, "base",
              description="Unit the sampled duration is expressed in: \"base\" (the model's own unit, no "
                          "conversion) or one of seconds/minutes/hours/days.",
              quantity="none", kinds=("choice",)),
)

# Shared by the two blocks that queue for a resource (SEIZE, PROCESS), PHASE4
# A4. A block with either option set needs a SECOND outgoing link: index 1 is
# where an entity that left without service goes (ARCHITECTURE D54).
# Blocking BEFORE service (PHASE5 O4), shared by SEIZE and PROCESS.
HOLD_SPACE_PARAMS = (
    ParamSpec("hold_until_space", bool, False,
              description="Blocking BEFORE service: after seizing, do not start the "
                          "job until the downstream space has a free place, and reserve it for "
                          "this part. The station waits idle (reported as Blocked %). Needs a "
                          "downstream BUFFER with capacity > 0 or a SEIZE/PROCESS with "
                          "queue_capacity > 0. Off (the default) is blocking AFTER service.",
              quantity="none"),
    ParamSpec("space_of", str, "",
              description="Which block's space `hold_until_space` reserves in; empty = the target "
                          "of this block's first outgoing link. The part must reach that block.",
              quantity="none"),
)

# A worker walking to the station (PHASE6 W3-M3, ARCHITECTURE D114), shared by
# SEIZE and PROCESS. `node` also makes the block a place (network.block_nodes).
WORKER_PARAMS = (
    ParamSpec("worker_fleet", str, "",
              description="A fleet with kind \"worker\". After this block's own "
                          "resource (if any) is seized, a worker is requested, walks EMPTY on the "
                          "network to `node`, and is held for the service. Empty = no worker.",
              quantity="none"),
    ParamSpec("worker_skill", str, "",
              description="Only a worker whose skills contain this name may serve; empty = any "
                          "worker of the fleet.",
              quantity="none"),
    ParamSpec("node", str, "",
              description="Network node this block sits on; required with worker_fleet (where "
                          "the worker walks to).",
              quantity="none"),
)

# What a worker does while its station is blocked after service (D171): one
# param, the same on PROCESS (its `worker_fleet`) and on RELEASE (a
# `resource_name` that is a worker fleet).
WORKER_BLOCKED_MODES = ("leaves", "stays")
WORKER_WHEN_BLOCKED = ParamSpec(
    "worker_when_blocked", str, WORKER_BLOCKED_MODES[0],
    description="What the worker does while this station holds a finished part that "
                "nothing downstream has room for. \"leaves\" (the default): the worker is "
                "free when the work ends. \"stays\": the worker waits with the part and is "
                "free when it is accepted downstream. The same either way where nothing "
                "downstream is finite.",
    quantity="none", kinds=("choice",))

QUEUE_PARAMS = (
    ParamSpec("balk_if", str, "",
              description="Condition evaluated on arrival; when true the entity does not join the "
                          "queue and leaves by the second outgoing link, e.g. \"queue_length >= 5\". "
                          "`queue_length` is the queue it would have joined. Empty = never balk.",
              quantity="none", kinds=("expression",)),
    ParamSpec("renege_after", str, "",
              description="How long the entity waits before giving up, e.g. \"EXPO(15)\" or "
                          "\"entity.patience\"; it then leaves by the second outgoing link. A grant "
                          "in the same instant wins. Empty = wait for ever.",
              quantity="time", kinds=("number", "expression"), min=0,
              unit_key="renege_time_unit"),
    ParamSpec("renege_time_unit", str, "base",
              description="Unit `renege_after` is expressed in: \"base\" (the model's own unit) or "
                          "one of seconds/minutes/hours/days.",
              quantity="none", kinds=("choice",)),
)
