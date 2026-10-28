Use BUFFER to put a limit on the space between two stations. Without it, a queue in front of a machine is unlimited. With a BUFFER, a full buffer holds back the machine upstream, which is how a line with limited work in process behaves. To limit one station's own queue only, use that station's `queue_capacity`.

## Behaviour and rules

A part takes one of the `capacity` places on the way in and keeps it until the next station accepts the part. If every place is taken, the upstream machine keeps its finished part, and its resource, until a place is free. This is called blocking after service.

A capacity of 0 is allowed. It means no place at all: the part never leaves the machine before the next station can start it. This is the classic zero-buffer line. There is no unlimited buffer. If you want an unlimited queue, do not draw a buffer.

The optional `level` mirrors the buffer's content into a declared level, so an asset can draw a fill indicator. It cannot be a conveyor level. The `buffer_summary` frame reports the average and maximum content and the share of time the buffer was full. It also reports the shares of time the upstream machine was blocked and the downstream one starved.
