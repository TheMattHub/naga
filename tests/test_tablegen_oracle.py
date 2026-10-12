"""Independent oracle for W7 Q5 "Models generated from tables" (PHASE6-6D-W7-PLAN,
D128). Written BEFORE `nagasim.tablegen` exists, from the plan text and the
model's own conventions (`docs/ARCHITECTURE.md` sections 5.16 orders/sequences,
5.29 deterministic mode) -- not from the (nonexistent) implementation.
Failures here are the spec, not a bug report; the coordinator reconciles real
names/shapes once Q5 lands.

Adapters (the one place that knows tablegen's real call shape):
    from nagasim import tablegen
    tablegen.generate(source) -> layout   # source: folder of CSVs, or an .xlsx path
CLI: `naga generate TABLES -o model.json`, `naga generate --template out.xlsx`.
Guarded with `nagasim.cli.COMMANDS` containing "generate" -- an unknown
subcommand falls through to the Qt editor and hangs under pytest (the same
ceiling `test_modeldiff_oracle.py`/`test_modelcheck_oracle.py` already guard).

Column names (plan text's own guesses; kept as module constants below so the
coordinator can diff them against the real implementation in one place):
    Stations: name, resource, capacity, distribution, param1, param2, node, x, y
    Products: entity_type, arrival_distribution, arrival_param1, max_arrivals
    Routings: product, step, station, process_time

Reading choices/assumptions this oracle bakes in:
- **`start_time` added to Products** beyond the plan text's four guessed
  columns. Without a per-product arrival offset, two products released at
  t=0 would race for a shared station in whatever order CREATE blocks happen
  to be scheduled in the same instant -- a tie the plan text does not pin
  down. CREATE already has a `start_time` param (ARCHITECTURE section on
  CREATE's own params), so a generated CREATE plausibly exposes it; this is
  the one addition the coordinator should look at first when reconciling.
- **No distance/travel table** is given, so a job's move from one station to
  the next is assumed to take 0 time (the natural default absent one) --
  stated here because the exact flow-time numbers below depend on it.
- **Case 1 is built to have NO contention at all** (the plan text allows
  either "absent or easy to compute"; absent is simpler and still exercises
  the shared station S2): station process times are Constant 2/3/4 (S1/S2/S3)
  and Product A starts at t=0 (arrivals 0, 10, 20), Product B at t=6
  (arrivals 6, 16, 26). A's S2 hold windows are [2,5], [12,15], [22,25]; B's
  are [6,9], [16,19], [26,29] -- disjoint, so every job flows through with no
  queueing anywhere (S1 and S3 are each used by one product only).
- `simulation.deterministic` is set as a `run_model` override (D119's own
  documented call form), not baked into the generated layout, since every
  distribution here is Constant anyway and this keeps the oracle from
  assuming what `generate()` puts in the `simulation` section.
- "no fatal problems" reads `schema.validate_model_structured`'s own fatal
  test (`severity != "warning"`), the same predicate `nagasim/cli.py`'s
  `validate` subcommand and Q3's oracle use.
"""
from __future__ import annotations

import csv
import json

import pytest

# -- module constants: the guessed table schema, for the coordinator --------

STATIONS_COLUMNS = ["name", "resource", "capacity", "distribution", "param1", "param2",
                    "node", "x", "y"]
PRODUCTS_COLUMNS = ["entity_type", "arrival_distribution", "arrival_param1", "max_arrivals",
                    "start_time"]  # start_time: this oracle's addition, see module docstring
ROUTINGS_COLUMNS = ["product", "step", "station", "process_time"]


# -- adapters: the one place that knows tablegen's/the CLI's real shape -----

def _generate(source):
    from nagasim import tablegen
    return tablegen.generate(source)


def _validate(layout):
    from nagasim.core import schema
    return schema.validate_model_structured(layout)


def _fatal(problems):
    return [p for p in problems if getattr(p, "severity", "error") != "warning"]


def _run_cli(args):
    from nagasim.cli import main
    return main(args)


def _cli_wired_for(token):
    """Guard, not a feature probe: an unregistered `nagasim.cli.main` subcommand
    falls through to the Qt editor's `app.exec()` and hangs forever under
    pytest. Checks the COMMANDS registry (ARCH-REVIEW-PLAN B4) rather than
    sniffing main()'s source text as before."""
    from nagasim.cli import COMMANDS
    return token in COMMANDS


def _require_cli(token):
    if not _cli_wired_for(token):
        pytest.fail(f"naga {token} is not wired into nagasim.cli.main yet (W7 Q5 pending)")


def _diff_ignoring_moves(a, b):
    """Semantic equality that ignores pure canvas noise (Q1's own `moved`
    kind), falling back to a structural comparison if `modeldiff` is
    unavailable for some reason (it should not be, Q1 already shipped)."""
    try:
        from nagasim import modeldiff
    except ImportError:
        return _deep_equal_ignoring_layout(a, b)
    changes = modeldiff.diff(a, b)
    return [c for c in changes if getattr(c, "kind", None) != "moved"]


def _deep_equal_ignoring_layout(a, b):
    def strip(doc):
        doc = json.loads(json.dumps(doc))
        for blk in doc.get("blocks", []):
            blk.pop("position", None)
            blk.pop("layout_position", None)
        doc["blocks"] = sorted(doc.get("blocks", []), key=lambda b: b["name"])
        doc["links"] = sorted(doc.get("links", []), key=lambda l: json.dumps(l, sort_keys=True))
        return doc
    return [] if strip(a) == strip(b) else ["differs"]


# -- table fixtures: the 2-product, 3-station case with no contention -------

_STATION_ROWS = [
    {"name": "S1", "resource": "S1", "capacity": 1, "distribution": "Constant",
     "param1": 2, "param2": "", "node": "", "x": "", "y": ""},
    {"name": "S2", "resource": "S2", "capacity": 1, "distribution": "Constant",
     "param1": 3, "param2": "", "node": "", "x": "", "y": ""},
    {"name": "S3", "resource": "S3", "capacity": 1, "distribution": "Constant",
     "param1": 4, "param2": "", "node": "", "x": "", "y": ""},
]

_PRODUCT_ROWS = [
    {"entity_type": "A", "arrival_distribution": "Constant", "arrival_param1": 10,
     "max_arrivals": 3, "start_time": 0},
    {"entity_type": "B", "arrival_distribution": "Constant", "arrival_param1": 10,
     "max_arrivals": 3, "start_time": 6},
]

_ROUTING_ROWS = [
    {"product": "A", "step": 1, "station": "S1", "process_time": ""},
    {"product": "A", "step": 2, "station": "S2", "process_time": ""},
    {"product": "B", "step": 1, "station": "S2", "process_time": ""},
    {"product": "B", "step": 2, "station": "S3", "process_time": ""},
]


def _write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="ascii") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _write_csv_tables(tmp_path, stations=_STATION_ROWS, products=_PRODUCT_ROWS,
                       routings=_ROUTING_ROWS):
    tmp_path.mkdir(parents=True, exist_ok=True)   # coordinator: test mechanics only
    _write_csv(tmp_path / "Stations.csv", STATIONS_COLUMNS, stations)
    _write_csv(tmp_path / "Products.csv", PRODUCTS_COLUMNS, products)
    _write_csv(tmp_path / "Routings.csv", ROUTINGS_COLUMNS, routings)
    return str(tmp_path)


def _write_xlsx_tables(path, stations=_STATION_ROWS, products=_PRODUCT_ROWS,
                        routings=_ROUTING_ROWS):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet_name, columns, rows in (("Stations", STATIONS_COLUMNS, stations),
                                       ("Products", PRODUCTS_COLUMNS, products),
                                       ("Routings", ROUTINGS_COLUMNS, routings)):
        ws = wb.create_sheet(sheet_name)
        ws.append(columns)
        for row in rows:
            ws.append([row.get(c, "") for c in columns])
    wb.save(path)
    return str(path)


# -- case 1: generated model validates and its deterministic run matches hand math

def test_generated_model_validates_and_runs_to_hand_derived_numbers(tmp_path):
    layout = _generate(_write_csv_tables(tmp_path))

    problems = _validate(layout)
    assert _fatal(problems) == [], f"unexpected fatal problems: {_fatal(problems)}"

    from nagasim import api
    results = api.run_model(layout, deterministic=True, run_length=100, warmup=0)
    assert results is not None

    ent = results["entity_summary"]
    row_a = ent[ent["Entity Type"] == "A"].iloc[0]
    row_b = ent[ent["Entity Type"] == "B"].iloc[0]
    # A: S1 (2) + S2 (3) = 5, no queueing (see module docstring's contention analysis)
    assert (row_a["Total Created"], row_a["Total Completed"]) == (3, 3)
    assert row_a["Avg Time"] == pytest.approx(5.0)
    # B: S2 (3) + S3 (4) = 7, no queueing
    assert (row_b["Total Created"], row_b["Total Completed"]) == (3, 3)
    assert row_b["Avg Time"] == pytest.approx(7.0)

    sysdf = results["system"]
    def metric(name):
        return float(sysdf.loc[sysdf["Metric"] == name, "Value"].iloc[0])
    assert metric("Total Entities Completed") == 6
    # (3*5 + 3*7) / 6 = 6.0 exactly
    assert metric("Avg Throughput Time") == pytest.approx(6.0)


# -- case 2: the same tables via .xlsx give the identical layout ------------

def test_xlsx_and_csv_tables_generate_identical_layout(tmp_path):
    csv_layout = _generate(_write_csv_tables(tmp_path / "csv_tables"))
    xlsx_layout = _generate(_write_xlsx_tables(tmp_path / "tables.xlsx"))
    assert _diff_ignoring_moves(csv_layout, xlsx_layout) == []


# -- case 3: a Routings row naming an unknown station is a clear error ------

def test_unknown_station_in_routing_errors_clearly(tmp_path):
    bad_routings = _ROUTING_ROWS + [{"product": "A", "step": 3, "station": "S9",
                                      "process_time": ""}]
    source = _write_csv_tables(tmp_path, routings=bad_routings)
    try:
        layout = _generate(source)
    except ValueError as exc:
        assert "S9" in str(exc)
        return
    problems = _validate(layout)
    assert any("S9" in (p.message or "") for p in problems), \
        f"expected a problem naming the unknown station S9, got: {problems}"


# -- case 4: `generate --template` writes a workbook with header rows -------

def test_generate_template_writes_workbook_headers(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    _require_cli("generate")
    out = tmp_path / "template.xlsx"
    code = _run_cli(["generate", "--template", str(out)])
    assert code == 0
    assert out.exists()

    wb = openpyxl.load_workbook(str(out))
    for sheet_name, columns in (("Stations", STATIONS_COLUMNS), ("Products", PRODUCTS_COLUMNS),
                                ("Routings", ROUTINGS_COLUMNS)):
        assert sheet_name in wb.sheetnames, f"missing sheet {sheet_name}"
        header = [c.value for c in next(wb[sheet_name].iter_rows(min_row=1, max_row=1))]
        assert header == columns


# -- case 5: CLI writes a model.json that loads and validates ---------------

def test_cli_generate_writes_loadable_valid_model(tmp_path):
    _require_cli("generate")
    source = _write_csv_tables(tmp_path)
    out = tmp_path / "model.json"
    code = _run_cli(["generate", source, "-o", str(out)])
    assert code == 0
    assert out.exists()

    layout = json.loads(out.read_text())
    assert _fatal(_validate(layout)) == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
