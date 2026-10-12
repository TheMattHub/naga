"""PHASE6 G1 (D132): the dashboard section (schema v26) -- validation, the
ModelDocument round trip, and sampling at the timeseries sampler's instants
(nagasim/core/timeseries.py, nagasim/core/outputs.py)."""
from nagasim.api import run_model
from nagasim.core import schema
from nagasim.core.document import ModelDocument

from conftest import create, dispose, layout, process


def test_dashboard_validation_codes():
    widgets = [
        {"name": "", "kind": "kpi", "expr": "1"},                       # E_DASHBOARD_NAME (empty)
        {"name": "dup", "kind": "kpi", "expr": "1"},
        {"name": "dup", "kind": "kpi", "expr": "1"},                    # E_DASHBOARD_NAME (dup)
        {"name": "badkind", "kind": "nope", "expr": "1"},                # E_DASHBOARD_KIND
        {"name": "badexpr", "kind": "kpi", "expr": "NOT_A_NAME"},        # E_DASHBOARD_EXPR
        {"name": "badseries", "kind": "line",
         "series": [{"label": "a", "expr": "1"}, {"label": "a", "expr": "2"}]},  # E_DASHBOARD_SERIES
        {"name": "badgauge", "kind": "gauge", "expr": "1", "min": 5, "max": 1},  # E_DASHBOARD_GAUGE
    ]
    problems = schema.validate_model_structured(layout([], [], dashboard={"widgets": widgets}))
    codes = {p.code for p in problems}
    assert {"E_DASHBOARD_NAME", "E_DASHBOARD_KIND", "E_DASHBOARD_EXPR", "E_DASHBOARD_SERIES",
            "E_DASHBOARD_GAUGE"} <= codes
    for p in problems:
        assert p.code in schema.PROBLEM_CODES, f"undeclared code {p.code}"

    # A well-formed dashboard has no problems.
    good = {"widgets": [
        {"name": "q", "kind": "kpi", "expr": "1"},
        {"name": "g", "kind": "gauge", "expr": "1", "max": 10, "thresholds": [3, 7]},
        {"name": "l", "kind": "line", "series": [{"label": "a", "expr": "1"}]},
        {"name": "b", "kind": "bar", "series": [{"label": "a", "expr": "1"}]},
    ]}
    assert schema.validate_model_structured(layout([], [], dashboard=good)) == []


def test_document_round_trip_and_undo():
    doc = ModelDocument()
    assert doc.dashboard == {"widgets": []}
    assert "dashboard" not in doc.to_layout()   # absent, like assertions (#226)

    widgets = [{"name": "q", "kind": "kpi", "expr": "TNOW"}]
    doc.set_dashboard({"widgets": widgets})
    assert doc.to_layout()["dashboard"] == {"widgets": widgets}

    doc.undo()
    assert doc.dashboard == {"widgets": []}
    assert "dashboard" not in doc.to_layout()
    doc.redo()
    assert doc.dashboard == {"widgets": widgets}

    # Round trip through a fresh document, as load() would build one.
    doc2 = ModelDocument(doc.to_layout())
    assert doc2.dashboard == {"widgets": widgets}


def _queue_model(dashboard):
    return {
        "blocks": [
            create(max_arrivals=3, dist_params={"value": 2.0}),
            process(resource="Server", value=1.0),
            dispose(),
        ],
        "links": [["create_1", "process_1"], ["process_1", "dispose_1"]],
        "resources": [{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        "dashboard": dashboard,
        "simulation": {"time_limit": 6, "warmup": 0, "replications": 1,
                       "timeseries_points": 6},
    }


def test_dashboard_samples_match_the_timeseries_state_at_the_same_instants():
    dashboard = {"widgets": [
        {"name": "q", "kind": "kpi", "expr": 'NQ("Server")'},
        {"name": "qr", "kind": "line", "series": [
            {"label": "queue", "expr": 'NQ("Server")'},
            {"label": "busy", "expr": 'NR("Server")'},
        ]},
    ]}
    results = run_model(_queue_model(dashboard), replications=1, seed=0, warmup=0, run_length=6)
    ts, dash = results["timeseries"], results["dashboard"]
    times = sorted(ts["Time"].unique())
    assert times and sorted(dash["Time"].unique()) == times

    def ts_value(t, series):
        return ts[(ts["Time"] == t) & (ts["Series"] == series)]["Value"].iloc[0]

    def dash_value(t, widget, series=""):
        row = dash[(dash["Time"] == t) & (dash["Widget"] == widget) & (dash["Series"] == series)]
        return row["Value"].iloc[0]

    for t in times:
        expected_q = ts_value(t, "resource/Server/queue")
        expected_r = ts_value(t, "resource/Server/in_service")
        assert dash_value(t, "q") == expected_q
        assert dash_value(t, "qr", "queue") == expected_q
        assert dash_value(t, "qr", "busy") == expected_r


def test_no_dashboard_gives_empty_frame_and_unchanged_kpis():
    plain = _queue_model({"widgets": []})
    del plain["dashboard"]
    with_dashboard = _queue_model({"widgets": [{"name": "q", "kind": "kpi",
                                                "expr": 'NQ("Server")'}]})

    r1 = run_model(plain, replications=1, seed=0, warmup=0, run_length=6)
    r2 = run_model(with_dashboard, replications=1, seed=0, warmup=0, run_length=6)

    assert list(r1["dashboard"].columns) == ["Run", "Time", "Widget", "Series", "Value", "Warmup"]
    assert r1["dashboard"].empty
    assert not r2["dashboard"].empty
    # Sampling a dashboard consumes no rng and adds no event: every other KPI
    # frame is untouched by a widget's presence (D132).
    assert r1["system"].equals(r2["system"])
    assert r1["timeseries"].equals(r2["timeseries"])
