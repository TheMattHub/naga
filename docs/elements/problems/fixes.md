E_UNKNOWN_BLOCK_TYPE: Correct the block's `type` to one of the block types in the block reference. Types are upper case, such as `PROCESS`; a misspelling or a type from another tool is the usual cause.
E_PARAM_UNKNOWN: Remove the parameter, or rename it to one the block type declares. The block's reference page lists every parameter; a typo or a parameter of a neighbouring block is the usual cause.
E_PARAM_TYPE: Give the parameter a value of the declared type, for example a number rather than the text "5", or a list rather than a single value. The hint names the types the block takes.
E_PARAM_MISSING: Add the required parameter to the block. The block's reference page marks the required ones, for example `resource_name` on a RELEASE.
E_FIELD_RANGE: Bring the value inside the bounds that the section's field table states. The message names the field, and the table gives its minimum and maximum.
E_DUPLICATE_BLOCK: Rename one of the two blocks. Block names are the model's identifiers, and links, results and overrides refer to a block by its name.
E_UNKNOWN_DISTRIBUTION: Use a distribution from the distribution reference, spelled as it is there (for example `Exponential`). The hint lists the valid names.
E_DIST_PARAM_UNKNOWN: Rename or remove the distribution parameter. Each distribution takes only the parameters on its reference page, and any other key would be ignored.
E_EXPRESSION: Rewrite the expression within the allowed grammar. Check the function names for case (`max`, not `MAX`), use positional arguments only, and quote string arguments such as table names.
E_ASSIGN_TARGET: Set the ASSIGN target to `entity.<attribute>`, `entity.agent.<attribute>` or the name of a declared variable. Other forms, such as a function call, cannot be written to.
E_UNDECLARED_VARIABLE: Declare the variable in the `variables` section, or correct the name in the expression or ASSIGN target. Names are case sensitive.
E_TABLE_SHAPE: Make the table a rectangle. A 1-D table needs one value per key. A 2-D table needs one row of values per key, each as long as `columns`, and no key may repeat.
E_NO_CREATE: Add a CREATE block and link it into the flow. Without a source, nothing ever arrives in the model.
E_NO_DISPOSE: Add a DISPOSE block at the end of the flow. Entities that never leave stay in the work in progress, and the throughput results stay empty.
E_CREATE_NO_OUTPUT: Link the CREATE block to the first block of the flow. Entities need an outgoing link to go anywhere.
E_CREATE_ZERO_INTERARRIVAL: Give the CREATE block a positive interarrival time, or set `max_arrivals` to a positive number. A constant gap of 0 with unlimited arrivals would never let time advance.
E_TOO_MANY_OUTPUTS: Remove the extra links, or insert a DECIDE to branch. Only DECIDE and LOGIC may branch. A block that can balk, renege or keep a carrier is allowed a second link.
E_TERMINAL_NOT_DISPOSE: End the flow in a DISPOSE: link this block to one, or add one after it. A block with no outgoing link would strand its entities.
E_DECIDE_BRANCHES: Give the DECIDE one outgoing link for every probability or condition, in the same order, or remove the extra entries. One more link than entries serves as the ELSE branch.
E_DECIDE_PROBS: Make the probabilities sum to at most 1. If they sum to less than 1, add one more outgoing link as the ELSE branch for the remainder, or raise the probabilities to sum to 1.
W_UNREACHABLE: Link the block into the flow from a CREATE, or delete it. A block that no entity can reach never runs and adds nothing to the results.
E_RESOURCE_UNKNOWN: Declare the resource in `resources` or `resource_sets`, or correct the name in the block's `resource_name`. Names are case sensitive.
E_DUPLICATE_RESOURCE: Rename one of the two. A resource and a resource set share one namespace because blocks seize either by name.
E_LINK_UNKNOWN_BLOCK: Add the block that the link names, or point the link at an existing block. A renamed or deleted block is the usual cause.
E_QUEUE_NO_LEAVE_LINK: Add a second outgoing link, from the block that balks or reneges to where entities that leave without service go, often a DISPOSE. Or clear `balk_if` and `renege_after`.
E_SERIES_SHAPE: Give the series one value per sample and either `times` or a `step`, not both. The sample count must match, and the times must increase.
E_AGENT_ATTRIBUTE: Give each agent attribute and parameter a unique valid name, make every parameter a number, and declare any `self.<name>` an expression reads. Do not name an attribute `heading`.
E_AGENT_STATE: Declare the agent type's states, and make the initial state and every transition end one of them. A population's initial states must also be declared.
E_DATA_READ: Check that the file exists at the path relative to the model file, and that the sheet, column or query is right. A network source must be reachable and permitted by the caller.
E_DATA_BINDING_TARGET: Point the binding's `target` at a path the model has, for example `resources.Nurse.capacity`. Declare the resource first if it does not exist.
E_SDIMPORT_UNSUPPORTED: Rebuild the variable that the import left out, using the model file's own sections. The message names the unsupported construct, such as a module, a macro or an external data reference.
E_ORDER_FIELD: Correct the order's field. Quantities are whole numbers of 0 or more, release and due are numbers or ISO timestamps, and extra attributes must be single values.
E_CONVEYOR_DIVERT: Write each divert as `at`, `condition` and optionally `time` and `when_full`, with `at` on the belt. Give the belt one more link than it has diverts.
E_CONVEYOR_MERGE: Write each merge as `at`, `from` and optionally `priority`, with `at` on the belt and `from` naming a block that links to the belt. Merges need an accumulating belt.
E_SEND_TARGET: Write `to` as `entity.agent`, an `AGENT("Pop", i)` expression, or an object with a `population`. A SEND block needs a model that declares at least one population.
E_SUBMODEL: Read the message for the definition that failed to flatten. The usual causes are a missing definition, an argument the definition does not declare, or a link to a port that does not exist.
E_SCENARIO_OVERRIDE: Give every override a `path` and a `value`. A path has the form `resources.<name>.capacity`, as listed on the section's page.
W_UNKNOWN_KEY: Remove the key, or correct its spelling. The model file format drops keys it does not have, so the setting would be lost when the file is saved.
E_SCHEMA_VERSION: Open the file with a newer version of the tool. It was written by a version that is newer than this one, so this build cannot be sure to read it right.
W_UNBOUNDED_DURATION: Use a distribution that cannot go below 0, such as `Lognormal`, `Gamma` or `BoundedNormal` in place of `Normal`. Negative samples are cut to 0, which raises the mean.
E_BUILDING: Correct the part of the `building` section the path names. The `building` section page gives the shape of each room, wall line, opening, marking, label and fixture, and the hint names the field or colour that is wrong.
W_OPENING_OFF_WALL: Point the opening at a wall that exists: `{"room": NAME, "edge": K}` or `{"line": INDEX, "edge": K}`, where edge K runs from point K to K + 1. An opening wider than its wall edge needs a narrower `width` or a longer wall.
W_WALL_CROSSING: Add an opening where the path goes through the wall, or move the path segment. Walls never change routing, so without an opening the movers in a run walk through the wall.
