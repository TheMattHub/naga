# Recipes: How do I...?

Each recipe answers one modelling question with a tiny model, the output it prints and a short explanation. The questions come first from the distributions, functions and blocks that no bundled model uses yet. Pick the question that is closest to yours, and copy the script as a start.

## Service and repair times

Which family to use for a service, repair or cycle time, and why.

- [How do I model handling times with an occasional very long one?](lognormal-tail.md)
- [How do I model a job made of several short steps?](erlang-steps.md)
- [How do I model repair times with a long tail?](repair-tail.md)
- [How do I model an approval or reaction time that is never negative?](decision-delay.md)
- [How do I keep a skewed time between a minimum and a maximum?](bounded-yield.md)
- [How do I use a histogram from a time study as a service time?](histogram-times.md)
- [How do I model a time that is a fixed part plus a random part?](formula-time.md)
- [How do I stop a cycle time from going negative?](bounded-cycle.md)
- [How do I model a correction time that grows with a positioning error?](positioning-error.md)
- [What happens to a queue when service times have wild outliers?](wild-outliers.md)

## Failures and arrivals

Machine lifetimes and arrivals that follow a plan.

- [How do I model a machine that wears out?](weibull-wear-out.md)
- [How do I run arrivals from a fixed timetable?](timetable-arrivals.md)

## Counts and chance

Whole-number counts: lines, defects, retests, samples and order sizes.

- [How do I model the number of lines on an order?](poisson-lines.md)
- [How do I count the defects in a batch?](binomial-defects.md)
- [How do I model retesting until a part passes?](retest-attempts.md)
- [How do I model inspecting a sample from a finite lot?](lot-sample.md)
- [How do I draw order sizes from my own data?](order-sizes.md)

## Delays, smoothing and forecasts

System-dynamics functions that delay, smooth and extrapolate a signal.

- [How do I add a first-order information delay to a forecast?](forecast-lag.md)
- [How do I delay goods in transit by a fixed time or a spread of times?](transit-delay.md)
- [How do I smooth a noisy signal?](smooth-noise.md)
- [How do I extrapolate a growing trend?](trend-forecast.md)

## Rates, tests and money

Cycles, test inputs, discounting, safe division and trigonometry in rates.

- [How do I add a daily or weekly cycle to a rate?](daily-cycle.md)
- [How do I test a model with a step, a ramp and a pulse?](test-inputs.md)
- [How do I discount a stream of cash?](discounted-cash.md)
- [How do I stop a ratio dividing by zero?](safe-ratio.md)
- [How do I turn a heading and a distance into x and y?](bearing.md)

## Agents

Reading the agents around an agent, in continuous space and on a grid.

- [How do I let an agent react to the agents near it?](near-agents.md)
- [How do I let an agent read the agents in the cells around it?](neighbour-grid.md)

## Flow blocks

Blocks that shape a flow: buffers, belts, copies, pairs, pallets, custom rules and chargers.

- [How do I limit the work waiting between two machines?](buffer-wip.md)
- [How do I move parts along a belt?](belt-transit.md)
- [How do I turn one order into several jobs?](order-copies.md)
- [How do I make two kinds of entity wait for each other?](pair-flows.md)
- [How do I pack parts on a pallet and unpack them later?](unpack-pallet.md)
- [How do I route entities by a rule I write in Python?](custom-routing.md)
- [How do I share a site's power limit between chargers?](site-power.md)

Longer walk-throughs are on the [learning path](../../learn/index.md), and every element has a page under the [reference](../../reference/blocks/index.md).
