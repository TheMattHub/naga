import inspect
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core import logic_authoring

log = logging.getLogger(__name__)

__all__ = ["Logic", "SPEC"]


class Logic(BaseProcess):
    """Custom routing written in Python: sends each entity to one of several outgoing links by your own rule when chance or a condition is not enough.

    Routes an entity through a user-supplied, plain (non-BaseProcess) class
    defined in nagasim.core.custom_logic, or first in the module the caller
    passes as `run_model(logic=...)`. The class only needs an
    `execute(self, entity, ctx) -> int` method returning the outgoing link index;
    `execute(self, entity)` (no ctx) is also accepted."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)

        params = getattr(block, "params", {})
        self.logic_class = params.get("logic_class", "SampleLogic")
        self.hook = ""
        # The item instance this block belongs to, stamped by flattening on a hook block of an
        # item with code (D1344); None everywhere else.
        self.item = None

    def self_process(self, entity):
        if self.hook and self.item is not None:
            runtime = self.ctx.items.get(self.item)
            if runtime is None:
                raise RuntimeError(f"Block '{self.name}': item '{self.item}' has no code loaded for its "
                                   f"{self.hook} hook")
            if self.hook == "entry":
                self.forward(entity, runtime.entry(entity, len(self.next_process)))
            else:
                runtime.exit(entity)
                self.forward(entity, 0)
            return
        # D914: the caller's module (run_model(logic=...)) first, then custom_logic.
        logic_cls = logic_authoring.logic_class(self.logic_class, self.ctx.logic_module)
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
        return {"logic_class": self.logic_class, **({"hook": self.hook} if self.hook else {})}

    def set_params_from_dict(self, param_dict):
        self.logic_class = param_dict.get("logic_class", "SampleLogic")
        self.hook = str(param_dict.get("hook") or "")


SPEC = BlockSpec(
    type_name="LOGIC", cls=Logic,
    params=(ParamSpec("logic_class", str, "SampleLogic",
                      description="Name of a class in nagasim.core.custom_logic implementing execute(entity, ctx) -> int.", quantity="none"),
            ParamSpec("hook", str, "", quantity="none",
                      description="Inside an item with code: call its on_entry (route by its answer) or its "
                                  "on_exit (then go on by the first link). Empty: use logic_class."),),
    factory=lambda env, block: Logic(env, block),
    multi_output=True,
    family="control",
    holding=True, transparent=True,
)
