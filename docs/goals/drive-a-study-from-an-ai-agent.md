# How do I drive a study from an AI agent?

> **In short**
>
> - **You build:** a short script that makes the [tool calls](../glossary.md#tool-call) an AI assistant makes: build a [queue](../glossary.md#queue), run it, and ask what happens with a second server.
> - **You need:** Python 3.11 or later and `pip install nagasim`. No language model and no network are involved.
> - **Time:** about ten minutes.

An AI assistant works on a model through a fixed set of tools, and each tool is a plain Python function. By the end you call those functions yourself, so you know exactly what an assistant sees and what it can reach. Then you know what to connect with `naga mcp` (the [MCP](../glossary.md#mcp) server).

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-agent.py`. It opens a [workspace](../glossary.md#workspace) in the current folder, builds a one-server [queue](../glossary.md#queue) with eight edit calls, and then runs and questions it.

```python
--8<-- "docs/snippets/goal-agent.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-agent.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-agent-output.txt"
```

3. Read the first line. Each `call(name, ...)` is one tool call. The last `connect` answers with `errors: 0`, so the model is complete and `validate_model` agrees. An edit that leaves the model broken answers with the problems it introduced. Each has a stable code, so an assistant learns of a mistake at the edit and not at the run.

4. Read the second line. The script asked to open a file outside the workspace. The answer is a value, `E_PATH_OUTSIDE_ROOT`, and not a traceback. The same holds for every tool: an error carries a code, a message and a hint.

5. Read the third line. `run_model` returns a digest, not raw data. Each KPI comes with the [half-width](../glossary.md#half-width) of its 95% [confidence interval](../glossary.md#confidence-interval), here 3.04 plus or minus 0.34 minutes.

6. Read the fourth line. `what_if` runs the model and a changed copy on paired [seeds](../glossary.md#seed), without editing the model. It doubles the [replications](../glossary.md#replication) from 5 until the difference is clear or the cap is reached. Here 5 were enough: a second server cuts the wait by about 3 minutes.

7. Connect a real assistant. The tools are the same functions, published by the server. The root folder is the workspace, and the person who starts the server sets it.

<!-- no-run: the server reads stdin until its client closes it -->
```console
naga mcp --root path/to/models
```

8. Give the assistant a whole study to run. The three tools `study_start`, `study_step` and `study_status` keep the question, the model versions and the checks in a folder, so another assistant can resume it tomorrow.

## Check yourself

- Predict, then run: change the override in `what_if` to `capacity` 3. Will the verdict say more clearly that the wait fell, and will it need more replications?
- Explain it back: why can an assistant not widen its own permissions, however its prompt is worded?

## Done when

- [ ] The script prints a valid model, a refusal with a code, a wait with its interval and a verdict.
- [ ] You can say where the workspace stops an assistant, and who sets that limit.
- [ ] You know the command that publishes the same tools to an assistant.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `E_PATH_OUTSIDE_ROOT` on a file you meant to open | The path leaves the workspace folder | Copy the file under the root, or start the server with `--root` on its folder. |
| `E_AGENT_PYTHON_REFUSED` from `run_model` | The model has an agent type that runs Python | Run it yourself with `naga run`. A tool never grants that permission. |
| A tool answer starts with `error` | The call was wrong, not the engine | Read `code` and `hint`. The hint names the tool that lists the valid values. |
| `naga mcp` says the `mcp` package is missing | The optional extra is not installed | Run `pip install "nagasim[mcp]"`. |
| The assistant picks the wrong tool | A small model struggles with so many tools | Add `--tool-picker gte` to narrow the choice. See [Pick tools with a small model](../tool-picker.md). |

## Elements this page uses

- Tools: every tool is listed in the [agent tool reference](../reference/tools.md). This page calls `new_model`, `upsert_record`, `add_block`, `connect`, `set_simulation_settings`, `validate_model`, `open_model`, `run_model` and `what_if`.
- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Command line: [`naga mcp`](../reference/cli/agents.md#naga-mcp) and [`naga study`](../reference/cli/agents.md#naga-study).
- Background: [Use with an agent](../mcp.md) covers permissions, and [Run a study with an assistant](../ai-study.md) covers the study folder.

Next: [Share a viewer or a player](share-a-viewer-or-player.md).
