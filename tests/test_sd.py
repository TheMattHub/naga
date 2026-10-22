"""System dynamics (PHASE5 SD-1/SD-2, ARCHITECTURE D89-D92): the step grid,
Euler/RK4, dynamic-flow detection, `min: null`, biflow, the trace stride; then
auxiliaries, delays and their expansion, STEP/PULSE/RAMP/IF, initial
expressions, the authoring classes and the agent tools. The closed-form
oracles live in `tests/test_sd_oracles.py` and `tests/test_sd_constructs_oracle.py`;
this file checks the mechanics around them.
"""
import math
import time

import pytest

from nagasim.api import run_model
from nagasim.core import levels as levels_mod
from nagasim.core import sd
from nagasim.core.expressions import ExpressionError
from nagasim.core.schema import CURRENT_VERSION, migrate, validate_model_structured
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

from conftest import problem_codes, run_env


def _model(rate='0.1*LEVEL("L")', method="euler", dt=0.25, T=10.0, **extra):
    layout = {"blocks": [], "links": [],
              "levels": [{"name": "L", "initial": 100.0}],
              "flows": [{"name": "out", "rate": rate, "from": "L"}],
              "simulation": {"time_limit": T, "sd": {"dt": dt, "method": method}}}
    layout.update(extra)
    return layout


def _run(layout, run_len):
    env, _ = run_env(layout, run_len, seed=0)
    return env


def _codes(layout):
    return problem_codes(layout, severity=None)


def _final(layout, level="L"):
    frame = run_model(layout)["level_summary"]
    return float(frame.loc[frame["Level"] == level, "Final"].iloc[0])


# ================================================================ the grid
def test_grid_points_are_integer_multiples_of_dt_with_no_drift():
    """0.1 is not a binary fraction: accumulated steps drift, `k * dt` does not."""
    layout = _model(dt=0.1, T=100.0)
    env = initialize_sim_env(seed=0)
    prepare_model(env, layout)
    system = env.ctx.level_system
    seen = []
    real = system._recompute
    system._recompute = lambda: (seen.append((env.now, system._grid_wake)), real())[1]
    system.start(100.0)
    step_until(env, 100.0)
    grid_times = [t for t, grid in seen if grid]
    assert len(grid_times) == 1000                 # the horizon itself is a grid point
    assert max(abs(t - k * 0.1) for k, t in enumerate(grid_times, 1)) < 1e-12


def test_only_dynamic_rates_are_evaluated_on_a_grid_wake_up():
    """A constant rate cannot change between events; a LEVEL()/TNOW rate can."""
    layout = _model(dt=1.0, T=10.0)
    layout["flows"] += [{"name": "const", "rate": "2", "to": "L"},
                        {"name": "clock", "rate": "TNOW * 0", "to": "L"}]
    env = initialize_sim_env(seed=0)
    prepare_model(env, layout)
    system = env.ctx.level_system
    counts = {}
    real = system._evaluate

    def counting(todo):
        for flow, *_ in todo:
            counts[flow.name] = counts.get(flow.name, 0) + 1
        real(todo)
    system._evaluate = counting
    system.start(10.0)
    step_until(env, 10.0)
    assert counts["const"] == 1                     # t=0 only
    assert counts["out"] == counts["clock"] == 11   # t=0 and grid points 1..10


def test_no_sd_key_or_no_dynamic_rate_means_no_grid():
    plain = _model()
    del plain["simulation"]["sd"]
    env = _run(plain, 10.0)
    assert env.ctx.level_system.dt is None
    static = _model(rate="3")
    env = _run(static, 10.0)
    assert env.ctx.level_system.dt is None           # nothing dynamic to integrate
    assert env.ctx.level_system.levels["L"].level == pytest.approx(70.0)


def test_the_sd_time_unit_scales_dt_into_the_base_unit():
    layout = _model(simulation={"time_limit": 120.0, "time_unit": "minutes",
                                "sd": {"dt": 0.5, "time_unit": "hours"}})
    env = _run(layout, 120.0)
    assert env.ctx.level_system.dt == 30.0


def test_rk4_stage_state_is_restored_when_a_stage_raises():
    """The rate is fine at t=0 and fails at the first stage's clock (t=0.125):
    the level and the clock offset must be back where they were."""
    layout = _model(rate='LEVEL("L") if TNOW < 0.1 else TABLE("no" + "pe", 1)', method="rk4",
                    tables=[])
    env = initialize_sim_env(seed=0)
    prepare_model(env, layout)
    system = env.ctx.level_system
    with pytest.raises(ExpressionError):
        system._recompute()
    assert env.ctx.stage_offset == 0.0
    assert env.ctx.now == 0.0
    assert system.levels["L"].level == 100.0


# ============================================================ bounds and biflow
def test_a_biflow_runs_backwards_between_unbounded_stocks():
    """Diffusion A <-> B: the flow A->B goes negative while B is fuller, and
    Euler conserves the total exactly."""
    layout = {"blocks": [], "links": [],
              "levels": [{"name": "A", "initial": 0.0, "min": None},
                         {"name": "B", "initial": 10.0, "min": None}],
              "flows": [{"name": "mix", "rate": '0.5*(LEVEL("A") - LEVEL("B"))',
                         "from": "A", "to": "B", "biflow": True}],
              "simulation": {"time_limit": 20.0, "sd": {"dt": 0.1}}}
    assert _codes(layout) == set()
    frame = run_model(layout)["level_summary"].set_index("Level")["Final"]
    assert frame["A"] == pytest.approx(5.0, abs=1e-6)
    assert frame["A"] + frame["B"] == pytest.approx(10.0, rel=1e-12)


def test_without_biflow_a_negative_rate_is_still_clamped():
    layout = _model(rate="-5")
    del layout["simulation"]["sd"]
    assert _final(layout) == 100.0


# ================================================================ validation
# dt <= 0 is test_sd_oracles.py::test_bad_sd_settings_is_a_problem.
@pytest.mark.parametrize("sd_value", [5, {"dt": "1"}, {"dt": 1, "method": "heun"},
                                      {"dt": 1, "time_unit": "fortnights"}, {"dt": 1, "step": 2}])
def test_bad_sd_settings_are_e_sd_settings(sd_value):
    layout = _model()
    layout["simulation"]["sd"] = sd_value
    assert "E_SD_SETTINGS" in _codes(layout)


def test_rk4_warns_on_bus_reads_and_ignores_static_random_rates():
    # A random dynamic rate is test_sd_oracles.py::test_rk4_with_a_random_sampler_...
    layout = _model(rate='LEVEL("L") * 0 + FLOW("out")', method="rk4")
    assert "W_SD_RK4_BUS" in _codes(layout)
    layout["simulation"]["sd"]["method"] = "euler"
    assert not {"E_SD_RK4_RANDOM", "W_SD_RK4_BUS"} & _codes(layout)
    layout["flows"][0]["rate"] = "RAND()"                  # static: never re-drawn in a stage
    layout["simulation"]["sd"]["method"] = "rk4"
    assert "E_SD_RK4_RANDOM" not in _codes(layout)


def test_biflow_needs_unbounded_ends():
    layout = _model(rate="1")
    layout["flows"][0]["biflow"] = True                    # L has the default min 0
    assert "E_FLOW_BIFLOW" in _codes(layout)
    layout["levels"][0]["min"] = None
    assert "E_FLOW_BIFLOW" not in _codes(layout)
    layout["levels"][0]["capacity"] = 50.0
    assert "E_FLOW_BIFLOW" in _codes(layout)
    layout["flows"][0]["biflow"] = "yes"
    assert "E_FLOW_BIFLOW" in _codes(layout)


def test_an_empty_layout_is_not_half_drawn_but_blocks_without_create_are():
    # A blockless SD model is test_sd_oracles.py::test_a_blockless_sd_model_validates_...
    # #185: a genuinely empty layout (nothing at all, not even a level) is not
    # "half-drawn" either -- ProblemsPanel shows "Empty model" instead.
    assert not {"E_NO_CREATE", "E_NO_DISPOSE"} & _codes({"blocks": [], "links": []})
    # A model with SOME blocks but no CREATE/DISPOSE is still half-drawn.
    assert {"E_NO_CREATE", "E_NO_DISPOSE"} <= _codes(
        {"blocks": [{"name": "b", "type": "DELAY", "position": [0, 0], "params": {}}],
         "links": []})


def test_schema_v21_migrates_a_v20_file_unchanged():
    assert CURRENT_VERSION >= 21
    v20 = {"schema_version": 20, "blocks": [], "levels": [{"name": "L", "min": 0.0}],
           "simulation": {"time_limit": 5}}
    migrated = migrate(v20)
    assert migrated["schema_version"] == CURRENT_VERSION
    assert migrated["levels"] == [{**v20["levels"][0], "rate_time_unit": "minutes"}]   # v28
    assert "sd" not in migrated["simulation"]


# ================================================================ trace stride
def test_grid_corners_are_thinned_to_the_trace_budget(monkeypatch):
    """100 grid steps, one level, a budget of 20: stride 5, so about 20 corners
    rather than 100 -- plus the start and the slope-sign flips."""
    monkeypatch.setattr(levels_mod, "TRACE_BUDGET", 20)
    env = initialize_sim_env(seed=0)
    prepare_model(env, _model(dt=0.1, T=10.0))
    corners = []
    env.ctx.bus.subscribe(lambda name, **p: corners.append(p["t"]) if name == "level_changed"
                          else None)
    env.ctx.level_system.start(10.0)
    step_until(env, 10.0)
    assert env.ctx.level_system._stride == 5
    assert 18 <= len(corners) <= 22


# ============================================================ authoring classes
def test_sd_records_round_trip_and_write_only_non_default_keys():
    stock = sd.Stock("P", 10, min=None, thresholds=[{"value": 5}])
    flow = sd.Flow("net", 'LEVEL("P")', source="P", biflow=True)
    settings = sd.SDSettings(0.25, "rk4")
    assert stock.to_dict() == {"name": "P", "initial": 10, "min": None,
                               "thresholds": [{"value": 5}]}
    assert flow.to_dict() == {"name": "net", "rate": 'LEVEL("P")', "from": "P", "biflow": True}
    assert settings.to_dict() == {"dt": 0.25, "method": "rk4"}
    assert sd.Stock("Q").to_dict() == {"name": "Q"}
    for record in (stock, flow, settings):
        assert type(record).from_dict(record.to_dict()) == record


def test_a_script_builds_and_runs_a_logistic_model():
    layout = sd.add({"simulation": {"time_limit": 50}}, sd.SDSettings(0.25, "rk4"),
                    sd.Stock("P", 10),
                    sd.Flow("growth", '0.2*LEVEL("P")*(1-LEVEL("P")/100)', target="P"))
    assert layout["simulation"]["sd"] == {"dt": 0.25, "method": "rk4"}
    exact = 100 / (1 + 9 * math.exp(-0.2 * 50))
    assert _final(layout, "P") == pytest.approx(exact, rel=1e-6)


def test_the_document_accepts_records_and_sd_settings_with_undo():
    from nagasim.core.document import ModelDocument

    doc = ModelDocument()
    doc.upsert_level(sd.Stock("P", 5, min=None))
    doc.upsert_flow(sd.Flow("f", "1", target="P"))
    assert doc.levels[0] == {"name": "P", "initial": 5, "min": None}
    assert doc.flows[0]["to"] == "P"
    doc.set_sd_settings(sd.SDSettings(0.5))
    assert doc.simulation["sd"] == {"dt": 0.5}
    doc.set_sd_settings(None)
    assert "sd" not in doc.simulation
    doc.undo()
    assert doc.simulation["sd"] == {"dt": 0.5}


# ================================================================ agent tools
def test_agent_tools_set_sd_settings_min_null_and_biflow(tmp_path):
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    T.call("upsert_record", model="m", section="levels", record={"name": "A", "min": None})
    T.call("upsert_record", model="m", section="levels",
           record={"name": "B", "min": None, "initial": 10.0})
    result = T.call("upsert_record", model="m", section="flows", record={
        "name": "mix", "from": "A", "to": "B", "rate": '0.5*(LEVEL("A") - LEVEL("B"))',
        "biflow": True})
    assert "error" not in result, result
    result = T.call("set_sd_settings", model="m", dt=0.1, method="rk4")
    assert result["sd"] == {"dt": 0.1, "method": "rk4"}
    listed = T.call("list_continuous", model="m")
    assert listed["sd"] == {"dt": 0.1, "method": "rk4"}
    assert listed["levels"][0]["min"] is None and listed["flows"][0]["biflow"] is True
    assert T.call("set_sd_settings", model="m", dt=None)["sd"] is None


# ================================================================ perf guard
@pytest.mark.slow
@pytest.mark.perf
def test_perf_100_dynamic_levels_x_720_steps():
    """Perf guard (PHASE5 refinements): 100 levels, 720 Euler steps, under
    0.5 s. Measured 2026-09-24: 0.20 s alone, but 0.5-0.9 s under `-n auto`
    contention, so it runs in the serial full set like the other perf guard
    (`test_smoke_engine`); the fast set keeps the evaluation-count proxy
    (`test_only_dynamic_rates_are_evaluated_on_a_grid_wake_up`)."""
    layout = {"blocks": [], "links": [],
              "levels": [{"name": f"L{i}", "initial": 100.0} for i in range(100)],
              "flows": [{"name": f"f{i}", "rate": f'0.01*LEVEL("L{i}")', "from": f"L{i}"}
                        for i in range(100)],
              "simulation": {"time_limit": 720.0, "sd": {"dt": 1.0}}}
    started = time.perf_counter()
    env = _run(layout, 720.0)
    elapsed = time.perf_counter() - started
    assert env.ctx.level_system.levels["L0"].level == pytest.approx(100 * 0.99 ** 720, rel=1e-9)
    assert elapsed < 0.5, f"{elapsed:.3f} s"


# ======================================================== SD-2: expansion
def test_expand_is_the_identity_for_a_model_without_delays():
    layout = _model()
    assert sd.expand(layout) is layout


def test_inline_calls_become_generated_delay_records_and_hidden_stages():
    layout = _model(rate='2 * SMOOTH(TS("é"), 5) + DELAYP(1, 2, 7) + DELAY3(1, 4)')
    before = repr(layout)
    out = sd.expand(layout)
    assert repr(layout) == before                                   # pure
    # Byte offsets survive a non-ASCII literal ahead of the calls.
    assert out["flows"][0]["rate"] == ('2 * DELAY("out#1") + DELAY("out#2") + DELAY("out#3")')
    assert [(d["name"], d["kind"], d.get("order"), d.get("initial")) for d in out["delays"]] == [
        ("out#1", "information", 1, 'TS("é")'), ("out#2", "pipeline", None, "7"),
        ("out#3", "material", 3, "1")]                  # no initial: the input's (DEVLOG #167)
    assert all(d["hidden"] for d in out["delays"])
    hidden = {r["name"] for r in out["levels"] + out["flows"] if r.get("hidden")}
    # `#1` drops out of the stem; a pipeline has no stages.
    assert hidden == {"out~s1", "out~f1",
                      "out#3~s1", "out#3~s2", "out#3~s3",
                      "out#3~f1", "out#3~f2", "out#3~f3", "out#3~out"}
    assert sd.expand(out) == out                                    # idempotent


def test_nested_inline_calls_expand_through_the_generated_input():
    out = sd.expand(_model(rate="SMOOTH(DELAY1(1, 3), 2)"))
    assert out["flows"][0]["rate"] == 'DELAY("out#1")'
    assert [(d["name"], d["input"]) for d in out["delays"]] == [
        ("out#1", 'DELAY("out#1#1")'), ("out#1#1", "1")]
    assert out["delays"][0]["initial"] == 'DELAY("out#1#1")'     # DEVLOG #167
    frame = run_model(_model(rate="SMOOTH(DELAY1(4, 3), 2)", T=2.0))["level_summary"]
    assert float(frame.loc[frame["Level"] == "L", "Final"].iloc[0]) == pytest.approx(92.0)


def test_a_material_delay_runs_its_stages_and_hides_them_from_every_output():
    layout = {"blocks": [], "links": [],
              "levels": [{"name": "Shipped", "initial": 0.0}],
              "flows": [{"name": "arrive", "rate": 'DELAY("ship")', "to": "Shipped"}],
              "auxiliaries": [{"name": "pending", "expr": 'LEVEL("ship~s1") + LEVEL("ship~s2")'}],
              "delays": [sd.Delay("ship", "STEP(10, 1)", 2, order=2).to_dict()],
              "simulation": {"time_limit": 30.0, "sd": {"dt": 0.25}}}
    results = run_model(layout)
    assert list(results["level_summary"]["Level"]) == ["Shipped"]
    assert list(results["flow_summary"]["Flow"]) == ["arrive"]
    series = set(results["timeseries"]["Series"])
    assert {"level/Shipped", "aux/pending"} <= series
    assert not any("~" in name for name in series)
    # Conservation in transit is test_sd_constructs_oracle.py::test_material_delay_conserves_...


# ============================================================ SD-2: functions
def test_step_pulse_ramp_if_and_their_change_instants():
    from nagasim.core.expressions import ExpressionScope, compile_expression

    class Ctx:
        now = 5.0
    scope = ExpressionScope(ctx=Ctx())
    value = compile_expression("STEP(1, 5) + PULSE(10, 4, 1) + RAMP(2, 3, 4) + IF(0 > 1, 1/0, 3)")
    assert value.evaluate(scope) == 1 + 0 + 2 + 3        # PULSE is over at 5; RAMP holds after 4
    assert value.instants == (3.0, 4.0, 5.0)
    assert compile_expression("STEP(1, t0)", {"t0"}).instants is None
    assert levels_mod.is_dynamic(compile_expression("STEP(1, t0)", {"t0"}))
    assert not levels_mod.is_dynamic(compile_expression("STEP(1, 5)"))


def test_an_aux_makes_its_reader_dynamic_and_bus_reading():
    from nagasim.core.expressions import compile_expression

    auxes = {"a": compile_expression('AUX("b")'), "b": compile_expression('0.1 * LEVEL("L")'),
             "c": compile_expression('FLOW("x")')}
    assert levels_mod.is_dynamic(compile_expression('AUX("a")'), auxes)
    assert not levels_mod.is_dynamic(compile_expression('AUX("c")'), auxes)

    class Flow:
        rate_expr = compile_expression('AUX("c")')
    assert levels_mod._reads_of(Flow, auxes) == {("flow", "x")}


def test_level_initial_expressions_run_in_dependency_order():
    layout = _model(levels=[{"name": "L", "initial": 'AUX("half") * 4'},
                            {"name": "M", "initial": 10.0}],
                    auxiliaries=[{"name": "half", "expr": 'LEVEL("M") / 2'}])
    env = _run(layout, 0.25)
    assert env.ctx.level_system.levels["L"].initial == 20.0


# =============================================================== SD-2: codes
# E_AUX_CYCLE, E_AUX_UNKNOWN and E_LEVEL_INITIAL_CYCLE are test_sd_constructs_oracle.py's.
@pytest.mark.parametrize("code, extra", [
    ("E_AUX_NAME", {"auxiliaries": [{"name": "a", "expr": "1"}, {"name": "a", "expr": "2"}]}),
    ("E_DELAY_UNKNOWN", {"auxiliaries": [{"name": "a", "expr": 'DELAY("zz")'}]}),
    ("E_DELAY_NAME", {"delays": [{"name": "", "input": "1", "delay_time": 1}]}),
    ("E_DELAY_SPEC", {"delays": [{"name": "d", "input": "1", "delay_time": 0}]}),
    ("E_DELAY_SPEC", {"delays": [{"name": "d", "input": "1", "delay_time": 1, "kind": "x"}]}),
    ("E_SD_DELAY_ORDER", {"auxiliaries": [{"name": "a", "expr": "DELAYN(1, 3, 2.5)"}]}),
    ("E_SD_FUNCTION_CONTEXT", {"variables": [{"name": "p", "initial": 1}],
                               "flows": [{"name": "out", "rate": "1", "from": "L",
                                          "price": "SMOOTH(p, 2)"}]}),
])
def test_sd2_problem_codes(code, extra):
    assert code in _codes(_model(**extra))


def test_material_and_information_delays_need_the_grid_but_a_pipeline_does_not():
    layout = _model(auxiliaries=[{"name": "a", "expr": "SMOOTH(1, 3) + DELAYP(1, 3)"}])
    del layout["simulation"]["sd"]
    problems = [p for p in validate_model_structured(layout) if p.code == "E_SD_REQUIRES_DT"]
    assert [p.message.split("'")[1] for p in problems] == ["a#1"]


def test_rk4_refuses_random_numbers_reached_through_an_aux():
    layout = _model(rate='AUX("noisy")', method="rk4",
                    auxiliaries=[{"name": "noisy", "expr": 'RAND() * LEVEL("L")'}])
    assert "E_SD_RK4_RANDOM" in _codes(layout)


# ====================================================== SD-2: authoring, doc
def test_aux_and_delay_records_round_trip_and_add_to_their_sections():
    aux = sd.Aux("g", "0.1", diagram={"x": 1})
    delay = sd.Delay("d", "x", 3, kind="information", order=3)
    assert aux.to_dict() == {"name": "g", "expr": "0.1", "diagram": {"x": 1}}
    assert delay.to_dict() == {"name": "d", "input": "x", "delay_time": 3,
                               "kind": "information", "order": 3}
    for record in (aux, delay):
        assert type(record).from_dict(record.to_dict()) == record
    layout = sd.add({}, aux, delay)
    assert (aux.SECTION, delay.SECTION) == ("auxiliaries", "delays")
    assert layout == {"auxiliaries": [aux.to_dict()], "delays": [delay.to_dict()]}


def test_the_document_edits_auxiliaries_and_delays_with_undo(tmp_path):
    from nagasim.core.document import ModelDocument

    doc = ModelDocument()
    doc.upsert_auxiliary(sd.Aux("g", "0.1"))
    doc.upsert_delay(sd.Delay("d", "1", 2, kind="pipeline"))
    doc.rename_auxiliary("g", "h")
    doc.rename_delay("d", "e")
    assert (doc.auxiliaries, doc.delays) == ([{"name": "h", "expr": "0.1"}],
                                             [{"name": "e", "input": "1", "delay_time": 2,
                                               "kind": "pipeline"}])
    doc.save(tmp_path / "m.json")
    loaded = ModelDocument.load(tmp_path / "m.json")
    assert (loaded.auxiliaries, loaded.delays) == (
        doc.auxiliaries, [{**doc.delays[0], "delay_time_unit": "minutes"}])     # save fills it
    doc.remove_auxiliary("h")
    doc.remove_delay("e")
    assert (doc.auxiliaries, doc.delays) == ([], [])
    doc.undo()
    assert doc.delays and not doc.auxiliaries


def test_a_scoped_auxiliary_is_cloned_per_instance_with_its_parameters():
    def instance(name, r):
        return {"name": name, "description": name, "type": "SUBMODEL", "position": [0, 0],
                "params": {"definition": "Pop", "arguments": {"r": r}}}
    layout = {"blocks": [instance("a", 2.0), instance("b", 3.0)], "links": [],
              "levels": [{"name": "P", "initial": 1.0}],
              "flows": [{"name": "grow", "rate": 'AUX("$g")', "to": "P"}],
              "auxiliaries": [{"name": "g", "expr": "r"}],
              "submodels": {"Pop": {"parameters": [{"name": "r", "default": 1.0}],
                                    "blocks": [], "links": [],
                                    "scoped": {"levels": {"P": {}}, "auxiliaries": {"g": {}}}}},
              "simulation": {"time_limit": 5.0}}
    from nagasim.core.submodels import flatten

    flat = flatten(layout)
    assert [(a["name"], a["locals"]) for a in flat["auxiliaries"]] == [
        ("a/g", {"r": 2.0}), ("b/g", {"r": 3.0})]
    frame = run_model(layout)["level_summary"]
    assert dict(zip(frame["Level"], frame["Final"])) == pytest.approx({"a/P": 11.0, "b/P": 16.0})


def test_agent_tools_edit_auxiliaries_and_delays(tmp_path):
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    T.call("upsert_record", model="m", section="levels", record={"name": "A", "initial": "2 * 3"})
    T.call("upsert_record", model="m", section="auxiliaries",
           record={"name": "g", "expr": '0.1 * LEVEL("A")'})
    result = T.call("upsert_record", model="m", section="delays", record={
        "name": "lag", "input": 'AUX("g")', "delay_time": 2, "kind": "pipeline"})
    assert result["record"] == "lag" and "error" not in result, result
    listed = T.call("list_continuous", model="m")
    assert listed["auxiliaries"] == [{"name": "g", "expr": '0.1 * LEVEL("A")'}]
    assert listed["delays"] == [{"name": "lag", "input": 'AUX("g")', "delay_time": 2,
                                 "kind": "pipeline"}]
    assert listed["levels"][0]["initial"] == "2 * 3"
    summary = T.call("get_model_summary", model="m")
    assert (summary["auxiliaries"], summary["delays"]) == (["g"], ["lag"])
    T.call("remove_record", model="m", section="auxiliaries", key="g")
    assert "E_AUX_UNKNOWN" in {p["code"] for p in T.call("validate_model", model="m")["problems"]}
    T.call("remove_record", model="m", section="delays", key="lag")
    assert T.call("list_continuous", model="m")["delays"] == []
