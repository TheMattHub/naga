"""The shareable HTML player (PHASE7 7.7, D196-D201): `nagasim/player.py`.

A player file leaves its author's machine. These tests pin what may be in it: a constant
page plus one data block that cannot hold markup, no address of any kind, nothing of the
model beyond the tape, and a tape that says what `Timeline` says.
"""
import base64
import hashlib
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from nagasim import api, player
from nagasim.core import tape, trace as trace_module
from nagasim.core.trace import TraceEvent
from nagasim.experiments import BASELINE, flatten_kpis
from nagasim.exporters import EXPORTER_REGISTRY
from test_tape import MODELS, SIX, assert_tape_equals_timeline, recorded

SERVER = str(MODELS / "single_server.json")
SAFE = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
        '<rect width="24" height="24"/></svg>')


def split(page: str) -> tuple[str, dict]:
    """(the data block as written, the data in it). Also checks the file's whole
    shape: the constant page, the block, the rest of the constant page."""
    before, after = player._shell().split("__TAPE__")
    assert page.startswith(before) and page.endswith(after)
    block = page[len(before):len(page) - len(after)]
    return block, json.loads(block)


def no_address(page: str) -> None:
    """Nothing in the file can make a browser ask for anything."""
    assert page.isascii()
    assert "//" not in page                     # no address, absolute or scheme-relative
    block, _data = split(page)
    fixed = page.replace(block, "")             # the constant page: all that is not data
    assert not re.search(r"https?:|ftp:|wss?:|@import|url\(", fixed, re.I)
    markup = fixed.replace(player._SCRIPT, "")
    assert not re.search(r"\b(src|href|srcset|action|poster|background|data)\s*=", markup, re.I)
    assert not re.search(r"<(link|img|iframe|object|embed|a|base|form|video|audio|source)\b",
                         markup, re.I)
    for picture in _data["pics"]:               # the pictures are base64: look inside them too
        svg = base64.b64decode(picture.partition(",")[2]).decode("utf-8")
        svg = re.sub(r'xmlns(:\w+)?="[^"]*"', "", svg)     # a namespace names, never fetches
        assert not re.search(r"https?:|//|@import|url\(", svg, re.I), svg


def test_the_address_check_looks_inside_a_picture():
    layout, recorder = recorded("two_cell_line.json")
    page = player.build_player(layout, {BASELINE: recorder}, samples=250)
    block, data = split(page)
    evil = SAFE.replace("<rect", "<style>@import url(http://evil.example/x.css)</style><rect")
    data["pics"] = ["data:image/svg+xml;base64," + base64.b64encode(evil.encode()).decode()]
    no_address(page)
    with pytest.raises(AssertionError):
        no_address(page.replace(block, player._embed(data)))


# -- state at t equals Timeline, in the file as written ------------------------------
@pytest.mark.parametrize("model", SIX)
def test_the_written_file_says_what_timeline_says(model, tmp_path):
    layout, recorder = recorded(model)
    out = tmp_path / "run.html"
    page = player.build_player(layout, {BASELINE: recorder}, title=model, samples=400, path=out)
    assert out.read_bytes() == page.encode("ascii")
    _block, data = split(out.read_text(encoding="ascii"))
    run = data["runs"][BASELINE]
    assert run["scene"]["view"] == tape.default_view(layout)
    assert_tape_equals_timeline(layout, recorder.events, run)
    no_address(page)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["run.html"]   # one file, there


@pytest.mark.slow
def test_every_bundled_model_makes_a_player_with_no_address_in_it():
    for path in sorted(MODELS.glob("*.json")):
        layout, recorder = recorded(path.name)
        for view in tape.VIEWS:
            page = player.build_player(layout, {BASELINE: recorder}, view=view, samples=250)
            no_address(page)
            _block, data = split(page)
            assert_tape_equals_timeline(layout, recorder.events, data["runs"][BASELINE], picks=10)


# -- text from a model is data --------------------------------------------------------
HOSTILE = ['</script><script>alert(1)</script>', '<img src=x onerror=alert(1)>',
           '"double" \'single\' `tick`', 'back\\slash \\u003c \\" end', 'line\u2028sep\u2029arator',
           '<!-- --> ]]> </ScRiPt >', '&lt;b&gt; &amp;', 'https://evil.example/x?a=1&b=2//c',
           'javascript:alert(1)', '{{7*7}} ${7*7} __TAPE__ __SCRIPT__']


def hostile_run():
    """A layout and a hand-built trace whose every name is one of HOSTILE."""
    (script, image, quotes, slash, separators, comment, entity, address, scheme,
     template) = HOSTILE
    blocks = [{"name": image, "description": "d", "type": "CREATE", "position": [0, 0],
               "params": {"node": "n1"}},
              {"name": "submodel_7", "description": script, "type": "PROCESS", "position": [200, 0],
               "params": {"resource_name": separators}}]
    layout = {"blocks": blocks, "links": [{"source": image, "target": "submodel_7"}],
              "entities": [{"name": quotes, "shape": slash, "color": comment}],
              "levels": [{"name": entity, "capacity": 10}, {"name": comment}],
              "scenarios": [{"name": template, "overrides": []}]}
    events = [
        TraceEvent(0.0, "resource_state", -1, "", "", None, resource=separators, state="capacity", value=1),
        TraceEvent(0.0, "level_changed", -1, "", "", None, resource=entity, value=1.0),
        TraceEvent(0.0, "level_changed", -1, "", "", None, resource=comment, value=2.0),
        TraceEvent(1.0, "entity_created", 1, quotes, image, None),
        TraceEvent(1.0, "entity_moved", 1, quotes, image, "submodel_7"),
        TraceEvent(1.0, "entity_seized", 1, quotes, "submodel_7", separators),
        TraceEvent(2.0, "mover_moved", 0, "", "", scheme, resource=scheme, state=address,
                   duration=1.0, path=("n1",)),
        TraceEvent(3.0, "entity_created", 2, slash, image, None),
        TraceEvent(4.0, "level_changed", -1, "", "", None, resource=entity, value=float("inf")),
        TraceEvent(5.0, "entity_moved", 1, quotes, "submodel_7", image, duration=2.0),
        TraceEvent(8.0, "entity_disposed", 1, quotes, image, None)]
    results = {"system": pd.DataFrame({"Run": [0, 0, 1, 1], "Metric": [script, "Run"] * 2,
                                       "Value": [1.0, 0, 3.0, 1]}),
               "user_summary": pd.DataFrame({"Run": [0, 1], "Statistic": [image, image],
                                             "Value": [float("nan"), 2.0]})}
    return layout, events, results


def strings(value) -> set:
    if isinstance(value, dict):
        return set().union(set(value), *(strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(strings(item) for item in value)) if value else set()
    return {value} if isinstance(value, str) else set()


def test_text_from_a_model_reaches_the_page_as_data_and_only_as_data():
    layout, events, results = hostile_run()
    address, template = HOSTILE[7], HOSTILE[9]
    page = player.build_player(layout, {template: events, HOSTILE[0]: events}, results=results,
                               title=address, view="schematic")
    block, data = split(page)
    # The page is the constant page around one block, and the block cannot hold a tag,
    # an entity, a closing script or an address: every such character is escaped.
    assert not set("<>&\n") & set(block)
    assert not re.search(r"(?<!\\)/", block)       # every slash is written as backslash-slash
    assert page.lower().count("<script") == 2 and page.lower().count("</script") == 2
    no_address(page)
    # ... and every string comes back exactly as the model had it.
    found = strings(data)
    for text in HOSTILE:
        assert text in found or text.upper() in found, text
    assert data["title"] == address and data["scenarios"] == [template, HOSTILE[0]]
    scene = data["runs"][template]["scene"]
    assert [b["name"] for b in scene["blocks"]] == [HOSTILE[1], "submodel_7"]
    assert scene["blocks"][1]["label"] == HOSTILE[0]
    assert scene["types"][0] == {"name": HOSTILE[2].upper(), "shape": HOSTILE[3].lower(),
                                 "color": "#000000"}       # an unknown colour is never passed on
    assert scene["resources"] == [HOSTILE[4]] and scene["fleets"] == [HOSTILE[8]]
    assert [level["name"] for level in scene["levels"]] == [HOSTILE[6], HOSTILE[5]]
    assert {row[0] for row in data["kpis"]} == {"system/" + HOSTILE[0], "user/" + HOSTILE[1]}
    # A number that is not finite is null, not a token JSON does not have.
    assert "NaN" not in block and "Infinity" not in block
    assert data["runs"][template]["frames"][-1]["lv"] == {"0": None, "1": 2.0}


def test_the_script_puts_text_on_the_page_only_as_text():
    """The other half of the proof: no name can become an element, an attribute or
    code, because the script has no way to make one from a string."""
    script = player._SCRIPT
    banned = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval",
              "Function", "fetch", "XMLHttpRequest", "WebSocket", "EventSource", "import",
              "setAttribute", "srcdoc", "location", "cookie", "localStorage", "sessionStorage",
              "navigator", "postMessage", "open", "setTimeout", "setInterval", "Worker",
              "importScripts", "RTCPeerConnection", "createElementNS", "indexedDB"]
    assert [word for word in banned if re.search(rf"\b{re.escape(word)}\b", script)] == []
    assert "//" not in script and "\\" not in script       # block comments only, no escapes
    assert len(re.findall(r"\.src\b", script)) == 1       # the one place a picture is loaded
    # One place makes elements, from a tag written in the script itself.
    assert script.count("createElement") == 1 and "document.createElement(tag)" in script
    tags = set(re.findall(r"\badd\(([^,)]*)", script))
    assert tags == {"tag", '"div"', '"span"', '"tr"', '"th"', '"td"', '"option"'}
    # Rule 13: the drawer reads the tape and never a trace event.
    named = [kind for kind in sorted(trace_module._RECORDED_EVENTS)
             if re.search(rf"\b{kind}\b", script)]
    assert named == []


def test_the_policy_pins_the_one_script_and_refuses_every_request(tmp_path):
    out = tmp_path / "run.html"
    player.build_player(*recorded("two_cell_line.json")[:1], {BASELINE: []}, path=out)
    page = out.read_bytes().decode("ascii")
    assert b"\r" not in out.read_bytes()
    policy = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', page)
    script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    assert policy.group(1) == ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
                               f"script-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'")
    assert page.index("Content-Security-Policy") < page.index("<style") < page.index("<script")
    assert page.startswith("<!doctype html>") and '<meta charset="utf-8">' in page[:120]


# -- only the tape leaves the machine -------------------------------------------------
def test_the_file_holds_the_tape_and_nothing_else_of_the_model_or_the_machine(tmp_path):
    layout, recorder = recorded("warehouse_forklifts.json")
    private = {**layout,
               # Pictures a model may carry: the engine reads none of them (D198).
               "entities": [{**row, "graphic": SAFE} for row in layout["entities"]],
               "fleets": [{**row, "graphic": SAFE} for row in layout["fleets"]],
               "submodels": {"asset": {"info": {"graphics": {"states": {"idle": SAFE}}},
                                       "blocks": [], "links": []}},
               "info": {"author": "Author Sentinel", "notes": str(Path.home() / "notes.txt")},
               "background": {"path": str(Path.home() / "plans" / "floor-sentinel.png")},
               "data_sources": [{"name": "erp", "kind": "json_url",
                                 "location": "https://erp.internal.example/api?token=SECRET-TOKEN"}],
               "data_bindings": [{"source": "erp", "target": "resources.Forklifts.capacity",
                                  "column": "column-sentinel"}],
               "variables": [{"name": "v", "initial": "EXPRESSION_SENTINEL + 1"}]}
    results = api.run_model(layout, replications=2, run_length=60, warmup=0)
    out = tmp_path / "private-out-sentinel.html"
    page = player.build_player(private, {BASELINE: recorder}, results=results,
                               title="shared", samples=250, path=out)
    plain = page.replace("\\/", "/").replace("\\\\", "\\")
    for secret in (str(Path.home()), Path.home().as_posix(), "Author Sentinel", "notes.txt",
                   "floor-sentinel", "erp.internal.example", "SECRET-TOKEN", "column-sentinel",
                   "EXPRESSION_SENTINEL", "private-out-sentinel", "json_url", "holding_cost",
                   "dist_params", "svg", "w3.org"):
        assert secret not in plain, secret
    _block, data = split(page)
    assert set(data) == {"title", "scenarios", "runs", "kpis", "pics", "art"}
    assert set(data["runs"][BASELINE]) == {"scene", "t", "frames", "truncated", "events",
                                           "seed", "of"}
    assert data["pics"] == []       # the engine reads no picture from a model (D198)
    assert all(row[0].startswith(("system/", "user/")) for row in data["kpis"])


def test_a_picture_is_the_callers_checked_svg_as_a_data_image():
    layout, recorder = recorded("two_cell_line.json")
    name = layout["blocks"][0]["name"]
    unsafe = SAFE.replace("<rect", '<script>alert(1)</script><rect')
    handler = SAFE.replace("<rect", '<rect onload="alert(1)"')
    unsized = SAFE.replace(' width="24" height="24">', ">")
    pictures = {"blocks": {name: {"size": (160, 80), "states": {"idle": SAFE, "busy": unsafe}},
                           "other": {"size": (1, 1), "states": {"idle": handler}}},
                "entities": {"DEFAULT": SAFE, "BAD": unsized},
                "fleets": {"Trucks": "not an svg"},
                "fills": {"Tank": (name, 50)}}
    page = player.build_player(layout, {BASELINE: recorder}, pictures=pictures, samples=250)
    _block, data = split(page)
    assert data["pics"] == ["data:image/svg+xml;base64," + base64.b64encode(SAFE.encode()).decode()]
    assert data["art"] == {"blocks": {name: {"w": 160.0, "h": 80.0, "states": {"idle": 0}}},
                           "types": {"DEFAULT": 0}, "fleets": {}, "fills": {"Tank": [name, 50.0]}}
    no_address(page)                    # the SVG's own namespace address is inside the base64
    assert "w3.org" not in page and "alert" not in page.replace(player._SCRIPT, "")


# -- replications ---------------------------------------------------------------------
def test_this_run_and_all_replications_are_separate_and_labelled():
    layout, recorder = recorded("single_server.json")
    results = api.run_model(SERVER, replications=4, seed=7, run_length=200, warmup=0)
    _block, data = split(player.build_player(layout, {BASELINE: recorder}, results=results))
    run = data["runs"][BASELINE]
    assert (run["seed"], run["of"]) == (7, 4)
    flat = flatten_kpis(results)
    rows = {row[0]: row for row in data["kpis"]}
    assert set(rows) == {kpi for kpi in flat["KPI"] if kpi.startswith(("system/", "user/"))}
    kpi = "system/Total Entities Created"
    values = flat[flat["KPI"] == kpi].sort_values("Run")["Value"].tolist()
    assert rows[kpi][1] == BASELINE and rows[kpi][2] == pytest.approx(sum(values) / 4)
    assert rows[kpi][3] <= rows[kpi][2] <= rows[kpi][4]
    assert rows[kpi][5] == values[0]                # the recorded replication: the first seed
    single = api.run_model(SERVER, replications=1, seed=3, run_length=200, warmup=0)
    _block, data = split(player.build_player(layout, {BASELINE: recorder}, results=single))
    assert data["kpis"] is None and (data["runs"][BASELINE]["seed"], data["runs"][BASELINE]["of"]) == (3, 1)
    _block, data = split(player.build_player(layout, {BASELINE: recorder}))
    assert data["kpis"] is None and "seed" not in data["runs"][BASELINE]


# -- limits ---------------------------------------------------------------------------
def test_over_the_size_limit_the_samples_halve_and_then_the_run_is_refused(monkeypatch):
    layout, recorder = recorded("two_cell_line.json")
    sizes = {samples: len(player.build_player(layout, {BASELINE: recorder}, samples=samples))
             for samples in (1000, 500, 250)}
    assert sizes[1000] > sizes[500] > sizes[250]
    monkeypatch.setattr(player, "MAX_BYTES", sizes[500])
    _block, data = split(player.build_player(layout, {BASELINE: recorder}, samples=1000))
    assert len(data["runs"][BASELINE]["t"]) == 501
    monkeypatch.setattr(player, "MAX_BYTES", sizes[250] - 1)
    with pytest.raises(ValueError, match="at 250 samples, over the .* byte limit"):
        player.build_player(layout, {BASELINE: recorder}, samples=1000)


def test_a_truncated_trace_and_an_empty_one_still_make_a_page():
    layout, recorder = recorded("two_cell_line.json", max_events=300)
    _block, data = split(player.build_player(layout, {BASELINE: recorder}, samples=250))
    run = data["runs"][BASELINE]
    assert run["truncated"] is True and run["events"] == 300 and run["t"][-1] < 200
    _block, data = split(player.build_player(layout, {BASELINE: []}))
    assert data["runs"][BASELINE]["t"] == [0.0]
    with pytest.raises(ValueError, match="at least one recorded run"):
        player.build_player(layout, {})


def test_two_builds_are_the_same_bytes():
    layout, recorder = recorded("warehouse_forklifts.json")
    assert player.build_player(layout, {BASELINE: recorder}, title="t", samples=300) \
        == player.build_player(layout, {BASELINE: recorder}, title="t", samples=300)


# -- the exporter and the command line ------------------------------------------------
def test_the_player_is_an_exporter_that_needs_the_trace(tmp_path):
    entry = EXPORTER_REGISTRY["player"]
    assert (entry.name, entry.extension, entry.needs_trace) == ("player", ".html", True)
    assert [name for name, other in EXPORTER_REGISTRY.items() if other.needs_trace] == ["player"]
    with pytest.raises(ValueError, match="--player"):
        entry.write({}, tmp_path / "none.html", layout={})
    assert list(tmp_path.iterdir()) == []


def test_naga_run_player_writes_the_one_file_it_was_asked_for(tmp_path, capsys):
    """The I/O surface (ARCHITECTURE section 6, rule 10): one file, at the caller's
    path, and nothing a model names is opened or fetched to write it."""
    short, long = tmp_path / "a" / "short.html", tmp_path / "b" / "long.html"
    short.parent.mkdir()
    long.parent.mkdir()
    run = [SERVER, "--replications", "2", "--run-length", "100", "--warmup", "0"]
    assert api.main([*run, "--player", str(short)]) == 0
    assert api.main([*run, "--export", f"player:{long}"]) == 0
    assert "player written to" in capsys.readouterr().out
    assert short.read_bytes() == long.read_bytes()
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) \
        == ["a", "a/short.html", "b", "b/long.html"]
    _block, data = split(short.read_text(encoding="ascii"))
    assert data["title"] == "single_server" and data["runs"][BASELINE]["of"] == 2
    no_address(short.read_text(encoding="ascii"))



# -- scenarios (D200) -----------------------------------------------------------------
def test_scenarios_share_one_file_each_drawn_on_its_own_layout():
    """Review focus: a scenario may move a node or change a fleet's size, so each run
    is drawn on the layout with its own overrides, not on the baseline's."""
    from nagasim import experiments
    from nagasim.core.schema import load_layout

    layout = load_layout(str(MODELS / "warehouse_forklifts.json"))
    result = experiments.run_scenarios(layout, replications=2, seed=5, run_length=150, warmup=0)
    recordings = experiments.record_scenarios(layout, result, run_length=150, warmup=0)
    page = player.build_player(layout, recordings, experiment=result, title="forklifts",
                               view="layout", samples=250)
    no_address(page)
    _block, data = split(page)
    runs = data["runs"]
    assert data["scenarios"] == list(runs) == ["Baseline", "Four forklifts",
                                               "Dock across the yard", "FIFO dispatch"]
    assert all((run["seed"], run["of"]) == (5, 2) for run in runs.values())

    def block(name, wanted):
        return next(b for b in runs[name]["scene"]["blocks"] if b["name"] == wanted)

    def fleet_units(name):
        index = str([b["name"] for b in runs[name]["scene"]["blocks"]].index("forklift_move"))
        return {sum(count for _label, count in frame["u"][index])
                for frame in runs[name]["frames"] if index in frame.get("u", {})}

    assert block("Baseline", "Dock")["x"] == 0.0 and block("Dock across the yard", "Dock")["x"] == -70.0
    assert fleet_units("Baseline") == {3} and fleet_units("Four forklifts") == {4}
    overrides = {scenario["name"]: scenario["overrides"] for scenario in layout["scenarios"]}
    for name, run in runs.items():
        own = experiments.apply_overrides(layout, overrides.get(name, []))
        assert_tape_equals_timeline(own, recordings[name].events, run, picks=15)

    # "All replications" is the comparison table of the study, beside "this run".
    table = result.comparison()
    table = table[table["KPI"].str.startswith(("system/", "user/"))]
    expected = {(row.KPI, row.Scenario): (row.Mean, row.LB, row.UB) for row in table.itertuples()}
    got = {(row[0], row[1]): row[2:5] for row in data["kpis"]}
    assert got.keys() == expected.keys() and len(got) > 40
    for key, values in expected.items():
        assert got[key] == [None if pd.isna(value) else pytest.approx(value) for value in values], key
    flat = flatten_kpis(result.results["Four forklifts"])
    first = flat[flat["Run"] == 5]
    this_run = {row[0]: row[5] for row in data["kpis"] if row[1] == "Four forklifts"}
    assert len(first) > 10
    for kpi, value in zip(first["KPI"], first["Value"]):
        if kpi.startswith(("system/", "user/")):
            assert this_run[kpi] == pytest.approx(value), kpi


# -- what the file says of the model's own words, and the size refusal on the command line ---
def test_a_label_is_the_description_only_for_a_block_with_its_automatic_name():
    """The privacy text says it: the file holds labels, and for a block still carrying its
    automatic `submodel_N` name the label is the description the canvas shows in its place.
    A described block with a name of its own does not give its description away."""
    layout = {"blocks": [
        {"name": "submodel_1", "description": "Confidential line for ACME", "type": "SUBMODEL",
         "position": [0, 0], "params": {}},
        {"name": "saw", "description": "Private note about the saw", "type": "PROCESS",
         "position": [200, 0], "params": {}}], "links": []}
    page = player.build_player(layout, {BASELINE: []}, view="schematic")
    _block, data = split(page)
    assert [b["label"] for b in data["runs"][BASELINE]["scene"]["blocks"]] == [
        "Confidential line for ACME", "saw"]
    assert "Confidential line for ACME" in page and "Private note" not in page


def test_naga_run_player_reports_a_too_large_file_as_a_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(player, "MAX_BYTES", 1000)
    out = tmp_path / "big.html"
    assert api.main([SERVER, "--replications", "1", "--run-length", "50", "--warmup", "0",
                     "--player", str(out)]) == 1
    printed = capsys.readouterr().out
    assert "--export: " in printed and "byte limit" in printed and not out.exists()


def test_naga_scenarios_player_reports_a_too_large_file_as_a_refusal(tmp_path, monkeypatch, capsys):
    from nagasim import experiments

    monkeypatch.setattr(player, "MAX_BYTES", 1000)
    out = tmp_path / "big.html"
    assert experiments.main([str(MODELS / "warehouse_forklifts.json"), "--replications", "1",
                             "--run-length", "50", "--warmup", "0", "--scenario",
                             "Four forklifts", "--player", str(out)]) == 1
    printed = capsys.readouterr().out
    assert "--player: " in printed and "byte limit" in printed and not out.exists()
