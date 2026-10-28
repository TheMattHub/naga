Use these functions inside a flow rate, an auxiliary or a delay input of a stock-and-flow model. The `SMOOTH` family models how a decision maker perceives a changing value. The `DELAY` family models material in transit, such as an order that takes time to arrive. The time functions `STEP`, `PULSE` and `RAMP` are test inputs. The rest read, extrapolate or discount a signal.

## Behaviour and rules

`tau` is a time in the model's time unit. `SMOOTH` and `SMOOTH3` are information delays of order 1 and 3. They average the input and do not conserve a quantity. `DELAY1`, `DELAY3` and `DELAYN` are material delays of order 1, 3 and n, where n must be a literal integer of 1 or more. They conserve the quantity: what enters comes out. A higher order gives a sharper step response.

`DELAYP` returns the input exactly `tau` later, from a stored history. The other delays integrate stages and therefore need `simulation.sd`. `DELAYP` does not. Each call in an expression gets its own hidden stages. A call is allowed only in a flow rate, an auxiliary or a delay input, and `tau` must be above 0.

The optional `init` is the value of the output at the start. For the `SMOOTH` and `DELAY` family it defaults to the input's value at time 0, so the model starts in balance. For `DELAYP` it defaults to 0.

`STEP(h, t0)` is 0 before `t0` and `h` from then on. `PULSE(h, t0, w)` is `h` from `t0` for a width of `w`. `RAMP(s, t0)` rises with slope `s` after `t0`, and a third argument holds it from there. `INITIAL(x)` freezes the value `x` had at time 0. `ACTIVE_INITIAL(x, init)` gives `init` while initial values are set up and `x` afterwards.

`TREND` is a fractional rate of change, `FORECAST` extends it ahead by a horizon, and `NPV` discounts a stream at a rate. `SAMPLE_IF_TRUE` holds the last `x` read while its condition was true. `NOISE_NORMAL` is a normal draw clipped to a range, fixed for each call site and seed.
