"""Phase 11 H (D726): the paced clock. The loop waits so one model second takes
1/pace wall seconds; a late event runs at once and is counted; results never
depend on the pace."""
import copy
import json
import time
from pathlib import Path

import pytest

import nagasim
from nagasim import api
from nagasim.core.outputs import calc_kpis
from nagasim.core.sim_runner import Pacer, setup_run, step_until

MODEL = Path(nagasim.__file__).parent / "models" / "single_server.json"


def _layout(unit="seconds"):
    layout = json.loads(MODEL.read_text(encoding="utf-8"))
    layout.setdefault("simulation", {})["time_unit"] = unit
    return layout


class FakeClock:
    """A monotonic clock that moves only when slept on (or pushed by hand)."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _csv(results):
    return {name: frame.to_csv(float_format="%.17g") for name, frame in results.items()}


def test_paced_run_model_gives_the_unpaced_frames():
    plain = api.run_model(_layout(), replications=2, run_length=30, warmup=0, seed=4)
    paced = api.run_model(_layout(), replications=2, run_length=30, warmup=0, seed=4, pace=1e6)
    assert _csv(paced) == _csv(plain)
    assert plain.pace is None
    assert paced.pace["paced"] == 1e6
    assert set(paced.pace) == {"paced", "late_events", "max_lag_s"}


def test_fake_clock_paced_kpis_identical_and_waits_add_up():
    def run(pacer):
        env, _ = setup_run(_layout(), seed=2, run_len=20, warmup=0)
        assert not step_until(env, 20, pacer=pacer)
        return calc_kpis(env, warmup=0, run_id=2)

    clock = FakeClock()
    pacer = Pacer(4.0, clock=clock, sleep=clock.sleep)
    paced, plain = run(pacer), run(None)
    assert {k: v.to_csv() for k, v in paced.items()} == {k: v.to_csv() for k, v in plain.items()}
    # 20 model seconds at 4 model s per wall s, waited up to the horizon.
    assert clock.now == pytest.approx(5.0)
    assert max(clock.sleeps) <= Pacer.SLICE_S + 1e-12
    assert pacer.late_events == 0


def test_model_time_unit_converts_to_seconds():
    clock = FakeClock()
    pacer = Pacer(60.0, unit_s=60, clock=clock, sleep=clock.sleep)    # 1 model min per wall s
    pacer.begin(0.0)
    assert not pacer.wait(3.0)
    assert clock.now == pytest.approx(3.0)


def test_late_events_are_counted_never_raised():
    clock = FakeClock()
    pacer = Pacer(2.0, clock=clock, sleep=clock.sleep)
    pacer.begin(0.0)
    assert not pacer.wait(4.0)                 # due at wall 2: slept
    assert clock.now == pytest.approx(2.0) and pacer.late_events == 0
    clock.now = 10.0                           # the work took 8 s
    assert not pacer.wait(6.0)                 # due at wall 3: 7 s late, run at once
    assert not pacer.wait(6.0)
    assert pacer.late_events == 2
    assert pacer.max_lag_s == pytest.approx(7.0)
    assert clock.now == 10.0
    assert not pacer.wait(10.0, late=False)    # the horizon is not an event
    assert pacer.late_events == 2


def test_a_lag_within_the_tolerance_is_not_late():
    clock = FakeClock()
    pacer = Pacer(1.0, clock=clock, sleep=clock.sleep)
    pacer.begin(0.0)
    clock.now = Pacer.LATE_AFTER_S / 2
    pacer.wait(0.0)
    assert pacer.late_events == 0 and pacer.max_lag_s == pytest.approx(Pacer.LATE_AFTER_S / 2)


def test_should_stop_is_asked_in_slices_during_a_wait():
    clock = FakeClock()
    pacer = Pacer(1.0, clock=clock, sleep=clock.sleep)
    pacer.begin(0.0)
    asked = []

    def should_stop():
        asked.append(clock.now)
        return clock.now >= 0.5

    assert pacer.wait(100.0, should_stop)
    assert clock.now == pytest.approx(0.5)
    assert len(asked) == 6


def test_should_stop_ends_a_real_long_wait_quickly():
    started = time.monotonic()
    stop_at = started + 0.3
    results = api.run_model(_layout(), replications=1, run_length=1000, warmup=0, seed=0,
                            pace=0.001, should_stop=lambda: time.monotonic() >= stop_at)
    assert results is None
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), "fast", True])
def test_bad_pace_refused(bad):
    with pytest.raises(ValueError, match="pace"):
        Pacer(bad)


@pytest.mark.parametrize("workers", [2, "auto"])
def test_paced_replications_run_one_after_another(workers):
    with pytest.raises(ValueError, match="workers"):
        api.run_model(_layout(), replications=2, run_length=5, pace=1e6, workers=workers)


def test_cli_pace_prints_its_summary(capsys, tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_layout()), encoding="utf-8")
    assert api.main([str(path), "--replications", "1", "--run-length", "5", "--pace", "1e6"]) == 0
    out = capsys.readouterr().out
    assert "paced at 1e+06x" in out and "late events" in out


def test_cli_pace_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        api.main([str(MODEL), "--pace", "0"])


@pytest.mark.perf
@pytest.mark.slow
def test_ten_seconds_at_pace_ten_takes_about_one_second():
    started = time.monotonic()
    results = api.run_model(_layout(), replications=1, run_length=10, warmup=0, seed=0, pace=10)
    elapsed = time.monotonic() - started
    assert 0.8 <= elapsed <= 3.0, elapsed
    assert results.pace["paced"] == 10
