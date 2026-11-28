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
editing its configuration.

On Windows, `where.exe naga` in PowerShell prints the full path of the command; a client that does
not see your PATH needs that path, with forward slashes in the JSON (`"command":
"C:/Users/you/AppData/Local/Programs/Python/Python312/Scripts/naga.exe"`). Claude Code adds the same
entry from a terminal, and Claude Desktop reads `%APPDATA%\Claude\claude_desktop_config.json`:

```text
claude mcp add naga -- naga mcp --root C:/Users/you/models
```

`--root` is a folder you choose, not one that ships with the package: the examples the server reads
are `naga://examples/<name>`, and a model of your own goes in the folder you name. To help the assistant pick the right tool, add `--tool-picker gte` (a
small local model) or `--tool-picker haiku` (hosted Claude Haiku, needs `ANTHROPIC_API_KEY`); both
are optional and off by default (see [Pick tools with a small model](tool-picker.md)).

Besides the tools the server publishes:

- resources: `naga://reference/blocks`, `naga://reference/distributions`,
  `naga://reference/expressions`, `naga://reference/schema`, `naga://guide/study` (the study guide,
  see [Run a study with an assistant](ai-study.md)) and `naga://examples/<name>` for each bundled
  model;
- four prompts: `build_model_from_description` and `analyse_results` for a model and its numbers,
  and `run_simulation_study` and `study_interview` for a study.

## Publish fewer tools

`naga mcp --tools PROFILE` publishes the tools of one situation instead of all of them, so a
client pays for fewer tool descriptions on every turn and picks among fewer. Tool names, arguments
and answers are the same in every profile, and a tool outside the profile cannot be called by name.
The default, `--tools all`, publishes every tool. Only the person who starts the server chooses a
profile; no tool argument can change it. The server's instructions name only the profile's own tools,
and `find_tool` is in every profile, so an assistant can always search for the right tool.

| Profile | Tools | For |
|---|---|---|
| `compact` | 10 | Write a whole model file, run it, save it. |
| `build` | 50 | Make and change a model: blocks and links, records, reference, validation, a picture. |
| `run` | 22 | Run a finished model: scenarios, what-if, precision, reality checks, the run debugger. |
| `study` | 33 | A decision study (`study_*`) and the searches behind it: `optimize`, `calibrate`, `evaluate_policy`. |
| `twin` | 14 | A live twin: data bindings, initial state, forecast, plant timeline, recommendations. |

Pick the profile that matches the session, or `all` when the work moves between situations.
A product built on the engine may add its own tools to a profile.

## The flags, all the person's

Every flag of `naga mcp` is set by the person who starts the server; no tool argument can set one.

- `--root`: the one folder the server reads and writes (default: the current directory).
- `--allow-network`, `--allow-private-network`, `--allow-host HOST`: network access for a `sql` or `json_url` data source.
- `--connections FILE`: the connections a live source may name.
- `--tools PROFILE`: the profile above.
- `--tool-picker gte|haiku`, `--tool-picker-backup PATH`: the optional tool picker (see [Pick tools with a small model](tool-picker.md)).
- `--room`: also serve the studies' live control room on `127.0.0.1`, at a random port with a token.
- `--twin DIR`: the result directory of a twin you started, read by three read-only tools.

## How the tools behave

- **An edit answers with the problems it introduced and the ones it fixed**, each with a stable
  code, a path into the model and a hint. An agent learns of a broken model at the edit, not at
  the run.
- **An error is a value**, `{"error": {"code", "message", "hint"}}`, never a traceback. Over MCP
  the answer is also flagged `isError`, and a wrongly shaped argument answers `E_BAD_ARGUMENT`
  naming it, before the tool runs.
- **An answer is data, sent once.** Each tool publishes a typed `outputSchema`, and its answer
  is the `structuredContent` with a one-line text summary beside it. A client that reads only
  the text gets the summary, not the answer.
- **A result is a digest.** `run_model` returns the system KPIs with their confidence-interval
  half-widths and a summary per resource; `get_kpi_table` pages a whole frame on request.
- **A run or load answer carries its log.** `run_model`, `run_scenarios`, `open_model`,
  `validate_model` and the model imports add `log` when the call logged a warning or an error:
  `{level, source, message, count}`, repeats folded, at most 20 with `log_truncated`. A refusal
  carries it too. `count` counts the records the calling process saw, so a warning repeated in
  every replication can count higher inline than from a process pool, which sends each distinct
  record back once per run.
- **A long call reports its progress.** A client that sends a progress token gets progress
  notifications from `run_model`, `run_scenarios`, `run_to_precision`, `forecast`, `optimize`,
  `calibrate`, `fit_surrogate`, `study_step` and `study_validate`.
- **Every edit can be undone** (`undo`, `redo`).
- **Tool names are permanent.** So are problem codes. A tool merged into another keeps answering
  under its old name, unlisted: `import_xmile` and `import_mdl` are now `import_sd_model` (it
  reads the file's extension), and `list_sequences` and `list_orders` are `list_records`.

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
`NAGASIM_LLM_API_KEY` and `NAGASIM_LLM_REASONING`, then the defaults. An address given with
`--base-url` or `NAGASIM_LLM_BASE_URL` selects the OpenAI-compatible provider unless `--provider`
says otherwise. It works on the one file
you name and reaches only the endpoint you configured.

<!-- no-run: needs a language model to answer -->
```console
naga assist model.json "add a second server" --model-id qwen3:8b
```
