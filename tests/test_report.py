"""ROADMAP 1.7 B: the HTML report builds for every curated model, is
well-formed, escapes model-provided strings, and covers the experiment section."""
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import json
import pytest

from nagasim import api, experiments
from nagasim.core import schema
from nagasim.report import _fmt, _kpi_label, build_report
from nagasim import svgcharts

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
MODELS = sorted(MODELS_DIR.glob("*.json"))

_VOID = {"meta", "link", "br", "hr", "img", "input", "area", "base", "col", "embed",
        "source", "track", "wbr"}

_HEADINGS = ["Model summary", "System KPIs", "Resources", "Stores", "Entities",
            "User statistics", "Charts", "Methods"]


class _BalanceChecker(HTMLParser):
    """Collects unclosed tags; a well-formed document ends with an empty stack."""

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass   # explicit self-closing tag (e.g. <br/>): never pushed

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"mismatched </{tag}>: stack={self.stack}"
        self.stack.pop()


def _assert_well_formed(doc: str) -> None:
    checker = _BalanceChecker()
    checker.feed(doc)
    assert checker.stack == [], f"unclosed tags: {checker.stack}"
    for svg in _extract_svgs(doc):
        ET.fromstring(svg)   # raises on malformed XML


def _extract_svgs(doc: str) -> list[str]:
    svgs, i = [], 0
    while True:
        start = doc.find("<svg", i)
        if start < 0:
            break
        end = doc.find("</svg>", start) + len("</svg>")
        svgs.append(doc[start:end])
        i = end
    return svgs


@pytest.mark.slow
def test_report_builds_for_every_curated_model(curated_results):
    for path, results in curated_results.items():
        layout = schema.migrate(json.loads(path.read_text()))

        doc = build_report(layout, results, title=path.stem)

        _assert_well_formed(doc)
        for heading in _HEADINGS:
            assert f">{heading}<" in doc, (path.stem, heading)
        assert "http://" not in doc.replace('xmlns="http://www.w3.org/2000/svg"', ""), path.stem
        assert "https://" not in doc, path.stem
        assert "<script" not in doc.lower(), path.stem


def test_report_costs_section_present_when_costs_are_declared():
    """job_shop_jackson.json declares resource and entity holding costs
    (ROADMAP 2.2 example); its report must include the Costs section."""
    path = MODELS_DIR / "job_shop_jackson.json"
    layout = schema.migrate(json.loads(path.read_text()))
    results = api.run_model(str(path), replications=2, run_length=200, warmup=0)

    doc = build_report(layout, results, title="job_shop_jackson")

    assert ">Costs (" in doc
    assert "Total Resource Cost" in doc


@pytest.fixture(scope="module")
def single_server():
    """One run of single_server.json and its report, shared by every test that
    only reads the report of a plain, cost-free, experiment-free model."""
    path = MODELS_DIR / "single_server.json"
    layout = schema.migrate(json.loads(path.read_text()))
    results = api.run_model(str(path), replications=2, run_length=50, warmup=0)
    return layout, results, build_report(layout, results, title="single_server")


def test_report_costs_section_absent_when_no_costs_declared(single_server):
    """single_server.json has no cost fields: the section is not worth
    showing, so it must not appear at all."""
    assert "<h2>Costs" not in single_server[2]


def test_report_writes_to_path(tmp_path, single_server):
    layout, results, _doc = single_server
    out = tmp_path / "report.html"

    returned = build_report(layout, results, title="single_server", path=out)

    assert out.exists()
    assert out.read_text(encoding="utf-8") == returned


def test_report_formats_kpi_values(single_server):
    _layout, results, doc = single_server

    # The System KPIs table shows the across-replication MEAN of each metric
    # (thousands separator, 3 decimals) -- must appear verbatim.
    created = results["system"]
    mean = created.loc[created["Metric"] == "Total Entities Created", "Value"].astype(float).mean()
    assert f"{mean:,.3f}" in doc
    assert "Server" in doc   # the model's own resource name, in the Resources table


def test_report_escapes_model_provided_strings(tmp_path):
    payload = "<script>alert(1)</script>"
    blocks = [
        {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                    "entities_per_arrival": 1, "max_arrivals": 5, "start_time": 0}},
        {"name": "seize_1", "description": "s", "type": "SEIZE", "position": [1, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.5}, "priority": 0,
                    "quantity": 1, "resource_name": payload}},
        {"name": "release_1", "description": "r", "type": "RELEASE", "position": [2, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}, "resource_name": payload}},
        {"name": "dispose_1", "description": "d", "type": "DISPOSE", "position": [3, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}},
    ]
    links = [["create_1", "seize_1"], ["seize_1", "release_1"], ["release_1", "dispose_1"]]
    resources = [{"name": payload, "capacity": 1, "discipline": "FIFO"}]
    layout = schema.migrate({
        "blocks": blocks, "links": links, "resources": resources, "entities": [], "variables": [],
        "schedules": [], "failures": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    })
    model_path = tmp_path / "xss.json"
    model_path.write_text(json.dumps(layout))
    results = api.run_model(str(model_path), replications=1, run_length=50, warmup=0)

    doc = build_report(layout, results, title="xss")

    assert payload not in doc
    assert html_escape(payload) in doc
    _assert_well_formed(doc)


def test_report_header_shows_the_model_description_html_escaped(single_server):
    # The header reads `info` from the layout only, so the shared results serve.
    payload = "A queue model <script>alert(1)</script>"
    layout, results, _doc = single_server
    layout = {**layout, "info": {"description": payload}}

    doc = build_report(layout, results, title="desc")

    assert payload not in doc
    assert html_escape(payload) in doc
    _assert_well_formed(doc)


def test_report_header_omits_description_paragraph_when_none_given(single_server):
    layout, results, _doc = single_server
    doc = build_report({**layout, "info": {}}, results, title="nodesc")

    assert 'class="description"' not in doc


def html_escape(s: str) -> str:
    import html
    return html.escape(s)


# ---------------------------------------------------------------- experiment section
@pytest.fixture(scope="module")
def call_centre_report():
    """call_centre_day.json's scenario set and its report (with the experiment section)."""
    path = MODELS_DIR / "call_centre_day.json"
    layout = schema.migrate(json.loads(path.read_text()))
    result = experiments.run_scenarios(str(path), replications=2, run_length=300, warmup=0)
    base = result.results.get(experiments.BASELINE) or next(iter(result.results.values()))
    return result, build_report(layout, base, experiment=result, title="call_centre_day")


def test_report_includes_experiment_section(call_centre_report):
    result, doc = call_centre_report

    _assert_well_formed(doc)
    assert "Scenarios" in doc
    assert "Scenario definitions" in doc
    assert "Comparison" in doc
    for name in result.scenarios:
        assert html_escape(name) in doc


def test_report_no_experiment_section_when_not_given(single_server):
    assert "Scenario definitions" not in single_server[2]


# ---------------------------------------------------------------- svgcharts unit tests
def test_nice_ticks_edge_cases():
    assert svgcharts.nice_ticks(5.0, 5.0)                       # constant series
    assert svgcharts.nice_ticks(float("nan"), float("nan"))     # all-NaN
    assert svgcharts.nice_ticks(3.0, 3.0)                       # single point
    ticks = svgcharts.nice_ticks(-10.0, -2.0)                   # negative values
    assert ticks[0] <= -10.0 and ticks[-1] >= -2.0
    assert all(b > a for a, b in zip(ticks, ticks[1:]))         # strictly increasing


def test_bar_chart_empty_and_populated():
    empty = svgcharts.bar_chart("t", [], [], "y")
    assert "No data" in empty
    ET.fromstring(empty)

    chart = svgcharts.bar_chart("t", ["A", "B"], [1.0, 2.0], "y", err=[(0.5, 1.5), (1.5, 2.5)])
    ET.fromstring(chart)
    assert "A" in chart and "B" in chart


def test_line_chart_with_band_and_markers():
    chart = svgcharts.line_chart("t", [0, 1, 2], {"s": [1.0, 2.0, 1.5]}, "x", "y",
                                 band=([0.5, 1.0, 1.0], [1.5, 3.0, 2.0]), markers=[1.0])
    ET.fromstring(chart)


def test_histogram_empty_and_populated():
    empty = svgcharts.histogram("t", [], [], "x", "y")
    assert "No data" in empty
    chart = svgcharts.histogram("t", [0, 1, 2, 3], [5, 2, 8], "x", "y")
    ET.fromstring(chart)


# ---------------------------------------------------------------- PART 0 a: chart layout
def test_charts_have_one_consistent_fixed_size():
    """Every chart is 460x300, fixed via width/height attributes (not just
    viewBox), so a lone chart in a report row never stretches full width."""
    for svg in (svgcharts.bar_chart("t", ["A"], [1.0], "y"),
               svgcharts.line_chart("t", [0, 1], {"s": [1.0, 2.0]}, "x", "y"),
               svgcharts.histogram("t", [0, 1, 2], [1, 2], "x", "y")):
        assert 'width="460"' in svg and 'height="300"' in svg
        assert 'width="100%"' not in svg


def test_report_chart_grid_is_two_columns_max_460px(single_server):
    doc = single_server[2]

    assert "grid-template-columns: repeat(2, minmax(0, 460px))" in doc
    assert 'width="100%"' not in doc  # no chart stretches to fill a lone row


# ---------------------------------------------------------------- PART 0 b: number formatting
def test_fmt_integers_have_no_decimals():
    assert _fmt(5) == "5"
    assert _fmt(5.0) == "5"
    assert _fmt(2400) == "2,400"
    assert _fmt(2400.0) == "2,400"
    assert _fmt(0) == "0"


def test_fmt_non_integers_have_3_decimals():
    assert _fmt(2.5) == "2.500"
    assert _fmt(1234.5678) == "1,234.568"
    assert _fmt(0.1) == "0.100"


def test_fmt_non_finite_and_none():
    assert _fmt(None) == "-"
    assert _fmt(float("nan")) == "-"
    assert _fmt(float("inf")) == "-"


# ---------------------------------------------------------------- PART 0 c: KPI labels
def test_kpi_label_mapping():
    assert _kpi_label("system/Avg Queue Length") == "Avg Queue Length (system)"
    assert _kpi_label("user/time_in_system") == "time_in_system (user)"
    assert _kpi_label("resource/Agents/Usage % (snapshot)") == "Usage % (snapshot) - Agents"
    assert _kpi_label("not-a-kpi-key") == "not-a-kpi-key"


def test_comparison_table_shows_labels_and_keeps_raw_key_as_title(call_centre_report):
    doc = call_centre_report[1]

    assert '<span title="system/Avg Queue Length">Avg Queue Length (system)</span>' in doc
    # the raw key appears only as a tooltip, never as the visible cell text
    assert ">system/Avg Queue Length<" not in doc



# ---------------------------------------------------------------- real dates (PHASE4 T10)
def test_a_dated_model_gets_date_axes_and_a_calendar_row_in_the_report():
    raw = json.loads((MODELS_DIR / "single_server.json").read_text())
    dated = {**raw, "simulation": {**raw.get("simulation", {}),
                                   "calendar": {"start": "2026-01-05T06:00:00",
                                                "timezone": "Europe/Rome"}}}
    results = api.run_model(dated, replications=1, run_length=4320, warmup=0)   # three days
    html = build_report(dated, results, title="dated")
    assert "<dt>Calendar</dt>" in html and "Mon 05 Jan 2026 06:00" in html
    # Ticks land on local midnight, so the first full day of the run is labelled.
    assert "Date (Europe/Rome)" in html and ">06 Jan<" in html and ">07 Jan<" in html

    plain = build_report(raw, api.run_model(raw, replications=1, run_length=4320, warmup=0),
                        title="plain")
    assert "<dt>Calendar</dt>" not in plain and ">Time<" in plain


def test_line_chart_x_ticks_replace_the_numeric_ladder():
    svg = svgcharts.line_chart("t", [0.0, 60.0], {"a": [1.0, 2.0]}, "Date", "v",
                               x_ticks=[(0.0, "05 Jan 06:00"), (60.0, "05 Jan 07:00")])
    assert "05 Jan 06:00" in svg and "05 Jan 07:00" in svg
    assert "rotate(-30" in svg            # long date labels are slanted, not overlapped
