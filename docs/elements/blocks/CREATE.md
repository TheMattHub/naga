Use CREATE as the source of every flow: it creates the parts, orders, customers or jobs that enter the model. A model needs at least one CREATE. It can draw gaps from a distribution, follow an arrival schedule that changes over the day, or release a book of orders. To bring in material rather than entities, use a flow on a level.

## Behaviour and rules

In the default mode `Interarrival`, the first arrival comes at `start_time`, with no gap before it. Each later gap is drawn from the distribution. A negative draw is cut to 0 and a warning is logged. A constant gap of 0 with unlimited arrivals is an error, because time could never advance.

`max_arrivals` counts arrival events, not entities. Each event creates `entities_per_arrival` entities. A value of 0 means unlimited. The `e_type` is stamped on every entity the block creates.

In the mode `Schedule`, arrivals follow the named arrival schedule as a random process whose rate changes with time. In a deterministic run, arrival number k comes when the schedule's accumulated rate reaches k. In the mode `Orders`, the block releases the orders assigned to it, each at its release time, and ignores the distribution, `max_arrivals` and `entities_per_arrival`. A release before `start_time` waits for it.

The `sequence` is a literal routing name that every entity starts at step 0. When `population` is set, each entity also gets a new agent in that population.
