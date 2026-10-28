Use DECIDE when entities must split across several links. It can send a share by chance, such as a 10 percent rework rate. It can test conditions, such as the entity type or size. It can hand the choice to a policy that a controller or a learning agent supplies. For a rule you must write in Python, use [LOGIC](logic.md). To send an entity along its own routing, use [ROUTE](route.md).

## Behaviour and rules

The outgoing links are the branches, in link order. In `Chance` mode, `probs` holds one probability per branch, and branch i is taken with the probability at position i. The numbers are not cumulative. Their sum must not exceed 1. If there is one more link than probabilities, the last link is the ELSE branch and takes the rest. A check reports probabilities that sum to less than 1 without that extra link.

In `Condition` mode, the conditions are tested in order and the first true one wins. The last link is again the ELSE branch when there is one more link than conditions. If no condition is true and there is no ELSE link, the block logs a warning and the entity goes nowhere. Always add an ELSE link.

In `Chance` mode, a draw that falls outside the probabilities with no ELSE link stops the run with an error. A deterministic run does not draw. It allocates the branches in the exact proportions instead.

In `Policy` mode, the number of actions is the number of links. Without an attached controller, the `fallback` picks the branch: `first`, `random`, or `shortest` using one `queue_exprs` expression per link. A controller that pauses the run, as used for reinforcement learning, suspends the model until it answers.
