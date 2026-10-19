"""study_animation: an animated GIF of a study version running, on request."""
from __future__ import annotations

import time

from nagasim import studyanim

from .core import _document_args, tool
from .study import _ID, _log, _open, _save, _sid, _study_errors

_document_args("study_animation", study=_ID, version="Version id, e.g. v002; omit for the last.",
               scenario="Name of a scenario of the model to animate instead of the baseline.")


@tool()
def study_animation(study: str, version: str | None = None, scenario: str | None = None) -> dict:
    """Make an animated GIF of one replication of a version (the step's stored seed): boxes shaded by
    utilisation, queue counts and a clock. On request only; it needs nagasim[plot] (matplotlib).

    Returns {"path": the GIF relative to the workspace root, "frames"}.
    """
    started = time.monotonic()
    folder, s = _open(study)
    with _study_errors():
        path, frames = studyanim.gif(folder, s, version, scenario)
    _log(s, "study_animation", started)
    _save(folder, s)
    return {"path": f"{_sid(folder)}/{path.relative_to(folder).as_posix()}", "frames": frames}
