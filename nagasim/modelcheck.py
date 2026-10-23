"""Model assertions, `naga check`, CI template (PHASE6 6D W7 Q3, ARCHITECTURE D126;
three states and the engine move: Matteo's 2026-09-26 review).

A model assertion (schema v25, `core/schema/`) pins one KPI -- an
`experiments.flatten_kpis` key, e.g. `"resource/Server/Usage % (snapshot)"` --
against a comparison operator and value, checked over the model's OWN
replications by a Student-t confidence interval:

    {"name": "util_ok", "kpi": "resource/Server/Usage % (snapshot)", "op": "<=",
     "value": 80, "replications": 5, "confidence": 0.95, "tolerance": 0}

An assertion is THREE-STATE: "pass" when the WHOLE interval satisfies the
op (`<`/`<=`/`>`/`>=` clears the bound; `==` lies inside `value +/-
tolerance`), "fail" when the whole interval instead VIOLATES it (for `==`,
lies entirely outside the tolerance band), and "inconclusive" when the
interval straddles the boundary -- neither the whole interval nor its
complement. `Result.passed` is kept as `status == "pass"` for callers that
only care about the boolean. A single replication, or one with zero
variance, has no interval to speak of -- the "interval" collapses to the
point estimate (never a straddling result from an undefined bound).

`check(layout)` also runs the model's existing "reality_checks" section
(PHASE6 S8, `run_reality_checks` below) in the same pass, as extra `Result`s
of kind "reality_check" -- one command, one pass, both kinds of model test.
A reality check is one deterministic replication with no CI, so it stays
plain pass/fail, never inconclusive. The "path_checks" section (`nagasim.pathcheck`,
D722) joins the same pass the same way, as results of kind "path_check": one traced
replication, pass/fail, each message saying it rests on one seed.

ENGINE side (O8, ARCHITECTURE section 10.1): this module is in
`packages/engine/build_wheel.py`'s ENGINE list, so the engine wheel alone can
run `naga check`/`naga ci-template` (dispatched as an ordinary engine
subcommand from `nagasim/cli.py`; also reachable as `product check`/
`product ci-template` on the full install).

REALITY CHECKS (F4: folded in from the former `nagasim/realitycheck.py`,
PHASE6 S8). A reality check pairs a TEST INPUT -- a condition imposed on the
model, such as "workforce = 0" or a parameter pushed to an extreme -- with a
CONSTRAINT expected to hold given that input, such as "production = 0" or
"inventory never negative". Each check is its own run: apply the input,
simulate, and walk the result looking for the first instant the constraint
fails.

    table = run_reality_checks(layout)
    table[["Check", "Passed", "Violation time"]]

A model file section (schema v23, `core/schema/`, `core/document.py`):

    {"reality_checks": [
        {"name": "shutdown", "inputs": {"auxiliaries.Workforce.expr": "0"},
         "constraint": "AUX(\\"Production\\") <= 0", "when": "always"}]}

INPUTS. `{override path: value}`, the same dotted-path grammar
`experiments.apply_overrides` already gives a scenario -- a map here rather
than a list because a check names each path once. Whether a path actually
addresses something in this model is `apply_overrides`'s job, exactly as for a
scenario; `core/schema/`'s structural validation only checks the shape.

CONSTRAINT. A boolean expression over LEVEL()/AUX()/FLOW() and TNOW -- the
model's own safe expression grammar (`core/expressions.py`), so nothing new to
parse or trust. It is evaluated twice (DEVLOG #165):

* LIVE, at every level-engine re-evaluation -- each SD grid point with
  `simulation.sd`, each event otherwise -- by a hidden "sample" delay the check
  adds (`_with_probe`, the SAMPLE_IF_TRUE machinery) that holds the first
  instant the constraint failed. Between events without a grid, a level moves
  linearly and an AUX of TNOW is not re-read: that is the resolution limit.
* On every "timeseries" sample (`simulation.timeseries_points`), which also
  gives the Snapshot row. AUX is looked up directly there (its expression tree
  is not carried in the results): the sampler evaluated it the same way.

The earlier of the two failures is reported; Snapshot is None when only the
live probe saw it (it fell between samples).

WHEN (grammar and parser: `core/schema/sections_validation.py`'s `parse_when`, shared with
the validator). "always" (every sample, the default), "final" (the last sample only) and
"after <t>" (samples at or after time t) are Vensim's own three shapes. D677 adds
"before <t>" and "between <t1> and <t2>" (every sample in the window, both ends in) and
"eventually", alone or with such a window: the constraint must hold at LEAST ONCE in the
window (Open Industry Project's "eventually"). For "eventually" the live probe holds the first
instant the constraint HELD inside the window, not the first failure, and the check passes if
the probe or any sample in the window saw it hold. One that already holds at the window's
first sample passes with a note ("held from the start of the window: nothing had to happen"):
it asked nothing of the model. One that never held fails with "never held <window in words>",
and "Violation time" is the window's end (the run's end when the window has none or runs past it).

RUN. Each check is ONE deterministic replication (replication 1) at the same
seed as every other check in the set (Vensim-style CRN across checks: unrelated
checks then draw identically wherever the model still samples anything, so a
difference in outcome is the input, not the noise).
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import sys
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from nagasim import api, experiments, laws, pathcheck
from nagasim.core import submodels
from nagasim.core.expressions import ExpressionError, ExpressionScope, compile_expression
from nagasim.core.levels import LevelSystem
from nagasim.core.schema.sections_validation import parse_when
from nagasim.core.stats import ci95

__all__ = ["Result", "check", "assert_model", "main", "run_reality_checks", "RESULT_COLUMNS"]

_OPS = ("<", "<=", ">", ">=", "==")
_STATUSES = ("pass", "fail", "inconclusive")


@dataclasses.dataclass
class Result:
    """One assertion or reality check outcome. `kpi`/`op`/`value`/`mean`/
    `half_width`/`lower`/`upper` are None for a reality check (it has no CI:
    one deterministic replication, pass/fail from `run_reality_checks`,
    so its `status` is only ever "pass" or "fail", never "inconclusive")."""
    name: str
    kpi: str | None
    op: str | None
    value: float | None
    mean: float | None
    half_width: float | None
    lower: float | None
    upper: float | None
    status: str  # "pass" | "fail" | "inconclusive"
    message: str
    kind: str  # "assertion" | "reality_check" | "path_check"
    note: str = ""  # a reality check's extra word on its outcome (D677); it is also in `message`

    @property
    def passed(self) -> bool:
        """Compatibility with the old boolean-only Result: True iff `status == "pass"`."""
        return self.status == "pass"


def _close_kpis(kpi: str, available) -> str:
    matches = difflib.get_close_matches(kpi, available, n=3)
    return f"; close keys: {', '.join(matches)}" if matches else ""


def _interval(values, confidence: float) -> tuple[float, float, float]:
    """(mean, lower, upper) -- a single replication or zero variance collapses
    the interval to the point estimate rather than the NaN `ci95` gives an
    n<2 sample, so a downstream comparison never has a bound to fail on."""
    stat = ci95(values, confidence)
    mean = float(stat["mean"])
    if len(values) < 2 or not (stat["sd"] > 0):
        return mean, mean, mean
    return mean, float(stat["lb"]), float(stat["ub"])


def _op_status(op: str, value: float, tolerance: float, lower: float, upper: float) -> str:
    """"pass" when the WHOLE interval satisfies *op*, "fail" when the whole
    interval instead violates it, "inconclusive" when it straddles the
    boundary (neither the whole interval nor its complement)."""
    if op == "==":
        lo, hi = value - tolerance, value + tolerance
        if lower >= lo and upper <= hi:
            return "pass"
        if upper < lo or lower > hi:
            return "fail"
        return "inconclusive"
    pass_cond, fail_cond = {
        "<": (upper < value, lower >= value),
        "<=": (upper <= value, lower > value),
        ">": (lower > value, upper <= value),
        ">=": (lower >= value, upper < value),
    }[op]
    if pass_cond:
        return "pass"
    if fail_cond:
        return "fail"
    return "inconclusive"


def _check_assertion(entry: dict, flat, available: list[str]) -> Result:
    name = entry["name"]
    kpi = entry["kpi"]
    op = entry["op"]
    if op not in _OPS:
        raise ValueError(f"assertion '{name}': unknown op {op!r}; use one of {', '.join(_OPS)}")
    value = float(entry["value"])
    tolerance = float(entry.get("tolerance", 0))
    confidence = float(entry.get("confidence", 0.95))

    values = (flat.loc[flat["KPI"] == kpi, "Value"].to_numpy(dtype=float)
              if flat is not None else [])
    if len(values) == 0:
        return Result(name, kpi, op, value, None, None, None, None, "fail",
                      f"assertion '{name}': unknown KPI {kpi!r}{_close_kpis(kpi, available)}",
                      "assertion")

    mean, lower, upper = _interval(values, confidence)
    half_width = (upper - lower) / 2.0
    bound = f"{value} +/- {tolerance}" if op == "==" else f"{op} {value}"
    status = _op_status(op, value, tolerance, lower, upper)
    word = {"pass": "passed", "fail": "failed", "inconclusive": "inconclusive"}[status]
    message = (f"assertion '{name}' {word}: {kpi} = {mean:.6g} "
              f"[{lower:.6g}, {upper:.6g}] {bound}")
    return Result(name, kpi, op, value, mean, half_width, lower, upper, status, message, "assertion")


def _reality_result(row) -> Result:
    name = row["Check"]
    passed = bool(row["Passed"])
    status = "pass" if passed else "fail"
    if passed:
        message = f"reality check '{name}' passed"
    else:
        when = row["Violation time"]
        message = (f"reality check '{name}' failed" +
                  (f" at t={when:.6g}" if when is not None else ""))
    note = row["Note"]
    if note:
        message += f" ({note})"
    return Result(name, None, None, None, None, None, None, None, status, message,
                  "reality_check", note)


def _path_result(row: dict) -> Result:
    """One `pathcheck.check_paths` row as a Result: the first violations with their entity
    and time in the message, and "one seed" always, because one traced run proves only that
    run's routes (D722)."""
    name = row["name"]
    seed_words = f"one seed, {row['judged']} entities judged"
    if row["truncated"]:
        seed_words += "; the trace stopped early, later arrivals unjudged"
    if row["passed"]:
        return Result(name, None, None, None, None, None, None, None, "pass",
                      f"path check '{name}' passed ({seed_words})", "path_check", seed_words)
    shown = "; ".join(f"entity {v['entity']} ({v['e_type']}) at t={v['t']:.6g}: {v['what']}"
                      for v in row["examples"])
    more = row["violations"] - len(row["examples"])
    message = (f"path check '{name}' failed: {row['violations']} violation(s); "
               f"{shown}" + (f"; and {more} more" if more > 0 else "") + f" ({seed_words})")
    return Result(name, None, None, None, None, None, None, None, "fail", message,
                  "path_check", seed_words)


# ---------------------------------------------------------------------- reality checks

RESULT_COLUMNS = ["Check", "Passed", "Violation time", "Snapshot", "Note"]
# Hidden "sample" delay + auxiliary holding the first failing instant (#165).
_PROBE = "~reality_check"
_INF = float("inf")
_FROM_START = "held from the start of the window: nothing had to happen"


def _overrides(name: str, inputs) -> list:
    if inputs is not None and not isinstance(inputs, dict):
        raise ValueError(f"reality check '{name}': \"inputs\" must be a {{path: value}} object")
    return [{"path": path, "value": value} for path, value in (inputs or {}).items()]


def _when_mask(mode: str, lo: float, hi: float, times: np.ndarray) -> np.ndarray:
    """Which sampled *times* the check looks at: the last one for "final", else those in [lo, hi]."""
    if mode == "final":
        mask = np.zeros_like(times, dtype=bool)
        if len(mask):
            mask[-1] = True
        return mask
    return (times >= lo) & (times <= hi)


def _window_words(lo: float, hi: float) -> str:
    if lo == -_INF and hi == _INF:
        return "during the run"
    if hi == _INF:
        return f"after {lo:g}"
    if lo == -_INF:
        return f"before {hi:g}"
    return f"between {lo:g} and {hi:g}"


def _with_probe(layout: dict, constraint_text: str, mode: str, lo: float, hi: float) -> dict:
    """*layout* plus the live probe (DEVLOG #165): a sample delay whose input is TNOW, so it
    holds the first instant its condition was true (-1 while none), read back through an
    auxiliary. The condition is "not yet seen and the constraint fails now" ("always"), or
    "... holds now" ("eventually", D677), inside the window [lo, hi]. "final" needs no probe:
    the last sample is exact."""
    if mode == "final":
        return layout
    seen = f"({constraint_text})" if mode == "eventually" else f"not ({constraint_text})"
    window = ((f" and TNOW >= {lo!r}" if lo > -_INF else "") +
              (f" and TNOW <= {hi!r}" if hi < _INF else ""))
    probe = json.dumps(_PROBE)
    return {**layout,
            "delays": list(layout.get("delays") or []) + [{
                "name": _PROBE, "kind": "sample", "input": "TNOW", "initial": -1,
                "condition": f"DELAY({probe}) < 0 and {seen}{window}"}],
            "auxiliaries": list(layout.get("auxiliaries") or []) + [
                {"name": _PROBE, "expr": f"DELAY({probe})"}]}


# A sampled row holds levels, flows and auxiliaries only (#687): these read state it has not.
_UNSAMPLED = ("CONV", "FRACTION", "POWER")


def _bus(layout: dict) -> dict:
    """tag -> [(flow name, priority)] of the flattened layout, for FLOWSUM on a row."""
    tagged: dict = {}
    for flow in submodels.flatten(layout).get("flows") or []:
        if isinstance(flow, dict) and flow.get("tag"):
            tagged.setdefault(str(flow["tag"]), []).append(
                (str(flow.get("name")), float(flow.get("priority") or 0.0)))
    return tagged


def _row_scope(t: float, row: dict, bus: dict | None = None) -> ExpressionScope:
    """A scope for LEVEL()/AUX()/FLOW()/TNOW backed by one sampled row of the
    "timeseries" frame instead of a live run.

    AUX short-circuits through `aux_memo` -- `core/expressions.py`'s own
    per-pass memo, checked before it would otherwise re-evaluate the
    auxiliary's expression tree, which is not available after the run ends.
    LEVEL/FLOW read a level/flow stand-in exposing only the one field each
    function touches; `_level`'s own resync check (`system._last_t !=
    system.env.now`) is defeated by giving the fake clock the same value
    twice, so it never calls a `.sync()` the stand-in does not have.
    """
    levels = {name[len("level/"):]: SimpleNamespace(level=value)
             for name, value in row.items() if name.startswith("level/")}
    # The trace already holds each flow per its own rate_time_unit, so `per` is 1.
    flows = {name[len("flow/"):]: SimpleNamespace(actual=value, per=1.0)
            for name, value in row.items() if name.startswith("flow/")}
    aux_memo = {name[len("aux/"):]: value for name, value in row.items() if name.startswith("aux/")}
    system = SimpleNamespace(levels=levels, flow_index=flows, _last_t=t,
                             env=SimpleNamespace(now=t), _initialising=False, _sums={},
                             tagged={tag: [SimpleNamespace(actual=row.get(f"flow/{n}", 0.0),
                                                           per=1.0, priority=p)
                                           for n, p in members]
                                     for tag, members in (bus or {}).items()})
    system.flow_sum = lambda tag, below=None: LevelSystem.flow_sum(system, tag, below)
    ctx = SimpleNamespace(now=t, level_system=system, aux_memo=aux_memo, variables=None)
    return ExpressionScope(ctx=ctx)


def _check_one(base: dict, entry: dict, *, seed: int, run_length, warmup) -> dict:
    name = entry.get("name") or ""
    if not name:
        raise ValueError("reality check: \"name\" must not be empty")
    constraint_text = entry.get("constraint") or ""
    if not constraint_text:
        raise ValueError(f"reality check '{name}': \"constraint\" must not be empty")
    try:
        constraint = compile_expression(constraint_text)
    except ExpressionError as exc:
        raise ValueError(f"reality check '{name}': {exc}") from None
    unsampled = sorted(constraint.calls & set(_UNSAMPLED))
    if unsampled:
        raise ValueError(f"reality check '{name}': {', '.join(unsampled)}() is not sampled; "
                         f"read it through an auxiliary and check AUX(\"<name>\")")

    try:                                                # reject a bad "when" before running
        mode, lo, hi = parse_when(entry.get("when", "always"))
    except ValueError as exc:
        raise ValueError(f"reality check '{name}': {exc}") from None
    layout = experiments.apply_overrides(base, _overrides(name, entry.get("inputs")))
    layout = _with_probe(layout, constraint_text, mode, lo, hi)
    results = api.run_model(layout, replications=1, seed=seed, run_length=run_length, warmup=warmup)
    if results is None:
        raise ValueError(f"reality check '{name}': the run produced no result")
    df = results.get("timeseries")
    if df is None or df.empty:
        raise ValueError(f"reality check '{name}': needs the timeseries sampler on "
                         f"(simulation.timeseries_points, or a run length)")

    pivot = df.pivot(index="Time", columns="Series", values="Value").sort_index()
    bus = _bus(layout) if "FLOWSUM" in constraint.calls else None
    column = f"aux/{_PROBE}"
    live = pivot.pop(column).dropna() if column in pivot else None
    first = float(live.iloc[-1]) if live is not None and len(live) else -1.0
    times = pivot.index.to_numpy(dtype=float)

    def outcome(passed, at=None, snapshot=None, note=""):
        return {"Check": name, "Passed": passed, "Violation time": at, "Snapshot": snapshot,
                "Note": note}

    # "eventually" looks for a sample where the constraint holds; the others for one where it fails.
    hunting = mode == "eventually"
    for i, t in enumerate(times[_when_mask(mode, lo, hi, times)]):
        if first >= 0 and t > first and not hunting:
            break                                       # the live probe failed earlier
        row = pivot.loc[t].dropna().to_dict()
        try:
            holds = bool(constraint.evaluate(_row_scope(float(t), row, bus)))
        except ExpressionError as exc:
            raise ValueError(f"reality check '{name}': {exc}") from None
        if holds == hunting:
            # "From the start" only when the probe, which sees between samples, agrees: the
            # first sample in the window may come after the constraint began to hold.
            from_start = i == 0 and (first < 0 or first <= max(lo, float(times[0])))
            return (outcome(True, note=_FROM_START if from_start else "") if hunting
                    else outcome(False, float(t), row))
    if first >= 0:                                      # only the live probe saw it
        return outcome(True) if hunting else outcome(False, first)
    if hunting:
        return outcome(False, min(hi, float(times[-1])),
                       note=f"never held {_window_words(lo, hi)}")
    return outcome(True)


def run_reality_checks(layout, *, seed: int = 0, run_length=None, warmup=None,
                       should_stop=None) -> pd.DataFrame:
    """Run every "reality_checks" entry and report pass/fail.

    Each check applies its own "inputs" over the base model (as an
    `experiments.apply_overrides` scenario) and runs ONE deterministic
    replication at *seed* -- every check uses the same seed, so unrelated
    randomness lines up across checks (CRN) and a difference in outcome is the
    input, not the noise. *run_length*/*warmup* fall back to the model's own
    settings, like `api.run_model`. *should_stop*, polled between checks, ends
    the sweep early with whatever finished so far.

    Returns a `RESULT_COLUMNS` DataFrame: "Check", "Passed", "Violation time"
    (the first sampled instant the constraint failed, None if it never did) and
    "Snapshot" (that instant's LEVEL()/AUX()/FLOW() values, None when passed).
    A model with no "reality_checks" returns an empty frame.
    """
    base = api.load_layout(layout)
    rows = []
    for entry in base.get("reality_checks") or []:
        if should_stop is not None and should_stop():
            break
        rows.append(_check_one(base, entry, seed=seed, run_length=run_length, warmup=warmup))
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


# ---------------------------------------------------------------------- assertions + reality checks

def check(layout, *, workers: int = 1, runs: dict | None = None) -> list[Result]:
    """Every "assertions", "reality_checks" and "path_checks" entry, as `Result`s.

    Assertions sharing a "replications" count run in ONE batch of
    `api.run_model` replications (the KPI is read off the same run for all of
    them), so N assertions at 5 replications each cost 5 runs total, not 5N.
    `workers` is passed straight through to that run. *runs*, when given, receives each
    batch's results by replication count, so the law check can reuse one (seed 0 here too).
    """
    base = api.load_layout(layout)
    results: list[Result] = []

    by_reps: dict[int, list[dict]] = {}
    for entry in base.get("assertions") or []:
        by_reps.setdefault(int(entry.get("replications", 5)), []).append(entry)

    for reps in sorted(by_reps):
        group = by_reps[reps]
        run_results = api.run_model(base, replications=reps, seed=0, workers=workers)
        if runs is not None:
            runs[reps] = run_results
        flat = experiments.flatten_kpis(run_results) if run_results is not None else None
        available = sorted(set(flat["KPI"])) if flat is not None else []
        for entry in group:
            results.append(_check_assertion(entry, flat, available))

    reality = run_reality_checks(base)
    for _, row in reality.iterrows():
        results.append(_reality_result(row))

    if base.get("path_checks"):
        results.extend(_path_result(row) for row in pathcheck.check_paths(base))

    return results


def assert_model(path_or_layout, strict: bool = False) -> None:
    """Assert, in a test, that a model passes its own assertions, reality and path checks.

    Raises ``AssertionError`` naming every failed assertion, so a normal pytest
    report shows what went wrong. An inconclusive result (an assertion's
    confidence interval straddles the bound) does not raise by default; it is
    reported through ``warnings.warn``.

    Parameters
    ----------
    path_or_layout : str or dict
        The model file or the layout dict.
    strict : bool, default False
        Treat an inconclusive result as a failure too.

    Returns
    -------
    None

    See Also
    --------
    nagasim.api.run_model : Runs the model without checking its assertions.

    Examples
    --------
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim.modelcheck import assert_model
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> assert_model(str(path)) is None
    True
    """
    results = check(path_or_layout)
    failed = [r for r in results if r.status == "fail"]
    inconclusive = [r for r in results if r.status == "inconclusive"]
    if strict:
        failed = failed + inconclusive
        inconclusive = []
    if failed:
        detail = "; ".join(f"{r.name}: {r.message}" for r in failed)
        raise AssertionError(f"model check failed ({len(failed)}): {detail}")
    for r in inconclusive:
        warnings.warn(f"model check inconclusive: {r.name}: {r.message}", stacklevel=2)


# ---------------------------------------------------------------------- CLI

def _junit_xml(all_results: list[tuple[str, list[Result]]]) -> str:
    """One <testsuite> per model file (classname = its stem), one <testcase>
    per Result: a <failure> child for "fail", a <skipped> child for
    "inconclusive" (JUnit has no native third state; "skipped" is the closest
    -- neither pass nor a hard failure)."""
    root = ET.Element("testsuites")
    for model_path, results in all_results:
        classname = Path(model_path).stem
        suite = ET.SubElement(root, "testsuite", name=classname,
                              tests=str(len(results)),
                              failures=str(sum(1 for r in results if r.status == "fail")),
                              skipped=str(sum(1 for r in results if r.status == "inconclusive")))
        for r in results:
            case = ET.SubElement(suite, "testcase", name=r.name, classname=classname)
            if r.status == "fail":
                failure = ET.SubElement(case, "failure", message=r.message)
                failure.text = r.message
            elif r.status == "inconclusive":
                skipped = ET.SubElement(case, "skipped", message=r.message)
                skipped.text = r.message
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _print_table(model_path: str, results: list[Result]) -> None:
    print(f"{model_path}:")
    if not results:
        print("  (no assertions, reality checks or path checks)")
        return
    for r in results:
        status = r.status.upper()
        detail = r.message if r.status != "pass" else "ok" + (f" ({r.note})" if r.note else "")
        print(f"  [{status}] {r.kind:13s} {r.name}: {detail}")


def _print_laws(model_path: str, workers: int, runs: dict | None = None) -> None:
    """The operational-law checks of one run with the model's own settings (D684). Warnings
    only: they never change the exit status, saturation can be the point of a model. An
    assertion batch run at the model's own replication count (in *runs*) is that same run,
    so it is reused rather than run again."""
    base = api.load_layout(model_path)
    own = api.run_settings(base, None, None, None)[0]
    run = (runs or {}).get(own)
    if run is None:
        run = api.run_model(base, seed=0, workers=workers)
    found = laws.checks(base, run) if run is not None else []
    if not found:
        return
    partial = [c for c in found if c["verdict"] != "passed"]
    for c in partial:
        print(f"  [WARN] {'law':13s} {c['name']}: {c['why']}")
    print(f"  laws: {len(found) - len(partial)} passed, {len(partial)} warning(s)")


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _ci_template(path: str, install: str = "pip install .") -> str:
    """The workflow text. *path* lands inside a double-quoted bash word (so a
    space is safe; a `"` or `$` in it would still be bash's to read), and the
    default install assumes the workflow runs in the product's own checkout --
    the package is not on PyPI (DEVLOG #217)."""
    text = (_TEMPLATE_DIR / "naga-models.yml").read_text(encoding="utf-8")
    return text.replace("{{MODELS_PATH}}", path).replace("{{INSTALL}}", install)


def _run_check(model_paths: list[str], *, junit: str | None, workers: int, strict: bool) -> int:
    all_results: list[tuple[str, list[Result]]] = []
    for path in model_paths:
        try:
            runs: dict = {}
            results = check(path, workers=workers, runs=runs)
        except (OSError, ValueError) as exc:
            print(f"naga check: {path}: {exc}", file=sys.stderr)
            return 2
        _print_table(path, results)
        _print_laws(path, workers, runs)
        all_results.append((path, results))

    if junit:
        Path(junit).write_text(_junit_xml(all_results), encoding="utf-8")

    failing_statuses = {"fail", "inconclusive"} if strict else {"fail"}
    any_failed = any(r.status in failing_statuses for _, results in all_results for r in results)
    return 1 if any_failed else 0


def main(argv=None) -> int:
    """`naga check MODEL.json ... [--junit OUT.xml] [--workers N] [--strict]` and
    `naga ci-template [--path DIR] [--install CMD]`, both dispatched here since both share
    this module's own argparse subparsers."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="naga",
                                     description="Model assertions and CI integration.")
    subs = parser.add_subparsers(dest="command", required=True)

    check_p = subs.add_parser("check",
                              help="run a model's assertions, reality checks and path checks")
    check_p.add_argument("models", nargs="+", help="model file(s) to check")
    check_p.add_argument("--junit", default=None, help="write a JUnit XML report here")
    check_p.add_argument("--workers", type=int, default=1)
    check_p.add_argument("--strict", action="store_true",
                         help="treat an inconclusive assertion (its CI straddles the bound) "
                              "as a failure too (exit 1)")

    tmpl_p = subs.add_parser("ci-template",
                             help="print a GitHub Actions workflow running naga check")
    tmpl_p.add_argument("--path", default="models/",
                        help="directory of *.json models to check (default models/)")
    tmpl_p.add_argument("--install", default="pip install .",
                        help='the install step (default "pip install .": '
                             "the product package from the repository checkout)")

    args = parser.parse_args(argv)
    if args.command == "ci-template":
        print(_ci_template(args.path, args.install))
        return 0
    return _run_check(args.models, junit=args.junit, workers=args.workers, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
