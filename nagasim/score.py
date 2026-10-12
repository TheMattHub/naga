"""Scores of a twin's forecasts (PHASE7 7.14, D291 to D295).

A forecast states, for each compared series, a mean and a 5% to 95% band at six times; the
twin later sets what the plant showed beside each (`twin.Cycle`). This module turns those
pairs into numbers a person can weigh a forecast by:

    score.point(mean, q05, q95, observed)   # inside the band, the error, the pinball loss
    score.of(record)                        # one forecast: points, inside, missed
    totals = score.Totals(model_hash, horizon, since)
    totals.add(record, point)               # once a point, when it is filled or missed
    totals.rows()                           # by series and lead: coverage, bias, error, pinball
    score.recompute(directory, model, mapping, observations)    # the same, from the log

* What a score means: agreement IN DISTRIBUTION. Work in progress is drawn afresh at each
  forecast, so one point outside its band proves nothing; a coverage far from the nominal
  90% over many forecasts, or a bias that does not average out, says the model or the
  mapping is off at that lead. It does not say why.
* It is not a test (D294): successive forecasts overlap in time and one plant history is
  behind them all, so the points are not independent and no interval is given. A score
  always carries its basis: how many forecasts, how many points, over what period.
* The rolling score is sums a (series, lead): bounded by the model, not by time. It lives in
  the twin's history document, beside the forecasts it sums: one file, one write.

Stdlib only. What is read back is the twin's own and is still judged whole (`Totals.load`)
before a number of it is used.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

__all__ = ["LEGACY_FILE", "NOMINAL", "LEADS", "MAX_SERIES", "MAX_BYTES", "point", "of", "lead",
           "Totals", "legacy", "recompute"]

# Where the first build of 7.14 kept the rolling score: read once when a history has none,
# never written (D293 as amended).
LEGACY_FILE = "twin_scores.json"
NOMINAL = 0.9           # of the stored band: 5% to 95%
LEADS = 6               # the stated times of a forecast (`twin.LIMITS["times"]`)
MAX_SERIES = 50         # compared series of a forecast (`twin.LIMITS["series"]`)
MAX_BYTES = 2 ** 20     # the legacy score file
_SUM = 1e150            # a sum read back is under this: sums of such never reach infinity
# A row: scored, inside, sum of errors, of absolute errors, of pinball losses, missed (the
# twin was late or its source not ready), lost (a counter that lost count), and how many of
# the scored readings were not whole numbers.
_N, _INSIDE, _ERROR, _ABS, _PINBALL, _MISSED, _LOST, _FRACTION = range(8)
_COUNTS = (_N, _INSIDE, _MISSED, _LOST, _FRACTION)


def _num(value):
    """A finite number as a float, or None. A JSON integer may be too large for one (#373)."""
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) \
        and abs(value) <= sys.float_info.max
    return float(value) if ok else None


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _rho(tau: float, u: float) -> float:
    """The pinball (quantile) loss of an observation *u* above the stated *tau* quantile."""
    return u * (tau - (u < 0))


def point(mean, q05, q95, observed) -> dict | None:
    """One stated point against what was observed, or None when a value is missing.
    `pinball` is the mean loss over the two stored quantiles: 1/40 of the interval score,
    so a band is paid for its width and, twenty times more, for each unit it misses by."""
    mean, q05, q95, observed = (_num(v) for v in (mean, q05, q95, observed))
    if None in (mean, q05, q95, observed):
        return None
    return {"inside": q05 <= observed <= q95, "error": observed - mean,
            "pinball": (_rho(0.05, observed - q05) + _rho(0.95, observed - q95)) / 2}


def _scored(found: dict):
    return point(found.get("mean"), found.get("q05"), found.get("q95"), found.get("observed"))


def of(record: dict) -> dict:
    """The score of one forecast record: its points that were scored, how many fell inside
    their band, how many were missed (the twin was late, or its source not ready) and how
    many were lost (a counter lost count). Coverage only: its series have different units."""
    scored = [s for s in map(_scored, record["points"]) if s is not None]
    return {"points": len(scored), "inside": sum(s["inside"] for s in scored),
            "missed": sum(1 for p in record["points"] if p.get("missed") and not p.get("lost")),
            "lost": sum(1 for p in record["points"] if p.get("lost"))}


def lead(record: dict, found: dict) -> int | None:
    """Which of the six stated times of its forecast a point is: 1 (a sixth of the horizon
    ahead) to 6 (the whole of it). None for a record that does not say."""
    start, horizon, at = (_num(v) for v in (record.get("from"), record.get("horizon"),
                                            found.get("at")))
    if None in (start, horizon, at) or horizon <= 0:
        return None
    return min(max(round((at - start) / (horizon / LEADS)), 1), LEADS)


def legacy(path) -> dict | None:
    """The rolling score a first build of 7.14 left in its own file, or None. Bounded."""
    try:
        if Path(path).stat().st_size > MAX_BYTES:
            return None
        doc = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


class Totals:
    """The rolling score of one twin (D293): sums a (series, lead) since `since`, of the
    forecasts of ONE model and horizon. A point is added once, by whoever fills or misses
    it; a forecast is counted when its first point is."""

    def __init__(self, model, horizon: float, since: str):
        self.model, self.horizon, self.since = model, float(horizon), since
        self.forecasts = self.same_seed = 0
        self.seed = None                    # of the forecast counted last
        self.first = self.last = None       # model time of the first and the last point
        self.sums: dict[str, list] = {}     # series -> a row of sums a lead

    @classmethod
    def load(cls, doc, model, horizon: float, since: str) -> tuple:
        """(the score *doc* holds, None) when it is this model's and this horizon's and
        sound; else (an empty one beginning at *since*, why): "absent", "model", "horizon"
        or "unsound". Never silently: the caller says the last three (D293)."""
        new = cls(model, horizon, since)
        if not isinstance(doc, dict):
            return new, "absent"
        if isinstance(doc.get("model"), str) and doc["model"] != model:
            return new, "model"
        if _num(doc.get("horizon")) is not None and _num(doc["horizon"]) != new.horizon:
            return new, "horizon"
        rows = doc.get("rows")
        if not (isinstance(doc.get("model"), str) and _num(doc.get("horizon")) is not None
                and isinstance(doc.get("since"), str) and len(doc["since"]) <= 64
                and _whole(doc.get("forecasts")) and _whole(doc.get("same_seed"))
                and doc["same_seed"] <= doc["forecasts"]
                and (doc.get("seed") is None or isinstance(doc["seed"], int))
                and all(doc.get(k) is None or _num(doc[k]) is not None for k in ("first", "last"))
                and isinstance(rows, dict) and len(rows) <= MAX_SERIES
                and all(isinstance(name, str) and isinstance(leads, list) and len(leads) == LEADS
                        and all(isinstance(row, list) and len(row) in (6, 8) for row in leads)
                        for name, leads in rows.items())):
            return new, "unsound"
        # A row of the first build has no `lost` and no count of fractions.
        sums = {name: [list(row) + [0, 0][:8 - len(row)] for row in leads]
                for name, leads in rows.items()}
        if not all(all(_num(v) is not None and abs(v) < _SUM for v in row)
                   and all(_whole(row[i]) for i in _COUNTS) and row[_INSIDE] <= row[_N]
                   and row[_FRACTION] <= row[_N] and row[_ABS] >= 0
                   for leads in sums.values() for row in leads):
            return new, "unsound"
        new.since, new.forecasts, new.same_seed = doc["since"], doc["forecasts"], doc["same_seed"]
        new.seed, new.first, new.last = doc.get("seed"), doc.get("first"), doc.get("last")
        new.sums = sums
        return new, None

    def takes(self, record: dict) -> bool:
        """Whether *record* is a forecast of this score's model and horizon."""
        return self.model is not None and record.get("model") == self.model \
            and _num(record.get("horizon")) == self.horizon

    def count(self, record: dict) -> None:
        """One more forecast is in the score: said when its first point is added."""
        seed = record.get("seed") if isinstance(record.get("seed"), int) else None
        self.same_seed += self.forecasts > 0 and seed is not None and seed == self.seed
        self.forecasts, self.seed = self.forecasts + 1, seed

    def add(self, record: dict, found: dict) -> None:
        """A point of *record* that was just filled, missed or lost."""
        k, scored = lead(record, found), _scored(found)
        missed = bool(found.get("missed")) and scored is None
        if k is None or not (missed or scored) or (
                found["series"] not in self.sums and len(self.sums) >= MAX_SERIES):
            return
        row = self.sums.setdefault(found["series"], [[0, 0, 0.0, 0.0, 0.0, 0, 0, 0]
                                                     for _ in range(LEADS)])[k - 1]
        if missed:
            row[_LOST if found.get("lost") else _MISSED] += 1
            return
        row[_N] += 1
        row[_INSIDE] += scored["inside"]
        row[_ERROR] += scored["error"]
        row[_ABS] += abs(scored["error"])
        row[_PINBALL] += scored["pinball"]
        row[_FRACTION] += not float(found["observed"]).is_integer()
        at = _num(found.get("observed_at"))
        if at is not None:
            self.first = at if self.first is None else min(self.first, at)
            self.last = at if self.last is None else max(self.last, at)

    @property
    def empty(self) -> bool:
        return not self.sums

    def document(self) -> dict:
        return {"model": self.model, "horizon": self.horizon, "since": self.since,
                "forecasts": self.forecasts, "same_seed": self.same_seed, "seed": self.seed,
                "first": self.first, "last": self.last, "rows": self.sums}

    def basis(self) -> dict:
        """What the rows stand on (D294): never shown without it. `missed`: the twin was
        late or its source not ready; `lost`: a counter lost count, or had no start."""
        rows = [row for series in self.sums.values() for row in series]
        return {"forecasts": self.forecasts, "same_seed": self.same_seed,
                "points": sum(row[_N] for row in rows), "missed": sum(row[_MISSED] for row in rows),
                "lost": sum(row[_LOST] for row in rows),
                "since": self.since, "first": self.first, "last": self.last}

    def rows(self) -> list[dict]:
        """By series and lead: `lead` in the model's time unit, `points`, `missed`, `lost`,
        and over the points `coverage` (share inside the band), `bias` (mean of observed
        minus forecast mean), `error` (mean absolute) and `pinball`; None where nothing was
        scored. `whole` marks a row whose every reading was a whole number: a band of whole
        numbers holds more than its nominal share (the quantiles of a discrete variable
        overshoot), so its coverage reads above 90% on a right model and its pinball loss
        is the number to compare."""
        out = []
        for series in sorted(self.sums):
            for k, row in enumerate(self.sums[series], 1):
                n = row[_N]
                if n or row[_MISSED] or row[_LOST]:
                    out.append({
                        "series": series, "lead": self.horizon * k / LEADS, "points": n,
                        "missed": row[_MISSED], "lost": row[_LOST],
                        "whole": bool(n) and not row[_FRACTION],
                        **{name: row[index] / n if n else None for name, index in (
                            ("coverage", _INSIDE), ("bias", _ERROR), ("error", _ABS),
                            ("pinball", _PINBALL))}})
        return out


# ---------------------------------------------------------------- from the log (D295)
def _same(a, b) -> bool:
    """Equal, sums up to the order they were added in."""
    if isinstance(a, float) or isinstance(b, float):
        return _num(a) is not None and _num(b) is not None and math.isclose(
            a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(map(_same, a, b))
    return a == b


def recompute(directory, model, mapping, observations) -> tuple:
    """(lines to print, how many of them are differences): every stored reading, record
    score and the rolling score of the twin in *directory*, computed again from the plant
    log *observations* through *mapping*. The log is sorted and mapped by the twin's own
    source, stopped at each stored `observed_at`; nothing is written.

    A twin that ran on the log itself (`--log`) recomputes exactly. One that ran live
    differs by what late events cost it (D255, D296): the difference is the measurement."""
    from nagasim import plant, twin     # late: `twin` scores with this module
    from nagasim.core.timeunits import UNITS
    from nagasim.live import stamp

    directory = Path(directory)
    history = twin._read(directory / twin.HISTORY_FILE, twin.KIND + "-history",
                         twin.LIMITS["history_bytes"])
    found = history.get("forecasts") if history else None
    records = [r for r in found if twin._sound(r)] if isinstance(found, list) else []
    if not records:
        return ["no stored forecast can be read there"], 1

    class Until(twin.LogFeed):
        """The log up to a model time, however often it is asked."""
        until = -math.inf

        def clock(self):
            return self.until

        def rebase(self, at) -> None:
            """Its own times as model time: "up to a reading's instant" is then exact."""
            self._times = [at(t) for t in self._times]

    feed = Until(observations)
    refused = [p for p in plant.check(mapping, model) if p.severity == "error"]
    if refused:
        return [f"{p.code}: {p.message}" for p in refused], 1
    source = twin.PlantSource(model, mapping, feed)
    feed.rebase(source.mapper.at)
    zero, unit = stamp(source.epoch), UNITS[source.time_unit]
    source._clock = lambda: zero + feed.until * unit        # the present, for the source

    def reading(at: float) -> dict:
        feed.until = at
        for _ in range(1000):               # a step reads a bounded part of a long log
            seen = source.observe()
            if not any(p.code == "W_TWIN_CUT" for p in seen["problems"]):
                break
        return seen

    # Every instant a stored number was read at, in time order: a forecast's start (the
    # base of a counted series) and each filled point. A base is taken again from the log
    # and not compared: it is a total since the mapper first saw the counter, which a twin
    # started later than the log saw later; the INCREASE is what was stored, and is compared.
    wanted = sorted({(r["from"], "from", i) for i, r in enumerate(records)
                     if isinstance(r.get("base"), dict) and _num(r.get("from")) is not None}
                    | {(p["observed_at"], "point", i) for i, r in enumerate(records)
                       for p in r["points"] if p["observed_at"] is not None})
    again = json.loads(json.dumps(records))
    lines, differences = [], 0

    def differs(text: str) -> None:
        nonlocal differences
        differences += 1
        if differences <= 50:
            lines.append("DIFFERENT " + text)

    for at, what, index in wanted:
        seen, record = reading(at), again[index]
        if what == "from":
            record["base"] = {series: [float(seen["observed"][series]), marker]
                              for series, marker in seen["cumulative"].items()
                              if series in records[index]["base"] and series in seen["observed"]}
            continue
        for found, stored in zip(record["points"], records[index]["points"]):
            if stored["observed_at"] != at:
                continue
            value = twin._reading(record, found["series"], seen)
            found["observed"] = value = value if isinstance(value, float) else None
            if value is None or not _same(value, float(stored["observed"])):
                differs(f"forecast {record['state'][:12]}, {found['series']} at t = "
                        f"{found['at']:g}: stored {stored['observed']:g}, the log gives "
                        f"{'no reading' if value is None else format(value, 'g')}")
    by: dict[tuple, Totals] = {}
    for record, stored in zip(again, records):
        if stored.get("score") is not None and of(record) != stored.get("score"):
            differs(f"forecast {record['state'][:12]}: its score is {of(record)}, stored "
                    f"{stored.get('score')}")
        totals = by.setdefault((record.get("model"), record["horizon"]),
                               Totals(record.get("model"), record["horizon"], ""))
        if any(p["observed"] is not None or p.get("missed") for p in record["points"]):
            totals.count(record)
        for found in record["points"]:
            totals.add(record, found)
    others = sum(1 for record in records if record.get("model") != source.model_hash)
    if others:
        lines.append(f"note: {others} stored forecasts ran on another model than the one "
                     f"given (the readings depend on the mapping and the log only)")
    lines.append(f"{len(records)} stored forecasts, {len([w for w in wanted if w[1] == 'point'])} "
                 f"instants read again from the log")
    kept = history.get("scores") if isinstance(history.get("scores"), dict) \
        else legacy(directory / LEGACY_FILE)
    if kept is None:
        lines.append("no rolling score is stored there yet")
    else:
        totals = by.get((kept.get("model"), kept.get("horizon")))
        if totals is not None and _same(totals.sums, kept.get("rows")) \
                and totals.forecasts == kept.get("forecasts"):
            lines.append(f"the rolling score ({kept['forecasts']} forecasts) equals its "
                         f"recomputation")
        elif totals is None or (_whole(kept.get("forecasts"))
                                and kept["forecasts"] > totals.forecasts):
            lines.append(f"the rolling score holds {kept.get('forecasts')} forecasts and "
                         f"{0 if totals is None else totals.forecasts} of them are still "
                         f"stored: it is not recomputed (the records above are)")
        else:
            differs("the rolling score is not the one its stored forecasts give")
    if differences > 50:
        lines.append(f"... and {differences - 50} more differences")
    lines.append("every stored score equals its recomputation from the log" if not differences
                 else f"{differences} differences: a twin that ran live differs from its "
                      f"sorted log by what late events cost it")
    return lines, differences
