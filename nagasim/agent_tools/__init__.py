"""Tools an LLM agent can call to build, validate, run and analyse a model
(ROADMAP 3.2 item 1).

Pure Python plus the existing top-level modules: no MCP import, no PySide6.
An MCP server is a thin adapter that exposes this registry over the Model
Context Protocol, but everything here is equally callable from a script, a
notebook or a test -- and `tests/test_agent_tools.py` drives exactly this
surface with no LLM involved.

Design rules, all of them load-bearing for an agent:

* **Every mutation goes through `ModelDocument`**, so `undo`/`redo` work and
  the editor and the agent share one source of truth.
* **Every mutation answers with the validation problems it introduced or
  fixed**, so an agent gets feedback after each edit instead of discovering a
  broken model at run time.
* **Errors are structured** (`{"error": {"code", "message", "hint"}}`), never
  a traceback: the `@tool` decorator catches everything.
* **Results are digests, never whole frames.** `run_model` returns system KPIs
  with CI half-widths plus a per-resource summary; the raw rows are paged out
  of `get_kpi_table` on request.
* **File access is confined to the workspace root** given at server start; a
  path escaping it is refused (`nagasim/netguard.py`).
* **No tool takes a permission as an argument.** Network access is a property
  of the `Workspace`, set once by the human who started the server; a tool that
  cannot reach something answers with a coded refusal whose hint is addressed
  to the human, never with a way for the model to widen its own access.

    from nagasim import agent_tools
    agent_tools.use_workspace(agent_tools.Workspace("/models"))
    agent_tools.TOOLS["new_model"].fn(name="mm1")

F2 (ARCH-REVIEW): this used to be one 2822-line file; it is now a package,
split by tool group, with the registry, `ToolError`, `Workspace` and every
shared helper in `core.py` (the module every other one imports) and the tools
themselves grouped as:

* `core.py`      -- the registry, `ToolError`, `Workspace`, workspace tools
                    (new/open/import/save/list/summary/json), undo/redo
* `model.py`     -- blocks, links, resources, calendar, entity types,
                    simulation/SD settings, `auto_layout`
* `records.py`   -- the generic record tools, the path network, scenarios and
                    the CSV importers
* `runs.py`      -- `run_model` and every study built on it (scenarios,
                    precision, reality checks, loop dominance, warm-up,
                    optimize, calibrate, forecast, policy evaluation, surrogate
                    fitting, export)
* `data.py`      -- data bindings, the warm-start initial state and
                    distribution fitting
* `reference.py` -- the reference tools and `validate_model`
* `wholemodel.py` -- `get_model_format` and `write_model`: the whole model file in one call
* `debug.py`     -- the run debugger: `debug_run` and `debug_inspect`

Importing this package registers every tool, in the module order above, then
loads plugins (`load_plugins()`) so a plugin's blocks/distributions are in
their registries before the reference tools describe them. A product that
ships tools of its own imports them after this package; they add themselves to
the same `TOOLS`, and `ToolSpec.origin` tells which module defined each.
"""
from __future__ import annotations

from nagasim.plugins import load_plugins

from .core import (ToolError, ToolSpec, Workspace, TOOLS, call, drop_python_consent,
                   python_consent_dropped, tool, tool_schemas, use_workspace, workspace)
from . import core, model, records, runs, data, reference, wholemodel, debug  # noqa: F401 -- import order is registration order
# Re-exported on purpose: tests read these three directly.
from .core import _ARG_DOCS
from .data import _BINDABLE
from .records import RECORD_TOOL_SECTIONS

__all__ = ["TOOLS", "ToolSpec", "ToolError", "Workspace", "use_workspace", "workspace", "call",
           "tool", "tool_schemas", "drop_python_consent", "python_consent_dropped"]

# Plugins must be registered before the reference tools describe them; a no-op after the first call.
load_plugins()
