"""One interval wording, one KPI label, one reference word for every hand-over page (D1236, S4-17)."""
import math

from nagasim import experiments, pagefmt, pagetext


def test_one_interval_and_one_reference_word():
    # The spec's own example: decimals follow the half-width (3.6 points -> one decimal).
    assert pagefmt.interval_text(0.276, 0.240, 0.312, share=True) == "27.6% (95% interval 24.0 to 31.2)"
    assert pagefmt.interval_text(0.276, 0.240, 0.312, share=True, lang="it") == \
        "27,6% (intervallo al 95% da 24,0 a 31,2)"
    assert pagefmt.interval_text(0.7096, 0.6844, 0.7348, "parts/min") == \
        "0.710 parts/min (95% interval 0.684 to 0.735)"
    # The reference case has one name, the engine's, on every page (Open 2).
    assert pagefmt.REFERENCE == experiments.BASELINE == pagefmt.reference_name() == "Baseline"
    assert pagefmt.reference_name("it") == pagetext.table("it")["name.baseline"]


def test_interval_edges():
    nan, inf = float("nan"), float("inf")
    # One replication: no interval, the mean at 4 significant digits.
    assert pagefmt.interval_text(4.6612, nan, nan, "min") == "4.661 min"
    assert pagefmt.interval_text(4.6612, None, None) == "4.661"
    # An infinite bound is never printed ("-inf" on a client page, S4-03).
    assert pagefmt.interval_text(2.0, -inf, 3.0) == "2"
    assert pagefmt.interval_text(nan, 1, 2) == "-" and pagefmt.interval_text(None) == "-"
    # Every replication equal: a zero-width interval keeps 4 digits.
    assert pagefmt.interval_text(5.0, 5.0, 5.0) == "5 (95% interval 5 to 5)"
    # A paired difference (P12c, D1241): signed, and never "-0.0".
    assert pagefmt.interval_text(0.2, 0.18, 0.22, signed=True) == "+0.200 (95% interval +0.180 to +0.220)"
    assert pagefmt.interval_text(-0.01, -0.04, 0.02, signed=True) == "-0.010 (95% interval -0.040 to +0.020)"
    assert "-0.0 " not in pagefmt.interval_text(-0.001, -0.3, 0.3)
    # Large numbers keep whole digits, no exponent.
    assert pagefmt.interval_text(327598.0, 300000.0, 355000.0) == "327598 (95% interval 300000 to 355000)"
    assert pagefmt.interval_text(2.3e-05, 2.0e-05, 2.6e-05) == "0.0000230 (95% interval 0.0000200 to 0.0000260)"
    assert pagefmt.number(1234567.0) == "1234567" and pagefmt.number(math.nan) == "-"
    assert pagefmt.number(0.5, "it") == "0,5"


def test_kpi_label_from_the_dashboard_else_the_plain_name():
    dashboard = {"results": [{"kpi": "system/Throughput (per time unit)", "label": "Throughput",
                              "unit": "parts/min"}]}
    assert pagefmt.kpi_label("system/Throughput (per time unit)", dashboard) == "Throughput (parts/min)"
    assert pagefmt.kpi_parts("system/Throughput (per time unit)", dashboard) == ("Throughput", "parts/min")
    assert pagefmt.kpi_label("system/Avg Queue Length") == "Avg Queue Length"
    assert pagefmt.kpi_label("user/time_in_system") == "time_in_system"
    assert pagefmt.kpi_label("resource/cell_1/Machine/in_service") == "in service - cell_1/Machine"
    # Not a family prefix: a study's goal KPI stays as written.
    assert pagefmt.kpi_label("Server/Share waiting over 5") == "Server/Share waiting over 5"
    for key in ("system/A", "user/b", "resource/r/c", "store/s/c", "entity/e/c"):
        assert not pagefmt.kpi_label(key).startswith(("system/", "user/", "resource/", "store/", "entity/"))


def test_bookkeeping_rows():
    assert pagefmt.is_bookkeeping("system/Entity Log Truncated", [0, 0])
    assert pagefmt.is_bookkeeping("system/Resource/Store Log Truncated", [0.0])
    assert pagefmt.is_bookkeeping("system/Time Unit") and pagefmt.is_bookkeeping("system/Started From State At")
    assert pagefmt.is_bookkeeping("system/Total Cost", [0, 0.0]) and not pagefmt.is_bookkeeping("system/Total Cost", [0, 3])
    assert not pagefmt.is_bookkeeping("system/Avg WIP")


def test_one_hide_rule_for_every_display():
    """D1251: the pages, the CLI summary and the editor hide rows by one engine rule."""
    from nagasim.core import outputs
    rows = ["system/Time Unit", "Started From State At", "system/Entity Log Truncated", "Total Cost",
            "system/Cost per Completed Entity", "resource/Cost centre/Utilization", "system/Avg WIP"]
    for values in (None, [0, 0.0, float("nan")], [0, 2], ["x"]):
        for key in rows:
            assert pagefmt.is_bookkeeping(key, values) == outputs.hidden_row(key, values), (key, values)
    assert [k for k in rows if outputs.hidden_row(k, [0, float("nan")])] == rows[:5]
    assert [k for k in rows if outputs.hidden_row(k, [0, 2])] == rows[:2]           # a non-zero flag or cost is news
    assert [k for k in rows if outputs.hidden_row(k)] == rows[:2]                   # no values: only the run's facts
    assert not outputs.hidden_when_zero("resource/Cost centre/Utilization")          # the last segment decides


def test_interval_numbers_is_the_one_decimals_rule():
    """The rule interval_text and the editor's Summary share (D1258)."""
    assert pagefmt.interval_numbers(3.0, 2.5, 3.5) == ("3.00", "2.50", "3.50")
    assert pagefmt.interval_numbers(1528.5, 1464.3, 1592.7, group=True) == ("1,528", "1,464", "1,593")
    assert pagefmt.interval_numbers(-1234.0, -1500.0, -1000.0, group=True) == ("-1,234", "-1,500", "-1,000")
    assert pagefmt.interval_numbers(2161.0, 2161.0, 2161.0) == ("2161", "2161", "2161")
    assert pagefmt.interval_numbers(4.6612, math.nan, math.nan) == ("4.661", "", "")
    assert pagefmt.interval_numbers(math.nan, 1, 2) == ("-", "", "")

