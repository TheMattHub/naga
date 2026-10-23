"""Twin refit on approval (Phase 12 C5, D740, D741): when a twin's alert has held long
enough, calibrate the caller's factors on the readings under it, prove the candidate out
of sample against the current model, and propose it through the outbox. Only a person's
`taken` reply adopts it; an `ended` reply reverts it.

    plan = refit.load("line.refit.json", model)         # the caller's --refit file only
    cycle = twin.Cycle(source, ..., advisor=advisor, refit=refit.Refitter(plan))

* THE FILE is the caller's, named by `naga twin --refit` and by nothing else: no model,
  mapping, folder or tool argument names one (D740). It holds the factors with their
  bounds, the calibration budget, replications a point, the readings the candidate is
  scored on and, optionally, the series whose alerts may start a refit. It is data: every
  factor must address a number of the model, and nothing in it selects code or a path.
* THE WINDOW. An alert on series S since t starts a refit once the first forecast started
  at or after t (its start R) has at least ceil(h/k) readings of S within its horizon: a
  shorter window chases noise. A counter's readings are taken as increases since R's
  start, as the forecast states them. Fewer readings: it waits; the alert ends: nothing.
* THE CANDIDATE is `calibrate.calibrate` from R's started model on the window
  (`SeriesTarget`), over the file's factors and bounds only.
* THE PROOF is out of sample. Each forecast after that runs the candidate too, on the same
  seed (common random numbers); once `readings` points of S are filled, both bands are
  scored by `score.point` and the candidate is proposed only when its mean pinball loss is
  lower. Worse or equal: it is said why, and nothing is proposed.
* ADOPTION is the twin's (D741): the overrides become a layer kept in its history (who,
  when), applied to every forecast from the next cycle, and the score and the alerts
  start again under a model identity of their own. The model file is never written.
* ONE REFIT AT A TIME: none starts while one calibrates or is scored, or while a
  proposal is unanswered; one alert (series, side, since) is tried once.

The in-flight refit is this process's only: a twin started again begins with none (the
proposal stays in the ledger, the adopted layer in the history). Pandas and the engine
are loaded only when a calibration runs.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import threading
from dataclasses import dataclass

from nagasim import netguard
from nagasim.core.utils import canonical_json, is_finite_number, sha256_text

__all__ = ["FORMAT", "KIND", "LIMITS", "Refused", "Plan", "load", "identity", "apply",
           "calibrate_window", "Refitter", "FORBIDDEN", "forbidden"]

FORMAT = 1
KIND = "naga-refit"
LIMITS = {
    "file_bytes": 64 * 2 ** 10,     # the refit file
    "factors": 8,                   # of one file
    "path": 400,                    # characters of a factor's path
    "budget": 2000,                 # replications of one calibration
    "replications": 20,             # a calibration point
    "readings": 300,                # the candidate is scored on
    "series": 50,                   # a file may name
    "held": 12,                     # forecast starts kept in memory for a window
    "shadows": 50,                  # candidate forecasts of one refit
}
_KEYS = {"format", "kind", "factors", "budget", "replications", "readings", "series"}
# A factor (or a setpoint's model path, #981) may not reach what a twin's start sets (the
# clock, the run length, the state) nor where data comes from: those are the plant's and
# the caller's, never a refit's or a PLC tag's.
FORBIDDEN = ("initial_state.", "simulation.", "data_sources.")


def forbidden(path: str) -> bool:
    """Whether *path* reaches the state, the run settings or the data sources."""
    return path.startswith(FORBIDDEN)
_COMPARED = re.compile(r"^(system/WIP|resource/.+/in_service|level/.+|block/.+/out)$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_LAYER_KEYS = {"id", "overrides", "series", "by", "at", "read"}
_FINISHED = ("proposed", "not_better", "unscored", "failed", "dropped")


class Refused(Exception):
    """A coded refusal of the caller's refit file (`E_REFIT`), before anything runs."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = "E_REFIT", message, hint


@dataclass(frozen=True)
class Plan:
    """What the caller's file allows: `factors` ({"path", "low", "high"} each), `budget`
    and `replications` of the calibration, `readings` to score on, `series` (None: any)."""

    factors: tuple
    budget: int
    replications: int
    readings: int
    series: tuple | None = None

    def check(self, overrides) -> str | None:
        """None when *overrides* sets this plan's factors only, each once and inside its
        bounds; else why, naming the path."""
        bounds = {f["path"]: (f["low"], f["high"]) for f in self.factors}
        if not (isinstance(overrides, list) and 0 < len(overrides) <= LIMITS["factors"]):
            return "it holds no list of overrides"
        seen = set()
        for override in overrides:
            if not (isinstance(override, dict) and set(override) == {"path", "value"}
                    and isinstance(override["path"], str)):
                return "an override is not {path, value}"
            path = override["path"][:LIMITS["path"]]
            if path not in bounds:
                return f"'{_shown(path)}' is not a factor of the refit file"
            if path in seen:
                return f"'{_shown(path)}' is set twice"
            seen.add(path)
            low, high = bounds[path]
            value = override["value"]
            if not (is_finite_number(value) and not isinstance(value, bool) and low <= value <= high):
                return f"'{_shown(path)}' is outside its bounds [{low:g}, {high:g}]"
        return None


def _shown(text) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in str(text))[:120]


def _whole(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _read(path) -> dict:
    """The JSON object in the caller's file: a local regular file under the bound, opened
    without following a link where the system can. Raises Refused."""
    text = str(path)
    if netguard.is_unc_path(text):
        raise Refused("the refit file is a UNC or device path",
                      "opening one is a network connection; use a local path")
    if ":" in os.path.splitdrive(text)[1]:
        raise Refused("the refit file's path holds a ':' after its drive",
                      "that names a stream inside another file, not a file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) \
        | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(text, flags)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise Refused("the refit file is not a plain file")
            if info.st_size > LIMITS["file_bytes"]:
                raise Refused(f"the refit file is over {LIMITS['file_bytes']} bytes")
            doc = json.loads(handle.read(LIMITS["file_bytes"] + 1))
    except (OSError, ValueError, RecursionError) as exc:
        raise Refused(f"the refit file cannot be read ({type(exc).__name__})") from None
    if not (isinstance(doc, dict) and doc.get("format") == FORMAT and doc.get("kind") == KIND):
        raise Refused(f"this is not a refit file of format {FORMAT} (kind \"{KIND}\")")
    return doc


def load(path, model) -> Plan:
    """The caller's refit file, checked against *model*. Raises `Refused`, naming the key
    or the path."""
    from nagasim import experiments
    from nagasim.core.schema import load_layout

    doc = _read(path)
    unknown = sorted(set(doc) - _KEYS)
    if unknown:
        raise Refused(f"a refit file has no key '{_shown(unknown[0])[:40]}'",
                      "its keys: " + ", ".join(sorted(_KEYS)))
    listed = doc.get("factors")
    if not (isinstance(listed, list) and 0 < len(listed) <= LIMITS["factors"]):
        raise Refused(f"\"factors\" is a list of 1 to {LIMITS['factors']} factors")
    layout = load_layout(model)
    factors = []
    for factor in listed:
        if not (isinstance(factor, dict) and set(factor) == {"path", "low", "high"}):
            raise Refused("a factor is {\"path\", \"low\", \"high\"} and nothing more")
        path, low, high = factor["path"], factor["low"], factor["high"]
        if not (isinstance(path, str) and 0 < len(path) <= LIMITS["path"] and path.isprintable()):
            raise Refused("a factor's \"path\" is an override path of the model")
        if forbidden(path):
            raise Refused(f"factor '{_shown(path)}': a refit sets no state, run setting or "
                          f"data source", "name a parameter of a block, a resource or a variable")
        if any(f["path"] == path for f in factors):
            raise Refused(f"factor '{_shown(path)}' is listed twice")
        if not (all(is_finite_number(v) and not isinstance(v, bool) for v in (low, high))
                and low < high):
            raise Refused(f"factor '{_shown(path)}': \"low\" and \"high\" are numbers, low "
                          f"under high")
        try:
            value = experiments.value_at(layout, path)
        except Exception:               # the model's own refusal: the path, not its text
            raise Refused(f"factor '{_shown(path)}' addresses nothing in the model") from None
        if not (is_finite_number(value) and not isinstance(value, bool)):
            raise Refused(f"factor '{_shown(path)}' does not address a number of the model")
        factors.append({"path": path, "low": float(low), "high": float(high)})
    budget, replications = doc.get("budget"), doc.get("replications", 2)
    readings, series = doc.get("readings"), doc.get("series")
    if not _whole(budget, 2, LIMITS["budget"]):
        raise Refused(f"\"budget\" is the calibration's replications, 2 to {LIMITS['budget']}")
    if not _whole(replications, 1, LIMITS["replications"]):
        raise Refused(f"\"replications\" a calibration point is 1 to {LIMITS['replications']}")
    if not _whole(readings, 1, LIMITS["readings"]):
        raise Refused(f"\"readings\" the candidate is scored on is 1 to {LIMITS['readings']}")
    if series is not None:
        if not (isinstance(series, list) and 0 < len(series) <= LIMITS["series"]
                and all(isinstance(s, str) and _COMPARED.match(s) for s in series)):
            raise Refused("\"series\" lists compared series of the twin",
                          "system/WIP, resource/<name>/in_service, level/<name>, block/<name>/out")
        series = tuple(sorted(set(series)))
    return Plan(tuple(factors), budget, replications, readings, series)


def identity(model_hash, layer) -> str | None:
    """The model a score and the alerts are of: the file's, or the file's with an adopted
    layer, which is another model (D741)."""
    if layer is None or model_hash is None:
        return model_hash
    return sha256_text(canonical_json([model_hash, layer["id"]]))


def apply(model: dict, overrides) -> dict:
    """A new model with *overrides* in place: an override of the run, never a file."""
    from nagasim import experiments

    return experiments.apply_overrides(model, overrides)


def calibrate_window(model, series: str, data: list, plan: Plan, *, seed: int, workers,
                     should_stop) -> dict:
    """The default calibration: the factors on the window's readings of *series*, from the
    started *model*. {"overrides", "objective", "used"}."""
    from nagasim import calibrate
    from nagasim.optimize import Factor

    fit = calibrate.calibrate(
        model, [calibrate.SeriesTarget(series=series, data=data)],
        [Factor(f["path"], "continuous", f["low"], f["high"], name=f["path"])
         for f in plan.factors],
        budget=plan.budget, replications_per_point=plan.replications, seed=seed,
        workers=workers, should_stop=should_stop)
    return {"overrides": [{"path": o["path"], "value": float(o["value"])} for o in fit.overrides],
            "objective": float(fit.objective) if math.isfinite(fit.objective) else None,
            "used": int(fit.budget_used)}


class Refitter:
    """The refit of one twin (`twin.Cycle(refit=...)`), owned by the cycle's thread but for
    the calibration, which runs in its own and touches nothing here but its answer."""

    def __init__(self, plan: Plan, *, calibrator=None):
        self.plan = plan
        self._calibrate = calibrator or calibrate_window
        self._held: list = []           # recent forecast starts: what a window starts from
        self._tried: set = set()        # (series, side, since) of the alerts refitted
        self._pins: dict = {}           # the start of each alert's window, while it waits
        self._job: dict | None = None   # the latest refit, in flight or finished
        self._thread: threading.Thread | None = None
        # (its job's token, the answer or what it raised): a dropped job's late answer
        # carries its own token, never taken for the next job's (#979).
        self._done = None
        self._token = None              # the job in flight's
        self._stop = threading.Event()  # set when that job is dropped

    # ------------------------------------------------------------ fed by the cycle
    def hold(self, pending: dict, model: dict) -> None:
        """A forecast start: the model as it was started, for a window that begins here."""
        self._held = (self._held + [{
            "state": pending["state"], "from": float(pending["from"]),
            "horizon": float(pending["horizon"]), "seed": pending["seed"],
            "base": copy.deepcopy(pending.get("base") or {}), "layer": pending.get("refit"),
            "model": copy.deepcopy(model)}])[-LIMITS["held"]:]

    def busy(self) -> bool:
        """A refit in flight, or a dropped calibration's thread still running: one at a
        time, so no second one runs beside a stale one (#979)."""
        return self._in_flight() or (self._thread is not None and self._thread.is_alive())

    def _in_flight(self) -> bool:
        return self._job is not None and self._job["status"] in ("calibrating", "scoring")

    def waiting(self, status_of) -> bool:
        """Whether this refit's proposal is still unanswered in the ledger (*status_of*)."""
        job = self._job
        return job is not None and job["status"] == "proposed" and bool(job.get("id")) \
            and status_of(job["id"]) in ("proposed", "delivered")

    def shadow_model(self, model: dict, layer_id) -> dict | None:
        """The candidate's model for this forecast, or None: shadowed only while it is
        scored, on the layer it was fitted on, and until enough points are on the way."""
        job = self._job
        if job is None or job["status"] != "scoring" or job["layer"] != layer_id:
            return None
        points = [p for s in job["shadows"] for p in s["points"] if not p.get("missed")]
        if len(points) >= self.plan.readings or len(job["shadows"]) >= LIMITS["shadows"]:
            return None
        return apply(model, job["candidate"])

    def shadowed(self, record: dict, result) -> None:
        """The candidate's forecast beside *record*, the current model's on the same seed:
        the points of the refitted series, each with both bands."""
        from nagasim import twin

        job = self._job
        if job is None or job["status"] != "scoring" or result is None \
                or isinstance(result, Exception):
            return
        try:
            mine, _cut = twin._record(record, result)
        except Exception:               # as a forecast that cannot be read: not scored
            return
        bands = {p["at"]: p for p in mine["points"] if p["series"] == job["series"]}
        points = [{"series": p["series"], "at": p["at"],
                   "current": [p["mean"], p["q05"], p["q95"]],
                   "candidate": [bands[p["at"]][k] for k in ("mean", "q05", "q95")],
                   "observed": None, "observed_at": None}
                  for p in record["points"] if p["series"] == job["series"] and p["at"] in bands]
        if points:
            job["shadows"].append({"from": record["from"], "horizon": record["horizon"],
                                   "base": record.get("base") or {}, "points": points})

    def fill(self, seen: dict) -> None:
        """As `twin.Cycle._compare` fills a forecast's points: once, or missed."""
        from nagasim import twin

        job, now = self._job, seen.get("at")
        if job is None or job["status"] != "scoring" or not seen.get("observed") or now is None:
            return
        for shadow in job["shadows"]:
            late = shadow["horizon"] / twin.LIMITS["times"]
            for point in shadow["points"]:
                if point["observed"] is not None or point.get("missed") or point["at"] > now:
                    continue
                value = None if now - point["at"] > late else twin._reading(
                    shadow, point["series"], seen)
                if now - point["at"] > late or value is False:
                    point["missed"] = True
                elif value is not None:
                    point["observed"], point["observed_at"] = value, now

    def reset(self, why: str) -> None:
        """The model changed under a refit in flight (a layer adopted or ended): it is
        dropped, since its proof would compare against another model."""
        if self._in_flight():
            self._job.update(status="dropped", reason=why)
            self._stop.set()            # its calibration stops at its next check

    # ------------------------------------------------------------ one step
    def step(self, *, history: list, alerts: list, k: float, h: float, now, layer,
             open_proposal: bool, background: bool, should_stop, workers) -> dict | None:
        """The refit's turn after the cycle compared: take in a calibration that ended,
        judge a candidate whose points are in, or start one. The fields of a proposal
        when one is made (`recommend.Advisor.propose_refit` completes it), else None."""
        self._collect()
        job = self._job
        if job is not None and job["status"] == "scoring":
            return self._judge(job)
        if self.busy() or open_proposal or not is_finite_number(now):
            return None
        needed = math.ceil(h / k)
        waiting, pins = None, {}
        for alert in alerts:
            series, side, since = alert.get("subject"), alert.get("side"), alert.get("since")
            key = (series, side, since)
            if (self.plan.series is not None and series not in self.plan.series) \
                    or not is_finite_number(since) or key in self._tried:
                continue
            # The start is pinned at first sight: the held ones slide on, the window does not.
            start = self._pins.get(key) or min(
                (s for s in self._held if since <= s["from"] < now), key=lambda s: s["from"],
                default=None)
            if start is not None:
                pins[key] = start
            data = [] if start is None else self._window(history, series, start)
            # A whole horizon of the start, and enough readings in it: ceil(h/k) readings
            # close together are a moment, not a rate.
            if start is None or len(data) < needed or now < start["from"] + start["horizon"]:
                waiting = waiting or {"status": "waiting", "series": series, "side": side,
                                      "since": since, "window": {"readings": len(data),
                                                                 "needed": needed}}
                continue
            self._tried.add((series, side, since))
            self._begin(start, series, side, since, data, needed, layer, background,
                        should_stop, workers)
            return None
        self._pins = pins                   # an alert that ended frees its start
        if waiting is not None:
            self._job = waiting
        elif job is None or job["status"] == "waiting":
            self._job = None            # the alert ended before its window filled
        return None

    @staticmethod
    def _window(history: list, series: str, start: dict) -> list:
        """The readings of *series* after *start* within its horizon, as [(t, value)]: a
        counter's as increases since that start, under the same count only."""
        end, base = start["from"] + start["horizon"], start["base"].get(series)
        readings: dict = {}
        for record in history:
            mine = (record.get("base") or {}).get(series)
            for point in record["points"]:
                at = point["observed_at"]
                if point["series"] != series or point["observed"] is None or at is None \
                        or not start["from"] < at <= end:
                    continue
                if mine is None and base is None:
                    readings.setdefault(at, point["observed"])
                elif mine is not None and base is not None and mine[1] == base[1]:
                    readings.setdefault(at, point["observed"] + mine[0] - base[0])
        return sorted(readings.items())

    def _begin(self, start, series, side, since, data, needed, layer, background,
               should_stop, workers) -> None:
        self._job = {"status": "calibrating", "series": series, "side": side, "since": since,
                     "window": {"readings": len(data), "needed": needed, "from": start["from"],
                                "until": data[-1][0], "state": start["state"]},
                     "seed": start["seed"], "layer": start["layer"],
                     "base": [dict(o) for o in (layer or {}).get("overrides", [])],
                     "shadows": []}
        if start["layer"] != (layer or {}).get("id"):
            self._job.update(status="dropped", reason="the model changed")
            return
        args = (start["model"], series, [list(pair) for pair in data], self.plan)
        token, stop = object(), threading.Event()
        self._token, self._stop = token, stop
        more = {"seed": start["seed"], "workers": workers,
                "should_stop": lambda: stop.is_set() or bool(should_stop and should_stop())}
        if background:
            self._thread = threading.Thread(target=self._run, args=(args, more, token),
                                            daemon=True, name="naga-twin-refit")
            self._thread.start()
        else:
            self._run(args, more, token)
            self._collect()

    def _run(self, args, more, token) -> None:
        try:
            answer = self._calibrate(*args, **more)
        except Exception as exc:
            answer = exc
        self._done = (token, answer)

    def _collect(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = None
        if self._done is None or self._job is None:
            return
        (token, answer), self._done = self._done, None
        job = self._job
        if token is not self._token or job["status"] != "calibrating":
            return
        if isinstance(answer, Exception):
            # The class only: the text may be whatever a model or a library put in it.
            job.update(status="failed", reason=type(answer).__name__)
            return
        merged = {o["path"]: o["value"] for o in job["base"]}
        merged.update({o["path"]: o["value"] for o in answer["overrides"]})
        candidate = [{"path": path, "value": value} for path, value in sorted(merged.items())]
        why = self.plan.check(candidate)
        if why is not None:
            job.update(status="failed", reason=why)
            return
        job.update(status="scoring", candidate=candidate, fitted=len(answer["overrides"]),
                   objective=answer.get("objective"), used=answer.get("used"))

    def _judge(self, job: dict) -> dict | None:
        """Both bands on the same points, of the first candidate forecasts whose every
        point is read or missed, until they hold `readings` readings: whole forecasts, so
        every lead weighs alike (the first readings to come in are the shortest leads).
        The proposal's fields, or the reason the candidate is not proposed."""
        from nagasim import score

        filled = []
        for shadow in job["shadows"]:
            if len(filled) >= self.plan.readings:
                break
            if any(p["observed"] is None and not p.get("missed") for p in shadow["points"]):
                break                       # in the order they were made: none skipped
            for point in shadow["points"]:
                current = score.point(*point["current"], point["observed"])
                mine = score.point(*point["candidate"], point["observed"])
                if current is not None and mine is not None:
                    filled.append((current["pinball"], mine["pinball"]))
        if len(filled) < self.plan.readings:
            if len(job["shadows"]) >= LIMITS["shadows"] and all(
                    p["observed"] is not None or p.get("missed")
                    for s in job["shadows"] for p in s["points"]):
                job.update(status="unscored", reason=f"{len(filled)} of {self.plan.readings} "
                           f"readings in {LIMITS['shadows']} forecasts")
            job["scores"] = {"readings": len(filled), "current": None, "candidate": None}
            return None
        current = sum(row[0] for row in filled) / len(filled)
        mine = sum(row[1] for row in filled) / len(filled)
        job["scores"] = {"readings": len(filled), "current": current, "candidate": mine}
        if not mine < current:
            job.update(status="not_better")
            return None
        job.update(status="proposed")
        return {"overrides": job["candidate"], "series": job["series"], "side": job["side"],
                "window": {k: job["window"][k] for k in ("readings", "from", "until", "state")},
                "scores": dict(job["scores"]), "seed": job["seed"], "budget": self.plan.budget,
                "base": job["layer"]}

    # ------------------------------------------------------------ joined, judged, shown
    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def layer_from(self, doc) -> tuple:
        """(the adopted layer a history holds, None), or (None, why): judged whole, never
        a part of one applied. Absent is (None, None)."""
        if doc is None:
            return None, None
        if not (isinstance(doc, dict) and set(doc) == _LAYER_KEYS
                and isinstance(doc["id"], str) and _HEX.match(doc["id"])
                and isinstance(doc["series"], str) and len(doc["series"]) <= 400
                and all(doc[k] is None or (isinstance(doc[k], str) and len(doc[k]) <= 200)
                        for k in ("by", "at", "read"))):
            return None, "it cannot be read as one"
        why = self.plan.check(doc["overrides"])
        return (None, why) if why else (copy.deepcopy(doc), None)

    def document(self, status_of) -> dict:
        """The `refit` key of a result: plain, bounded, no reply text. *status_of* gives a
        proposal's status in the ledger."""
        job = dict(self._job or {"status": "idle"})
        shown = {"factors": [f["path"] for f in self.plan.factors],
                 "readings": self.plan.readings, "status": job["status"]}
        for key in ("reason", "series", "side", "since", "window", "candidate", "scores",
                    "objective", "used"):
            if key in job:
                shown[key] = copy.deepcopy(job[key])
        if job["status"] == "proposed" and job.get("id"):
            shown["id"], shown["outbox"] = job["id"], status_of(job["id"])
        scores = shown.get("scores") or {}
        if job["status"] == "proposed":     # a lower loss than the current's: never 0 over 0
            n = job.get("fitted", len(job.get("candidate") or []))
            shown["message"] = (
                f"a refit of {n} parameter{'s' if n != 1 else ''} cuts the pinball loss "
                f"{1 - scores['candidate'] / scores['current']:.0%} on the last "
                f"{scores['readings']} readings of {job['series']}: adopt?")
        elif job["status"] == "not_better":
            shown["message"] = (
                f"the refitted model's pinball loss {scores['candidate']:.3g} is not below the "
                f"current model's {scores['current']:.3g} on the last {scores['readings']} "
                f"readings of {job['series']}: nothing is proposed")
        return shown

    def named(self, record_id: str) -> None:
        """The ledger's id of the proposal just made."""
        if self._job is not None and self._job["status"] == "proposed":
            self._job["id"] = record_id
