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
# Agents
E_AGENT_ACTION: Rewrite the action in the agent type with exactly one action key and its own fields, as the message names. A `set` target must be `self.` plus a declared attribute or a declared variable, and movement actions need the matching `space`.
E_AGENT_EXPRESSION: Rewrite the agent type's expression so it compiles, reading only `self.` attributes, the type's parameters, model variables, `TNOW` and built-in functions. Read `payload` only in a message transition, never `entity`, and draw no random numbers in a `condition`.
E_AGENT_GRID: Give the agent type a grid space, such as `"space": {"kind": "grid", "size": [50, 50]}`, or remove the grid functions, `step` or `move_random` actions and `patch.` writes.
E_AGENT_NETWORK: Fix the population's `network` to the keys its `kind` takes: `p` or `mean_degree` for `random`, an even `k` and `beta` for `small_world`, `m` for `scale_free`, a table with numeric `from` and `to` columns for `table`.
E_AGENT_PATCH: Fix the patch under the grid space: a unique identifier `name`, `diffuse` and `evaporate` in [0, 1], `grow` as `{rate, max}`, and `patch_tick` above 0 when it changes. Read only declared patches with `PATCH`.
E_AGENT_PLACEMENT: Set the population's `placement.kind` to `random` or `table`. A table placement needs a non-empty list of points or nodes; on a grid, cells `[cx, cy]` inside it and no `bounds`.
E_AGENT_PYTHON: Make the agent type's `python` top-level `def` statements without imports, globals, classes, `yield`, `with` or underscore names. Each `{python: ...}` action must name a defined function taking `(agent, api)`.
E_AGENT_RATE_FORM: Write the rate as an own part (`self`, parameters, constants) times a shared part, with no factor mixing both. Express time dependence with `STEP`, `PULSE` or a step time series, not a bare `TNOW`.
E_AGENT_SPACE: Fix the agent type's `space`: `kind` is `continuous`, `network` or `grid`. A continuous space needs `bounds` `[x0, y0, x1, y1]`; a grid needs a valid `size`; a network space needs nodes in the model's `network` section.
E_AGENT_TICK: Write `tick` as `{"dt": 1, "activation": "synchronous"}` with `dt` above 0 and `phase` below `dt`. Give a tick to any population whose type has `on_tick` actions or `tick` transitions, and give `let` new identifier names.
E_AGENT_TRANSITION: Give the transition exactly one trigger among `timeout`, `rate`, `condition`, `message` and `tick`. A `message` must be a name, a `tick` a condition string, and `scan` a number above 0.
E_AGENT_TYPE_NAME: Give every entry in `agent_types` a non-empty `name` that no other agent type uses.
E_AGGREGATE_AGENTS: Write the agent aggregate with quoted literal names, for example `SUM("People", "age")` or `MEAN("People", "age", "Sick")`. For the system-dynamics form use `SUM(LEVEL("x[*]"))`.
E_POPULATION_COUNT: Set the population's `count` to an integer of 0 or more (or a table count), keep the `initial` state counts within it, and make `max_count` an integer at least `count`.
E_POPULATION_FULL: Raise the population's `max_count`, or make the agent type's `spawn` actions create fewer agents. The run stops when a spawn finds the population already at `max_count`.
E_POPULATION_NAME: Give every entry in `populations` a non-empty `name` that no other population uses.
E_POPULATION_TYPE: Set the population's `type` to the name of an agent type declared under `agent_types`, or declare that type. The hint lists the declared types.
E_POPULATION_UNKNOWN: Correct the population name in the `COUNT`, `SUM`, `MEAN` or `AGENT` call, or in the send, spawn or remove action, or declare that population under `populations`.
W_AGENT_MESSAGE_UNUSED: Add a `send` action or a SEND block that sends this message, or remove the transition that waits for it. Check the spelling: message names must match exactly.
W_AGENT_SCAN: Add `"scan": 0.5` (any interval above 0) to the transition so it is re-checked on that grid. If re-evaluating only at agent events is intended, the warning can stay.
W_AGENT_STATE_COMPOSITE: Compare `self.state` with one of the composite state's leaf states, or count the group with `COUNT("Population", "Composite")`. `self.state` always holds the leaf.
# Submodels and libraries
E_ASSET_BLOCK_TYPE: Replace the block's `type` in the asset's `definition.blocks` with a built-in block type, or install the plugin that registers it. Until then the asset stays unavailable.
E_ASSET_CODE: Remove every `custom_logic`, `python`, `plugin`, `entry_point` or `code` key from the file, and replace any LOGIC block in a `definition` with DECIDE and ASSIGN blocks. A library is data only.
E_ASSET_DEFINITION: Give the asset a `definition` object with a `blocks` list of block objects, then fix what the message names. The definition is checked like any submodel, so a bad link or parameter fails here.
E_ASSET_GEOMETRY: Give each entry in `geometry.nodes` a unique `name` and numeric `dx` and `dy`, with `attach` true or false. Each segment's `from` and `to`, and every `$node` a block binds, must name a declared node.
E_ASSET_GRAPHIC: Fix the graphic the path names. Use only the states `idle`, `busy`, `down`, `off` and `blocked`, include `idle`, keep each SVG safe and small, write colours as `#RRGGBB`, and draw `icon` on `viewBox="0 0 24 24"`.
E_ASSET_ID: Give every asset an object with a unique `id` made of letters, digits, `_`, `-` or `.`, starting with a letter or digit.
E_ASSET_MIGRATION: Give each `migrations` entry a semver range in `from`, such as `^1.2.0`, and a non-empty `steps` list. Each step is one `rename_parameter`, `set_default` or `rename_record` naming a parameter or record kind this version declares.
E_ASSET_PARAMETER: Give each entry in `parameters` a unique identifier `name` and a known `type`, with a `default` that fits it. A `choice` needs `choices` holding the default, and a number must lie within numeric `min` and `max`.
E_ASSET_REQUIRES: Make `requires` an object whose keys are model sections, such as `resources` or `variables`, each a list of records with a `name`. Move any `$arg` value into the definition's `scoped` overrides, because required records are copied as written.
E_ASSET_TEST: Make `tests` a list of objects, each with a non-empty `expect` list. Every expectation needs a numeric `value` and one of `key`, `kpi`, `exit` or `resource`, and every `records` probe needs a `name` and an `expression`.
E_ASSET_UNKNOWN_NAME: Correct the name in the definition expression, or declare it in the asset's `parameters`. A variable the surrounding model supplies goes in `requires.variables`, or in a test's `model.variables`.
E_CATALOGUE_COLUMN: Give every row in the catalogue's `rows` a value for each name in its `columns`. In the asset's `preset.map`, map only declared columns to parameters the asset declares in `parameters`.
E_CATALOGUE_VALUE: Correct the catalogue cell so it fits the parameter the `preset.map` fills with it, for example a number within the parameter's `min` and `max`, or one of its `choices`.
E_DUPLICATE_SUBMODEL_PORT: Rename one of the two `SUB_ENTRY` or `SUB_EXIT` blocks' `port` values in the definition. Port names must be unique per side, so links can say which port they mean.
E_EXTENDS_CYCLE: Change one `extends` in the loop the message shows so it names a definition outside the chain, or remove it. A definition cannot extend itself, directly or through another definition.
E_EXTENDS_UNKNOWN: Set the definition's `extends` to the name of an existing definition under `submodels`, or remove `extends`. The hint lists the definitions you can extend.
E_LIBRARY_CATALOGUE: Give each entry in `catalogues` a unique `name`, a non-empty list of string `columns` that includes its `key`, and at least one row. A `preset` must name a declared catalogue and give a non-empty `map`.
E_LIBRARY_FORMAT: Make the file a JSON object with `format_version` 1 and at least one entry in `assets`. If present, `entities` must be a list of objects with unique names.
E_LIBRARY_HEADER: Give the file a `library` object with an identifier `id`, a non-empty `name` and a semver `version` such as `1.0.0`. An optional `color` must be `#RRGGBB`, and `min_schema_version` a whole number.
E_LIBRARY_VERSION: Lower `library.min_schema_version` to a model schema version this build writes, if the library does not use the newer schema. Otherwise use a newer engine, or an older release of the library.
E_SUBMODEL_ARGUMENTS: Make the instance's `arguments` an object that maps each declared parameter name to its value, for example `{"capacity": 2}`, or remove it to use the defaults.
E_SUBMODEL_ARGUMENT_MISSING: Add the parameter to the SUBMODEL instance's `arguments`, or give it a `default` in the definition's `parameters`. The message names the missing parameter.
E_SUBMODEL_ARGUMENT_UNKNOWN: Remove the argument from the instance's `arguments`, or rename it to a parameter the definition declares. The hint lists the definition's `parameters`; a typo is the usual cause.
E_SUBMODEL_ARG_REFERENCE: Declare the named record in the model's matching section, or pass the name of one that exists. The hint lists the records the argument may refer to.
E_SUBMODEL_ARG_TYPE: Pass an argument of the type the target parameter or repeat count declares, for example an int rather than `2.5` or text. The message names the `$arg` and the value it received.
E_SUBMODEL_ARG_UNKNOWN: Declare the parameter in the definition's `parameters`, or correct the `{"$arg": ...}` name to one it declares. A `$arg` outside any submodel definition must become a plain value.
E_SUBMODEL_CYCLE: Break the loop the message shows by removing or replacing a SUBMODEL block that instantiates a definition earlier in the chain. A definition cannot contain itself, directly or through another.
E_SUBMODEL_DEAD_END: Add a `SUB_EXIT` block to the definition and link the named block to it, or end that flow in a `DISPOSE`. Without an exit every flow must end in a DISPOSE.
E_SUBMODEL_PORT_NAME: Give every `SUB_ENTRY` and `SUB_EXIT` block in the definition a non-empty `port` parameter, such as `in` or `out`. Links to the instance pick a port by this name.
E_SUBMODEL_PORT_REQUIRED: Set the link's `source_port` or `target_port` to one of the instance's ports. A port may be left out only when the definition has exactly one port on that side.
E_SUBMODEL_PORT_UNKNOWN: Set the link's `source_port` or `target_port` to a port the instance has, as listed in the hint. With repeat groups, lowering a count removes ports: delete the link or change the arguments.
E_SUBMODEL_REPEAT: Fix the definition's `repeat` groups as the message says. Each needs `count` and a non-empty `blocks` list of its own blocks, and `{i}` goes in every repeated name or in none.
E_SUBMODEL_REPEAT_COUNT: Make the group's `count` an int from 0 to 500, a `{"$arg": ...}` or an expression of the definition's parameters. A group whose names lack `{i}` takes only 0 or 1.
E_SUBMODEL_SCOPE: Move the `SUB_ENTRY` or `SUB_EXIT` block into a definition under `submodels`, or delete it. Port blocks mean something only inside a submodel definition.
E_SUBMODEL_SCOPED_NAME: Declare a top-level record named as the `$name` without its `$`, in the section the message names. Flattening clones that template for each instance.
E_UNKNOWN_SUBMODEL: Set the instance's `definition` to the name of a definition under `submodels`, as listed in the hint, or add the missing definition. A typo is the usual cause.
W_LIBRARY_ITEM_CLASH: Check the model's own record against the one the asset ships. If they are different things, rename one; if the model's copy is intended, the warning can stay.
W_LIBRARY_UPGRADE: Add a `migrations` entry to the new asset version with a `rename_parameter` step from the old parameter name to the new one, so instances keep the argument's value. Or keep the old parameter in `parameters`.
# Data, live feeds and the twin
E_CANDIDATES: Write the candidates file as `{"format": 1, "kind": ...}` with only `kpi`, `better`, `expires` and `candidates`, each action exactly `verb`, `target` and `value`. Point every target at a block, resource or order the model declares; the message names the action refused.
E_CANDIDATE_VERB: Set the action's `verb` to a built verb: `set_capacity`, `resequence`, `expedite`, `hold`, `release`, `reroute` or `setpoint`. The hint lists the verbs this version builds.
E_CONN_FORMAT: Fix the connections file passed as `--connections`: write it as `{"format": 1, "connections": {...}}`, and correct the entry the message names. Paths in it resolve beside the file and must exist.
E_DATA_BINDING_NAME: Give every entry in `data_bindings` a non-empty `name`, and rename one of two bindings that share a name.
E_DATA_BINDING_SOURCE: Set the binding's `source` to the `name` of a data source declared under `data_sources`, or declare that source. The hint lists the declared ones.
E_DATA_BINDING_TRANSFORM: Use a transform from the hint in the binding's `transform`, with the argument its form needs, such as `quantile:0.9`, `table:KEY` or `series:TIME`. For `orders`, write `map` as an object of column names.
E_DATA_CONNECTION: Set the live source's `connection` to the name of a connection of its kind in the caller's connections file (`--connections`). The person who runs the model defines it there, never in the model.
E_DATA_CREDENTIALS: Remove the password from the source's `location`. Write the whole URL as `${NAGASIM_DATA_DB_URL}` and set that environment variable before the run.
E_DATA_EMPTY: Check the binding's `filter` and `column`: after filtering, the column must hold usable values (numbers, two distinct timestamps for a rate, an id column and a row per table key).
E_DATA_ENV: Set the environment variable the message names before the run, or edit the source's `location` so it no longer refers to it.
E_DATA_ENV_NAME: Rename the `${...}` reference in the source to a variable whose name starts with `NAGASIM_DATA_`, such as `${NAGASIM_DATA_DB_URL}`, and set that variable before the run.
E_DATA_FILTER: Write the binding's `filter` as an object of `"column": value` or `"column op": value` entries, with an operator from the hint and a column the source has.
E_DATA_NETWORK_REFUSED: Pass `--allow-network` to the `naga` command that runs the model if a network source is intended. Otherwise point the source at a local path or a mapped drive.
E_DATA_PATH_OUTSIDE_ROOT: Move the data file under the root the run is confined to, and set the source's `location` to a plain path under it; UNC, device and alternate-stream paths are refused.
E_DATA_PLAINTEXT_CREDENTIAL: Use the encrypted scheme for the connection in the connections file (https, mqtts, an OPC UA security policy), or drop its credential and read anonymously.
E_DATA_PROXY: Use the `https` URL for the source, or list the host in `NO_PROXY` if it is reachable directly. If the tunnel failed, check `HTTPS_PROXY`.
E_DATA_QUERY: Write the source's `query` as one `SELECT` (or `WITH`) statement, with no comment before it and no second statement after a semicolon.
E_DATA_SOURCE_KIND: Set the data source's `kind` to one the hint lists. For a live reading, name a source of a live kind declared in the model's `data_sources`.
E_DATA_SOURCE_LOCATION: Give the data source a `location` its kind can use: a path or URL for a file source, a `query` for `sqlite` and `sql`. For a live kind, give only the path, topic, node ids or registers inside its connection.
E_DATA_SOURCE_NAME: Give every entry in `data_sources` a non-empty `name`, and rename one of two sources that share a name.
E_DATA_SOURCE_OPTION: Remove the unknown key from the source's `options`, or give the option a value it can take. The hint lists the options of that kind and the values allowed.
E_DATA_TLS: Give the server a certificate valid for its host name and dates, or name the private CA as `ca_file` in the connections file. A certificate file it names must be a PEM or DER certificate.
E_DATA_TOO_LARGE: Point the source at a smaller extract, so the response stays under the byte and row caps, with flatter rows for a `rest` source.
E_DATA_TYPE: Pick a `transform` whose result matches the type the target field holds, or point the binding's `target` elsewhere. Text from a live source is never written into the model; bind a number or a boolean.
E_DATA_URL_HOST: Write the source's `location` as `http(s)://host/path` with a hostname that resolves. If `--allow-host` restricts the run, add the host to it.
E_DATA_URL_PARAMETER: Remove the query parameters the hint does not allow from the database URL. Settings outside that list belong to the database account or a service file, not to the model.
E_DATA_URL_PRIVATE: Point the source at a public host. For an intranet host, pass `--allow-private-network`, or add `"private": true` to the connection in the connections file; loopback-only or metadata addresses stay refused for URLs.
E_DATA_URL_REDIRECT: Point the source's `location`, or the connection's address, at the final URL, so the fetch is not redirected, and keep it on `https`.
E_DATA_URL_SCHEME: Use an `http` or `https` URL in the source's `location`. A local file belongs in a `csv`, `xlsx` or `sqlite` source, never in a database URL.
E_FLEET_POWER_STATE: Key the fleet's `power` only by `busy`, `idle`, `blocked`, `down` or `off_shift`, and remove or rename any other state.
E_INITIAL_AGE: Set the entry's `age` in `initial_state.entities` to a number of at least 0, in base time units.
E_INITIAL_ARRIVALS: Give each CREATE in `initial_state.arrivals` a `count` of at least 0 and, for an Interarrival CREATE, a `next` at or after the clock; tied arrivals need different `rank` values.
E_INITIAL_BATCH: Give an entry that meets a `SEPARATE` its `members`, from 1 to the group size of the BATCH or MATCH that forms it. Each member has its own `e_type`, `age`, `attributes` and `holds`.
E_INITIAL_BELT: Correct the belt part in `initial_state.entities`: one entity at an accumulating CONVEYOR, with a `stage` it has and `until` or `entered` on the right side of the clock. Take the state at another time if a part blocks an end stop.
E_INITIAL_BLOCK: Make each `initial_state.entities` entry an object whose `block` names a block of the model, with valid `attributes`, `place`, `held_by` and `transfer`. Inside a submodel, name the block `instance/inner`.
E_INITIAL_BLOCK_TYPE: Move the entry in `initial_state.entities` to a block that can hold entities, such as a PROCESS or BUFFER, instead of the block type the message names.
E_INITIAL_BUSY: Make `initial_state.resources` `busy` equal to the units the in-service entities hold on that resource, or correct `in_service` in the entities.
E_INITIAL_CARRIER: Take the state with the carrier before the carrier BATCH (at its door SEIZE), not while it waits at the BATCH.
E_INITIAL_CLOCK: Set `initial_state.clock` to a number of at least 0 that lies before the end of the run, in the model's base time unit.
E_INITIAL_COUNT: Set the entry's `count` in `initial_state.entities` to a whole number of at least 0.
E_INITIAL_CYCLE: Give a `cycle` only to one in-service entity at a MOTION block, with a `stage` it has. Set `until` at or after the clock and within that segment, and give the time of each segment.
E_INITIAL_FAILURE: Name only failures the resource declares in `initial_state.resources.NAME.failures`, and give each a `next` or a `down_until` time.
E_INITIAL_FLEET: Correct `initial_state.fleets` or the entity's trip so each fleet, vehicle, node, charger and stage it names exists in the model. The message names the one missing.
E_INITIAL_IN_SERVICE: Lower `in_service` to at most the entry's `count` and the resource's capacity, or raise the capacity. Only service blocks take `in_service`.
E_INITIAL_KEY: Remove the unknown key from `initial_state`, or run the state with the engine version that wrote it. The hint lists the keys this engine knows.
E_INITIAL_ORDERS: Name only orders declared in `orders` in `initial_state.orders`, with `created` and `completed` of at least 0 and `completed_at` before the clock. Each `order_book` entry needs `held` true or false.
E_INITIAL_QUEUE_CAPACITY: Lower the number of entities waiting at the block, or parts on the belt, to what its queue, BUFFER or belt holds, or raise its capacity.
E_INITIAL_REMAINING: Give `remaining` one number of at least 0 per in-service entity. Set `ends`, `deadline` and `arrives` at or after the clock, and `since` and `held_since` at or before it.
E_INITIAL_RESOURCE: Name only resources declared in `resources` in `initial_state.resources`, `used` and an entity's `holds`. A hold must be released by a later block, and a released resource must be held.
E_INITIAL_SCHEDULE_OFFSET: Set `initial_state.schedule_offset` to a number of at least 0, how far into its cycle the calendar is at time 0.
E_INITIAL_SET: Name only declared resource sets in `initial_state.resource_sets`, with a `cursor` from -1 to the last member's index, and give `member` a resource of the set the block seizes.
E_INITIAL_VARIABLE: Declare the variable in `variables`, or remove it from `initial_state.variables`. The hint lists the declared variables.
E_LEVEL_INITIAL_CYCLE: Give one of the levels in the cycle a number as its `initial`, so the initial expressions no longer read each other in a loop.
E_MESMAP_FEED: Use only known keys under `feeds`, and give the model what each enabled feed writes: a CREATE with `arrival_mode` `Orders` for `plan`, a declared `parameters.process_time_table`. Check the mapped table, failure and `scrap_target` names exist in the model.
E_MESMAP_FORMAT: Make the mapping file a valid JSON object. Set `format_version` to a version this reader supports, or write the mapping in the older format.
E_MESMAP_PRODUCT: Give each `products` entry a unique `mes` id, as the MES reports it. Point `entity_type` and `sequence` at names the model declares, or set `feeds.sequences` to `true` to build routings from the MES.
E_MESMAP_TIME: Set `time.epoch` to an ISO 8601 instant for model time 0, such as `2026-09-21T06:00:00Z`. Set `time.unit` to `seconds`, `minutes`, `hours` or `days`.
E_MESMAP_UNMAPPED: Add a `work_centres` or `products` entry for each MES id named, and map each work centre holding WIP to a STATION block that leads to a PROCESS. Fix a routing step with no work centre in the MES extract.
E_MESMAP_WORK_CENTRE: Give each `work_centres` entry a unique `mes` id. Point `station`, `resource` and `failure` at a STATION block, resource and failure the model declares, and make `process_time_target` a model path ending in `.distribution`.
E_MES_ADAPTER: Write the adapter spec as `sqlite:<path>`, `csv:<folder>` or `rest:<connection>`, with a location after the colon.
E_MES_READ: Make the source readable: the database file or CSV folder must exist and follow the MES table schema, with a one-row `snapshot` table holding `taken_at`. For a `rest:` source, check the connection's address and credential.
E_MES_TIME: Write the MES timestamp in ISO 8601 with a UTC offset, such as `2026-09-21T06:00:00Z`.
E_OUTBOX_SINK: Give `--outbox` as `file:<path>` in an existing local folder (not a UNC or device path, a link or a hard-linked file), or as `mqtt:<connection>` naming an mqtt connection with `"use": "outbox"` in the connections file.
E_PLANTMAP_DOUBLE: Keep one signal for each count, state, level or resource's units in the plant mapping's `signals`. Tell a block's content either by a `queue` signal or by identities, not both. The engine does not choose between two reports.
E_PLANTMAP_FORMAT: Fix the `*.plantmap.json` at the path given: it needs `"format": 1`, a `time.epoch` ISO 8601 instant, only known keys, and signals whose `as`, `key` and tables match their kind. `naga plant init` writes a valid scaffold.
E_PLANTMAP_NAME: Correct the name in the signal (`block`, `blocks`, `resource`, `level`, `nodes`, `sequences` or `unit`) to one the model has; the hint lists the model's names. Check that the mapping is used with the model it was written for.
E_PLANTMAP_SIZE: Shrink the plant mapping under its bounds. The file stays under 1 MiB, with at most 2000 `signals`, at most 10 000 entries in each table, and each `key` at most 1024 bytes.
E_PLANT_LOG: Pass a readable log under 256 MiB and 1 000 000 lines, one JSON object a line, as `naga watch --record` writes it. Cut a longer log or record a shorter period.
E_RECOMMEND_KPI: Set `kpi` in the candidates file to a KPI of the forecast's table. Counted throughput is `timeseries/block/<name>/out/max` of a block the plant mapping counts.
E_REFIT: Fix the `--refit` file: `"format": 1`, `"kind": "naga-refit"`, only its own keys, and a non-empty `factors` list, each `{"path", "low", "high"}` with `low` under `high` and a `path` that addresses a number of the model.
E_RESOURCE_POWER_STATE: Use only `busy`, `idle`, `blocked`, `down` and `off_shift` as keys of the resource's `power` object, each with a number of kW per unit; rename or remove any other key.
E_SDIMPORT_MDL: Re-save the Vensim `.mdl` file as plain text (UTF-8 or cp1252) under the 20 MB cap, and check that the path given to `naga sdimport` exists and is readable.
E_SDIMPORT_XML: Re-save the XMILE file as UTF-8, well-formed XML under the 20 MB cap, with no DOCTYPE or ENTITY declaration. Check that the path given to `naga sdimport` exists and is readable.
E_SERIES_TIMES: Sort the samples of the time series so `times` or `timestamps` strictly increase with no duplicates, and give `step` a positive value.
E_STATE_FORMAT: Pass a state document of format 1 as `naga state take` writes it, with finite numbers and plain JSON values only. Take the state again rather than writing one by hand.
E_STATE_HASH: Take the state again with `naga state take` instead of editing the file; a state document is checked against its own hash, so any hand edit refuses it.
E_STATE_MODEL: Take the state again with `naga state take` on the exact model file you run. A change to the model, or an engine upgrade that moves its hash, refuses an older state.
E_STATE_SIZE: Take the state at a time with fewer entities in the system, or reduce the model's work in progress; a state holds at most 16 MiB and 100 000 entities.
E_STATE_STALE: Write the observed state again so it carries a current time stamp, or allow an older state with a larger `--max-age`.
E_STATE_UNSUPPORTED: Take the state at another time, when the kind the message names is not in progress, or remove that element from the model. The coverage table of the state documentation lists what a state carries.
E_TIMESERIES_COUNTS: Set `simulation.timeseries_counts` to a list of at most 1000 different block names that exist in the model, for example `["Drill", "Pack"]`.
E_TWIN_FORECAST: Run the model from the same state with `naga run --state` to see why it fails, then fix the model; the twin keeps the last good forecast meanwhile.
E_TWIN_OUTPUT: Check the twin's `--out` directory: its write rights, its free space, and that no reader keeps `twin.json` or the ledger open. The next cycle writes again.
E_TWIN_REFIT: End the adopted refit with an `ended` reply in the outbox replies, or start the twin with the model the refit was adopted for.
E_TWIN_RESULT: Pass the `--out` directory of a twin that has written at least one cycle, a local folder holding its `twin.json`; start one with `naga twin run ... --out DIR`.
E_TWIN_STATE: Remove or replace the element kind the message names, or map a `vehicle` signal for every vehicle of each fleet. `naga state check` and the state coverage table show what can start from the plant.
E_VARIABLE_INITIAL: Give the variable's `initial` a scalar value: a number, a string or `true`/`false`, not a list or an object.
E_VIEWER_MODEL: Remove what the message names: freeze `data_sources` and `data_bindings` with `naga viewer --freeze`, drop agent `python`, LOGIC blocks and plugin types, or keep the model under 5 MB. Otherwise share a `naga run --player` recording.
W_DATA_ALLOW_NETWORK_IGNORED: Remove `allow_network` from the data source in the model file. Network access is granted by whoever runs the model (`--allow-network`), never by the file.
W_INITIAL_STATE_WARMUP: Set `simulation.warmup` to 0 for a model that starts from an `initial_state`, or remove the initial state if a warm-up from empty is what you want.
W_LAW: Compare the result the law names with the model's arrival rates, service times and capacities, and correct the input that disagrees. Saturation can be intended; with no warm-up or many entities left inside, add a warm-up or lengthen the run.
W_LIVE_DROPPED: Check what the source sends: keys and values must be plain data under the size bound. Its distinct keys must stay under the connection's `keys` limit, which `limits` in the connections file can raise.
W_LIVE_GAP: Check the connection to the source and the network or server behind it; the latest values are right again, but the history has a hole of unknown size.
W_LIVE_LIMIT: Use an encrypted OPC UA connection to a server you trust, lower the server's own message limits, or install an asyncua version the reader supports.
W_LIVE_LOST: Read the source more often, or raise the connection's `events` limit under `limits` in the connections file (ceiling 100 000).
W_LIVE_SKEW: Synchronise the clocks of the source and this machine, for example with NTP; both stamps travel with every event.
W_MESMAP_UNUSED: Correct the entry's `mes` id to the one the MES reports for that equipment, or remove the `work_centres` entry.
W_MES_NAIVE_TIME: Export the MES timestamps, in the `events` table and the `snapshot` table's `taken_at`, with a UTC offset such as `Z`. A source that writes local time otherwise shifts the history window by its offset.
W_MES_TABLE_MISSING: If the plant has the table, give the REST connection's credential access to it, or serve it at the connection's address. Otherwise no change is needed: the table reads as empty.
W_MES_THIN_DATA: Widen `parameters.window_days` or lower `parameters.min_samples` in the mapping. If no distribution could be fit, check the units and signs of the durations in the MES extract.
W_OUTBOX: Make the outbox reader open, read and close its file and the replies file, and move or empty a file at its ceiling. Failed records are retried until they expire; past the daily ceiling the rest is dropped until the next UTC day.
W_PLANT_CONTRADICTION: Check the plant signals the message counts by reason: a job for an unseen entity, a retyped entity, a level out of bounds, a counter going back. Fix the mapping, or set `rollover` on a counter that wraps.
W_PLANT_DROPPED: Check the mapping's tables (`states`, `blocks`, `types`) and fields against what the plant sends; the message counts each reason. A recorded log needs one JSON object a line, as `naga watch --record` writes.
W_PLANT_GAP: Check the plant connection and raise the source's `events` limit in the connections file if events were lost. What was missed is not shown; the next values put the timeline right.
W_PLANT_SERVICE_INFERRED: Map the station's `job` signal in the plant mapping, so the plant names which entities are in service instead of the longest-waiting ones being assumed.
W_PLANT_SYNTHETIC: Map identities with `at`, `seen` or `job` signals where the plant has them, so the timeline follows named items and units instead of counts alone.
W_PLANT_TIME: Synchronise the plant's clocks with this machine and have each signal carry a source stamp; the message counts stamps that were missing, ahead of their receipt or late.
W_SDIMPORT_APPROX: Check the translated element at the path given. To avoid it, set the source model's method to euler or rk4 and its time unit to day, hour, minute or second. Set its start time to 0, and give each graphical function an input.
W_STATE_NOT_COVERED: Map a plant signal for the element kind the message names, or take the state again with this version. If the model's declared start is acceptable for it, the warning can stay.
W_TWIN_ALERT: Use one `--out` directory per model and keep the same `--alert-k` and `--alert-h` when the twin is started again. If the change was intended, the sums restart and the warning can stay.
W_TWIN_CLOCK: Check this machine's time synchronisation. A single jump, such as a manual clock change, needs no action: cadences are not moved and later stamps use the new time.
W_TWIN_CUT: Reduce what one result holds: fewer KPIs or compared series in the forecast, or a smaller view. For a busy source, shorten `--every` so each cycle has fewer events to read.
W_TWIN_FORECAST_BUSY: Lengthen `--forecast-every`, or shorten `--horizon` or lower `--replications`, so each forecast ends before the next is due.
W_TWIN_MODEL_CHANGED: Stop the twin and start it again to use the changed model file; until then it keeps the model it loaded.
W_TWIN_RECOMMEND: Act on what the message names. Use one `--out` directory per model so its ledger matches, keep the start state under its size bound, and check that a taken action's target still exists in the model.
W_TWIN_REFIT: Give the twin the same `--refit` file it ran with, and adopt only refits whose factors that file allows; reply `ended` to a refit that should not apply.
W_TWIN_SCORE: Use one `--out` directory per model and `--horizon`. A twin started with another model or horizon restarts its score, and the warning can then stay.
W_TWIN_SOURCE: Check that each live source is up and connected, that `--max-age` is longer than its silent spells, and that the mapping's signal keys match what the plant sends (`naga watch`).
W_VIEWER_EMPTY: List at least one entry under `dashboard.inputs` and one under `dashboard.results`. If the author's run could not be recorded, run the model with `naga run` to see why.
W_VIEWER_LONG_RUN: Lower `dashboard.limits.max_replications` so a run at the most replications a viewer allows takes under five minutes in a browser.
# Networks, fleets and automation
E_BATTERY: Correct the fleet's `battery` field the message names: `capacity_kwh` above 0, `initial`, `low`, `target` and `opportunity_below` between 0 and 1 with `low` below `target`, and a known `strategy` with the field it needs (`opportunity_below` or `schedule`).
E_BATTERY_SCHEDULE: Declare the schedule under `schedules`, or correct the name in the fleet's `battery.schedule`. A scheduled battery charges while that schedule's value is above 0.
E_CALENDAR_TIMEZONE: Set `simulation.calendar.timezone` to an IANA time zone name such as `UTC`, `Europe/Rome` or `America/New_York`, or remove it to use UTC.
E_CHARGER: Correct the entry under `chargers` the message names. It needs a unique `name` no resource or fleet uses, a declared network `node`, `kind` `charge` or `swap`, and integer `points` of 1 or more. Set `power_kw` above 0, fractions within 0-1, and `swap_time` on a swap station.
E_CHARGER_UNKNOWN: Declare the charger under `chargers`, or correct the name in the fleet's `battery.chargers` list. Drop the list to allow every charger of the matching kind.
E_DISPATCH: Fix what the message names under `dispatch`: non-empty `supply_tags` and `demand_tags`, a `replan_every` that is a multiple of `step` within `horizon`, known `terminal` and `forecast`. Keep one flow per grid role, and give every `optimal` storage a declared `setpoint` variable.
E_DISTANCE_UNKNOWN: Correct the place in the `DISTANCE` call to a node under `network.nodes`, or to a STATION block (or its `station` alias) whose `node` is set. Place names are case sensitive.
E_FLEET_ARM: Set the fleet's `arm` to a profile declared under `motion_profiles`, or remove `arm` from a worker fleet, a gantry fleet or one with `loads` above 1. Drop `exchange` on a fleet without an `arm`.
E_FLEET_FIELD: Correct the fleet field the message names: an integer `count` of 1 or more, `kind` `vehicle` or `worker`, and a `speed` above 0. Use known `dispatch`, `select` and `park` rules, and a `capacity_schedule` naming a schedule of kind `capacity`.
E_FLEET_LOADS: Set the fleet's `loads` to an integer of 1 or more, or drop it for 1. A fleet of kind `worker` carries one load at a time: drop `loads` there, or make the fleet kind `vehicle`.
E_FLEET_MOTION: Set the fleet's `motion` to `network` or `gantry`. A gantry fleet needs `speed_x` and `speed_y` above 0 and a `hoist_time` of 0 or more, and cannot be of kind `worker`.
E_FLEET_NAME: Give every entry under `fleets` a unique, non-empty `name` that no resource or resource set uses. A fleet is itself a resource, so do not also declare it under `resources`.
E_FLEET_NODE: Set the fleet's `home`, and each unit's `home`, to a node declared under `network.nodes`, or add that node. A fleet also needs a `network` section with nodes and segments to drive on.
E_FLEET_POWER: Write the fleet's `power` as an object of kW per vehicle for each state, such as `{"busy": 5.0, "idle": 1.0}`, with every value a number of 0 or more.
E_FLEET_UNITS: Make the fleet's `units` a non-empty list of objects, each with a unique `name` and a `skills` list of names. Drop `count`, or make it equal the number of units.
E_FLEET_UNKNOWN: Declare the fleet under `fleets`, or correct the TRANSPORT block's `fleet` parameter to a declared fleet name. Names are case sensitive.
E_INVESTMENT: Make `investment` an object whose `baseline` names a scenario (or is `""`), with `horizon_years` and `operating_hours_per_year` above 0. Set `discount_rate` to 0 or more, a numeric `margin_per_part`, and a `throughput` KPI key such as `user/Good parts`.
E_MOTION_PAYLOAD: Lower the move segment's `payload_kg` to at most the profile's `payload_max`, raise `payload_max` on the motion profile, or point the MOTION block's `profile` at one rated for the load.
E_MOTION_PROFILE: Correct the entry under `motion_profiles` the message names: a unique `name`, `kind` `robot`, `human` or `mechanism`, `speed` and `accel` above 0, `payload_derate` in (0, 1]. Set `collaborative_speed` when a MOTION block uses an `hrc` mode.
E_MOTION_PROFILE_UNKNOWN: Declare the profile under `motion_profiles`, or correct the MOTION block's `profile` to a declared profile name. Names are case sensitive.
E_MOTION_SEGMENT: Give the MOTION block's `segments` at least one entry with a known `kind`. A `move` takes only `distance_m` (0 or more) and `payload_kg`; any other kind takes only a `time`, a non-negative number or an expression.
E_MOTION_ZONE: Set the MOTION block's `hrc` to `none`, `collaborative` or `separation`. For `separation`, name a zone from `zones`, or give the inline `zone` a network `node`, a `radius_m` above 0 and a `measure` of `path` or `straight`.
E_MOTION_ZONE_UNKNOWN: Declare the zone under `zones`, or correct the MOTION block's `zone.name` to a declared zone name.
E_NETWORK_NODE: Give every entry under `network.nodes` a unique, non-empty `name`, numeric `x` and `y`, and a `capacity` that is an integer of 1 or more or null.
E_NETWORK_NODE_UNKNOWN: Add the node under `network.nodes`, or correct the block's `node` parameter to a declared node. For a TRANSPORT `destination`, set the `node` of that STATION.
E_NETWORK_SEGMENT: Give each entry under `network.segments` a `from` and `to` naming two different declared nodes, and a `length` and `speed` above 0 or null. Its `capacity` is an integer of 1 or more or null, and `two_way` is a boolean.
E_NETWORK_SHAPE: Write `network` as an object with `nodes` and `segments` lists, and set `length_factor` to a number above 0 (1.0 is the straight-line distance) or remove it.
E_NETWORK_UNREACHABLE: Add segments under `network.segments` so a path joins the two places the message names, or make a one-way segment on the way two-way (`two_way` true).
E_TENDING: Correct the STATION's `tending` field the message names: only `place`, `pick`, `time_unit`, `hrc`, `zone` and `cobot`, with segments as in a MOTION block. Set `cobot` to `leave` or `stay`, `collaborative_speed` on the arm's profile for an `hrc` mode, and the STATION's `node`.
E_TRANSPORT_KEEP_ARM: Remove `keep_vehicle` from the TRANSPORT on the arm fleet. To keep a cobot with its part, set the station's `tending` `cobot` to `stay` instead.
E_TRAVEL_MODE: Set the ROUTE block's `travel` to `time` or `network`. For `network`, add a `network` section and give every STATION involved a `node`.
E_WORKER_FLEET: Point the block's `worker_fleet` at a fleet declared under `fleets` with `kind` `worker`, or set that fleet's `kind` to `worker`.
E_WORKER_NO_NODE: Set the block's `node` parameter to the network node the worker walks to, or clear `worker_fleet` on the block.
E_WORKER_PREEMPT: Clear `preempt` on the block. A block with `worker_fleet` cannot preempt, and no preempting block may seize a resource (or resource-set member) that a worker block also seizes.
E_WORKER_SKILL_UNKNOWN: Add the skill to the `skills` of a unit in the worker fleet's `units`, or correct the block's `worker_skill`. Skill names are case sensitive.
E_WORKER_WHEN_BLOCKED: Set the block's `worker_when_blocked` to `leaves` or `stays`, or remove it to use the default `leaves`.
E_ZONE: Give every entry under `zones` a unique `name`, a `node` declared under `network.nodes`, a `radius_m` above 0 and a `measure` of `path` or `straight`. Remove any other field except `description`.
W_CONVEYOR_ZONE: Widen the flow's `leak.zone` so it covers at least one slat of the belt, or use a smaller `dt` for more slats. As it is, the leak never takes anything.
W_DISPATCH_FORECAST: Write the flow's `rate` from time and time series only, without reading levels, flows, resources or the bus, so the plan can forecast it. If holding the current rate over the horizon is intended, the warning can stay.
W_DISPATCH_MIXED: Mark the level's `storage` as `optimal`, with a declared `setpoint` variable, so the plan drives it. If the storage should keep its own rule, the warning can stay.
W_INVESTMENT_NO_CAPEX: Set `"capex": true` on the entries under `fixed_costs` that are the investment, in the model or through a scenario override of `fixed_costs.<name>.capex`.
W_INVESTMENT_ONE_SCENARIO: Add a scenario under `scenarios` to compare with the `investment.baseline`. With only the baseline, the investment case has nothing to compare.
W_NO_CHARGER: Add a charger under `chargers` of the kind the strategy needs (`swap` for `swap`, else `charge`) on a node the fleet can reach from its `home`. Or join the charger's node to the network, and check the battery's `chargers` list allows it.
W_SEGMENT_DEADLOCK_RISK: Make the segment one-way (`two_way` false), give it a `capacity` of 2 or more, or add a passing place. If vehicles never meet head-on there, the warning can stay.
W_TENDING_DEADLOCK: Raise the arm fleet's `count` above 1, set `exchange` on the fleet, put a BUFFER before the machine, or set the station's `tending` `cobot` to `stay`.
W_TENDING_STAY: Set the station's `tending` `cobot` to `leave`, or carry the part on from the station with a TRANSPORT on the same arm fleet.
W_TENDING_UNUSED: Set `arm` to a motion profile on the fleet that tends the station, or remove `tending` from the STATION. Without an arm fleet it is served as a plain station.
W_TRANSPORT_KEEP_UNFREED: Add a RELEASE naming the fleet where the vehicle should go free, or a later TRANSPORT on that fleet without `keep_vehicle`. Otherwise drop `keep_vehicle` from the block.
# Expressions, stocks and flows
E_ASSIGN_KEY: Give each entry in the ASSIGN block's `assignments` only the keys `target` and `expr`. A misspelt key, such as `value` for `expr`, is otherwise dropped; rename or remove it.
E_AUX_CYCLE: Break the loop the message lists in `auxiliaries`: make one of those auxiliaries read a level (a stock) or a delay instead of another auxiliary in the cycle.
E_AUX_NAME: Give every entry in `auxiliaries` a non-empty `name`, and rename one of any two auxiliaries that share a name.
E_AUX_UNKNOWN: Declare the auxiliary in the `auxiliaries` section, or correct the name in the `AUX("...")` call. Names are case sensitive.
E_BUFFER_LEVEL: Declare the level named in the BUFFER's `level` parameter in the `levels` section, correct the name, or clear the `level` field.
E_CHARGE: On the CHARGE block, set `sharing` to `equal` or `first_come` and `efficiency` to a number above 0 and at most 1. If `leave_when_full` is false, also set a `time_limit`.
E_CONVERSION_CURVE: Fix the substrate in `conversions.<name>.substrates`: set `yield` to a number of 0 or more. Give either a positive `half_life` or a `curve` of `[age, cumulative fraction]` knots with increasing ages that ends at `1.0`.
E_CONVERSION_INTERVAL: Set the conversion's `cohort_interval` to a positive number, or leave it out for the default of 1.
E_CONVERSION_NAME: Make every entry in `conversions` an object with a non-empty `name`, and rename one of any two conversions that share a name.
E_CONVERSION_SUBSTRATE: Give the conversion at least one entry in `substrates`, each with a non-empty `name`, and rename one of any two substrates that share a name.
E_CONVERSION_UNKNOWN: Declare the conversion in the `conversions` section, or correct the name in the `CONV("...")` call. Names are case sensitive.
E_CONVEYOR_GRID: Add the system-dynamics grid to the model, for example `"simulation": {"sd": {"dt": 0.25}}`, or remove `conveyor` from the level.
E_CONVEYOR_LEAK: Set the leak flow's `from` to a conveyor level, give its `leak` a `fraction`, and keep `zone` as `[start, end]` with `0 <= start < end <= 1`. Otherwise drop `leak` from the flow.
E_CONVEYOR_METHOD: Set `simulation.sd.method` to `euler`, or remove the conveyor. A conveyor moves one slat per DT, which `rk4` cannot do.
E_CONVEYOR_OUTFLOW: Give the conveyor exactly one outflow without `leak` and mark the other outflows with `leak`. Set that primary outflow's `rate` to empty, since it takes what leaves the belt.
E_CONVEYOR_PARAM: On the CONVEYOR block, set `speed` and `spacing` to positive numbers and `length` to a number of 0 or more. With no `length`, set both `from_node` and `to_node` to declared `network` nodes; declare any `exit_resource` under `resources`.
E_CONVEYOR_SPEC: Give the conveyor level a `conveyor.transit_time` (a number or an expression), and set `leak_kind` to one of the kinds the hint lists, or leave it out.
E_CONVEYOR_STOP: Write each entry of the CONVEYOR's `stops` as `{at, time, resource}` only, with `at` between 0 and `length` (both excluded). Make `time` a number of 0 or more or an expression, and declare any `resource` under `resources`.
E_CONVEYOR_WRITE: Do not write a conveyor level directly. Point the TRANSFER, agent transfer action or BUFFER `level` at an ordinary level, drop it from `initial_state.levels`, and give each element its own conveyor instead of `dims`.
E_DASHBOARD_SERIES: Give a `kpi` or `gauge` widget an `expr` and no `series`. Give a `line` widget 1-4 and a `bar` widget 1-8 `series` entries, each with a unique non-empty `label`, and no `expr`.
E_DELAY_NAME: Give every entry in `delays` a non-empty `name`. Rename one of any two delays that share a name, or whose hidden stage names clash, such as `f` and the inline `f#1`.
E_DELAY_SPEC: Set the delay's `kind` to a known kind and fill in its required fields (`input` and `delay_time` for most kinds). A numeric `delay_time` must be greater than 0.
E_DELAY_UNKNOWN: Declare the delay in the `delays` section, or correct the name in the `DELAY("...")` call. Names are case sensitive.
E_DUPLICATE_REALITY_CHECK: Rename one of the two entries in `reality_checks` that share a name. Each reality check needs a unique `name`.
E_DUPLICATE_VARIABLE: Rename one of the two entries in `variables` that share a name, or delete the duplicate. Expressions and overrides refer to a variable by its name.
E_ENERGY_PRICE: Set `simulation.energy_price` to a number, such as `0.18` money per kWh, or to an expression over declared variables that compiles.
E_FIXED_COST_AMOUNT: Set the fixed cost's `amount` to a number, or to an expression over variables and model data that compiles, such as `capacity_kwh * 600`.
E_FIXED_COST_NAME: Make every entry in `fixed_costs` an object with a non-empty `name`, and rename one of any two fixed costs that share a name.
E_FLOW_BIFLOW: Set the flow's `biflow` to `true` or `false`. For a bi-directional flow, give the levels it joins `"min": null` and no capacity or power limit, or split it into two one-way flows.
E_FLOW_COMPONENT: Set the flow's `components` to an object of component name to share, such as `{"ch4": 0.52}`, or leave it out to carry the source level's own blend.
E_FLOW_EFFICIENCY: Set the flow's `efficiency` to a number above 0 and at most 1, or leave it out for a lossless flow.
E_FLOW_LEVEL: Set the flow's `from` and `to` to levels declared in `levels`, or leave the field out for a source or sink outside the model.
E_FLOW_MIN_RATE: Set the flow's `min_rate` to a number of 0 or more, or leave it out.
E_FLOW_NAME: Give every entry in `flows` a non-empty `name`, and rename one of any two flows that share a name.
E_FLOW_PEAK_PRICE: Set the flow's `peak_price` to a number (money per unit of peak rate), or leave it out. A time-varying tariff goes in `price`.
E_FLOW_TAG: Set the flow's `tag` to a string and its `priority` to a number. Give every flow that shares a tag the same `rate_time_unit`.
E_FLOW_THRESHOLD: Give each entry in the flow's `thresholds` a numeric `value`, a `direction` of `falling`, `rising` or `both`, and a numeric `cost` if any.
E_FLOW_UNKNOWN: Declare the flow in the `flows` section, or correct the name in the `FLOW("...")` call. Names are case sensitive.
E_LEVEL_BOUNDS: Give the level's `capacity`, `min`, `initial`, `max_inflow` and `max_outflow` numeric values, keep `capacity` at or above `min`, and start `initial` inside them. A value in `initial_state.levels` must be a number.
E_LEVEL_COMPONENT: Give each of the level's `components` a unique non-empty `name` and an `initial_fraction` between 0 and 1, and set `blend_tolerance` to a positive number.
E_LEVEL_NAME: Give every entry in `levels` a non-empty `name`, and rename one of any two levels that share a name.
E_LEVEL_THRESHOLD: Give each entry in the level's `thresholds` a numeric `value`, a `direction` of `falling`, `rising` or `both`, and a numeric `cost` if any.
E_LEVEL_UNKNOWN: Declare the level in the `levels` section, or correct the name in the `LEVEL("...")` call, the TRANSFER's `level`, the agent's transfer action or `initial_state.levels`.
E_LOOP_GRID: Add the system-dynamics grid before asking for loop dominance, for example `"simulation": {"sd": {"dt": 0.25, "method": "euler"}}`.
E_POWER_UNKNOWN: Declare the name in `resources`, `fleets` or `chargers`, or correct the name in the `POWER("...")` call. Names are case sensitive.
E_REALITY_CHECK_INPUT: Set the reality check's `inputs` to an object of override path to value, such as `{"auxiliaries.Workforce.expr": "0"}`, with no empty path.
E_REALITY_CHECK_NAME: Give every entry in `reality_checks` a non-empty `name`.
E_REALITY_CHECK_WHEN: Set the reality check's `when` to `always`, `final`, `after <t>`, `before <t>`, `between <t1> and <t2>` (t1 not after t2), or `eventually`, alone or before one of those windows.
E_SD_DELAY_ORDER: Give the delay's `order` as a literal integer of 1 or more, such as `DELAYN(x, tau, 3)`. The number of stages is fixed when the model is built, so an expression or a fraction cannot set it.
E_SD_FUNCTION_CONTEXT: Move the SMOOTH or DELAY call into a level's initial value, a flow rate, an auxiliary or a delay field. Read its result in the block or price with `AUX()`, `LEVEL()` or `DELAY()`.
E_SD_REQUIRES_DT: Add a step grid, such as `"simulation": {"sd": {"dt": 0.25}}`, since material and information delays integrate hidden stocks. Or use a pipeline delay (`DELAYP`), which is exact without one.
E_SD_RK4_RANDOM: Set `simulation.sd.method` to `euler`, or move the random draw out of the flow rate into a variable an ASSIGN sets. RK4 evaluates a dynamic rate four times per step.
E_SD_SETTINGS: Make `simulation.sd` an object with a numeric `dt` above 0, a `method` from the listed ones and a `time_unit` of `base` or a known unit. Remove any other key, such as a misspelt one.
E_SERIES_MODE: Set the series' `interpolation` and `beyond` to one of the values the hint lists. A text `period` must be `daily`, `weekly` or `yearly`; otherwise give a number.
E_SERIES_NAME: Give every entry in `timeseries_inputs` a non-empty `name`, and rename one of two series that share a name. `TS()` calls find a series by its name.
E_SERIES_NO_CALENDAR: Add `simulation.calendar` with a `start` (and a `timezone`) so real timestamps can be placed on the model clock. Or give the series plain numeric `times` instead.
E_SERIES_UNKNOWN: Declare the series under `timeseries_inputs`, or correct the name in the `TS("name")` call. Names are case sensitive.
E_SUBSCRIPT_AGGREGATE: Write a one-argument `SUM(x)` or `MEAN(x)` over a `[*]` index, such as `SUM(LEVEL("Pop[*]"))`, in a level, flow, auxiliary or delay. Read the result elsewhere with `AUX()`.
E_SUBSCRIPT_DIM: Give the record the `dims` its `[dim]` indexes name, list each dimension once, and keep `[*]` inside SUM or MEAN. A flow joining a subscripted level needs that level's `dims`.
E_SUBSCRIPT_ELEMENT: Make the record's `elements` an object keyed by element combinations of its own `dims`. Remove or rename overrides whose key matches no element.
E_SUBSCRIPT_MAP: Point the subscript's `map_to` at another declared subscript. Give `elements` one target element per element, or make both subscripts the same size so they map by position.
E_SUBSCRIPT_NAME: Give each entry in `subscripts` a unique identifier as `name` and a non-empty list of unique `elements`. Elements may not hold `[`, `]`, `,` or `*`, nor share a subscript's name.
E_SUBSCRIPT_RANGE: Set the subscript's `parent` to another declared subscript that holds every one of its elements, or remove `parent`. Add any missing elements to the parent first.
E_SUBSCRIPT_SIZE: Use fewer or smaller dimensions in the record's `dims`. The product of their element counts must stay within the 200,000 cap.
E_SUBSCRIPT_UNKNOWN: Make the record's `dims` a list of names declared under `subscripts`, or declare the missing subscript there. The hint lists the declared ones.
E_TABLE_KEYS: Pass as many keys as the table has axes: one for a 1-D table, two for a table with `columns`, such as `TABLE("name", k1, k2)`.
E_TABLE_MODE: Set the table's `key_mode` or `column_mode` to one of the listed lookup modes. Use `exact` for text keys, since `step` and `linear` interpolate only numeric keys.
E_TABLE_NAME: Give every entry in `tables` a non-empty `name`, and rename one of two tables that share a name. `TABLE()` calls find a table by its name.
E_TABLE_UNKNOWN: Declare the table under `tables`, or correct the name in the `TABLE("name", ...)` call. The hint lists the declared tables.
E_TABLE_VALUE: Make every cell in the table's `values` a number, and make `default` a number or drop it. Text such as `"5"` is not accepted.
E_TIME_UNIT: Set the named `time_unit` (or rate unit key) to one of the known time units, such as `minutes` or `hours`. A block or record field may also take `base`, the model's own unit.
E_TRANSFER_MODE: Set the TRANSFER block's or the agent transfer action's `mode` to `remove` or `add`.
E_UNIT: Set the unit key, such as `simulation.length_unit` or a field's unit, to one of its quantity's units. A record field may also take `base`; the hint names the quantity.
E_UNIT_MISMATCH: Correct the `unit` of the level, flow or auxiliary, or the expression, so expected and found units agree. Or set `simulation.sd.units` to `warn` to report it as a warning only.
E_UNIT_TIME: Write the rate in the model's base time unit, or correct the record's `unit` so the time scales match. Or set `simulation.sd.units` to `warn` to report it as a warning only.
E_VARIABLE_NAME: Rename the variable to a Python identifier: letters, digits and `_`, not starting with a digit. Update every expression and ASSIGN that uses it.
E_VARIABLE_RESERVED: Rename the variable so it does not shadow a reserved name or built-in function, such as `min` or `TNOW`. The hint lists the reserved names.
E_VARIABLE_UNKNOWN: Declare the variable under `variables`, or correct the name in the `VAR("name")` call. Names are case sensitive.
W_BLOCKING_LOOP: Give one buffer in the loop enough places that it never fills, or route one station's output to a block with an unlimited queue. If the loop can never fill, the warning can stay.
W_CONVEYOR_LEVEL_KEY: Remove `capacity`, `min`, `max_inflow` and `max_outflow` from the conveyor level. Set its content limit with `conveyor.capacity` and its inflow per time unit with `conveyor.in_limit`.
W_CONVEYOR_STOP_HELD: Release the resource before the part enters the belt, or remove the stop's `resource`, since the part already holds it. Otherwise the run stops with an error at that stop.
W_CONVEYOR_TRANSIT: Pick a `simulation.sd.dt` that divides the conveyor's `transit_time`, or change the transit time to a multiple of `dt`. Otherwise the belt runs with round(T/dt) slats.
W_FLOW_CLOCK_NO_GRID: Read a timetable from a `timeseries_inputs` entry with `TS("name")`, `interpolation` `step` and `beyond` `repeat`. Or add a step grid with `simulation.sd.dt`; each switch is then late by up to `dt`.
W_LOOP_HYBRID: Treat the scores as covering the SD layer only, since DES blocks and agents are read at initial values. Run `naga loops` on an SD-only model to score every loop; otherwise the warning can stay.
W_RATE_UNIT_CHANGED: Check that the flow's `peak_price` and `thresholds` are per its `rate_time_unit`, and a conversion's numbers per its `time_unit`. Rescale values written per the base unit; if already right, the warning can stay.
W_RATE_UNIT_READ: Give the reading rate and the record it reads with `FLOW()`, `FLOWSUM()` or `CONV()` the same time unit. Or convert the value in the expression.
W_RATE_UNIT_UNCHECKED: Write the name in the `FLOW()`, `FLOWSUM()` or `CONV()` call as a literal, or give every record it may read the same time unit. Then the read can be unit-checked.
W_REF_UNSET: Set the asset parameter that the `POWER()` or `TS()` call takes its name from to the source or series it should read. Or guard the read with a condition on that parameter.
W_SD_RK4_BUS: Set `simulation.sd.method` to `euler` for exact `FLOW()` and `FLOWSUM()` reads. Under RK4 the read lags by one stage; if that is acceptable, the warning can stay.
W_SERIES_BEFORE_START: Check the series' dates against `simulation.calendar.start` and the run length. Trim or shift samples before the start, or a series whose samples all fall after the run ends.
W_SERIES_PERIOD_UNUSED: Add `"beyond": "repeat"` to the series so it cycles every `period`, or drop `period`. Without repeat the series holds its last value.
W_UNIT_MISMATCH: Correct the `unit` of the level, flow or auxiliary, or its expression, so the expected and found units agree. If the mismatch is intended, set `simulation.sd.units` to `off`.
W_UNIT_MISSING: Add a `unit` to the level or flow, such as `people` or `people/day`, so mismatches can be checked. Or set `simulation.sd.units` to `warn`.
W_UNIT_TIME: Write the rate's `unit` in the model's base time unit, such as `people/day` for levels counted per day. If the scaling is intended, the warning can stay.
W_UNIT_UNPARSED: Rewrite the `unit` string in a form the unit parser reads, such as `kg`, `people/day` or `m/s`. Until then the record is not unit-checked.
# Resources and time
E_ARRIVAL_MODE: Set the CREATE block's `arrival_mode` to `Interarrival`, `Schedule` or the orders mode, spelled as the hint lists them. Leave it out to keep the default `Interarrival`.
E_ARRIVAL_SCHEDULE: Give the CREATE block a `schedule_name` that names a schedule declared under `schedules` with `"kind": "arrival"`, or switch `arrival_mode` back to `Interarrival`.
E_CALENDAR_DATE: Write the date as an ISO 8601 timestamp such as `2026-01-05T06:00:00`, or use a number in the base time unit. A dated field needs `simulation.calendar`, and its `end` must come after `start`.
E_CALENDAR_START: Make `simulation.calendar` an object with a `start` written as an ISO 8601 instant, for example `"start": "2026-01-05T06:00:00"`. That instant is what model time 0 stands for.
E_DUPLICATE_FAILURE: Rename one of the two entries in `failures`. Every failure needs a unique `name`.
E_DUPLICATE_SCHEDULE: Rename one of the two entries in `schedules`. Schedule names must be unique, because CREATE blocks and resources refer to a schedule by its name.
E_ENTITY_COST: Set the entity type's `holding_cost` in `entities` to a number of 0 or more, or remove it. Use 0 when work in progress costs nothing to hold.
E_FAILURE_NAME: Give the failure a non-empty `name` in the `failures` section. Each failure needs a unique name.
E_FAILURE_RESOURCES: List at least one resource in the failure's `resources`, naming the resources or fleets it takes down, or remove the failure.
E_FAILURE_RESOURCE_UNKNOWN: Correct the name in the failure's `resources` list, or declare that resource under `resources` (or a fleet under `fleets`) first. The hint lists the declared names.
E_PREEMPT_RESOURCE_SET: Set the preempting PROCESS block's `resource_name` to one member resource instead of the resource set, or drop `preempt`. Preemption interrupts a job on a single resource.
E_RANKING_REQUIRED: Add a `ranking_expr` to the resource, such as `entity.priority`, or change its `discipline` to one that needs no ranking, such as `FIFO`.
E_RESOURCE_CAPACITY: Raise the resource's `capacity` to at least the largest value its `capacity_schedule` asks for, or lower the schedule's values. The hint gives the minimum.
E_RESOURCE_COST: Set the resource's `cost_busy`, `cost_idle` and `cost_per_use` to numbers of 0 or more. Use 0 for no cost.
E_RESOURCE_DISCIPLINE: Set the resource's `discipline` to one of `FIFO`, `LIFO`, `LowestValueFirst`, `HighestValueFirst` or `Plan`, spelled with that case.
E_RESOURCE_POWER: Make the resource's `power` an object of kW per unit, such as `{"busy": 5.0, "idle": 1.0}`, with every value a number of 0 or more.
E_RESOURCE_SCHEDULE: Correct the resource's `capacity_schedule` to the name of a schedule declared under `schedules` with `"kind": "capacity"`, or declare that schedule. An arrival schedule cannot set capacity.
E_SCHEDULE_EXCEPTION: Give each entry in the schedule's `exceptions` a `start` written as an ISO date such as `2026-12-25`, and an `end`, if any, on or after `start`.
E_SCHEDULE_KIND: Set the schedule's `kind` to `arrival` or `capacity`. Leave it out for the default, `arrival`.
E_SCHEDULE_NAME: Give the schedule a non-empty, unique `name` in the `schedules` section.
E_SCHEDULE_NO_CALENDAR: Declare `simulation.calendar` with a `start` (and a `timezone` if needed), or write the schedule as `points` instead of a `weekly` grid. A weekday grid needs real dates.
E_SCHEDULE_POINT: Write each entry in the schedule's `points` as a two-element `[duration, value]` array, with duration above 0 and value 0 or more. A capacity schedule's values must be whole numbers.
E_SCHEDULE_POINTS: Add at least one `[duration, value]` pair to the schedule's `points`, or write it as a `weekly` grid.
E_SCHEDULE_WEEKLY: Fix the schedule's `weekly` grid: use day keys such as `mon` and `sat`, write each band as `["06:00", "14:00", 3]`, and keep the bands of one day from overlapping.
E_SET_EMPTY: List at least one declared resource in the resource set's `members`, or remove the set.
E_SET_MEMBER: Correct the member's name in the resource set's `members`, or declare that resource under `resources` first. The hint lists the declared resources.
E_SET_RULE: Set the resource set's `rule` to one of `PreferredOrder`, `Cyclical`, `Random`, `SmallestNumberBusy` or `LargestRemainingCapacity`.
W_RERANK_UNRANKED: Remove `rerank` from the resource, or change its `discipline` to `LowestValueFirst` or `HighestValueFirst` with a `ranking_expr`. Only a ranked queue can be re-ranked.
W_SCHEDULE_EXCEPTION_UNUSED: Check the exception's dates against the run window that the hint gives. Move the dates, lengthen the run, or remove the exception if it is no longer needed.
# Scenarios, checks and the editor
E_ANNOTATION_ID: Give the annotation a non-empty `id` in the `annotations` section. Every annotation needs its own unique id.
E_ANNOTATION_KIND: Set the annotation's `kind` to `text` or `frame`. No other kind exists, and an annotation without a `kind` gets this error too.
E_ANNOTATION_SHAPE: Give a `text` annotation a `position` of `[x, y]`, and a `frame` annotation a `rect` of `[x, y, width, height]`.
E_ASSERTION_CONFIDENCE: Set the assertion's `confidence` to a number strictly between 0 and 1, such as `0.95`, or remove it to use that default.
E_ASSERTION_KPI: Fill in the assertion's `kpi` with a KPI key the run produces, such as `system/Throughput (per time unit)`.
E_ASSERTION_NAME: Give the assertion a non-empty `name` in the `assertions` section. Names must be unique within the section.
E_ASSERTION_OP: Set the assertion's `op` to one of `<`, `<=`, `>`, `>=` or `==`. The field is required and takes no other operator.
E_ASSERTION_REPLICATIONS: Set the assertion's `replications` to a whole number of at least 1, or remove it to use the default of 5.
E_BACKGROUND: Fix the `background` section: an object with a non-empty `path`, `position` as `[x, y]`, `scale` above 0, `opacity` from 0 to 1 and `locked` true or false. Use `null` for no background.
E_DASHBOARD_CONTROL: Give the control in `dashboard.controls` a unique `name`, a `kind` of `slider` or `decision`, a known target and `min` below `max`. A decision's `policy` needs a Policy-mode DECIDE with one option per outgoing link.
E_DASHBOARD_DEFINITION: Fix the `dashboard` section at the path the message names. Drop unknown keys, keep lists and text within their ceilings, and make every page item's `ref` name an existing input, result, widget or control.
E_DASHBOARD_EXPR: Rewrite the widget's `expr` so it compiles and names only declared variables and resources. It may not draw a random number, and a constant must be a number.
E_DASHBOARD_GAUGE: Give the gauge widget a numeric `max` greater than `min`, and list its `thresholds` as ascending numbers within `min` and `max`.
E_DASHBOARD_INPUT: Point the input's `path` at a value an override can change in this model, and set its `kind` to match that value: `number`, `switch` for true/false, or `choice`.
E_DASHBOARD_KIND: Set the widget's `kind` to one of `kpi`, `gauge`, `line` or `bar` in `dashboard.widgets`.
E_DASHBOARD_NAME: Give the dashboard widget a non-empty `name` that no other widget in `dashboard.widgets` uses.
E_DUPLICATE_ANNOTATION: Rename one of the two annotations in the `annotations` section so every `id` is unique.
E_DUPLICATE_ASSERTION: Rename one of the two assertions in the `assertions` section so every `name` is unique.
E_DUPLICATE_PATH_CHECK: Rename one of the two path checks in the `path_checks` section so every `name` is unique.
E_DUPLICATE_SCENARIO: Rename one of the two scenarios in the `scenarios` section so every `name` is unique.
E_PATH_CHECK_BLOCK: Set the path check's `block`, or an `order` rule's `first` and `then`, to names of blocks in the model. `first` and `then` must name different blocks.
E_PATH_CHECK_COUNT: Set the `at_most` path check's `count` to a whole number of 0 or more: the most visits an entity may make to the block.
E_PATH_CHECK_NAME: Give the path check a non-empty `name` in the `path_checks` section. Names must be unique within the section.
E_PATH_CHECK_RULE: Make `path_checks` a list of objects whose `rule` is `order`, `never`, `at_most` or `always`. Keep only the keys that rule takes, and give `e_type` as an entity type name.
E_SCENARIO_GOAL: Write the scenario's `goal` as text of at most 500 characters, such as one sentence naming the question it answers.
E_SCENARIO_NAME: Give the scenario a non-empty `name` in the `scenarios` section. Names must be unique within the section.
W_BACKGROUND_MISSING: Put the image at the `background.path` the model names, or correct the path. A relative path is read from the model file's folder.
# Blocks, links and routing
E_BATCH_CARRIER_CAPACITY: Make the BATCH's `carrier_capacity` expression return 0 or more for every carrier. Check the carrier attributes it reads, or wrap it in `max(0, ...)`.
E_BATCH_MEMBER_TIME: Set the BATCH's `carrier_type` to the carrier's entity type, or clear `member_time` and `carrier_capacity`. Both apply only to a carrier BATCH.
E_BUFFER_CAPACITY: Set the BUFFER's `capacity`, or the SEIZE/PROCESS `queue_capacity`, to a count of 0 or more. For an unlimited queue, leave `queue_capacity` at 0 and draw no BUFFER.
E_DECIDE_FALLBACK: Set the DECIDE's `fallback` to `first`, `random` or `shortest`. The fallback picks the branch when no policy answers.
E_DECIDE_POLICY_NAME: Give the Policy-mode DECIDE a non-empty `policy`, the name of its decision point, for example `routing`. Several DECIDE blocks may share one name.
E_DECIDE_QUEUE_EXPRS: Give `queue_exprs` exactly one expression per outgoing link, in branch order, or choose a `fallback` other than `shortest`.
E_DETERMINISTIC_NO_MEAN: Use a distribution and `dist_params` with a finite mean (for example an `Exponential` with a positive rate), or turn off `simulation.deterministic`. Deterministic mode replaces each draw by its mean.
E_DISPOSE_HAS_OUTPUT: Remove the DISPOSE block's outgoing link. A DISPOSE ends the flow; to continue after it, link from the block before it instead.
E_DISTRIBUTION_MISSING: Add a `distribution` to the duration spec the message names: the link's `delay`, or a failure's `uptime` or `downtime`. For a link, you can drop the `delay` instead.
E_DUPLICATE_STATION: Rename one of the two STATION blocks or change its `station` value. A station name identifies exactly one block.
E_HOLD_SPACE_TARGET: Point `hold_until_space` at a real space: link the block, or name in `space_of`, a BUFFER with `capacity` above 0 or a SEIZE/PROCESS with `queue_capacity` above 0. Otherwise clear `hold_until_space`.
E_MATCH_ATTRIBUTE: Set the MATCH's `match_on` to an attribute name, such as `order` or `entity.order`, not starting with `_`. Leave it empty to match by entity type alone.
E_ON_PREEMPT: Set `on_preempt` to `resume` (the interrupted job keeps its remaining service) or `restart` (it starts over).
E_ORDER_ENTITY_TYPE: Declare the entity type under `entities`, or correct the order's `entity_type` to a declared one. The hint lists the declared types.
E_ORDER_ID: Give every order a unique, non-empty `id`, such as `WO-1001`. Rename one of two orders that share an id.
E_ORDER_SOURCE: Set the order's `source` to a CREATE block whose `arrival_mode` is `Orders`, or drop `source` to use the first such block.
E_PREEMPT_BLOCKING: Drop `preempt` on this block, or give the downstream BUFFER enough `capacity` that the station holding the resource never blocks after service.
E_PREEMPT_SCOPE: Use `preempt` only on a PROCESS with action `Seize Delay Release`, and make every other block seizing that resource one too. Otherwise drop `preempt`.
E_RECORD_KIND: Set the RECORD's `kind` to one of the kinds the hint lists, such as `count`. Check the spelling and case.
E_RECORD_KIND_CONFLICT: Give every RECORD writing the same `stat_name` the same `kind`, or record the second kind into a different `stat_name`.
E_RECORD_STAT_NAME: Set the RECORD's `stat_name` to the name of the statistic it collects. The name keys the results.
E_ROUTE_NO_SEQUENCES: Declare at least one sequence under `sequences` and a STATION block for each step, or remove the ROUTE block.
E_SEPARATE_CARRIER: Set the SEPARATE's `carrier` to `dispose` (the batch ends here) or `keep` (the carrier leaves by a second link).
E_SEPARATE_CARRIER_LINK: Give the SEPARATE exactly two outgoing links: link 0 for the members, link 1 for the empty carrier. Or set `carrier` to `dispose`.
E_SEPARATE_MEMBER_TIME: Set the SEPARATE's `carrier` to `keep`, or clear `member_time`. The per-member time applies only when the carrier lives on.
E_SEPARATE_RELEASE_ORDER: Set the SEPARATE's `release_order` to `fifo` (load order) or `lifo` (last loaded first out).
E_SEQUENCE_EMPTY: List at least one step in the sequence's `steps`, such as `{"station": "Mill"}` naming a STATION block, or delete the sequence.
E_SEQUENCE_NAME: Give every entry under `sequences` a unique, non-empty `name`. Rename one of two sequences that share a name.
E_SEQUENCE_STATION_UNKNOWN: Add a STATION block for the named station, or correct the step's `station` (or the block's `destination`) to a declared one. The hint lists the declared stations.
E_SEQUENCE_STEP: Write each step as an object with a non-empty `station`. Its optional `values` must be an object of attribute names mapping to a number, string or boolean.
E_SEQUENCE_UNKNOWN: Declare the sequence under `sequences`, or correct the `sequence` on the CREATE, ASSIGN or order to a declared name.
W_AVAILABLE_IS_CAPACITY: Use `IDLE("name")` for the free units, or `NR("name")` for the busy ones, in the `balk_if` or DECIDE condition. Keep `AVAILABLE()` only if you mean the capacity in force.
W_CREATE_NO_ORDERS: Add orders under `orders` whose `source` names this CREATE, or switch its `arrival_mode` back to `Interarrival`.
W_EMPIRICAL_CONTINUOUS: Switch to `EmpiricalDiscrete`, or give `values` one more entry than `probs` so they act as bin edges for true continuous sampling.
W_LONG_NAME: Shorten the block or SUBMODEL instance name to 20 characters or fewer, and move the detail to its description. If the wrapped name is fine, the warning can stay.
W_ORDERS_NOT_CONSUMED: Set a CREATE block's `arrival_mode` to `Orders` so it releases the orders, or remove the `orders` section.
W_ORDER_DUE_BEFORE_RELEASE: Set the order's `due` to an absolute time after its `release`, in the model's base unit. A due date is not a lead time.
W_ORDER_FIELD: Rename the order attribute, or map the bound column to another name with the binding's `map`, so it is a valid identifier and not a field of the entity itself.
W_PLAN_STATION_UNKNOWN: Correct the station name in the order's `plan`, or add a STATION block with that name. The hint lists the declared stations.
W_PREEMPT_NO_PRIORITY: Give the preempting block a lower `priority` number than the blocks whose jobs it should interrupt, or drop `preempt`.
W_QUEUE_LEAVE_LINK_UNUSED: Set `balk_if` or `renege_after` on the block, or remove its second outgoing link.
W_STATION_UNUSED: Add the station to a sequence's `steps`, or delete the STATION block if no route visits it.
