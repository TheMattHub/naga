# How do I check a model in continuous integration?

> **In short**
>
> - **You build:** a [model file](../glossary.md#model-file) with three [assertions](../glossary.md#assertion), a `naga check` run that fails the build when one is false, and the workflow file that runs it on every push.
> - **You need:** Python 3.11 or later, `pip install nagasim`, and a GitHub repository for the last step. You can run the rest on your own machine.
> - **Time:** about ten minutes.

A model changes as people edit it, and an edit can quietly break what it was built to show. By the end you pin three claims about a [queue](../glossary.md#queue) in the model file. You read a pass, a fail and the exit code that [continuous integration](../glossary.md#continuous-integration) uses. Then you generate a workflow that runs the checks on every push.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-ci.py`. It writes `model.json` with three assertions on a one-server queue, then calls `naga check` on it, as a CI job would. The third assertion is false on purpose, so you can see a failure.

```python
--8<-- "docs/snippets/goal-ci.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-ci.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-ci-output.txt"
```

3. Read the assertions. Each one names a [KPI](../glossary.md#kpi), a comparison and a value, and runs over its own `replications`. The check computes a 95% [confidence interval](../glossary.md#confidence-interval) across them.

4. Read the three verdicts. An assertion passes when the whole interval satisfies the claim and fails when the whole interval breaks it. The first two pass. The third fails, with the mean wait at 2.87 minutes and an interval from 2.30 to 3.43 against a limit of 2.

5. Read the exit code. It is 1 because one assertion failed. A CI job treats a non-zero exit as a failed build, so a merge that makes the model break its own claim is stopped. The third verdict an assertion can have is `inconclusive`: the interval straddles the limit, so more replications are needed. It fails the build only with `--strict`.

6. Read the `laws` line. `naga check` also runs five built-in sanity checks, such as [Little's law](../glossary.md#littles-law), on the same runs.

7. Read the workflow. `naga ci-template` prints a GitHub Actions file that installs the engine, runs `naga check` on every model under `models/`, and uploads the [JUnit report](../glossary.md#junit) as a build artifact. Save it as `.github/workflows/model-checks.yml`, and put your models under `models/`.

8. Fix the failing claim. Change the third limit from 2 to 3.5, or fix the model, and run the script again. The exit code becomes 0.

## Check yourself

- Predict, then run: if you lower `replications` for the third assertion from 20 to 3, will it still fail, or turn inconclusive? Try it, then run `naga check model.json --strict`.
- Explain it back: why does the check judge the whole interval and not just the mean?

## Done when

- [ ] The script prints two passes, one failure, exit code 1 and a workflow file.
- [ ] You can say what `inconclusive` means and what `--strict` does with it.
- [ ] You can say which file you commit to run the checks on every push.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `unknown KPI` in a verdict | The KPI key is misspelt | The message lists close matches. The [KPI reference](../reference/kpis.md) has every key. |
| An assertion flips between runs | Its interval straddles the limit, so it is `inconclusive` or close to it | Raise `replications`, or move the limit to what you can defend. |
| The CI job fails to install the engine | The default install step is `pip install .`, which installs the repository itself | Pass `--install "pip install nagasim"` to `naga ci-template` in a repository that only holds models. |
| The job passes with "no models found" | No `*.json` file is under the `--path` folder | Move the models there, or pass the right `--path`. |
| Exit code 2 | A file is missing or is not valid JSON | Open the file named in the message. |

## Elements this page uses

- Python: [`Model`](../reference/api/Model.md) with `assertions`, and [`assert_model`](../reference/api/assert_model.md) for a pytest suite.
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- KPIs: `Usage % (snapshot)` and `Time in Queue Avg` in the [resource summary](../reference/kpis.md#resource_summary).
- Command line: [`naga check`](../reference/cli/model-files.md#naga-check), [`naga ci-template`](../reference/cli/model-files.md#naga-ci-template) and [`naga verify`](../reference/cli/model-files.md#naga-verify), which checks the engine itself against queueing theory.
- Background: [Model assertions and CI](../model-assertions.md) covers reality checks and path checks.

Next: [Cost a model and find the cost drivers](cost-drivers.md).
