"""E5 (D1138): the tornado takes named structural changes (two-sided or one-sided overrides) and
a second KPI that ranks ties; the plain call is unchanged. A synthetic response through the
evaluation seam, so every number is known."""
import pandas as pd
import pytest

from nagasim import optimize
from nagasim.optimize import Factor, tornado
from nagasim.optimize.tornado import COLUMNS, SECONDARY_COLUMNS

KPI, SERVERS = "system/ThroughputTime", "user/Servers"


def _fake(layout, overrides, seeds, **kwargs):
    """Throughput time = 100 + 10 x rate - 5 x (rule == "spt") + 3 x staff; servers = staff // 2."""
    got = {o["path"]: o["value"] for o in overrides}
    rate, staff = float(got.get("rate", 1.0)), int(got.get("staff", 2))
    spt = got.get("rule") == "spt"
    span = 100 + 10 * rate - 5 * spt + 3 * staff
    return {KPI: {s: float(span) for s in seeds}, SERVERS: {s: float(staff // 2) for s in seeds}}


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr("nagasim.optimize.evaluation._evaluate_point", _fake)


FACTORS = [Factor("rate", "continuous", 0.0, 2.0), Factor("staff", "integer", 2, 4)]


def test_the_plain_call_is_unchanged():
    frame = tornado({}, FACTORS, KPI)
    assert list(frame.columns) == COLUMNS
    assert list(frame["Factor"]) == ["rate", "staff"]
    assert frame["Swing"].tolist() == [20.0, 6.0]


def test_named_changes_two_sided_and_one_sided():
    changes = [{"name": "dispatch rule", "low": [{"path": "rule", "value": "fifo"}],
                "high": [{"path": "rule", "value": "spt"}]},
               {"name": "two more staff", "overrides": [{"path": "staff", "value": 5}]}]
    frame = tornado({}, FACTORS, KPI, scenarios=changes).set_index("Factor")
    base = frame["Base KPI"].iloc[0]
    assert base == 100 + 10 * 1.0 + 3 * 3        # every factor at mid-range (staff 3)
    rule = frame.loc["dispatch rule"]
    assert (rule["KPI at low"], rule["KPI at high"]) == (base, base - 5)
    assert rule["Low value"] == "rule=fifo" and rule["High value"] == "rule=spt"
    staff = frame.loc["two more staff"]
    assert staff["KPI at low"] == base and staff["KPI at high"] == base + 6   # a missing side is the base
    assert pd.isna(staff["Low value"])
    # a change alone, with no numeric factor
    alone = tornado({}, [], KPI, scenarios=changes[:1])
    assert list(alone["Factor"]) == ["dispatch rule"]


def test_the_second_kpi_ranks_ties():
    """Equal throughput-time swings (+6 each, from the base staff 2): the change that also moves the
    server count ranks first."""
    equal = [{"name": "same servers", "high": [{"path": "rate", "value": 1.6}]},
             {"name": "more servers", "high": [{"path": "staff", "value": 4}]}]
    ranked = tornado({}, [], KPI, scenarios=equal, secondary=SERVERS)
    assert list(ranked.columns) == COLUMNS + SECONDARY_COLUMNS
    assert ranked["Swing"].tolist() == [6.0, 6.0]
    assert list(ranked["Factor"]) == ["more servers", "same servers"]
    assert ranked["Secondary swing"].tolist() == [1.0, 0.0]
    assert ranked["Secondary base"].tolist() == [1.0, 1.0]


def test_bad_changes_are_refused():
    for bad, match in (({"low": []}, "needs a name"), ({"name": "x"}, "changes nothing"),
                       ({"name": "x", "high": [{"path": "a"}]}, "an override is")):
        with pytest.raises(ValueError, match=match):
            tornado({}, FACTORS, KPI, scenarios=[bad])
    with pytest.raises(ValueError, match="at least one factor or scenario"):
        tornado({}, [], KPI)
    with pytest.raises(ValueError, match="unknown response KPI"):
        tornado({}, FACTORS, KPI, secondary="user/Nope")
    assert optimize.tornado is tornado
