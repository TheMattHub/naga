Use CONVEYOR for a belt where length, speed and spacing matter. Parts take time to travel, the belt holds a limited number of them, and a blocked exit backs parts up. If the stretch is only a wait with no capacity, a [DELAY](delay.md) is simpler. For a vehicle that picks up and carries a part, use [TRANSPORT](transport.md).

## Behaviour and rules

The belt holds `floor(length / spacing)` parts, at least one. A part takes `length / speed` to cross. Consecutive parts enter at least `spacing / speed` apart, so the belt can never feed faster than that. If `length` is 0, the length is the network distance between `from_node` and `to_node`.

A full belt, or one whose entry rhythm is not met, holds back the station that feeds it. That station keeps its finished part and its resource. This is blocking after service, the same rule that [BUFFER](buffer.md) follows.

At the exit, a part leaves only when `exit_resource` (if set) is taken and any downstream belt has room. The part carries the resource on, so release it downstream, for example with a PROCESS whose action is Delay Release. With neither, the belt is a delay with a capacity.

With `accumulating` on, a blocked part stops at the end and the parts behind pack against it. With it off, the whole belt stops. Optional `stops` halt each part at a point, `diverts` send some parts out sideways on the links after the first, and `merges` let parts from another block join. The `conveyor_summary` frame reports occupancy, blocked time and transit time.
