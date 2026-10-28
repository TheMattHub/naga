Use SIGNAL to broadcast an event that releases entities waiting at [HOLD](hold.md) blocks: a shift start, a green light, a truck arrival. Send a dummy entity through it from a CREATE on a schedule, or let a real entity trigger it as it passes. To release entities on a state of the model, use a HOLD in `Condition` mode with no SIGNAL.

## Behaviour and rules

After its optional delay, the block fires the signal named `signal_name`. Every HOLD that is waiting on that name at that moment is released. The signal then resets at once. It is a broadcast that fires on a single edge, so it has no memory.

An entity that reaches a HOLD after the signal has fired waits for the next one. A signal that nobody waits on is lost, and there is no error. The name must match the HOLD's `signal_name` exactly.

The entity that passes through SIGNAL continues unchanged along the single outgoing link after the delay. A sampled delay below 0 is cut to 0.
