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

## Known ceilings

- A `{"$arg": ...}` written inside a **top-level record** only resolves where that record is cloned
  for an instance (a `$` template). Use the definition's `scoped` overrides for a record that is
  not `$`-scoped.
- A link key in `remove` drops every parent link between those two blocks, whatever its ports.
