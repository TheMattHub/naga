"""Speed review A+B (2026-11-23): a PROCESS on a plain resource seizes down
`seizing.acquire_plain`; every other seize takes the full `acquire`. Each feature that
rules the short path out is shown to take the full path, and every model gives the same
results whichever path it takes (`_full_path_only` forces the full one)."""
import pandas as pd
import pytest

from conftest import chain, create, dispose, layout, process
from nagasim import api
from nagasim.core import seizing
from nagasim.core.blocks import process as process_module

RUN = {"time_limit": 60, "warmup": 5, "replications": 1}
EXPO = {"distribution": "Exponential", "dist_params": {"rate": 1.0}}


def _line(stations=None, resources=None, simulation=None, **sections):
    """Exponential arrivals into a chain of PROCESS blocks on their own servers."""
    c = create("c", **EXPO)
    stations = stations or [process("p", value=1.2, distribution="Exponential")]
    d = dispose("d")
    blocks = [c, *stations, d]
    resources = resources or [{"name": s["params"]["resource_name"], "capacity": 1}
                              for s in stations]
    return layout(blocks, chain(*blocks), resources=resources,
                  simulation=dict(simulation or RUN), **sections)


def _two_outputs(doc, block):
    """A queue behaviour needs a second link (D54): to the dispose."""
    doc["links"].append({"source": block, "target": "d"})
    return doc


def _tandem():
    return _line([process("p1", resource="S1", value=1.2, distribution="Exponential"),
                  process("p2", resource="S2", value=1.1, distribution="Exponential")])


def _set():
    doc = _line([process("p", resource="Crew", value=0.6, distribution="Exponential")],
                resources=[{"name": "A", "capacity": 1}, {"name": "B", "capacity": 1}])
    doc["resource_sets"] = [{"name": "Crew", "members": ["A", "B"]}]
    return doc


def _multi_unit():
    return _line([process("p", value=0.5, distribution="Exponential", quantity=2)],
                 resources=[{"name": "Server", "capacity": 3}])


def _lifo():
    return _line(resources=[{"name": "Server", "capacity": 1, "discipline": "LIFO"}])


def _patience():
    return _two_outputs(_line([process("p", value=1.2, distribution="Exponential",
                                       renege_after="1.5")]), "p")


def _preempt():
    hi = process("hi", value=0.4, distribution="Exponential", priority=0, preempt=True)
    lo = process("lo", value=0.8, distribution="Exponential", priority=1)
    c1, c2, d = create("c1", **EXPO), create("c2", **EXPO), dispose("d")
    return layout([c1, c2, hi, lo, d],
                  [*chain(c1, hi, d), *chain(c2, lo, d)],
                  resources=[{"name": "Server", "capacity": 1}], simulation=dict(RUN))


def _space():
    doc = _tandem()
    doc["blocks"][2]["params"]["queue_capacity"] = 1       # p2: one place, so p1 blocks
    return doc


def _power():
    doc = _line(resources=[{"name": "Server", "capacity": 1, "power": {"busy": 10, "idle": 2}}])
    doc.update(levels=[{"name": "Meter", "initial": 0}],
               flows=[{"name": "draw", "to": "Meter", "rate": 'POWER("Server")'}])
    return doc


def _warm_start():
    """Only warm-started entities, waiting since before the clock (`_since`); arrivals
    start after the run ends. One with no `since` would enter as a plain arrival."""
    c = create("c", start_time=1000)
    p, d = process("p", value=1.5), dispose("d")
    return layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 1}],
                  simulation={"time_limit": 20, "warmup": 0, "replications": 1},
                  initial_state={"clock": 8.0, "entities": [
                      {"block": "p", "count": 1, "in_service": 1, "since": 7.0},
                      {"block": "p", "count": 3, "since": 7.5}]})


def _schedule():
    return _line(schedules=[{"name": "night", "kind": "capacity",
                             "points": [[10, 0], [4, 1]], "repeat": True}],
                 resources=[{"name": "Server", "capacity": 1, "capacity_schedule": "night"}])


def _failure():
    return _line(failures=[{"name": "F", "resources": ["Server"],
                            "uptime": {"distribution": "Exponential", "dist_params": {"rate": 0.1}},
                            "downtime": {"distribution": "Constant", "dist_params": {"value": 2.0}}}])


def _worker_only():
    """A PROCESS with no resource of its own (a worker does the job) seizes nothing here."""
    doc = _line([process("p", resource="", value=0.5, worker_fleet="Ops", node="n1")])
    doc["resources"] = []
    doc["network"] = {"nodes": [{"name": "n0", "x": 0, "y": 0}, {"name": "n1", "x": 5, "y": 0}],
                      "segments": [{"from": "n0", "to": "n1"}]}
    doc["fleets"] = [{"name": "Ops", "kind": "worker", "count": 1, "home": "n0", "speed": 1.0}]
    return doc


def _logs_off():
    return _line(simulation={**RUN, "record_logs": False, "timeseries_points": 0})


def _costs():
    return _line(resources=[{"name": "Server", "capacity": 1, "cost_busy": 6.0,
                             "cost_per_use": 0.5}])


# The feature, and the blocks it keeps on the full path. "lo" shares the preempted resource
# but does not preempt: its request is the plain one, and a job of it that is preempted
# resumes down the full path (`Process._serve`).
FULL_PATH = {"set": (_set, {"p"}), "multi_unit": (_multi_unit, {"p"}), "lifo": (_lifo, {"p"}),
             "patience": (_patience, {"p"}), "preempt": (_preempt, {"hi"}),
             "space": (_space, {"p1", "p2"}), "power": (_power, {"p"}),
             "warm_start": (_warm_start, {"p"}), "worker_only": (_worker_only, {"p"})}
SHORT_PATH = {"plain": _line, "tandem": _tandem, "schedule": _schedule, "failure": _failure,
              "logs_off": _logs_off, "costs": _costs}


def _run(doc):
    return api.run_model(doc, seed=3)


def _same(a, b):
    assert a.keys() == b.keys()
    for key in a:
        if isinstance(a[key], pd.DataFrame):
            pd.testing.assert_frame_equal(a[key], b[key], check_exact=True, obj=key)
        else:
            assert a[key] == b[key], key


def _spy(monkeypatch):
    taken = set()

    def spy(block, *args, **kwargs):
        taken.add(block.name)
        return seizing.acquire_plain(block, *args, **kwargs)
    monkeypatch.setattr(process_module, "acquire_plain", spy)
    return taken


def _full_path_only(monkeypatch):
    """Every seize down the full path: no block is plain (`process.py` imports `plain_target`
    by name, so it is patched there), and the short path is never taken."""
    taken = _spy(monkeypatch)
    monkeypatch.setattr(process_module, "plain_target", lambda block: None)
    return taken


@pytest.mark.parametrize("name", sorted(FULL_PATH))
def test_a_feature_takes_the_full_path(name, monkeypatch):
    build, blocks = FULL_PATH[name]
    taken = _spy(monkeypatch)
    results = _run(build())
    assert len(results["resource_summary"]) > 0
    assert not taken & blocks


@pytest.mark.parametrize("name", sorted(SHORT_PATH))
def test_a_plain_resource_takes_the_short_path(name, monkeypatch):
    taken = _spy(monkeypatch)
    _run(SHORT_PATH[name]())
    assert taken


@pytest.mark.parametrize("name", ["schedule", "failure"])
def test_the_schedule_and_failure_cases_drive_their_resource(name):
    """The short path must meet the blockers of a live controller, not a plain model."""
    from nagasim.core.sim_runner import setup_run, step_until
    env, _model = setup_run(SHORT_PATH[name](), seed=3, run_len=60, warmup=0.0)
    step_until(env, 60)
    server = env.ctx.resource_manager.get("Server")
    assert server.has_controller and server.available_area(60) < 60


@pytest.mark.parametrize("name", sorted({**FULL_PATH, **SHORT_PATH}))
def test_both_paths_give_the_same_results(name, monkeypatch):
    doc = SHORT_PATH[name] if name in SHORT_PATH else FULL_PATH[name][0]
    short = _run(doc())
    taken = _full_path_only(monkeypatch)
    full = _run(doc())
    assert not taken
    _same(short, full)


def test_a_watched_run_gives_the_same_events_on_both_paths(monkeypatch):
    """A subscriber sees the same seize and release events, in the same order."""
    from nagasim.core.sim_runner import setup_run, step_until

    def events():
        env, _model = setup_run(_tandem(), seed=3, run_len=30, warmup=0.0)
        seen = []
        env.ctx.bus.subscribe(lambda name, **p: seen.append(
            (name, round(p.get("t", -1), 12), getattr(p.get("entity"), "id", None),
             p.get("resource"), p.get("value"))))
        step_until(env, 30)
        return seen
    short = events()
    taken = _full_path_only(monkeypatch)
    assert events() == short and any(e[0] == "entity_seized" for e in short)
    assert not taken
