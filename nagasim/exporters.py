"""Exporter registry (ROADMAP 2.4 C): one `Exporter` entry per output format,
replacing the three separately-wired export paths (`core/export.export_workbook`,
`ui/results.py`'s CSV loop, `report.build_report`) with a single lookup a
plugin can add to (`nagasim.plugins`, `nagasim.exporters` entry-point group).

Top-level module beside `api.py`/`report.py`/`inputfit.py`: it needs pandas
(already a core dependency) but no PySide6, so it stays importable from a
headless CLI run (ARCHITECTURE 4.1's pandas-confinement rule already allows
pandas here).

    from nagasim.exporters import EXPORTER_REGISTRY
    EXPORTER_REGISTRY["xlsx"].write(results, "out.xlsx")

`Exporter.write(results, path, *, layout=None, title=None)`: every built-in
writer ignores the `layout`/`title` kwargs it does not need (via **_), except
`html` (needs `layout` for the report's model summary, `title` for its
heading) -- kept as *optional* keyword-only extras rather than a second
required parameter, so a plugin exporter that only ever needs
`(results, path)` (e.g. the example `markdown` exporter) can ignore them too.
`extension` is the file dialog / CLI hint; an empty string marks a directory
output (today, only `csv`: one file per result frame).

`needs_trace` (PHASE7 7.7, D201) marks a format drawn from the run's recorded
trace rather than from its results (the HTML player). Only such a writer is also
given `trace=`, and it raises ValueError without one; every other writer is called
exactly as before, so a plugin written against the three fields keeps working.
`naga run` records the trace when an `--export` asks for such a format; the
Results window and the `export` agent tool, which hold no trace, do not offer it.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from nagasim.core.export import export_csv_folder, export_workbook, with_datetimes

__all__ = ["Exporter", "EXPORTER_REGISTRY"]


@dataclass(frozen=True)
class Exporter:
    name: str          # "xlsx", "csv", "html", "json", a plugin's own name ("markdown")
    extension: str     # ".xlsx"; "" means *path* is a directory (one file per frame)
    write: Callable    # (results, path, *, layout=None, title=None) -> object (usually a Path)
    needs_trace: bool = False   # D201: the writer also takes `trace=` and refuses without one


def _write_xlsx(results, path, *, layout=None, **_):
    return export_workbook(results, path, layout=layout)


def _write_csv(results, path, *, layout=None, **_):
    # PHASE4 T10: a calendar model's CSVs carry the wall-clock column too.
    return export_csv_folder(with_datetimes(results, layout), path)


def _write_html(results, path, *, layout=None, title=None, **_):
    from nagasim.report import build_report  # local: report.py is a top-level module too, avoid a hard import cycle risk
    build_report(layout or {}, results, title=title, path=path)
    return Path(path)


def _write_json(results, path, *, layout=None, **_):
    """All nine frames as records, one JSON object keyed by frame name."""
    payload = {name: df.to_dict(orient="records")
               for name, df in with_datetimes(results, layout).items()
               if isinstance(df, pd.DataFrame)}
    Path(path).write_text(_json.dumps(payload, indent=2, default=str))
    return Path(path)


def _write_player(results, path, *, layout=None, title=None, trace=None, **_):
    """The shareable HTML player of the recorded replication (PHASE7 7.7, D201)."""
    if trace is None:
        raise ValueError("the player is drawn from the run's recorded trace: use "
                         "`naga run MODEL --player PATH`, or pass trace= (a TraceRecorder)")
    from nagasim.experiments import BASELINE    # local, like the report above
    from nagasim.player import build_player
    build_player(layout or {}, {BASELINE: trace}, results=results, title=title, path=path)
    return Path(path)


EXPORTER_REGISTRY: dict[str, Exporter] = {
    "xlsx": Exporter("xlsx", ".xlsx", _write_xlsx),
    "csv": Exporter("csv", "", _write_csv),
    "html": Exporter("html", ".html", _write_html),
    "json": Exporter("json", ".json", _write_json),
    "player": Exporter("player", ".html", _write_player, needs_trace=True),
}
