import logging

from nagasim.core import tending
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import SEPARATE_EXCLUDED_FIELDS
from nagasim.core.stats import account_and_retain

log = logging.getLogger(__name__)

__all__ = ["Separate", "SPEC"]


class Separate(BaseProcess):
    """
    Split a batch entity into its children.

    Parameters
    ----------
    mode : {'retain','all','specific'}
        - 'retain'    children keep their own attributes
        - 'all'       copy *all* parent attributes to each child
        - 'specific'  copy only the names listed in *attrs*
    attrs : list[str] | str | None
        Comma-separated string or list used when *mode* == 'specific'.
    """
    # default fields. used by the UI
    MODES = (
        "Retain original entity values",
        "Take all representative values",
        "Take specific representative values",
    )

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
        children = parent.batch_elems

        # --- parent handling  (always destroy) --------------------------
        self.ctx.live_entities.remove(parent)
        self.emit("entity_absorbed", entity=parent)      # #319: the batch itself is gone
        tending.cobot_of(self, parent)       # D169: a cobot staying with the batch is freed

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

        # --- build payload according to mode ----------------------------
        if self.mode == "Take all representative values":
            # Identity/lifecycle fields never copy onto a child (DEVLOG #28):
            # doing so used to duplicate the parent's id and self-reference batch_elems.
            payload = {k: v for k, v in parent.__dict__.items() if k not in SEPARATE_EXCLUDED_FIELDS}
        elif self.mode == "Take specific representative values":
            payload = {}
            for label in self.attrs:
                real_attr = self.CURATED_ATTRS_MAP.get(label)
                if real_attr and hasattr(parent, real_attr):
                    payload[real_attr] = getattr(parent, real_attr)
                else:
                    log.warning(f"Skipping unknown or missing attr '{label}' -> '{real_attr}'")

        else:  # 'retain'
            payload = None

        # --- forward every child ---------------------------------------
        for child in children:
            # The children left live_entities when they were batched; splitting
            # the batch puts them back in WIP (DEVLOG #29).
            child.in_batch = None
            if child not in self.ctx.live_entities:
                self.ctx.live_entities.append(child)
            child.cost = cost_share
            child._auto_cost = auto_share
            child._holding_cost = holding_share

            if payload:
                for attr, val in payload.items():
                    try:
                        setattr(child, attr, val)
                    except Exception as e:
                        log.warning(f"Could not set '{attr}' on child entity: {e}")

            else: # separate info collection when retaining original values
                child.batch_info = {"batch_id": parent.id,
                                "batch_name": parent.name,
                                "batch_type": parent.e_type,
                                "batch_creation": parent.creation_time,
                                "batch_deletion": parent.deletion_time,
                                "batch_throughput_time": parent.throughput_time,
                                "batch_size": len(children),
                                "batch_elems": children}
            self.forward(child, 0)

    def self_run(self, entity):
        self.self_process(entity)

    def get_params_dict(self):
        """Return dict that will be persisted in Block."""
        # we persist the _user_ picks only:
        return {"mode": self.mode, "attrs": self.attrs}

    def set_params_from_dict(self, param_dict):
        self.mode = param_dict.get("mode", "Retain original entity values")
        self.attrs = param_dict.get("attrs", [])


SPEC = BlockSpec(
    type_name="SEPARATE", cls=Separate,
    params=(
        ParamSpec("mode", str, "Retain original entity values",
                  description="How children get their attributes: retain their own values, take all of the "
                              "parent's, or take only the ones listed in attrs.", quantity="none"),
        ParamSpec("attrs", list, [], description="Attribute labels to copy from the parent, used when mode is \"specific\".", quantity="none"),
    ),
    factory=lambda env, block: Separate(env, block),
    family="group",
    holding=True,
)
