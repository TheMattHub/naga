import inspect
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core import logic_authoring

log = logging.getLogger(__name__)

__all__ = ["Logic", "SPEC"]


class Logic(BaseProcess):
    """Routes an entity through a user-supplied, plain (non-BaseProcess) class
    defined in nagasim.core.custom_logic. The class only needs an
    `execute(self, entity, ctx) -> int` method returning the outgoing link index;
    `execute(self, entity)` (no ctx) is also accepted."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)

        params = getattr(block, "params", {})
        self.logic_class = params.get("logic_class", "SampleLogic")

    def self_process(self, entity):
        module = logic_authoring.load_custom_logic_module()
        logic_cls = getattr(module, self.logic_class, None)
        if logic_cls is None:
            raise RuntimeError(
                f"Logic class '{self.logic_class}' not found in nagasim.core.custom_logic"
            )

        logic_instance = logic_cls()
        if len(inspect.signature(logic_instance.execute).parameters) >= 2:
            next_idx = logic_instance.execute(entity, self.ctx)
        else:
            next_idx = logic_instance.execute(entity)

        self.forward(entity, next_idx)

    def self_run(self, entity):
        self.self_process(entity)
        # if self_process ever needs a yield, switch this to
        # return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return { "logic_class": self.logic_class }

    def set_params_from_dict(self, param_dict):
        self.logic_class = param_dict.get("logic_class", "SampleLogic")


SPEC = BlockSpec(
    type_name="LOGIC", cls=Logic,
    params=(ParamSpec("logic_class", str, "SampleLogic",
                      description="Name of a class in nagasim.core.custom_logic implementing execute(entity, ctx) -> int.", quantity="none"),),
    factory=lambda env, block: Logic(env, block),
    multi_output=True,
    family="control",
    holding=True, transparent=True,
)
