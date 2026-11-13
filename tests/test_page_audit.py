"""The gate of the hand-over pages (journeys tier P11b, review slice S4): every page a person
receives, in a real browser at a phone's 360 px and a desktop's 1440 px, light and dark. Each
must: show no internal name (`system/`, `resource/`, `user/`), no "-inf", no command line; keep
text at 4.5:1 against what is behind it; give every control 40 px of height at 360 px; never
scroll sideways. Driven by tests/viewer_drive.cjs (node 22, Chrome or Edge, no package)."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_nurse_study

HERE = Path(__file__).resolve().parent
MODELS = HERE.parents[0] / "nagasim" / "models"
NODE = shutil.which("node")
BROWSERS = [path for path in (
    shutil.which("chrome") or r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if path and Path(path).is_file()]


def _node22() -> bool:
    if NODE is None:
        return False
    try:
        version = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=30).stdout
        return int(version.strip().lstrip("v").split(".")[0]) >= 22
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not (_node22() and BROWSERS), reason="node 22 and Chrome or Edge are needed")]

# What the page shows, measured in the page. Text: every element with a text node of its own that
# the reader can see, its colour against the first opaque background behind it (WCAG 2 contrast).
# Controls: buttons, selects, text inputs, sliders and fold headers a reader can see.
AUDIT = r"""(function () {
  function rgb(text) { var m = text.match(/[\d.]+/g) || []; return m.map(Number); }
  function lum(c) { var v = c.slice(0, 3).map(function (x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; }
  function ratio(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  function behind(el) {
    for (var e = el; e; e = e.parentElement) {
      var c = rgb(getComputedStyle(e).backgroundColor);
      if (c.length >= 3 && (c.length < 4 || c[3] > 0.5)) { return c; }
    }
    return [255, 255, 255];
  }
  var low = [], small = [], text = document.body.innerText, notices = document.getElementById("notices-text");
  /* The viewer's third-party licence texts quote their own file paths: not the model's names. */
  if (notices) { text = text.replace(notices.innerText, ""); }
  Array.prototype.forEach.call(document.querySelectorAll("body *"), function (el) {
    if (el.closest("svg, canvas, script, style, template") || !el.checkVisibility || !el.checkVisibility()) { return; }
    var own = Array.prototype.some.call(el.childNodes, function (n) { return n.nodeType === 3 && n.textContent.trim(); });
    var st = getComputedStyle(el);
    if (own && !el.disabled && parseFloat(st.opacity) > 0.9) {
      var r = ratio(rgb(st.color), behind(el));
      if (r < 4.5) { low.push(el.tagName + "." + el.className + " '" + el.textContent.trim().slice(0, 30) + "' " + r.toFixed(2)); }
    }
    if (el.matches("button, select, input:not([type=checkbox]):not([type=hidden]), summary")) {
      var h = el.getBoundingClientRect().height;
      if (h > 0 && h < 40) { small.push(el.tagName + "#" + el.id + "." + el.className + " " + Math.round(h)); }
    }
  });
  return JSON.stringify({
    width: document.documentElement.scrollWidth, inner: window.innerWidth,
    internal: (text.match(/(^|[\s(])(system|resource|user)\/[^\s]*/g) || []).slice(0, 5),
    inf: /-inf\b/.test(text), command: (text.match(/\bnaga (room|run|study|twin|viewer|scenarios)\b[^\n]*/g) || []).slice(0, 3),
    low: low.slice(0, 8), small: small.slice(0, 8)});
})()"""


def _runtime():
    from nagasim import viewer_runtime as vr
    from nagasim.settings import userpaths
    own = userpaths._base_dir(userpaths._NEW_NAME) / "viewer-runtime" / f"pyodide-{vr.VERSION}"
    for folder in (os.environ.get(vr.ENV_VAR), own):
        if folder and (Path(folder) / vr.CORE_NAME).is_file():
            return Path(folder)
    return None


def _cli(*args, cwd):
    done = subprocess.run([sys.executable, "-m", "nagasim", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=600, env={**os.environ, "PYTHONPATH": str(HERE.parent)})
    assert done.returncode == 0, done.stderr[-2000:]


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    """{name: path} of the six kinds of page (the package holds two pages: client and room)."""
    from nagasim import api, monitor, studypackage, studywriteup, viewer
    from nagasim.core import schema
    from nagasim.report import build_report

    root = tmp_path_factory.mktemp("pages")
    folder, st = make_nurse_study(root / "study")
    out = {"deck": studywriteup.slides(folder, st)}
    studypackage.package(folder, st)
    out["client"], out["room"] = folder / "package" / "client.html", folder / "package" / "control-room.html"
    lwb = MODELS / "line_with_buffers.json"
    layout = schema.migrate(json.loads(lwb.read_text(encoding="utf-8")))
    results = api.run_model(str(lwb), replications=2, run_length=300, warmup=0)
    out["report"] = root / "report.html"
    build_report(layout, results, title="line_with_buffers", path=out["report"])
    from nagasim import experiments
    run = experiments.run_scenarios(layout, replications=2, run_length=300)
    out["scenario report"] = root / "scenario-report.html"
    build_report(layout, run.results[experiments.BASELINE], experiment=run, title="line_with_buffers",
                 path=out["scenario report"])
    out["player"] = root / "player.html"
    _cli("scenarios", str(lwb), "--replications", "2", "--run-length", "300", "--player", str(out["player"]), cwd=root)
    runtime = _runtime()
    if runtime is not None:
        out["viewer"] = root / "viewer.html"
        viewer.build_viewer(layout, out["viewer"], runtime=runtime)
    import importlib.util
    spec = importlib.util.spec_from_file_location("monitor_tests", HERE / "test_monitor.py")
    doc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doc)
    model = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    page = monitor.Page(model, root / "monitor.html", every=5.0, title="Single server")
    result = doc.result()
    result["forecast"]["kpis"] += [{"kpi": "resource/cell_1/Machine/in_service", "mean": 1.0, "q05": 1.0, "q95": 1.0},
                                   {"kpi": "system/Started From State At", "mean": 700.0, "q05": 700.0, "q95": 700.0}]
    page.write(result, [])
    out["monitor"] = root / "monitor.html"
    return out


def _audit(page: Path, tmp_path: Path) -> dict:
    steps = []
    for width in (360, 1440):
        for dark in (False, True):
            steps += [{"width": width}, {"dark": dark}, {"eval": "new Promise(function (r) { setTimeout(r, 400); })"},
                      {"eval": AUDIT}]
    # And once with every fold open: what a reader finds by opening one counts too (S4-03).
    steps += [{"eval": 'document.querySelectorAll("details").forEach(function (d) { d.open = true; }); 1'},
              {"eval": "new Promise(function (r) { setTimeout(r, 400); })"}, {"eval": AUDIT}]
    done = subprocess.run([NODE, str(HERE / "viewer_drive.cjs"), BROWSERS[0], str(page), str(tmp_path / "profile"),
                           json.dumps(steps)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=420)
    assert done.returncode == 0 and done.stdout, done.stderr[-2000:]
    got = json.loads(done.stdout)
    assert not got.get("failed"), got["failed"]
    measured = [json.loads(a) for a in got["answers"] if isinstance(a, str) and a.startswith("{")]
    assert len(measured) == 5, got["answers"]
    return dict(zip(("360 light", "360 dark", "1440 light", "1440 dark", "1440 dark, folds open"), measured))


@pytest.mark.parametrize("name", ["deck", "client", "room", "report", "scenario report", "player", "viewer",
                                  "monitor"])
def test_every_hand_over_page_passes_the_audit(pages, name, tmp_path):
    if name not in pages:
        pytest.skip("the browser runtime is not fetched (naga viewer --fetch)")
    failures = []
    for where, m in _audit(pages[name], tmp_path).items():
        if m["width"] > m["inner"]:
            failures.append(f"{where}: scrolls sideways ({m['width']} > {m['inner']})")
        if m["internal"]:
            failures.append(f"{where}: internal names {m['internal']}")
        if m["inf"]:
            failures.append(f"{where}: -inf")
        if m["command"]:
            failures.append(f"{where}: a command line {m['command']}")
        if m["low"]:
            failures.append(f"{where}: contrast under 4.5:1 {m['low']}")
        if where.startswith("360") and m["small"]:
            failures.append(f"{where}: controls under 40 px {m['small']}")
    assert not failures, "\n".join(failures)
