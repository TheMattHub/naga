"""The time-series sampler is taken by the run loop, not a SimPy process
(docs/history/PERFORMANCE-REPORT-2026-10-07.md, S1): it schedules no event, and a
session that moves the clock samples the same instants with the same state."""
import copy
import json
from pathlib import Path

from nagasim.core.session import SimSession
from nagasim.core.sim_runner import setup_run, step_until

MODEL = json.loads((Path(__file__).resolve().parents[1] / "nagasim" / "models"
                    / "single_server.json").read_text(encoding="utf-8"))


def test_sampling_schedules_no_event():
    quiet = copy.deepcopy(MODEL)
    quiet["simulation"]["timeseries_points"] = 0
    scheduled, samples = [], []
    for layout in (MODEL, quiet):
        env, _model = setup_run(layout, seed=0, run_len=1000, warmup=0)
        step_until(env, 1000)
        scheduled.append(next(env._eid))        # simpy numbers every event it schedules
        samples.append(len(env.ctx.timeseries))
    assert scheduled[0] == scheduled[1]
    assert samples[0] > 0 and samples[1] == 0


def test_an_edit_between_two_ticks_is_sampled_on_the_right_side_and_replays():
    """Ticks every 5: capacity cut at 202.5 and restored at 312.5."""
    s = SimSession(MODEL, seed=3)
    assert s.advance(to=202.5) == "time"
    s.set_capacity("Server", 0)
    assert s.advance(to=312.5) == "time"
    s.set_capacity("Server", 1)
    results = s.finish()
    series = results["timeseries"]
    capacity = series[series["Series"] == "resource/Server/capacity"].set_index("Time")["Value"]
    assert list(capacity.index) == [5.0 * k for k in range(201)]
    assert all(capacity[t] == (0.0 if 202.5 < t < 312.5 else 1.0) for t in capacity.index)

    replayed = SimSession.replay(MODEL, 3, s.game_log)
    assert set(replayed) == set(results)
    for name in results:
        assert replayed[name].equals(results[name]), name


def test_a_drain_is_not_asked_about_as_if_it_were_a_step():
    """`should_stop` is asked once before each event and each sample, and not
    once more when the model drains with a tick still ahead (final review of
    the performance work): a session counts its steps from those calls."""
    layout = copy.deepcopy(MODEL)
    for block in layout["blocks"]:
        if block["type"] == "CREATE":
            block["params"]["max_arrivals"] = 5      # drains long before the run length
    env, _model = setup_run(layout, seed=0, run_len=1000, warmup=0)
    asked, stepped, real_step = [], [], env.step
    env.step = lambda: (stepped.append(1), real_step())
    step_until(env, 1000, lambda: asked.append(1) and False)
    samples = len({row[0] for row in env.ctx.timeseries})
    assert env.peek() == float("inf") and env.ctx.sampler is None       # drained, sampler stopped
    assert stepped and samples and len(asked) == len(stepped) + samples
