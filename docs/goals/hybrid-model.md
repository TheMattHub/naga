# How do I combine event flow, system dynamics and agents?

> **In short**
>
> - **You build:** a script that builds a help desk with tickets, a frustration [stock](../glossary.md#stock) and a staff team that takes breaks, then compares four and five staff.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and the [learning path](../learn/index.md) through [step 4, agents](../learn/4-agents.md).
> - **Time:** about fifteen minutes.

Real systems rarely stay in one modelling style, and the engine runs all three in one model. By the end you build a [hybrid model](../glossary.md#hybrid-model) in which [agents](../glossary.md#agent) change how fast tickets are worked, the queue feeds a [stock](../glossary.md#stock), and the stock changes how fast tickets arrive.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-hybrid.py`. It builds the same help desk for a given team size, in three layers that share one clock.

```python
--8<-- "docs/snippets/goal-hybrid.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-hybrid.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-hybrid-output.txt"
```

3. Read the event layer. Tickets are created, wait for a desk and are worked. The arrival time and the work time are expressions, so they can read the other layers: `LEVEL("Frustration")` is a stock, and `COUNT("Team", "Working")` counts the agents in one state.

4. Read the agent layer. Each staff member alternates between `Working` and `OnBreak`, with exponential dwell times of 60 and 15 minutes. A ticket takes longer when fewer staff are working, since the mean work time is divided by the number working.

5. Read the stock layer. Frustration grows by 0.1 per waiting ticket per minute and drains by 5 per cent of itself. `NQ("Desk")` is the number of tickets waiting, so the queue feeds the stock. The arrival expression caps the stock's effect at 10, which keeps the loop from running away.

6. Read the first output line. With four staff, 3.17 of them are working on average, the mean wait is 14.9 minutes, and frustration averages 35. The loop has tipped. A long queue raises frustration, and frustration raises arrivals.

7. Read the second line. With five staff the wait drops to 0.9 minutes and frustration to 1.9. One extra person does not scale the result by a fifth. It moves the system out of the vicious circle, and only a model with all three layers shows that.

## Check yourself

- Predict, then run: if you lower the cap in `min(LEVEL("Frustration"), 10)` to 5, will the four-staff wait rise or fall? Change it and check.
- Explain it back: which layer carries the effect of a staff break on the wait, and which carries the effect of the wait on arrivals?

## Done when

- [ ] The script prints one line for four staff and one for five.
- [ ] You can name the three layers and the expression that links each pair.
- [ ] You can say why the stock's effect on arrivals is capped.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| The run takes minutes and the queue grows without bound | The feedback loop has no cap, so more waiting makes more arrivals without limit | Cap the stock's effect, as in `min(LEVEL(...), 10)`, or weaken the gain. |
| `unknown function 'MAX'` | Expression names are case sensitive | Use lower-case `max` and `min`. The [expression reference](../reference/functions/index.md) lists the allowed names. |
| `COUNT` returns 0 | The population or state name is misspelt | Use the names given to `add_population` and the state list. |
| A level does not move | The flow has no `to` or `from`, or its rate is 0 | Give a flow the level it feeds in `to`, and the level it drains in `from`. |

## Elements this page uses

- Python: [`Model`](../reference/api/Model.md) with `add_level`, `add_flow`, `add_agent_type` and `add_population`, and [`Results`](../reference/api/Results.md).
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Distribution: [CustomExpression](../reference/distributions/custom-expression.md).
- Expressions: `LEVEL`, `COUNT`, `NQ`, `EXPO`, `min` and `max` in the [expression reference](../reference/functions/index.md).
- Background: [System dynamics](../system-dynamics.md) and [Agent-based modelling](../agent-based-modelling.md).

Next: [Check a model in continuous integration](check-in-ci.md).
