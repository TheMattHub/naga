"""PHASE6 W3 M2 (ARCHITECTURE D113): the entity-flow Sankey and the Roser
active-period bottleneck analyser.

The oracle (tests/test_sankey_bottleneck_oracle.py) covers the material_flow_summary and
bottleneck_summary frames end to end, the overlap/shifting arithmetic and the
Sankey's nonzero-link paths. This file keeps the analyser's edge cases, the
Tracker's state accounting, the empty chart and the `outputs.bottleneck`
round trip through `ModelDocument` and the generated JSON schema.
"""
from __future__ import annotations

import pytest

from nagasim import svgcharts
from nagasim.core.bottleneck import Tracker, active_period_shares
from nagasim.core.document import ModelDocument
from nagasim.core.schema import migrate
from nagasim.modelschema import build_json_schema

_ZERO = {"Sole %": 0.0, "Shifting %": 0.0, "Total %": 0.0}
_HALF = {"Sole %": 50.0, "Shifting %": 0.0, "Total %": 50.0}
_ALL = {"Sole %": 100.0, "Shifting %": 0.0, "Total %": 100.0}


# ---------------------------------------------------------------- pure algorithm
@pytest.mark.parametrize("periods, t0, t1, expected", [
    ({"A": [(0, 5)], "B": [(5, 10)]}, 0, 10, {"A": _HALF, "B": _HALF}),  # touching: no shifting
    ({"A": [(0, 10)], "B": []}, 0, 10, {"A": _ALL, "B": _ZERO}),          # never active
    ({}, 0, 10, {}),
    ({"A": [(0, 10)]}, 5, 5, {"A": _ZERO}),                               # empty window
    ({"A": [(-5, 15)]}, 0, 10, {"A": _ALL}),                              # clipped to the window
])
def test_active_period_shares_edge_cases(periods, t0, t1, expected):
    assert active_period_shares(periods, t0, t1) == expected


# ---------------------------------------------------------------- Tracker
@pytest.mark.parametrize("calls, end, expected", [
    # Blocked nets out busy: INACTIVE while blocked (D113's ACTIVE/INACTIVE split).
    ([("busy", 1, 0.0), ("blocked", 1, 3.0), ("blocked", 0, 6.0), ("busy", 0, 10.0)], 10.0,
     [(0.0, 3.0), (6.0, 10.0)]),
    # A repair (down) is ACTIVE per D113, even with no busy units.
    ([("down", True, 2.0), ("down", False, 4.0)], 10.0, [(2.0, 4.0)]),
    # An open period is closed at the query time.
    ([("busy", 1, 0.0)], 7.0, [(0.0, 7.0)]),
    # Warm-up reset at 5 drops the closed [0, 2) and restarts the open one at 5.
    ([("busy", 1, 0.0), ("busy", 0, 2.0), ("busy", 1, 3.0), ("reset", 5.0), ("busy", 0, 8.0)], 10.0,
     [(5.0, 8.0)]),
], ids=["blocked", "down", "open_tail", "reset"])
def test_tracker_closed_periods(calls, end, expected):
    t = Tracker()
    for name, *args in calls:
        getattr(t, name)(*(args if name == "reset" else ["R1", *args]))
    assert t.closed_periods(end) == {"R1": expected}


def test_sankey_empty_input_is_the_no_data_chart():
    assert "No data" in svgcharts.sankey([])


# ---------------------------------------------------------------- outputs.bottleneck round trip
def test_document_set_bottleneck_output_round_trips_and_omits_when_off():
    doc = ModelDocument()
    assert doc.outputs == {}
    doc.set_bottleneck_output(True)
    assert doc.to_layout()["outputs"] == {"bottleneck": True}
    doc.set_bottleneck_output(False)
    assert doc.to_layout()["outputs"] == {}   # dropped, not written as false


def test_outputs_bottleneck_conforms_to_the_generated_json_schema():
    # Imported here, not at module level: jsonschema is not a dependency (see
    # test_schema), and a module-level import cost every xdist worker ~0.5 s.
    jsonschema = pytest.importorskip("jsonschema")
    good = migrate({"schema_version": 9, "blocks": [], "links": [], "outputs": {"bottleneck": True}})
    assert not list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(good))

    bad = migrate({"schema_version": 9, "blocks": [], "links": [], "outputs": {"bottleneck": "yes"}})
    assert list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(bad))
