Use SUB_EXIT inside a submodel definition to mark where entities leave. Each output port of the definition has one. Outside, the instance's link on that port starts from the block after the [SUBMODEL](submodel.md) instance.

## Behaviour and rules

The `port` names the output. A SUB_EXIT has incoming links only and no outgoing link. Port names must be unique within a definition, and a duplicate is an error.

The block never runs. Before a run, the engine removes it and joins the blocks behind it directly to the instance's outgoing link. It takes no time and adds no results. It is not a DISPOSE, so entities do not leave the model there. A definition with no SUB_EXIT whose flow ends in a block other than a DISPOSE is reported by a check.
