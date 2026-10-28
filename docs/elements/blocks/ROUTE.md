Use ROUTE for job shops and multi-step routings where each entity carries its own list of stations. The entity jumps to the next station in its sequence, with no drawn link. For a fixed route that every entity follows, ordinary links are simpler. To pick a link by chance or condition, use [DECIDE](decide.md). To carry the entity with a vehicle, use [TRANSPORT](transport.md).

## Behaviour and rules

The entity must carry a `sequence`, a name from the `sequences` section, and a `step` index. Set the sequence on the CREATE block or with an ASSIGN to `entity.sequence`. An entity with no sequence, or one with an unknown name, stops the run with an error. A step that names a station without a STATION block does too.

At each visit, the block copies the step's `values` onto the entity, advances `step` and sends the entity to the step's STATION. It then continues from that station's link. When the sequence is finished, the entity leaves ROUTE by its own outgoing link, so a DISPOSE can follow.

In `time` travel mode, the transfer time is the block's distribution, which can read `from_station` and `to_station`, for example from a distance table. In `network` travel mode, the time is the shortest path on the path network at the `speed`. The destination station needs a `node`, and the speed must be above 0. An entity with no station yet arrives at once.
