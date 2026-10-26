"""The twin cycle (PHASE7 7.12, D265 to D276): monitor, start from the plant's state,
forecast, compare, report.

    source = twin.PlantSource(model, mapping, feed)         # the caller's feed (rule 12)
    cycle = twin.Cycle(source, out_dir="twin_out", horizon=480, replications=50,
                       forecast_every=300, background=True)
    twin.loop(cycle.step, every=5.0)

* ONE `step()`: ask the source what the plant looks like, take in a forecast that finished,
  compare the stored forecasts with what is observed now, start a forecast when one is due,
  write one result. A step never raises for what a plant, a disk or a forecast does: each
  is a coded problem in the result it still writes.
* A SOURCE is what differs between twins: `observe()` and `start()`. `PlantSource` reads a
  `live.Feed` through a plant mapping (7.11); the product's MES twin is another source.
* TWO CADENCES on the monotonic clock: every step monitors; a forecast has its own, slower
  one, never two at once, cancelled by `stop()`, which is terminal.
* A start is made or refused, NEVER replaced: a model that cannot start from the plant's
  state is said so, monitored, and not forecast (D270).
* The result is one JSON document (`twin.json`, format 1) replaced atomically in the
  caller's directory, the only place written; `read_result` is how a second process (a
  monitor page, the Presenter, an agent tool) reads it. Every bound is in `LIMITS`.

* With an `advisor` (`recommend.Advisor`, 7.15) the caller's candidate actions are compared
  after each forecast, in its thread and on its seed, and never enter the history or the
  rolling score (D301).
* With a `refit` (`refit.Refitter`, D740, D741) a held alert has the caller's factors
  calibrated and proposed through the advisor's outbox; an adopted refit is an overrides
  layer kept in the history, applied from the next cycle under a model identity of its own
  (its score and alerts start again), until an `ended` reply. The model file is never written.

Read-only towards the plant: nothing here sends anything to it. The one write outside the
caller's result directory is the advisor's, to the outbox the caller named at start (D227).
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from nagasim import netguard, plant, readers, score
from nagasim import refit as refits
from nagasim.core import initial_state
from nagasim.core.playback import Timeline, resource_rows
from nagasim.core.problems import Problem, problem
from nagasim.live import SKEW_SECONDS, Changes, iso
from nagasim.sources import ReadError

__all__ = ["FORMAT", "KIND", "LIMITS", "RESULT_FILE", "HISTORY_FILE", "Cycle", "PlantSource",
           "LogFeed", "loop", "parse_interval", "read_result", "write_json", "write_text",
           "forecast_from_state", "main"]

log = logging.getLogger(__name__)

FORMAT = 1
KIND = "naga-twin"
RESULT_FILE = "twin.json"
HISTORY_FILE = "twin_history.json"
# Ceilings (D274). The caller's flags move a value under its ceiling; a model, a mapping,
# a log or a tool moves none.
LIMITS = {
    "replications": 1000,           # of one forecast
    "every": 1.0,                   # seconds: the shortest cadence
    "forecasts": 50,                # records kept in the history
    "times": 6,                     # stated times of one forecast
    "series": 50,                   # compared series of one forecast
    "kpis": 200,                    # end-of-horizon rows of one forecast
    "comparison": 100,              # rows of one result
    "problems": 100,                # of one result
    "names": 2000,                  # entries of one table of the view
    "pulls": 50,                    # of one source in one step, 1000 events each
    "result_bytes": 2 * 2 ** 20,    # one result file
    "history_bytes": 8 * 2 ** 20,   # the history file: 50 records of 300 points and 200 KPIs
}
# What 7.11 observes without inference (D269), and what a plant counter counts (D290): the
# series a forecast is compared on.
_COMPARED = re.compile(r"^(system/WIP|resource/.+/in_service|level/.+|block/.+/out)$")
_PULL = 1000                        # events of one pull of a source
_HELD = 100_000                     # mapped events kept for `PlantSource.take`
_INTERVAL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$")


def parse_interval(text) -> float:
    """"15m" / "2h" / "90" (seconds) -> seconds."""
    match = _INTERVAL.match(str(text))
    if not match:
        raise ValueError(f"'{text}' is not an interval; write 30s, 15m, 2h or 1d")
    return float(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2) or "s"]


def _num(value):
    """A finite number, or None: a result is valid JSON whatever a forecast holds."""
    ok = isinstance(value, (int, float)) and abs(value) <= sys.float_info.max  # an int may be
    return float(value) if ok else None                     # too large for a float (#373)


# ---------------------------------------------------------------- files
def write_json(path, data) -> None:
    """*data* as the whole content of *path*, or the file as it was (`write_text`)."""
    try:
        text = json.dumps(data, allow_nan=False)
    except (ValueError, TypeError) as exc:
        log.warning("twin: %s could not be written: %r", Path(path).name, exc)
        raise OSError(type(exc).__name__) from None
    write_text(path, text)


def write_text(path, text: str, *, level: int = logging.WARNING) -> None:
    """*text* as the whole content of *path*, or the file as it was: written beside it and
    moved over it, so a reader never sees a part (D267). Raises OSError naming the class
    of what went wrong (it may end in a result, D216); the exception itself is logged, at
    *level* (a caller that has said a refusal already logs its repeats lower)."""
    path = Path(path)
    # A name of this write's own, opened only if new: a second writer on the directory, or
    # what a crashed one left, is never opened. Default permissions (not `mkstemp`'s 600):
    # the reader may be another account's monitor.
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The same bytes on every system (a page pins its script by hash).
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        log.log(level, "twin: %s could not be written: %r", path.name, exc)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise OSError(type(exc).__name__) from None


def _read(path, kind: str, ceiling: int):
    """The document in *path* when it is one of ours and under its bound, else None."""
    # The one checked reader (D790): a plain file judged on the opened handle, so a FIFO,
    # device or link is not read, and bounded by the bytes read (#522).
    try:
        doc = json.loads(netguard.read_caller_file(path, max_bytes=ceiling))
    except (netguard.FileRefused, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) and doc.get("format") == FORMAT \
        and doc.get("kind") == kind else None


def read_result(directory) -> tuple:
    """(the latest result a twin wrote into *directory*, []) or (None, the problem). The
    one reader, for a second process: bounded before it is parsed, whole because the writer
    replaces the file, and trusted for nothing beyond its `format`."""
    # A network path is a connection (`FileSink` refuses it the same way, #523).
    doc = None if netguard.is_unc_path(str(directory)) else         _read(Path(directory) / RESULT_FILE, KIND, LIMITS["result_bytes"])
    if doc is None:
        return None, [problem("E_TWIN_RESULT", "no twin result of format 1 can be read there "
                              "(none yet, over the size bound, or another file)", "/",
                              hint="start a twin with `naga twin run ... --out DIR`")]
    return doc, []


# ---------------------------------------------------------------- the forecast
def forecast_from_state(model, *, horizon, replications, seed, workers, should_stop,
                        base_dir=None):
    """The default runner: *horizon* from the clock the model's state starts at (D232). The
    parameters are the model's own: nothing is bound from live data inside a forecast."""
    from nagasim import forecast        # pandas: loaded only when a forecast runs

    return forecast.forecast(model, horizon=initial_state.clock_of(model) + float(horizon),
                             replications=replications, seed=seed, workers=workers,
                             bind_data=False, base_dir=base_dir, should_stop=should_stop)


def _record(pending: dict, result) -> tuple:
    """(the stored record of a finished forecast, whether something was cut). The frames
    are read through `to_dict` only, so this module needs no pandas."""
    rows = result.kpis.to_dict(orient="records")
    # What a person reads first, first: the cut then falls on the long tail.
    rows.sort(key=lambda r: not str(r["KPI"]).startswith(("system/", "user/")))
    kpis = [{"kpi": str(r["KPI"]), "mean": _num(r["Mean"]), "lb": _num(r["LB"]),
             "ub": _num(r["UB"]), "q05": _num(r["Q05"]), "q95": _num(r["Q95"]),
             "n": int(r["N"])} for r in rows[:LIMITS["kpis"]]]
    series: dict[str, list] = {}
    for row in result.bands.to_dict(orient="records"):
        if _COMPARED.match(str(row["Series"])):
            series.setdefault(str(row["Series"]), []).append(row)
    start, points = pending["from"], []
    for name in sorted(series)[:LIMITS["series"]]:
        taken = set()
        for index in range(1, LIMITS["times"] + 1):
            wanted = start + pending["horizon"] * index / LIMITS["times"]
            row = min(series[name], key=lambda r: abs(r["Time"] - wanted))
            if row["Time"] > start and row["Time"] not in taken:
                taken.add(row["Time"])
                points.append({"series": name, "at": float(row["Time"]), "mean": _num(row["Mean"]),
                               "q05": _num(row["Q05"]), "q95": _num(row["Q95"]),
                               "observed": None, "observed_at": None})
    cut = len(rows) > LIMITS["kpis"] or len(series) > LIMITS["series"]
    return {**pending, "kpis": kpis, "points": points}, cut


def _sound(record) -> bool:
    """Whether a record read back from the history file is one `_compare` can walk: the
    file is this module's own, and still only known to be format 1."""
    def number_or_none(value) -> bool:
        return value is None or _num(value) is not None
    return (isinstance(record, dict) and _num(record.get("horizon")) is not None
            and isinstance(record.get("state"), str) and isinstance(record.get("points"), list)
            and all(isinstance(p, dict) and _num(p.get("at")) is not None
                    and isinstance(p.get("series"), str)
                    # Every key the walkers index: a point with one missing is not walked.
                    and all(k in p and number_or_none(p[k])
                            for k in ("observed", "observed_at", "q05", "q95", "mean"))
                    for p in record["points"]))


def _reading(record: dict, series: str, seen: dict):
    """What *seen* gives a stated point of *series* of *record*: the value; None when the
    series is not observed now; False for a running total that cannot be compared (D290:
    it lost count since the forecast started, was first seen after it, or is another
    mapper's count: a twin started again with no last reading to go on from counts from 0
    again, D378). A total is read as its
    increase since the forecast's start, and only under the marker that start was taken
    under, whole: when the count began and how often it was lost."""
    value = _num((seen.get("observed") or {}).get(series))
    cumulative = seen.get("cumulative") or {}
    if value is None or series not in cumulative:
        return value
    base = record.get("base") if isinstance(record.get("base"), dict) else {}
    start = base.get(series)
    if not (isinstance(start, list) and len(start) == 2 and _num(start[0]) is not None
            and start[1] == cumulative[series]):
        return False
    return value - start[0]


class Cycle:
    """One twin. Owned by one thread (the loop's, or the editor's worker: rule 14)."""

    def __init__(self, source, *, horizon: float, replications: int = 50, seed: int = 0,
                 workers: int = 1, out_dir=None, forecast_every: float = 300.0,
                 every: float | None = None, background: bool = False, runner=None,
                 advisor=None, alert_k: float = score.K, alert_h: float = score.H,
                 refit=None, clock=time.time, monotonic=time.monotonic):
        if not (isinstance(horizon, (int, float)) and math.isfinite(horizon) and horizon > 0):
            raise ValueError("a twin needs a positive forecast horizon")
        # The alerts' limits are the caller's (D721): a model, a mapping or a file sets none.
        if not (_num(alert_k) is not None and alert_k >= 0 and _num(alert_h) is not None
                and alert_h > 0):
            raise ValueError("an alert needs k >= 0 and h > 0")
        if refit is not None and (advisor is None or not alert_k > 0):
            raise ValueError("a refit needs an advisor, whose outbox proposes it, and k > 0")
        self.source = source
        self.horizon = float(horizon)
        self.replications = max(1, min(int(replications), LIMITS["replications"]))
        self.seed, self.workers = int(seed), workers
        self.out_dir = None if out_dir is None else Path(out_dir)
        self.forecast = None                # the latest forecast as the runner returned it
        self.failure = None                 # what the latest attempt raised, for a caller that re-raises
        # The trace events the source mapped in the latest step, for a view in this process
        # (the monitor page, the editor's canvas: D279). Never in the result (D268).
        self.events: list = []
        self._every = float(forecast_every)
        # For a reader of the result only: the cadence its caller steps this cycle at (none
        # for one step under a scheduler) and when this twin began, so that "late" can be
        # told from "quiet" and one twin from the next on the same directory.
        self._cadence = {"every": None if every is None else float(every),
                         "forecast_every": self._every}
        self._born = iso(clock())
        self._background = background
        self._runner = runner or forecast_from_state
        self._clock, self._monotonic = clock, monotonic
        self._n = 0
        self._last: tuple | None = None     # (wall, monotonic) of the last step
        self._started: float | None = None  # monotonic, of the last forecast attempt
        self._forecasts = 0                 # started by this cycle: each has its own seeds
        self._status: tuple = ("none", None)
        self._standing: list = []           # the problems of the last attempt, until the next
        self._carried: list = []            # a failed write, for the next result
        self._thread: threading.Thread | None = None
        self._done: tuple | None = None     # (pending, model, result or exception)
        self._stop = threading.Event()
        self._advice = None
        # What recommends (7.15): candidates compared after each forecast, never in the
        # history or the rolling score (D301). None: this twin recommends nothing.
        self._advisor = advisor
        history = None if self.out_dir is None else _read(
            self.out_dir / HISTORY_FILE, KIND + "-history", LIMITS["history_bytes"])
        found = history.get("forecasts") if history else None
        found = found if isinstance(found, list) else []
        self._history: list = [r for r in found if _sound(r)][-LIMITS["forecasts"]:]
        # The refit (D740) and the layer a reply adopted (D741): judged whole, as the score.
        self._refit, self._layer = refit, None
        self._refused: set = set()          # ids of taken refits the file does not allow
        kept_layer = history.get("refit") if history else None
        if refit is not None:
            self._layer, why = refit.layer_from(kept_layer)
            # The ledger holds the reply: a layer is the one it says, whole, or none.
            if self._layer is not None and (advisor is None or advisor.adopted() != self._layer):
                self._layer, why = None, "it is not the refit the outbox ledger holds as taken"
            if why:
                self._carried.append(problem(
                    "W_TWIN_REFIT", f"the adopted refit in this directory is not applied: {why}",
                    "/refit", hint="its score is not continued; a refit the outbox ledger still "
                                   "holds as taken is adopted again from its record"))
        elif kept_layer is not None:
            self._carried.append(problem(
                "W_TWIN_REFIT", "an adopted refit is kept in this directory and no --refit file "
                "was given: it is not applied, and the score of its model is not continued",
                "/refit", hint="give the same --refit file to go on with it"))
        # The rolling score (D293): this model's and this horizon's, kept in the history
        # beside the forecasts it sums, or a new one, and then never in silence.
        kept = history.get("scores") if history else None
        if kept is None and self.out_dir is not None:
            kept = score.legacy(self.out_dir / score.LEGACY_FILE)   # a first build's, once
        self._scores, why = score.Totals.load(kept, self._identity(), self.horizon, self._born)
        if why in ("model", "horizon", "unsound"):
            self._carried.append(problem(
                "W_TWIN_SCORE", "the rolling score in this directory was not continued and "
                "starts again from now: " + {
                    "model": "it is of another model", "horizon": "it is of another horizon",
                    "unsound": "it cannot be read as one"}[why], "/scores",
                hint="the stored forecasts keep their own scores; use one --out directory "
                     "a model and a horizon to keep a score"))
        # The alerts' sums (D721), kept beside the score and judged whole as it is.
        self._cusum, why = score.Cusum.load(history.get("alerts") if history else None,
                                            self._identity(), alert_k, alert_h, self._born)
        if why in ("model", "limits", "unsound"):
            self._carried.append(problem(
                "W_TWIN_ALERT", "the alert sums in this directory were not continued and "
                "start again from now: " + {
                    "model": "they are of another model",
                    "limits": "they were kept under other limits (--alert-k, --alert-h)",
                    "unsound": "they cannot be read as such"}[why], "/alerts"))
        self._observes = False              # whether the source offers a series to compare
        # A twin started again goes on from the counters' last readings (D378), which the
        # result in this directory holds when it is this model's. The oldest reading that
        # is continued is one horizon old: no increase is read over more than that. The
        # file is data: the source judges each entry, and one it refuses is no reading.
        carry = getattr(source, "carry", None)
        last = None if carry is None or self.out_dir is None else _read(
            self.out_dir / RESULT_FILE, KIND, LIMITS["result_bytes"])
        if last is not None and isinstance(last.get("model"), dict)                 and last["model"].get("hash") == getattr(source, "model_hash", None):
            try:
                carry(last.get("readings"), self.horizon)
            except Exception as exc:        # as `observe`: no step is lost to a file
                log.warning("twin: the counters' last readings were not continued (%s)",
                            type(exc).__name__)

    def _identity(self):
        """The model a score is of: the file's, or the file's with the adopted layer."""
        return refits.identity(getattr(self.source, "model_hash", None), self._layer)

    # ------------------------------------------------------------ one step
    def step(self) -> dict:
        self._n += 1
        wall, mono = self._clock(), self._monotonic()
        problems, self._carried = self._carried, []
        if self._last is not None \
                and abs((wall - self._last[0]) - (mono - self._last[1])) > SKEW_SECONDS:
            problems.append(problem(
                "W_TWIN_CLOCK", f"this machine's clock moved by "
                f"{(wall - self._last[0]) - (mono - self._last[1]):+.0f} s against its own "
                f"count of time", "/", hint="cadences are not moved; stamps written from here "
                                           "on use the new time"))
        self._last = (wall, mono)
        try:
            seen = self.source.observe()
        except Exception as exc:            # a source's own failure mode: the class only
            seen = {"ready": False, "problems": [problem(
                "W_TWIN_SOURCE", f"the source could not be read ({type(exc).__name__})", "/",
                hint="the cycle goes on and reads again at the next step")]}
        # What the source mapped since it was last asked, a listen window before the first
        # step included: a view fed from here misses nothing the result's view counts.
        take = getattr(self.source, "take", None)
        self.events = take() if take is not None else []
        problems += seen.get("problems") or []
        problems += self._collect()
        due = self._started is None or mono - self._started >= self._every
        if due and self._thread is not None:
            self._started = mono            # this turn is given up, not queued (D266)
            problems.append(problem(
                "W_TWIN_FORECAST_BUSY", "the previous forecast is still running: none was "
                "started", "/forecast", hint="lengthen --forecast-every, or shorten the "
                                             "horizon or the replications"))
        elif due and not seen.get("ready", True):
            self._status = ("skipped", "W_TWIN_SOURCE")     # and tried again at the next step
            self._no_set()
        elif due:
            self._begin(mono, seen)
            problems += self._collect()
        self._compare(seen)
        if self._refit is not None and self._advisor is not None:
            problems += self._refit_turn(seen, wall)
        if self._advisor is not None:
            try:
                problems += self._advisor.step(seen, wall)
            except Exception as exc:        # as a source's: the class only, and the step ends
                problems.append(problem(
                    "W_TWIN_RECOMMEND", f"the recommendations could not be stepped "
                    f"({type(exc).__name__})", "/recommend"))
        if self._refit is not None and self._advisor is not None:
            problems += self._follow(wall)      # a reply read now counts from the next cycle
        doc = self._document(seen, problems + self._standing, wall)
        self._write(RESULT_FILE, doc)
        return doc

    def stop(self) -> None:
        """Ask a running forecast to stop; what it had is dropped. Terminal: a stopped
        cycle still monitors and starts no forecast again, so a stop that lands while one
        is being started is not lost."""
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for a background forecast to end; whether none is running now. A stopped
        forecast on several workers ends with the replications already running."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        refitted = self._refit is None or self._refit.join(timeout)
        return (thread is None or not thread.is_alive()) and refitted

    def _no_set(self) -> None:
        """No recommendation is made from a forecast that was not (D302)."""
        if self._advisor is not None:
            self._advisor.none()

    # ------------------------------------------------------------ the forecast
    def _begin(self, mono: float, seen: dict) -> None:
        if self._stop.is_set():
            return
        self._started, self._standing, self.failure = mono, [], None
        try:
            model, doc, found = self.source.start(self.horizon)
        except Exception as exc:            # as `observe`: the class only
            model, doc, found = None, None, [problem(
                "E_TWIN_STATE", f"no start could be made from the plant's state "
                f"({type(exc).__name__})", "/initial_state",
                hint="see `naga state check` and the coverage table of the state "
                     "documentation")]
        self._standing = list(found)
        if model is None:
            refusal = [p.code for p in found if p.severity == "error"] or ["E_TWIN_STATE"]
            self._status = ("refused", refusal[0])
            self._no_set()
            return
        # Successive forecasts are independent samples: replications run on seed, seed + 1
        # ..., so each forecast starts where the one before it ended. The seed used is in
        # the record, which is what reproduces a forecast (D228).
        seed = self.seed + self._forecasts * self.replications
        if self._layer is not None:
            try:
                model = refits.apply(model, self._layer["overrides"])
            except Exception as exc:        # validated at load: the model's own refusal
                self._status = ("refused", "E_TWIN_REFIT")
                self._standing.append(problem(
                    "E_TWIN_REFIT", f"the adopted refit does not apply to the started model "
                    f"({type(exc).__name__}): no forecast is made with it", "/refit",
                    hint="end it with an \"ended\" reply, or start the twin with its model"))
                self._no_set()
                return
        self._forecasts += 1
        # A forecast of the layered model is of another model (D741): its score is its own.
        pending = {"state": doc["hash"],
                   "model": refits.identity(doc["model"]["hash"], self._layer), "seed": seed,
                   "from": initial_state.clock_of(model), "stamp": seen.get("stamp"),
                   "horizon": self.horizon, "replications": self.replications,
                   "approximate": bool(getattr(self.source, "approximate", False))}
        # A running total is compared as its increase since this start (D290): what it
        # showed now, and how often it had lost count by now.
        base = {series: [float(seen["observed"][series]), marker]
                for series, marker in (seen.get("cumulative") or {}).items()
                if _num((seen.get("observed") or {}).get(series)) is not None}
        if base:
            pending["base"] = base
        if self._layer is not None:
            pending["refit"] = self._layer["id"]
        # What the candidates need beside the forecast: the state as a document, and the
        # actions already taken, which the plant has and the model must be told (D307).
        extra = {"doc": doc, "standing": [], "cursors": seen.get("cursors")}
        if self._advisor is not None:
            model, extra["standing"] = self._advisor.apply(model)
            if extra["standing"]:
                pending["actions"] = [action["id"] for action in extra["standing"]]
        if self._refit is not None:
            # The start a refit's window may begin at, and the candidate scored beside it.
            self._refit.hold(pending, model)
            extra["shadow"] = self._refit.shadow_model(model, pending.get("refit"))
        if self._background:
            self._status = ("running", None)
            self._thread = threading.Thread(target=self._run, args=(pending, model, extra),
                                            daemon=True, name="naga-twin-forecast")
            self._thread.start()
        else:
            self._run(pending, model, extra)

    def _run(self, pending: dict, model, extra: dict) -> None:
        began = self._monotonic()
        try:
            result = self._runner(model, horizon=self.horizon, replications=self.replications,
                                  seed=pending["seed"], workers=self.workers,
                                  should_stop=self._stop.is_set)
        except Exception as exc:
            result = exc
        took = round(self._monotonic() - began, 3)      # the forecast's own
        compared = shadowed = None
        if extra.get("shadow") is not None and not isinstance(result, Exception):
            # The refit's candidate, on this forecast's seed and in its thread (D740).
            try:
                shadowed = self._runner(extra["shadow"], horizon=self.horizon,
                                        replications=self.replications, seed=pending["seed"],
                                        workers=self.workers, should_stop=self._stop.is_set)
            except Exception as exc:
                shadowed = exc
        if self._advisor is not None and self._advisor.recommends \
                and not isinstance(result, Exception):
            # The candidates, on this forecast's seed and in its thread (D301).
            try:
                compared = self._advisor.compare(
                    model, pending, result, runner=self._runner, should_stop=self._stop.is_set,
                    workers=self.workers, standing=extra["standing"])
            except Exception as exc:
                compared = exc
        # How stale its start was when it could first be read: for whoever scores it (7.14).
        pending = {**pending, "finished": iso(self._clock()), "took": took}
        # One reference: read by `_collect`.
        self._done = (pending, model, result, {**extra, "compared": compared,
                                                "shadowed": shadowed})

    def _collect(self) -> list:
        """Take in a forecast that ended since the last step; its one-time problems."""
        if self._thread is not None and not self._thread.is_alive():
            self._thread = None
        if self._done is None or self._thread is not None:
            return []
        (pending, model, result, extra), self._done = self._done, None
        if self._stop.is_set():             # cancelled: half a forecast is not one
            self._status = ("none", None) if self._status[0] == "running" else self._status
            return []
        if isinstance(result, Exception):
            # The class only: the text may be whatever a model or a library put in it.
            self._status, self.failure = ("failed", "E_TWIN_FORECAST"), result
            self._standing.append(problem(
                "E_TWIN_FORECAST", f"the forecast failed ({type(result).__name__})",
                "/forecast", hint="the last good forecast is kept; run the model from this "
                                  "state with `naga run --state` to see why"))
            self._no_set()
            return []
        try:
            record, cut = _record(pending, result)
            record["score"] = score.of(record)      # nothing yet: its points lie ahead
            advise = getattr(self.source, "advise", None)
            self._advice = advise(result, model) if advise is not None else None
        except Exception as exc:
            self._status, self.failure = ("failed", "E_TWIN_FORECAST"), exc
            self._standing.append(problem(
                "E_TWIN_FORECAST", f"the forecast could not be read ({type(exc).__name__})",
                "/forecast"))
            self._no_set()
            return []
        self.forecast, self._status = result, ("ok", None)
        self._history = (self._history + [record])[-LIMITS["forecasts"]:]
        self._save_history()
        if self._refit is not None:
            self._refit.shadowed(record, extra.get("shadowed"))
        if self._advisor is not None and self._advisor.recommends:
            try:
                self._advisor.take(pending, extra["doc"], model, extra["compared"],
                                   standing=extra["standing"], cursors=extra["cursors"])
            except Exception as exc:        # the forecast stands; its set does not
                self._advisor.none("failed")
                self._standing.append(problem(
                    "W_TWIN_RECOMMEND", f"the recommendations could not be made "
                    f"({type(exc).__name__})", "/recommend"))
        return [problem("W_TWIN_CUT", f"the forecast holds more than {LIMITS['kpis']} KPIs "
                        f"or {LIMITS['series']} compared series: the rest is not in the result",
                        "/forecast")] if cut else []

    def _save_history(self) -> None:
        # The forecasts and the score that sums them, in one write: neither is ever ahead
        # of the other on disk.
        doc = {"format": FORMAT, "kind": KIND + "-history", "forecasts": self._history,
               "scores": self._scores.document(), "alerts": self._cusum.document()}
        if self._layer is not None:
            doc["refit"] = self._layer      # who adopted it and when: here, in no result
        self._write(HISTORY_FILE, doc)

    # ------------------------------------------------------------ refit (D740, D741)
    def _refit_turn(self, seen: dict, wall: float) -> list:
        """Fill the candidate's points, then judge it, or start a refit on a held alert."""
        if self._stop.is_set():
            return []
        self._refit.fill(seen)
        try:
            fields = self._refit.step(
                history=self._history, alerts=self._cusum.alerts(), k=self._cusum.k,
                h=self._cusum.h, now=seen.get("at"), layer=self._layer,
                open_proposal=self._refit.waiting(self._advisor.status_of),
                background=self._background, should_stop=self._stop.is_set,
                workers=self.workers)
        except Exception as exc:            # as the advisor's: the class only
            return [problem("W_TWIN_REFIT", f"the refit could not be stepped "
                                            f"({type(exc).__name__})", "/refit")]
        if fields is None:
            return []
        from nagasim.live import stamp

        made = stamp(seen.get("stamp")) if isinstance(seen.get("stamp"), str) else None
        ident = self._advisor.propose_refit(fields, wall if made is None else made)
        if ident is None:
            return [problem("W_TWIN_REFIT", "the refit's proposal is no record of format 1: "
                                            "it is not proposed", "/refit")]
        self._refit.named(ident)
        return []

    def _follow(self, wall: float) -> list:
        """The layer follows the ledger: a `taken` reply to a refit adopts it, an `ended`
        reply (or a later refit taken) ends it. Each change starts the score and the
        alerts again: they are of another model now (D741)."""
        want, have = self._advisor.adopted(), self._layer
        if (want or {}).get("id") == (have or {}).get("id"):
            return []
        said = []
        if want is not None:
            why = self._refit.plan.check(want["overrides"])
            if why is not None:
                if want["id"] not in self._refused:
                    self._refused.add(want["id"])
                    said.append(problem(
                        "W_TWIN_REFIT", f"the refit {want['id'][:12]} a reply took is not "
                        f"applied: {why}", "/refit",
                        hint="the --refit file decides what a refit may set; reply \"ended\" "
                             "to it"))
                want = None
                if have is None:
                    return said
        self._layer = None if want is None else dict(want)
        self._refit.reset("the model changed: a refit was adopted or ended")
        since = iso(wall)
        self._scores = score.Totals(self._identity(), self.horizon, since)
        self._cusum = score.Cusum(self._identity(), self._cusum.k, self._cusum.h, since)
        self._save_history()
        return said

    # ------------------------------------------------------------ compare
    def _compare(self, seen: dict) -> None:
        """Each stated point whose time has come takes what is observed now, once (D269).
        A point the twin did not see within one spacing of its time is missed, not made up."""
        self._observes = self._observes or "observed" in seen
        observed, now = seen.get("observed") or {}, seen.get("at")
        if not observed or now is None:
            return
        changed = False
        for record in self._history:
            late, touched = record["horizon"] / LIMITS["times"], []
            for point in record["points"]:
                if point["observed"] is not None or point.get("missed") or point["at"] > now:
                    continue
                overdue = now - point["at"] > late
                value = None if overdue else _reading(record, point["series"], seen)
                if overdue:
                    point["missed"] = True
                elif value is None:
                    continue
                elif value is False:        # a count that cannot be compared: said apart
                    point["missed"] = point["lost"] = True
                else:
                    point["observed"], point["observed_at"] = value, now
                touched.append(point)
            if touched:
                # Scored once, here (D293): the record's own score, and the rolling one.
                changed = True
                if self._scores.takes(record):
                    was = record.get("score")
                    if not (isinstance(was, dict) and (was.get("points") or was.get("missed"))):
                        self._scores.count(record)
                    for point in touched:
                        self._scores.add(record, point)
                        if point["observed"] is not None:
                            self._cusum.add(point)
                record["score"] = score.of(record)
        if changed:
            self._save_history()

    def _rows(self) -> list:
        rows = []
        for record in reversed(self._history):
            for point in reversed(record["points"]):
                if point["observed"] is not None and len(rows) < LIMITS["comparison"]:
                    low, high = point["q05"], point["q95"]
                    rows.append({"state": record["state"], **{k: point[k] for k in (
                        "series", "at", "mean", "q05", "q95", "observed", "observed_at")},
                        "inside": None if low is None or high is None
                        else low <= point["observed"] <= high})
        return rows

    # ------------------------------------------------------------ the result
    def _document(self, seen: dict, problems: list, wall: float) -> dict:
        status, reason = self._status
        # A copy: `_compare` goes on writing into the history, and a result handed out (to
        # another thread, by the editor's worker) is not to change under its reader.
        forecast = copy.deepcopy(self._history[-1]) if self._history else {}
        forecast["status"] = status
        if reason:
            forecast["reason"] = reason
        if len(problems) > LIMITS["problems"]:
            problems = problems[:LIMITS["problems"] - 1] + [problem(
                "W_TWIN_CUT", f"{len(problems) - LIMITS['problems'] + 1} more problems are "
                              f"not in the result", "/problems")]
        doc = {"format": FORMAT, "kind": KIND, "cycle": self._n, "written": iso(wall),
               "started": self._born, "cadence": dict(self._cadence),
               # `time_unit` and `epoch` (what model time 0 stands for) turn `at` and a
               # point's time into an instant; a source that has none says null.
               "model": {"name": str(getattr(self.source, "name", "")),
                         "hash": getattr(self.source, "model_hash", None),
                         "time_unit": getattr(self.source, "time_unit", None),
                         "epoch": getattr(self.source, "epoch", None)},
               "at": seen.get("at"), "stamp": seen.get("stamp"),
               "sources": seen.get("sources") or [], "view": seen.get("view"),
               "problems": [p.as_dict() if isinstance(p, Problem) else p for p in problems],
               "forecast": forecast, "comparison": self._rows()}
        # Never rows without their basis, nor none without the reason (D294).
        rows = self._scores.rows()
        doc["scores"] = {"nominal": score.NOMINAL, "basis": self._scores.basis(), "rows": rows}
        if not rows:
            doc["scores"]["reason"] = "not_observed" if not self._observes \
                else "none_due" if self._history else "no_forecast"
        # Bounded by the series (two sides each, `score.MAX_SERIES`): never cut for size.
        doc["alerts"] = {"k": self._cusum.k, "h": self._cusum.h,
                         "basis": self._cusum.basis(), "rows": self._cusum.alerts()}
        if seen.get("mapped"):
            doc["mapped"] = seen["mapped"]
        if seen.get("readings"):
            doc["readings"] = seen["readings"]      # for the twin started next (D378)
        if self._advice is not None:
            doc["advice"] = self._advice
        if self._advisor is not None and self._advisor.recommends:
            doc["recommend"] = self._advisor.document()     # bounded by its own counts
        if self._refit is not None:
            status_of = self._advisor.status_of if self._advisor is not None else lambda _id: None
            layer = self._layer
            doc["refit"] = {**self._refit.document(status_of), "adopted": None if layer is None
                            else {"id": layer["id"], "overrides": layer["overrides"],
                                  "series": layer["series"], "since": layer["read"]}}
        # The size bound: a source's advice goes first, then its view. What is left is the
        # cycle's own and bounded by the counts above.
        for key in ("advice", "view"):
            try:
                if len(json.dumps(doc, allow_nan=False)) <= LIMITS["result_bytes"]:
                    break
            except (TypeError, ValueError):
                pass                        # not plain data: cut like what is too large
            if doc.pop(key, None) is not None:
                doc["problems"].append(problem(
                    "W_TWIN_CUT", f"the result was over {LIMITS['result_bytes']} bytes or "
                    f"held a value that is not plain data: \"{key}\" was left out",
                    f"/{key}").as_dict())
            if key == "view":
                doc["view"] = None
        return doc

    def _write(self, name: str, data: dict) -> None:
        if self.out_dir is None:
            return
        try:
            write_json(self.out_dir / name, data)
        except OSError as exc:              # logged where it was raised
            # In the next result that is written; the one it belongs to is lost with it.
            self._carried = [problem(
                "E_TWIN_OUTPUT", f"{name} could not be written at cycle {self._n} ({exc})",
                "/", hint="check the result directory: its rights, its free space, and that "
                          "no reader keeps the file open")]


def loop(step, *, every: float, max_cycles: int | None = None, wait=time.sleep,
         monotonic=time.monotonic) -> int:
    """Call *step* every *every* seconds until Ctrl+C or *max_cycles*; the steps made. A
    plain loop, not a daemon: one failing step is logged and the next one runs. *wait* is
    the sleep, and the seam no test of this ever sleeps through."""
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            started = monotonic()
            try:
                step()
            except Exception as exc:        # one bad cycle never ends the service
                log.warning("twin cycle %d failed: %s", cycles + 1, exc)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            wait(max(every - (monotonic() - started), 0.0))
    except KeyboardInterrupt:
        log.info("twin stopped after %d cycle(s)", cycles)
    return cycles


# ---------------------------------------------------------------- a plant as a source
class LogFeed:
    """A recorded plant log (`naga plant mock`, `naga watch --record`) answering "what
    changed since" like a `live.Feed`: the mock mode, with no connection. With *speed* the
    log plays at that many of its own seconds a second from the moment this is built; with
    none it is all there at once."""

    def __init__(self, observations, *, speed: float | None = None, monotonic=time.monotonic):
        timed, last = [], -math.inf
        for index, line in enumerate(o for o in observations if isinstance(o, dict)):
            stamps = [line.get(k) for k in ("source_time", "received")] if "key" in line else []
            # A hole keeps its place: it has the time of what came before it.
            last = next((float(v) for v in stamps if _num(v) is not None), last)
            timed.append((last, index, line))
        timed.sort(key=lambda entry: entry[:2])
        self._times = [entry[0] for entry in timed]
        self._lines = [entry[2] for entry in timed]
        self.sources = sorted({str(line.get("source")) for line in self._lines if "key" in line})
        self._first = next((t for t in self._times if t > -math.inf), 0.0)
        self._speed, self._monotonic, self._t0 = speed, monotonic, monotonic()

    def clock(self) -> float:
        """The instant the playback has reached, in the log's own seconds since 1970."""
        if self._speed is None:
            return self._times[-1] if self._times else 0.0
        return self._first + (self._monotonic() - self._t0) * self._speed

    def changes(self, source: str, cursor: str | None = None, limit: int = 1000) -> Changes:
        since = int(cursor) if isinstance(cursor, str) and cursor.isdigit() else 0
        until = bisect.bisect_right(self._times, self.clock())
        mine = [line for line in self._lines[since:until] if line.get("source") == source]
        events = tuple(line for line in mine if "key" in line)
        holes = [line for line in mine if "key" not in line]
        lost = sum(int(h["lost"]) for h in holes if _num(h.get("lost")) is not None)
        return Changes(events, str(until), lost, any(h.get("gap") for h in holes), "live", 0.0, ())

    def close(self) -> None:
        pass


class PlantSource:
    """A plant read through a feed and a plant mapping (7.8, 7.11): what the generic twin
    observes and starts from. The feed, with its connections, is the caller's.

    *clock* gives the present in seconds since 1970 (the `clock` of a `LogFeed` for a
    recording); *path* is the model file, watched only to say that it changed (D271).
    *max_age* (seconds, the caller's; none by default) is how long a source may have been
    silent before no forecast is started from what it last said (D276)."""

    def __init__(self, model, mapping, feed, *, name: str = "", sources=None, clock=time.time,
                 path=None, max_age: float | None = None):
        from nagasim import state       # late: `state` builds on the session

        self._state = state
        self.model, self.name = model, name
        self.model_hash = state.model_hash(model)
        self._clock, self._max_age = clock, max_age
        self._file = None if path is None else (Path(path), self._digest(path))
        self._refused = [p for p in plant.check(mapping, model) if p.severity == "error"]
        self._kind: str | None | bool = False       # False: not asked yet
        self._held: list = []
        self._at = 0.0
        signals = [] if self._refused else mapping.get("signals", [])
        # Counts are approximate where no item is named (D272): a queue length makes
        # anonymous members, and a mapping with no "at" or "seen" names nobody at all.
        kinds = {signal.get("as") for signal in signals}
        self.approximate = "queue" in kinds or not kinds & {"at", "seen"}
        # With no signal that says where an item is (counters alone), WIP is not told, and
        # not told is not zero: it is left out of what is observed and scored (#758).
        self._tells_wip = bool(kinds & {"queue", "at", "seen", "job"})
        # The blocks a counter tells the departures of (D288): what a forecast must sample.
        self._counted = sorted({signal["block"] for signal in signals
                                if signal.get("as") == "counter"})
        self.time_unit = self.epoch = None
        if self._refused:
            return
        # As the mapper reads them: the unit model time is in, and the instant of its 0.
        self.time_unit = mapping["time"].get("unit") or plant.layout_names(model)["unit"]
        self.epoch = mapping["time"]["epoch"]
        self._timeline = Timeline([], live=True)
        if sources is None:
            sources = getattr(feed, "sources", None) or None
        self._watch = plant.Watch(mapping, model, feed, sources)
        self.mapper = self._watch.mapper

    @staticmethod
    def _digest(path) -> str | None:
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return None

    def observe(self) -> dict:
        if self._refused:
            return {"ready": False, "problems": list(self._refused)}
        # A full pull means more is waiting: a busy plant is caught up with in this step, up
        # to a ceiling, so that one step always ends.
        events = self._watch.step(_PULL, LIMITS["pulls"], tolerant=True)
        problems, ready = list(self._watch.problems), bool(self._watch.cursors)
        for name, row in self._watch.rows.items():
            if row["state"] == "refused":
                ready = False
            elif row["state"] != "live" or row["age"] is None:
                ready = False
                problems.append(problem(
                    "W_TWIN_SOURCE", f"source '{name}' is down or has not answered yet: the "
                    f"view keeps its last picture and no forecast is started",
                    f"/data_sources/{name}", hint="the feed connects again by itself"))
            elif self._max_age is not None and row["age"] > self._max_age:
                ready = False
                problems.append(problem(
                    "W_TWIN_SOURCE", f"source '{name}' last spoke {row['age']:.0f} s ago, over "
                    f"the {self._max_age:g} s of --max-age: no forecast is started",
                    f"/data_sources/{name}", hint="the view keeps its last picture; a "
                                                  "forecast starts again when the source speaks"))
        self._timeline.append(events)
        # Kept until `take`: the newest only, for a caller that observes and never takes.
        self._held = (self._held + events)[-_HELD:]
        if self._file is not None and self._digest(self._file[0]) != self._file[1]:
            problems.append(problem(
                "W_TWIN_MODEL_CHANGED", "the model file changed on disk: this twin keeps the "
                "model it loaded", "/", hint="stop the twin and start it again to use the "
                                             "new model"))
        # The timeline's clock moves with events; the present is later when the plant is
        # quiet, and nothing has changed in between.
        told = self.mapper.now
        try:
            present = float(self._clock())
            self._at = max(told, self.mapper.at(present))
            stamp = iso(present) if self._at > told else self.mapper.stamp()
        except (OverflowError, OSError, ValueError):
            self._at, stamp = told, self.mapper.stamp()
        seen = self._timeline.state_at(told)
        counts, moving = seen.get("counts") or {}, seen.get("in_transit") or {}
        wip = sum(counts.values()) + len(moving)
        resources = resource_rows(seen)

        def cut(table: dict) -> dict:
            return dict(sorted(table.items())[:LIMITS["names"]])
        counters = self.mapper.counters()
        view = {"approximate": self.approximate, "wip": wip, "blocks": cut(counts),
                "in_service": cut(seen.get("in_service") or {}),
                "resources": resources[:LIMITS["names"]], "levels": cut(seen.get("levels") or {}),
                "counters": cut(counters)}
        observed = {**({"system/WIP": wip} if self._tells_wip else {}),
                    **{f"resource/{r['name']}/in_service": r["busy"] for r in resources},
                    **{f"level/{name}": value for name, value in (seen.get("levels") or {}).items()},
                    **{f"block/{name}/out": said["total"] for name, said in counters.items()}}
        report = self.mapper.report()
        if 0 < report["observations"] <= report["counts"].get("unread", 0):
            # Live, and not one key a signal reads: an empty view that is ignorance is not
            # the plant's state, so nothing is forecast from it (D270). A plant that is
            # read and merely idle (no event from its values) is forecast.
            ready = False
            problems.append(problem(
                "W_TWIN_SOURCE", f"{report['observations']} observations, none mapped (no "
                f"signal's key matches any of them): no forecast is started", "/signals",
                hint="check the mapping's keys against what the plant sends (naga watch)"))
        # The view keeps its last picture while a source is down, silent past the caller's
        # limit or unread; a picture is not a reading, and none is taken from it (#371).
        return {"at": self._at, "stamp": stamp, "ready": ready, "view": view,
                "observed": observed if ready else {},
                # A total is one mapper's: when it began to count and how often it lost
                # count. A forecast's start is compared with a reading under the same two.
                "cumulative": {f"block/{name}/out": [said["since"], said["resets"]]
                               for name, said in counters.items()},
                "sources": [dict(row) for row in self._watch.rows.values()],
                "cursors": dict(self._watch.cursors),     # what a record was computed from (D228)
                # Each counter's last raw value and when it was said: what a twin started
                # after this one continues the count from (D378).
                "readings": cut(self.mapper.readings()),
                "mapped": {key: report[key] for key in ("observations", "events", "entities",
                                                        "counts")},
                "problems": problems + self.mapper.problems()}

    def carry(self, readings, max_age: float) -> None:
        """Go on from the counters' readings a twin before this one left (D378): before
        the first `observe`. Each entry is judged by the mapper (`plant.Mapper.carry`)."""
        if not self._refused:
            self.mapper.carry(readings, max_age)

    def take(self) -> list:
        """The trace events mapped since this was last called, whoever called `observe` in
        between (D279): what a view in this process feeds its own timeline with."""
        held, self._held = self._held, []
        return held

    def start(self, horizon: float) -> tuple:
        """(the model with the plant's state of now in place, its state document, the
        problems), or (None, ...) with the refusal: never a start from nothing (D270). The
        run it is for ends *horizon* after now, whatever length the model file gives."""
        state = self._state
        if self._kind is False:
            self._kind = state.refused_kind(self.model)
        if self._kind:
            return None, None, [problem(
                "E_TWIN_STATE", f"this model cannot be started from plant state yet: "
                f"{self._kind}", "/initial_state",
                hint="the twin goes on monitoring; see the coverage table of the state "
                     "documentation")]
        seen = dict(self.mapper.snapshot(), at=self._at)
        doc, found = state.from_plant(self.model, seen, name=self.name or None)
        if any(str(p.path).startswith("/initial_state/fleets/") for p in found):
            # D388: a fleet the mapping does not observe would start as declared under a
            # plant that says otherwise. Refused by name, as before; the rest is carried.
            return None, None, found + [problem(
                "E_TWIN_STATE", "this model cannot be started from plant state yet: fleets "
                "and vehicles the plant mapping does not observe", "/initial_state",
                hint="map a \"vehicle\" signal for every vehicle of the fleet; the twin goes "
                     "on monitoring")]
        model, refused = state.start(self.model, doc, until=self._at + float(horizon))
        if model is not None:
            # A forecast has no warm-up (it would discard the state it starts from); the
            # model's own setting would only be warned about, once a replication.
            model["simulation"]["warmup"] = 0
            if self._counted:
                model["simulation"]["timeseries_counts"] = list(self._counted)
        return model, doc, found + refused


# ---------------------------------------------------------------- the CLI
def _show(value, limit: int = 300) -> str:
    """Text out of a result file for a terminal: it is data, so nothing in it may be an
    escape sequence."""
    return "".join(ch if ch.isprintable() else "?" for ch in str(value))[:limit]


def _summary(doc: dict) -> list[str]:
    """A result in a few lines, in the monitor's one wording (D281). Read defensively: the
    file is only known to be format 1."""
    from nagasim import monitor

    def table(value):
        return value if isinstance(value, dict) else {}
    shown = monitor.summary(doc)
    model, view = table(doc.get("model")), table(doc.get("view"))
    lines = [f"cycle {_show(doc.get('cycle'))} written {_show(doc.get('written'))}: model "
             f"'{_show(model.get('name'))}' ({_show(model.get('hash'), 12)}), plant at t = "
             f"{_show(doc.get('at'))} ({_show(doc.get('stamp'))})"]
    lines += [f"source {_show(name)}: {_show(state)}, age {_show(age)} s, {_show(lost)} lost, "
              f"{_show(gaps)} gaps" for name, state, age, lost, gaps in shown["sources"]]
    if view:
        lines.append(f"view: {_show(view.get('wip'))} in the system"
                     + (" (approximate counts)" if view.get("approximate") else "")
                     + f", {len(table(view.get('blocks')))} blocks holding items, "
                       f"{len(view.get('resources') or [])} resources")
    lines += [_show(note, 1000) for note in shown["notes"]]
    lines += [_show(said, 1000) for said in shown["forecast"]["lines"]]
    if shown["counters"]:
        lines.append("counted by the plant: " + ", ".join(
            f"{_show(name, 80)} {total:g}" + (f" ({resets:g} resets)" if resets else "")
            for name, total, _since, resets in shown["counters"] if total is not None))
    if shown["alerts"]:                 # the alerting sides, then the basis
        lines.append("alerts: " + _show(shown["alerts"]["lines"][0], 1000))
        lines += ["  " + _show(line, 1000) for line in shown["alerts"]["lines"][1:-1]]
    lines.append("scores: " + _show(shown["scores"]["lines"][0]))
    lines += [f"  {_show(series, 80)}, lead {lead:g}: {points:g} readings"
              + (f", coverage {coverage}, bias {bias:+.3g}, abs. error {error:.3g}, pinball "
                 f"{pinball:.3g}" if points else "") + (f", {missed:g} missed" if missed else "")
              + (f", {lost:g} not compared" if lost else "")
              for series, lead, points, missed, lost, coverage, bias, error, pinball
              in shown["scores"]["rows"] if None not in (lead, points, missed, lost)]
    if len(shown["scores"]["lines"]) > 1:
        lines += [_show(line, 1000) for line in shown["scores"]["lines"][1:]]
    if shown["refit"]:
        lines.append("refit: " + _show(shown["refit"][0], 1000))
        lines += ["  " + _show(line, 1000) for line in shown["refit"][1:]]
    if shown["recommend"]:
        # Plainest form (D358): what stands, the recommendation and the outbox first; the
        # rows; then the basis, indented. Nothing is hidden behind a flag.
        lead = shown["recommend"]["lead"]
        lines.append("recommend: " + _show(lead[0], 1000))
        lines += ["  " + _show(line, 1000) for line in lead[1:]]
        lines += [f"  {'' if rank is None else f'{rank:g}. '}{_show(action, 120)}: {_show(verdict)}"
                  + ("" if effect is None else f", effect {effect:+.4g}"
                     + ("" if None in (low, high) else f" [{low:.4g}, {high:.4g}]")
                     + ("" if confidence is None else f", confidence {confidence:.2f}"))
                  + (f", {_show(status)}" if status else "") + (f" ({_show(ident)})" if ident else "")
                  for rank, action, effect, low, high, confidence, verdict, status, ident
                  in shown["recommend"]["rows"]]
        fold = shown["recommend"]["fold"]       # the KPI line, then effects; ADVICE is the page's
        lines += ["  basis: " + _show(fold[0], 1000)] + ["    " + _show(line, 1000) for line in fold[2:]]
    return lines + [f"{_show(code)}: {_show(message)}" for _severity, code, message, _hint in shown["problems"]]


def _failed(doc: dict) -> bool:
    return any(isinstance(p, dict) and p.get("severity") == "error"
               for p in doc.get("problems") or [])


def _limit(floor):
    """An argparse type: a finite number, at least *floor* (None: over 0)."""
    def parse(text) -> float:
        import argparse

        try:
            value = float(text)
        except ValueError:
            value = math.nan
        if not math.isfinite(value) or (value <= 0 if floor is None else value < floor):
            raise argparse.ArgumentTypeError(
                f"'{text}' is not a finite number " + ("over 0" if floor is None
                                                       else f"of at least {floor:g}"))
        return value
    return parse


def _cadence(every: str, forecast_every: str) -> tuple:
    """(seconds between steps, seconds between forecasts): never under the floor, and a
    forecast never more often than a step."""
    step = max(parse_interval(every), LIMITS["every"])
    return step, max(parse_interval(forecast_every), step)


def main(argv=None, *, wait=time.sleep) -> int:
    """`naga twin run | once | status | score | reproduce`. *wait* is the loop's sleep (a
    test's seam)."""
    import argparse

    from nagasim import api
    from nagasim._cli import add_model_argument, setup_cli
    from nagasim.live import Feed

    setup_cli()
    parser = argparse.ArgumentParser(
        prog="naga twin",
        description="An operational twin of a plant: monitor it through a plant mapping, "
                    "forecast from its state of now, compare each forecast with what then "
                    "happened, score them, and write one result a monitor reads. With "
                    "--candidates it compares your candidate actions after each forecast "
                    "and recommends. Read-only towards the plant: nothing is sent to it; "
                    "only the --out directory is written and, with --outbox, the file you "
                    "name there.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    once_ = verbs.add_parser("once", help="one cycle with its forecast, for the OS scheduler; "
                                         "exit 1 when the result holds an error")
    run_ = verbs.add_parser("run", help="keep monitoring, with a forecast on its own slower "
                                       "cadence, until Ctrl+C")
    status_ = verbs.add_parser("status", help="print the latest result of a twin; exit 1 for "
                                             "an error in it or no result")
    status_.add_argument("directory", help="the --out directory of a twin")
    score_ = verbs.add_parser("score", help="compute a twin's stored scores again from a "
                                           "recorded plant log; exit 1 for any difference. "
                                           "Nothing is written")
    score_.add_argument("directory", help="the --out directory of a twin")
    add_model_argument(score_, help="path to the model JSON the twin ran on, or a bundled model's name (naga examples)")
    score_.add_argument("--map", required=True, metavar="FILE", help="its plant mapping")
    score_.add_argument("--log", required=True, metavar="FILE",
                        help="the plant log of the same period (naga watch --record, or the "
                             "log the twin played)")
    again_ = verbs.add_parser("reproduce", help="compute a twin's stored recommendations "
                                                "again from their stored states and seeds; "
                                                "exit 1 for any difference. Nothing is written")
    again_.add_argument("directory", help="the --out directory of a twin")
    add_model_argument(again_, help="path to the model JSON the twin ran on, or a bundled model's name (naga examples)")
    again_.add_argument("--id", action="append", default=None, metavar="ID",
                        help="only this recommendation (its id, or the first characters of "
                             "it); may repeat (default: every one whose state is kept)")
    again_.add_argument("--workers", type=int, default=1)
    for verb in (once_, run_, again_):
        verb.add_argument("--setpoints", default=None, metavar="FILE",
                          help="your setpoints file (format 1, kind naga-setpoints): the tags a "
                               "\"setpoint\" candidate may name, each with the model path that "
                               "simulates it and its range. Only naga plc-write writes one to a "
                               "PLC, and only after a person's reply")
    for verb in (once_, run_):
        add_model_argument(verb)
        verb.add_argument("--map", required=True, metavar="FILE",
                          help="the plant mapping (*.plantmap.json; see naga plant init)")
        source = verb.add_mutually_exclusive_group(required=True)
        source.add_argument("--connections", metavar="FILE",
                            help="your connections file: the model's live sources are read "
                                 "over the connections it defines, and over nothing else")
        source.add_argument("--log", metavar="FILE",
                            help="a recorded plant log instead of a plant (naga plant mock, "
                                 "naga watch --record): no connection is opened")
        verb.add_argument("--source", action="append", default=None, metavar="NAME",
                          help="only this live source; may repeat (default: every one)")
        verb.add_argument("--out", required=True, metavar="DIR",
                          help="the result directory: twin.json and twin_history.json (and "
                               "monitor.html with --monitor)")
        verb.add_argument("--monitor", action="store_true",
                          help="also write DIR/monitor.html, a page to open in a browser that "
                               "reloads itself; the model's layout (names and places) is in it")
        verb.add_argument("--candidates", default=None, metavar="FILE",
                          help="your candidate actions (format 1, kind naga-candidates): each "
                               "is compared with doing nothing after every forecast, on one "
                               "KPI; at most 20, each one more forecast")
        verb.add_argument("--refit", default=None, metavar="FILE",
                          help="your refit file (format 1, kind naga-refit): the factors and "
                               "bounds a held alert may refit, the calibration budget and the "
                               "readings a candidate is scored on. Only a better-scoring "
                               "candidate is proposed, through --outbox, and only a \"taken\" "
                               "reply adopts it; the model file is never written. Needs --outbox")
        verb.add_argument("--outbox", default=None, metavar="SPEC",
                          help="with --candidates or --refit: where each recommendation or refit "
                               "is also written "
                               "as a record, file:<path> (one JSON line each, appended; "
                               "replies are read from <path>.replies) or mqtt:<connection> "
                               "(a connection of --connections with \"use\": \"outbox\"; "
                               "<root>/proposals/<id>, replies on <root>/replies). A "
                               "queue of yours: nothing here acts on a plant. Default: "
                               "nothing is written "
                               "outside --out")
        verb.add_argument("--horizon", type=float, required=True, metavar="T",
                          help="how far each forecast looks, in the model's base time unit")
        verb.add_argument("--replications", type=int, default=50,
                          help=f"of each forecast (default 50, at most {LIMITS['replications']})")
        verb.add_argument("--seed", type=int, default=0)
        verb.add_argument("--workers", type=int, default=1)
        verb.add_argument("--max-age", type=parse_interval, default=None, metavar="INTERVAL",
                          help="start no forecast while a source has been silent for longer "
                               "than this (30s, 15m, 2h; default: no limit)")
        verb.add_argument("--for", dest="seconds", type=float, default=5.0, metavar="SECONDS",
                          help="how long to listen to live sources before the first cycle, so "
                               "that it starts from the whole plant (default 5)")
        verb.add_argument("--alert-k", type=_limit(0.0), default=score.K, metavar="K",
                          help="the alerts' allowance, in standard deviations of the band: a "
                               "shift smaller than this is not summed (default 0.5)")
        verb.add_argument("--alert-h", type=_limit(None), default=score.H, metavar="H",
                          help="the alerts' limit: a series alerts while its sum of "
                               "standardised residuals beyond K is over this (default 4)")
    run_.add_argument("--every", default="5s", metavar="INTERVAL",
                      help="between two cycles (default 5s, at least 1s)")
    run_.add_argument("--forecast-every", default="5m", metavar="INTERVAL",
                      help="between two forecasts (default 5m); one still running is not "
                           "started again")
    run_.add_argument("--speed", type=float, default=1.0, metavar="X",
                      help="with --log: seconds of the log played each second (default 1)")
    run_.add_argument("--max-cycles", type=int, default=None, metavar="N",
                      help="stop after this many cycles")
    args = parser.parse_args(argv)

    if args.verb == "status":
        doc, problems = read_result(args.directory)
        for found in problems:
            print(f"{found.code}: {found.message}")
        if doc is None:
            return 1
        print("\n".join(_summary(doc)))
        return int(_failed(doc))

    layout = api.load_layout(Path(args.model))
    setpoints = None
    if getattr(args, "setpoints", None):
        from nagasim import setpoints as setpoints_file    # the reader, not the writer (D811)

        try:            # the flag's file, and only the flag's (D742)
            setpoints = setpoints_file.load(args.setpoints)["tags"]
        except setpoints_file.Refused as exc:
            print(f"{exc.code}: {exc.message}" + (f" ({exc.hint})" if exc.hint else ""))
            return 1
    if args.verb == "reproduce":
        from nagasim import recommend

        lines, differences = recommend.reproduce(args.directory, layout, ids=args.id,
                                                 workers=args.workers, setpoints=setpoints)
        print("\n".join(_show(line, 400) for line in lines))
        return int(bool(differences))
    mapping, problems = plant.load(args.map)
    observations: list = []
    if not problems and args.log:
        observations, problems = plant.read_log(args.log)
    for found in problems:
        print(f"{found.code}: {found.message}")
    if any(found.severity == "error" for found in problems):
        return 1
    if args.verb == "score":
        lines, differences = score.recompute(args.directory, layout, mapping, observations)
        print("\n".join(_show(line, 400) for line in lines))
        return int(bool(differences))
    asked = sink = plan = None
    if args.outbox and not (args.candidates or args.refit):
        print("--outbox needs --candidates: there is nothing to recommend without them")
        return 1
    if args.refit and not args.outbox:
        print("--refit needs --outbox: a refit is adopted only by a person's reply to its proposal")
        return 1
    if args.refit and not args.alert_k > 0:
        print("--refit needs --alert-k over 0: a refit's window is ceil(h/k) readings")
        return 1
    connections = None if args.log else netguard.load_connections(args.connections)
    if args.candidates or args.refit:
        from nagasim import outbox, recommend

        try:
            # The refit file is the flag's, and only the flag's: none is looked for (D740).
            plan = refits.load(args.refit, layout) if args.refit else None
            asked = recommend.load(args.candidates, layout, setpoints) if args.candidates \
                else None
            if args.log and str(args.outbox or "").startswith("mqtt:"):
                raise outbox.SinkError("an mqtt outbox needs --connections",
                                       "it names a connection of your connections file")
            sink = outbox.open_sink(args.outbox, connections) if args.outbox else None
        except (recommend.Refused, refits.Refused, outbox.SinkError) as exc:
            print(f"{exc.code}: {exc.message}" + (f" ({exc.hint})" if exc.hint else ""))
            return 1
    running = args.verb == "run"
    every, forecast_every = _cadence(args.every, args.forecast_every) if running else (0.0, 0.0)
    if args.log:
        speed = max(args.speed, 0.0) if running else None
        feed = LogFeed(observations, speed=speed)
        names, clock = None, feed.clock
    else:
        specs = [s for s in layout.get("data_sources", []) if s.get("kind") in readers.KINDS
                 and (not args.source or s.get("name") in args.source)]
        if not specs:
            print("no live data source to read")
            return 1
        for name in sorted({s.get("connection") for s in specs} & set(connections)):
            print(netguard.describe_connection(connections[name]))
        feed = Feed(specs, network=netguard.NetworkPolicy(connections=connections))
        names, clock = [s["name"] for s in specs], time.time
    try:
        source = PlantSource(layout, mapping, feed, name=Path(args.model).stem, sources=names,
                             clock=clock, path=args.model, max_age=args.max_age)
        advisor = None
        if asked is not None or plan is not None:
            advisor = recommend.Advisor(
                asked, model=layout, horizon=args.horizon, name=Path(args.model).stem,
                mapping=mapping, out_dir=args.out, sink=sink, setpoints=setpoints,
                lifetime=recommend.lifetime(asked or {}, args.horizon, source.time_unit))
        if asked is not None:
            print(f"recommending on {_show(advisor.kpi)} ({advisor.better} is better), "
                  f"{len(advisor.actions)} candidates a forecast" + (
                      "" if advisor.series else "; the effect of an action taken cannot be "
                      "measured on this KPI (only timeseries/block/<name>/out/max of a "
                      "counted block can)"))
        if plan is not None:
            print(f"refit: {len(plan.factors)} factors on an alert held for "
                  f"{math.ceil(args.alert_h / args.alert_k)} readings, a candidate scored on "
                  f"{plan.readings} readings and proposed only when better")
        if advisor is not None:
            if sink is not None and sink.kind == "mqtt":
                print(netguard.describe_connection(sink.conn))
                print(f"outbox: records are published to {sink.topic('<id>')} (QoS 1, not "
                      f"retained); replies are read from {sink.root}/replies")
            elif sink is not None:
                print(f"outbox: records are appended to {sink.path}; replies are read from "
                      f"{sink.replies.name} beside it")
        cycle = Cycle(source, horizon=args.horizon, replications=args.replications,
                      seed=args.seed, workers=args.workers, out_dir=args.out,
                      forecast_every=forecast_every, every=every if running else None,
                      background=running, advisor=advisor, alert_k=args.alert_k,
                      alert_h=args.alert_h,
                      refit=None if plan is None else refits.Refitter(plan))
        page = None
        if args.monitor:
            from nagasim import monitor

            page = monitor.Page(layout, Path(args.out) / monitor.PAGE_FILE,
                                every=every if running else None, setpoints=setpoints)
            print(f"monitor page: {page.path} (it reloads every {page.refresh} s)")

        def shown() -> dict:
            """One step, and its page: of the same step, written after the result."""
            doc = cycle.step()
            if page is not None and (said := page.write(doc, cycle.events)):
                print(said)                 # a refused write, once, and once that it ended
            return doc

        if not args.log:
            # A subscription says what it holds over a moment: the first state must not be
            # taken from the first value that happens to arrive.
            source.observe()
            deadline = time.monotonic() + max(0.0, min(args.seconds, 3600.0))
            while (left := deadline - time.monotonic()) > 0:
                feed.wait(left)
        if not running:
            doc = shown()
            print("\n".join(_summary(doc)))
            return int(_failed(doc))

        def step() -> None:
            doc = shown()
            view, forecast = doc.get("view") or {}, doc["forecast"]
            print(f"cycle {doc['cycle']}: t = {doc['at']}, {view.get('wip', '-')} in the "
                  f"system, forecast {forecast['status']}, {len(doc['problems'])} problems")

        made = loop(step, every=every, max_cycles=args.max_cycles, wait=wait)
        cycle.stop()                        # a forecast still running is cancelled
        if not cycle.join(10.0):
            # A pool hears the stop between replications only: said, not waited for in
            # silence (the interpreter cannot leave before its workers do).
            print(f"a forecast is still finishing on {args.workers} workers; the command "
                  f"exits when it ends")
        print(f"{made} cycle(s); the result is in {args.out}")
        return 0
    finally:
        feed.close()
        if sink is not None and sink.kind == "mqtt":
            sink.close()


if __name__ == "__main__":
    raise SystemExit(main())
