"""study_extensions: verified candidate modules for a study's model (nagasim/studyextensions.py)."""
from __future__ import annotations

from nagasim import studyextensions as ext

from .core import ToolError, _document_args, tool
from .study import _ID, _open, _read_layout, _sid, _study_errors

_document_args("study_extensions", study=_ID,
               apply="Name of the module to add (shifts, failures, arrival_peak); omit to list the verified candidates.",
               params="Optional parameters of the module, e.g. {\"mtbf\": 200}.")


@tool(read_only=True)
def study_extensions(study: str, apply: str | None = None, params: dict | None = None) -> dict:
    """Candidate extension modules for the study's model: shifts with a break, resource failures,
    an arrival peak. Without `apply`, lists the ones that validate, run and pass the law checks
    on the last version, with their effect on the goal KPIs in one short run (indicative only).
    With `apply`, answers the model layout with that module added: review it, then send it to
    study_step; this tool never steps the study.
    Example: study_extensions(study="naga-studies/cranes", apply="failures", params={"mtbf": 200}).

    Returns {"candidates": [{"name", "label", "purpose", "effort", "indicative"}]} or
    {"module", "layout", "next_step"}.
    """
    folder, s = _open(study)
    if not s["versions"]:
        raise ToolError("E_STUDY_NO_VERSION", "the study has no model version to extend",
                        "build a version with study_step first")
    if apply is None:
        with _study_errors():
            return {"candidates": ext.candidates(folder, s)}
    if params is not None and not isinstance(params, dict):
        raise ToolError("E_STUDY_BAD_VALUE", "params: must be an object", "send params as {name: value}")
    try:
        layout = ext.apply(apply, _read_layout(folder, s["versions"][-1]["id"]), **(params or {}))
    except ValueError as exc:
        raise ToolError("E_STUDY_BAD_VALUE", f"apply: {exc}", "fix apply or params and send it again") from None
    return {"module": apply, "layout": layout,
            "next_step": f'Review the layout, then study_step(study="{_sid(folder)}", layout=<it>, note="add {apply}").'}
