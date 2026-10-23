"""ROADMAP 3.2 item 2: the MCP adapter.

Two layers. The adapter's own shape (resources, prompts, install hint) needs
no SDK beyond building the server, and the integration test drives a real
`naga mcp` subprocess through the SDK's stdio client: initialize, list
tools, then the build-validate-run sequence an agent would perform. The
product's server (its own tools, the --mes-* arguments) is in
test_mcp_server_product.py.

`mcp` is an optional extra, so everything that needs it skips with a clear
reason when it is not installed.
"""
import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nagasim import mcp_server

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "nagasim" / "models"

# find_spec, not importorskip: importing the SDK at collection cost every xdist
# worker ~1 s, and only the worker that runs these tests needs it.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="the MCP SDK is an optional extra: pip install \"nagasim[mcp]\"")


def _text(result) -> str:
    """The text payload of a call_tool / read_resource result.

    The SDK hands back a CallToolResult with `.content`, or a plain list of
    ReadResourceContents with `.content` on each item; both shapes are flattened
    here so the tests do not depend on which mcp major version is installed.
    """
    if isinstance(result, str):
        return result
    items = result if isinstance(result, list) else (getattr(result, "content", None) or [])
    out = []
    for item in items:
        value = getattr(item, "text", None)
        if value is None:
            value = getattr(item, "content", "")
        out.append(value if isinstance(value, str) else "")
    return "".join(out)


# -- adapter shape -----------------------------------------------------------

def test_the_server_publishes_every_tool_resource_and_prompt(tmp_path):
    server = mcp_server.build_server(tmp_path)

    async def surface():
        return (await server.list_tools(), await server.list_resources(),
                await server.list_resource_templates(), await server.list_prompts())

    tools, resources, templates, prompts = asyncio.run(surface())

    from nagasim import agent_tools
    assert {t.name for t in tools} == set(agent_tools.TOOLS)
    assert all(len(t.description) > 80 for t in tools)

    uris = {str(r.uri) for r in resources} - {"ui://naga/study.html"}   # the MCP Apps template
    assert uris == {"naga://reference/blocks", "naga://reference/distributions",
                    "naga://reference/expressions", "naga://reference/schema"}
    assert [getattr(t, "uriTemplate", getattr(t, "uri_template", None)) for t in templates] \
        == ["naga://examples/{name}"]
    assert {p.name for p in prompts} == {"build_model_from_description", "analyse_results",
                                        "run_simulation_study", "study_interview"}


def test_tools_compact_publishes_what_building_a_model_needs_and_nothing_else(tmp_path):
    """D397: `--tools compact` is the whole-model surface, so a client pays for eight tool
    descriptions on every turn instead of every one; it is chosen by whoever starts the
    server, and the default stays every tool."""
    import argparse

    parser = argparse.ArgumentParser()
    mcp_server.add_arguments(parser)
    assert parser.parse_args([]).tools == "all"
    assert parser.parse_args(["--tools", "compact"]).tools == "compact"
    server = mcp_server.build_server(tmp_path, tools=mcp_server.COMPACT_TOOLS)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == mcp_server.COMPACT_TOOLS
    assert {"get_model_format", "write_model", "run_model", "save_model"} <= names


def test_resources_serve_the_live_reference_and_the_bundled_examples(tmp_path):
    server = mcp_server.build_server(tmp_path)

    async def read(uri):
        return _text(await server.read_resource(uri))

    blocks = asyncio.run(read("naga://reference/blocks"))
    assert "## PROCESS" in blocks and "multi-output" in blocks
    assert "## Exponential" in asyncio.run(read("naga://reference/distributions"))
    assert "NQ" in asyncio.run(read("naga://reference/expressions"))
    assert json.loads(asyncio.run(read("naga://reference/schema")))["$defs"]["block"]
    example = json.loads(asyncio.run(read("naga://examples/single_server")))
    assert [b["type"] for b in example["blocks"]] == ["CREATE", "PROCESS", "DISPOSE"]


def test_prompts_carry_high_signal_guidance(tmp_path):
    server = mcp_server.build_server(tmp_path)

    async def render(name, args):
        return await server.get_prompt(name, args)

    build = str(asyncio.run(render("build_model_from_description",
                                   {"description": "a bakery with two ovens"})))
    assert "a bakery with two ovens" in build
    assert "write_model" in build and "time_limit" in build
    analyse = str(asyncio.run(render("analyse_results", {"question": "where is the bottleneck"})))
    assert "confidence interval" in analyse and "run_scenarios" in analyse


def test_tool_descriptions_include_the_per_argument_guidance(tmp_path):
    server = mcp_server.build_server(tmp_path)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert "- model:" in tools["add_block"].description
    schema = getattr(tools["add_block"], "input_schema", None) or tools["add_block"].inputSchema
    assert set(schema["required"]) == {"model", "type"}


def test_a_server_started_without_allow_network_refuses_a_remote_binding(tmp_path):
    """The permission lives in the server's start arguments. Started plainly,
    no model file and no tool call can reach the network."""
    from nagasim import agent_tools

    mcp_server.build_server(tmp_path)                       # no --allow-network
    assert agent_tools.workspace().network.allow_network is False

    agent_tools.call("new_model", name="m")
    agent_tools.workspace().models["m"].set_data_sources(
        [{"name": "remote", "kind": "json_url", "allow_network": True,
          "location": "https://example.invalid/live.json"}])
    agent_tools.workspace().models["m"].set_data_bindings(
        [{"name": "live", "source": "remote", "column": "svc", "transform": "mean",
          "target": "simulation.replications", "enabled": True}])

    answer = agent_tools.call("resolve_data_bindings", model="m")
    assert "network access is off" in answer["bindings"][0]["problem"]

    parsed = mcp_server.argparse.ArgumentParser()           # the flags a human would pass
    from nagasim import netguard
    netguard.add_arguments(parsed)
    policy = netguard.policy_from_args(parsed.parse_args(["--allow-network",
                                                          "--allow-host", "data.example.com"]))
    assert policy.allow_network and policy.allowed_hosts == frozenset({"data.example.com"})


def test_the_install_hint_names_the_optional_extra():
    assert "nagasim[mcp]" in mcp_server.INSTALL_HINT
    assert "nagasim.agent_tools" in mcp_server.INSTALL_HINT


def test_naga_mcp_is_a_command_of_the_engine_cli(capsys):
    """`naga mcp --help` reaches this module's parser; the --mes-* arguments are the product's."""
    from nagasim import cli

    assert cli.COMMANDS["mcp"][0] == "mcp_server"
    with pytest.raises(SystemExit):
        cli.main(["mcp", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: naga mcp") and "--root" in out and "--allow-network" in out
    assert "--mes-" not in out


@pytest.mark.slow
def test_the_engine_server_runs_without_the_product(tmp_path):
    """`naga mcp` in a fresh interpreter: 96 tools, resources under naga://, no product import."""
    code = (
        "import asyncio, sys\n"
        "from nagasim import mcp_server\n"
        "server = mcp_server.build_server(sys.argv[1])\n"
        "tools = asyncio.run(server.list_tools())\n"
        "resources = asyncio.run(server.list_resources())\n"
        "assert 'product' not in sys.modules and 'PySide6' not in sys.modules\n"
        "print(len(tools), sorted(str(r.uri) for r in resources)[0])\n")
    out = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    count, first = out.stdout.split()
    assert count == "96" and first.startswith("naga://reference/")


def test_tool_calls_batched_by_a_client_run_one_after_the_other(tmp_path, monkeypatch):
    """The SDK runs a sync tool on a worker thread and serves requests concurrently,
    and the workspace is one mutable object: two debug_run on one model must share
    one run, not each build a session and the second overwrite the first."""
    import time
    from nagasim.agent_tools import debug

    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    built = []

    class SlowSession(debug.SimSession):
        def __init__(self, *args, **kwargs):
            built.append(1)
            time.sleep(0.2)             # the second call arrives while the first still starts
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(debug, "SimSession", SlowSession)
    server = mcp_server.build_server(tmp_path)

    async def batch():
        opened = await server.call_tool("open_model", {"path": "single_server.json"})
        model = json.loads(_text(opened))["model"]
        return await asyncio.gather(*(server.call_tool("debug_run", {"model": model, "to": 10})
                                      for _ in range(2)))

    answers = [json.loads(_text(result)) for result in asyncio.run(batch())]
    assert len(built) == 1, f"{len(built)} sessions built: the calls ran at once"
    assert [(a["state"], a["t"]) for a in answers] == [("time", 10.0)] * 2, answers


def test_serving_drops_the_callers_python_consent(monkeypatch, tmp_path):
    """D186: `naga mcp` forgets the variable before a single tool can run."""
    import argparse
    import os
    from nagasim.core.agents import PYTHON_CONSENT
    monkeypatch.setenv(PYTHON_CONSENT, "1")
    absent_at = []          # the variable's absence where the tools could be built and served

    class Server:
        def run(self):
            absent_at.append(("run", PYTHON_CONSENT not in os.environ))

    def build_server(*args, **kwargs):
        absent_at.append(("build", PYTHON_CONSENT not in os.environ))
        return Server()

    monkeypatch.setattr(mcp_server, "build_server", build_server)
    parser = argparse.ArgumentParser()
    mcp_server.add_arguments(parser)
    assert mcp_server.serve(parser.parse_args(["--root", str(tmp_path)])) == 0
    assert absent_at == [("build", True), ("run", True)]


# -- the real thing: a stdio subprocess -------------------------------------

@pytest.mark.slow
def test_a_client_drives_the_stdio_server_end_to_end(tmp_path):
    """`naga mcp --root tmp` as a subprocess, driven by the SDK's own stdio
    client: initialize, tools/list, then build -> connect -> validate -> run."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nagasim", "mcp", "--root", str(tmp_path)],
        cwd=str(ROOT),
        env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "QT_QPA_PLATFORM": "offscreen",
             "PATH": __import__("os").environ.get("PATH", "")},
    )

    async def session():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                info = await client.initialize()
                tools = await client.list_tools()
                names = {t.name for t in tools.tools}

                async def call(tool, **arguments):
                    payload = json.loads(_text(await client.call_tool(tool, arguments)))
                    assert "error" not in payload, payload
                    return payload

                await call("new_model", name="mm1")
                await call("add_block", model="mm1", type="CREATE", name="arrive",
                           params={"distribution": "Exponential",
                                   "dist_params": {"rate": 1.0}, "max_arrivals": 0})
                await call("add_block", model="mm1", type="PROCESS", name="serve",
                           params={"resource_name": "Server", "distribution": "Exponential",
                                   "dist_params": {"rate": 1.25}})
                await call("add_block", model="mm1", type="DISPOSE", name="leave")
                await call("connect", model="mm1", source="arrive", target="serve")
                await call("connect", model="mm1", source="serve", target="leave")
                await call("upsert_record", model="mm1", section="resources",
                           record={"name": "Server", "capacity": 1})
                await call("set_simulation_settings", model="mm1", run_length=2000,
                           warmup=200, replications=4)
                report = await call("validate_model", model="mm1")
                digest = await call("run_model", model="mm1")
                return info, names, report, digest

    info, names, report, digest = asyncio.run(asyncio.wait_for(session(), timeout=180))

    server_info = getattr(info, "server_info", None) or info.serverInfo
    assert server_info.name == "naga"
    assert {"new_model", "add_block", "connect", "validate_model", "run_model"} <= names
    assert report["ok"] and report["errors"] == 0
    utilization = digest["resources"]["Server"]["Usage % (snapshot)"]["mean"] / 100.0
    assert utilization == pytest.approx(0.8, abs=0.1)


@pytest.mark.slow
def test_the_stdio_server_refuses_a_python_model_even_when_started_with_consent(tmp_path):
    """D186 end to end: `naga mcp` started with NAGASIM_ALLOW_AGENT_PYTHON=1 in its own
    environment still answers E_AGENT_PYTHON_REFUSED for a model that carries Python."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from test_agents_python import _ring

    (tmp_path / "py.json").write_text(json.dumps(_ring(python=True)), encoding="utf-8")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nagasim", "mcp", "--root", str(tmp_path)],
        cwd=str(ROOT),
        env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "QT_QPA_PLATFORM": "offscreen",
             "NAGASIM_ALLOW_AGENT_PYTHON": "1",
             "PATH": __import__("os").environ.get("PATH", "")},
    )

    async def session():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()

                async def call(tool, **arguments):
                    return json.loads(_text(await client.call_tool(tool, arguments)))

                opened = await call("open_model", path="py.json", name="py")
                return opened, await call("run_model", model="py")

    opened, run = asyncio.run(asyncio.wait_for(session(), timeout=180))
    assert "error" not in opened, opened
    assert run["error"]["code"] == "E_AGENT_PYTHON_REFUSED", run


def test_image_answers_become_image_content(tmp_path):
    answer = {"version": "v001", "image": {"mime_type": "image/png", "data": "iVBORw0KGgo="}}
    image, text = mcp_server._with_image(answer).content
    assert image.model_dump(by_alias=True)["mimeType"] == "image/png"    # attribute name differs per SDK line
    assert (image.type, image.data) == ("image", "iVBORw0KGgo=")
    assert json.loads(text.text) == {"version": "v001"}
    plain = {"ok": True}
    assert mcp_server._with_image(plain) is plain


def test_study_view_reaches_the_client_as_an_image(tmp_path):
    pytest.importorskip("matplotlib")
    shutil.copy(EXAMPLES / "single_server.json", tmp_path / "single_server.json")
    server = mcp_server.build_server(tmp_path)
    layout = json.loads((tmp_path / "single_server.json").read_text(encoding="utf-8"))

    async def go():
        start = json.loads(_text(await server.call_tool("study_start", {"question": "q?"})))
        await server.call_tool("study_step", {"study": start["study"], "layout": layout, "note": "n"})
        return await server.call_tool("study_view", {"study": start["study"]})

    kinds = [c.type for c in asyncio.run(go()).content]
    assert kinds == ["image", "text"]


# -- MCP Apps: the study control room in the chat ------------------------------

needs_apps = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None or importlib.util.find_spec("mcp.server.apps") is None,
    reason="MCP Apps needs mcp 2.x (mcp.server.apps)")


@needs_apps
def test_study_show_is_bound_to_the_app(tmp_path):
    from nagasim import studyapp
    tools = {t.name: t for t in asyncio.run(mcp_server.build_server(tmp_path).list_tools())}
    ui = {n: tools[n].meta["ui"] for n in
          ("study_show", "study_page_html", "study_revert", "study_accept")}
    assert all(u["resourceUri"] == studyapp.URI for u in ui.values())
    assert ui["study_page_html"]["visibility"] == ["app"]
    assert ui["study_show"]["visibility"] == ["model", "app"]


@needs_apps
def test_the_app_resource_is_listed(tmp_path):
    from nagasim import studyapp
    server = mcp_server.build_server(tmp_path)

    async def both():
        return await server.list_resources(), await server.read_resource(studyapp.URI)

    resources, read = asyncio.run(both())
    listed = [r for r in resources if str(r.uri) == studyapp.URI]
    assert [r.mime_type for r in listed] == ["text/html;profile=mcp-app"]
    assert _text(read) == studyapp.HTML


@needs_apps
def test_other_tools_have_no_ui_meta(tmp_path):
    tools = asyncio.run(mcp_server.build_server(tmp_path).list_tools())
    study = {t.name for t in tools if t.name.startswith("study_")}
    assert [t.name for t in tools if t.name not in study and (t.meta or {}).get("ui")] == []


@needs_apps
def test_initialize_advertises_the_apps_extension(tmp_path):
    server = mcp_server.build_server(tmp_path)
    caps = server._lowlevel_server.get_capabilities(
        __import__("mcp.server.lowlevel.server", fromlist=["NotificationOptions"])
        .NotificationOptions(), {})
    assert "io.modelcontextprotocol/ui" in (caps.extensions or {})


def test_mcp_unknown_tool_suggests(tmp_path):
    server = mcp_server.build_server(tmp_path)
    result = asyncio.run(server.call_tool("validate_modle", {}))
    error = json.loads(_text(result))["error"]
    assert error["code"] == "E_UNKNOWN_TOOL" and "validate_model" in error["did_you_mean"]


def test_mcp_unknown_argument_suggests(tmp_path):
    server = mcp_server.build_server(tmp_path)
    result = asyncio.run(server.call_tool("open_model", {"pth": "single_server.json"}))
    error = json.loads(_text(result))["error"]
    assert error["code"] == "E_BAD_ARGUMENT" and "path" in error["did_you_mean"]


def test_a_tool_the_server_hides_cannot_be_called_by_name(tmp_path):
    """--tools compact hides most tools; calling one by name must not run it, and the
    suggestions name only tools this server exposes."""
    server = mcp_server.build_server(tmp_path, tools=mcp_server.COMPACT_TOOLS)
    hidden = next(n for n in sorted(mcp_server.agent_tools.TOOLS) if n not in mcp_server.COMPACT_TOOLS)
    error = json.loads(_text(asyncio.run(server.call_tool(hidden, {}))))["error"]
    assert error["code"] == "E_UNKNOWN_TOOL"
    assert set(error.get("did_you_mean") or []) <= mcp_server.COMPACT_TOOLS
