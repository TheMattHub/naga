"""Block implementations, one module per type (ARCHITECTURE 10.3 rule 2).

Re-exports every block class, plus ALL_SPECS -- the list of BlockSpecs that
`nagasim.core.block_registry.BLOCK_REGISTRY` is assembled from. This package
has no dependency on block_registry (see `nagasim.core.block_spec` for why),
so block_registry can safely import it.
"""
from nagasim.core.blocks.create import Create, SPEC as _CREATE_SPEC
from nagasim.core.blocks.dispose import Dispose, SPEC as _DISPOSE_SPEC
from nagasim.core.blocks.assign import Assign, SPEC as _ASSIGN_SPEC
from nagasim.core.blocks.decide import Decide, SPEC as _DECIDE_SPEC
from nagasim.core.blocks.seize import Seize, SPEC as _SEIZE_SPEC
from nagasim.core.blocks.delay import Delay, SPEC as _DELAY_SPEC
from nagasim.core.blocks.release import Release, SPEC as _RELEASE_SPEC
from nagasim.core.blocks.batch import Batch, SPEC as _BATCH_SPEC
from nagasim.core.blocks.buffer import Buffer, SPEC as _BUFFER_SPEC
from nagasim.core.blocks.match import Match, SPEC as _MATCH_SPEC
from nagasim.core.blocks.separate import Separate, SPEC as _SEPARATE_SPEC
from nagasim.core.blocks.duplicate import Duplicate, SPEC as _DUPLICATE_SPEC
from nagasim.core.blocks.hold import Hold, SPEC as _HOLD_SPEC
from nagasim.core.blocks.signal import Signal, SPEC as _SIGNAL_SPEC
from nagasim.core.blocks.send import Send, SPEC as _SEND_SPEC
from nagasim.core.blocks.logic import Logic, SPEC as _LOGIC_SPEC
from nagasim.core.blocks.process import Process, SPEC as _PROCESS_SPEC
from nagasim.core.blocks.motion import Motion, SPEC as _MOTION_SPEC
from nagasim.core.blocks.record import Record, SPEC as _RECORD_SPEC
from nagasim.core.blocks.station import Station, SPEC as _STATION_SPEC
from nagasim.core.blocks.route import Route, SPEC as _ROUTE_SPEC
from nagasim.core.blocks.transfer import Transfer, SPEC as _TRANSFER_SPEC
from nagasim.core.blocks.charge import Charge, SPEC as _CHARGE_SPEC
from nagasim.core.blocks.transport import Transport, SPEC as _TRANSPORT_SPEC
from nagasim.core.blocks.conveyor import Conveyor, SPEC as _CONVEYOR_SPEC
from nagasim.core.blocks.submodel import (
    SubmodelBlock, ENTRY_SPEC as _SUB_ENTRY_SPEC, EXIT_SPEC as _SUB_EXIT_SPEC,
    INSTANCE_SPEC as _SUBMODEL_SPEC,
)

ALL_SPECS = [
    _CREATE_SPEC, _DISPOSE_SPEC, _ASSIGN_SPEC, _DECIDE_SPEC, _SEIZE_SPEC,
    _DELAY_SPEC, _RELEASE_SPEC, _BATCH_SPEC, _MATCH_SPEC, _SEPARATE_SPEC,
    _DUPLICATE_SPEC, _HOLD_SPEC, _SIGNAL_SPEC, _LOGIC_SPEC, _PROCESS_SPEC,
    _MOTION_SPEC,
    _RECORD_SPEC, _STATION_SPEC, _ROUTE_SPEC, _TRANSFER_SPEC, _CHARGE_SPEC,
    _TRANSPORT_SPEC, _CONVEYOR_SPEC, _BUFFER_SPEC, _SEND_SPEC,
    _SUBMODEL_SPEC, _SUB_ENTRY_SPEC, _SUB_EXIT_SPEC,
]

__all__ = [
    "Create", "Dispose", "Assign", "Decide", "Seize", "Delay", "Release",
    "Batch", "Buffer", "Match", "Separate", "Duplicate", "Hold", "Signal", "Send", "Logic", "Process",
    "Motion",
    "Record", "Station", "Route", "Transfer", "Charge", "Transport", "Conveyor",
    "SubmodelBlock", "ALL_SPECS",
]
