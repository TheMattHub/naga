"""The browser viewer (docs/superpowers/specs/2026-10-10-browser-viewer-design.md,
D320-D331): the runtime's pin and its one download, the what-if both sides run, what a
model may not carry, the page, and (with a runtime, node and a browser) the page run for
real."""
import bz2
import hashlib
import io
import json
import tarfile
import zipfile

import pytest

from nagasim import viewer_runtime as vr


# ---------------------------------------------------------------- the runtime (V1)
def _wheel(name):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(zipfile.ZipInfo(f"{name}-1.0.dist-info/LICENSE"), f"the licence of {name}")
    return raw.getvalue()


def _fake_runtime(monkeypatch):
    """A made-up archive and its wheels, pinned as the real ones are: ({url: bytes},
    {wheel file name: bytes})."""
    names = {"numpy": [], "pandas": ["numpy", "python-dateutil"], "python-dateutil": ["six"],
             "six": [], "scipy": ["numpy"], "tzdata": [], "unrelated": []}
    wheels = {f"{name}-1.0-none.whl": _wheel(name) for name in names}
    lock = {"info": {"python": "3.14.2"},
            "packages": {name: {"file_name": f"{name}-1.0-none.whl", "depends": depends,
                                "sha256": hashlib.sha256(wheels[f"{name}-1.0-none.whl"]).hexdigest()}
                         for name, depends in names.items()}}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in vr.CORE_FILES:
            data = json.dumps(lock).encode() if name == "pyodide-lock.json" else name.encode()
            info = tarfile.TarInfo(f"pyodide/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive = bz2.compress(raw.getvalue())
    monkeypatch.setattr(vr, "CORE_SHA256", hashlib.sha256(archive).hexdigest())
    urls = {vr.CORE_URL: archive, **{vr.WHEEL_URL + name: data for name, data in wheels.items()}}
    return urls, wheels


def _opener(urls, asked):
    def open_(url, timeout=None):
        asked.append(url)
        return io.BytesIO(urls[url])
    return open_


def test_fetch_keeps_only_the_pinned_files_and_load_gives_them(tmp_path, monkeypatch):
    urls, wheels = _fake_runtime(monkeypatch)
    asked = []
    assert vr.fetch(tmp_path / "rt", opener=_opener(urls, asked)) == tmp_path / "rt"
    files = vr.load(tmp_path / "rt")
    # The lock file's closure of PACKAGES, and nothing else of the lock file.
    assert sorted(files) == sorted([*vr.CORE_FILES, *(n for n in wheels if "unrelated" not in n)])
    assert files["pyodide.mjs"] == b"pyodide.mjs" and len(asked) == 7
    assert all(url.startswith(("https://github.com/pyodide/", "https://cdn.jsdelivr.net/pyodide/"))
               for url in asked)
    # A second fetch asks for nothing: every file is there and is the pinned one.
    vr.fetch(tmp_path / "rt", opener=_opener({}, asked))
    assert len(asked) == 7 and not list((tmp_path / "rt").glob("*.part"))


def test_another_archive_or_wheel_is_refused_and_not_kept(tmp_path, monkeypatch):
    urls, _wheels = _fake_runtime(monkeypatch)
    with pytest.raises(vr.ViewerRuntimeError, match="not the pinned file"):
        vr.fetch(tmp_path / "a", opener=_opener({**urls, vr.CORE_URL: b"another"}, []))
    assert not list((tmp_path / "a").iterdir())
    bad = vr.WHEEL_URL + "scipy-1.0-none.whl"
    with pytest.raises(vr.ViewerRuntimeError, match="not the file the lock file pins"):
        vr.fetch(tmp_path / "b", opener=_opener({**urls, bad: b"tampered"}, []))
    assert not (tmp_path / "b" / "scipy-1.0-none.whl").exists()
    with pytest.raises(vr.ViewerRuntimeError, match="sent more than"):
        vr.fetch(tmp_path / "c", opener=_opener({**urls, bad: b"x" * (vr._MAX_FILE + 1)}, []))


def test_load_says_how_to_get_a_runtime_and_checks_a_copied_one(tmp_path, monkeypatch):
    urls, _wheels = _fake_runtime(monkeypatch)
    with pytest.raises(vr.ViewerRuntimeError, match=r"naga viewer --fetch.*--runtime DIR"):
        vr.load(tmp_path / "none")
    vr.fetch(tmp_path / "rt", opener=_opener(urls, []))
    (tmp_path / "rt" / "numpy-1.0-none.whl").write_bytes(b"edited")
    with pytest.raises(vr.ViewerRuntimeError, match="numpy.*not the pinned file"):
        vr.load(tmp_path / "rt")
    (tmp_path / "rt" / "numpy-1.0-none.whl").unlink()
    with pytest.raises(vr.ViewerRuntimeError, match="numpy-1.0-none.whl is not there"):
        vr.load(tmp_path / "rt")


def test_a_lock_file_cannot_name_a_path(monkeypatch):
    lock = {"packages": {name: {"file_name": "../x.whl", "depends": [], "sha256": ""}
                         for name in vr.PACKAGES}}
    with pytest.raises(vr.ViewerRuntimeError, match="not a wheel"):
        vr.wheels(json.dumps(lock).encode())


def test_the_runtime_folder_is_the_callers(tmp_path, monkeypatch):
    monkeypatch.delenv(vr.ENV_VAR, raising=False)
    assert vr.resolve_dir() == vr.default_dir() and vr.VERSION in vr.default_dir().name
    monkeypatch.setenv(vr.ENV_VAR, str(tmp_path / "env"))
    assert vr.resolve_dir() == tmp_path / "env" and vr.resolve_dir(tmp_path / "arg") == tmp_path / "arg"


# ---------------------------------------------------------------- the what-if (V2)
import copy                                                         # noqa: E402
from pathlib import Path                                            # noqa: E402

import nagasim                                                      # noqa: E402
from nagasim import dashboard, experiments, viewer                  # noqa: E402
from nagasim.api import load_layout                                 # noqa: E402

MODELS = Path(nagasim.__file__).parent / "models"


def model(name="single_server.json"):
    return load_layout(str(MODELS / name))


def test_what_if_is_the_presenters_run_as_one_json_safe_record():
    layout = model()
    record = viewer.what_if(layout, {"resources.Server.capacity": 2}, replications=3)
    json.loads(json.dumps(record, allow_nan=False))
    assert record["inputs"] == {"resources.Server.capacity": 2} and record["seeds"] == [0, 1, 2]
    assert set(record["cells"]) == {"Baseline", "Yours"} and record["versions"]["nagasim"]
    # The same numbers as the run the Presenter makes.
    made = {**layout, "scenarios": [{"name": "Yours", "overrides": dashboard.input_overrides(
        layout, {"resources.Server.capacity": 2})}]}
    result = experiments.run_scenarios(made, ["Yours"], replications=3, seed=0)
    frame = result.comparison(kpis=["system/Avg WIP (Little)"]).to_dict("records")
    rows = [r for r in record["rows"] if r["kpi"] == "system/Avg WIP (Little)"]
    assert [(r["scenario"], r["mean"], r["lb"], r["diff_ub"]) for r in rows] == \
        [(f["Scenario"], f["Mean"], f["LB"], f["Diff UB"]) for f in frame]
    assert rows[0]["label"] == "Avg WIP (Little)" and rows[1]["mean"] < rows[0]["mean"]
    flat = experiments.flatten_kpis(result.results["Yours"])
    assert record["cells"]["Yours"]["system/Avg WIP (Little)"] == list(
        flat[flat["KPI"] == "system/Avg WIP (Little)"].sort_values("Run")["Value"])


def test_what_if_refuses_what_the_author_did_not_allow():
    layout = model()
    with pytest.raises(ValueError, match="not a number within"):
        viewer.what_if(layout, {"resources.Server.capacity": 99})
    with pytest.raises(ValueError, match="is not an input of this model"):
        viewer.what_if(layout, {"simulation.time_limit": 1e9})
    for count in (0, 31, 2.5, True):
        with pytest.raises(ValueError, match="replications must be a whole number from 1 to 30"):
            viewer.what_if(layout, replications=count)


def test_a_model_that_cannot_be_a_viewer_is_refused_by_name():
    layout = model()
    assert viewer.viewer_problems(layout) == []
    bound = copy.deepcopy(layout)
    bound["data_sources"] = [{"name": "plant_db", "kind": "csv", "location": "C:/secret/x.csv"}]
    bound["data_bindings"] = [{"name": "b1", "source": "plant_db"}]
    logic = copy.deepcopy(layout)
    logic["blocks"][0] = {**logic["blocks"][0], "type": "LOGIC"}
    python = copy.deepcopy(layout)
    python["agent_types"] = [{"name": "Walker", "python": "def step(agent, api): pass"}]
    for bad, said in ((bound, r"data_sources \(plant_db\).*naga viewer --freeze"),
                      (logic, "is a LOGIC block"), (python, "agent type 'Walker' runs Python")):
        problems = viewer.viewer_problems(bad)
        assert problems and all(p.code == "E_VIEWER_MODEL" for p in problems)
        with pytest.raises(ValueError, match=said):
            viewer.what_if(bad)
    # The address of a refused source is not repeated in the message.
    assert "secret" not in " ".join(p.message for p in viewer.viewer_problems(bound))
    big = {**layout, "info": {"notes": "x" * viewer.MAX_MODEL_BYTES}}
    assert "over the 5000000" in viewer.viewer_problems(big)[0].message


def test_compare_holds_cells_exact_and_bounds_to_a_tolerance():
    layout = model()
    author = viewer.what_if(layout, replications=3)
    page = viewer.what_if(layout, {"resources.Server.capacity": 2}, replications=3)
    same = viewer.compare(page, author)
    assert same["different"] == 0 and same["bounds_different"] == 0
    assert same["cells"] == 3 * len(author["cells"]["Baseline"]) and same["bounds"] == 12
    # Another scipy: a bound moved in its 11th digit is the same; a cell is not.
    nudged = copy.deepcopy(page)
    for row in nudged["rows"]:
        row["lb"] = row["lb"] * (1 + 2e-11) if row["lb"] else row["lb"]
    assert viewer.compare(nudged, author)["bounds_different"] == 0
    nudged["rows"][0]["ub"] *= 1 + 1e-6
    nudged["cells"]["Baseline"]["system/Avg WIP (Little)"][1] += 1e-12
    assert viewer.compare(nudged, author) == {**same, "different": 1, "bounds_different": 1}
    # More replications in the page: the first three are the author's three.
    longer = viewer.compare(viewer.what_if(layout, replications=4), author)
    assert longer["replications"] == 3 and longer["different"] == 0 and longer["bounds"] == 0


# ---------------------------------------------------------------- the page (V3)
import base64                                                       # noqa: E402
import gzip                                                         # noqa: E402
import hashlib as _hashlib                                          # noqa: E402
import re                                                           # noqa: E402
import shutil                                                       # noqa: E402
import subprocess                                                   # noqa: E402

HOSTILE = "<img src=x onerror=alert(1)>"


def _fake_folder(tmp_path, monkeypatch):
    urls, _wheels = _fake_runtime(monkeypatch)
    return vr.fetch(tmp_path / "rt", opener=_opener(urls, []))


def _hostile_model():
    layout = model()
    layout["dashboard"] = {"inputs": [{"path": "resources.Server.capacity", "label": HOSTILE,
                                       "kind": "number", "min": 1, "max": 3, "step": 1}]}
    layout["info"] = {"notes": "__FILES__ __MAIN__ </script><script>alert(2)</script>"}
    layout["view"] = {"tab": "C:/Users/someone/private"}
    return layout


def _blocks(page):
    return dict(re.findall(r'<script type="application/json" id="(\w+)">(.*?)</script>', page, re.S))


def _digest(text):
    return base64.b64encode(_hashlib.sha256(text.encode()).digest()).decode()


def test_the_page_is_a_constant_shell_and_data_that_cannot_break_out(tmp_path, monkeypatch):
    folder = _fake_folder(tmp_path, monkeypatch)
    layout = _hostile_model()
    made = viewer.build_viewer(layout, tmp_path / "a.html", runtime=folder, title=HOSTILE)
    page = (tmp_path / "a.html").read_bytes().decode("ascii")
    assert made["bytes"] == len(page) and made["warnings"] == []
    # The policy pins the one script and the one style sheet; nothing else may load.
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', page).group(1)
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    from nagasim import player
    assert policy == ("default-src 'none'; script-src 'sha256-%s' 'sha256-%s' blob: 'wasm-unsafe-eval'; "
                      "style-src 'sha256-%s' 'sha256-%s'; img-src data:; worker-src data:; "
                      "base-uri 'none'; form-action 'none'"
                      % (_digest(script), _digest(player._SCRIPT), _digest(style), _digest(player._CSS)))
    # The author's recorded run is the 7.7 player's page, whole, for a frame of its own origin.
    packed = re.search(r'id="player-data">([^<]+)<', page).group(1)
    recorded = gzip.decompress(base64.b64decode(packed)).decode("ascii")
    assert recorded.startswith("<!doctype html>") and player._SCRIPT in recorded
    assert 'sandbox="allow-scripts"' in page and "allow-same-origin" not in page
    assert script == viewer._MAIN and "unsafe-inline" not in policy and "'unsafe-eval'" not in policy
    # Text from the model is data: no tag of its own, and no mark of the page is filled from it.
    assert "<img" not in page and "alert(2)" in page and "<script>alert" not in page
    assert page.count("<script>") == 1 and page.count('data-name="engine.zip"') == 1
    blocks = _blocks(page)
    shipped = json.loads(blocks["model"])
    assert shipped["info"] == layout["info"] and "view" not in shipped and "private" not in page
    data = json.loads(blocks["data"])
    assert data["title"] == HOSTILE and data["inputs"][0]["label"] == HOSTILE
    assert data["values"] == {"resources.Server.capacity": 1} and data["frozen"] is None
    assert json.loads(blocks["baseline"])["replications"] == data["limits"]["replications"]
    # The embedded files by name; no list of hashes (a file cannot vouch for itself: M1).
    names = re.findall(r'<script type="application/octet-stream" data-name="([^"]+)"', page)
    assert names == sorted(names) and {"pyodide.asm.wasm", "engine.zip"} <= set(names)
    assert "manifest" not in data
    # No address outside the embedded files and the (compressed) notices.
    rest = re.sub(r'<script type="(application/octet-stream|text/plain)" '
                  r'(data-name|id="notices-data|id="player-data).*?</script>', "", page, flags=re.S)
    assert "://" not in rest and "//embedded" not in rest
    # The same model, runtime and baseline give the same bytes.
    viewer.build_viewer(layout, tmp_path / "b.html", runtime=folder, title=HOSTILE,
                        baseline=made["baseline"])
    assert (tmp_path / "b.html").read_bytes() == (tmp_path / "a.html").read_bytes()
    # Without a recording the page is written all the same, and says so.
    bare = viewer.build_viewer(layout, tmp_path / "c.html", runtime=folder, recording="",
                               baseline=made["baseline"])
    assert [w.code for w in bare["warnings"]] == ["W_VIEWER_EMPTY"]
    assert 'id="player-data"></script>' in (tmp_path / "c.html").read_text("ascii")


def test_the_page_carries_the_engine_as_installed_without_the_authors_code(tmp_path):
    data, licences = viewer._engine_zip()
    assert viewer._engine_zip()[0] == data
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert archive.read("nagasim/viewer.py") == Path(viewer.__file__).read_bytes()
    assert {"nagasim/__init__.py", "nagasim/core/blocks/logic.py", "simpy/__init__.py",
            "openpyxl/__init__.py", "et_xmlfile/__init__.py"} <= names
    assert "nagasim/core/custom_logic.py" not in names
    assert not [n for n in names if n.startswith("nagasim/models/") or n.endswith(".pyc")]
    assert {title.split(" ")[0] for title, _text in licences} == {"simpy", "openpyxl", "et_xmlfile"}
    notices = viewer._notices(licences)
    for owed in ("Mozilla Public License", "github.com/pyodide/pyodide/tree/314.0.7",
                 "PYTHON SOFTWARE FOUNDATION LICENSE", "OpenBLAS", "AT&T", "Apache License",
                 "Naga engine", "SciPy 1.18.0 bundles: Qhull", "pandas 3.0.2 bundles: ULTRAJSON_LICENSE",
                 # What the build files say is linked in, each from a pinned tag or commit.
                 "libffi/f08493d249d2067c8b3207ba46693dd858f95db3", "hiwire/6a1e67280a15d929ebeceee54a6358c9c8d5f697",
                 "cpython-source-deps/zstd-1.5.7", "musl as a whole is licensed under", "LLVM Exceptions",
                 "libunwind (Emscripten 5.0.3", "compiler-rt (Emscripten 5.0.3", "Doug Lea and released to the public domain",
                 "zlib 1.3.1", "bzip2 1.0.6", "SQLite 3.39.0", "Wenzel Jakob", "David M. Cooke",
                 "A. Volgenant/Amsterdam School of Economics", "Not established from build files"):
        assert owed in notices, owed


def test_a_model_is_refused_before_anything_is_read_or_run(tmp_path, monkeypatch):
    layout = model()
    layout["data_sources"] = [{"name": "plant_db", "kind": "csv", "location": "x.csv"}]
    with pytest.raises(ValueError, match="cannot be a viewer: data_sources"):
        viewer.build_viewer(layout, tmp_path / "a.html", runtime=tmp_path / "none")
    with pytest.raises(vr.ViewerRuntimeError, match="naga viewer --fetch"):
        viewer.build_viewer(model(), tmp_path / "a.html", runtime=tmp_path / "none")
    assert not (tmp_path / "a.html").exists()


def test_a_long_or_empty_viewer_is_said(tmp_path, monkeypatch):
    folder = _fake_folder(tmp_path, monkeypatch)
    slow = viewer.what_if(model(), replications=3)
    slow["seconds"] = 3 * 5.0                                   # 5 s a replication here
    made = viewer.build_viewer(model(), tmp_path / "a.html", runtime=folder, baseline=slow)
    assert made["estimate"] == 12.5 and [w.code for w in made["warnings"]] == ["W_VIEWER_LONG_RUN"]
    assert "30 replications is estimated at 6 minutes" in made["warnings"][0].message
    # The estimate is MEASURED, so both baselines carry a fixed time (bug 412: on a busy
    # machine the real one made the empty model "long" too).
    quick = viewer.what_if(model("agents_machine_repair.json"), replications=3)
    quick["seconds"] = 3 * 0.01
    empty = viewer.build_viewer(model("agents_machine_repair.json"), tmp_path / "b.html",
                                runtime=folder, baseline=quick)
    assert [w.code for w in empty["warnings"]] == ["W_VIEWER_EMPTY", "W_VIEWER_EMPTY"]


NODE = shutil.which("node")
_NODE_PROBE = """
console.log(JSON.stringify({
  fmt: [fmt(null), fmt(0), fmt(4.65812), fmt(0.000123456), fmt(12345678.9), fmt(-39.4567), fmt(1)],
  span: [span(1, 2.5), span(null, 1)],
  moved: moved({a: 1, b: true, c: "x"}, {a: 1, b: false, c: "x", constructor: 2}),
  seconds: [seconds(0.2), seconds(89.4), seconds(200)],
  grouped: grouped([{kpi: "k", label: "K", scenario: "Baseline", mean: 1},
                    {kpi: "__proto__", label: "P", scenario: "Baseline", mean: 3},
                    {kpi: "k", label: "K", scenario: "Yours", mean: 2}]).map(
                      function (line) { return [line[0], line[1] && line[1].mean, line[2] && line[2].mean]; }),
  same: [sameText({different: 0, bounds_different: 0, cells: 265, replications: 5, bounds: 12}, {}, {}),
         sameText({different: 2, bounds_different: 1, cells: 265, replications: 5, bounds: 12},
                  {python: "3.14.2", numpy: "2.4.6", scipy: "1.18.0"},
                  {python: "3.12.1", numpy: "2.1.0", scipy: "1.16.3"})]}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_scripts_pure_half_under_node(tmp_path):
    (tmp_path / "main.js").write_text(viewer._MAIN + _NODE_PROBE, encoding="utf-8")
    done = subprocess.run([NODE, str(tmp_path / "main.js")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert out["fmt"] == ["-", "0", "4.658", "1.235e-4", "1.235e+7", "-39.46", "1"]
    assert out["span"] == ["1 to 2.5", "-"] and out["seconds"] == ["1 s", "89 s", "3 min"]
    assert out["moved"] == {"b": False, "constructor": 2}
    assert out["grouped"] == [["K", 1, 2], ["P", 3, None]]
    assert out["same"][0].startswith("Same engine: this browser's baseline equals the author's run in all 265")
    assert "2 of 265 baseline values and 1 of 12 interval bounds differently" in out["same"][1] \
        and "scipy 1.18.0; the author: Python 3.12.1, numpy 2.1.0, scipy 1.16.3" in out["same"][1]


# ---------------------------------------------------------------- naga viewer (V4)
def test_naga_viewer_writes_the_file_says_what_it_holds_and_never_downloads(
        tmp_path, monkeypatch, capsys):
    folder = _fake_folder(tmp_path, monkeypatch)

    def no_network(*_args, **_kwargs):
        raise AssertionError("naga viewer asked the network without --fetch")
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    out = tmp_path / "v.html"
    assert viewer.main([str(MODELS / "single_server.json"), "-o", str(out),
                        "--runtime", str(folder)]) == 0
    said = capsys.readouterr().out
    assert "whole model and the engine's source" in said and "--player" in said and " MB; " in said
    assert json.loads(_blocks(out.read_text("ascii"))["data"])["title"] == "single_server"
    monkeypatch.setenv(vr.ENV_VAR, str(tmp_path / "none"))
    with pytest.raises(ValueError, match="naga viewer --fetch"):
        viewer.main([str(MODELS / "single_server.json"), "-o", str(out)])
    with pytest.raises(SystemExit):
        viewer.main([str(MODELS / "single_server.json")])


def test_naga_knows_the_viewer_command():
    from nagasim.cli import COMMANDS
    assert COMMANDS["viewer"][:2] == ("viewer", "main")


# ---------------------------------------------------------------- a real browser (V5)
# The page run for real: the runtime as fetched, node 22 (its own WebSocket drives the
# DevTools protocol, tests/viewer_drive.cjs) and Chrome or Edge. Firefox has no DevTools
# protocol and is checked by hand (site_docs/sharing-a-model.md).
import math                                                         # noqa: E402
import os                                                           # noqa: E402

from nagasim.settings import userpaths                              # noqa: E402


def _real_runtime():
    """The fetched runtime: where the variable says, else the user's own copy (the suite
    redirects the user folder, so the real one is worked out here)."""
    own = userpaths._base_dir(userpaths._NEW_NAME) / "viewer-runtime" / f"pyodide-{vr.VERSION}"
    for folder in (os.environ.get(vr.ENV_VAR), own):
        if folder and (Path(folder) / vr.CORE_NAME).is_file():
            return Path(folder)
    return None


def _node22():
    if NODE is None:
        return False
    try:
        version = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=30).stdout
        return int(version.strip().lstrip("v").split(".")[0]) >= 22
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


RUNTIME = _real_runtime()
BROWSERS = [path for path in (
    shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    or r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if path and Path(path).is_file()]
_MISSING = ", ".join(what for what, there in (
    ("the browser runtime (run `naga viewer --fetch`)", RUNTIME is not None),
    ("node 22 or later", _node22()), ("Chrome or Edge", bool(BROWSERS))) if not there)
in_a_browser = pytest.mark.skipif(bool(_MISSING), reason=f"not installed: {_MISSING}")
READY, RAN = {"wait": "^Ready|could not start"}, {"wait": "^Run finished|^Not run|error"}


def _set(value, index=0):
    return {"eval": '(function () { var box = document.querySelectorAll("#inputs input[type=number]")[%d];'
                    ' box.value = "%s"; box.dispatchEvent(new Event("input")); return box.value; })()'
                    % (index, value)}


def _click(*ids):
    return {"eval": "".join('document.getElementById("%s").click();' % name for name in ids) + " 1"}


def _text(name):
    return {"eval": 'document.getElementById("%s").textContent' % name}


# The author's baseline is drawn by the page's script, which follows 40 MB of data: on a
# slow runner the markup is there before the table is. Wait for the table, then read it.
BASELINE_SHOWN = {"eval": 'new Promise(function (done) { var tries = 0; (function look() {'
                          ' var text = document.getElementById("results-body").textContent;'
                          ' if (text || (tries += 1) > 300) { done(text); } else { setTimeout(look, 100); }'
                          ' })(); })'}


def _drive(browser, page, tmp_path, steps):
    done = subprocess.run(
        [NODE, str(Path(__file__).with_name("viewer_drive.cjs")), browser, str(page),
         str(tmp_path / "profile"), json.dumps(steps)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420)
    assert done.returncode == 0 and done.stdout, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert not out.get("failed"), (out["failed"], out["console"][-5:])
    return out


def _same_run(answer, desktop):
    """The page's record is the desktop's: every cell equal, every row to 1e-9 (D328)."""
    record = answer["record"]
    assert record["cells"] == desktop["cells"] and record["seeds"] == desktop["seeds"]
    assert record["inputs"] == desktop["inputs"] and len(record["rows"]) == len(desktop["rows"])
    for mine, theirs in zip(record["rows"], desktop["rows"]):
        assert (mine["kpi"], mine["scenario"], mine["significant"]) == \
            (theirs["kpi"], theirs["scenario"], theirs["significant"])
        for key in ("mean", "lb", "ub", "diff", "diff_lb", "diff_ub"):
            assert (mine[key] is None and theirs[key] is None) or math.isclose(
                mine[key], theirs[key], rel_tol=1e-9, abs_tol=1e-12), (mine["kpi"], key)
    assert answer["same"]["different"] == 0 and answer["same"]["bounds_different"] == 0
    assert answer["same"]["cells"] > 0


@in_a_browser
@pytest.mark.parametrize("browser", BROWSERS, ids=lambda path: Path(path).stem)
def test_exit_check_a_changed_input_run_in_the_page_gives_the_desktops_numbers(tmp_path, browser):
    """The first cut's exit check: a bundled model exported to one file, opened from disk,
    an input changed, re-run, and the KPIs are desktop Python's for that input and seed."""
    page = tmp_path / "single_server.html"
    made = viewer.build_viewer(MODELS / "single_server.json", page, runtime=RUNTIME)
    assert 35e6 < made["bytes"] < 45e6
    out = _drive(browser, page, tmp_path, [
        BASELINE_SHOWN, READY, _set(2), _click("run"), RAN, _text("record"),
        _text("results-body"), _text("same"), _text("made")])
    shown_first, ready, _value, _clicked, ran, record, shown, same, made_from = out["answers"]
    assert ready.startswith("Ready after") and ran.startswith("Run finished in")
    # The page asked for nothing: no address but its own file, data: and blob:.
    assert out["asked"] == [] and out["attached"] >= 1      # the worker was watched too
    assert not [line for line in out["console"] if "Content Security" in line]
    desktop = viewer.what_if(model(), {"resources.Server.capacity": 2})
    _same_run(json.loads(record), desktop)
    assert same.startswith("Same engine: this browser's baseline equals the author's run in all ")
    assert "Yours: Server = 2. 5 replications, seeds 0 to 4" in made_from
    # Shown at once, before the engine started: the author's baseline. Then both.
    assert shown_first.startswith("Avg Throughput Time") and shown.count(" to ") == 12


@pytest.mark.slow          # three starts of the engine: 45 s of a busy machine in the fast set
@in_a_browser
def test_in_the_page_a_models_text_is_text_a_range_is_enforced_and_cancel_starts_again(tmp_path):
    page = tmp_path / "hostile.html"
    viewer.build_viewer(_hostile_model(), page, runtime=RUNTIME, title=HOSTILE)
    out = _drive(BROWSERS[0], page, tmp_path, [
        {"eval": 'document.getElementById("inputs").innerHTML + document.getElementById("title").innerHTML'},
        {"eval": 'document.getElementById("notices").open = document.getElementById("watch").open = true'},
        # Cancel while the engine starts (M5): it stops, and Run starts it again and runs.
        {"wait": "^Starting the engine"},
        {"eval": 'String(document.getElementById("cancel").disabled) + document.getElementById("run").disabled'},
        _click("cancel"), _text("status"),
        {"eval": 'String(document.getElementById("cancel").disabled) + document.getElementById("run").disabled'},
        _click("run"), {"wait": "^Run finished|could not start|error"},
        _set(99), _click("run"), RAN,
        _set(2), _click("run", "cancel"), _text("status"), READY, _click("run"), RAN, _text("record"),
        {"eval": 'document.getElementById("notices-text").textContent.slice(0, 46) + "|" + '
                 'document.getElementById("player").srcdoc.slice(0, 15) + "|" + '
                 'document.getElementById("watch-note").textContent'}])
    (markup, _open, _start, starting, _cancel, stopped, then, _run0, first, _99, _run, refused, _2,
     _cancelled, status, again, _run2, ran, record, opened) = out["answers"]
    assert starting == "falsetrue" and then == "truefalse"
    assert stopped == "The engine was stopped before it was ready. Press Run to start it and run."
    assert first.startswith("Run finished in")
    # Read when opened: the notices, and the author's recorded run in its frame.
    assert opened.startswith("Third-party notices of the Naga browser viewer|<!doctype html>|"
                             "One replication of the model as the author left it (seed 0)")
    assert markup.count("&lt;img src=x onerror=alert(1)&gt;") == 3 and "<img" not in markup
    # The range a slider shows is enforced by the engine in the worker, not by the control.
    assert refused.startswith("Not run: input 'resources.Server.capacity': 99 is not a number within")
    assert status.startswith(("Cancelled. Starting the engine again", "Starting the engine"))
    assert again.startswith("Ready after") and ran.startswith("Run finished in")
    assert json.loads(record)["record"]["inputs"] == {"resources.Server.capacity": 2}
    assert out["asked"] == []


@pytest.mark.slow
@in_a_browser
@pytest.mark.parametrize("name, index, value, path", [
    ("sd_supply_game.json", 1, 4, "auxiliaries.lead_time.expr"),
    ("automation_floor.json", 2, 30, "blocks.tended_machine_1.params.arguments.process_time"),
    ("agents_machine_repair.json", None, None, None)])
def test_process_system_dynamics_and_agent_models_match_in_every_browser(
        tmp_path, name, index, value, path):
    page = tmp_path / "model.html"
    viewer.build_viewer(MODELS / name, page, runtime=RUNTIME)
    desktop = viewer.what_if(model(name), {path: value} if path else {})
    for number, browser in enumerate(BROWSERS):
        steps = [READY, *([_set(value, index)] if path else []), _click("run"), RAN, _text("record")]
        out = _drive(browser, page, tmp_path / str(number), steps)
        assert out["answers"][-2].startswith("Run finished"), out["answers"][-2]
        _same_run(json.loads(out["answers"][-1]), desktop)
        assert out["asked"] == []


# ---------------------------------------------------------------- frozen data (V10)
def _clinic(tmp_path, notes="A walk-in clinic fed from the visit log."):
    """The bundled clinic twin in a folder of its own, with its data file beside it."""
    folder = tmp_path / "client"
    (folder / "data").mkdir(parents=True)
    shutil.copy(MODELS / "data" / "clinic_visits.csv", folder / "data" / "clinic_visits.csv")
    layout = json.loads((MODELS / "clinic_twin.json").read_text(encoding="utf-8"))
    layout["info"] = {"description": notes}
    (folder / "clinic.json").write_text(json.dumps(layout), encoding="utf-8")
    return folder / "clinic.json"


def _cells(results):
    flat = experiments.flatten_kpis(results).sort_values(["KPI", "Run"], kind="stable")
    cells = {}
    for kpi, value in zip(flat["KPI"], flat["Value"]):
        cells.setdefault(str(kpi), []).append(viewer._number(value))
    return cells


def test_frozen_data_is_plain_model_data_and_runs_as_the_bound_model_does(tmp_path, monkeypatch):
    from nagasim.api import run_model
    path = _clinic(tmp_path)
    with pytest.raises(ValueError, match=r"data_sources \(visits\).*naga viewer --freeze"):
        viewer.what_if(load_layout(str(path)))
    frozen, read = viewer.freeze_data(path, path.parent)
    assert read == ["visits"] and "data_sources" not in frozen and "data_bindings" not in frozen
    assert viewer.viewer_problems(frozen) == []
    # The values read are in the copy: the fitted distribution and the measured schedule.
    original = load_layout(str(path))
    assert frozen["schedules"] != original["schedules"] or frozen["blocks"] != original["blocks"]
    # The same numbers as the desktop run that binds the same file.
    record = viewer.what_if(frozen, replications=3)
    bound = run_model(str(path), bind_data=True, replications=3, seed=0)
    assert record["cells"]["Baseline"] == _cells(bound)
    # The page names no source: not its file, not its folder, not the author's path.
    folder = _fake_folder(tmp_path, monkeypatch)
    viewer.build_viewer(frozen, tmp_path / "v.html", runtime=folder, frozen="2026-10-10",
                        baseline=record, recording="")
    page = (tmp_path / "v.html").read_text("ascii")
    shipped = _blocks(page)["model"] + _blocks(page)["data"] + _blocks(page)["baseline"]
    for trace in ("clinic_visits", "data\\/", ".csv", path.parent.name, "service_minutes"):
        assert trace not in shipped, trace
    model_shipped = json.loads(_blocks(page)["model"])
    assert not model_shipped.get("data_sources") and not model_shipped.get("data_bindings")
    assert json.loads(_blocks(page)["data"])["frozen"] == "2026-10-10"
    assert '"Data as read on " + D.frozen' in viewer._MAIN


def test_freezing_refuses_by_name_what_it_cannot_read_or_must_not_keep(tmp_path, monkeypatch):
    path = _clinic(tmp_path, notes="Fed from data/clinic_visits.csv every morning.")
    with pytest.raises(ValueError, match="data source visits: its location is also written in "
                                         "the model's info"):
        viewer.freeze_data(path, path.parent)
    path = _clinic(tmp_path / "b")
    (path.parent / "data" / "clinic_visits.csv").unlink()
    with pytest.raises(ValueError, match="could not be read, so nothing was frozen: binding "
                                         "'consultation_time'"):
        viewer.freeze_data(path, path.parent)
    # A plant's feed is not frozen, and is not asked: no connection is given or opened.
    layout = json.loads(path.read_text(encoding="utf-8"))
    layout["data_sources"].append({"name": "line_plc", "kind": "mqtt", "location": "plant/line/#",
                                   "connection": "shop"})
    with pytest.raises(ValueError, match="data source line_plc: a live source is a plant's feed"):
        viewer.freeze_data(layout, path.parent)
    # A remote source needs the caller's permission, as everywhere (netguard): none here.
    layout = json.loads(path.read_text(encoding="utf-8"))
    layout["data_sources"][0] = {"name": "visits", "kind": "json_url",
                                 "location": "https://example.invalid/visits.json"}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("asked the network"))
    with pytest.raises(ValueError, match="could not be read, so nothing was frozen"):
        viewer.freeze_data(layout, path.parent)
    # A ceiling on what is frozen in.
    path = _clinic(tmp_path / "c")
    monkeypatch.setattr(viewer, "MAX_MODEL_BYTES", 2000)
    with pytest.raises(ValueError, match="with its data frozen in is .* over the 2000"):
        viewer.freeze_data(path, path.parent)


def test_naga_viewer_freeze_reads_once_and_says_so(tmp_path, monkeypatch, capsys):
    folder = _fake_folder(tmp_path, monkeypatch)
    path, out = _clinic(tmp_path), tmp_path / "v.html"
    with pytest.raises(ValueError, match="naga viewer --freeze"):
        viewer.main([str(path), "-o", str(out), "--runtime", str(folder)])
    assert viewer.main([str(path), "-o", str(out), "--runtime", str(folder), "--freeze"]) == 0
    said = capsys.readouterr().out
    assert "data read once from 1 source(s) (visits) and written into the copy" in said
    data = json.loads(_blocks(out.read_text("ascii"))["data"])
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", data["frozen"]) and "clinic_visits" not in \
        _blocks(out.read_text("ascii"))["model"]


@pytest.mark.slow
@in_a_browser
def test_a_model_with_its_data_frozen_in_runs_in_the_page_as_on_the_desktop(tmp_path):
    path = _clinic(tmp_path)
    frozen, _read = viewer.freeze_data(path, path.parent)
    page = tmp_path / "clinic.html"
    viewer.build_viewer(frozen, page, runtime=RUNTIME, frozen="2026-10-10")
    out = _drive(BROWSERS[0], page, tmp_path, [_text("inputs"), READY, _click("run"), RAN,
                                               _text("record")])
    assert "Data as read on 2026-10-10." in out["answers"][0]
    assert out["answers"][-2].startswith("Run finished"), out["answers"][-2]
    _same_run(json.loads(out["answers"][-1]), viewer.what_if(frozen))
    assert out["asked"] == []


# ---------------------------------------------------------------- what the policy does not govern (fix round, I1)
def test_the_pages_own_scripts_hold_nothing_that_leaves_without_a_request():
    """In Chrome a Content-Security-Policy does not govern WebRTC or a navigation, and the
    DevTools protocol reports neither as a request. So the three scripts a viewer runs
    are pinned here: none opens a peer connection, navigates, makes a frame or a meta, or
    sets a `src` other than the player's one picture (a `data:` image the builder made,
    and `img-src data:` refuses any other). An edited script is another hash: refused."""
    from nagasim import player
    scripts = {"main": viewer._MAIN, "worker": viewer._WORKER, "player": player._SCRIPT}
    never = (r"RTCPeerConnection", r"RTCDataChannel", r"window\.open", r"\blocation\s*=[^=]",
             r"\blocation\.(href|hash|search|pathname)\s*=[^=]", r"\blocation\.(assign|replace|reload)\s*\(",
             r"""["']iframe["']""", r"""["']meta["']""", r"""["']form["']""", r"""["']a["']""",
             r"sendBeacon", r"XMLHttpRequest", r"\bWebSocket\b", r"EventSource", r"importScripts",
             r"WebTransport", r"\beval\s*\(", r"new Function", r"innerHTML", r"document\.write",
             r"insertAdjacentHTML", r"http:", r"https:\w", r"\bws:", r"\bwss:")
    for name, script in scripts.items():
        for pattern in never:
            assert not re.search(pattern, script), (name, pattern)
    sources = {name: re.findall(r"[\w.\]\[]+\.src\s*=[^=][^;]*", script) for name, script in scripts.items()}
    assert sources == {"main": [], "worker": [], "player": ["image.src = source"]}
    # The only requests in the scripts: the worker's own answer to Pyodide and its two
    # imports of embedded bytes; the main thread makes none.
    assert "fetch" not in viewer._MAIN and "fetch" not in player._SCRIPT
    assert re.findall(r"\bimport\s*\(([^)]*)\)", viewer._WORKER) == ['url("pyodide.mjs"', 'url("pyodide.asm.mjs"']
    assert re.findall(r"fetch\b[^;\n]*", viewer._WORKER) == ["fetch = async function (what) {"]


_LEAKY = """<!doctype html><html><body><p id="status">start</p><script>
var code = "fetch('http://127.0.0.1:9/from-worker').catch(function () {});" +
  "try { new WebSocket('ws://127.0.0.1:9/ws-from-worker'); } catch (e) {}" +
  "setTimeout(function () { postMessage(1); }, 600);";
var worker = new Worker(URL.createObjectURL(new Blob([code], {type: "text/javascript"})));
worker.onmessage = function () { document.getElementById("status").textContent = "Ready"; };
</script></body></html>"""


@pytest.mark.skipif(not (_node22() and BROWSERS), reason="node 22 and Chrome or Edge are needed")
def test_the_driver_sees_what_a_worker_asks_for(tmp_path):
    """The driver's own check: a page whose WORKER asks for an address and opens a
    WebSocket. Before the fix round the driver watched the page alone and saw neither."""
    page = tmp_path / "leaky.html"
    page.write_text(_LEAKY, encoding="utf-8")
    out = _drive(BROWSERS[0], page, tmp_path, [READY])
    assert out["attached"] >= 1
    assert "http://127.0.0.1:9/from-worker" in out["asked"]
    assert "ws://127.0.0.1:9/ws-from-worker" in out["asked"]


# ---------------------------------------------------------------- steps and cancel (fix round, I2, I3)
def test_a_build_says_its_steps_and_a_cancel_leaves_no_file(tmp_path, monkeypatch):
    folder = _fake_folder(tmp_path, monkeypatch)
    steps = []
    viewer.build_viewer(model(), tmp_path / "a.html", runtime=folder,
                        on_progress=lambda done, total: steps.append((done, total)))
    assert [s for s in steps if s[1] == 5] == [(1, 5), (2, 5), (3, 5), (4, 5), (4, 5), (5, 5)]
    assert not list(tmp_path.glob("*.part"))
    # Asked between steps and between the baseline's replications.
    for allowed in (0, 1, 2, 4):
        asked = []

        def should_stop():
            asked.append(1)
            return len(asked) > allowed
        with pytest.raises(viewer.Cancelled):
            viewer.build_viewer(model(), tmp_path / "b.html", runtime=folder, should_stop=should_stop)
        assert not (tmp_path / "b.html").exists() and not list(tmp_path.glob("*.part"))
    with pytest.raises(viewer.Cancelled, match="the run was cancelled"):
        viewer.what_if(model(), replications=3, should_stop=lambda: True)


def test_a_cancelled_download_keeps_only_whole_pinned_files(tmp_path, monkeypatch):
    urls, _wheels = _fake_runtime(monkeypatch)
    asked, stops = [], iter([False, False, True])
    with pytest.raises(vr.Cancelled):
        vr.fetch(tmp_path / "rt", opener=_opener(urls, asked), should_stop=lambda: next(stops))
    assert len(asked) == 3 and not list((tmp_path / "rt").glob("*.part"))
    vr.fetch(tmp_path / "rt", opener=_opener(urls, asked))          # goes on from there
    assert len(asked) == 7 and vr.load(tmp_path / "rt")


def test_the_engines_files_are_checked_before_anything_else(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(vr, "load", lambda *a, **k: pytest.fail("read the runtime"))
    monkeypatch.setattr(viewer, "what_if", lambda *a, **k: pytest.fail("ran the baseline"))
    with pytest.raises(ValueError, match="engine's files are not on disk here.*naga viewer"):
        viewer.build_viewer({"not": "even a model"}, tmp_path / "v.html")


# ---------------------------------------------------------------- fix round: M4, M8
def test_replications_stop_at_the_engines_ceiling_whatever_the_file_says(monkeypatch):
    """M4: in a page the definition is text of the file. An edited one cannot ask for more
    replications than the engine's own ceiling for a dashboard."""
    from nagasim.core.schema.sections_validation import DASHBOARD_LIMITS
    layout = model()
    monkeypatch.setattr(dashboard, "resolve_dashboard", lambda _layout: {
        "inputs": [], "results": [], "limits": {"replications": 3, "max_replications": 10 ** 9}})
    monkeypatch.setattr(experiments, "run_scenarios", lambda *a, **k: pytest.fail("ran"))
    with pytest.raises(ValueError, match=f"from 1 to {DASHBOARD_LIMITS['replications']}, not"):
        viewer.what_if(layout, replications=DASHBOARD_LIMITS["replications"] + 1)


def test_the_engine_zip_carries_no_record_of_how_the_author_installed(tmp_path, monkeypatch):
    """M8: a distribution's direct_url.json holds the path or address it was installed
    from, RECORD and INSTALLER the rest of the story. Only METADATA and licences go."""
    import importlib.metadata
    from pathlib import PurePosixPath

    site = tmp_path / "site"
    files = {"fakepkg/__init__.py": "x = 1\n", "fakepkg-1.0.dist-info/METADATA": "Name: fakepkg\n",
             "fakepkg-1.0.dist-info/RECORD": "fakepkg/__init__.py,,\n",
             "fakepkg-1.0.dist-info/INSTALLER": "pip\n", "fakepkg-1.0.dist-info/REQUESTED": "",
             "fakepkg-1.0.dist-info/direct_url.json": '{"url": "file:///C:/Users/author/secret"}',
             "fakepkg-1.0.dist-info/licenses/LICENSE": "the licence of fakepkg\n"}
    for name, text in files.items():
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_bytes(text.encode("utf-8"))

    listed = [PurePosixPath(name) for name in files]

    class Fake:
        version = "1.0"
        files = listed

        @staticmethod
        def locate_file(item):
            return site / item

    monkeypatch.setattr(viewer, "_PURE", ("fakepkg",))
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: Fake)
    data, licences = viewer._engine_zip()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        kept = sorted(n for n in archive.namelist() if n.startswith("fakepkg"))
        assert b"secret" not in b"".join(archive.read(n) for n in archive.namelist() if "fakepkg" in n)
    assert kept == ["fakepkg-1.0.dist-info/METADATA", "fakepkg-1.0.dist-info/licenses/LICENSE",
                    "fakepkg/__init__.py"]
    assert licences == [("fakepkg 1.0: fakepkg-1.0.dist-info/licenses/LICENSE", "the licence of fakepkg\n")]


# ---------------------------------------------------------------- fix round: M6, M7
def test_a_download_follows_only_https_hops_and_keeps_the_callers_proxies(monkeypatch):
    import urllib.request
    handler = vr._HttpsOnly()
    request = urllib.request.Request(vr.CORE_URL)
    for bad in ("http://objects.example/x", "file:///C:/Windows/win.ini", "ftp://example/x"):
        with pytest.raises(vr.ViewerRuntimeError, match="redirects to an address that is not https"):
            handler.redirect_request(request, None, 302, "Found", {}, bad)
    assert handler.redirect_request(request, None, 302, "Found", {},
                                    "https://objects.example/x").full_url == "https://objects.example/x"
    with pytest.raises(vr.ViewerRuntimeError, match="is not an https address"):
        vr._open("http://github.com/x", 1)
    # What `_open` opens with: the https-only handler, and the environment's proxy.
    built = []
    real = urllib.request.build_opener

    class Stop(Exception):
        pass

    def build(*handlers):
        opener = real(*handlers)
        built.append(opener)
        monkeypatch.setattr(opener, "open", lambda *a, **k: (_ for _ in ()).throw(Stop()))
        return opener
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setattr(urllib.request, "build_opener", build)
    with pytest.raises(Stop):
        vr._open(vr.CORE_URL, 1)
    kinds = [type(h).__name__ for h in built[0].handlers]
    assert "_HttpsOnly" in kinds and "ProxyHandler" in kinds and "HTTPRedirectHandler" not in kinds
    assert vr.fetch.__kwdefaults__["opener"] is vr._open


def test_freezing_finds_a_location_in_another_spelling(tmp_path):
    """M7: the author's notes name the file as Windows shows it, in capitals."""
    for number, spelled in enumerate(("DATA\\CLINIC_VISITS.CSV", "data%2Fclinic_visits.csv", "Data/Clinic_Visits.csv")):
        path = _clinic(tmp_path / str(number), notes=f"Fed from {spelled} every morning.")
        with pytest.raises(ValueError, match="its location is also written in the model's info"):
            viewer.freeze_data(path, path.parent)
