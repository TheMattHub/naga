# How do I plan or schedule jobs?

> **In short**
>
> - **You build:** a script that sequences five jobs through a cutter and a packer with a solver, checks the plan by running it, and exports it for a planning system.
> - **You need:** Python 3.11 or later, `pip install "nagasim[plan]"`, which adds the OR-Tools package, and the [optimisation tutorial](optimise-a-decision.md).
> - **Time:** about ten minutes.

A schedule decides which job a machine works next, and a good one keeps every job on time. By the end you hand an [order book](../glossary.md#order-book) to [CP-SAT](../glossary.md#cp-sat), read the [tardiness](../glossary.md#tardiness) it removes, and replay the plan in the simulation to confirm it.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-plan.py`. The model is the bundled `order_priority_shop`, five orders that each cut and then pack. The script gives every order one entity, so each planned start is exact, and runs the shop first-come first-served as it runs today.

```python
--8<-- "docs/snippets/goal-plan.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-plan.py
```

You should see this. Durations are constants here, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-plan-output.txt"
```

3. Read the first line. Lateness is the finish time minus the due date, so a negative number is early. Today order WO-3 is 1 minute late, because it queues behind the others.

4. Read the second line. CP-SAT proved the plan optimal for weighted tardiness, and the total tardiness is 0. The makespan is 12 minutes. `refused` counts the parts of the model the solver could not plan. It is 0 here.

5. Read the third line. `propose` returns a layout with a start time for each order at each station and a `Plan` queue discipline on both machines. Running that layout is the check. Every order is on time, and WO-3 is 3 minutes early.

6. Read the last two lines. `plan_export` writes a CSV with one row per operation, which a planning system can import. The file holds the order, the operation, the resource and the planned start and end.

7. To score a plan under randomness, give the durations a spread and call `planning.plan_risk(layout, replications)`. It returns for each order the chance of finishing on time.

## Check yourself

- Predict, then run: if you pass `objective="makespan"` instead, will the makespan fall below 12? Try it.
- Explain it back: why do you replay the plan in the simulation when the solver already reports its result?

## Done when

- [ ] The script prints lateness today, a solver line, lateness under the plan and a CSV header.
- [ ] You can say which order was late today and what the plan changes for it.
- [ ] You can say what `refused` tells you.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `ImportError` naming `ortools` | The optional package is not installed | Run `pip install "nagasim[plan]"`. Without it, only the solver step fails. |
| `refused` lists an order | An order with more than one entity runs at a multi-unit resource or a resource set, where the replay may pack its entities differently | Use one entity per order, as the script does, or read the replay as the truth. |
| The replay is later than the solver's claim | Something in the model sits outside what the solver models | Read `plan.refused`, and trust the replay. |
| `status` is `FEASIBLE`, not `OPTIMAL` | The time limit stopped the search | Raise `time_limit_s`. A feasible plan is valid, only possibly not the best. |

## Elements this page uses

- Python: `propose` from `nagasim.cpsat`, `plan_export` and `plan_risk` from `nagasim.planning`, and [`run_model`](../reference/api/run_model.md).
- Command line: [`naga plan`](../reference/cli/plan-and-share.md#naga-plan) works on a model file.
- Background: [Model file format](../model-file-format.md#orders) describes orders and sequences.

Next: [Import a Vensim or XMILE model](import-vensim-xmile.md).
