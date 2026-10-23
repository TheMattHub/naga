"""Data bindings, warm starts and distribution fitting (the engine half of the twin tools).
"""
from __future__ import annotations

import numpy as np

from nagasim import experiments, inputfit, uncertainty
from nagasim.core import sections
from nagasim.core.document import ModelDocument
from nagasim.core.schema import ModelValidationError

from .core import (
    MAX_PROBLEMS, MAX_ROWS, ToolError, _budget, _document_args, _edit_result, _finite, _open,
    note_asked, tool, workspace,
)

_document_args("resolve_data_bindings", model="Workspace name of the model to resolve.",
               apply="Write the resolved values into the model (an undoable edit). False just "
                     "reports what the bindings WOULD read.")


@tool()
def resolve_data_bindings(model: str, apply: bool = False) -> dict:
    """Read the model's data bindings and report what each one produced.

    A binding is "this parameter comes from that column of that file", so this
    is how a model's numbers come from measurements instead of guesses. Nothing
    here raises: a source that is missing, refused or unreadable comes back as
    a `problem` on its own row and simply is not applied.

    Files must be under the workspace root, and a `sql`/`json_url` source is
    read only if the person who started this server allowed network access.
    There is no argument that changes either: a refusal is final for you, and
    its `problem` tells the HUMAN what to re-run if they want it.

    Call with `apply=true` to write the values into the model (undoable), or
    leave it false to preview. `run_model` does not resolve bindings by
    default, but its own `bind_data=true` argument resolves `series:` and
    `orders` bindings at RUN TIME -- those two never get written into
    the model file even with `apply=true` here; see the E_DATA_BINDING_TARGET
    hint below.

    A `value` is what the binding read, and it is data from outside: it is cut
    to 200 rows and 200 characters a text, and `value_cut` then says how much
    was left out ({"rows", "characters"}; null when nothing was). `apply=true`
    still writes the whole value.

    Returns {"model", "applied", "bindings": [{"name", "source", "kind",
    "location", "column", "transform", "target", "value", "value_cut", "rows",
    "source_time", "problem"}], "problems": n}.
    """
    doc, before = _open(model)
    from nagasim import databind

    ws = workspace()
    base_dir = doc.path.parent if doc.path is not None else ws.root
    resolved, report = databind.resolve_bindings(doc.to_layout(), base_dir,
                                                 network=ws.network,
                                                 confine_to=ws.root)
    rows = []
    for r in report.records[:MAX_ROWS]:
        cut = {"rows": 0, "characters": 0}
        rows.append({"name": r.name, "source": r.source, "kind": r.kind, "location": r.location,
                     "column": r.column, "transform": r.transform, "target": r.target,
                     "value": _bounded(r.value, cut),
                     "value_cut": cut if cut["rows"] or cut["characters"] else None,
                     "rows": r.rows, "source_time": r.source_time,
                     "problem": r.problem.message if r.problem else None})
    out = {"model": model, "applied": bool(apply), "bindings": rows,
           "problems": len(report.problems)}
    if not apply:
        return out
    with doc.transaction():             # every binding lands in ONE undo step
        for override in report.overrides:
            _apply_override(doc, override)
    return _edit_result(doc, before, out)


MAX_TEXT = 200      # characters of one text in a binding's value, in a tool's answer


def _bounded(value, cut: dict):
    """*value* as a tool's answer may carry it: a list cut to `MAX_ROWS` items and a text to
    `MAX_TEXT` characters, at any depth, with what was left out added to *cut*. What a
    binding read is data from outside the model, and an LLM reads every answer."""
    if isinstance(value, str):
        cut["characters"] += max(0, len(value) - MAX_TEXT)
        return value[:MAX_TEXT]
    if isinstance(value, list):
        cut["rows"] += max(0, len(value) - MAX_ROWS)
        return [_bounded(item, cut) for item in value[:MAX_ROWS]]
    if isinstance(value, dict):
        return {key: _bounded(item, cut) for key, item in value.items()}
    return value


# The record sections a binding writes back (core/sections.py `bindable`).
_BINDABLE = tuple(s.name for s in sections.SECTIONS if s.bindable)


def _apply_override(doc: ModelDocument, override: dict) -> None:
    """Write one resolved binding into the document through its own setters, so
    the edit is undoable and the editor sees it (agent_tools rule 1)."""
    path = override.get("path", "")
    layout = experiments.apply_overrides(doc.to_layout(), [override])
    section = path.partition(".")[0]
    if section in _BINDABLE:
        doc.set_records(section, layout[section])
    elif section == "blocks":
        # The block whose params changed: the path's second segment may itself
        # contain dots, so compare against the document's own block names.
        rest = path.partition(".")[2]
        name = next((n for n in sorted(doc.blocks, key=len, reverse=True)
                     if rest == n or rest.startswith(n + ".")), None)
        block = next((b for b in layout["blocks"] if b["name"] == name), None)
        if block is None:
            raise ToolError("E_DATA_BINDING_TARGET", f"no block for override path '{path}'",
                            "check the target against the model's block names")
        doc.set_params(name, block["params"])
    elif section == "simulation":
        doc.set_simulation(**layout["simulation"])
    elif section in ("timeseries_inputs", "orders"):
        # D146: series:/orders bindings are bulk data, not a scalar to write
        # into the model file -- they bind at run time only.
        raise ToolError("E_DATA_BINDING_TARGET", f"cannot apply an override to '{path}'",
                        "series: and orders bindings bind at run time only -- call run_model "
                        "with bind_data=true")
    else:
        raise ToolError("E_DATA_BINDING_TARGET", f"cannot apply an override to '{path}'",
                        "bindings may target blocks, resources, sets, variables, schedules, "
                        "failures, tables or simulation settings")


_document_args("set_initial_state", model="Workspace name of the model to edit.",
               entities="[{\"block\", \"count\", \"e_type\"?, \"age\"?, \"in_service\"?, "
                        "\"remaining\"?, \"attributes\"?}] -- entities already in each block.",
               variables="{name: value} the run starts from.",
               resources="{name: {\"down_until\": t}} for a resource that starts failed.",
               time="Timestamp of the snapshot this state came from (recorded, never shifts "
                    "the clock).")


@tool()
def set_initial_state(model: str, entities: list | None = None, variables: dict | None = None,
                      resources: dict | None = None, time: float = 0.0) -> dict:
    """Give the model a WARM START: work already in the system at t=0.

    This is what makes a model a twin rather than a study. Entities are
    injected into the named block in list order as if they had just arrived
    there; `age` is how long an entity has already been in the system (its
    reported time in system includes that history); `in_service: k` means the
    first k are already being served, holding `remaining[i]` if you know the
    residual service times -- give them when you do, since a fresh sample is
    only exact for an exponential service.

    A warm-started model needs no warm-up, and `run_model` defaults it to 0.
    Pass no arguments to clear the initial state.

    Returns the usual edit answer plus {"entities": total injected}.
    """
    doc, before = _open(model)
    state: dict = {}
    if time:
        state["time"] = float(time)
    if entities:
        state["entities"] = [dict(e) for e in entities]
    if variables:
        state["variables"] = dict(variables)
    if resources:
        state["resources"] = {k: dict(v) for k, v in resources.items()}
    doc.set_initial_state(state)
    total = sum(int(e.get("count", 0) or 0) for e in state.get("entities", []))
    return _edit_result(doc, before, {"entities": total})


_document_args("fit_distribution",
               path="CSV/TSV/XLSX file under the workspace root holding the sample.",
               values="An inline list of numbers, instead of a file.",
               column="Column name in the file; defaults to the first numeric column.",
               candidates="Distribution families to try; omit for all of them.",
               top="How many ranked fits to return.")


@tool(read_only=True)
def fit_distribution(path: str | None = None, values: list | None = None,
                     column: str | None = None, candidates: list | None = None,
                     top: int = 5) -> dict:
    """Fit candidate distributions to observed data and rank them, so a block's
    duration comes from measurements instead of a guess.

    Give either a file path under the workspace root or an inline list of
    numbers. The ranking is by AIC (lower is better) with KS/AD statistics
    alongside. Apply the winner by setting the block's `distribution` and
    `dist_params` to the returned `spec`.

    Returns {"n", "dropped", "summary", "fits": [{"name", "params", "aic", "ks",
    "ad", "warnings", "spec"}]}.
    """
    if (path is None) == (values is None):
        raise ToolError("E_BAD_INPUT", "give exactly one of `path` or `values`",
                        "fit_distribution(path='times.csv') or fit_distribution(values=[1.2, ...])")
    if path is not None:
        loaded = inputfit.load_column(workspace().resolve(path, must_exist=True), column)
        sample, dropped = loaded.values, loaded.dropped
    else:
        sample, dropped = np.asarray(values, dtype=float), 0
    if sample.size < 2:
        raise ToolError("E_TOO_FEW_VALUES", f"only {sample.size} usable value(s)",
                        "fitting needs at least a couple of dozen observations to mean anything")
    fits = inputfit.fit_candidates(sample, candidates=candidates)[:max(1, int(top))]
    return {
        "n": int(sample.size), "dropped": int(dropped),
        "summary": {k: _finite(v) if isinstance(v, float) else v
                    for k, v in inputfit.describe(sample).items()},
        "fits": [{"name": f.distribution,
                  "params": {k: _finite(v) if isinstance(v, (int, float)) else v
                             for k, v in f.dist_params.items()},
                  "aic": _finite(f.aic), "ks": _finite(f.ks_stat), "ad": _finite(f.ad_stat),
                  "warnings": list(f.warnings),
                  "spec": {"distribution": f.distribution, "dist_params": f.dist_params}}
                 for f in fits],
    }


_document_args("input_uncertainty", model="Workspace name of the model.",
               inputs="One entry per input whose data is uncertain: {\"path\": a block's "
                      "\"...params.distribution\" field, or \"schedules.<name>.points\" of an "
                      "arrival schedule; \"data\": a CSV/TSV/XLSX file under the workspace root; "
                      "\"column\": optional, the first numeric column by default; for a "
                      "schedule also \"bin_width\" and \"cycle\" in base time units}.",
               kpis="KPI keys to report; default every system/ KPI.",
               resamples="Bootstrap resamples of the data (2 to 200).",
               replications="Replications per resample (2 to 50); every resample runs on "
                            "the same seeds.",
               run_length="Run length in base time units.", warmup="Warm-up time.",
               seed="Base seed of the runs; the resampling has its own stream from it.",
               max_seconds="Wall-clock budget.",
               overrides="Optional change, as in what_if: every KPI row is then the changed model "
                         "minus the baseline, both run per resample on the same seeds (twice the "
                         "runs); nothing is saved to the model.")


@tool()
def input_uncertainty(model: str, inputs: list, kpis: list | None = None, resamples: int = 20,
                      replications: int = 5, run_length: float | None = None,
                      warmup: float | None = None, seed: int = 0,
                      max_seconds: float | None = None, overrides: list | None = None) -> dict:
    """How much of each KPI's spread comes from the data its inputs were fitted to.

    Each input's data is resampled with replacement and refitted in the family of its
    best fit (an arrival schedule's timestamps are resampled by whole cycles), and the
    model runs once for the fit to all the data and once per resample, all on the same
    replication seeds, so resamples differ only in their inputs. Nothing is saved to the
    model. The runs are (resamples + 1) x replications, at most 2000.

    Per KPI: "run_ci", the 95% interval over the replications; "input_interval", the
    interval with the inputs' uncertainty too (it always holds run_ci; a KPI the inputs
    do not move keeps zero width); "input_share", the inputs' part of the variance of the
    KPI's mean, the inputs' part being the variance of the resample means on common
    random numbers (more replications shrink the rest, only more data shrinks it). The
    intervals rest on "resamples_used" resamples, said in "notes"; a resample that could
    not be refitted is counted in "failed" and left out.

    With overrides (a change, as what_if takes it) each row is the change's difference
    instead ("measure": "difference"): the baseline and the changed model run per resample on
    the same seeds, so the interval is the difference's, not a level's. A later what_if on this
    model quotes the latest answer in its "input_note".

    Returns {"model", "completed", "measure", "overrides", "resamples", "resamples_used", "failed", "replications",
    "runs", "seed", "inputs": [{"path", "kind", "n", "point"}], "kpis": [{"kpi", "mean",
    "run_ci", "input_interval", "input_share", "resamples_used"}], "notes"}.
    """
    specs = []
    for entry in inputs or []:
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("data"):
            raise ToolError("E_BAD_INPUT", "each input needs a \"path\" and a \"data\" file",
                            "inputs=[{\"path\": \"blocks.<name>.params.distribution\", "
                            "\"data\": \"times.csv\"}]")
        loaded = inputfit.load_column(workspace().resolve(entry["data"], must_exist=True),
                                      entry.get("column"))
        specs.append({"path": entry["path"], "values": loaded.values.tolist(),
                      "bin_width": entry.get("bin_width"), "cycle": entry.get("cycle")})
    try:
        out = uncertainty.input_uncertainty(
            workspace().document(model).to_layout(), specs, kpis=kpis,
            resamples=max(2, min(int(resamples), 200)),
            replications=max(2, min(int(replications), 50)), seed=seed,
            run_length=run_length, warmup=warmup, should_stop=_budget(max_seconds),
            overrides=overrides or None)
    except ModelValidationError as exc:
        raise ToolError("E_BAD_OVERRIDE", "; ".join(exc.problems),
                        "check the input and override paths (list_override_paths shows the valid ones)")
    except ValueError as exc:
        raise ToolError("E_BAD_INPUT", str(exc), "fix the inputs or the counts") from None
    rows = [{k: _finite(v) if isinstance(v, float) else v for k, v in row.items()}
            for row in out["kpis"][:MAX_ROWS]]
    # The answer itself is kept, so what_if can quote it and the what-next rules read it (D737).
    note_asked(model, {"tool": "input_uncertainty", "inputs": [s["path"] for s in specs],
                       "input_args": [{k: e[k] for k in ("path", "data", "column", "bin_width", "cycle")
                                       if e.get(k) is not None} for e in inputs],
                       "measure": out["measure"],
                       "overrides": out["overrides"], "resamples_used": out["resamples_used"],
                       "replications": out["replications"],
                       "kpis": [{k: r[k] for k in ("kpi", "mean", "input_interval", "input_share")}
                                for r in rows] if out["completed"] else []})
    return {"model": model, **out, "kpis": rows}


_document_args("get_plant_timeline", model="Workspace name of the model the mapping is for.",
               mapping="Plant mapping (*.plantmap.json), a path relative to the workspace root.",
               log="Recorded plant log (one JSON object a line, as `naga watch --record` "
                   "writes), a path relative to the workspace root. Empty: only check the "
                   "mapping.")


@tool(read_only=True)
def get_plant_timeline(model: str, mapping: str, log: str = "") -> dict:
    """Check a plant mapping against a model and, given a recorded plant log,
    say what the plant looked like at the log's last instant.

    A plant mapping says what each key a plant sends asserts about the model
    (which block an item is in, how many units of a resource are busy, what a
    level holds). The log's observations become the same trace events a run
    emits, so `blocks` and `resources` read exactly like a run's playback.

    Both files are data under the workspace root. This tool opens no
    connection and has no argument that could: watching a live plant is the
    person's own `naga plant watch` with their connections file.

    `counts` says what was dropped, repaired or made up, by reason; `problems`
    carries the mapping's errors (then nothing is replayed) and the replay's
    warnings.

    Returns {"model", "signals", "problems"} and, with a log, {"observations",
    "events", "truncated", "now", "stamp", "entities", "counts", "blocks":
    {block: items}, "resources": [{"name", "state", "busy", "capacity"}]}.
    """
    from nagasim import plant
    from nagasim.core.playback import Timeline, resource_rows

    doc, _before = _open(model)
    layout = doc.to_layout()
    loaded, problems = plant.load(workspace().resolve(mapping, must_exist=True))
    problems = problems or plant.check(loaded, layout)
    out = {"model": model, "signals": len((loaded or {}).get("signals", []))
           if isinstance(loaded, dict) and isinstance(loaded.get("signals", []), list) else 0,
           "problems": [p.as_dict() for p in problems[:MAX_ROWS]]}
    if problems or not log:
        return out
    observations, problems = plant.read_log(workspace().resolve(log, must_exist=True))
    events, mapper = plant.replay(loaded, layout, observations)
    state = Timeline(events).state_at(mapper.now)
    report = mapper.report()
    out.update(
        observations=report["observations"], events=report["events"],
        truncated=report["truncated"], now=report["now"], stamp=mapper.stamp(),
        entities=report["entities"], counts=report["counts"],
        blocks=dict(sorted(state["counts"].items())[:MAX_ROWS]),
        resources=resource_rows(state)[:MAX_ROWS],
        problems=[p.as_dict() for p in (problems + mapper.problems())[:MAX_ROWS]])
    return out


# ============================================================ the twin cycle (PHASE7 7.12, D273)
# Two READ-ONLY tools over the result files of a twin the PERSON started. Neither takes an
# argument: the directory is the server's `--twin DIR` start flag, and an agent can start
# no twin, open no connection and name no file through them.
_TWIN_TEXT = 300        # characters of one text of a result in a tool answer


def _cut(value, depth: int = 0):
    """*value* out of a result file, cut to what a tool answer may hold: the file is only
    known to be JSON of format 1."""
    if isinstance(value, dict) and depth < 6:
        return {str(key)[:_TWIN_TEXT]: _cut(item, depth + 1)
                for key, item in list(value.items())[:MAX_ROWS]}
    if isinstance(value, list) and depth < 6:
        return [_cut(item, depth + 1) for item in value[:MAX_ROWS]]
    if isinstance(value, str):
        return value[:_TWIN_TEXT]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    return _finite(value) if isinstance(value, float) else None


def _twin_result() -> dict:
    from nagasim import twin

    directory = workspace().twin_dir
    if directory is None:
        raise ToolError(
            "E_NO_TWIN", "this server was given no twin to read",
            "the person running the server starts a twin (`naga twin run ... --out DIR`) and "
            "this server with `naga mcp --twin DIR`; no tool argument can choose one")
    doc, problems = twin.read_result(directory)
    if doc is None:
        raise ToolError(problems[0].code, problems[0].message, problems[0].hint)
    return _cut(doc)


def _listed(value, limit: int = MAX_ROWS) -> list:
    return value[:limit] if isinstance(value, list) else []


@tool(read_only=True)
def get_twin_monitor() -> dict:
    """What the plant looks like now, as the running twin last wrote it.

    A twin is started by a person (`naga twin run`), reads their plant through
    their connections and writes a result every few seconds; this tool reads
    that result and nothing else. `sources` says whether each source is live
    and how old its last word is; `view` holds the items in each block, each
    resource's state and the levels at model time `at` (`approximate` is true
    when the plant names no items, so counts are estimates; `counters` is what
    the plant itself counted as having left a block since the twin first saw
    that counter, observed and not estimated); `problems` are
    coded, with what the twin did about each. Check `written` before trusting
    the picture: a twin that stopped leaves its last result behind, and one
    that runs writes every `cadence.every` seconds (null: it is run one step
    at a time by a scheduler). `at` is model time, in `model.time_unit` from
    `model.epoch` (null where the source has no epoch).

    Returns {"cycle", "written", "started", "cadence": {"every",
    "forecast_every"}, "model": {"name", "hash", "time_unit", "epoch"}, "at", "stamp",
    "sources": [{"name", "state", "age", "lost", "gaps"}], "view": {"approximate",
    "wip", "blocks", "in_service", "resources", "levels", "counters"}, "mapped", "problems":
    [{"code", "severity", "message", "path", "hint"}]}.
    """
    doc = _twin_result()
    return {**{key: doc.get(key) for key in ("cycle", "written", "started", "cadence", "model",
                                              "at", "stamp", "view", "mapped")},
            "sources": _listed(doc.get("sources")),
            "problems": _listed(doc.get("problems"), MAX_PROBLEMS)}


@tool(read_only=True)
def get_twin_forecast() -> dict:
    """The running twin's latest forecast, and how its earlier ones compare
    with what the plant then did.

    `forecast.status` is "ok", "running", "skipped" (a source is down),
    "refused" (the model cannot be started from the plant's state: `reason`
    and the monitor's problems say which kind), "failed" or "none"; with any
    status but "ok" the record shown is the last good one. The record names
    what it was computed from (`state` and `model` hashes, `seed`, `from`,
    `horizon`, `replications`), so it can be reproduced; `finished` and `took`
    (seconds) say when it could first be read and how long it ran. `kpis` are
    end-of-horizon means with a confidence interval (`lb`, `ub`) and the 5-95%
    band of single replications; `points` are predictions at stated times.
    `comparison` sets each prediction whose time has come beside the observed
    value: `inside` says whether it fell in the 5-95% band. `approximate` is
    about counts of items inferred where the plant names none (`system/WIP`
    then rests on estimates); a `block/<name>/out` row is what the plant's own
    counter counted and is observed either way. `scores` sums those
    comparisons by series and lead (how far ahead, in model time): `coverage`
    is the share inside the band (`nominal`, 0.9, when the model is right; a
    row with `whole` true holds whole numbers only and covers more than that
    on a right model, so compare its `pinball`), `bias` the mean of observed
    minus forecast mean, `error` the mean absolute error, `pinball` the
    quantile loss of the band (lower is better). Never quote a row without
    `basis` (forecasts, points, `missed` because the twin was late or a source
    not ready, `lost` because a counter lost count, since when; `same_seed`
    forecasts are not independent samples), and say `reason` when there are no
    rows. Readings overlap in time: this is evidence in distribution, not a
    test. When `forecast.actions` is there, the forecast ASSUMES actions a
    person took through the twin's outbox are in the plant (`assumes` says so
    in words; `get_twin_recommendations` lists them under `standing`): say so
    whenever you quote such a forecast.

    Returns {"cycle", "written", "model", "forecast": {"status", "reason",
    "state", "model", "seed", "from", "stamp", "horizon", "replications",
    "approximate", "finished", "took", "kpis": [...], "points": [...]},
    "comparison": [{"state",
    "series", "at", "mean", "q05", "q95", "observed", "observed_at", "inside"}],
    "scores": {"nominal", "basis": {"forecasts", "same_seed", "points", "missed",
    "lost", "since", "first", "last"}, "reason", "rows": [{"series", "lead",
    "points", "missed", "lost", "whole", "coverage", "bias", "error",
    "pinball"}]}}.
    """
    doc = _twin_result()
    forecast = doc.get("forecast") if isinstance(doc.get("forecast"), dict) else {}
    scores = doc.get("scores") if isinstance(doc.get("scores"), dict) else {}
    from nagasim import monitor         # the one wording (D281)

    assumed = _listed(forecast.get("actions"), 20)
    return {**{key: doc.get(key) for key in ("cycle", "written", "model")},
            "forecast": {**forecast, "kpis": _listed(forecast.get("kpis")),
                         "points": _listed(forecast.get("points")),
                         **({"assumes": monitor.ASSUMES.replace("{n}", str(len(assumed)))}
                            if assumed else {})},
            "comparison": _listed(doc.get("comparison"), 100),
            "scores": {**scores, "rows": _listed(scores.get("rows"), 300)}}


@tool(read_only=True)
def get_twin_recommendations() -> dict:
    """What the running twin recommends now, and what became of what it
    recommended before. Read-only: this tool cannot deliver, approve, reject
    or address a recommendation, and no tool can.

    The person who started the twin gave it candidate actions and one KPI
    (`kpi`, with `better`: "higher" or "lower"). After each forecast the twin
    runs every candidate from the same plant state on the same random numbers
    (`crn`) and sets it beside doing nothing. An item has its `action`
    ({"verb", "target", "value"}), `baseline` and `with_action` (means of the
    KPI), `effect` (the mean difference) with its 95% interval (`low`, `high`),
    `confidence` (the share of replications in which the action did better)
    and a `verdict`: "better" (the interval excludes no effect), "no_effect"
    (it includes it: do not present this as a gain), "worse", "failed" or
    "standing" (already taken). Only "better" items have a `rank`, an `id` and
    a `status`: "proposed" (shown, not delivered), "delivered" (written to the
    person's own outbox, unanswered), "taken", "rejected", "expired" or
    "dropped". With `status` "none", say `reason`: "no_forecast" (a source is
    down or the state was refused: see `get_twin_forecast`), "none_better",
    "kpi", "replications" or "failed". `standing` lists the actions a person
    took, which every later forecast now assumes until a person ends one
    (`ended` lists those). `effects` is what the plant's
    own counter then showed over one horizon (`observed`), and whether that fell
    in the 5-95% band the twin gave (`inside_interval`); "missed", "lost" and
    "not_observable" say why there is no number. This is what the model says,
    in distribution: not a causal claim about the plant, and one effect outside
    its band proves little. A proposal is acted on by a person, in their own
    queue or in the editor; if asked to approve or send one, say that you
    cannot.

    Returns {"cycle", "written", "model", "recommend": {"kpi", "better",
    "level", "crn", "measurable", "status", "reason", "state", "seed",
    "replications", "from", "stamp", "took", "reproducible", "items": [...],
    "standing": [{"id", "action", "since"}], "ended": [{"id", "action",
    "since", "ended"}], "effects": [{"id", "action",
    "status", "observed", "inside_interval", "from", "until", "q05", "q95"}],
    "outbox": {"sink", "state", "why", "records", "replies"}}}; `recommend` is
    null for a twin that was given no candidates.
    """
    doc = _twin_result()
    said = doc.get("recommend") if isinstance(doc.get("recommend"), dict) else None
    return {**{key: doc.get(key) for key in ("cycle", "written", "model")},
            "recommend": None if said is None else {
                **said, "items": _listed(said.get("items"), 20),
                "standing": _listed(said.get("standing"), 20),
                "ended": _listed(said.get("ended"), 20),
                "effects": _listed(said.get("effects"), 20)}}
