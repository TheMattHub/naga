"""Model Context Protocol server over stdio (ROADMAP 3.2 item 2).

A thin adapter: every tool, every word of guidance and every rule lives in
`nagasim/agent_tools/`; this file only publishes them over MCP. `mcp` is an
OPTIONAL extra:

    pip install nagasim[mcp]
    naga mcp --root /path/to/models

The SDK import is lazy and its absence is a clear install message, never a
traceback.

A product built on the engine adds its own tools by importing the module that
registers them into `agent_tools.TOOLS` before it builds the server, and may
pass its own server name, instructions and twin session to `build_server` /
`serve`; this file knows nothing of such a product.

Published surface:

* TOOLS      -- every entry of `agent_tools.TOOLS`, under its own name.
* RESOURCES  -- naga://reference/{blocks,distributions,expressions,schema}
                and naga://examples/{name} for each bundled example model.
* PROMPTS    -- build_model_from_description, analyse_results.

Both the mcp 1.x `FastMCP` class and the mcp 2.x `MCPServer` it was renamed to
are supported; they share the decorator/registration API this file uses.
"""
from __future__ import annotations

import argparse
import functools
import json
import threading
from pathlib import Path

from nagasim import __version__, agent_tools, examples, netguard
from nagasim.assistant import COMPACT_TOOL_NAMES
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.distributions import DISTRIBUTION_REGISTRY

__all__ = ["build_server", "add_arguments", "serve", "main", "MCPUnavailable", "INSTALL_HINT",
           "INSTRUCTIONS", "COMPACT_TOOLS"]

INSTALL_HINT = (
    "The Model Context Protocol SDK is not installed. It is an optional extra:\n"
    "    pip install \"nagasim[mcp]\"      (or: pip install \"mcp>=1.2\")\n"
    "Everything the server exposes is also callable directly from "
    "nagasim.agent_tools without it."
)
INSTRUCTIONS = (
    "Build, validate, run and analyse discrete-event simulation models. To build one: "
    "get_model_format once, write_model with the whole model file, fix what its answer lists "
    "with one more write_model, run_model, save_model. run_model returns a compact KPI digest "
    "with confidence intervals; get_kpi_table pages the raw frames. The block-by-block tools "
    "(add_block, connect, upsert_record, ...) edit an open model."
)
# `naga mcp --tools compact` (D397): what building and running a model needs, and the file
# tools around it. Every tool's description is part of each turn an agent pays for, so a
# client that only builds models is better served by eight tools than by every one. The
# five are a local model's compact list (`assistant`), the three are the file tools.
COMPACT_TOOLS = COMPACT_TOOL_NAMES | {"open_model", "get_model_json", "save_model"}


class MCPUnavailable(ImportError):
    """The MCP SDK is not installed. An ImportError subclass (PHASE4 T9) so
    the ordinary optional-extra idiom -- `except ImportError` around the
    import, as `rl.py` and `surrogate.py` raise -- catches it; it exists as a
    named type only so a caller can print INSTALL_HINT instead of the bare
    module name."""


# Process-wide, like the workspace it guards (`agent_tools.use_workspace`).
_TOOL_LOCK = threading.Lock()


def _server_class():
    """The SDK's server class: `MCPServer` (mcp 2.x) or `FastMCP` (mcp 1.x)."""
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP
        return FastMCP
    except ImportError as exc:      # no mcp at all, or a version with neither name
        raise MCPUnavailable(INSTALL_HINT) from exc


def _described(spec: agent_tools.ToolSpec) -> str:
    """The docstring plus the per-argument one-liners. The SDK derives the
    input schema from the signature and so cannot see `agent_tools._ARG_DOCS`;
    folding them into the description is what keeps that guidance in front of
    the model."""
    described = [(name, prop["description"])
                 for name, prop in spec.input_schema["properties"].items()
                 if prop.get("description")]
    if not described:
        return spec.description
    lines = "\n".join(f"- {name}: {text}" for name, text in described)
    return f"{spec.description}\n\nArguments:\n{lines}"


def _annotations(spec: agent_tools.ToolSpec):
    """MCP tool annotations for one tool, or None when the SDK has no such type."""
    try:
        from mcp.types import ToolAnnotations
    except ImportError:
        return None
    return ToolAnnotations(
        title=spec.name.replace("_", " ").title(),
        readOnlyHint=spec.read_only,
        destructiveHint=False,          # every edit is one undo step away
        idempotentHint=spec.read_only,
        # The workspace root only -- and the network solely when the human
        # starting the server allowed it.
        openWorldHint=agent_tools.workspace().network.allow_network
        or bool(agent_tools.workspace().network.connections),
    )


# ---------------------------------------------------------------- reference text
def _blocks_reference() -> str:
    lines = ["# Block types", "",
             "`type` values accepted by add_block. Only types marked multi-output may have "
             "more than one outgoing link.", ""]
    for name in sorted(BLOCK_REGISTRY):
        spec = BLOCK_REGISTRY[name]
        doc = " ".join((spec.cls.__doc__ or "").split())
        params = ", ".join(f"{p.name}: {p.type.__name__}" + ("*" if p.required else "")
                           for p in spec.params) or "(none)"
        lines.append(f"## {name}{' (multi-output)' if spec.multi_output else ''}\n{doc}\n"
                     f"Params: {params}   (* = required)\n")
    return "\n".join(lines)


def _distributions_reference() -> str:
    lines = ["# Distributions", "",
             "Names accepted by a block's `distribution`, a link delay and a failure's "
             "uptime/downtime. Every numeric parameter also accepts an expression string.", ""]
    for name in sorted(DISTRIBUTION_REGISTRY):
        spec = DISTRIBUTION_REGISTRY[name]
        params = ", ".join(f"{p.name}={p.default!r}" for p in spec.params) or "(none)"
        lines.append(f"## {name} ({spec.category})\n{' '.join(spec.use_case.split())}\n"
                     f"Params: {params}\n")
    return "\n".join(lines)


def _expressions_reference() -> str:
    help_text = agent_tools.TOOLS["get_expression_help"].fn()
    lines = ["# Expression language", "",
             "A restricted-Python allow-list, never eval. Used by ASSIGN, DECIDE/HOLD "
             "conditions, RECORD, resource ranking and expression-valued distribution "
             "parameters.", "",
             "## Names", ""]
    lines += [f"- {n}" for n in help_text["names"]]
    lines += ["", "## Operators", "", help_text["operators"], "", "## Functions", ""]
    lines += [f"- `{f['name']}` ({f['arity']} args)" for f in help_text["functions"]]
    lines += ["", "## Examples", ""] + [f"- `{e}`" for e in help_text["examples"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- prompts
_BUILD_PROMPT = """\
You are building a discrete-event simulation model from a description.

Work in this order:

1. Read the description for: what ARRIVES (and how often), what HAPPENS to it
   (in what order, taking how long), what it COMPETES for (staff, machines,
   space), and what LEAVES.
2. Call get_model_format once: the file's shape, every block type with its
   parameters, the distributions' parameter names, the expression functions.
3. write_model with the WHOLE model file: one CREATE, the processing blocks
   (PROCESS is seize-delay-release in one block -- prefer it), DISPOSE at the
   end; a resource for everything that is competed for, named in each
   processing block's resource_name; the links in flow order (a DECIDE's links
   are its branches in order: branch 0 matches probs[0]/conditions[0]); and
   simulation.time_limit unless the model drains on its own. Set a warm-up for
   a steady-state question.
4. The answer lists every problem with its JSON path and a hint: fix them all
   in ONE more write_model. Warnings (W_ codes) are advisory but usually worth
   reading.
5. run_model, then save_model.

Rules of thumb: interarrival times are Exponential unless the description says
otherwise; service times are rarely Normal (it can go negative -- use Lognormal,
Gamma or BoundedNormal); a "rate" parameter is per base time unit, so state the
unit in set_simulation_settings and keep every duration consistent with it.

The description to model:

{description}
"""

_ANALYSE_PROMPT = """\
You are reading simulation results for a model in the workspace.

1. run_model (or get_kpi_table if it has already run). Read the digest, not the
   raw frames.
2. Every mean comes with `ci_half_width`: the half-width of its 95% confidence
   interval over replications. Quote both. A half-width that is a large
   fraction of the mean means the answer is not yet precise -- run more
   replications, or call run_to_precision with a target.
3. With one replication there is no interval at all (null). Never present a
   single-replication number as an answer.
4. Look for the bottleneck: the resource with high utilization AND a long
   queue. Utilization alone is not a bottleneck, and a queue alone may just be
   a burst.
5. Check the model was in steady state: if no warm-up was set and the question
   is a steady-state one, call suggest_warmup and re-run.
6. To compare options, do NOT eyeball two runs -- create scenarios with
   upsert_scenario and call run_scenarios. It reports the DIFFERENCE with its
   own interval and a significance verdict under common random numbers. "Not
   significant" means "not distinguishable at this replication count", not "no
   difference".
7. To find a best setting rather than compare a handful, use optimize with a
   replication budget.

The question to answer: {question}
"""


# ---------------------------------------------------------------- server
def build_server(root: str | Path | None = None, network=None, twin=None, *,
                 twin_dir=None, name: str = "naga", instructions: str = INSTRUCTIONS,
                 tools=None):
    """Build the MCP server with every tool, resource and prompt registered.

    *network* is the `netguard.NetworkPolicy` the human passed at start-up; with
    none, no tool reaches the network, and no tool argument can change that.
    *twin* is the twin session they configured, on the same rule: with none,
    every twin tool refuses; *twin_dir* is the result directory of a twin they
    started, which two tools read. *name* and *instructions* are what the server
    tells a client about itself. *tools* limits the published tools to those names
    (COMPACT_TOOLS for `--tools compact`); None publishes every one.

    A caller that serves the result itself, not through `serve`, must call
    `agent_tools.drop_python_consent()` first, as `serve` does (D186).

    Raises MCPUnavailable when the SDK is not installed.
    """
    server_class = _server_class()
    agent_tools.use_workspace(agent_tools.Workspace(root, network=network, twin=twin,
                                                    twin_dir=twin_dir))
    server = server_class(name, version=__version__, instructions=instructions)

    # The SDK runs a sync tool on a worker thread and serves requests concurrently, while
    # the workspace is one mutable object (a debug run is a live engine session): calls are
    # serialised here, at the one place every MCP call passes.
    def serialised(fn):
        @functools.wraps(fn)        # the SDK reads the signature, hints and docstring off it
        def locked(*args, **kwargs):
            with _TOOL_LOCK:
                return fn(*args, **kwargs)
        return locked

    for spec in agent_tools.TOOLS.values():
        if tools is not None and spec.name not in tools:
            continue
        server.add_tool(serialised(spec.fn), name=spec.name, description=_described(spec),
                        annotations=_annotations(spec))

    @server.resource("naga://reference/blocks", mime_type="text/markdown",
                     description="Every block type with its parameters.")
    def blocks_resource() -> str:
        return _blocks_reference()

    @server.resource("naga://reference/distributions", mime_type="text/markdown",
                     description="Every distribution with its parameters and when to use it.")
    def distributions_resource() -> str:
        return _distributions_reference()

    @server.resource("naga://reference/expressions", mime_type="text/markdown",
                     description="The safe expression language: names, operators, functions.")
    def expressions_resource() -> str:
        return _expressions_reference()

    @server.resource("naga://reference/schema", mime_type="application/json",
                     description="JSON Schema (draft 2020-12) of the model file format.")
    def schema_resource() -> str:
        from nagasim.modelschema import build_json_schema
        return json.dumps(build_json_schema(), indent=2)

    @server.resource("naga://examples/{name}", mime_type="application/json",
                     description="A bundled example model, by file stem "
                                 "(e.g. single_server, job_shop_jackson).")
    def example_resource(name: str) -> str:
        return examples.path(Path(name).stem).read_text(encoding="utf-8")

    @server.prompt(name="build_model_from_description",
                   description="Turn a plain-English process description into a validated model.")
    def build_model_from_description(description: str) -> str:
        return _BUILD_PROMPT.format(description=description)

    @server.prompt(name="analyse_results",
                   description="Read a model's KPIs correctly: confidence intervals, "
                               "bottlenecks, warm-up and scenario comparison.")
    def analyse_results(question: str = "What do these results say?") -> str:
        return _ANALYSE_PROMPT.format(question=question)

    return server


def add_arguments(parser) -> None:
    parser.add_argument("--root", default=".",
                        help="workspace root: the only directory models may be read from or "
                             "written to (default: the current directory)")
    netguard.add_arguments(parser)
    parser.add_argument("--tools", choices=("all", "compact"), default="all",
                        help="all: every tool (default); compact: only what building, running "
                             "and saving a model needs (get_model_format, write_model, "
                             "run_model, ...), so a client pays for fewer tool descriptions")
    parser.add_argument("--twin", dest="twin_dir", default=None, metavar="DIR",
                        help="the result directory of a twin you started (naga twin run "
                             "--out DIR): two read-only tools then answer from it. No tool "
                             "can start a twin or choose this directory")


def serve(args, *, twin=None, name: str = "naga", instructions: str = INSTRUCTIONS) -> int:
    agent_tools.drop_python_consent()       # D186: before anything can run, in this process and its workers
    try:
        server = build_server(args.root, network=netguard.policy_from_args(args), twin=twin,
                              twin_dir=getattr(args, "twin_dir", None),
                              name=name, instructions=instructions,
                              tools=COMPACT_TOOLS if getattr(args, "tools", "all") == "compact" else None)
    except MCPUnavailable as exc:
        print(str(exc))     # stdout is the transport only once the server runs
        return 1
    server.run()
    return 0


def main(argv=None) -> int:
    """`naga mcp [--root DIR] [--allow-network ...]`: every permission is decided here, by the
    person starting the server, and none of them is reachable from a tool argument."""
    parser = argparse.ArgumentParser(
        prog="naga mcp",
        description="Run the MCP server over stdio. File access is confined to --root, and "
                    "network access is off unless you allow it here.")
    add_arguments(parser)
    return serve(parser.parse_args(argv))
