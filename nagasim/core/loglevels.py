"""Run log levels (D1395): what a run records, by name.

Three levels, each a name for a pair of run settings that already exist:

    kpi      record_logs False, sampler off (timeseries_points 0): the summary frames only
    series   record_logs False, sampler as the model sets it: plus "timeseries" and "dashboard"
    full     record_logs True, sampler as the model sets it: plus the three per-event logs

and three profiles on top: study (kpi), explore (full, the editor's default) and replay (full; the
caller records the trace). A level never raises `timeseries_points` above the model's own value.

Precedence: a caller's level beats the model's `simulation.log_level`, which beats the model's
own `record_logs` / `timeseries_points`. With no level anywhere, those two settings apply exactly
as before levels existed, and a result is only labelled with the level they amount to.
"""
from __future__ import annotations

# Nothing here imports pandas: schema validation and the JSON Schema read this module.

# The summary columns whose value is computed FROM the per-event log (the queue-length and
# store-size snapshots) and so is blank or different with `record_logs` off; everything else
# comes from the run's collectors. `core.outputs` computes them and imports this table, held to
# the code by tests/test_perf_study_logs.py (exactly these columns differ between a logs-on
# and a logs-off run). A study that names one of them runs at full (`needed_level`).
LOG_DERIVED_COLUMNS = {
    "resource_summary": frozenset({"Queue Length Mode", "Queue Length Mode %", "Queue Len P95",
                                   "Queue Len P99", "Queue Len Std"}),
    "store_summary": frozenset({"Mode Size", "Mode Size %", "P95 Size", "P99 Size", "Std Size"}),
}
__all__ = ["LEVELS", "PROFILES", "FRAME_LEVEL", "COLUMN_LEVEL", "NotRecordedError", "resolve",
           "rank", "switches", "apply_level", "level_of", "named_level", "key_level", "needed_level",
           "label_of", "not_recorded", "unrecorded", "PREFIX_FRAME", "model_sets_own",
           "message"]

LEVELS = ("kpi", "series", "full")
PROFILES = {"study": "kpi", "explore": "full", "replay": "full"}
NAMES = (*LEVELS, *PROFILES)
# The result frames a level below their own does not record.
FRAME_LEVEL = {"timeseries": "series", "dashboard": "series",
               "entity_log": "full", "resource_log": "full", "store_log": "full"}
# The summary columns computed from the resource and store logs ("frame.column": level).
COLUMN_LEVEL = {f"{frame}.{col}": "full" for frame, cols in LOG_DERIVED_COLUMNS.items()
                for col in sorted(cols)}
# `flatten_kpis` prefixes of the frames that hold a log-derived column.
PREFIX_FRAME = {"resource": "resource_summary", "store": "store_summary"}


class NotRecordedError(KeyError):
    """A frame, column or KPI this run did not record at its level; the message names the level
    to re-run at. A KeyError, so a caller that catches a missing frame keeps working. Made by
    `of`, it also carries the *name*, the run's *level* and the level *needed*."""

    name: str | None = None
    level: str | None = None
    needed: str | None = None

    @classmethod
    def of(cls, name: str, level: str, needed: str) -> "NotRecordedError":
        error = cls(message(name, level, needed))
        error.name, error.level, error.needed = name, level, needed
        return error

    def __str__(self):          # KeyError quotes its argument; this one is a sentence
        return str(self.args[0]) if self.args else ""


def resolve(name) -> str:
    """The level *name* stands for: a level as is, a profile as its level."""
    if name in LEVELS:
        return name
    if name in PROFILES:
        return PROFILES[name]
    raise ValueError(f"log_level must be one of {', '.join(NAMES)}; got {name!r}")


def rank(level: str) -> int:
    return LEVELS.index(level)


def switches(level: str) -> dict:
    """The run settings *level* (a level, not a profile) sets, the ONE mapping of a level onto
    them: `apply_level` writes them into a layout and `GraphModel.load_from_json` onto the run's
    context for a model that names its own level. kpi switches the sampler off for every kind of
    run: `timeseries_points` 0 for a time-limited one, no `timeseries_interval` for one with no
    length (review I2). A level never sets either to sample more than the model asks."""
    out = {"record_logs": level == "full"}
    if level == "kpi":
        out.update(timeseries_points=0, timeseries_interval=None)
    return out


def apply_level(layout, level):
    """*layout* as a layout dict with the run settings of *level* (a level or a profile): the ONE
    place a level becomes `record_logs` and `timeseries_points`. The named level is written as
    `simulation.log_level` too, which is what the run and its result read. The caller's dict is
    never mutated."""
    from nagasim.core import schema
    level = resolve(level)
    out = dict(schema.read_layout(layout))
    out["simulation"] = {**(out.get("simulation") or {}), "log_level": level, **switches(level)}
    return out


def model_sets_own(layout) -> bool:
    """Whether *layout*'s file sets its own `simulation.log_level` or `record_logs`: a caller's
    default level (the editor's explore, the run_model tool's) then stands aside and the file's
    setting applies, as before levels existed."""
    sim = (layout.get("simulation") or {}) if isinstance(layout, dict) else {}
    return "log_level" in sim or "record_logs" in sim


def named_level(sim_settings) -> str | None:
    """The level a model's `simulation` section names, resolved, or None."""
    name = (sim_settings or {}).get("log_level")
    return None if name is None else resolve(name)


def level_of(sim_settings) -> str:
    """The level the two switches amount to: logs on is full; logs off with the sampler on is
    series; both off is kpi. The label of a run that names no level."""
    sim = sim_settings or {}
    if sim.get("record_logs", True):
        return "full"
    return "kpi" if sim.get("timeseries_points", 200) == 0 else "series"


def key_level(key) -> str:
    """The lowest level that records one KPI key, spelled as `flatten_kpis` writes it, or a raw
    log column a calibration target names (`entity_log/Throughput Time`)."""
    prefix, _, rest = str(key).partition("/")
    if FRAME_LEVEL.get(prefix) == "full":
        return "full"
    frame = PREFIX_FRAME.get(prefix)
    # The column is the last part: an object name may itself hold a "/".
    if frame and f"{frame}.{rest.rpartition('/')[2]}" in COLUMN_LEVEL:
        return "full"
    return "series" if prefix == "timeseries" else "kpi"


def needed_level(kpis=(), goals=()) -> str:
    """The lowest level that records every KPI key in *kpis* and every study goal in *goals*;
    a mix takes the highest. A tail goal (`share_above`) reads the entity or resource log."""
    if any(isinstance(g, dict) and g.get("share_above") is not None for g in goals or ()):
        return "full"
    return max((key_level(kpi) for kpi in kpis), key=rank, default="kpi")


def label_of(sim_settings) -> str:
    """The level a run's result carries: the one it named, else the one its switches amount to."""
    return named_level(sim_settings) or level_of(sim_settings)


def unrecorded(kpis, level) -> NotRecordedError | None:
    """The error for the first KPI key in *kpis* that a run at *level* (a level or a profile)
    does not record, or None. The one check a study's explicit level is held to."""
    level = resolve(level)
    for key in kpis:
        need = key_level(key) if isinstance(key, str) else "kpi"
        if rank(need) > rank(level):
            return NotRecordedError.of(key, level, need)
    return None


def not_recorded(level: str) -> dict:
    """{frame or "frame.column": level needed} for what a run at *level* leaves out."""
    return {name: need for name, need in {**FRAME_LEVEL, **COLUMN_LEVEL}.items()
            if rank(need) > rank(level)}


def message(name: str, level: str, needed: str) -> str:
    """The one plain line an agent or a person gets for data the run did not record."""
    return f"{name!r} was not recorded: this run was at level {level}; re-run with log_level='{needed}'"
