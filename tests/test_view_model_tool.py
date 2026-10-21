"""view_model (D681): an agent that can only read JSON gets a picture of the model.

The PNG needs matplotlib (nagasim[plot]); without it the tool answers the text, which a test
forces by monkeypatch. The MCP case needs the SDK, an optional extra, and skips without it.
"""
import asyncio
import base64
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import assistant, flowchart, mcp_server

EXAMPLES = Path(__file__).resolve().parents[1] / "nagasim" / "models"
PNG = b"\x89PNG\r\n\x1a\n"

needs_sdk = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="the MCP SDK is an optional extra: pip install \"nagasim[mcp]\"")


@pytest.fixture
def ws(tmp_path):
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path="single_server.json", name="m")
    return tmp_path


def test_png_is_an_image_with_the_block_list(ws):
    pytest.importorskip("matplotlib")
    out = T.call("view_model", model="m")
    assert out["format"] == "png" and out["image"]["mime_type"] == "image/png"
    assert base64.b64decode(out["image"]["data"]).startswith(PNG)
    assert "- process_1 (PROCESS)" in out["text"]


def test_svg_and_text_are_the_drawing_source_and_the_list(ws):
    svg = T.call("view_model", model="m", format="svg")
    assert svg["svg"].startswith("<svg") and "process_1" in svg["svg"]
    assert "image" not in svg and "text" not in svg
    text = T.call("view_model", model="m", format="text")
    assert "image" not in text and "- create_1 -> process_1" in text["text"]


def test_a_png_request_without_matplotlib_answers_the_text(ws, monkeypatch):
    monkeypatch.setattr(flowchart, "to_png", lambda geom, title="": None)
    out = T.call("view_model", model="m")
    assert out["format"] == "text" and "image" not in out
    assert "nagasim[plot]" in out["note"] and "- process_1 (PROCESS)" in out["text"]


def test_a_bad_format_or_an_unknown_model_is_a_structured_error(ws):
    assert T.call("view_model", model="m", format="pdf")["error"]["code"] == "E_BAD_ARGUMENT"
    assert T.call("view_model", model="nope")["error"]["code"] == "E_UNKNOWN_MODEL"


def test_it_is_read_only_and_offered_on_the_mcp_servers_only():
    assert T.TOOLS["view_model"].read_only
    assert "view_model" not in assistant.CURATED_TOOL_NAMES | assistant.COMPACT_TOOL_NAMES


@needs_sdk
def test_the_picture_reaches_a_client_as_image_content(ws):
    pytest.importorskip("matplotlib")
    server = mcp_server.build_server(ws)

    async def go():
        await server.call_tool("open_model", {"path": "single_server.json", "name": "again"})
        return await server.call_tool("view_model", {"model": "again"})

    image, text = asyncio.run(go()).content
    assert (image.type, image.model_dump(by_alias=True)["mimeType"]) == ("image", "image/png")   # the attribute name differs per SDK line
    assert base64.b64decode(image.data).startswith(PNG)
    assert "image" not in json.loads(text.text) and json.loads(text.text)["model"] == "again"
