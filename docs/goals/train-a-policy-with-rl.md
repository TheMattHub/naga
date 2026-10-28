# How do I train a policy with reinforcement learning?

> **In short**
>
> - **You build:** a [policy](../glossary.md#policy) for a routing decision, learned by [reinforcement learning](../glossary.md#reinforcement-learning) over many [episodes](../glossary.md#episode), and judged against two simple rules.
> - **You need:** Python 3.11 or later, `pip install nagasim` and the bundled routing model. The training loop below needs only numpy.
> - **Time:** about fifteen minutes.

Some decisions have no fixed answer, such as which of two machines gets the next job. By the end you let a program try both machines thousands of times, keep what worked, and test the result on runs it never saw. A library trainer is optional and comes at the end.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-rl.py`. It opens the bundled routing model as an environment, trains a table of values for 60 episodes, and then compares the result with a random rule and a shortest-queue rule.

```python
--8<-- "docs/snippets/goal-rl.py"
```

2. Run it. It takes a few seconds.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-rl.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-rl-output.txt"
```

3. Read the model. Jobs arrive at 1.5 a minute. Server A finishes 2 a minute and server B only 0.5, so sending too many jobs to B builds a queue. A `DECIDE` block in `Policy` mode asks, at each job, which of the two links to take.

4. Read the first line. The decision has 2 actions, A or B, and 4 observation values: the queue and the busy count of each server.

5. Read the reward. It is minus the work in progress ([WIP](../glossary.md#wip)) since the last decision, so a policy that keeps few jobs in the system earns more. The numbers in the table are the sum of that reward over one run of 200 minutes, so a higher number, closer to zero, is better.

6. Read the training loop. It rounds the four observation values to a small state and keeps a value for each state and action. After every decision it nudges that value toward the reward plus the best value of the next state. It tries a random action one time in five, so it keeps exploring.

7. Read the table. The random rule scores about -5370. The shortest-queue rule scores -465 and the learned table -468. Their [confidence intervals](../glossary.md#confidence-interval) overlap, so the table has learned the shortest-queue behaviour and is not better than it. Both beat random by a factor of more than ten.

8. Use a library trainer when the state is too big for a table. `SimEnv.as_gymnasium_env()` returns a Gymnasium environment, which Stable-Baselines3 and others accept. Neither is installed with the engine.

<!-- no-run: needs the optional gymnasium and stable-baselines3 packages -->
```python
from stable_baselines3 import PPO
from nagasim import examples, rl

env = rl.SimEnv(examples.path("parallel_routing_policy"), policy="routing", horizon=200.0).as_gymnasium_env()
PPO("MlpPolicy", env, seed=0).learn(total_timesteps=10_000)
```

## Check yourself

- Predict, then run: raise the number of training episodes from 60 to 300. Will the learned table beat the shortest-queue rule, or only match it?
- Explain it back: why do you judge the policy on seeds 1000 to 1009 and not on the seeds it trained on?

## Done when

- [ ] The script prints three rows and the learned row is within the shortest-queue interval.
- [ ] You can say what the reward is and why higher is better.
- [ ] You can name the one thing a model needs for a policy to be trained: a `DECIDE` block in `Policy` mode.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `no Policy-mode DECIDE block in this model uses policy 'routing'` | The policy name does not match the block's `policy` | Use the name written in the DECIDE block. |
| `as_gymnasium() needs the optional gymnasium package` | The extra is not installed | Run `pip install "nagasim[ml]"`. |
| The learned row is far below the shortest-queue row | Too few episodes, or a state that hides what matters | Train longer, or add observation values to the DECIDE block. |
| The result changes when you change the seed | A short training run is noisy | Judge on 10 or more evaluation runs and read the interval, as the script does. |
| You want to ship the trained policy | A pickled file would run code when loaded | Export a data-only policy file of kind `table` or `linear`. The engine loads nothing else. |

## Elements this page uses

- Python: `nagasim.rl.SimEnv` and `nagasim.rl.evaluate_policy`, and [`Model`](../reference/api/Model.md) for the model they run.
- Blocks: [DECIDE](../reference/blocks/decide.md) in `Policy` mode, [PROCESS](../reference/blocks/process.md) and [CREATE](../reference/blocks/create.md).
- Command line: [`naga policy-eval`](../reference/cli/run-and-compare.md#naga-policy-eval) evaluates a rule over several episodes.
- Background: [DECIDE](../reference/blocks/decide.md) describes how a policy pauses the run to wait for an action.
