import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import DUPLICATE_EXCLUDED_FIELDS
from nagasim.core.sim_classes import Entity

log = logging.getLogger(__name__)

__all__ = ["Duplicate", "SPEC"]


class Duplicate(BaseProcess):
    """Makes copies of an entity, for example to model one job spawning several; the original goes on unchanged. All copies leave by the one output link: follow it with a DECIDE to send them down different branches.

    Forwards the original entity unchanged, then forwards
    `number_of_duplicates - 1` shallow copies, each a fresh Entity with its
    own id and name (never a deepcopy: that used to clone the entity manager
    and any live resource requests)."""

    def __init__(self, env, block, next_process=None, number_of_duplicates=2):
        super().__init__(env, block, next_process)
        if number_of_duplicates < 2:
            raise ValueError("number_of_duplicates must be greater than or equal to 2.")
        else:
            self.number_of_duplicates = number_of_duplicates    # total number of entities leaving this block

    def self_process(self, entity):

        if self.ctx.debug:
            log.debug("Entity %s arrives at the duplicate at time %s.", entity.id, self.env.now)
        # The fields are read BEFORE the original moves on: a block that runs in
        # the caller (a zero-delay DISPOSE, an ASSIGN) may already have finished
        # or changed it by the time `forward` returns, and a copy is the entity
        # as it arrived here (DEVLOG #342). The copies themselves are still made
        # after it, so entity ids are handed out in the same order as before.
        fields = [(key, value) for key, value in vars(entity).items()
                  if key not in DUPLICATE_EXCLUDED_FIELDS]
        self.forward(entity, 0)  # the first entity moves on to the next process unchanged

        for _ in range(self.number_of_duplicates - 1):

            # Shallow field-by-field copy onto a fresh Entity (its own id, name)
            # instead of deepcopy, which used to clone the entity manager and
            # any live SimPy resource requests.
            new_entity = Entity(next(self.ctx.entity_ids))
            for key, value in fields:
                if key == "batch_elems":
                    value = list(value)  # own list, not shared with the source entity
                setattr(new_entity, key, value)

            if self.ctx.debug:
                log.debug("Entity %s has been duplicated at time %s.", new_entity.id, self.env.now)

            self.ctx.all_entities.append(new_entity)
            self.ctx.live_entities.append(new_entity)
            self.emit("entity_created", entity=new_entity)

            self.forward(new_entity, 0)

    def self_run(self, entity):
        self.self_process(entity)

    def get_params_dict(self):
        return {
            "number_of_duplicates": self.number_of_duplicates
        }

    def set_params_from_dict(self, param_dict):
        self.number_of_duplicates = max(2, param_dict.get("number_of_duplicates", 2))


SPEC = BlockSpec(
    type_name="DUPLICATE", cls=Duplicate,
    params=(ParamSpec("number_of_duplicates", int, 2,
                      description="Total number of entities leaving this block, original included (>= 2).", quantity="count", min=0),),
    factory=lambda env, block: Duplicate(env, block, number_of_duplicates=2),
    family="group",
    holding=True,
)
