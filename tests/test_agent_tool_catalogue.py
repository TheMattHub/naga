"""ARCH-REVIEW B5: the tool catalogue an agent (MCP server, assistant) sees --
name, description, input schema, read_only -- must not drift when the tool
bodies are refactored (`edit_tool`/`_records`) or drift silently by accident.
Guards against both: a missing tool and a schema whose properties changed
because a signature moved. This file pins the engine's 92 tools; the product's
ten are in `test_agent_tool_catalogue_product.py`."""
import json
from pathlib import Path

from nagasim import agent_tools as T

SNAPSHOT = Path(__file__).resolve().parent / "data" / "agent_tool_catalogue.json"


def test_tool_catalogue_matches_the_committed_snapshot():
    committed = {t["name"]: t for t in json.loads(SNAPSHOT.read_text(encoding="utf-8"))}
    # The registry is shared: a product import elsewhere in this process must not count.
    current = {t["name"]: t for t in json.loads(json.dumps(T.tool_schemas(), sort_keys=True))
               if T.TOOLS[t["name"]].origin.startswith("nagasim.")}
    assert len(committed) == 92
    assert set(current) == set(committed)
    for name, spec in current.items():
        assert spec == committed[name], name
