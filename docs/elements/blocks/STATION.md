Use STATION to give a place in the model a name that a [ROUTE](route.md) or a [TRANSPORT](transport.md) can target. Examples are a machine group in a job shop, a dock and a pick position. It does nothing to the entity beyond marking where it is. Put the PROCESS for that place right after it.

## Behaviour and rules

An entity reaching the block gets its `station` attribute set to the station's name, then moves on along the single outgoing link at once. The block takes no time. It can be reached by a route step, by an ordinary link, or both. The next ROUTE uses `entity.station` as the place the entity travels from.

The name is the block's own name unless `station` gives an alias. The block then answers to both names, and two blocks claiming one name are an error. A PROCESS at the station can read `entity.station`, for example in a table of process times by type and station.

A `node` puts the station on the path network. Travel to and from it is then the shortest path, and `DISTANCE` can name it. The `tending` setting lets a fleet's robot arm load and unload the machine. Inside a submodel the station name is prefixed with the instance name.
