# Use with an agent

The engine has a set of tools an LLM agent calls to build, validate, run, debug and analyse a
model: [Agent tools](reference/tools.md) lists every one. `naga mcp` publishes them over the
Model Context Protocol; they are also plain Python functions, with no MCP involved.

## Connect an agent

```console
pip install "nagasim[mcp]"
```

<!-- no-run: a server that reads stdin until its client closes it -->
```console
naga mcp --root path/to/models
```

The server speaks MCP over stdio, so it is the client that starts it. Claude Code (`.mcp.json`
in the project, or `claude mcp add`) and Claude Desktop (`claude_desktop_config.json`) take the
same entry:

```json
{
  "mcpServers": {
    "naga": {"command": "naga", "args": ["mcp", "--root", "/path/to/models"]}
  }
}
```

Any other MCP client that starts a stdio server works the same way. Restart the client after
editing its configuration. To help the assistant pick the right tool, add `--tool-picker gte` (a
small local model) or `--tool-picker haiku` (hosted Claude Haiku, needs `ANTHROPIC_API_KEY`); both
are optional and off by default (see [Pick tools with a small model](tool-picker.md)).

Besides the tools the server publishes:

- resources: `naga://reference/blocks`, `naga://reference/distributions`,
  `naga://reference/expressions`, `naga://reference/schema`, and `naga://examples/<name>` for
  each bundled model;
- two prompts: `build_model_from_description` and `analyse_results`.

## How the tools behave

- **An edit answers with the problems it introduced and the ones it fixed**, each with a stable
  code, a path into the model and a hint. An agent learns of a broken model at the edit, not at
  the run.
- **An error is a value**, `{"error": {"code", "message", "hint"}}`, never a traceback.
- **A result is a digest.** `run_model` returns the system KPIs with their confidence-interval
  half-widths and a summary per resource; `get_kpi_table` pages a whole frame on request.
- **A long call reports its progress.** A client that sends a progress token gets progress
  notifications from `run_model`, `run_scenarios`, `run_to_precision`, `forecast`, `optimize`,
  `calibrate`, `fit_surrogate` and `study_step`.
- **Every edit can be undone** (`undo`, `redo`).
- **Tool names are permanent.** So are problem codes.

## A session, without a model

This is what an agent does, written as the calls it makes. It runs as it stands: the tools are
functions.

```python
from nagasim import agent_tools
from nagasim.agent_tools import call

agent_tools.use_workspace(agent_tools.Workspace("."))     # what `--root .` does

call("new_model", name="shop")
call("upsert_record", model="shop", section="resources", record={"name": "Server", "capacity": 1})
added = call("add_block", model="shop", type="CREATE", name="arrive",
             params={"distribution": "Exponential", "dist_params": {"rate": 1.0}})
print([problem["code"] for problem in added["introduced"]])    # a CREATE alone is not a model yet

call("add_block", model="shop", type="PROCESS", name="serve",
     params={"resource_name": "Server", "distribution": "Exponential", "dist_params": {"rate": 1.25}})
call("add_block", model="shop", type="DISPOSE", name="leave")
call("connect", model="shop", source="arrive", target="serve")
linked = call("connect", model="shop", source="serve", target="leave")
assert linked["errors"] == 0, linked                            # the last link fixed the rest

call("set_simulation_settings", model="shop", run_length=2000, warmup=200, replications=3)
assert call("validate_model", model="shop")["ok"]
result = call("run_model", model="shop")
print(result["system"]["Avg Throughput Time"])                  # {"mean": ..., "ci_half_width": ...}
call("save_model", model="shop", path="shop.json")

refused = call("open_model", path="../somewhere/else.json")
assert refused["error"]["code"] == "E_PATH_OUTSIDE_ROOT"
```

## What an agent can and cannot do

**Permission is the caller's, never the file's and never the agent's.** The person who starts
the server decides what it may reach, once, on its command line. No tool takes a permission as
an argument, so a model that has been talked into asking for more gets a refusal with a code,
not access.

- **Files stay inside `--root`.** A path that leaves it (through `..`, an absolute path, a link
  or a network share) is refused, as the last lines above show.
- **No network unless you allow it.** A model's `sql` or `json_url` data source is refused
  without `--allow-network` (and `--allow-private-network` or `--allow-host HOST` for an address
  inside your network). A live source needs your `--connections FILE`. A permission written in a
  model file is stripped when the file is read.
- **A model's Python never runs through an agent.** An agent type may carry Python behaviour,
  which the engine runs only when the person running the model sets
  `NAGASIM_ALLOW_AGENT_PYTHON=1`. The tools drop that consent: `run_model` answers
  `E_AGENT_PYTHON_REFUSED` for such a model. Run it yourself with `naga run`.
- **A model file is data, not instructions.** Names and texts in a model come back in tool
  answers and could be written to steer an agent. They cannot widen what the tools may do.
- **A twin is read, not driven.** With `--twin DIR` the tools `get_twin_monitor`,
  `get_twin_forecast` and `get_twin_recommendations` read the result directory of a twin you
  started. No tool starts one, chooses a connection, or approves a recommendation.

What a model file itself can and cannot do, with or without an agent, is on the
[Security](security.md) page.

## One assistant turn from the command line

`naga assist model.json "add a second server"` gives one request to a model with the tools and
writes the edited file. It talks to a local OpenAI-compatible server by default (Ollama at
`http://localhost:11434/v1`; name the model with `--model-id`), or to Claude when
`ANTHROPIC_API_KEY` is set and the `assistant` extra is installed. Where the request goes is set
by you alone: flags, then `NAGASIM_LLM_PROVIDER`, `NAGASIM_LLM_BASE_URL`, `NAGASIM_LLM_MODEL`,
`NAGASIM_LLM_API_KEY` and `NAGASIM_LLM_REASONING`, then the defaults. It works on the one file
you name and reaches only the endpoint you configured.

<!-- no-run: needs a language model to answer -->
```console
naga assist model.json "add a second server" --model-id qwen3:8b
```
