"""explain_kpi (Phase 10, D713-D716): measured contributors to a KPI, ranked, and one
suggested what-if test that is never run."""
import json
import re
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import experiments, explain, verify

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "nagasim" / "models"
TT = "system/Avg Throughput Time"
NO_CAUSE = re.compile(r"\b(cause[sd]?|because)\b", re.IGNORECASE)


def _res(name, wait, seizes, usage=50.0, down=0.0):
    return {f"resource/{name}/Time in Queue Avg (incl. waiting)": wait,
            f"resource/{name}/Total Seizes": seizes,
            f"resource/{name}/Usage % (snapshot)": usage, f"resource/{name}/Down %": down}


def _table(**extra):
    """Two replications, 100 completed each: A waits 5 x 100 / 100 = 5, B 2 x 50 / 100 = 1,
    C never seized; the KPI is 10, so the rest is 4 and the waits cover 60%."""
    table = {TT: [10.0, 10.0], explain.COMPLETED: [100.0, 100.0],
             explain.THROUGHPUT: [0.5, 0.5], "system/Avg WIP": [5.0, 5.0],
             **_res("A", [4.0, 6.0], [100.0, 100.0], usage=[90.0, 92.0], down=[5.0, 5.0]),
             **_res("B", [2.0, 2.0], [50.0, 50.0], usage=[40.0, 42.0]),
             **_res("C", [0.0, 0.0], [0.0, 0.0], usage=[0.0, 0.0])}
    table.update(extra)
    return {k: (v if isinstance(v, list) else [v, v]) for k, v in table.items()}


def test_level_rows_are_ranked_measured_shares_with_a_coverage_line():
    answer = explain.rows_of(_table(), TT)
    rows = [(r["row"], round(r["mean"], 6), round(r["share"], 6)) for r in answer["rows"]]
    assert rows == [("A", 5.0, 0.5), (explain.REST, 4.0, 0.4), ("B", 1.0, 0.1)]
    assert all(r["label"] == "measured" for r in answer["rows"])
    assert "C" not in {r["row"] for r in answer["rows"]}          # never seized: no row
    assert answer["coverage"] == pytest.approx(0.6)
    assert "waiting rows cover 60% of the KPI" in answer["notes"]
    assert answer["summary"] == "A holds the largest measured share"


def test_waits_beyond_the_kpi_drop_the_rest_row_and_say_so():
    answer = explain.rows_of(_table(**{TT: 5.5}), TT)
    assert [r["row"] for r in answer["rows"]] == ["A", "B"]
    assert answer["coverage"] == pytest.approx(6 / 5.5)
    assert "still in the system at the end" in answer["notes"][0]


def test_wip_rows_are_the_waits_times_the_throughput():
    answer = explain.rows_of(_table(), "system/Avg WIP")
    assert answer["rows"][0]["row"] == "A" and answer["rows"][0]["mean"] == pytest.approx(2.5)
    assert answer["coverage"] == pytest.approx(3.0 / 5.0)


def test_throughput_without_bottleneck_shares_ranks_by_usage_and_says_so():
    answer = explain.rows_of(_table(), explain.THROUGHPUT, blocks={"A": ["a_block"]})
    assert [r["row"] for r in answer["rows"]] == ["A", "B"]
    assert "outputs.bottleneck" in answer["notes"][0]
    with_buffer = explain.rows_of(_table(**{"buffer/a_block/Blocked %": 12.0}),
                                  explain.THROUGHPUT, blocks={"A": ["a_block"]})
    assert with_buffer["rows"][0]["beside"] == {"Down %": 5.0, "Blocked %": 12.0}


def test_throughput_with_bottleneck_shares_ranks_by_them():
    table = _table(**{"bottleneck/A/Total %": 20.0, "bottleneck/B/Total %": 70.0})
    answer = explain.rows_of(table, explain.THROUGHPUT)
    assert [r["row"] for r in answer["rows"]] == ["B", "A"] and not answer["notes"]
    assert answer["rows"][0]["beside"]["Usage % (snapshot)"] == pytest.approx(41.0)


def test_a_resource_kpi_lists_that_resources_own_rows():
    answer = explain.rows_of(_table(), "resource/A/Down %")
    assert [r["row"].rpartition("/")[2] for r in answer["rows"]] == [
        "Usage % (snapshot)", "Down %", "Time in Queue Avg (incl. waiting)"]
    assert {r["resource"] for r in answer["rows"]} == {"A"}


def test_an_unexplained_or_unknown_kpi_is_refused():
    with pytest.raises(ValueError, match="explains"):
        explain.rows_of(_table(), "system/Total Cost")
    with pytest.raises(ValueError, match="unknown KPI"):
        explain.rows_of(_table(), "system/Avg WIP (Little)")
    with pytest.raises(ValueError, match="unknown KPI"):
        explain.rows_of(_table(), "resource/Z/Down %")


def test_change_rows_rank_by_paired_difference_with_the_undecided_last():
    base = {TT: [10.0, 11.0, 12.0], explain.COMPLETED: [100.0] * 3,
            **_res("A", [5.0, 5.5, 4.5], [100.0] * 3), **_res("B", [2.0] * 3, [50.0] * 3)}
    changed = {**base, TT: [8.0, 9.1, 9.9],
               "resource/A/Time in Queue Avg (incl. waiting)": [2.0, 3.9, 2.1],  # falls
               "resource/B/Time in Queue Avg (incl. waiting)": [2.5, 1.5, 2.0]}  # noise only
    answer = explain.compare(base, changed, TT)
    order = [(r["row"], r["verdict"]) for r in answer["rows"]]
    # B (diff 0) and the rest (diff 1/3) are both undecided, so they follow A by |diff|.
    assert order == [("A", "lower"), (explain.REST, "not distinguishable"),
                     ("B", "not distinguishable")]
    assert all(r["label"] == "measured" for r in answer["rows"])
    assert answer["value"]["verdict"] == "lower"
    assert answer["summary"] == "A has the largest measured difference"


def test_the_suggested_test_runs_through_what_if_unchanged():
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    test, note = explain.suggested_test(layout, "Server", TT)
    assert note is None and test["label"] == "suggested test" and test["tool"] == "what_if"
    assert test["arguments"] == {"overrides": [{"path": "resources.Server.capacity", "value": 2}],
                                 "kpis": [TT]}
    out = experiments.what_if(layout, **test["arguments"], start=2, cap=2, run_length=200,
                              warmup=20)
    assert out["completed"] and [k["kpi"] for k in out["kpis"]] == [TT]


def test_an_instance_resource_gets_its_argument_and_a_template_none():
    layout = json.loads((MODELS / "line_with_buffers.json").read_text(encoding="utf-8"))
    test, _ = explain.suggested_test(layout, "station_1/Mfg Machine", TT)
    assert test["arguments"]["overrides"] == [
        {"path": "blocks.station_1.params.arguments.machines", "value": 2}]
    none, note = explain.suggested_test(layout, "Mfg Machine", TT)
    assert none is None and note.startswith("no suggested test")


def test_mm1_waiting_row_matches_wq():
    """`naga verify`'s M/M/1 (lambda 1, mu 1.25): the waiting row is Wq = rho / (mu - lambda)
    = 3.2 within its interval, and the rest is the service time."""
    case = verify._mm1()
    answer = explain.explain_kpi(case.layout_fn(), TT, replications=8, run_length=case.run_len,
                                 warmup=case.warmup)
    wait = next(r for r in answer["rows"] if r["resource"] == "Server")
    assert wait["lb"] <= 3.2 <= wait["ub"], wait
    rest = next(r for r in answer["rows"] if r["row"] == explain.REST)
    assert rest["lb"] <= 0.8 <= rest["ub"], rest
    assert answer["suggested_test"]["arguments"]["overrides"] == [
        {"path": "resources.Server.capacity", "value": 2}]


def test_cli_and_agent_tool_on_one_model(tmp_path, capsys):
    assert explain.main(["single_server", TT, "--replications", "2"]) == 0
    text = capsys.readouterr().out
    assert "Server" in text and "(measured)" in text and "suggested test:" in text
    assert not NO_CAUSE.search(text)
    assert explain.main(["single_server", "system/Total Cost"]) == 2

    shutil.copy(MODELS / "single_server.json", tmp_path / "single_server.json")
    T.use_workspace(T.Workspace(tmp_path))
    assert "error" not in T.call("open_model", path="single_server.json", name="m")
    out = T.call("explain_kpi", model="m", kpi=TT, replications=2, run_length=300, warmup=30)
    assert out["mode"] == "level" and out["rows"][0]["label"] == "measured"
    assert out["suggested_test"]["label"] == "suggested test"
    assert not NO_CAUSE.search(json.dumps(out))
    assert T.call("explain_kpi", model="m", kpi="nope")["error"]["code"] == "E_UNKNOWN_KPI"


@pytest.mark.slow
def test_change_mode_capacity_at_the_bottleneck_puts_its_wait_first():
    layout = json.loads((MODELS / "line_with_buffers.json").read_text(encoding="utf-8"))
    level = explain.explain_kpi(layout, TT, replications=5, run_length=1200, warmup=120)
    top = level["rows"][0]["resource"]
    overrides = [{"path": "blocks.station_1.params.arguments.machines", "value": 2}]
    assert top == "station_1/Mfg Machine"
    answer = explain.explain_kpi(layout, TT, overrides, replications=5, cap=10,
                                 run_length=1200, warmup=120)
    first = answer["rows"][0]
    assert answer["mode"] == "change" and first["row"] == top and first["verdict"] == "lower"
    assert not NO_CAUSE.search(explain.render(answer))


def test_859_the_top_two_rows_not_separated_are_said():
    """#859: level mode named a winner even when the top two intervals overlap."""
    note = "the top two rows are not separated at this number of replications (#859)"
    assert note in explain.rows_of(_table(), TT)["notes"]          # A 5 [-7.7, 17.7] vs rest 4
    tight = explain.rows_of(_table(**{"resource/A/Time in Queue Avg (incl. waiting)": [5.0, 5.0]}), TT)
    assert note not in tight["notes"]


def test_857_a_lightly_loaded_line_still_gets_a_test_for_its_top_resource():
    """#857: when "processing and moving" ranked first there was no suggested test; the test
    is for the top resource row."""
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    layout["blocks"][0]["params"]["dist_params"] = {"rate": 0.2}      # rho 0.16: service dominates
    answer = explain.explain_kpi(layout, TT, None, replications=2, run_length=500, warmup=0)
    assert answer["rows"][0]["row"] == explain.REST
    assert answer["suggested_test"]["arguments"]["overrides"][0]["path"].endswith("capacity")


def test_860_the_replication_cap_is_clamped_for_agents(tmp_path, monkeypatch):
    """#860: an agent could pass cap=10**6 to what_if or explain_kpi; both clamp to 200."""
    shutil.copy(MODELS / "single_server.json", tmp_path / "m.json")
    T.use_workspace(T.Workspace(tmp_path))
    T.call("open_model", path="m.json", name="m")
    seen = []
    monkeypatch.setattr(experiments, "what_if", lambda *a, **k: seen.append(k["cap"]) or
                        {"n": 2, "met": True, "completed": True, "kpis": []})
    monkeypatch.setattr(explain, "explain_kpi", lambda *a, **k: seen.append(k["cap"]) or
                        {"kpi": TT, "rows": [], "notes": []})
    overrides = [{"path": "resources.Server.capacity", "value": 2}]
    T.call("what_if", model="m", overrides=overrides, kpis=[TT], cap=10 ** 6)
    T.call("explain_kpi", model="m", kpi=TT, cap=10 ** 6)
    assert seen == [200, 200]
