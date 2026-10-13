"""State in (PHASE7 7.10, D231-D238): the clock, the vocabulary, a state as a document and
the exit check, "a warm start from a state reproduces the uninterrupted run"."""
import json
from pathlib import Path

import numpy as np
import pytest

import nagasim
from conftest import chain, create, delay, dispose, layout, metric_value, problem_codes, process

from nagasim import api, experiments, state
from nagasim.core.outputs import LOG_DERIVED_COLUMNS
from nagasim.core.schema import ModelValidationError
from nagasim.core.session import SimSession

MODELS = Path(nagasim.__file__).resolve().parent / "models"
SERVER = [{"name": "Server", "capacity": 1}]


def _line(**sections):
    """Arrivals every 2, a 1.5 service, constant: nothing random."""
    c, p, d = create("c", dist_params={"value": 2.0}), process("p", value=1.5), dispose("d")
    sections.setdefault("simulation", {"time_limit": 20, "warmup": 0, "replications": 1})
    sections.setdefault("resources", SERVER)
    return layout([c, p, d], chain(c, p, d), **sections)


# ---------------------------------------------------------------- the clock (D232)
def test_a_run_with_a_clock_starts_there_and_reports_the_window_after_it():
    doc = _line(initial_state={"clock": 8.0})
    session = SimSession(doc, seed=0)
    assert session.now == 8.0 and session.warmup == 8.0
    system = api.run_model(doc)
    assert metric_value(system, "Simulation Time") == 12.0
    assert metric_value(system, "Started From State At") == 8.0
    # no arrival clock in the state: a fresh gap, so arrivals at 10 ... 18 leave by 19.5
    assert metric_value(system, "Total Entities Completed") == 5


def test_a_run_without_a_clock_has_no_state_row():
    system = api.run_model(_line())["system"]
    assert "Started From State At" not in set(system["Metric"])


def test_a_capacity_schedule_is_read_at_the_clock():
    doc = _line(initial_state={"clock": 8.0},
                schedules=[{"name": "shift", "kind": "capacity", "points": [[6, 1], [100, 0]]}],
                resources=[{"name": "Server", "capacity": 1, "capacity_schedule": "shift"}])
    system = api.run_model(doc)
    assert metric_value(system, "Total Entities Completed") == 0     # closed since t = 6


@pytest.mark.parametrize("clock", [-1, "soon", True, 20, 25])
def test_a_bad_clock_is_refused(clock):
    assert "E_INITIAL_CLOCK" in problem_codes(_line(initial_state={"clock": clock}))


def test_a_warmup_after_the_clock_still_holds():
    doc = _line(initial_state={"clock": 8.0})
    system = api.run_model(doc, warmup=12.0)
    assert metric_value(system, "Simulation Time") == 8.0


# ---------------------------------------------------------------- the vocabulary
FAILURE = [{"name": "F", "resources": ["Server"],
            "uptime": {"distribution": "Constant", "dist_params": {"value": 100.0}},
            "downtime": {"distribution": "Constant", "dist_params": {"value": 3.0}}}]


def _completed(doc):
    return metric_value(api.run_model(doc), "Total Entities Completed")


def test_an_arrival_clock_resumes_the_arrivals_where_they_were():
    state = {"clock": 8.0, "arrivals": {"c": {"count": 4, "next": 8.0}}}
    assert _completed(_line(initial_state=state)) == 6          # arrivals at 8, 10 ... 18
    c, p, d = create("c", dist_params={"value": 2.0}, max_arrivals=6), process("p", value=1.5), \
        dispose("d")
    capped = layout([c, p, d], chain(c, p, d), resources=SERVER, initial_state=state,
                    simulation={"time_limit": 20, "warmup": 0, "replications": 1})
    assert _completed(capped) == 2                              # four of the six were made


@pytest.mark.parametrize("arrivals", [{"nobody": {"count": 1}}, {"p": {"count": 1}},
                                      {"c": {"count": -1}}, {"c": {"count": 1, "next": 7.0}},
                                      {"c": 3}])
def test_a_bad_arrival_clock_is_refused(arrivals):
    doc = _line(initial_state={"clock": 8.0, "arrivals": arrivals})
    assert "E_INITIAL_ARRIVALS" in problem_codes(doc)


def _down_at(doc, *times):
    session = SimSession(doc, seed=0)
    out = []
    for t in times:
        session.advance(to=t)
        out.append(session.ctx.resource_manager.get("Server").down.level)
    return out


def test_a_failure_clock_fails_the_resource_when_it_was_due():
    state = {"clock": 8.0, "resources": {"Server": {"failures": {"F": {"next": 9.0}}}}}
    assert _down_at(_line(initial_state=state, failures=FAILURE), 8.5, 10.0, 12.5) == [0, 1, 0]


def test_a_failure_in_progress_is_repaired_when_it_was_due():
    state = {"clock": 8.0, "resources": {"Server": {"failures": {"F": {"down_until": 10.0}}}}}
    doc = _line(initial_state=state, failures=FAILURE,
                simulation={"time_limit": 200, "warmup": 0, "replications": 1})
    # down until 10, then up for the failure's own 100, down again for 3
    assert _down_at(doc, 9.0, 11.0, 109.0, 111.0, 114.0) == [1, 0, 0, 1, 0]


@pytest.mark.parametrize("failures", [{"G": {"next": 9.0}}, {"F": {"next": "soon"}}, {"F": 9.0}])
def test_a_bad_failure_clock_is_refused(failures):
    doc = _line(initial_state={"resources": {"Server": {"failures": failures}}}, failures=FAILURE)
    assert "E_INITIAL_FAILURE" in problem_codes(doc)


def test_an_observed_busy_count_must_match_the_entities_in_service():
    busy = {"resources": {"Server": {"busy": 1}}}
    assert "E_INITIAL_BUSY" in problem_codes(_line(initial_state=busy))
    served = {**busy, "entities": [{"block": "p", "count": 2, "in_service": 1}]}
    assert "E_INITIAL_BUSY" not in problem_codes(_line(initial_state=served))
    idle = {"resources": {"Server": {"busy": 0}},
            "entities": [{"block": "p", "count": 1, "in_service": 1}]}
    assert "E_INITIAL_BUSY" in problem_codes(_line(initial_state=idle))


def test_in_service_is_summed_over_the_entries_of_one_resource():
    state = {"entities": [{"block": "p", "count": 1, "in_service": 1},
                          {"block": "p", "count": 1, "in_service": 1}]}
    assert "E_INITIAL_IN_SERVICE" in problem_codes(_line(initial_state=state))


def test_more_waiting_than_the_queue_holds_is_refused():
    c, p, d = create("c"), process("p", queue_capacity=2), dispose("d")
    doc = layout([c, p, d], chain(c, p, d), resources=SERVER,
                 simulation={"time_limit": 20, "warmup": 0, "replications": 1},
                 initial_state={"entities": [{"block": "p", "count": 4, "in_service": 1}]})
    assert "E_INITIAL_QUEUE_CAPACITY" in problem_codes(doc)
    doc["initial_state"]["entities"][0]["count"] = 3
    assert "E_INITIAL_QUEUE_CAPACITY" not in problem_codes(doc)


def test_an_entity_ends_its_service_and_arrives_at_the_absolute_time_given():
    c = create("c", start_time=1000)
    p, d = process("p", value=50.0), dispose("d")
    doc = layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 2}],
                 simulation={"time_limit": 20, "warmup": 0, "replications": 1},
                 initial_state={"clock": 8.0, "entities": [
                     {"block": "p", "count": 1, "in_service": 1, "ends": [9.25]},
                     {"block": "p", "count": 1, "arrives": 12.0}]})
    session = SimSession(doc, seed=0)
    counts = []
    for t in (9.25, 9.26, 12.0, 12.01):
        session.advance(to=t)
        counts.append((len(session.ctx.live_entities), len(session.model.blocks["p"].active_entity)))
    assert counts == [(2, 1), (1, 0), (1, 0), (1, 1)]


@pytest.mark.parametrize("entry", [{"count": 2, "arrives": 9.0}, {"count": 1, "arrives": 7.0},
                                   {"count": 1, "in_service": 1, "arrives": 9.0},
                                   {"count": 1, "in_service": 1, "ends": [7.0]},
                                   {"count": 1, "in_service": 1, "ends": [9.0, 9.5]}])
def test_a_bad_absolute_time_of_an_entry_is_refused(entry):
    doc = _line(initial_state={"clock": 8.0, "entities": [{"block": "p", **entry}]})
    assert "E_INITIAL_REMAINING" in problem_codes(doc)


def test_the_run_marks_what_a_state_needs():
    """D235: the end of a service, the next arrival and the next failure, where each is drawn."""
    session = SimSession(_line(failures=FAILURE), seed=0)
    session.advance(to=8.5)
    (entity,) = session.model.blocks["p"].active_entity
    assert entity._ends == 9.5
    assert session.model.blocks["c"].next_at == 10.0
    assert session.ctx.resource_manager.get("Server").failure_clock == {"F": ("up", 100.0)}


# ---------------------------------------------------------------- a state as data (D231, D237)
def test_a_state_taken_from_a_run_names_what_was_there():
    doc = state.take(_line(failures=FAILURE), 8.5, seed=3, name="line")
    assert doc["format"] == 1 and doc["model"]["name"] == "line"
    assert doc["taken"] == {"source": "run", "at": 8.5, "seed": 3, "engine": nagasim.__version__}
    assert doc["state"] == {
        "clock": 8.5,
        "arrivals": {"c": {"count": 5, "next": 10.0}},
        "resources": {"Server": {"failures": {"F": {"next": 100.0}}}},
        "entities": [{"block": "p", "count": 1, "e_type": "DEFAULT", "age": 0.5,
                      "attributes": {"cost": 0.0}, "in_service": 1, "ends": [9.5],
                      "since": 8.0}]}


def test_the_hash_is_of_the_content_not_of_its_order_or_its_origin():
    model = _line()
    one = state.document(model, {"clock": 8.0, "variables": {"a": 1, "b": 2}}, source="run")
    two = state.document(model, {"variables": {"b": 2, "a": 1}, "clock": 8.0}, source="observed")
    other = state.document(model, {"clock": 8.0, "variables": {"a": 1, "b": 3}}, source="run")
    assert one["hash"] == two["hash"] != other["hash"] and len(one["hash"]) == 64
    moved = _line()
    moved["blocks"][0]["position"] = [5, 5]
    assert state.model_hash(moved) != state.model_hash(model)
    assert state.model_hash({**model, "initial_state": {"clock": 3.0}}) == state.model_hash(model)


def _codes(problems):
    return [p.code for p in problems]


def test_a_run_starts_from_a_state_document():
    model = _line()
    doc = state.take(model, 8.5)
    layout_, problems = state.start(model, doc)
    assert problems == [] and layout_["initial_state"] == doc["state"]
    assert metric_value(api.run_model(layout_), "Started From State At") == 8.5


def test_a_document_that_is_not_one_or_was_edited_or_is_for_another_model_is_refused():
    model = _line()
    doc = state.take(model, 8.5)
    for broken in (None, [], {"format": 2}, {**doc, "state": []}, {**doc, "model": "line"}):
        assert _codes(state.start(model, broken)[1]) == ["E_STATE_FORMAT"]
    edited = {**doc, "state": {**doc["state"], "clock": 9.0}}
    assert _codes(state.start(model, edited)[1]) == ["E_STATE_HASH"]
    other = _line(variables=[{"name": "x", "value": 1}])
    layout_, problems = state.start(other, doc)
    assert layout_ is None and _codes(problems) == ["E_STATE_MODEL"]
    assert doc["model"]["hash"][:12] in problems[0].message


def test_a_state_over_the_bounds_is_refused(monkeypatch):
    model = _line()
    doc = state.take(model, 8.5)
    monkeypatch.setattr(state, "MAX_ENTITIES", 0)
    assert _codes(state.start(model, doc)[1]) == ["E_STATE_SIZE"]


def test_an_observed_state_older_than_the_callers_age_is_refused():
    model = _line()
    doc = state.document(model, {"variables": {}}, source="observed",
                         stamp="2026-10-08T06:00:00+00:00")
    at = 1791439200.0 + 120          # two minutes after the stamp
    assert state.start(model, doc, max_age=300, now=at)[1] == []
    assert _codes(state.start(model, doc, max_age=60, now=at)[1]) == ["E_STATE_STALE"]
    unstamped = state.document(model, {"variables": {}}, source="observed", stamp=None)
    assert _codes(state.start(model, unstamped, max_age=60, now=at)[1]) == ["E_STATE_STALE"]
    assert state.start(model, unstamped)[1] == []           # no age asked: none checked


def _refusal(model, at, **kw):
    with pytest.raises(state.StateError) as caught:
        state.take(model, at, **kw)
    assert caught.value.code == "E_STATE_UNSUPPORTED"
    return caught.value.message


def test_a_job_that_holds_a_resource_between_blocks_is_refused():
    c, d = create("c", dist_params={"value": 2.0}), dispose("d")
    p = process("p", value=0.5, action="Seize Delay")
    w = delay("w", 1.0)
    r = {"name": "r", "description": "r", "type": "RELEASE", "position": [3, 0],
         "params": {"resource_name": "Server"}}
    model = layout([c, p, w, r, d], chain(c, p, w, r, d), resources=SERVER,
                   simulation={"time_limit": 20, "warmup": 0, "replications": 1})
    assert "holds" in _refusal(model, 8.75)


def test_a_state_between_two_states_is_taken_again():
    """A run started from a state gives a state: the marks are set by the warm start too."""
    model = _line(failures=FAILURE)
    first, _ = state.start(model, state.take(model, 8.5))
    again = state.take(first, 9.0)["state"]
    assert again["entities"][0]["ends"] == [9.5] and again["arrivals"]["c"] == {"count": 5, "next": 10.0}


# ---------------------------------------------------------------- the exit check (D233)
# "A warm start from a state reproduces the uninterrupted run": the run started from the
# state taken at t against the uninterrupted run with a warm-up of t, every KPI of
# `experiments.flatten_kpis`. Left out: the row that labels a started run, and the columns
# computed from the per-event log, which depend on the order of events inside one instant
# (the new run's own).
def _bundled(name, **simulation):
    model = json.loads((MODELS / f"{name}.json").read_text(encoding="utf-8"))
    model["simulation"] = {**model.get("simulation", {}), "replications": 1, **simulation}
    return model


def _kpis(results):
    flat = experiments.flatten_kpis(results)
    skipped = {column for columns in LOG_DERIVED_COLUMNS.values() for column in columns}
    return {kpi: value for kpi, value in zip(flat["KPI"], flat["Value"])
            if kpi.rsplit("/", 1)[-1] not in skipped and kpi != "system/Started From State At"}


def _reproduces(model, at):
    doc = state.take(model, at)
    warm, problems = state.start(model, doc)
    assert problems == []
    reference = _kpis(api.run_model(model, warmup=at, replications=1))
    started = _kpis(api.run_model(warm, replications=1))
    assert started.keys() == reference.keys()
    differ = {kpi: (reference[kpi], started[kpi]) for kpi in reference
              if reference[kpi] != pytest.approx(started[kpi], rel=1e-9, abs=1e-9, nan_ok=True)}
    assert differ == {}
    return doc["state"]


@pytest.mark.parametrize("name, at", [("single_server", 250.25), ("machine_with_breakdowns", 200.5),
                                      ("priority_rework", 100.5), ("table_process_times", 100.3)])
def test_discrete_event_a_deterministic_run_is_reproduced_exactly(name, at):
    taken = _reproduces(_bundled(name, deterministic=True), at)
    assert taken["entities"] and taken["arrivals"]      # the state was not an empty system


def test_discrete_event_a_closed_shift_a_queue_and_a_failure_are_reproduced_exactly():
    shift = [{"name": "shift", "kind": "capacity", "points": [[6, 1], [4, 0]], "repeat": True}]
    model = _line(schedules=shift, failures=FAILURE,
                  resources=[{"name": "Server", "capacity": 1, "capacity_schedule": "shift"}],
                  simulation={"time_limit": 300, "warmup": 0, "replications": 1})
    for at in (8.5, 53.25, 101.5):          # shift closed with a queue; open; down by the failure
        taken = _reproduces(model, at)
        assert len(taken["entities"]) > 1


@pytest.mark.parametrize("name, at", [("sd_sir", 10.0), ("sd_inventory_delay3", 20.0),
                                      ("sd_population", 10.0), ("sd_workforce_delay1", 10.0)])
def test_system_dynamics_is_reproduced_exactly(name, at):
    assert _reproduces(_bundled(name), at)["levels"]


@pytest.mark.parametrize("name, at", [("energy_pv_battery", 100.0), ("energy_heat_and_ev", 100.0)])
def test_rate_based_is_reproduced_exactly(name, at):
    assert _reproduces(_bundled(name), at)["levels"]


def test_discrete_event_a_stochastic_run_is_reproduced_in_distribution():
    """M/M/1, 30 seeds: the state of seed s at t = 400, started with ANOTHER seed, against the
    uninterrupted run of seed s over the same window. Stream positions are not state, so the
    two differ run by run; over the seeds the difference of each mean has 0 inside its 99%
    interval, and the runs are not the same numbers.

    Only a guard that a restart with another seed is another run with the same law: it has
    no power against a dropped entity. That is the bookkeeping test further down."""
    model = _bundled("single_server", time_limit=1000)
    kpis = ("entity/DEFAULT/Avg Time", "resource/Server/Idle %", "resource/Server/Queue Length Avg",
            "system/Total Entities Completed")
    differences = {kpi: [] for kpi in kpis}
    for seed in range(30):
        warm, _ = state.start(model, state.take(model, 400.0, seed=seed))
        reference = _kpis(api.run_model(model, warmup=400.0, replications=1, seed=seed))
        started = _kpis(api.run_model(warm, replications=1, seed=1000 + seed))
        for kpi in kpis:
            differences[kpi].append(started[kpi] - reference[kpi])
    for kpi, values in differences.items():
        mean, error = np.mean(values), np.std(values, ddof=1) / np.sqrt(len(values))
        assert abs(mean) <= 2.756 * error, kpi          # Student t, 29 degrees, 99%
        assert error > 0, kpi


@pytest.mark.parametrize("name, at, kind", [("agents_sir_agents", 5.0, "agent populations"),
                                            ("abm_wolf_sheep", 5.0, "agent populations"),
                                            ("warehouse_forklifts", 50.0, "conveyors")])
def test_what_a_state_cannot_carry_is_refused_by_name(name, at, kind):
    assert kind in _refusal(_bundled(name), at)


# ---------------------------------------------------------------- a state from a plant (D236)
def _plant(tmp_path, rows, **sections):
    source = tmp_path / "plant.csv"
    source.write_text("waiting,serving,down_until,shift\n" + rows, encoding="utf-8")
    bind = lambda column, target: {"name": column, "source": "plant", "column": column,  # noqa: E731
                                   "transform": "last", "target": target, "enabled": True}
    return _line(
        variables=[{"name": "shift", "value": 1}],
        data_sources=[{"name": "plant", "kind": "csv", "location": str(source)}],
        data_bindings=[bind("waiting", "initial_state.entities.p.count"),
                       bind("serving", "initial_state.entities.p.in_service"),
                       bind("serving", "initial_state.resources.Server.busy"),
                       bind("shift", "initial_state.variables.shift")], **sections)


def test_an_override_path_writes_into_the_state_and_reading_one_changes_nothing():
    model = _line(variables=[{"name": "shift", "value": 1}])
    out = experiments.apply_overrides(model, [
        {"path": "initial_state.entities.p.count", "value": 3},
        {"path": "initial_state.resources.Server.busy", "value": 1},
        {"path": "initial_state.variables.shift", "value": 2}])
    assert out["initial_state"] == {"entities": [{"block": "p", "count": 3}],
                                    "resources": {"Server": {"busy": 1}}, "variables": {"shift": 2}}
    assert experiments.value_at(model, "initial_state.entities.p.count") == 0
    assert "initial_state" not in model
    for path in ("initial_state.entities.p.attributes", "initial_state.clock", "initial_state"):
        with pytest.raises(ModelValidationError):
            experiments.apply_overrides(model, [{"path": path, "value": 1}])


def test_the_latest_observed_values_become_a_state_a_run_starts_from(tmp_path):
    model = _plant(tmp_path, "0,0,0,1\n3,1,0,2\n")
    doc, problems = state.observe(model, tmp_path)
    assert doc["state"] == {"entities": [{"block": "p", "count": 3, "in_service": 1}],
                            "resources": {"Server": {"busy": 1}}, "variables": {"shift": 2}}
    assert doc["taken"]["source"] == "observed" and doc["taken"]["stamp"]
    assert _codes(problems) == ["W_STATE_NOT_COVERED"] and "arrivals" in problems[0].message
    started, refused = state.start(model, doc)
    assert refused == [] and started["data_bindings"] == []
    session = SimSession(started, seed=0)
    session.advance(to=0.5)
    # the three observed and the arrival of t = 0, one of the three in service
    assert len(session.ctx.live_entities) == 4 and len(session.model.blocks["p"].active_entity) == 1


@pytest.mark.parametrize("rows, code", [("3,2,0,1\n", "E_INITIAL_IN_SERVICE"),     # 2 on 1 unit
                                        ("9,1,0,1\n", "E_INITIAL_QUEUE_CAPACITY")])
def test_an_observation_the_model_cannot_hold_is_refused(tmp_path, rows, code):
    model = _plant(tmp_path, rows)
    model["blocks"][1]["params"]["queue_capacity"] = 4
    doc, _ = state.observe(model, tmp_path)
    started, refused = state.start(model, doc)
    assert started is None and code in _codes(refused)


def test_a_resource_busy_with_no_entity_is_refused(tmp_path):
    model = _plant(tmp_path, "0,1,0,1\n")
    model["data_bindings"] = [b for b in model["data_bindings"] if "in_service" not in b["target"]]
    doc, _ = state.observe(model, tmp_path)
    assert _codes(state.start(model, doc)[1]) == ["E_INITIAL_BUSY"]


def test_a_source_that_cannot_be_read_is_a_problem_and_no_value_is_guessed(tmp_path):
    model = _plant(tmp_path, "3,1,0,2\n")
    model["data_sources"][0]["location"] = str(tmp_path / "missing.csv")
    doc, problems = state.observe(model, tmp_path)
    assert doc["state"] == {} and doc["taken"]["stamp"] is None
    assert {p.code for p in problems} >= {"E_DATA_READ", "W_STATE_NOT_COVERED"} \
        or any(code.startswith("E_DATA") for code in _codes(problems))
    assert _codes(state.start(model, doc, max_age=60)[1]) == ["E_STATE_STALE"]


def test_a_cycle_observes_from_its_own_feed():
    class _Feed:                # what `live.Feed.rows` answers: the latest value per key
        def rows(self, source, settle=False):
            return [{"key": "line/waiting", "value": 2, "source_time": None}], \
                "2026-10-08T06:00:00+00:00"

    model = _line(data_sources=[{"name": "line", "kind": "mqtt", "connection": "broker",
                                 "location": "line/#"}],
                  data_bindings=[{"name": "w", "source": "line", "column": "value",
                                  "transform": "last", "target": "initial_state.entities.p.count",
                                  "enabled": True}])
    doc, problems = state.observe(model, feed=_Feed())
    assert doc["state"] == {"entities": [{"block": "p", "count": 2}]}
    assert doc["taken"]["stamp"] == "2026-10-08T06:00:00+00:00"


# ---------------------------------------------------------------- the commands
def test_the_commands_take_check_and_start_from_a_state(tmp_path, capsys):
    model, other, out = tmp_path / "line.json", tmp_path / "other.json", tmp_path / "state.json"
    model.write_text(json.dumps(_line()), encoding="utf-8")
    other.write_text(json.dumps(_line(variables=[{"name": "x", "value": 1}])), encoding="utf-8")
    assert state.main(["take", str(model), "--at", "8.5", "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["model"]["name"] == "line" and doc["state"]["clock"] == 8.5
    assert doc["hash"] in capsys.readouterr().out

    assert state.main(["check", str(model), str(out)]) == 0
    assert state.main(["check", str(other), str(out)]) == 1
    assert "E_STATE_MODEL" in capsys.readouterr().out

    assert api.main([str(model), "--state", str(out)]) == 0
    assert "Started From State At" in capsys.readouterr().out
    assert api.main([str(other), "--state", str(out)]) == 1
    assert "E_STATE_MODEL" in capsys.readouterr().out

    out.write_text("{not json", encoding="utf-8")
    assert state.main(["check", str(model), str(out)]) == 1
    assert "E_STATE_FORMAT" in capsys.readouterr().out


def test_the_take_command_says_what_it_cannot_carry(tmp_path, capsys):
    assert state.main(["take", str(MODELS / "warehouse_forklifts.json"), "--at", "50"]) == 1
    assert "E_STATE_UNSUPPORTED" in capsys.readouterr().out


def test_the_observe_command_writes_the_state_and_its_problems(tmp_path, capsys):
    model, out = tmp_path / "line.json", tmp_path / "state.json"
    model.write_text(json.dumps(_plant(tmp_path, "3,1,0,2\n")), encoding="utf-8")
    assert state.main(["observe", str(model), "--out", str(out)]) == 0
    assert "W_STATE_NOT_COVERED" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["state"]["entities"][0]["count"] == 3
    assert state.main(["check", str(model), str(out), "--max-age", "3600"]) == 0


def test_the_engine_command_table_has_the_state_command():
    from nagasim import cli
    assert cli.COMMANDS["state"][:2] == ("state", "main")


# ---------------------------------------------------------------- the review round
def _backlog(discipline, per_arrival=1, **resource):
    """Arrivals every 1 (or two at once), a 1.5 service: the queue grows."""
    c = create("c", dist_params={"value": 1.0}, entities_per_arrival=per_arrival)
    p, d = process("p", value=1.5), dispose("d")
    return layout([c, p, d], chain(c, p, d),
                  resources=[{"name": "Server", "capacity": 1, "discipline": discipline, **resource}],
                  simulation={"time_limit": 40, "warmup": 0, "replications": 1})


@pytest.mark.parametrize("model", [
    _backlog("LIFO"), _backlog("LIFO", per_arrival=2), _backlog("FIFO", per_arrival=2),
    _backlog("LowestValueFirst", per_arrival=2, ranking_expr="1")],
    ids=["lifo", "lifo-ties", "fifo-ties", "ranked-ties"])
def test_a_backlog_is_served_in_the_order_of_its_discipline(model):
    taken = _reproduces(model, 10.25)
    assert len(taken["entities"]) > 3


def test_a_state_taken_at_the_clock_of_a_started_run_is_the_same_state():
    model = _line(failures=FAILURE)
    doc = state.take(model, 8.5)
    first, _ = state.start(model, doc)
    again = state.take(first, 8.5)
    assert again["state"] == doc["state"] and again["hash"] == doc["hash"]


def _tied():
    shift = [{"name": "shift", "kind": "capacity", "points": [[6, 1], [4, 0]], "repeat": True}]
    failure = [{**FAILURE[0], "uptime": {"distribution": "Constant", "dist_params": {"value": 10.0}}}]
    return _line(schedules=shift, failures=failure,
                 resources=[{"name": "Server", "capacity": 1, "capacity_schedule": "shift"}],
                 simulation={"time_limit": 110, "warmup": 0, "replications": 1})


def test_a_state_is_not_taken_at_an_instant_with_an_event_due():
    assert "an instant with an event due" in _refusal(_tied(), 10.0)
    _reproduces(_tied(), 10.25)


def test_a_state_is_not_taken_at_or_after_the_end_of_the_run():
    assert "the end of the run" in _refusal(_line(), 20.0)
    assert "the end of the run" in _refusal(_line(), 25.0)


@pytest.mark.parametrize("name, at, simulation", [
    ("single_server", 400.0, {}), ("single_server", 733.3, {}),
    ("machine_with_breakdowns", 200.5, {}), ("priority_rework", 150.25, {}),
    ("table_process_times", 100.3, {"deterministic": True}),
    ("two_teller_set", 101.5, {})])
def test_no_entity_is_lost_or_invented_between_a_run_its_state_and_the_started_run(
        name, at, simulation):
    """Exact bookkeeping, stochastic runs included: a state with one entity dropped fails."""
    model = _bundled(name, **simulation)
    for seed in range(3, 40):           # the first seed with work in progress at *at*
        plain = SimSession(model, seed=seed)
        plain.advance(to=at)
        if len(plain.ctx.live_entities):
            break
    doc = state.take(model, at, seed=seed)
    injected = sum(entry["count"] for entry in doc["state"].get("entities", []))
    assert injected == len(plain.ctx.live_entities) > 0

    warm, _ = state.start(model, doc)
    started = SimSession(warm, seed=11)
    assert len(started.ctx.live_entities) == injected
    results = started.finish()
    blocks = started.model.blocks.values()
    created = sum(getattr(b, "created_entities", 0) for b in blocks)
    disposed = sum(getattr(b, "disposed_entities", 0) for b in blocks)
    assert injected + created == disposed + len(started.ctx.live_entities)
    log = results["entity_log"]
    left = sum(1 for e in started.ctx.live_entities if getattr(e, "initial", False))
    assert int(log["Initial"].sum()) + left == injected


def test_the_model_name_in_a_refusal_is_text_of_bounded_length():
    model = _line()
    doc = state.take(model, 8.5)
    doc["model"] = {"name": ["x" * 500], "hash": "0" * 64}
    doc["hash"] = state.document(model, {}, source="run")["hash"]      # any: the model fails first
    (found,) = state.check(_line(), {**doc, "hash": state._sha(state._canonical(
        [doc["model"]["hash"], doc["state"]]))})
    assert found.code == "E_STATE_MODEL" and len(found.message) < 250


def test_the_bound_counts_entities_not_entries(monkeypatch):
    model = _line()
    big = state.document(model, {"entities": [{"block": "p", "count": 10 ** 9}]}, source="observed")
    assert _codes(state.check(model, big)) == ["E_STATE_SIZE"]


# ---------------------------------------------------------------- the second round (D260-D264)
RUN = {"time_limit": 60, "warmup": 0, "replications": 1}


def _block(name, kind, **params):
    return {"name": name, "description": name, "type": kind, "position": [1, 0], "params": params}


def _carried(model, at):
    """The exit check with its teeth: exact against the uninterrupted run, and the state
    taken again at the clock of the started run is the same document."""
    taken = _reproduces(model, at)
    doc = state.take(model, at)
    started, _ = state.start(model, doc)
    assert state.take(started, at)["hash"] == doc["hash"]
    return taken


def _held_line():
    c, h, d = create("c"), _block("h", "HOLD", mode="Signal", signal_name="go"), dispose("d")
    c2 = create("c2", dist_params={"value": 7.0}, start_time=3.5)
    s, d2 = _block("s", "SIGNAL", signal_name="go"), dispose("d2")
    return layout([c, h, d, c2, s, d2], chain(c, h, d) + chain(c2, s, d2), simulation=RUN)


def _batch_line(**params):
    c, b, d = create("c"), _block("b", "BATCH", batch_size=4, **params), dispose("d")
    return layout([c, b, d], chain(c, b, d), simulation=RUN)


def _match_line(stores={"A": 0, "B": 1}):
    a, b = create("a", e_type="A"), create("b", e_type="B", dist_params={"value": 3.0})
    m, d = _block("m", "MATCH", number_to_match=2, type_store_map=stores), dispose("d")
    return layout([a, b, m, d], chain(a, m, d) + chain(b, m), simulation=RUN)


@pytest.mark.parametrize("model, at, waiting", [(_held_line(), 9.25, 6), (_batch_line(), 9.5, 2),
                                                (_match_line(), 10.5, 7)],
                         ids=["hold", "batch", "match"])
def test_entities_waiting_in_a_store_are_carried_in_their_order(model, at, waiting):
    taken = _carried(model, at)
    assert len(taken["entities"]) == waiting and all("since" in e for e in taken["entities"])


@pytest.mark.parametrize("name, at", [("hold_on_store", 40.5), ("hold_on_store", 71.25)])
def test_the_bundled_hold_models_are_reproduced_exactly(name, at):
    assert _carried(_bundled(name, deterministic=True), at)["entities"]


def test_a_match_that_maps_no_types_is_refused():
    assert "type_store_map" in _refusal(_match_line(stores={}), 10.5)


def test_a_batch_whose_maximum_wait_is_running_is_refused():
    assert "maximum wait" in _refusal(_batch_line(max_wait="2.5"), 9.5)


def test_a_hold_on_a_polled_condition_is_refused():
    c, d = create("c"), dispose("d")
    h = _block("h", "HOLD", mode="Condition", conditions=["TNOW > 30"])
    model = layout([c, h, d], chain(c, h, d), simulation=RUN)
    assert "polled condition" in _refusal(model, 9.5)


@pytest.mark.parametrize("rule", ["Cyclical", "PreferredOrder", "SmallestNumberBusy"])
@pytest.mark.parametrize("at", [101.5, 102.5])
def test_a_resource_set_keeps_its_members_jobs_and_its_turn(rule, at):
    model = _bundled("two_teller_set", deterministic=True)
    model["resource_sets"][0]["rule"] = rule
    taken = _carried(model, at)
    assert all("member" in e for e in taken["entities"] if e.get("in_service"))
    assert ("resource_sets" in taken) == (rule == "Cyclical")


@pytest.mark.parametrize("state_, path", [
    ({"resource_sets": {"Nobody": {"cursor": 0}}}, "resource_sets"),
    ({"resource_sets": {"Tellers": {"cursor": 7}}}, "resource_sets"),
    ({"entities": [{"block": "service", "count": 1, "in_service": 1, "member": "Nobody"}]}, "member")])
def test_a_bad_set_member_or_cursor_is_refused(state_, path):
    model = _bundled("two_teller_set")
    block = next(b["name"] for b in model["blocks"] if b["type"] == "PROCESS")
    for entry in state_.get("entities", []):
        entry["block"] = block
    assert "E_INITIAL_SET" in problem_codes({**model, "initial_state": state_})


def _impatient():
    c = create("c")
    p = process("p", value=1.6, renege_after="4.3")
    d, lost = dispose("d"), dispose("lost")
    return layout([c, p, d, lost], chain(c, p, d) + chain(p, lost), resources=SERVER,
                  simulation=RUN)


def test_a_queue_with_patience_keeps_each_deadline():
    taken = _carried(_impatient(), 10.25)
    waiting = [e for e in taken["entities"] if not e.get("in_service")]
    assert len(waiting) >= 3 and all(e["deadline"] == e["since"] + 4.3 for e in waiting)


def test_a_state_is_not_taken_when_a_patience_runs_out():
    assert "the patience of" in _refusal(_impatient(), 12.3)


def test_a_preempted_job_is_refused():
    model = _bundled("impatient_customers", deterministic=True)
    assert "a job preempted" in _refusal(model, 200.7)


@pytest.mark.parametrize("at", [50.3, 333.3])
def test_the_bundled_reneging_model_is_reproduced_exactly(at):
    assert _carried(_bundled("impatient_customers", deterministic=True), at)["entities"]


@pytest.mark.parametrize("entry", [{"count": 1, "deadline": 3.0}, {"count": 1, "deadline": "soon"}])
def test_a_bad_deadline_is_refused(entry):
    doc = _impatient()
    doc["initial_state"] = {"clock": 8.0, "entities": [{"block": "p", **entry}]}
    assert "E_INITIAL_REMAINING" in problem_codes(doc)
    plain = _line(initial_state={"entities": [{"block": "p", "count": 1, "deadline": 9.0}]})
    assert "E_INITIAL_REMAINING" in problem_codes(plain)        # no patience at this block


def _two_families():
    a, b = create("a", e_type="A", dist_params={"value": 2.0}), \
        create("b", e_type="B", dist_params={"value": 3.0}, start_time=0.5)
    p, d = process("p", value=0.7, setup="0.4"), dispose("d")
    return layout([a, b, p, d], chain(a, p, d) + chain(b, p), resources=SERVER, simulation=RUN)


@pytest.mark.parametrize("at", [10.55, 13.25, 14.45, 20.6, 25.15])
def test_a_station_keeps_what_it_is_set_up_for(at):
    taken = _carried(_two_families(), at)
    assert taken["blocks"]["p"]["setup"] in ("A", "B")


def test_a_changeover_under_way_is_refused():
    model = _two_families()
    refused = []
    for at in (x / 20 for x in range(200, 260)):
        try:
            state.take(model, at)
        except state.StateError as exc:
            refused.append(exc.message)
    assert any("changing over" in message for message in refused)


def test_a_bad_setup_is_refused():
    model = _two_families()
    for blocks in ({"nobody": {"setup": "A"}}, {"p": {"setup": ["A"]}}, {"d": {"setup": "A"}}):
        assert "E_INITIAL_BLOCK" in problem_codes({**model, "initial_state": {"blocks": blocks}})


def test_a_bundled_library_model_with_stores_is_reproduced_exactly():
    _carried(_bundled("supply_chain_library", deterministic=True), 50.3)


@pytest.mark.parametrize("name, at, kind", [
    ("automation_floor", 50.3, "a mobile cobot that tends"), ("warehouse_library", 20.3, "conveyors"),
    ("automation_cell", 50.3, "mid-cycle"),
    ("mfg_line_library", 50.3, "holds a resource between blocks"),
    ("clinic_library", 60.3, "holds a resource between blocks"),
    ("biogas_plant_library", 50.3, "conveyor, a blended level")])
def test_what_the_second_round_still_refuses_is_named(name, at, kind):
    assert kind in _refusal(_bundled(name), at)


# ---------------------------------------------------------------- the second review round
def _state_line(*, link_delay=None, failure=None, capacity=1, sets=None, resource="Server",
                service=1.4, **resource_fields):
    """Two sources into one station, for the state defects of merged 7.10 (#349-#351)."""
    a = create("a", e_type="A", dist_params={"value": 2.0}, start_time=0.25)
    b = create("b", e_type="B", dist_params={"value": 2.0}, start_time=1.25)
    p, d = process("p", resource=resource, value=service), dispose("d")
    links = chain(a, p, d) + chain(b, p)
    if link_delay:
        links[0]["delay"] = {"distribution": "Constant", "dist_params": {"value": link_delay}}
    names = sets[0]["members"] if sets else [resource]
    failures = [{"name": "F", "resources": [names[0]],
                 "uptime": {"distribution": "Constant", "dist_params": {"value": 10.0}},
                 "downtime": {"distribution": "Constant", "dist_params": {"value": 3.0}}}] \
        if failure else []
    return layout([a, b, p, d], links, failures=failures, resource_sets=sets or [],
                  resources=[{"name": n, "capacity": capacity, **resource_fields} for n in names],
                  simulation={"time_limit": 60, "warmup": 0, "replications": 1})

SET = [{"name": "S", "members": ["R0", "R1"], "rule": "Cyclical"}]
TIMES = ("clock", "since", "deadline", "arrives", "next", "down_until", "age")


def _no_int_time(value, key=None):
    if isinstance(value, dict):
        return all(_no_int_time(v, k) for k, v in value.items())
    if isinstance(value, list):
        return all(_no_int_time(v, key) for v in value)
    return not (key in TIMES + ("ends",) and isinstance(value, int))


@pytest.mark.parametrize("fields", [{}, {"discipline": "LIFO"},
                                    {"discipline": "LowestValueFirst", "ranking_expr": "1"}],
                         ids=["fifo", "lifo", "ranked"])
@pytest.mark.parametrize("at", [8.5, 13.5])
def test_a_queue_fed_through_a_timed_link_keeps_its_order_and_its_hash(fields, at):
    taken = _carried(_state_line(link_delay=4.0, **fields), at)
    waits = [e["since"] for e in taken["entities"] if "since" in e and not e.get("in_service")]
    assert waits == sorted(waits) and len(waits) >= 2
    assert any("arrives" in e for e in taken["entities"]) and _no_int_time(taken)


def _store_through_a_link(kind, **params):
    a = create("a", e_type="A", dist_params={"value": 2.0}, start_time=0.25)
    b = create("b", e_type="B", dist_params={"value": 2.0}, start_time=1.25)
    s, d = _block("s", kind, **params), dispose("d")
    links = chain(a, s, d) + chain(b, s)
    links[0]["delay"] = {"distribution": "Constant", "dist_params": {"value": 4.0}}
    extra, more = [], []
    if kind == "HOLD":
        c2 = create("c2", dist_params={"value": 7.0}, start_time=3.5)
        g, d2 = _block("g", "SIGNAL", signal_name="go"), dispose("d2")
        extra, more = [c2, g, d2], chain(c2, g, d2)
    return layout([a, b, s, d] + extra, links + more, simulation=RUN)


@pytest.mark.parametrize("kind, params", [
    ("HOLD", {"mode": "Signal", "signal_name": "go"}), ("BATCH", {"batch_size": 5}),
    ("MATCH", {"number_to_match": 2, "type_store_map": {"A": 0, "B": 1}})])
@pytest.mark.parametrize("at", [8.5, 13.5])
def test_a_store_fed_through_a_timed_link_keeps_its_order(kind, params, at):
    assert _no_int_time(_carried(_store_through_a_link(kind, **params), at))


@pytest.mark.parametrize("model, at", [
    (_state_line(failure=True, capacity=2, service=2.9), 12.1),
    (_state_line(failure=True, capacity=2, service=1.9), 10.6),
    (_state_line(failure=True, sets=SET, resource="S", service=4.3), 11.5),
    (_state_line(failure=True, sets=SET, resource="S", service=4.3), 12.6),
    (_state_line(service=2.9, capacity=2, capacity_schedule="shift",
                 ), 6.5)], ids=["plain-a", "plain-b", "set-a", "set-b", "shift"])
def test_a_waiting_entity_never_takes_a_unit_that_is_down_or_off_shift(model, at):
    if model["resources"][0].get("capacity_schedule"):
        model["schedules"] = [{"name": "shift", "kind": "capacity", "points": [[6, 2], [4, 0]],
                               "repeat": True}]
    taken = _carried(model, at)
    assert any(e.get("in_service") for e in taken["entities"])
    assert any(not e.get("in_service") for e in taken["entities"])


@pytest.mark.parametrize("rule", ["Cyclical", "PreferredOrder", "LargestRemainingCapacity"])
@pytest.mark.parametrize("at", [10.5, 20.6])
def test_an_overloaded_set_is_reproduced_exactly(rule, at):
    sets = [{**SET[0], "rule": rule}]
    assert len(_carried(_state_line(sets=sets, resource="S", service=4.3), at)["entities"]) > 4


def test_an_entity_that_entered_at_zero_has_a_float_time():
    c, d = create("c"), dispose("d")
    model = layout([c, _block("b", "BATCH", batch_size=5), d], [], simulation=RUN)
    model["links"] = chain(*model["blocks"])
    taken = state.take(model, 0.5)["state"]
    assert taken["entities"][0]["since"] == 0 and _no_int_time(taken)
    _carried(model, 0.5)


def test_a_deadline_already_met_does_not_refuse_a_take():
    model = _impatient()        # an entity served at 0 keeps the mark 4.3 it never needed
    doc = state.take(model, 4.3 + 1e-9)
    assert doc["state"]["clock"] > 4.3
    refused = 0
    for at in (x / 100 for x in range(430, 1500, 7)):
        try:
            state.take(model, at)
        except state.StateError as exc:
            refused += "the patience of" in exc.message and "an event due" in exc.message
    assert refused <= 3         # only the deadlines of entities still waiting


def test_more_jobs_than_a_set_member_holds_is_refused():
    model = _state_line(sets=SET, resource="S")
    model["initial_state"] = {"entities": [{"block": "p", "count": 2, "in_service": 2,
                                            "member": "R0"}]}
    assert "E_INITIAL_IN_SERVICE" in problem_codes(model)


def test_a_setup_that_is_not_text_or_a_number_names_the_station():
    model = _two_families()
    tap = state.Tap()
    session = SimSession(model, seed=0, subscribers=(tap,))
    session.advance(to=10.55)
    session.model.blocks["p"]._previous_key = True
    with pytest.raises(state.StateError) as caught:
        state._read(session, tap)
    assert "the setup of station 'p'" in caught.value.message


def _fast_arrivals(*, sets=None, capacity=1, failure=True, patience=None):
    """The review's probes: arrivals every 0.6, a 1.6 service, a failure up 10 and down 3."""
    c, d, lost = create("c", dist_params={"value": 0.6}), dispose("d"), dispose("lost")
    extra = {"renege_after": patience} if patience else {}
    p = process("p", resource="S" if sets else "R0", value=1.6, **extra)
    names = sets[0]["members"] if sets else ["R0"]
    failures = [{"name": "F", "resources": ["R0"],
                 "uptime": {"distribution": "Constant", "dist_params": {"value": 10.0}},
                 "downtime": {"distribution": "Constant", "dist_params": {"value": 3.0}}}]
    return layout([c, p, d, lost], chain(c, p, d) + (chain(p, lost) if patience else []),
                  resources=[{"name": n, "capacity": capacity} for n in names],
                  resource_sets=sets or [], failures=failures if failure else [], simulation=RUN)


@pytest.mark.parametrize("model, at", [
    (_fast_arrivals(sets=SET), 11.5), (_fast_arrivals(sets=SET, patience="9.3"), 10.25),
    (_fast_arrivals(capacity=2), 11.1), (_fast_arrivals(sets=SET, failure=False), 10.25),
    (_fast_arrivals(sets=[{**SET[0], "rule": "Random"}], failure=False), 10.25)],
    ids=["set-failure", "set-failure-patience", "plain-two-units", "set-open-waits", "random-set"])
def test_the_probes_the_review_found_wrong_are_exact(model, at):
    _carried(model, at)


# ---------------------------------------------------------------- the third review round
def _tie(kind="PROCESS", **fields):
    """X at 3 keeps the station busy; A set out at 0 over a 4.0 link and C created at 4
    reach it in ONE instant: the order of that instant is the queue's own, not the Tap's."""
    x = create("x", e_type="X", start_time=3, max_arrivals=1)
    a = create("a", e_type="A", max_arrivals=1)
    c = create("c", e_type="C", start_time=4, max_arrivals=1)
    late = create("late", e_type="L", start_time=20, dist_params={"value": 5.0})
    p, d = process("p", value=6.0), dispose("d")
    resources = [{"name": "Server", "capacity": 1, **fields}]
    if kind == "PROCESS":
        blocks, links = [x, a, c, late, p, d], chain(x, p, d) + chain(a, p) + chain(c, p) \
            + chain(late, p)
    else:
        params = {"HOLD": {"mode": "Signal", "signal_name": "go"}, "BATCH": {"batch_size": 4}}[kind]
        s = _block("s", kind, **params)
        g, d2 = _block("g", "SIGNAL", signal_name="go"), dispose("d2")
        go = create("go", e_type="G", start_time=9.25, dist_params={"value": 50.0})
        blocks = [x, a, c, late, s, p, d, go, g, d2]
        links = chain(x, s, p, d) + chain(a, s) + chain(c, s) + chain(late, s) + chain(go, g, d2)
    links[[(l["source"], l["target"]) for l in links].index(("a", blocks[4]["name"]))]["delay"] = {
        "distribution": "Constant", "dist_params": {"value": 4.0}}
    return layout(blocks, links, resources=resources, simulation=RUN)


@pytest.mark.parametrize("model", [
    _tie(), _tie(discipline="LIFO"), _tie(discipline="LowestValueFirst", ranking_expr="1"),
    _tie("HOLD"), _tie("BATCH")], ids=["fifo", "lifo", "ranked", "hold", "batch"])
def test_two_entities_of_one_past_instant_keep_the_order_the_run_gave_them(model):
    taken = _carried(model, 8.5)
    tied = [e["e_type"] for e in taken["entities"] if e.get("since") == 4.0]
    assert sorted(tied) == ["A", "C"]


def _ranked(expr, **fields):
    """B's rank is a variable a side chain flips: what a request was made with is frozen."""
    model = _state_line(discipline="LowestValueFirst", ranking_expr=expr, **fields)
    flip = create("flip", e_type="F", dist_params={"value": 5.0}, start_time=2.5)
    assign = _block("set", "ASSIGN", assignments=[{"target": "rank_b", "expr": "10 - rank_b"}])
    end = dispose("end")
    model["blocks"] += [flip, assign, end]
    model["links"] += chain(flip, assign, end)
    model["variables"] = [{"name": "rank_b", "value": 1}]
    return model


def test_a_ranked_queue_that_reads_beyond_the_entity_is_refused_unless_it_reranks():
    reads_a_variable = 'IF(entity.e_type == "B", rank_b, 5)'
    assert "a ranked queue" in _refusal(_ranked(reads_a_variable), 17.3)
    _carried(_ranked(reads_a_variable, rerank=True), 17.3)
    _carried(_ranked('IF(entity.e_type == "B", 1, 5)'), 17.3)


@pytest.mark.parametrize("at", [17.7, 20.75])
def test_the_set_probe_is_exact_once_a_freed_unit_is_owed_to_its_queue(at):
    _carried(_fast_arrivals(sets=SET), at)
    _carried(_fast_arrivals(sets=[{**SET[0], "rule": "PreferredOrder"}]), at)


@pytest.mark.parametrize("at", [13.5, 17.3])
def test_a_child_of_a_split_batch_is_carried(at):
    c, b = create("c"), _block("b", "BATCH", batch_size=3)
    s, p, d = _block("s", "SEPARATE"), process("p", value=1.7), dispose("d")
    model = layout([c, b, s, p, d], chain(c, b, s, p, d), resources=SERVER, simulation=RUN)
    taken = _carried(model, at)
    assert any("batch_info" in e["attributes"] for e in taken["entities"])
