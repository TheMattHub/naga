# How do I share a viewer or a player?

> **In short**
>
> - **You build:** a [player file](../glossary.md#player-file), one HTML page that replays a recorded run in a browser. You also learn when to send a [viewer file](../glossary.md#viewer-file) instead.
> - **You need:** Python 3.11 or later and `pip install nagasim`. The reader needs only a browser.
> - **Time:** about ten minutes for the player. The viewer needs a 29 MB download once.

A model can leave your machine as one file. By the end you write a player, check what it contains, and choose between it and the viewer for a given reader.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-share.py`. It builds a one-server [queue](../glossary.md#queue), records the first [replication](../glossary.md#replication) with `naga run --player`, and then opens the file to see what it asks of a browser.

```python
--8<-- "docs/snippets/goal-share.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-share.py
```

You should see this. The size of the file can change after an engine upgrade.

```text
--8<-- "docs/snippets/goal-share-output.txt"
```

3. Open `queue.html` in a browser. Double-click it. There is no server and no install, and the page makes no request.

4. Read the last three lines. The page loads no script from elsewhere, and a Content-Security-Policy makes the browser refuse every request and every script but its own. It shows the resource name, because a reader sees what the model is called.

5. Choose what to send. The player holds one recorded run: names, layout and the sampled run, and no parameter, expression or data source of the model. The reader watches it and cannot change it.

6. Send a viewer when the reader should change inputs and run again. The viewer holds the engine itself, so **anyone who gets it can read the whole model**. Export it once the runtime is on your machine.

<!-- no-run: needs the browser runtime, 29 MB, fetched from github.com and cdn.jsdelivr.net -->
```console
naga viewer --fetch
naga viewer model.json -o model.html
```

7. Pick by the reader and the model. A confidential model goes as a player. A model the client may see in full goes as a viewer. The table below sets them side by side.

| | Player | Viewer |
|---|---|---|
| The reader can | watch one run | change inputs, run again, compare |
| The reader can read | names, layout, the run | the whole model and the engine's source |
| Size | under 10 MB, usually far less | about 40 MB |
| Send it by | email | a link or a shared drive |

## Check yourself

- Predict, then run: change `time_limit=240` to `time_limit=480`. Will the file roughly double, or does the engine thin the recording?
- Explain it back: why does a viewer, and not a player, need the "whole model" warning?

## Done when

- [ ] `queue.html` exists and opens in a browser with no network.
- [ ] You can say what a player leaves out of the model.
- [ ] You can say which file to send for a confidential model and which for a client who wants to try inputs.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| The command fails with a message about the size | The recording passed the 10 MB limit | The engine writes the tape again with fewer samples. If even that does not fit, shorten the run. |
| `naga viewer` says the runtime is missing | It is downloaded only when you ask | Run `naga viewer --fetch`, or pass `--runtime DIR` from another machine. |
| The viewer is refused for a model with data sources | A viewer reads no data source | Add `--freeze` to write the values read into the copy. |
| The viewer is refused by name | The model has a LOGIC block, a plugin block or agent Python | Share a player instead. A viewer runs no code but the engine's. |
| The page says the browser is not supported | Safari, a phone or a tablet | Use Chrome, Edge or Firefox on a computer. |

## Elements this page uses

- Blocks: [CREATE](../reference/blocks/create.md), [PROCESS](../reference/blocks/process.md) and [DISPOSE](../reference/blocks/dispose.md).
- Command line: [`naga run`](../reference/cli/run-and-compare.md#naga-run) with `--player`, and [`naga viewer`](../reference/cli/plan-and-share.md#naga-viewer).
- Background: [Share a model](../sharing-a-model.md) covers the viewer's levers and the numbers it shows, and [Security](../security.md#a-shared-player-file) covers what a shared player carries.

Next: [Write a plugin block](write-a-plugin-block.md).
