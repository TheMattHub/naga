# Learn Naga

The learning path builds one model in six steps, and each page adds a few lines to the previous one and prints what the model does. Every script on these pages runs on its own, the output printed under it is made by running it, and the whole path takes about half an hour.

1. [One queue](1-one-queue.md)
2. [Two servers and a schedule](2-two-servers-and-a-schedule.md)
3. [A tank level](3-a-tank-level.md)
4. [Agents](4-agents.md)
5. [Replications and scenarios](5-replications-and-scenarios.md)
6. [Reading the results and the event log](6-results.md)

You need Python 3.11 or later and `pip install nagasim`. To run a step, copy its script into a file such as `step1.py` and run `python step1.py`: it prints what the page shows under the script. The path starts from a queue and ends with replications, scenarios and the event log. On the way, the same model meets discrete-event processes, a schedule, a continuous level and agents. When you have finished it, the [modelling guide](../modelling-guide/index.md) explains each part in depth and the [gallery](../gallery/index.md) shows the bundled models. If you would rather start from a model file than from Python, use the [Quick start](../quickstart.md).

## If something goes wrong

| What happens | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'nagasim'` | The engine is installed in another Python. | Run `python -m pip install nagasim` with the same `python` that runs the script. |
| The numbers differ from the page. | A changed seed, or an older engine. | Keep `seed=1`. If they still differ, update with `python -m pip install -U nagasim`. |
| A step fails on a name the step does not define. | Each script is the whole model so far, not only the new lines. | Copy the whole script of the page, not the changed lines. |

<!-- goals:begin -->
## By goal

If you know what you want to do, find it here. Each row names one page, and a row without a page is a goal the docs do not teach yet.

| I want to... | Start here |
|---|---|
| Compare scenarios with replications | [Step 5: Replications and scenarios](../learn/5-replications-and-scenarios.md) |
| How many replications do I need, and what warm-up? | [How many replications do I need, and what warm-up?](../goals/replications-and-warm-up.md) |
| Build a model from an event log | [From an event log to a model in five minutes](../event-log-tutorial.md) |
| Run a model from Python | [Learn Naga](../learn/index.md) |
| Which decision is best? Optimise it | [Which decision is best? Optimise it](../goals/optimise-a-decision.md) |
| Which inputs matter? Sensitivity and design of experiments | [Which inputs matter? Sensitivity and design of experiments](../goals/sensitivity-and-doe.md) |
| Fit input distributions from data | [How do I fit input distributions from data?](../goals/fit-input-distributions.md) |
| How sure am I, given my input data? Input uncertainty | [How sure am I, given my input data? Input uncertainty](../goals/input-uncertainty.md) |
| Calibrate a model to data | [How do I calibrate a model to data?](../goals/calibrate-to-data.md) |
| Connect live data end to end | [How do I connect live data end to end?](../goals/connect-live-data.md) |
| Plan or schedule jobs | [How do I plan or schedule jobs?](../goals/plan-or-schedule-jobs.md) |
| Drive a study from an AI agent | [How do I drive a study from an AI agent?](../goals/drive-a-study-from-an-ai-agent.md) |
| Import a Vensim or XMILE model | [How do I import a Vensim or XMILE model?](../goals/import-vensim-xmile.md) |
| Combine discrete-event, system dynamics and agents | [How do I combine event flow, system dynamics and agents?](../goals/hybrid-model.md) |
| Share a viewer or a player | [How do I share a viewer or a player?](../goals/share-a-viewer-or-player.md) |
| Write a plugin block | [How do I write a plugin block?](../goals/write-a-plugin-block.md) |
| Check a model in continuous integration | [How do I check a model in continuous integration?](../goals/check-in-ci.md) |
| Train a policy with reinforcement learning | [How do I train a policy with reinforcement learning?](../goals/train-a-policy-with-rl.md) |
| Cost a model and find the cost drivers | [How do I cost a model and find the cost drivers?](../goals/cost-drivers.md) |
<!-- goals:end -->
