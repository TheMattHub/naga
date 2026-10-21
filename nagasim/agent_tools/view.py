"""view_model: a picture of a model in the workspace, for an agent that can only read JSON (D681)."""
from __future__ import annotations

import base64

from nagasim import flowchart

from .core import ToolError, _document_args, tool, workspace

FORMATS = ("png", "svg", "text")

_document_args("view_model", model="Workspace name of the model.",
               format="png (an image, the default), svg (drawing source) or text (a list of blocks and links).")


@tool(read_only=True)
def view_model(model: str, format: str = "png") -> dict:
    """See a model as a picture: its blocks, each where it sits in the editor's process view, and the
    links between them. Use it to check that the model looks like the process you meant to build.

    Returns {"model", "format", "text"} (every block with its type, then every link) plus "image" for
    png; svg answers {"model", "format", "svg"} instead. Without matplotlib a png request answers with
    the text and a "note", as format "text".
    """
    if format not in FORMATS:
        raise ToolError("E_BAD_ARGUMENT", f"format must be one of {', '.join(FORMATS)}, not {format!r}",
                        "send png, svg or text")
    geom = flowchart.geometry(workspace().document(model).to_layout())
    out = {"model": model, "format": format}
    if format == "svg":
        out["svg"] = flowchart.to_svg(geom, model)
        return out
    # ponytail: the text lists every block, so a model of thousands of blocks answers thousands of
    # lines (study_view is the same); cut it like the paged tools do if such a model turns up.
    out["text"] = flowchart.to_text(geom)
    if format == "png":
        png = flowchart.to_png(geom, model)
        if png is None:
            out.update(format="text", note="install nagasim[plot] for images")
        else:
            out["image"] = {"mime_type": "image/png", "data": base64.b64encode(png).decode("ascii")}
            if geom["width"] > 2 * 1600:   # drawn at under half size: names are too small to read
                out["note"] = "the model is wide, so the image is scaled down; read the text for names"
    return out
