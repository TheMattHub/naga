"""PHASE6 S11 (docs/PHASE6_PLAN.md S11, ARCHITECTURE 5.24): conformance against a
small subset of the SDXorg `test-models` corpus (`tests/data/sdx/`, MIT licence --
see `tests/data/sdx/README.md` and `THIRD_PARTY_NOTICES.md`). Each model is
imported (`sdimport`/`mdlimport`, by extension), run, and every level/flow/
auxiliary column its own `output.csv`/`output.tab` names is checked against the
imported model's sampled timeseries at the reference file's own time grid.

One model is marked `xfail(strict=True)`: a the product architectural limit (a
flow with more than two ends) the corpus happens to exercise (see its `reason`).
`test_sdimport.py`/`test_sdimport_mdl.py` own the importers' unit tests; this file only checks end-to-end numeric agreement.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from nagasim import mdlimport, sdimport
from nagasim.api import run_model

DATA = Path(__file__).parent / "data" / "sdx"
TOL = 1e-3          # relative tolerance (task convention); absolute floor below


def _load_reference(path: Path) -> dict[str, dict[float, float]]:
    """{column: {time: value}} from a tab- or comma-separated SDXorg output file."""
    delimiter = "\t" if path.suffix == ".tab" else ","
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter=delimiter))
    header = rows[0]
    series: dict[str, dict[float, float]] = {name: {} for name in header[1:]}
    for row in rows[1:]:
        if not row:
            continue
        t = round(float(row[0]), 6)
        for name, cell in zip(header[1:], row[1:]):
            try:
                series[name][t] = float(cell)
            except ValueError:
                pass                                    # blank/non-numeric cell
    return series


def _series_lookup(layout: dict) -> dict[str, str]:
    """{model variable name: nagasim timeseries "Series" name}, from the
    imported layout's own levels/flows/auxiliaries -- never hard-coded, so a
    variable the importer refused (and so left out of the layout) is simply
    absent and its output.csv column is skipped rather than guessed at."""
    lookup: dict[str, str] = {}
    for kind, key in (("levels", "level"), ("flows", "flow"), ("auxiliaries", "aux")):
        for record in layout.get(kind, []):
            lookup[sdimport._fold(record["name"])] = f'{key}/{record["name"]}'
    return lookup


def _run_and_compare(layout: dict, reference: dict[str, dict[float, float]]) -> None:
    if not layout.get("levels"):
        # A pure-auxiliary model has nothing that schedules a future event, so
        # the product's own clock never advances past t=0 (core/timeseries.py's
        # sampler stops once the event queue empties, ARCHITECTURE 5.24's grid
        # only exists once a level does) -- the same harmless static level/flow
        # test_sd_functions.py's `_sd_layout` uses to drive a pure-aux model's
        # timeseries over its whole run.
        layout["levels"] = [{"name": "_sdx_dummy", "initial": 0.0}]
        layout["flows"] = layout.get("flows", []) + [
            {"name": "_sdx_dummy_flow", "from": "_sdx_dummy", "rate": "0"}]

    sim = layout.setdefault("simulation", {})
    time_limit = sim["time_limit"]
    dt = sim.get("sd", {}).get("dt") or 1.0
    # Sample on the same grid as the reference file (one point per dt) so the
    # comparison needs no interpolation (ARCHITECTURE 5.24's grid never drifts).
    sim["timeseries_points"] = max(1, round(time_limit / dt))

    results = run_model(layout, replications=1, workers=1)
    ts = results["timeseries"]
    lookup = _series_lookup(layout)

    compared = 0
    for column, points in reference.items():
        series_name = lookup.get(sdimport._fold(column))     # "Stock A" is Stock_A (#164)
        if series_name is None:
            continue                                     # not one of ours (control var, or refused)
        rows = ts[ts["Series"] == series_name]
        sampled = {round(t, 6): v for t, v in zip(rows["Time"], rows["Value"])}
        for t, expected in points.items():
            assert t in sampled, f"{column} ({series_name}): no sample at t={t}"
            actual = sampled[t]
            assert actual == pytest.approx(expected, rel=TOL, abs=1e-6), (
                f"{column} ({series_name}) at t={t}: expected {expected}, got {actual}")
        compared += 1
    assert compared > 0, "no output.csv column matched a level/flow/auxiliary -- nothing was checked"


def _check(model_path: Path, output_path: Path) -> None:
    if model_path.suffix == ".mdl":
        layout, problems = mdlimport.load_mdl(model_path)
    else:
        layout, problems = sdimport.load_xmile(model_path)
    reference = _load_reference(output_path)
    _run_and_compare(layout, reference)


# --------------------------------------------------------------- passing models
@pytest.mark.parametrize("model, output", [
    ("comparisons/comparisons.xmile", "comparisons/output.csv"),
    ("eval_order/eval_order.xmile", "eval_order/output.csv"),
    ("lookups_no_indirect/test_lookups_no-indirect.xmile", "lookups_no_indirect/output.tab"),
    ("min_max_1arg/test_min_max_1arg.xmile", "min_max_1arg/output.tab"),  # array literal
    # SDXorg cleanup round
    ("abs/test_abs.mdl", "abs/output.csv"),                     # unbounded Vensim stocks
    ("rounding/test_rounding.mdl", "rounding/output.tab"),      # MODULO sign (#168), INTEGER
    ("lookups_without_range/test_lookups_without_range.mdl",
     "lookups_without_range/output.tab"),                       # standalone lookup
    ("na/test_na.mdl", "na/output.tab"),                        # :NA:
    ("pi/test_pi.xmile", "pi/output.tab"),
    ("input_functions/test_inputs.mdl", "input_functions/output.csv"),  # PULSE, PULSE TRAIN
    ("smooth/test_smooth.mdl", "smooth/output.tab"),            # default initial (#167)
    ("trig/test_trig.xmile", "trig/output.csv"),                # isee namespace (#164)
])
def test_conformance(model, output):
    _check(DATA / model, DATA / output)


# --------------------------------------------------------------- known gaps
@pytest.mark.xfail(strict=True, reason="OutFlow and if_else each drain TWO sibling stocks "
                   "(TestStock0/TestStock1, TestStock2/TestStock3) to compare non_negative "
                   "on/off side by side; Naga's flow model joins exactly two ends (a level and "
                   "a level/None), so both flows are refused and every stock stays at its initial "
                   "value -- an architectural limit (sdimport.py's own refusal message), not an "
                   "importer gap")
def test_non_negative_stocks_conformance():
    _check(DATA / "non_negative_stocks/test_non_negative_stocks.xmile",
           DATA / "non_negative_stocks/output.tab")
