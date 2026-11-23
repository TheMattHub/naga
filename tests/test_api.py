"""M2: the headless API runs a model with no GUI object anywhere. Also covers
every curated example model in nagasim/models/ (formerly test_examples_load.py):
each must load and run through the same api.run_model entry point (DEVLOG M0)."""
import json
from pathlib import Path

import openpyxl
import pytest

from nagasim import api
from nagasim.core.schema import ModelValidationError

from conftest import metric_value
from tools.make_golden import (GOLDEN_DIR, MODELS as GOLDEN_MODELS, SEEDS,
                               golden_hash, golden_payload)

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
MODELS = sorted(MODELS_DIR.glob("*.json"))
KEYS = {"system", "entity_summary", "entity_log", "resource_summary", "resource_log",
        "store_summary", "store_log", "user_summary", "order_summary", "due_date_summary",
        "queue_summary", "motion_summary", "level_summary", "flow_summary", "conversion_summary",
        "component_summary", "cost_summary", "energy_summary",
        "fleet_summary", "segment_summary", "conveyor_summary", "buffer_summary",
        "charging_summary", "battery_summary", "tending_summary",
        "material_flow_summary", "bottleneck_summary", "schedule_log",
        "agent_summary", "agent_attributes", "agent_attribute_log", "timeseries", "dashboard"}


def test_examples_exist():
    assert len(MODELS) >= 3, "curated examples missing"


@pytest.mark.slow
def test_run_model_returns_the_frames_with_rows_per_replication(curated_results):
    # One loop over every bundled example rather than one test each, over the
    # session-wide `curated_results` runs (conftest) rather than its own: the
    # runs are what cost, and the loop reports the first example that breaks.
    # D1321: only the frames with rows come back ("system" always); the rest of
    # KEYS are named in `_dropped`, so nothing is lost without a trace.
    # a belt with points; a photo-eye's frame too (D1347), only when a model has one
    extra = {"conveyor_stops", "conveyor_diverts", "conveyor_merges", "conveyor_eyes"}
    for path, results in curated_results.items():
        assert "system" in results, path.stem
        empty = sorted(name for name, frame in results.items() if frame.empty and name != "system")
        assert not empty, (path.stem, empty)
        assert set(results) | set(results._dropped) | extra >= KEYS, path.stem
        assert set(results) - extra <= KEYS and not set(results) & set(results._dropped), path.stem
        assert set(results["system"]["Run"]) == {0, 1}, path.stem
        # A purely continuous example has no entities at all (PHASE4 D1); its
        # own statistics are checked by the test below.
        if "flow_summary" in results:
            continue
        # An agent-based example (6D1 U3 templates) creates no entities
        # either (PHASE5 AB-1) -- check an agent KPI instead.
        if "agent_summary" in results:
            assert results["agent_summary"]["Max"].max() > 0, path.stem
            continue
        assert metric_value(results, "Total Entities Created") > 0, path.stem


def test_keep_empty_returns_every_frame_and_the_default_names_the_dropped_ones():
    """D1321: a single server gets the frames with rows (7, not 33); keep_empty=True gives all."""
    args = dict(replications=2, run_length=200, warmup=20, seed=0)
    lean = api.run_model("single_server", **args)
    full = api.run_model("single_server", keep_empty=True, **args)
    assert set(full) == KEYS and full._dropped == ()
    assert set(lean) == {name for name, frame in full.items() if len(frame)} | {"system"}
    assert set(lean._dropped) == KEYS - set(lean) and len(lean) == 7
    with pytest.raises(KeyError, match="no rows in this run"):
        lean["store_summary"]
    assert lean.get("store_summary") is None and "store_summary" not in lean


def test_run_scenarios_follows_the_same_rule():
    """D1328: each scenario keeps only the frames with rows (`system` always); keep_empty=True keeps all."""
    from nagasim import experiments
    layout = json.loads((Path(api.__file__).parent / "models" / "single_server.json").read_text(encoding="utf-8"))
    layout["scenarios"] = [{"name": "Faster", "overrides": [
        {"path": "blocks.process_1.params.dist_params.rate", "value": 1.6}]}]
    args = dict(replications=2, run_length=200, warmup=20, seed=0)
    lean = experiments.run_scenarios(layout, **args)
    full = experiments.run_scenarios(layout, keep_empty=True, **args)
    for name, frames in lean.results.items():
        assert len(frames) == 7 and "system" in frames and set(full.results[name]) == KEYS
        assert set(frames) == {k for k, f in full.results[name].items() if len(f)} | {"system"}
        assert frames.get("store_summary") is None
    assert lean.comparison() is not None


@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_every_example_produces_statistics_under_its_own_settings(path):
    """DEVLOG 2026-09-20. `curated_results` runs every example at run_length 40
    with no warm-up, which is exactly why `clinic_library.json` could ship
    unable to produce a single post-warm-up statistic the way a USER runs it
    (`run_model(path)`): 30 arrivals, all of them gone before a warm-up of 200.

    So each example is run its own way here -- its own warm-up, its own
    horizon -- with one replication and a horizon capped at `warmup + 200`,
    which always clears the warm-up. The oracle is the run warning itself:
    `Short Statistics Window %` appears only when the statistics cover under a
    tenth of the horizon asked for. One case per example (suite reduction,
    6D): the ~3 s sweep was a single long pole that xdist could not spread;
    the check that the warning really fires is the test below."""
    raw = json.loads(path.read_text())
    sim = raw.get("simulation", {})
    warmup = float(sim.get("warmup") or 0.0)
    limit = float(sim.get("time_limit") or 0.0)
    horizon = min(limit, warmup + 200.0) if limit else warmup + 200.0
    results = api.run_model(str(path), replications=1, seed=0, warmup=warmup,
                            run_length=horizon)
    metrics = dict(zip(results["system"]["Metric"], results["system"]["Value"]))
    assert "Short Statistics Window %" not in metrics
    assert metrics["Simulation Time"] > 0
    # A purely CONTINUOUS example has no entities at all (PHASE4 D1): its
    # statistics are the level and flow frames, which must not be empty.
    if raw.get("flows"):
        assert not results["flow_summary"].empty
        assert float(results["flow_summary"]["Total"].sum()) > 0
        return
    # An agent-based example (6D1 U3 templates) has no entities either
    # (PHASE5 AB-1) -- check its agent population instead.
    if raw.get("populations"):
        assert not results["agent_summary"].empty
        assert results["agent_summary"]["Max"].max() > 0
        return
    assert metrics["Total Entities Created"] > 0
    assert metrics["Total Entities Completed"] > 0


def test_the_short_statistics_window_warning_really_fires():
    """The oracle of the test above really fires: 5 arrivals, all gone by t=5,
    warm-up 50."""
    from conftest import chain, layout as make_layout

    blocks = [_create(5), _dispose()]
    broken = api.run_model(make_layout(blocks, chain(*blocks)), replications=1,
                           warmup=50, run_length=1000)
    assert dict(zip(broken["system"]["Metric"], broken["system"]["Value"]))[
        "Short Statistics Window %"] == 0.0


def test_run_model_accepts_a_layout_dict(simple_model_path):
    layout = json.loads(Path(simple_model_path).read_text())

    results = api.run_model(layout, replications=2, run_length=20)

    assert set(results["system"]["Run"]) == {0, 1}


# -- run_model fallback + non-draining guard (ROADMAP 1.5) ------------------

def _create(max_arrivals):
    return {"name": "create_1", "description": "create_1", "type": "CREATE", "position": [0, 0],
            "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                      "entities_per_arrival": 1, "max_arrivals": max_arrivals, "start_time": 0}}


def _dispose():
    return {"name": "dispose_1", "description": "dispose_1", "type": "DISPOSE", "position": [1, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}}


def test_run_model_fallback_uses_replications_and_run_length_from_the_model(make_model):
    """max_arrivals=0 never drains on its own, so `run_length` from the
    model's `simulation.time_limit` is exactly the horizon reached."""
    path = make_model([_create(0), _dispose()], [["create_1", "dispose_1"]],
                      simulation={"time_limit": 30, "warmup": 0, "replications": 4})

    results = api.run_model(path)   # replications, warmup, run_length all omitted

    assert set(results["system"]["Run"]) == {0, 1, 2, 3}
    assert metric_value(results, "Simulation Time") == pytest.approx(30.0)


def test_run_model_explicit_values_override_the_model_settings(make_model):
    path = make_model([_create(0), _dispose()], [["create_1", "dispose_1"]],
                      simulation={"time_limit": 30, "warmup": 0, "replications": 4})

    results = api.run_model(path, replications=1, run_length=10, warmup=0)

    assert set(results["system"]["Run"]) == {0}
    assert metric_value(results, "Simulation Time") == pytest.approx(10.0)


# The ten columns the flat logs ARE: event-sampled cosmetics, already inexact
# by design (event averages, and truncated on a long run -- DEVLOG #21, #23).
# Everything else in every frame comes from a collector and must not move.
_SNAPSHOT_ONLY = {"Queue Length Mode", "Queue Length Mode %", "Queue Len P95", "Queue Len P99",
                  "Queue Len Std", "Mode Size", "Mode Size %", "P95 Size", "P99 Size", "Std Size"}


def test_record_logs_off_keeps_every_collector_statistic_and_empties_the_logs():
    """PHASE4 T7. `record_logs=False` stops the per-event logs being built; the
    two empty log frames are left out (D1321), the entity log keeps no disposed rows, and
    every statistic that comes from a collector is bit-identical."""
    from nagasim import experiments

    model = str(MODELS_DIR / "warehouse_library.json")
    on = api.run_model(model, replications=1, seed=0)
    off = api.run_model(model, replications=1, seed=0, record_logs=False)

    for key in ("resource_log", "store_log"):
        assert key not in off and not on[key].empty     # no rows, so left out (D1321)
    assert len(off["entity_log"]) < len(on["entity_log"])      # only live entities remain

    def kpis(frames):
        flat = experiments.flatten_kpis(frames)
        return {k: v for k, v in zip(flat["KPI"], flat["Value"])}

    before, after = kpis(on), kpis(off)
    moved = {k for k in before if before[k] != after.get(k)}
    assert moved, "the snapshot cosmetics are expected to go NaN"
    assert {k.rsplit("/", 1)[-1] for k in moved} <= _SNAPSHOT_ONLY
    for key in ("resource_summary", "store_summary", "entity_summary"):
        assert len(off[key]) == len(on[key])   # every one of them still reports


def test_run_model_raises_for_a_never_ending_create_with_no_run_length(make_model):
    path = make_model([_create(0), _dispose()], [["create_1", "dispose_1"]])
    with pytest.raises(ModelValidationError):
        api.run_model(path)


def test_run_model_raises_for_a_failure_with_no_run_length(make_model):
    path = make_model(
        [_create(5), _dispose()], [["create_1", "dispose_1"]],
        resources=[{"name": "Server", "capacity": 1, "discipline": "FIFO"}],
        failures=[{"name": "breakdown", "resources": ["Server"],
                  "uptime": {"distribution": "Constant", "dist_params": {"value": 100}},
                  "downtime": {"distribution": "Constant", "dist_params": {"value": 10}}}])
    with pytest.raises(ModelValidationError):
        api.run_model(path)


def test_run_model_raises_for_a_repeating_capacity_schedule_with_no_run_length(make_model):
    path = make_model(
        [_create(5), _dispose()], [["create_1", "dispose_1"]],
        resources=[{"name": "Server", "capacity": 5, "discipline": "FIFO",
                   "capacity_schedule": "shift"}],
        schedules=[{"name": "shift", "kind": "capacity", "repeat": True, "points": [[10, 5]]}])
    with pytest.raises(ModelValidationError):
        api.run_model(path)


def test_run_model_does_not_raise_for_a_bounded_create_model(make_model):
    path = make_model([_create(5), _dispose()], [["create_1", "dispose_1"]])
    results = api.run_model(path)
    assert results is not None
    assert metric_value(results, "Total Entities Created") == 5


def test_cli_writes_the_ten_sheet_workbook(simple_model_path, tmp_path, capsys):
    """Also covers DEVLOG #8: simple_model_path has no SEIZE block, so this is an
    end-to-end proof that a no-SEIZE export no longer raises KeyError on 'Bottleneck %'.

    `api.main` IS the CLI (`python -m product.api` is one line that calls it),
    so it runs here rather than in a second interpreter: tests/test_packaging.py
    owns the one subprocess that proves the module entry points dispatch
    (suite reduction, P4-D5)."""
    out = tmp_path / "cli.xlsx"

    assert api.main([simple_model_path, "--replications", "2", "--run-length", "20",
                     "--out", str(out)]) == 0

    assert "Simulation Time" in capsys.readouterr().out
    # +1 over the 10-sheet baseline: this model has entity traversals, so
    # "Material Flow" (PHASE6 W3 M2, always collected) gets a sheet.
    assert len(openpyxl.load_workbook(out).sheetnames) == 11


def test_cli_writes_a_report(simple_model_path, tmp_path):
    out = tmp_path / "report.html"
    rc = api.main([simple_model_path, "--replications", "1", "--run-length", "20",
                  "--report", str(out)])
    assert rc == 0
    assert out.exists()
    assert "<html" in out.read_text(encoding="utf-8").lower()


# -- workers="auto" (ROADMAP 1.7 E) -----------------------------------------
# PHASE4 T9: the rule is asserted with its inputs GIVEN, not read off this
# machine. The version that took the core count from os.cpu_count() passed here
# and failed on a 2-core CI runner, because the old cap reserved a core and so
# handed back a single worker -- i.e. the test was measuring the runner, and the
# product rule it was measuring was itself wrong on a small box.
@pytest.mark.parametrize("elapsed,remaining,cpus,setup_cost,expected", [
    (2.0, 4, 8, 3.0, 4),      # plenty of work, plenty of cores -> one per task
    (2.0, 20, 16, 3.0, 8),    # ... and never more than the cap
    (2.0, 4, 2, 3.0, 2),      # a 2-core CI runner still pools, on both cores
    (2.0, 4, 1, 3.0, 1),      # one core: a pool would only add overhead
    (0.01, 5, 8, 3.0, 1),     # 0.05 s of work left is not worth 3 s of pool
    (0.01, 5, 8, 0.3, 1),     # ... nor 0.3 s of a fork pool
    (0.1, 50, 8, 3.0, 8),     # many quick design points DO add up (T3), to the cap of 8
    (0.1, 4, 8, 0.3, 4),      # fork: 0.4 s of work clears the cheaper bar
    (0.1, 4, 8, 3.0, 1),      # spawn: the same work does not
    (2.0, 2, 8, 3.0, 1),      # fewer than 3 tasks left: never worth it
])
def test_auto_workers_decides_on_work_left_cores_and_the_pools_own_cost(
        elapsed, remaining, cpus, setup_cost, expected):
    assert api.auto_workers(elapsed, remaining, cpus=cpus, setup_cost=setup_cost) == expected


def test_pool_setup_cost_is_cheaper_where_workers_are_forked():
    """Charging fork the price of spawn kept Linux sequential for workloads
    that are worth pooling there (PHASE4 T9)."""
    monkey = pytest.MonkeyPatch()
    with monkey.context() as m:
        m.setattr(api.multiprocessing, "get_start_method", lambda allow_none=False: "fork")
        forked = api.pool_setup_cost()
        m.setattr(api.multiprocessing, "get_start_method", lambda allow_none=False: "spawn")
        spawned = api.pool_setup_cost()
    assert 0 < forked < spawned


def test_workers_auto_uses_the_pool_when_the_first_replication_is_slow(monkeypatch):
    calls = []

    def fake_pool(path, seeds, run_length, warmup, workers, on_progress, should_stop, total, done,
                 **kwargs):
        calls.append(workers)
        return [(s, api.run_single_simulation(path, seed=s, run_len=run_length, warmup=warmup))
               for s in seeds]

    monkeypatch.setattr(api, "_run_pool", fake_pool)
    # The machine's own core count and start method are pinned: this test is
    # about run_model TAKING the pool branch, not about where the crossover is.
    monkeypatch.setattr(api.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(api, "pool_setup_cost", lambda: 3.0)
    times = iter([0.0, 2.0])
    monkeypatch.setattr(api.time, "perf_counter", lambda: next(times))

    results = api.run_model(str(MODELS_DIR / "single_server.json"), replications=5,
                            run_length=50, warmup=0, workers="auto")
    assert results is not None
    assert calls == [4], "the pool path should have been chosen for a slow first replication"


def test_workers_auto_stays_sequential_when_the_first_replication_is_fast(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the pool should not run for a fast first replication")

    monkeypatch.setattr(api, "_run_pool", boom)
    times = iter([0.0, 0.01])
    monkeypatch.setattr(api.time, "perf_counter", lambda: next(times))

    results = api.run_model(str(MODELS_DIR / "single_server.json"), replications=5,
                            run_length=50, warmup=0, workers="auto")
    assert results is not None
    assert set(results["system"]["Run"]) == {0, 1, 2, 3, 4}


# ============================ golden determinism (ROADMAP 2.5) ============
def _golden_files():
    # `view_state.json` (PHASE7 7.7) lives in the same folder and is not a KPI record.
    return sorted(GOLDEN_DIR.glob("*_seed*.json"))


def test_golden_kpis_unchanged():
    for path in _golden_files():
        record = json.loads(path.read_text())
        payload = golden_payload(record["model"], record["seed"])
        assert golden_hash(payload) == record["hash"], (
            f"{record['model']} seed={record['seed']}: KPIs drifted.\n"
            f"expected: {json.dumps(record['payload'], indent=2, sort_keys=True)}\n"
            f"actual:   {json.dumps(payload, indent=2, sort_keys=True)}"
        )


def test_golden_covers_every_model_and_seed():
    """The fixture set itself: every (model, seed) tools/make_golden.py
    declares actually has a golden file -- catches a generation gap, not a
    KPI drift."""
    have = {(json.loads(p.read_text())["model"], json.loads(p.read_text())["seed"])
            for p in _golden_files()}
    want = {(model, seed) for model in GOLDEN_MODELS for seed in SEEDS}
    assert have == want


def test_a_pool_workers_warnings_are_logged_once_in_the_parent(caplog):
    """A replication run in a worker process hands back what it logged; the
    parent logs it again, once however many replications repeat it -- so a
    window started without a console still gets it (the Log dock)."""
    from conftest import chain, create, dispose, layout, process

    blocks = [create(), process("work", "M", 1.0), dispose(), process("orphan", "M", 1.0)]
    doc = layout(blocks, chain(*blocks[:3]) + chain(blocks[3], blocks[2]),
                 resources=[{"name": "M", "capacity": 1}])
    with caplog.at_level("WARNING"):
        api.run_model(doc, replications=3, seed=0, run_length=20, workers=2)
    unreachable = [r for r in caplog.records if "orphan is unreachable" in r.getMessage()]
    assert len(unreachable) == 1 and unreachable[0].levelname == "WARNING"


def test_interval_and_json_keys_agree(capsys):
    path = str(MODELS_DIR / "single_server.json")
    argv = ["--replications", "4", "--run-length", "300"]
    assert api.main([path, *argv, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["kpis"]
    results = api.run_model(path, replications=4, run_length=300)
    assert rows and all(r["key"] == "system/" + r["metric"] and r["ci95"] == r["half_width"]
                        for r in rows)
    row = rows[0]
    got = results.interval(row["key"])
    assert (got.mean, got.half_width, got.n) == (row["mean"], row["half_width"], row["n"])
    assert results.kpis().set_index("metric").loc[row["key"], "ci95"] == got.half_width
    one = api.run_model(path, replications=1, run_length=300).interval(row["key"])
    assert one.n == 1 and one.half_width != one.half_width       # NaN
    with pytest.raises(KeyError):
        results.interval("system/nope")


def test_results_repr_is_the_kpi_summary():
    results = api.run_model(str(MODELS_DIR / "single_server.json"), replications=3, run_length=300)
    text = repr(results)
    assert len(text) < 2000
    assert "system" in text and "entity_summary" in text        # the frame names
    assert "system/Avg Throughput Time" in text or "system/Total Entities Created" in text
    assert repr(api.Results()) and "Results" in repr(api.Results())     # empty: no failure


def test_run_model_takes_a_bundled_name(tmp_path, monkeypatch):
    by_name = api.run_model("single_server", replications=1, run_length=300)
    by_path = api.run_model(str(MODELS_DIR / "single_server.json"), replications=1, run_length=300)
    assert by_name.kpi("system/Total Entities Created") == by_path.kpi("system/Total Entities Created")
    monkeypatch.chdir(tmp_path)         # a file at that path always wins over the bundled model
    (tmp_path / "single_server").write_text("not json")
    with pytest.raises(Exception) as err:
        api.run_model("single_server", replications=1)
    assert "bundled" not in str(err.value)


def test_a_wrong_call_says_what_is_wrong_and_names_the_nearest(capsys):
    """#1899: replications under 1 are refused (they returned None), a misspelt bundled name or
    KPI key names the nearest, and the CLI says the bundled name once."""
    from nagasim import cli
    for bad in (0, -3, 2.5):
        with pytest.raises(ValueError, match=f"replications must be a whole number of at least 1, not {bad!r}"):
            api.run_model("single_server", replications=bad)
    with pytest.raises(FileNotFoundError, match="did you mean the bundled model 'single_server'"):
        api.run_model("single_sever", replications=1)
    assert cli.main(["run", "single_sever"]) == 2
    assert capsys.readouterr().err.count("single_server") == 1
    results = api.run_model("single_server", replications=2, run_length=200)
    with pytest.raises(KeyError, match="did you mean 'system/Avg Queue Length'"):
        results.kpi("system/Avg Queue Lenght")
    with pytest.raises(KeyError, match=r"see results\.kpis\(\)"):
        results.interval("zzz")


def test_the_first_pages_modules_load_on_first_use_and_import_stays_light():
    """#1897: after a bare `import nagasim`, dir() lists api, experiments and optimize and
    attribute access imports them; the import itself loads none of them."""
    import subprocess
    import sys
    code = ("import sys, nagasim\n"
            "assert not any(m in sys.modules for m in ('nagasim.api', 'nagasim.experiments', 'nagasim.optimize'))\n"
            "assert {'api', 'experiments', 'optimize'} <= set(dir(nagasim))\n"
            "assert callable(nagasim.experiments.run_scenarios) and callable(nagasim.api.run_model)\n"
            "assert callable(nagasim.optimize.tornado)\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
