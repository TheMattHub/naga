"""find_tool: search the tool catalogue by intent (nagasim.toolsearch)."""
from __future__ import annotations

from nagasim import toolsearch

from .core import TOOLS, tool

# The names an MCP server with a tools subset publishes (set by mcp_server.build_server); None = every tool.
exposed: frozenset[str] | None = None
_INDEX: tuple[int, toolsearch.Index] | None = None


def catalogue_index() -> toolsearch.Index:
    """The index over every registered tool, rebuilt when the registry's size changes (a product adds tools)."""
    global _INDEX
    if _INDEX is None or _INDEX[0] != len(TOOLS):
        specs = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                 for t in TOOLS.values()]
        _INDEX = (len(TOOLS), toolsearch.index(specs))
    return _INDEX[1]


@tool(read_only=True)
def find_tool(query: str, limit: int = 5) -> dict:
    """Find the tools that fit what you want to do, by a few words or a tool name you guessed
    ("validate against a formula", "make slides", "run_study"). Answers {"tools": [{name, score,
    summary, args}]}, best first, `args` being the required arguments. Use it when no tool in your list is obviously right."""
    return {"tools": toolsearch.search(catalogue_index(), query, limit=max(1, min(int(limit), 20)), choices=exposed)}
