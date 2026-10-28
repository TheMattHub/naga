# How do I write a plugin block?

> **In short**
>
> - **You build:** a [plugin](../glossary.md#plugin) file with one new [block](../glossary.md#block), `INSPECT`, and a model that uses it like a built-in block.
> - **You need:** Python 3.11 or later and `pip install nagasim`. You write plain Python, so a plugin is trusted code.
> - **Time:** about fifteen minutes.

A plugin adds a block, a [distribution](../glossary.md#distribution) or an exporter without changing the engine. By the end you write a block that inspects each [entity](../glossary.md#entity) and rejects one in five. Then you load it, wire it with the [builder](../glossary.md#builder), and check its result against a hand calculation.

This page has three parts: [Step by step](#step-by-step), [Check yourself](#check-yourself) and [If stuck](#if-stuck). The last section lists the [reference pages](#elements-this-page-uses) for the elements used.

## Step by step

1. Save this script as `goal-plugin.py`. It writes the plugin file `plugins/inspect_plugin.py` and a second script, `use_plugin.py`, and then runs that script in a fresh Python.

```python
--8<-- "docs/snippets/goal-plugin.py"
```

2. Run it.

<!-- no-run: the script above is the file this command runs -->
```console
python goal-plugin.py
```

You should see this. The script seeds itself, so the numbers are the same on your machine.

```text
--8<-- "docs/snippets/goal-plugin-output.txt"
```

3. Read the plugin file. A block is a class that waits, then calls `self.forward(entity, port)`. Port 0 is the first outgoing link and port 1 the second. `INSPECT` sends an entity out of port 1 with probability `reject_rate`.

4. Read its spec. `BlockSpec` gives the block a type name, its parameters with their defaults, and `multi_output=True` because it has two exits. The file ends with a list called `NAGASIM_BLOCKS`, which is the name the loader looks for.

5. Read how it is found. The environment variable `NAGASIM_PLUGIN_PATH` names a folder. The engine imports every `.py` file in it once, on the first run or when you call `plugins.load_plugins()`. The first output line shows `blocks:INSPECT` loaded and nothing failed.

6. Read the model. The builder gained a method `m.inspect(...)`, named after the type in lower case. A rejected entity goes to a five-minute `delay`, and a passed one leaves at once.

7. Check the result by hand. An entity spends 0.5 minutes in the inspection. One in five also spends 5 minutes in rework, so the mean is 0.5 + 0.2 x 5 = 1.5 minutes. The run gives 1.52.

8. Ship it as a package when others need it. Declare the spec under the entry-point group `nagasim.blocks` in `pyproject.toml`, and `pip install` loads it with no environment variable. The example in the repository under `examples/plugins/inspection_plugin` shows all three kinds.

## Check yourself

- Predict, then run: set `reject_rate` to 0.5. What mean time does the hand calculation give, and does the run agree?
- Explain it back: why must a plugin's type name not collide with a built-in block?

## Done when

- [ ] The first line lists `blocks:INSPECT` and an empty `failed` list.
- [ ] The run's mean time is within a few percent of the hand calculation.
- [ ] You can say how a plugin is loaded without installing it, and how it is loaded after `pip install`.

## If stuck

| What happens | Likely cause | Fix |
|---|---|---|
| `failed` lists your file | The file raised an error on import | Read the message next to the path, fix the file and run again. |
| `failed` says the name redefines a built-in | `type_name` matches a block that exists | Choose a new upper-case name. |
| `AttributeError` on `m.inspect` | The plugin was not loaded | Set `NAGASIM_PLUGIN_PATH` before the first run, or call `plugins.load_plugins()`. |
| An entity never reaches the second exit | The block has no link on port 1 | Add `m.link(check, other, source_port=1)`. |
| A shared model fails on another machine | The plugin is not installed there | Install it there, or do not share a model that depends on it. A viewer refuses plugin blocks. |

## Elements this page uses

- Python: [`Model`](../reference/api/Model.md) and [`run_model`](../reference/api/run_model.md).
- Blocks: [CREATE](../reference/blocks/create.md), [DELAY](../reference/blocks/delay.md) and [DISPOSE](../reference/blocks/dispose.md).
- Distributions: [Exponential](../reference/distributions/exponential.md) and [Constant](../reference/distributions/constant.md).
- Background: [Security](../security.md) explains why a plugin is trusted code, and [Use from Python](../python-api.md) lists `nagasim.plugins`.

Next: [Train a policy with reinforcement learning](train-a-policy-with-rl.md).
