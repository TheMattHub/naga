Use these functions when agents live in space and react to what is around them: flocking birds, a segregation model, grazing animals, cars on a ring road. Neighbour counts and sums read the cells around an agent on a grid. The `NEAR` family reads agents within a distance. `PATCH` reads the value of the grid cell the agent stands on.

## Behaviour and rules

The grid functions `NEIGHBOURS`, `NEIGHBOUR_SUM`, `NEIGHBOUR_MEAN`, `CELL_COUNT` and `PATCH` need a grid space, or the check reports an error. `NEIGHBOURS` counts the agents in the cells around self and leaves out self's own cell. `CELL_COUNT` counts self's own cell and includes self. `PATCH` and `CELL_COUNT` give 0 off a bounded grid.

`NEAR` counts the agents of a population within a distance `r` of self, and the distance itself is included. It leaves self out when the population is self's own. Distances are in the space's own units. `NEAREST` is the distance to the closest other agent and is infinite if there is none. `KNN_SUM` and `KNN_MEAN` take the k nearest agents, with ties broken by the lower index.

An optional last argument filters by state first. Sums, means and counts give 0 when no agent qualifies. The maximum, the minimum and the heading give NaN. `NEAR_HEADING` is the circular mean heading and needs a continuous space whose agents have a heading.
