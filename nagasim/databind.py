"""Data bindings: model parameters read from real data (ROADMAP 3.4a).

The step from study tool to twin. A study hard-codes "service time ~ Exp(1/6)";
a twin says "service time is whatever the last month of the service-time column
says it is", and re-reads it on every run. Two model sections (schema v13)
describe that, and NOTHING here runs by itself: a caller asks for it
(`api.run_model(bind_data=True)`, `naga bind`, the UI's Bindings tab).

    "data_sources": [
      {"name": "visits", "kind": "csv", "location": "data/clinic_visits.csv"}
    ],
    "data_bindings": [
      {"name": "service", "source": "visits", "column": "service_minutes",
       "filter": {"clinic": "north", "day >=": 20},
       "transform": "fit", "target": "blocks.Consult.params.distribution",
       "enabled": true}
    ]

A source's `kind` is `csv` / `xlsx` / `sqlite` / `sql` / `json_url`; `location`
is a path relative to the MODEL FILE (never the process's working directory)
or, for the last two, a URL. `query` carries the SELECT for the two SQL kinds
and `sheet` the worksheet for `xlsx`; `options` is passed straight to the
pandas reader.

TRANSFORMS, one column of data in, one model value out:

| transform | value written |
|---|---|
| `mean` / `median` / `sum` / `count` / `last` | that statistic of the column |
| `quantile:<q>` | the q-quantile, q in [0, 1] |
| `value` | the first cell -- for a column that holds one setting |
| `rate_per:<unit>` | rows / observed span, as a rate per `<unit>` |
| `fit` | best-AIC distribution (`inputfit.fit_candidates`) |
| `empirical` | piecewise-uniform empirical spec (`inputfit.empirical_spec`) |
| `arrival_schedule:<bin>[:<cycle>]` | schedule points (`inputfit.arrival_rates`) |
| `table:<key column>[:<column-key column>]` | a lookup table's values (PHASE4 A1) |
| `series:<time column>` | a time-series input's samples (PHASE4 D2) |
| `orders` | the whole "orders" section, one order per row (PHASE4 A3) |

`fit` and `empirical` write a whole distribution, so their `target` names the
`...params.distribution` leaf and BOTH it and the sibling `dist_params` are
written. `arrival_schedule` targets a `schedules.<name>.points`. A `table:`
binding targets `tables.<name>.values` and rewrites that table's values in
place: the source rows are read as (key[, column key], value) and every cell
the declared table has must be covered, so a binding can refresh a routing or
tariff table from the plant without changing the model's shape. A `series:`
binding is the same idea for a PROFILE: it targets
`timeseries_inputs.<name>.values` and writes both the sample times and the
values out of two source columns, so yesterday's measured load or tomorrow's
price forecast replaces the authored curve outright (a profile has no fixed
shape to preserve, unlike a table's key grid). `orders` is
the one transform that REPLACES a section: an MES work-order export is already
one row per order, so it targets `orders` and maps source columns onto the
order fields with the binding's own `map` (`{"id": "WO_NUMBER"}`); a field with
no mapping falls back to a column of the same name. Every other
transform writes one scalar, and it must match the type the target already
holds (an integer capacity gets a rounded integer, never 4.7).

SAFETY, all of it deliberate:

* A `sql` or `json_url` source is REFUSED unless the CALLER passes a
  `NetworkPolicy` that allows it (`--allow-network` on a CLI, the server's
  start arguments, the editor's per-session consent dialog). A model file is
  untrusted content (ARCHITECTURE 7.4): opening one must not make outbound
  connections on its own, and a permission written INTO the file is stripped
  on load (`schema.CONSENT_KEYS`) rather than obeyed. Every URL that is
  allowed still goes through `nagasim.netguard` -- scheme, credentials,
  resolved addresses, redirects and a size cap.
* A caller that is acting for an LLM passes `confine_to`, and then every file
  a source reads must be under that root (`netguard.confine_path`). A `csv` /
  `xlsx` / `sqlite` `location` that is a UNC path (`\\\\server\\share\\...`) is
  refused the same way as `sql`/`json_url` (`E_DATA_NETWORK_REFUSED`) unless
  the caller allowed network, even with no `confine_to` at all: stat-ing one
  still opens an SMB connection that can leak NTLM credentials
  (`sources.confined`, DEVLOG #268).
* A model file never carries a credential. A literal password in a URL is a
  validation error (`E_DATA_CREDENTIALS`); `${NAGASIM_DATA_<NAME>}` anywhere
  in a location, query, option or filter value is expanded from the
  environment at resolve time. A model file may name ONLY variables with that
  prefix: a reference to any other name is refused (`E_DATA_ENV_NAME`) before
  the environment is looked at, so a file cannot read a variable the caller
  did not name for it (DEVLOG #335). The expanded value opens the source, and
  the text of a failure, a hint or a log line quotes the reference as written
  (DEVLOG #334), except a value shorter than four characters and a part of a
  value that a library extracts (the host of a URL held in a variable). A
  `NAGASIM_DATA_` value can still leave as DATA, through a query, an option, a
  filter or a location on a host the caller allowed: put there only what any
  model file you run may read.
* A `filter` is data, not code: `{"column": value}` or `{"column >=": value}`
  with the six comparison operators. There is no expression path here at all.
* Nothing raises. Every refusal and every failure -- missing file, bad query,
  empty column, wrong type -- is a structured `Problem` with an `E_DATA_*`
  code on the report, and the binding is simply not applied, so the run falls
  back to the value the model already had.

`resolve_bindings` is pure: it returns a NEW layout and never touches the one
it was given.
"""
from __future__ import annotations

from nagasim.examples import model_path as _bundled  # a model argument may be a bundled name

import argparse
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

from nagasim import api, experiments, inputfit, netguard, sources
from nagasim._cli import setup_cli
from nagasim.core import schema
from nagasim.core.orders import clashing_names, scalar_cell
from nagasim.core.problems import Problem, problem
from nagasim.core.schema import ModelValidationError
from nagasim.core.timeunits import factor

__all__ = ["BindingRecord", "BindingReport", "ENV_PREFIX", "READERS", "Reader", "expand_env",
           "read_rows", "resolve_bindings", "main"]

log = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# DEVLOG #335: the only environment variables a model file may name. The caller names a
# variable for model files by giving it this prefix; every other name is refused unread.
ENV_PREFIX = "NAGASIM_DATA_"
_FILTER_OPS = {
    "==": lambda s, v: s == v, "!=": lambda s, v: s != v,
    ">=": lambda s, v: s >= v, "<=": lambda s, v: s <= v,
    ">": lambda s, v: s > v, "<": lambda s, v: s < v,
}
REPORT_COLUMNS = ["Binding", "Source", "Kind", "Location", "Column", "Transform", "Target",
                  "Value", "Rows", "Source time", "Problem"]


class _BindingError(Exception):
    """A coded failure of ONE binding; caught per binding and turned into a
    Problem, so one bad source never stops the rest from resolving."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


@dataclass
class BindingRecord:
    """One binding's outcome: what was read, from where, and what it produced."""

    name: str
    source: str = ""
    kind: str = ""
    location: str = ""
    column: str = ""
    transform: str = ""
    target: str = ""
    value: object = None
    rows: int = 0
    source_time: str | None = None
    problem: Problem | None = None
    warnings: list = field(default_factory=list)   # applied, but with something skipped

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass
class BindingReport:
    """Every binding's outcome plus the overrides that were applied.

    `frame()` is what `api.run_model(bind_data=True)` attaches to its results
    as the "data_inputs" frame and what the report's "Data inputs" header
    section prints -- the provenance a twin's answer has to carry.
    """

    records: list[BindingRecord] = field(default_factory=list)
    overrides: list[dict] = field(default_factory=list)

    @property
    def problems(self) -> list[Problem]:
        return [r.problem for r in self.records if r.problem is not None]

    @property
    def ok(self) -> bool:
        return not self.problems

    def frame(self) -> pd.DataFrame:
        rows = [{"Binding": r.name, "Source": r.source, "Kind": r.kind, "Location": r.location,
                 "Column": r.column, "Transform": r.transform, "Target": r.target,
                 "Value": _printable(r.value), "Rows": r.rows, "Source time": r.source_time,
                 "Problem": r.problem.message if r.problem else ""}
                for r in self.records]
        return pd.DataFrame(rows, columns=REPORT_COLUMNS)


def _printable(value) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
        return text if len(text) <= 120 else text[:117] + "..."
    return "" if value is None else str(value)


# ---------------------------------------------------------------- environment
def expand_env(value, used: dict | None = None):
    """Expand every `${NAGASIM_DATA_<NAME>}` in *value* from the environment.

    Strings, and the values of a dict, are expanded; anything else passes
    through. This is the one place a model file's text reaches the environment
    (location, options, query, filter values), so the rule is enforced here: a
    name without `ENV_PREFIX` is refused before the environment is read, set
    or not (DEVLOG #335). A name that is not set is a coded failure, never an
    empty string silently pasted into a connection URL. *used*, when given,
    collects {value: "${NAME}"} of every reference expanded, so a failure can
    be scrubbed.
    """
    if isinstance(value, dict):
        return {k: expand_env(v, used) for k, v in value.items()}
    if not isinstance(value, str):
        return value

    def replace(match):
        name = match.group(1)
        if not name.startswith(ENV_PREFIX):
            raise _BindingError(
                "E_DATA_ENV_NAME",
                f"'{match.group(0)}' names an environment variable a model file may not read",
                f"a model file may reference only variables whose names start with "
                f"{ENV_PREFIX}; the person running the model sets one, for example "
                f"{ENV_PREFIX}DB_URL, and the source names that")
        if name not in os.environ:
            raise _BindingError("E_DATA_ENV", f"environment variable '{name}' is not set",
                                f"set {name} before running, or edit the source's location")
        if used is not None:
            used[os.environ[name]] = match.group(0)
        return os.environ[name]

    return _ENV_PATTERN.sub(replace, value)


# DEVLOG #334: the text of a failure, a hint or a log line quotes a `${NAME}` as written,
# not the value it expanded to. Our own messages quote the location as written; a message we
# do not write (netguard's refusals, pandas, sqlite3, urllib, SQLAlchemy, OSError) quotes what
# it was given, so every call that used an expanded value runs under `_guarded`, which puts
# the reference back before the text leaves. It does NOT stop a value leaving as DATA (a
# query that selects it, a filter that ranks it), nor a reader's own warning on stderr that
# quotes an option: that is why `expand_env` reads only the variables the caller named for
# model files (DEVLOG #335). (A warning is not captured here: capturing swaps the process's
# warning state, which is not thread-safe, and a capture another thread has open would leave
# ours installed for good.)
_MIN_REDACTED = 4          # a shorter value ("1", "on") would garble ordinary text
_READ_HINT = "check the source's location, query and column names"


def _spellings(value: str) -> set[str]:
    """The ways a library is likely to write *value* back: as is and lower-cased (netguard
    names a host in lower case, and the lower case of U+0130 is longer than it is), with the
    other path separator (Path() rewrites them on Windows), with backslashes doubled (repr of
    a Windows path) and percent-encoded (urllib.parse.quote)."""
    forms = {value, value.lower()}
    forms |= {form.replace("/", "\\") for form in forms} | {form.replace("\\", "/") for form in forms}
    forms |= {form.replace("\\", "\\\\") for form in forms}
    return forms | {quote(form, safe=safe) for form in forms for safe in ("", "/")}


def _hide(text: str | None, used: dict) -> str | None:
    """*text* with every key of *used* (a value, or a path text) replaced by what it maps to
    (the `${NAME}` that produced it, or the location as written), in any letter case; a text
    that also occurs as ordinary words there is replaced too. It cannot catch a value a
    library transforms in another way (a hash, a truncation, one character of it, a path
    rebuilt from parts), or one shorter than `_MIN_REDACTED` that is not part of a path."""
    shown = {spelling: reference for value, reference in used.items()
             if len(value) >= _MIN_REDACTED for spelling in _spellings(value)}
    if not text or not shown:
        return text
    # One group per spelling, as written (a lower-cased copy of U+0130 is two characters long),
    # found by which group matched. A reference already in the text is group 1 and matches
    # first, so a value that is a piece of its own name cannot garble it.
    spellings = sorted(shown, key=len, reverse=True)
    pattern = re.compile("|".join([_ENV_PATTERN.pattern] + [
        f"({re.escape(spelling)})" for spelling in spellings]), re.I)
    return pattern.sub(lambda m: m.group() if m.lastindex == 1 else shown[spellings[m.lastindex - 2]],
                       text)


def _scrubbed(exc: Exception, used: dict) -> Exception:
    """A failure to show in place of *exc*: the same class and code for a coded refusal
    (callers catch those), `E_DATA_READ` for a library's own, and no cause chain."""
    if isinstance(exc, sources.ReadError):
        return sources.ReadError(exc.code, _hide(exc.message, used), _hide(exc.hint, used),
                                 exc.status)
    if isinstance(exc, (_BindingError, netguard.GuardError)):
        return type(exc)(exc.code, _hide(exc.message, used), _hide(exc.hint, used))
    return _BindingError("E_DATA_READ", _hide(str(exc), used), _READ_HINT)


def _guarded(used: dict, what: str, call, *args):
    """`call(*args)`, with a failure it raises freed of what *used* maps. *what* names the
    call for the fixed text that stands in when the scrub itself fails (a value `quote`
    cannot encode): it holds no value."""
    try:
        return call(*args)
    except Exception as exc:
        try:
            error = _scrubbed(exc, used)
        except Exception:
            error = _BindingError("E_DATA_READ", f"{what} failed; the details are withheld, as "
                                  "they could not be checked for a secret", _READ_HINT)
    # Raised outside the handler: inside it, `__context__` would keep the original, whose
    # text quotes the value, for a caller that walks the chain.
    raise error


# ---------------------------------------------------------------- source reading
def _joined(location: str, base_dir) -> Path:
    path = Path(location)
    # A UNC spelling is absolute on Windows only: joined to the base on Linux or macOS it
    # would reach `sources.confined` as a local path, and the refusal depend on the system.
    if path.is_absolute() or netguard.is_unc_path(location):
        return path
    return Path(base_dir or ".") / path


def _file_time(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


def _read_source(source: dict, base_dir, policy: netguard.NetworkPolicy,
                 confine_to) -> tuple[pd.DataFrame, str | None]:
    """(frame, source timestamp) for one declared source. The text of a failure quotes a
    `${NAME}` it expanded as written, not as its value (DEVLOG #334)."""
    if schema.password_in_url(source.get("location") or ""):
        raise _BindingError("E_DATA_CREDENTIALS",
                            f"source '{source.get('name')}': the location embeds a password",
                            "write the whole URL as \"${NAGASIM_DATA_DB_URL}\" and set "
                            "that environment variable")
    used: dict[str, str] = {}
    what = f"source '{source.get('name')}' ('{source.get('location') or ''}')"
    return _guarded(used, what, _read_expanded, source, base_dir, policy, confine_to, used)


def _read_expanded(source: dict, base_dir, policy: netguard.NetworkPolicy, confine_to,
                   used: dict) -> tuple[pd.DataFrame, str | None]:
    """`_read_source` after the credential check; every value it expands lands in *used*."""
    reader, where, options = _open_reader(source, base_dir, policy, confine_to, used)
    rows, stamp = reader.read(source, where, options, policy, confine_to, used)
    return (rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)), stamp


# ---------------------------------------------------------------- the reader table (D213)
@dataclass(frozen=True)
class Reader:
    """One kind of data source. `read(source, where, options, policy, confine_to, used)`
    returns (rows or a frame, stamp); *where* is what `_open_reader` made of the location
    after guarding it by `target`: a confined `Path` ("file"), the URL ("url", "sql")."""

    kind: str
    read: Callable
    target: str = "file"                # what `location` is: "file" | "url" | "sql" | "connection"


def _open_reader(source: dict, base_dir, policy: netguard.NetworkPolicy, confine_to,
                 used: dict) -> tuple[Reader, object, dict]:
    """(reader, where, options) for one source: the ONE place a reader is looked up and its
    location guarded by the reader's `target`; nothing else calls a reader."""
    kind = source.get("kind")
    reader = READERS.get(kind)
    if reader is not None and reader.target == "connection":
        # A live source holds a connection's NAME and nothing to expand (D216): the
        # address and the credential are the caller's, resolved by `netguard`.
        return reader, netguard.connection_for(source, policy), source.get("options") or {}
    raw_location = source.get("location") or ""
    location = expand_env(raw_location, used)
    options = expand_env(source.get("options") or {}, used)
    if reader is None:
        raise _BindingError("E_DATA_SOURCE_KIND", f"unknown source kind '{kind}'",
                            f"use one of: {', '.join(schema.DATA_SOURCE_KINDS)}")
    if reader.target == "url":
        # Vetted here and again, hop by hop, by `netguard.fetch`: the URL comes from an
        # untrusted file.
        netguard.check_url(location, policy)
        return reader, location, options
    if reader.target == "sql":
        netguard.check_sql_url(location, policy)
        return reader, location, options
    # A path is hidden whole, as the two texts the checks and the readers are given: Path()
    # collapses "//", drops a trailing separator and resolves "..", so what they quote is
    # no spelling of the value (E1, E2). Only when a reference was expanded, so a plain
    # location keeps its full path in a message.
    full = _joined(location, base_dir)
    if location != raw_location:
        used[str(full)] = raw_location
    # Confined only when the caller asked for it: the desktop editor and the
    # plain CLI read whatever file the HUMAN pointed them at, an agent does not.
    # A UNC location is refused as network access regardless (sources.confined).
    path = sources.confined(full, confine_to, policy)
    if location != raw_location:
        used[str(path)] = raw_location
    if not path.exists():
        raise _BindingError("E_DATA_READ", f"no such file: {raw_location}",
                            "the location is relative to the model file")
    return reader, path, options


def _read_csv(source, path, options, policy, confine_to, used):
    return pd.read_csv(path, **options), _file_time(path)


def _read_xlsx(source, path, options, policy, confine_to, used):
    sheet = source.get("sheet")
    return pd.read_excel(path, sheet_name=sheet if sheet else 0, **options), _file_time(path)


_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_SQL_SELECT = re.compile(r"^[\s(]*(select|with)\b", re.IGNORECASE)


def _one_select(source: dict, used: dict) -> str:
    """The source's query, expanded, or a refusal: it must be ONE statement that starts with
    SELECT or WITH. Judged on the text, so it holds whatever a driver would do with it: no
    ATTACH, PRAGMA, COPY, CALL or SET, and nothing after a semicolon. It does not make a
    query harmless (a SELECT can still call what the database account may call): the account
    the caller connects with is the real bound."""
    query = expand_env(source.get("query") or "", used)
    bare = _SQL_LITERAL.sub("''", query).strip().rstrip(";").rstrip()
    if not _SQL_SELECT.match(bare) or ";" in bare:
        raise _BindingError("E_DATA_QUERY",
                            f"source '{source.get('name')}': its query must be one SELECT "
                            f"statement",
                            "start it with SELECT or WITH, with no comment before it and "
                            "nothing after a semicolon")
    return query


def _read_sqlite(source, path, options, policy, confine_to, used):
    stamp = _file_time(path)
    query = _one_select(source, used)
    with sources.open_sqlite(path, confine_to=confine_to, network=policy) as conn:
        return pd.read_sql_query(query, conn, **options), stamp


def _read_json_url(source, location, options, policy, confine_to, used):
    payload = json.loads(netguard.fetch(location, policy).decode("utf-8"))
    records = payload if isinstance(payload, list) else payload.get("data", payload)
    return pd.DataFrame(records), datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_sql(source, location, options, policy, confine_to, used):
    query = _one_select(source, used)       # before the driver is imported, let alone asked
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:
        raise _BindingError("E_DATA_READ", "a sql source needs the optional sqlalchemy package",
                            "pip install \"nagasim[sql]\" (or: pip install sqlalchemy), "
                            "or use a sqlite source") from exc
    engine = create_engine(location)
    with engine.connect() as conn:
        return (pd.read_sql_query(query, conn, **options),
                datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _read_live(source, conn, options, policy, confine_to, used):
    """A live source read once, outside a cycle: a feed of its own, opened and closed here.
    A caller that runs a cycle passes its own feed instead (`resolve_bindings(feed=)`)."""
    from nagasim import live

    with live.Feed([source], network=policy) as feed:
        return feed.rows(source.get("name"), settle=True)


# kind -> reader. Its keys are `schema.DATA_SOURCE_KINDS` (tests/test_readers.py); there is
# no entry-point group for it (D214): a reader runs with the caller's network consent.
READERS: dict[str, Reader] = {reader.kind: reader for reader in (
    Reader("csv", _read_csv),
    Reader("xlsx", _read_xlsx),
    Reader("sqlite", _read_sqlite),
    Reader("sql", _read_sql, target="sql"),
    Reader("json_url", _read_json_url, target="url"),
    Reader("rest", _read_live, target="connection"),
    Reader("mqtt", _read_live, target="connection"),
    Reader("opcua", _read_live, target="connection"),
)}


def read_rows(source: dict, base_dir=None, *, network: netguard.NetworkPolicy | None = None,
              confine_to=None) -> tuple[list[dict], str | None]:
    """(rows as plain dicts, stamp) of one source, through the same guarded way in as a
    binding: for a caller that wants a table, not a frame (the product's MES adapter)."""
    frame, stamp = _read_source(source, base_dir, network or netguard.NetworkPolicy(), confine_to)
    return _jsonable(frame.astype(object).where(frame.notna(), None).to_dict("records")), stamp


def _apply_filter(frame: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    """`{"col": v}` / `{"col >=": v}` against the frame -- comparisons only."""
    for key, wanted in (filters or {}).items():
        column, _, op = str(key).rpartition(" ")
        if not column:
            column, op = str(key), "=="
        if column not in frame.columns:
            raise _BindingError("E_DATA_FILTER", f"filter column '{column}' is not in the source",
                                f"available columns: {', '.join(map(str, frame.columns))}")
        compare = _FILTER_OPS.get(op)
        if compare is None:
            raise _BindingError("E_DATA_FILTER", f"unknown filter operator '{op}'",
                                f"use one of: {', '.join(_FILTER_OPS)}")
        used: dict[str, str] = {}
        wanted = expand_env(wanted, used)
        frame = frame[_guarded(used, f"filter on column '{column}'", compare, frame[column], wanted)]
    return frame


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame.columns:
        raise _BindingError("E_DATA_READ", f"column '{name}' is not in the source",
                            f"available columns: {', '.join(map(str, frame.columns))}")
    return frame[name]


def _numbers(series: pd.Series) -> np.ndarray:
    values = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)
    if values.size == 0:
        raise _BindingError("E_DATA_EMPTY", "the column holds no numeric value after filtering",
                            "check the filter, or point the binding at a numeric column")
    return values


# ---------------------------------------------------------------- transforms
def _transform(binding: dict, frame: pd.DataFrame, base_unit: str,
               layout: dict | None = None, warnings: list | None = None) -> list[tuple[str, object]]:
    """(override path, value) pairs one binding produces -- two for a
    distribution-writing transform, one for everything else. *layout* is the
    model as earlier bindings left it, which a `table:` binding reads its
    table's shape from."""
    name = binding.get("transform", "")
    target = binding.get("target", "")
    column = binding.get("column", "")

    if name == "count":
        return [(target, int(len(frame)))]
    if name.startswith("table:"):
        return [(target, _table_values(name, binding, frame, layout or {}))]
    if name.startswith("series:"):
        return _series_points(name, binding, frame)
    if name == "orders":
        return [(target, _orders(binding, frame, warnings))]

    series = _column(frame, column)
    if name == "value":
        if series.empty:
            raise _BindingError("E_DATA_EMPTY", "the column is empty after filtering",
                                "check the filter")
        value = series.iloc[0]
        return [(target, value.item() if hasattr(value, "item") else value)]
    if name == "last":
        return [(target, float(_numbers(series)[-1]))]

    values = _numbers(series)
    if name == "mean":
        return [(target, float(np.mean(values)))]
    if name == "median":
        return [(target, float(np.median(values)))]
    if name == "sum":
        return [(target, float(np.sum(values)))]
    if name.startswith("quantile:"):
        q = _float_arg(name, 1, "a quantile in [0, 1]")
        if not 0.0 <= q <= 1.0:
            raise _BindingError("E_DATA_BINDING_TRANSFORM", f"quantile {q} is outside [0, 1]",
                                "use quantile:0.9 for the 90th percentile")
        return [(target, float(np.quantile(values, q)))]
    if name.startswith("rate_per:"):
        unit = name.split(":", 1)[1]
        span = float(values.max() - values.min())
        if span <= 0:
            raise _BindingError("E_DATA_EMPTY", "the timestamps span no time, so no rate exists",
                                "give the column at least two distinct timestamps")
        # The column holds timestamps in the model's base time unit; convert
        # the span into `unit` (factor = base units per unit) and divide.
        return [(target, float(values.size) / (span / factor(unit, base_unit)))]
    if name in ("fit", "empirical"):
        spec = (_best_fit(values) if name == "fit" else inputfit.empirical_spec(values))
        base, _, leaf = target.rpartition(".")
        if leaf != "distribution" or not base:
            raise _BindingError(
                "E_DATA_BINDING_TARGET",
                f"a '{name}' binding must target a '...params.distribution' path, not '{target}'",
                "point the target at the block's \"distribution\" field; \"dist_params\" is "
                "written alongside it")
        return [(target, spec["distribution"]), (f"{base}.dist_params", spec["dist_params"])]
    if name.startswith("arrival_schedule:"):
        parts = name.split(":")
        bin_width = _float_arg(name, 1, "a bin width in base time units")
        cycle = float(parts[2]) if len(parts) > 2 and parts[2] else None
        return [(target, inputfit.arrival_rates(values, bin_width=bin_width, cycle=cycle).points)]

    raise _BindingError("E_DATA_BINDING_TRANSFORM", f"unknown transform '{name}'",
                        f"use one of: {', '.join(schema.DATA_TRANSFORMS)}")


def _table_values(transform: str, binding: dict, frame: pd.DataFrame, layout: dict):
    """A lookup table's "values" grid, read row by row out of the source
    (PHASE4 A1). `table:<key column>[:<column-key column>]`; the binding's own
    `column` holds the numbers. The DECLARED table fixes the shape -- every
    key (and column key) it has must appear in the data, so a binding refreshes
    a table's numbers and can never silently reshape the model."""
    parts = transform.split(":")
    key_column = parts[1] if len(parts) > 1 else ""
    column_key_column = parts[2] if len(parts) > 2 and parts[2] else None
    if not key_column:
        raise _BindingError("E_DATA_BINDING_TRANSFORM",
                            f"transform '{transform}' needs the source's key column",
                            "write it as 'table:<key column>' or "
                            "'table:<key column>:<column-key column>'")
    target = binding.get("target", "")
    base, _, leaf = target.rpartition(".")
    if leaf != "values" or not base.startswith("tables."):
        raise _BindingError("E_DATA_BINDING_TARGET",
                            f"a 'table:' binding must target 'tables.<name>.values', not '{target}'",
                            "point the target at the declared table's \"values\"")
    entry = next((t for t in layout.get("tables", []) if t.get("name") == base[len("tables."):]),
                 None)
    if entry is None:
        raise _BindingError("E_DATA_BINDING_TARGET", f"no table named '{base[len('tables.'):]}'",
                            "declare the table first; a binding refreshes its values")

    keys = _column(frame, key_column).tolist()
    numbers = _column(frame, binding.get("column", "")).tolist()
    columns = entry.get("columns")
    if columns is None:
        cells = dict(zip(keys, numbers))
        return [_table_cell(cells, key, entry) for key in entry.get("keys", [])]
    column_keys = _column(frame, column_key_column).tolist() if column_key_column else None
    if column_keys is None:
        raise _BindingError("E_DATA_BINDING_TRANSFORM",
                            f"table '{entry.get('name')}' is 2-D, so '{transform}' needs a "
                            f"column-key column",
                            "write it as 'table:<key column>:<column-key column>'")
    cells = {(k, c): v for k, c, v in zip(keys, column_keys, numbers)}
    return [[_table_cell(cells, (key, column), entry) for column in columns]
            for key in entry.get("keys", [])]


def _series_points(transform: str, binding: dict, frame: pd.DataFrame):
    """A time-series input's samples, out of a time column and a value column
    (PHASE4 D2). `series:<time column>`; the binding's own `column` holds the
    values. Two paths are written -- `times` and `values` -- so the profile is
    replaced whole. The declared entry must therefore already be in the
    irregular (`times`) shape: an override only ever changes a value the model
    already has, so a regular `start`/`step` series has no `times` to write."""
    parts = transform.split(":")
    time_column = parts[1] if len(parts) > 1 else ""
    if not time_column:
        raise _BindingError("E_DATA_BINDING_TRANSFORM",
                            f"transform '{transform}' needs the source's time column",
                            "write it as 'series:<time column>'")
    target = binding.get("target", "")
    base, _, leaf = target.rpartition(".")
    if leaf != "values" or not base.startswith("timeseries_inputs."):
        raise _BindingError(
            "E_DATA_BINDING_TARGET",
            f"a 'series:' binding must target 'timeseries_inputs.<name>.values', not '{target}'",
            "point the target at the declared series' \"values\"")
    times = _numbers(_column(frame, time_column))
    values = _numbers(_column(frame, binding.get("column", "")))
    if times.size != values.size:
        raise _BindingError("E_DATA_BINDING_TRANSFORM",
                            f"{times.size} times but {values.size} values",
                            "both columns must have a number in every row")
    order = np.argsort(times, kind="stable")
    return [(f"{base}.times", [float(t) for t in times[order]]),
            (target, [float(v) for v in values[order]])]


# The order fields an `orders` binding can map a source column onto; `id` is
# the only one a release list must have.
_ORDER_FIELDS = ("id", "release", "due", "quantity", "entity_type", "sequence", "priority",
                 "source")
_ORDER_NUMBERS = ("release", "due", "quantity", "priority")


def _orders(binding: dict, frame: pd.DataFrame, warnings: list | None = None) -> list:
    """The whole "orders" section, one order per source row (PHASE4 A3).

    Orders are the main thing an MES feeds a twin, and a work-order export is
    already one row per order -- so unlike every other transform this one
    REPLACES a section rather than writing one value. The binding's `map`
    renames columns onto the order fields; a field with no mapping falls back
    to a source column of the same name, so a file that already uses the
    field names needs no `map` at all. Every other column becomes a free attribute of the
    order, as `orders_from_csv` does.
    """
    if binding.get("target") != "orders":
        raise _BindingError("E_DATA_BINDING_TARGET",
                            f"an 'orders' binding must target 'orders', not "
                            f"'{binding.get('target')}'",
                            "an orders binding replaces the whole order book")
    mapping = binding.get("map") or {}
    if not isinstance(mapping, dict):
        raise _BindingError("E_DATA_BINDING_TRANSFORM", "an orders binding's \"map\" must be an "
                                                        "object",
                            "write {\"id\": \"WO_NUMBER\", \"due\": \"DUE_DATE\"}")
    columns = {field: str(mapping.get(field, field)) for field in _ORDER_FIELDS}
    if columns["id"] not in frame.columns:
        raise _BindingError("E_DATA_EMPTY", f"the source has no order-id column "
                                            f"'{columns['id']}'",
                            "map it with {\"id\": \"<the source's id column>\"}")
    present = {field: column for field, column in columns.items() if column in frame.columns}
    # Any other column is a free attribute, as in orders_from_csv (#677).
    extras = [c for c in frame.columns if c not in present.values() and c != "time_unit"]
    # #681: a column named like an entity field is dropped; #682: so is one that is no
    # attribute name ("Customer Name", "Unnamed: 0", "__class__"), which validation refuses.
    skipped = sorted(set(clashing_names(extras)) | {
        str(c) for c in extras if not str(c).isidentifier() or str(c).startswith("_")})
    if skipped and warnings is not None:
        warnings.append(problem(
            "W_ORDER_FIELD", f"binding '{binding.get('name', '')}': column(s) "
            f"{', '.join(skipped)} skipped: each names a field of the entity itself or is "
            f"no valid attribute name", "/data_bindings",
            hint="rename it with the binding's map; an attribute is a Python identifier "
                 "not starting with _"))
    extras = [c for c in extras if str(c) not in skipped]
    entries = []
    for _, row in frame.iterrows():
        oid = str(row[present["id"]]).strip()
        if not oid:
            continue
        entry: dict = {"id": oid}
        for field, column in present.items():
            if field == "id":
                continue
            value = row[column]
            if pd.isna(value):
                continue
            if field in _ORDER_NUMBERS:
                number = float(value)
                entry[field] = int(number) if field == "quantity" else number
            else:
                entry[field] = str(value)
        attributes = {str(c): scalar_cell(row[c]) for c in extras
                      if not pd.isna(row[c]) and str(row[c]).strip()}
        if attributes:
            entry["attributes"] = attributes
        entries.append(entry)
    if not entries:
        raise _BindingError("E_DATA_EMPTY", "the source holds no order row after filtering",
                            "check the filter, or the id column")
    return entries


def _table_cell(cells: dict, key, entry: dict) -> float:
    if key not in cells:
        raise _BindingError("E_DATA_EMPTY",
                            f"table '{entry.get('name')}': the source has no row for {key!r}",
                            "the data must cover every key the declared table has")
    return float(cells[key])


def _float_arg(transform: str, index: int, what: str) -> float:
    parts = transform.split(":")
    try:
        return float(parts[index])
    except (IndexError, ValueError) as exc:
        raise _BindingError("E_DATA_BINDING_TRANSFORM",
                            f"transform '{transform}' needs {what}",
                            f"write it as '{parts[0]}:<number>'") from exc


def _best_fit(values: np.ndarray) -> dict:
    fit = inputfit.best_fit(values)
    if fit is None:
        raise _BindingError("E_DATA_EMPTY", "no distribution could be fit to this column",
                            "use the 'empirical' transform instead")
    return {"distribution": fit.distribution, "dist_params": fit.dist_params}


# ---------------------------------------------------------------- type checking
def _current(layout: dict, path: str):
    """The value the target currently holds, via the override grammar itself --
    so a path this model cannot address fails here, before anything is written."""
    try:
        return experiments.value_at(layout, path)
    except ModelValidationError as exc:
        raise _BindingError("E_DATA_BINDING_TARGET", "; ".join(exc.problems),
                            "see the override path grammar (Experiments window's path picker)") from None


def _jsonable(value):
    """Plain Python for anything numpy handed back, so a resolved layout still
    round-trips through `json.dumps` (`naga bind --write`, the editor's
    save, a process-pool worker's pickle)."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _checked(value, current, path: str):
    """*value* as the target can actually hold it, or a coded type failure."""
    value = _jsonable(value)
    if current is None or isinstance(current, type(value)) and not isinstance(current, bool):
        return value
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
    elif isinstance(current, int) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(round(value))            # an integer field (a capacity) gets an integer
    elif isinstance(current, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    elif isinstance(current, str) and isinstance(value, str):
        return value
    elif isinstance(current, (list, dict)) and isinstance(value, type(current)):
        return value
    raise _BindingError(
        "E_DATA_TYPE",
        f"'{path}' holds a {type(current).__name__}, but the binding produced a "
        f"{type(value).__name__}",
        "pick a transform whose result matches the field, or point the binding elsewhere")


# ---------------------------------------------------------------- entry point
def resolve_bindings(layout: dict, base_dir=None, *, network: netguard.NetworkPolicy | None = None,
                     confine_to=None, feed=None) -> tuple[dict, BindingReport]:
    """Resolve every enabled data binding and return (new layout, report).

    *layout* is never mutated. Each binding is resolved independently: one that
    fails contributes a `Problem` to the report and leaves the model's own
    stored value in place, so a twin whose database is down still runs on
    yesterday's parameters instead of not running at all.

    *network* is the caller's outbound permission (default: none at all, so
    every `sql`/`json_url` source is refused). *confine_to*, when given, is the
    one directory a file-backed source may be read from. *feed*, when given,
    is the caller's open `live.Feed`: a live source is then answered from it
    (`feed.rows`), so a cycle reads its subscriptions without reconnecting.
    """
    policy = network or netguard.NetworkPolicy()
    base = current = schema.migrate(layout)
    declared = {s.get("name"): s for s in base.get("data_sources", [])}
    report = BindingReport()
    frames: dict[str, tuple[pd.DataFrame, str | None]] = {}
    base_unit = (base.get("simulation") or {}).get("time_unit", "minutes")

    for binding in base.get("data_bindings", []):
        if not binding.get("enabled", True):
            continue
        source_name = binding.get("source", "")
        source = declared.get(source_name) or {}
        record = BindingRecord(name=binding.get("name", ""), source=source_name,
                               kind=source.get("kind", ""), location=source.get("location", ""),
                               column=binding.get("column", ""),
                               transform=binding.get("transform", ""),
                               target=binding.get("target", ""))
        try:
            if source_name not in declared:
                raise _BindingError("E_DATA_BINDING_SOURCE",
                                    f"unknown data source '{source_name}'",
                                    f"declared: {', '.join(sorted(declared)) or '(none)'}")
            if source_name not in frames and feed is not None \
                    and source.get("kind") in schema.CONNECTION_SOURCE_KINDS:
                rows, stamp = feed.rows(source_name)
                frames[source_name] = (pd.DataFrame(rows), stamp)
            if source_name not in frames:
                frames[source_name] = _read_source(source, base_dir, policy, confine_to)
            frame, stamp = frames[source_name]
            filtered = _apply_filter(frame, binding.get("filter"))
            record.rows, record.source_time = int(len(filtered)), stamp
            pairs = _transform(binding, filtered, base_unit, current, record.warnings)
            if source.get("kind") in schema.CONNECTION_SOURCE_KINDS \
                    and binding.get("transform") == "value" and isinstance(pairs[0][1], str):
                # What a plant sends is data (D225): a text from one is never written into
                # a model field, where it could be read as a name or an expression.
                raise _BindingError("E_DATA_TYPE",
                                    "a live source's text is not written into the model",
                                    "bind a number or a true/false value; an order book "
                                    "takes texts through the 'orders' transform")
            # Applied one binding at a time, against the layout as EARLIER
            # bindings left it: a binding that rewrites a whole distribution
            # changes which `dist_params` fields exist, and a later binding
            # into one of them must be checked against what is actually there.
            overrides = [{"path": path, "value": _checked(value, _current(current, path), path)}
                         for path, value in pairs]
            current = experiments.apply_overrides(current, overrides)
            record.value = overrides[0]["value"] if len(overrides) == 1 else {
                o["path"].rpartition(".")[2]: o["value"] for o in overrides}
            report.overrides.extend(overrides)
        except (_BindingError, netguard.GuardError, sources.ReadError) as exc:
            # All carry (code, message, hint); a refused URL or path reads on
            # the report exactly like any other coded binding failure.
            record.problem = problem(exc.code, f"binding '{record.name}': {exc.message}",
                                     f"/data_bindings/{len(report.records)}", hint=exc.hint)
            log.warning("data binding '%s': %s", record.name, exc.message)
        except Exception as exc:                        # a reader's own failure mode
            record.problem = problem("E_DATA_READ", f"binding '{record.name}': {exc}",
                                     f"/data_bindings/{len(report.records)}",
                                     hint=_READ_HINT)
            log.warning("data binding '%s': %s", record.name, exc)
        report.records.append(record)

    return current, report


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    """`naga bind model.json [--allow-network ...] [--write resolved.json]`:
    resolve the model's data bindings and print what each one read."""
    setup_cli()

    parser = argparse.ArgumentParser(
        prog="naga bind",
        description="Resolve a model's data bindings and show what each one read.")
    parser.add_argument("model", type=_bundled, help="path to the model JSON, or a bundled model's name (naga examples)")
    netguard.add_arguments(parser)
    parser.add_argument("--write", help="write the resolved model here")
    args = parser.parse_args(argv)

    path = Path(args.model)
    layout = api.load_layout(path)
    resolved, report = resolve_bindings(layout, path.parent,
                                        network=netguard.policy_from_args(args))

    frame = report.frame()
    print(frame.to_string(index=False) if not frame.empty else "no enabled data bindings")
    for entry in report.problems + [w for r in report.records for w in r.warnings]:
        print(f"{entry.code}: {entry.message}")
    if args.write:
        Path(args.write).write_text(json.dumps(resolved, indent=2))
        print(f"Resolved model written to {args.write}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
