"""Twin anomaly alerts (Phase 11 Task C, D721): a two-sided CUSUM of standardised residuals
in `nagasim/score.py`, run by `twin.Cycle._compare`, in the result and on the monitor page."""
import json
import random
import types

import pytest

from nagasim import monitor, score, twin
from test_twin_cycle import Frame, Source, bands, make

SD = 4.0 / 3.29                     # the band 2..6 of `bands`, read as a normal's 5% to 95%


def point(observed, at=0.0, series="system/WIP"):
    return {"series": series, "mean": 4.0, "q05": 2.0, "q95": 6.0, "observed": observed,
            "observed_at": at}


def run_length(shift, seed, limit=100_000):
    rng, cusum = random.Random(seed), score.Cusum("m")
    for i in range(limit):
        cusum.add(point(4.0 + rng.gauss(shift * SD, SD), float(i)))
        if cusum.alerts():
            return i + 1
    return limit


# ---------------------------------------------------------------- the statistic
def test_the_residual_is_scaled_by_the_band_and_a_flat_band_is_not_summed():
    assert score.residual(4.0, 2.0, 6.0, 4.0 + SD) == pytest.approx(1.0)
    assert score.residual(4.0, 4.0, 4.0, 5.0) is None and score.residual(4.0, 2.0, 6.0, None) is None
    cusum = score.Cusum("m")
    cusum.add({**point(5.0), "q05": 4.0, "q95": 4.0})
    assert cusum.rows == {} and cusum.basis()["flat"] == 1


def test_a_step_shift_alerts_within_the_tabled_run_length():
    # Montgomery's two-sided table for k = 0.5, h = 4: ARL 8.38 at a shift of one sd.
    lengths = [run_length(1.0, seed, 1000) for seed in range(200)]
    assert 6.5 < sum(lengths) / len(lengths) < 10.5
    # A shift of the whole band's half-width: about three readings.
    assert max(run_length(1.645, seed, 1000) for seed in range(50)) <= 12


def test_in_band_noise_raises_few_alerts_over_a_long_run():
    # In control the tabled run length is 168 readings: 20000 readings raise about 150.
    lengths = [run_length(0.0, seed) for seed in range(60)]
    assert 110 < sum(lengths) / len(lengths) < 260
    rng, cusum = random.Random(1), score.Cusum("m")
    for i in range(20_000):
        cusum.add(point(4.0 + rng.gauss(0.0, SD), float(i)))
    assert cusum.basis()["raised"] < 250


def test_a_side_alerts_while_over_h_and_ends_after_its_shift_does():
    cusum = score.Cusum("m", k=0.5, h=4.0)
    for i in range(3):                  # z = 2: the sum rises by 1.5 a reading
        cusum.add(point(4.0 + 2 * SD, float(i)))
    alert, = cusum.alerts()
    assert (alert["subject"], alert["side"], alert["since"]) == ("system/WIP", "above", 2.0)
    assert alert["numbers"] == {"statistic": pytest.approx(4.5), "limit": 4.0, "k": 0.5, "points": 3}
    assert "sustained shift above the forecast" in alert["message"]
    assert "fault" not in alert["message"] and "fault" not in monitor.ALERTED
    for i in range(30):                 # capped at 2h, so it ends within h/k readings
        cusum.add(point(4.0 + 9 * SD, float(3 + i)))
    assert cusum.rows["system/WIP"]["high"] == 8.0
    for i in range(9):
        cusum.add(point(4.0, float(40 + i)))
    assert cusum.alerts() == [] and cusum.rows["system/WIP"]["above"] == 1
    for i in range(3):
        cusum.add(point(4.0 - 2 * SD, float(50 + i)))
    assert [a["side"] for a in cusum.alerts()] == ["below"]


def test_the_state_is_bounded_by_the_series():
    cusum = score.Cusum("m")
    for i in range(score.MAX_SERIES + 10):
        cusum.add(point(4.0 + 9 * SD, series=f"level/l{i}"))
    assert len(cusum.rows) == score.MAX_SERIES and len(cusum.alerts()) == score.MAX_SERIES


# ---------------------------------------------------------------- read back whole
def test_the_state_reloads_and_a_hand_edited_one_is_judged_whole():
    cusum = score.Cusum("m", since="then")
    for i in range(4):
        cusum.add(point(4.0 + 2 * SD, float(i)))
    doc = json.loads(json.dumps(cusum.document()))
    again, why = score.Cusum.load(doc, "m", score.K, score.H, "now")
    assert why is None and again.document() == cusum.document() and again.alerts() == cusum.alerts()
    assert score.Cusum.load(doc, "other", score.K, score.H, "now")[1] == "model"
    assert score.Cusum.load(doc, "m", 1.0, score.H, "now")[1] == "limits"
    assert score.Cusum.load(None, "m", score.K, score.H, "now")[1] == "absent"
    row = doc["rows"]["system/WIP"]
    for edit in ({"high": -1.0}, {"high": 9.0}, {"high": "1"}, {"high": True},
                 {"high_since": "1"}, {"low_since": 3.0}, {"points": -1}, {"points": 1.5},
                 {"above": None}, {"extra": 1}):
        bad = {**doc, "rows": {"system/WIP": {**row, **edit}}}
        new, why = score.Cusum.load(bad, "m", score.K, score.H, "now")
        assert (why, new.rows, new.since) == ("unsound", {}, "now"), edit
    # An alerting side whose instant is unknown (no observed_at, #941) is sound.
    assert score.Cusum.load({**doc, "rows": {"system/WIP": {**row, "high_since": None}}},
                            "m", score.K, score.H, "now")[1] is None
    for edit in ({"rows": []}, {"flat": -1}, {"since": 5}, {"since": "x" * 65},
                 {"rows": {f"s{i}": row for i in range(score.MAX_SERIES + 1)}}):
        assert score.Cusum.load({**doc, **edit}, "m", score.K, score.H, "now")[1] == "unsound"


# ---------------------------------------------------------------- in the cycle
def shifted(tmp_path, values, **more):
    """A twin stepped every 10 time units with a forecast at each step, so up to six
    overlapping forecasts fill a point at once, and the WIP the plant shows a step."""
    cycle, source, clock, _calls = make(tmp_path, **more)
    docs = []
    for value in values:
        source.values = {"system/WIP": value}
        docs.append(cycle.step())
        source.at += 10.0
        clock.tick(300.0)
    return cycle, source, docs


def test_a_shift_the_plant_shows_alerts_in_the_result_and_survives_a_restart(tmp_path):
    _cycle, source, docs = shifted(tmp_path, [4.0] * 8 + [8.0] * 3)
    assert all(doc["alerts"]["rows"] == [] for doc in docs[:8])
    assert docs[0]["alerts"] == {"k": 0.5, "h": 4.0, "rows": [], "basis": {
        "points": 0, "flat": 0, "raised": 0, "since": docs[0]["started"]}}
    alert, = docs[8]["alerts"]["rows"]          # the first step of the shift
    assert (alert["side"], alert["since"], alert["rule"]) == ("above", 80.0, "sustained shift")
    history = json.loads((tmp_path / twin.HISTORY_FILE).read_text())
    assert history["alerts"]["rows"]["system/WIP"]["high_since"] == 80.0
    # Started again on the same directory: the sums go on.
    cycle, *_ = make(tmp_path, source=source)
    assert cycle.step()["alerts"]["rows"][0]["since"] == 80.0


def test_a_restart_on_a_hand_edited_or_other_limits_file_starts_again_and_says_so(tmp_path):
    shifted(tmp_path, [8.0] * 4)
    path = tmp_path / twin.HISTORY_FILE
    history = json.loads(path.read_text())
    history["alerts"]["rows"]["system/WIP"]["high"] = "a lot"
    path.write_text(json.dumps(history))
    doc = make(tmp_path)[0].step()
    assert "W_TWIN_ALERT" in [p["code"] for p in doc["problems"]]
    assert doc["alerts"]["rows"] == [] and doc["forecast"]["status"] == "ok"
    doc = make(tmp_path, alert_h=6.0)[0].step()
    said, = [p for p in doc["problems"] if p["code"] == "W_TWIN_ALERT"]
    assert "other limits" in said["message"] and doc["alerts"]["h"] == 6.0


def test_the_limits_are_the_callers_and_only_the_callers():
    with pytest.raises(ValueError):
        make(None, alert_h=0.0)
    with pytest.raises(ValueError):
        make(None, alert_k=-1.0)
    with pytest.raises(ValueError):
        make(None, alert_h=float("nan"))
    # A larger h: the same shift alerts later, or not at all.
    assert shifted(None, [4.0 + 3 * SD] * 3, alert_h=40.0)[2][-1]["alerts"]["rows"] == []


def test_the_command_takes_its_limits_from_flags(tmp_path, capsys):
    from test_twin_cycle import on_disk, slow_line

    model, mapping, log = on_disk(tmp_path, slow_line())
    common = [model, "--map", mapping, "--log", log, "--horizon", "10", "--replications", "2"]
    out = tmp_path / "out"
    assert twin.main(["once", *common, "--out", str(out), "--alert-k", "1", "--alert-h", "6"]) == 0
    doc, _ = twin.read_result(out)
    assert (doc["alerts"]["k"], doc["alerts"]["h"]) == (1.0, 6.0)
    capsys.readouterr()
    assert twin.main(["status", str(out)]) == 0
    assert "alerts: No alert: no compared series shows a sustained shift (k = 1, h = 6)." \
        in capsys.readouterr().out
    for bad in (["--alert-h", "0"], ["--alert-k", "-1"], ["--alert-h", "nan"], ["--alert-k", "x"]):
        with pytest.raises(SystemExit) as stopped:
            twin.main(["once", *common, "--out", str(out), *bad])
        assert stopped.value.code == 2


def test_alerts_on_every_series_keep_the_result_under_its_bound(tmp_path):
    names = [f"level/{'x' * 200}{i}" for i in range(80)]

    class Many(Source):
        def observe(self):
            return {**super().observe(), "observed": {name: 99.0 for name in names}}

    def run(model, **_):
        start = model["initial_state"]["clock"]
        return types.SimpleNamespace(kpis=Frame(), bands=bands(start, 60.0, names))

    cycle, source, clock, _calls = make(tmp_path, source=Many(), runner=run)
    for _ in range(3):
        doc = cycle.step()
        source.at += 10.0
        clock.tick(300.0)
    assert len(doc["alerts"]["rows"]) == score.MAX_SERIES
    assert len(json.dumps(doc)) < twin.LIMITS["result_bytes"] / 10
    assert "view" in doc and doc["view"] is not None


# ---------------------------------------------------------------- shown
def test_the_monitor_says_an_alert_as_text_and_escapes_it(tmp_path):
    _cycle, _source, docs = shifted(tmp_path, [8.0] * 3)
    doc = json.loads(json.dumps(docs[-1]))
    doc["alerts"]["rows"][0]["message"] = "</script><img src=x onerror=alert(1)>"
    shown = monitor.summary(doc)["alerts"]
    assert shown["alerting"] == 1 and shown["lines"][0].startswith("Alert: </script><img")
    assert shown["lines"][-1] == monitor.ALERTED and "not independent" in monitor.ALERTED
    page = monitor.Page({}, tmp_path / "m.html").build(doc, [])
    assert "<img src=x" not in page and "\\u003c\\/script\\u003e\\u003cimg" in page
    assert 'id="m-alerts"' in page
    # A result from before alerts shows none; one without a row says why there is none.
    assert monitor.summary({**doc, "alerts": None})["alerts"]["lines"][0].startswith("No alert")
    old = dict(doc)
    del old["alerts"]
    assert monitor.summary(old)["alerts"] is None
