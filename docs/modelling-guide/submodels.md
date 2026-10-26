# Submodels

A submodel is a reusable piece of model stored **once**, as a named definition, and used
as many times as needed through a **SUBMODEL** instance block. It is purely an authoring
concept: `core/submodels.flatten()` expands every instance into the definition's own blocks before
the engine builds anything, so a submodel costs the engine nothing and a model with none behaves
exactly as before. The bundled model `two_cell_line` is the worked example.

## Building a definition

A definition (an entry of `submodels`) has:

- **Parameters**: named values an instance can override, each with a default.
- **Blocks and links**: any block type, CREATE/DISPOSE included (a definition can be a
  self-contained source or sink), and instances of *other* definitions (a definition cycle is a
  validation error, not a runtime hang).
- **SUB_ENTRY / SUB_EXIT**: port blocks that never run. SUB_ENTRY names an input port (exactly one
  outgoing link inside the definition); SUB_EXIT names an output port (incoming links only).

## Flattening rules

| Rule | What happens |
|---|---|
| Names | Every expanded block is renamed `<instance>/<inner>`; nesting composes (`a/b/inner`). KPIs, traces, user statistics and validation problems all use these flattened names. |
| Links | A link's `source_port`/`target_port` (defaulting to the definition's single port on that side) is rewired transitively through SUB_ENTRY/SUB_EXIT; a definition that only forwards (entry straight to exit) disappears completely. |
| Link delays | A rewired hop keeps the *first* delay found along the path it replaced. |
| Branch order | Preserved: links are emitted in layout order, each rewiring expands in port order. |
| Parameters | An instance's `arguments`, merged over the definition's own parameter defaults, become `locals` on every block the instance expands to -- read like a model variable, but scoped to that instance ([Expressions and variables](expressions-variables.md)). A string argument is an expression, evaluated once at flatten time in the *enclosing* instance's own locals -- unless the parameter's `type` is a literal one, see below. |

## Parameters that are not expressions

An argument read from an expression can only reach a field an expression is allowed in. For
everything else -- a resource or schedule **name**, an **integer** (`BATCH.batch_size`,
`MATCH.number_to_match`), a string **choice** (a distribution family, an action), a **bool** --
write the param value as the object `{"$arg": "<parameter>"}` in the definition:

```jsonc
{"name": "take", "type": "SEIZE",
 "params": {"resource_name": {"$arg": "pool"}, "quantity": 1}}
```

Flattening replaces it with the instance's argument (or the parameter's default) and checks it
against the type the block declares, so a wrong value is a validation problem naming the instance
and the parameter, never a surprise mid-run. It works the same way in a `scoped` override value and
in a nested instance's own `arguments`, so a parameter can be passed further down.

A definition parameter may declare a `type`. `name`, `choice` and `string` are **literal**: the
string is the word itself, not an expression -- which is what lets an argument name a record. A
`name` parameter may add `refers_to` (`resource`, `resource_set`, `schedule`, `table`, `sequence`,
`entity`); the instance form then offers the records the model declares, and a name that does not
exist is reported. Every other type keeps the expression rule.

## `$` scoping

Resources, resource sets, schedules, stores, signals and user statistics named inside a
definition are **global** by default -- every instance shares them. A name written with a leading
`$` is **per-instance** instead: flattening rewrites `$Machine` to `<instance>/Machine` everywhere
it appears (a bare param value, or a quoted string literal inside an expression like
`NQ("$Machine")`).

When the `$`-less name (`Machine`) is declared at the top level as a resource, a resource set or a
schedule, that record is the **template**: it is cloned per instance, bringing its capacity
schedule (also cloned) and, for a set, its members (each cloned as a resource in turn). A failure
declared against a template resource is cloned too, with its own per-instance RNG stream.
`schema.validate_model` checks the template exists. Stores and signals have no top-level record, so
`$hold_1` is simply the per-instance store/signal name.

## Inheritance: `extends`

A definition can start from another one. Write `"extends": "<parent>"` in its section and list
only what differs:

```json
"submodels": {
  "Cell":      {"parameters": [{"name": "rate", "default": 1.0}], "blocks": [...], "links": [...]},
  "Fast cell": {"extends": "Cell", "parameters": [{"name": "rate", "default": 3.0}],
                "blocks": [{"name": "work", "type": "DELAY", "params": {...}}],
                "remove": ["inspect"]}
}
```

The effective definition is the parent's with the child merged in, before flattening and
validation:

- **blocks** and **parameters** by name: a child record of the same name replaces the parent's,
  new ones are added;
- **links** by source, target and ports: the child's links are added, one with the same ends
  replaces the parent's;
- **`remove`** drops parent blocks, and every parent link that touched one; an entry written as
  a link key, `"P->SUB_OUT"`, drops the parent's links from `P` to `SUB_OUT` instead, which is how
  a child inserts a step between two kept blocks (`"remove": ["P->SUB_OUT"]` plus links `P->Q`,
  `Q->SUB_OUT`);
- **`scoped`** overrides merge per record; every other field (`info`, `annotations`) is the
  child's.

Chains work (`A` extends `B` extends `C`). A cycle is `E_EXTENDS_CYCLE` and an unknown parent
`E_EXTENDS_UNKNOWN`. The instance form offers the inherited parameters too, and the definition
combo's tooltip names the parent. A definition that others extend cannot be deleted; renaming it
updates their `extends`.

## Repeated blocks: `repeat`

A definition can hold a block group that each instance copies as many times as one of its
parameters says. A sorter with one lane per chute, or a buffer row with one bay per slot, is one
definition whose lane count is an argument, not a chain of fixed-size copies:

```json
"Sorter": {
  "parameters": [{"name": "lanes", "type": "int", "default": 4}],
  "repeat": [{"count": {"$arg": "lanes"}, "blocks": ["lane_{i}", "out_{i}"]}],
  "blocks": [... {"name": "lane_{i}", "type": "PROCESS",
                  "params": {"resource_name": "$Lane {i}", ...}},
             {"name": "out_{i}", "type": "SUB_EXIT", "params": {"port": "lane_{i}"}} ...],
  "links": [{"source": "divert", "target": "lane_{i}"}, {"source": "lane_{i}", "target": "out_{i}"}]
}
```

The rules, applied per instance before anything else reads the definition:

- **Stamping.** Copy k (1 to `count`) of each named block, and of every link that touches one,
  has `{i}` replaced by k in every string inside it. The copies take the group's first place,
  copy by copy, so a DECIDE's branches to the group come out as lane 1, lane 2, and so on.
- **List items.** An item `{"$repeat": <count>, "item": <value>}` in a list param is spliced in as
  `count` copies the same way, which is how a DECIDE gets one condition per lane.
- **Count.** It is an int, a `{"$arg": ...}`, or an expression of the definition's parameters,
  from 0 to 500. A group whose block names hold no `{i}` is **optional**: its count must be 0 or 1,
  which is how an asset has a reject chute and its port only when a parameter asks for one.
- **Ports.** An instance has the ports its own arguments give it. Lowering the count below a wired
  port is refused with `E_SUBMODEL_PORT_UNKNOWN`, never dropped silently.
- **Templates.** `$Lane 7` with no record of that exact name clones the template named
  `Lane {i}`, which is declared once. A failure on it needs `{i}` in its own name too
  (`Jam {i}`), so each copy gets its own. A record whose name holds `{i}` is never live itself.
- **Order.** Copies register in number order, so lane 2 comes before lane 10 in every result frame.

Malformed groups are `E_SUBMODEL_REPEAT`; bad counts are `E_SUBMODEL_REPEAT_COUNT`. The Logistics
library's Sortation asset is the worked example: N lanes and an optional reject chute.

## Known ceilings

- A `{"$arg": ...}` written inside a **top-level record** only resolves where that record is cloned
  for an instance (a `$` template). Use the definition's `scoped` overrides for a record that is
  not `$`-scoped.
- A link key in `remove` drops every parent link between those two blocks, whatever its ports.
- A repeat group has one index, `{i}`, and one level: a group cannot repeat inside another group.
  One `scoped` override serves every copy of a `{i}` template, so copies cannot differ there.
