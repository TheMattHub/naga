"""Reading and preparing a model (Wave A): `read_layout`/`load_layout`, the
one read -> migrate -> validate -> flatten -> sd.expand pass (`prepare`), the
whole-model validators and `requires_run_length`."""
from __future__ import annotations

import json
from pathlib import Path

from nagasim.core import sd, submodels
from nagasim.core.orders import ORDERS_MODE
from nagasim.core.problems import Problem, problem
from nagasim.core.schema.graph import validate_graph_structured
from nagasim.core.schema.layout import validate_layout_structured
from nagasim.core.schema.migrate import migrate
from nagasim.core.schema.submodels import _library_clashes, validate_submodels_structured


def fatal_problems(problems: list[Problem]) -> list[Problem]:
    """The problems that must stop a load/run: by `Problem.severity`, never by
    sniffing the message text (bug #247 -- `W_RERANK_UNRANKED`/`W_UNIT_MISSING`
    carry no "warning: " text prefix, which made every text-sniffing caller
    treat them as fatal). Every caller that used to filter
    `validate_model()` strings by `not p.startswith("warning:")` calls this on
    `validate_model_structured()` instead."""
    return [p for p in problems if p.severity != "warning"]


def format_problems(problems: list[Problem]) -> list[str]:
    """The string form every existing caller expects: a warning always reads
    "warning: ...", derived from severity (not the other way around, #247) --
    a no-op for the messages that already spell it out, so the text every
    existing caller and test reads is unchanged."""
    return [p.message if p.severity != "warning" or p.message.startswith("warning: ")
            else f"warning: {p.message}" for p in problems]


def _prepare_structured(layout: dict) -> tuple[dict, list[Problem]]:
    """validate_model_structured's actual work, keeping the flattened, expanded
    layout instead of discarding it -- so `prepare()` (Wave A2) does not
    flatten/expand a second time to get what validation already built."""
    if not submodels.has_submodels(layout):
        # SD delays are validated EXPANDED (PHASE5 SD-2): the generated records
        # and hidden stages are what will run; identity for a non-SD layout.
        flat = sd.expand(layout)
        return flat, validate_layout_structured(flat) + validate_graph_structured(flat)
    problems = validate_submodels_structured(layout) + _library_clashes(layout)
    if any(p.severity != "warning" for p in problems):
        return layout, problems     # flattening a broken section would only add noise
    try:
        flat = submodels.flatten(layout)
    except submodels.SubmodelError as exc:
        # A check with its own permanent code kept its Problem (structural
        # arguments, PHASE4 B6); everything else is the generic E_SUBMODEL.
        return layout, problems + [
            found if isinstance(found, Problem)
            else problem("E_SUBMODEL", str(found), "/submodels",
                         hint="fix the submodel section, then validate again")
            for found in exc.items]
    flat = sd.expand(flat)
    return flat, problems + validate_layout_structured(flat) + validate_graph_structured(flat)


def validate_model_structured(layout: dict) -> list[Problem]:
    """Every problem a saved model can have, as coded Problems (ROADMAP 3.1
    item 1). `validate_model` is a thin formatter over this, so the strings
    every existing caller reads are unchanged."""
    return _prepare_structured(layout)[1]


def read_layout(path_or_dict) -> dict:
    """The raw (unmigrated) layout dict from a model file path, utf-8 (#253),
    or the dict itself. Only for the model file: a data source a model links
    to (csv/sql/json_url) goes through its own netguard-confined reader."""
    if isinstance(path_or_dict, dict):
        return path_or_dict
    return json.loads(Path(path_or_dict).read_text(encoding="utf-8"))


def load_layout(path_or_dict) -> dict:
    """A migrated layout from a model file path or a dict (Wave A2): the "read
    or take a dict, then migrate" idiom every top-level module used to copy.
    Not a run preparation -- a module about to RUN the layout wants `prepare`."""
    return migrate(read_layout(path_or_dict))


def prepare(path_or_dict) -> tuple[dict, list[Problem]]:
    """read -> migrate -> validate -> flatten -> sd.expand in ONE pass (Wave
    A2), shared by `GraphModel.load_from_json`, `SimSession`, `rl.SimEnv` and
    the experiment/optimize design points instead of each repeating the steps
    by hand (`SimSession` ran them twice per game start).

    Returns (flat, problems): *flat* is ready to run (migrated, submodels
    flattened, SD delays expanded -- identity for a plain model); *problems*
    is every Problem found, fatal or not (see `fatal_problems`). On a fatal
    submodel/schema error *flat* is only migrated, not flattened -- it must
    not be run."""
    return _prepare_structured(load_layout(path_or_dict))


def validate_model(layout: dict) -> list[str]:
    """`validate_model_structured` as strings: every problem a saved model can have.

    With submodels (ROADMAP 2.3 part B) the section's own rules are checked
    first, on the UNFLATTENED layout so the messages name definitions and the
    blocks inside them; then the FLATTENED layout goes through exactly the same
    layout and graph validation as any other model, so every existing check
    applies to the expanded instances (their names carry the `cell_1/` prefix).
    """
    return format_problems(validate_model_structured(layout))


def requires_run_length(layout: dict) -> bool:
    """True when the model cannot drain on its own, so a run length of 0/None
    would hang forever (ROADMAP 1.5 follow-up to P1.4's controller processes,
    which already never return on their own). Three endless event sources:
    a CREATE that never stops (`max_arrivals == 0`, whichever arrival mode),
    any resource failure, and a repeating capacity schedule (a one-shot one
    still runs out of points and returns, `Schedule.next_change_after` -> inf).

    Reads the FLATTENED layout, so an endless CREATE inside a submodel counts
    exactly like one at the top level."""
    if submodels.has_submodels(layout):
        try:
            layout = submodels.flatten(layout)
        except submodels.SubmodelError:
            return True     # cannot tell; asking for a run length is the safe answer
    layout = sd.expand(layout)
    for block in layout.get("blocks", []):
        # A CREATE in "Orders" mode always stops: the order book is finite and
        # `max_arrivals` means nothing to it (PHASE4 A3).
        if (block.get("type", "").upper() == "CREATE"
                and block.get("params", {}).get("arrival_mode") != ORDERS_MODE
                and block.get("params", {}).get("max_arrivals", 0) == 0):
            return True
    if layout.get("failures"):
        return True
    # A flow keeps re-evaluating itself at every series knot and every level
    # crossing, so a model with one never drains on its own (PHASE4 D1) --
    # exactly the same contract a declared failure has always had.
    if layout.get("flows") or layout.get("conversions") or layout.get("delays"):
        return True
    # A rate transition can cycle forever (S -> I -> S), and a pure agent model
    # keeps a wake-up at the horizon for its statistics (PHASE5 AB-1).
    if layout.get("populations"):
        return True
    for schedule in layout.get("schedules", []):
        if schedule.get("kind") == "capacity" and schedule.get("repeat"):
            return True
    return False
