# Step 4: Agents

An agent is a small statechart that lives in the model beside the blocks. This step adds four staff members who work, go on a break and return, and the model counts how many are in each state.

```python
--8<-- "docs/snippets/learn-4.py"
```

An agent type lists its states and the transitions between them; a transition fires after a delay drawn from an expression, here `EXPO(120)` minutes of work and `EXPO(15)` minutes of break. A population makes four agents of the type, all starting in `Working`. Nothing in the model reads the staff yet, but they run on the same clock as the queue and the tank.

It prints:

```text
--8<-- "docs/snippets/learn-4-output.txt"
```

The summary gives the average number of agents in each state and how many times each state was entered. About one in nine of the four staff is on a break at any moment, as the two delays imply. The model now has all four kinds of component, and the next step asks how sure we can be of its numbers.

Next: [Replications and scenarios](5-replications-and-scenarios.md).
