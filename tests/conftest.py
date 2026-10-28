import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
# Headless Qt for every UI test and subprocess; set before PySide6 is imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# DEVLOG #273: set BEFORE any test module imports nagasim/product, so the
# whole session -- including a plugin-loading subprocess, which inherits
# os.environ -- resolves user files under a session temp dir instead of the
# real profile. setdefault: a test that wants the real resolution logic
# (test_userpaths.py) monkeypatches the env var away per test, same as today.
_TEST_USER_DIR = Path(tempfile.mkdtemp(prefix="nagasim-test-user-"))
os.environ.setdefault("NAGASIM_USER_DIR", str(_TEST_USER_DIR))
os.environ.setdefault("PRODUCT_LIBRARY_DIR", str(_TEST_USER_DIR / "libraries"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import pytest

from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, setup_run, step_until


@pytest.fixture
def env():
    """Fresh simpy Environment wired with the engine's managers (seed=0)."""
    return initialize_sim_env(seed=0)


@pytest.fixture
def make_model(tmp_path):
    """Returns a function that writes a gen-3 model dict to tmp_path and
    returns its path as a string."""

    def _make(blocks, links, resources=None, entities=None, variables=None, simulation=None,
              schedules=None, failures=None, filename="model.json"):
        model = {
            "blocks": blocks,
            "links": links,
            "resources": resources or [],
            "entities": entities or [],
            "variables": variables or [],
            "schedules": schedules or [],
            "failures": failures or [],
            "simulation": simulation or {"time_limit": None, "warmup": 0, "replications": 1},
        }
        path = tmp_path / filename
        path.write_text(json.dumps(model))
        return str(path)

    return _make


@pytest.fixture
def simple_model_path(make_model):
    """A minimal valid gen-3 model: Create(Constant 1, max_arrivals=5) -> Delay(Constant 1) -> Dispose."""
    blocks = [
        {
            "name": "create_1", "description": "create_1", "type": "CREATE", "position": [0, 0],
            "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                       "entities_per_arrival": 1, "max_arrivals": 5, "start_time": 0},
        },
        {
            "name": "delay_1", "description": "delay_1", "type": "DELAY", "position": [1, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 1.0}},
        },
        {
            "name": "dispose_1", "description": "dispose_1", "type": "DISPOSE", "position": [2, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0}},
        },
    ]
    links = [["create_1", "delay_1"], ["delay_1", "dispose_1"]]
    return make_model(blocks, links)


def run_layout(env, path, run_len):
    """Prepare a gen-3 model at `path` against `env`, start every CREATE block,
    and step the run to run_len. Returns the model (GraphModel). Plain helper
    (not a fixture): it has no fixture dependencies of its own, so any test or
    helper can import and call it directly."""
    model = prepare_model(env, path)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, run_len)
    return model


def run_env(layout, run_len, *, seed=0, warmup=0.0, subscribers=(), controllers=None):
    """Build and step one replication exactly the way a real run does
    (`setup_run` + `step_until`), then integrate the level system to the
    run's end the way `calc_kpis` does. Returns (env, model).

    Replaces ~28 hand-rolled `_run` helpers that each replayed this sequence
    themselves and had drifted from it -- some skipped the warm start, the
    level/agent start or the sampler (docs/history/ARCH-REVIEW-PLAN-2026-09-27.md
    D1). A file whose own `_run` still differs on purpose (e.g. a mid-run
    snapshot before stepping further) keeps its own body and says why."""
    env, model = setup_run(layout, seed=seed, run_len=run_len, warmup=warmup,
                            subscribers=subscribers, controllers=controllers)
    step_until(env, run_len)
    if env.ctx.level_system is not None:
        env.ctx.level_system.sync(env.ctx.sim_end_time)
    return env, model


def problem_codes(layout, *, severity="error"):
    """`p.code` for every problem `validate_model_structured` finds in
    *layout*, filtered to *severity* (None for all codes, warnings included).
    Replaces ~23 hand-copied `_codes` helpers."""
    return {p.code for p in validate_model_structured(layout)
            if severity is None or p.severity == severity}


def metric_value(kpi, name):
    """Read one row's Value from a KPI dict's 'system' frame by Metric name."""
    df = kpi["system"]
    return float(df.loc[df["Metric"] == name, "Value"].iloc[0])


def resource_row(kpi, name="Server"):
    """One row of a KPI dict's 'resource_summary' frame, by Resource name."""
    res = kpi["resource_summary"]
    return res[res["Resource"] == name].iloc[0]


# -- layout_builder helpers ---------------------------------------------
# A handful of files were hand-building the same gen-current block/layout
# dicts (test_oracles.py, test_schedules.py, test_link_delays.py,
# test_case_study.py). These give the same shapes as a plain dict, for tests
# that need the layout dict itself (not a file path -- see `make_model` above
# for that). Validation migrates any schema_version < CURRENT up, so
# the exact starting version is not load-bearing for these tests.
def layout(blocks, links, **sections):
    doc = {
        "schema_version": 9, "blocks": blocks, "links": links,
        "resources": [], "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }
    doc.update(sections)
    return doc


def create(name="create_1", **params):
    base = {"e_type": "DEFAULT", "arrival_mode": "Interarrival", "schedule_name": "",
            "distribution": "Constant", "dist_params": {"value": 1.0},
            "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0}
    base.update(params)
    return {"name": name, "description": name, "type": "CREATE", "position": [0, 0], "params": base}


def process(name="process_1", resource="Server", value=1.0, distribution="Constant", **extra):
    params = {"action": "Seize Delay Release", "distribution": distribution,
              "dist_params": ({"value": value} if distribution == "Constant" else {"rate": value}),
              "priority": 0, "quantity": 1, "resource_name": resource}
    params.update(extra)
    return {"name": name, "description": name, "type": "PROCESS", "position": [1, 0], "params": params}


def delay(name="delay_1", value=3.0):
    return {"name": name, "description": name, "type": "DELAY", "position": [1, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": value}}}


def dispose(name="dispose_1"):
    return {"name": name, "description": name, "type": "DISPOSE", "position": [2, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}}


def chain(*blocks):
    """Sequential gen-9 links for a run of blocks, by their own "name" field:
    chain(b0, b1, b2) -> [{"source": b0.name, "target": b1.name}, ...]."""
    return [{"source": blocks[i]["name"], "target": blocks[i + 1]["name"]}
            for i in range(len(blocks) - 1)]


def sd_layout(**sections):
    """A blockless model: a pure SD model has no CREATE or DISPOSE, only
    levels, flows and auxiliaries."""
    return layout([], [], **sections)


def resource_record(name, capacity=1):
    return {"name": name, "capacity": capacity, "discipline": "FIFO",
            "cost_busy": 0.0, "cost_idle": 0.0, "cost_per_use": 0.0,
            "cost_time_unit": "hours"}


def block(name, btype, params, position=None):
    return {"name": name, "description": name, "type": btype,
            "position": position or [0, 0], "params": params}


def decide(name, mode, **params):
    base = {"mode": mode, "probs": [], "conditions": [], "policy": "", "observation": [],
            "fallback": "first", "queue_exprs": []}
    base.update(params)
    return block(name, "DECIDE", base)


def entity_count(kpi, e_type):
    df = kpi["entity_summary"]
    rows = df[df["Entity Type"] == e_type]
    return int(rows["Total Created"].iloc[0]) if not rows.empty else 0


def field_rows(panel):
    """(param_name, widget) for every row of the panel's body form."""
    form = panel._body_layout
    rows = []
    for i in range(form.rowCount()):
        label_item = form.itemAt(i, form.ItemRole.LabelRole)
        field_item = form.itemAt(i, form.ItemRole.FieldRole)
        if field_item is None:
            continue
        # properties._add_row inserts a zero-width space after each "_" so a
        # long param name can wrap (item 5, U1.9); strip it back out here so
        # the label still reads as the plain param name it labels.
        label = (label_item.widget().property("param")
                 if label_item is not None and label_item.widget() else None)
        rows.append((label, field_item.widget()))
    return rows


def agent_seed(seed, name, *words):
    """Agent-system SeedSequence, the D107 recipe restated for oracles:
    [seed, lo, hi, *words, md5("agent") % 1e4], (lo, hi) = md5(name)'s first 64 bits."""
    import hashlib

    import numpy as np
    h = int.from_bytes(hashlib.md5(name.encode()).digest()[:8], "little")
    tag = int(hashlib.md5(b"agent").hexdigest(), 16) % 10_000
    return np.random.SeedSequence([seed, h & 0xFFFFFFFF, h >> 32, *words, tag])


def agent_rng(seed, population, key, call=0):
    """Draw call `call` of agent `key` (slot, or count + birth serial): Philox
    under the population's key at counter (0, call, key, 0), empty buffer (D107)."""
    import numpy as np
    k = agent_seed(seed, population).generate_state(2, np.uint64)
    return np.random.Generator(np.random.Philox(key=k, counter=[0, call, key, 0]))


class AgentStream:
    """Agent `key`'s stream as expressions see it: every attribute read is
    the agent's next draw call (`agent_rng(..., call)`), as the engine's
    `_Stream` proxy resolves one `_Population.rng(i)` per read (D107)."""

    def __init__(self, seed, population, key):
        self.args, self.calls = (seed, population, key), 0

    def __getattr__(self, name):
        self.calls += 1
        return getattr(agent_rng(*self.args, self.calls - 1), name)


# 6D1 U3: the four agent-based templates whose 2-replication, 40-time-unit
# curated run costs more than about 0.3 s (measured 0.46-0.83 s here, against
# 0.01-0.25 s for the other 12 new templates) -- a grid/continuous-space
# population update is heavier per tick than a queueing model's event list.
# Excluded from the session-wide curated run to keep its cost down; each one
# is still checked on its own by its library's asset test (the oracle run
# `PY -m product library test <id>` uses, see `product/assetlibs/*.nagalib.json`).
CURATED_RESULTS_EXCLUDED = {
    "abm_boids_flock.json", "abm_schelling_segregation.json",
    "abm_sugarscape_lite.json", "abm_wolf_sheep.json",
}


@pytest.fixture(scope="session")
def curated_results():
    """Every bundled example run ONCE per session: 2 replications, 40 time
    units, no warm-up (some examples declare a warm-up longer than this run) --
    except `CURATED_RESULTS_EXCLUDED`, whose own tests already cover them.

    Three tests used to re-run all of them independently -- the frame-key
    check (test_api), the report builder (test_report) and the results window
    (test_ui_run) -- and the runs, not the assertions, were what cost. Each
    only needs SOME real results for every example; the models' own numbers
    are checked by the oracle tests.
    """
    from nagasim.api import run_model

    models_dir = Path(__file__).resolve().parents[1] / "nagasim" / "models"
    return {path: run_model(str(path), replications=2, run_length=40, warmup=0)
            for path in sorted(models_dir.glob("*.json"))
            if path.name not in CURATED_RESULTS_EXCLUDED}


@pytest.fixture(scope="session")
def worker_pool():
    """ONE `ProcessPoolExecutor` for the whole session.

    On Windows every pool spawns fresh interpreters that re-import pandas,
    scipy and simpy: about 2.5 s for 20 workers and 0.8 s for 2. Three tests
    used to pay that separately. `api.run_model` and
    `experiments.run_scenarios` both accept a caller-owned `pool=`, so the
    spawn is paid once here and the runs themselves are unchanged -- a
    replication is seeded from its own seed and the frames are re-ordered by
    seed before they are concatenated, whoever ran them.
    """
    from concurrent.futures import ProcessPoolExecutor

    # Under xdist every test worker that asks builds its OWN pool while the
    # other workers already hold every core: 20 more interpreters each bought
    # nothing but spawn time. Serially (the full set) the machine is ours.
    cap = 4 if os.environ.get("PYTEST_XDIST_WORKER") else 20
    with ProcessPoolExecutor(max_workers=min(cap, os.cpu_count() or 1)) as pool:
        yield pool


@pytest.fixture(scope="session")
def qapp():
    """Shared offscreen QApplication for every Qt UI test (test_ui_shell.py,
    test_ui_forms.py, ...). Skips the whole test if PySide6 is not installed."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from product.ui import appfont
    appfont.apply_app_font(app)   # the same start-up path as the real app (D783)
    app.setOrganizationName("NagaTests")
    # One QSettings store PER xdist worker: with a shared one, a theme test's
    # teardown on worker A removed the key worker B had just written.
    app.setApplicationName("NagaTests-" + os.environ.get("PYTEST_XDIST_WORKER", "main"))
    # QSettings persist on disk between sessions, so a per-model choice one
    # run stored (layout view on job_shop_jackson.json) reached a later run's
    # fresh MainWindow and emptied its schematic: start every session clean.
    from PySide6.QtCore import QSettings
    QSettings().clear()
    from product.ui.app import MainThreadGC
    collector = MainThreadGC(app)   # same guard as the real app (DEVLOG #94)
    yield app
    collector.stop()
    # DEVLOG #89: the offscreen platform keeps the last QMimeData a copy test
    # put on the clipboard, and destroying it at interpreter exit segfaults
    # (exit 139 after every test passed). The real Windows platform hands the
    # data to the OS and is unaffected.
    app.clipboard().clear()


_QT_MODULES: dict = {}


def _module_uses_qt(module) -> bool:
    """True when a test module holds anything from PySide6 or product.ui."""
    name = module.__name__
    if name not in _QT_MODULES:
        _QT_MODULES[name] = any(
            str(getattr(value, "__module__", "") or "").startswith(("PySide6", "product.ui"))
            for value in vars(module).values())
    return _QT_MODULES[name]


@pytest.fixture(autouse=True)
def _qt_tests_always_have_an_application(request):
    """DEVLOG #97: a Qt test that forgot the `qapp` fixture crashed natively
    (access violation) whenever xdist scheduled it FIRST on a worker, before any
    other test had created the QApplication -- which looked like random worker
    crashes moving between tests. Every test of a module that uses Qt now gets
    the application, asked for or not."""
    if not (_module_uses_qt(request.module) or "qapp" in request.fixturenames):
        yield
        return
    app = request.getfixturevalue("qapp")
    yield
    # DEVLOG #113: a test that launched the real entry point in-process
    # renamed the shared QApplication, and every later test then read and
    # wrote the USER's QSettings profile. Put the test names back after
    # each test, whatever it did.
    if not app.applicationName().startswith("NagaTests"):
        app.setOrganizationName("NagaTests")
        app.setApplicationName("NagaTests-" + os.environ.get("PYTEST_XDIST_WORKER", "main"))
    # DEVLOG #255: settings one test wrote (a run seed, a theme) leaked into the
    # next test on the same worker; every Qt test starts from empty settings.
    # Not `clear()`: on Windows it deletes the application's registry key, and
    # a long-lived QSettings (a module-scoped window's) then writes into the
    # deleted key and loses everything. Removing the children keeps the key.
    from PySide6.QtCore import QSettings
    settings = QSettings()
    for key in settings.childGroups() + settings.childKeys():
        settings.remove(key)


@pytest.fixture(autouse=True)
def _modal_dialogs_never_hang_the_suite(request, monkeypatch):
    """A `QMessageBox.critical/warning/information/question` call that reaches
    a REAL `.exec()` blocks forever under the offscreen platform (no user to
    click it) -- traced to a 55-minute xdist hang (a background worker's
    `_on_..._failed` slot showed one after an unrelated exception). Every
    static opener now raises AssertionError with the dialog's own text
    instead of a call any test forgot to expect, so a modal that should not
    have happened fails that test immediately rather than hanging the whole
    run. A test that DOES expect one still monkeypatches it itself, inside
    the test body -- that setattr runs after this fixture's and wins for the
    rest of the test, exactly as it did before this fixture existed.
    Active for every test that uses the Qt application too, including one that
    imports Qt only inside its body (a moved dialog once hung the suite that way)."""
    if not (_module_uses_qt(request.module) or "qapp" in request.fixturenames):
        yield
        return
    from PySide6.QtWidgets import QMessageBox

    def _opener(name):
        def raise_unexpected(*args, **kwargs):
            text = args[2] if len(args) > 2 else kwargs.get("text", "")
            raise AssertionError(f"unexpected QMessageBox.{name}(...): {text!r} -- "
                                 f"monkeypatch QMessageBox in the test if one is expected")
        return staticmethod(raise_unexpected)

    for name in ("critical", "warning", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, _opener(name))
    yield


@pytest.fixture(scope="module")
def _main_window(qapp):
    """ONE MainWindow per test module. Building one costs tens of milliseconds
    of Qt work that no test is actually about, and it adds up over ~150 UI
    tests; `window` below hands each test the same window with a fresh empty
    document, which is the state a per-test MainWindow would have given it."""
    from product.ui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.document.dirty = False  # no save prompt on teardown
    win.close()


@pytest.fixture
def window(_main_window):
    _main_window.document.dirty = False   # never prompt when resetting
    assert _main_window.new_model()
    return _main_window


@pytest.fixture(scope="session", autouse=True)
def _no_real_keyring():
    """The suite never reads the developer's OS credential store: `keyring` does not import
    (a test that wants one installs a fake, see `fake_keyring`). Session scope, because the
    module-scoped `_main_window` builds the Assistant dock, which reads the key, before any
    function-scoped fixture runs."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "keyring", None)
        yield


@pytest.fixture(scope="session", autouse=True)
def _no_real_qsettings(tmp_path_factory):
    """QSettings reads and writes a temp ini file, never the developer's own preferences
    (a theme test wrote the real "ui/theme", and two runs at once collided). Nothing is
    restored: the process ends with the session."""
    try:
        from PySide6.QtCore import QSettings
    except ImportError:         # the engine alone has no Qt
        return
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      str(tmp_path_factory.mktemp("qsettings")))


@pytest.fixture(autouse=True)
def _find_tool_state():
    """find_tool's process-wide state (the tools a server exposes, the tool picker) starts and ends
    clean: a test that built a `--tools compact` server left its subset for the next test on the worker."""
    from nagasim.agent_tools import discover
    yield
    discover.exposed = None
    discover.picker = None


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch, request):
    """Tests never reach a real model: the local default points at the discard port and no
    key or provider setting leaks in from the developer's shell. A `live` test is the
    exception: it runs only on request, with the caller's key (test_assistant_live.py)."""
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setenv("NAGASIM_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    for name in ("ANTHROPIC_API_KEY", "NAGASIM_LLM_API_KEY", "NAGASIM_LLM_PROVIDER",
                 "NAGASIM_LLM_MODEL", "NAGASIM_LLM_REASONING"):
        monkeypatch.delenv(name, raising=False)
    from nagasim import llm
    monkeypatch.setattr(llm, "_stored_user_key", lambda: "")    # nor the key stored for the Windows user (#745)


# ---------------------------------------------------------------- live connectivity (PHASE7 7.8)
@pytest.fixture(scope="session")
def tls_files(tmp_path_factory):
    """(certificate, key): PEM paths of a self-signed certificate for the host name
    `plant.test`, made here at session start, so no key is ever committed."""
    pytest.importorskip("cryptography")
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "plant.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1))
                   .not_valid_after(now + datetime.timedelta(days=2))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("plant.test")]),
                                  critical=False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()))
    folder = tmp_path_factory.mktemp("tls")
    (folder / "plant.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (folder / "plant.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return str(folder / "plant.pem"), str(folder / "plant.key")


@pytest.fixture
def plant_net(monkeypatch):
    """No test of a live reader reaches a real service: every host name resolves from the
    table this returns ({host: [addresses]}; a name not listed is this machine), and a
    socket that tries to connect anywhere but this machine fails the test."""
    import ipaddress
    import socket

    table: dict[str, list[str]] = {}
    real_connect = socket.socket.connect

    def getaddrinfo(host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))
                for address in table.get(host, ["127.0.0.1"])]

    def connect(sock, address):
        assert ipaddress.ip_address(address[0]).is_loopback, f"a test tried to reach {address}"
        return real_connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    return table


@pytest.fixture
def rest_plant(plant_net, tls_files):
    """`rest_plant(routes, tls=False) -> (port, seen)`: an HTTP server on 127.0.0.1 that
    answers from *routes* ({path with query: answer}). An answer is rows (sent as JSON), a
    (status, headers, body bytes) triple, or a callable taking the request handler. *seen*
    collects (method, path, headers) of every request, whatever its method."""
    import http.server
    import socketserver
    import ssl
    import threading

    servers = []

    def start(routes, tls=False):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def parse_request(self):
                parsed = super().parse_request()
                if parsed:
                    seen.append((self.command, self.path, dict(self.headers)))
                return parsed

            def do_GET(self):
                answer = routes.get(self.path, (404, {}, b"not here"))
                if callable(answer):
                    return answer(self)
                if not isinstance(answer, tuple):
                    answer = (200, {"Content-Type": "application/json"},
                              json.dumps(answer).encode("utf-8"))
                status, headers, body = answer
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.handle_error = lambda *_args: None       # a client that hung up is not news
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls_files)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
        servers.append(server)
        return server.server_address[1], seen

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
