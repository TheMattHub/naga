Use SUB_ENTRY inside a submodel definition to mark where entities come in. Each input port of the definition has one. Outside, the links that reach the [SUBMODEL](submodel.md) instance on that port continue from the block that SUB_ENTRY links to.

## Behaviour and rules

The `port` names the input. A SUB_ENTRY has exactly one outgoing link inside the definition and no incoming link. Port names must be unique within a definition, and a duplicate is an error.

The block never runs. Before a run, the engine removes it and joins the instance's incoming link directly to the block behind the port. It takes no time and adds no results. A link that names a port the definition does not have is reported by a check.
