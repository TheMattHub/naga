"""Submodel port and instance blocks (ROADMAP 2.3 part B, schema v12).

Three editor-only block types:

  SUB_ENTRY   an input port of a definition: `port` names it; exactly one
              outgoing link, no incoming one.
  SUB_EXIT    an output port of a definition: `port` names it; incoming links
              only.
  SUBMODEL    an instance of a definition: `definition` names it, `arguments`
              binds the definition's parameters, `preset` records which
              catalogue row those argument values came from (PHASE4 B4).

None of the three ever runs. `core/submodels.flatten()` expands every SUBMODEL
into the definition's own blocks (named `<instance>/<inner>`) and rewires the
links through the ports, and `GraphModel.load_from_json` flattens before it
builds processes -- so a submodel costs the engine nothing. The classes exist
only so BLOCK_REGISTRY stays the single source of truth for what the editor
can drop on a canvas (param schema, palette icon, properties form); `self_run`
refuses loudly, because reaching it means a layout was run unflattened.
"""
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec

log = logging.getLogger(__name__)

__all__ = ["SubmodelBlock", "ENTRY_SPEC", "EXIT_SPEC", "INSTANCE_SPEC"]


class SubmodelBlock(BaseProcess):
    """Editor-only placeholder shared by three types, never run directly --
    `core/submodels.flatten()` expands them away before the engine builds any
    process. SUB_ENTRY: an input port of a submodel definition (`port` names
    it; exactly one outgoing link inside the definition, none incoming).
    SUB_EXIT: an output port of a definition (`port` names it; incoming links
    only). SUBMODEL: an instance of a definition dropped on the canvas
    (`definition` names it, `arguments` binds its parameters)."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.port = ""
        self.definition = ""
        self.arguments = {}

    def _refuse(self):
        raise RuntimeError(
            f"Block '{self.name}' ({self.block.block_type}) cannot run: the layout was "
            f"not flattened (see nagasim.core.submodels.flatten).")

    def self_process(self, entity=None):
        self._refuse()

    def self_run(self, entity=None):
        self._refuse()

    def get_params_dict(self):
        return {}


ENTRY_SPEC = BlockSpec(
    type_name="SUB_ENTRY", cls=SubmodelBlock,
    params=(ParamSpec("port", str, "in", description="Input port name of the definition", quantity="none"),),
    factory=lambda env, block: SubmodelBlock(env, block),
    family="source", shape="pill",
)

EXIT_SPEC = BlockSpec(
    type_name="SUB_EXIT", cls=SubmodelBlock,
    params=(ParamSpec("port", str, "out", description="Output port name of the definition", quantity="none"),),
    factory=lambda env, block: SubmodelBlock(env, block),
    family="sink", shape="cap",
)

INSTANCE_SPEC = BlockSpec(
    type_name="SUBMODEL", cls=SubmodelBlock,
    params=(
        ParamSpec("definition", str, "", description="Name of the submodel definition", quantity="none"),
        ParamSpec("arguments", dict, {},
                  description="Parameter bindings: {name: number | expression | bool}", quantity="none"),
        ParamSpec("preset", str, "",
                  description="Provenance only: the catalogue row whose values were "
                              "written into `arguments`. The values are what the model uses, so "
                              "editing one afterwards is fine and nothing re-reads the catalogue.", quantity="none"),
    ),
    factory=lambda env, block: SubmodelBlock(env, block),
    # One outgoing link per output port, and a port may fan out further.
    multi_output=True,
    family="container", shape="submodel",
)
