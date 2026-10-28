These sections describe the discrete flow of entities. `blocks` lists the steps and `links` joins them. `sequences` gives entities their own routes through stations. `orders` is a book of jobs, with release times and due dates, that a CREATE block releases. A simple model needs only `blocks` and `links`.

## Behaviour and rules

The order of `links` is the branch order. The first link out of a DECIDE is branch 0, and a link out of a PROCESS with balking is index 1 for the entities that leave. Several links may join the same pair of blocks. A link can carry a `delay`, sampled for each entity that crosses it.

A sequence is a list of steps, each naming a STATION and the attributes to set there. An entity starts a sequence at step 0 when its CREATE block names it, or when an ASSIGN writes `entity.sequence`. A ROUTE block then walks the steps. A step that names a station without a STATION block is an error.

Orders only take effect when a CREATE block is in the `Orders` arrival mode. Each order releases `quantity` entities at its `release` time. The `due` time is absolute, not a lead time. An order may override the entity type, the sequence and the priority.

A scenario can override a block parameter with the path `blocks.<name>.params.<parameter>`. Every flow needs a CREATE and a DISPOSE, and a check reports a model that lacks one.
