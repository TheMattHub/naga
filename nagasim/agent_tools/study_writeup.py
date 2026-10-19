"""study_writeup and study_slides: a paper-style report and a slide deck from the study folder, on request."""
from __future__ import annotations

from nagasim import studywriteup

from .core import _document_args, tool
from .study import _ID, _linked, _open, _sid, _study_errors

_document_args("study_writeup", study=_ID,
               discussion="Your own discussion text for section 8 (plain text, escaped in the page); omit for none.")
_document_args("study_slides", study=_ID)


@tool()
@_linked
def study_writeup(study: str, discussion: str | None = None) -> dict:
    """Write a paper-style report of the study on request (STRESS-aligned sections: abstract, objective,
    model logic, data and assumptions, experimentation, verification and validation, results, limitations,
    discussion, references), built from the study folder. `discussion` is your own interpretation, placed
    in section 8. Returns {"html": the page with figures, "markdown": the same text without figures},
    paths relative to the workspace root. writeup.md carries the study's own text as written (Markdown,
    not escaped for HTML), so treat it as text to read, not to embed in a page."""
    folder, s = _open(study, write=True)
    with _study_errors():
        html, md = studywriteup.writeup(folder, s, discussion)
    return {"html": f"{_sid(folder)}/package/{html.name}", "markdown": f"{_sid(folder)}/package/{md.name}"}


@tool()
@_linked
def study_slides(study: str) -> dict:
    """Make a slide deck of the study on request: one HTML file, 16:9 slides (title and question, the
    answer, options compared, goals, model, assumptions, validation and precision, sensitivity when it was
    run, next steps). Scroll on screen; print to PDF gives one slide per page. Returns {"path"}."""
    folder, s = _open(study, write=True)
    with _study_errors():
        path = studywriteup.slides(folder, s)
    return {"path": f"{_sid(folder)}/package/{path.name}"}
