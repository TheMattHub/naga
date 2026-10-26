import logging

from nagasim.core import tending
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import SEPARATE_EXCLUDED_FIELDS, member_time_of
from nagasim.core.movers import free_held
from nagasim.core.quantities import field_factor, param
from nagasim.core.stats import account_and_retain

log = logging.getLogger(__name__)

__all__ = ["Separate", "SPEC"]


class Separate(BaseProcess):
    """Splits a batch, pallet or lot back into its individual parts, orders or customers, optionally copying the batch's attributes to each.

    Parameters
    ----------
    mode : {'retain','all','specific'}
        - 'retain'    children keep their own attributes
        - 'all'       copy *all* parent attributes to each child
        - 'specific'  copy only the names listed in *attrs*
    attrs : list[str] | str | None
        Comma-separated string or list used when *mode* == 'specific'.
    carrier : {'dispose','keep'}
        'keep': the parent is a CARRIER (a truck) that lives on. Its members
        leave one at a time on exit 0, each after its own `member_time`, and the
        carrier leaves on exit 1 once it is empty.
    """
    # default fields. used by the UI
    MODES = (
        "Retain original entity values",
        "Take all representative values",
        "Take specific representative values",
    )
    CARRIER_MODES = ("dispose", "keep")
    RELEASE_ORDERS = ("fifo", "lifo")

    CURATED_ATTRS_MAP = {
        "Batch ID": "_id",
        "Batch Name": "name",
        "Batch Type": "e_type",
        "Batch Creation": "creation_time",
        "Batch Deletion": "deletion_time",
        "Batch Throughput Time": "throughput_time",
        "Batch Elements": "batch_elems"
    }

    def __init__(self, env, block, *,
                 next_process=None,
                 mode="Retain original entity values",
                 attrs=None):
        super().__init__(env, block, next_process)
        self.mode = mode

        # normalise attrs -> list[str]
        if attrs is None:
            attrs = []
        if isinstance(attrs, str):
            attrs = [a.strip() for a in attrs.split(",") if a.strip()]
        self.attrs = attrs
        self.carrier = "dispose"
        self.member_time = ""
        self.member_time_unit = "base"
        self.release_order = "fifo"
        self._time_factor = 1.0

        # and our full "always copy" list
        block.curated_attrs_map = list(self.CURATED_ATTRS_MAP.keys())  # visible to UI

    def self_process(self, parent):          # parent == batch entity
        # --- validation -------------------------------------------------
        if not self.next_process:
            raise RuntimeError("SEPARATE block has no outgoing connector.")
        if not getattr(parent, "batch_elems", None):
            raise ValueError("SEPARATE received an entity that is not a batch")

        #Compute and store info
        parent.deletion_time = self.env.now
        parent.throughput_time = parent.deletion_time - parent.creation_time
        children = self._ordered(parent.batch_elems)

        # --- parent handling  (always destroy) --------------------------
        self.ctx.live_entities.remove(parent)
        self.emit("entity_absorbed", entity=parent)      # #319: the batch itself is gone
        tending.cobot_of(self, parent)       # D169: a cobot staying with the batch is freed
        free_held(self, parent)              # D837: and a vehicle a TRANSPORT kept for it

        # Split the parent's cost evenly across children (ROADMAP 2.2), then
        # zero it on the parent: it stays in entity_summary as one completed
        # BATCH-type entity, but the cost itself now lives only in the
        # children, so it is never counted twice.
        n = len(children)
        cost_share = parent.cost / n if n else 0.0
        auto_share = parent._auto_cost / n if n else 0.0
        holding_share = parent._holding_cost / n if n else 0.0
        parent.cost = parent._auto_cost = parent._holding_cost = 0.0

        # Separate finalizes deletion_time itself instead of routing the
        # parent through Dispose (ROADMAP 2.5, DEVLOG #62): account for it
        # here, after the cost split above, so a report sees the same
        # already-zeroed parent cost it always has.
        account_and_retain(self.ctx, parent)

        payload = self._payload(parent)

        # --- forward every child ---------------------------------------
        for child in children:
            child.cost = cost_share
            child._auto_cost = auto_share
            child._holding_cost = holding_share
            self._restore(child, parent, payload, children, parent.deletion_time)
            self.forward(child, 0)

    def _ordered(self, members):
        return list(reversed(members)) if self.release_order == "lifo" else list(members)

    def _payload(self, parent):
        """The parent's values each child takes, by `mode`; None = retain."""
        if self.mode == "Take all representative values":
            # Identity/lifecycle fields never copy onto a child (DEVLOG #28):
            # doing so used to duplicate the parent's id and self-reference batch_elems.
            return {k: v for k, v in parent.__dict__.items() if k not in SEPARATE_EXCLUDED_FIELDS}
        if self.mode == "Take specific representative values":
            payload = {}
            for label in self.attrs:
                real_attr = self.CURATED_ATTRS_MAP.get(label)
                if real_attr and hasattr(parent, real_attr):
                    payload[real_attr] = getattr(parent, real_attr)
                else:
                    log.warning(f"Skipping unknown or missing attr '{label}' -> '{real_attr}'")
            return payload
        return None  # 'retain'

    def _restore(self, child, parent, payload, children, left_at):
        """*child* is a part of its own again: the children left live_entities
        when they were batched, and splitting puts them back in WIP (DEVLOG #29)."""
        child.in_batch = None
        if child not in self.ctx.live_entities:
            self.ctx.live_entities.append(child)
        if payload:
            for attr, val in payload.items():
                try:
                    setattr(child, attr, val)
                except Exception as e:
                    log.warning(f"Could not set '{attr}' on child entity: {e}")
        else:  # separate info collection when retaining original values
            child.batch_info = {"batch_id": parent.id,
                                "batch_name": parent.name,
                                "batch_type": parent.e_type,
                                "batch_creation": parent.creation_time,
                                "batch_deletion": left_at,
                                "batch_throughput_time": left_at - parent.creation_time,
                                "batch_size": len(children),
                                "batch_elems": children}

    def _unload(self, carrier):
        """Keep mode (D819, D820): the members come off one at a time and the
        carrier lives on. A member keeps the cost it brought in -- frozen while it
        was aboard -- and exactly that leaves the carrier with it (D821), so the
        carrier ends with only what it accrued itself."""
        if len(self.next_process) < 2:
            raise RuntimeError(f"SEPARATE block '{self.name}' keeps the carrier but has no "
                               f"second outgoing link for it.")
        members = self._ordered(getattr(carrier, "batch_elems", None) or [])
        payload = self._payload(carrier)
        self.active_entity = carrier     # the truck is here while it unloads (review R3)
        for child in members:
            wait = member_time_of(self, self.member_time, self._time_factor, child)
            if wait > 0:
                yield self.env.timeout(wait)
            carrier.batch_elems.remove(child)
            carrier.cost -= child.cost
            carrier._auto_cost -= child._auto_cost
            carrier._holding_cost -= child._holding_cost
            self._restore(child, carrier, payload, members, self.env.now)
            self.forward(child, 0)
        self.remove_active_entity(carrier)
        self.forward(carrier, 1)

    def self_run(self, entity):
        if self.carrier == "keep":
            return self.env.process(self._unload(entity))
        self.self_process(entity)

    def get_params_dict(self):
        """Return dict that will be persisted in Block."""
        # we persist the _user_ picks only:
        return {"mode": self.mode, "attrs": self.attrs, "carrier": self.carrier,
                "member_time": self.member_time, "member_time_unit": self.member_time_unit,
                "release_order": self.release_order}

    def set_params_from_dict(self, param_dict):
        self.mode = param_dict.get("mode", "Retain original entity values")
        self.attrs = param_dict.get("attrs", [])
        self.carrier = param_dict.get("carrier", "dispose") or "dispose"
        self.member_time = str(param_dict.get("member_time", "") or "").strip()
        self.member_time_unit = param_dict.get("member_time_unit", "base")
        self.release_order = param_dict.get("release_order", "fifo") or "fifo"
        if self.carrier not in self.CARRIER_MODES:
            raise ValueError(f"SEPARATE block '{self.name}': carrier must be one of "
                             f"{self.CARRIER_MODES}, not {self.carrier!r}")
        if self.release_order not in self.RELEASE_ORDERS:
            raise ValueError(f"SEPARATE block '{self.name}': release_order must be one of "
                             f"{self.RELEASE_ORDERS}, not {self.release_order!r}")
        self._time_factor = field_factor(param(SPEC, "member_time"), param_dict, self.ctx.units,
                                         SPEC.params)


SPEC = BlockSpec(
    type_name="SEPARATE", cls=Separate,
    params=(
        ParamSpec("mode", str, "Retain original entity values",
                  description="How children get their attributes: retain their own values, take all of the "
                              "parent's, or take only the ones listed in attrs.", quantity="none"),
        ParamSpec("attrs", list, [], description="Attribute labels to copy from the parent, used when mode is \"specific\".", quantity="none"),
        ParamSpec("carrier", str, "dispose",
                  description="\"dispose\": the batch ends here and its parts leave together. \"keep\": "
                              "the batch is a carrier (a truck) that lives on -- its members leave one "
                              "at a time by the first link, each after member_time, and the carrier "
                              "leaves by the SECOND link once it is empty.", quantity="none",
                  kinds=("choice",)),
        ParamSpec("member_time", str, "",
                  description="Keep mode only: time for one member to come off, e.g. \"2\" or "
                              "\"TRIA(1, 2, 3)\", evaluated for each member in its own scope when its "
                              "turn starts. Empty = every member leaves in the same instant.",
                  quantity="time", kinds=("number", "expression"), min=0,
                  unit_key="member_time_unit"),
        ParamSpec("member_time_unit", str, "base",
                  description="Unit `member_time` is expressed in: \"base\" (the model's own unit) or "
                              "one of seconds/minutes/hours/days.", quantity="none", kinds=("choice",)),
        ParamSpec("release_order", str, "fifo",
                  description="Order the members leave in: \"fifo\" (the order they were loaded) or "
                              "\"lifo\" (last loaded first off, a rear-door truck).", quantity="none",
                  kinds=("choice",)),
    ),
    factory=lambda env, block: Separate(env, block),
    family="group",
    holding=True,
)
