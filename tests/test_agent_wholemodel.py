"""The whole-model tools (D396): `get_model_format` describes the file from the live
registries, and `write_model` takes the complete file in one call and answers with its
problems, read and validated as a file on disk is."""
import json

import pytest

from nagasim import agent_tools as T
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.distributions import DISTRIBUTION_REGISTRY


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def _mm1(**simulation):
    return {"blocks": [{"name": "c", "type": "CREATE",
                        "params": {"distribution": "Exponential", "dist_params": {"rate": 1}}},
                       {"name": "p", "type": "PROCESS",
                        "params": {"resource_name": "R", "distribution": "Exponential",
                                   "dist_params": {"rate": 1.25}}},
                       {"name": "d", "type": "DISPOSE"}],
            "links": [{"source": "c", "target": "p"}, {"source": "p", "target": "d"}],
            "resources": [{"name": "R"}],
            "simulation": {"time_limit": 500, **simulation}}


def test_the_format_names_every_block_distribution_and_function_and_its_example_is_valid(ws):
    text = T.call("get_model_format")["format"]
    assert all(f"\n{name}" in text for name in BLOCK_REGISTRY)
    assert all(f"{name}(" in text for name in DISTRIBUTION_REGISTRY)
    assert "Exponential(rate)" in text and "IDLE/1: free units" in text
    assert len(text) < 20_000                  # read once per conversation, then every turn
    example = json.loads(text.split("Example (an M/M/1 queue): ", 1)[1].split("\n", 1)[0])
    answer = T.call("write_model", model="example", layout=example)
    assert answer["ok"] and answer["problems"] == [], answer

    detail = T.call("get_model_format", blocks=["process"], sections=["levels"])["format"]
    assert detail.startswith("PROCESS: ") and "priority=0 (int) -- Lower draws first" in detail
    assert "levels: [{" in detail and "initial=0" in detail
    assert T.call("get_model_format", blocks=["NOPE"])["error"]["code"] == "E_UNKNOWN_BLOCK_TYPE"
    assert T.call("get_model_format", sections=["nope"])["error"]["code"] == "E_UNKNOWN_SECTION"


def test_a_whole_model_is_written_run_and_saved_in_three_calls(ws, tmp_path):
    answer = T.call("write_model", model="m", layout=_mm1())
    assert answer == {"ok": True, "model": "m", "blocks": 3, "errors": 0, "warnings": 0,
                      "requires_run_length": True, "problems": []}
    positions = {tuple(block.position) for block in ws.document("m").blocks.values()}
    assert len(positions) == 3                  # laid out, not piled on one spot
    assert T.call("run_model", model="m")["system"]["Avg Throughput Time"]["mean"] > 0
    assert T.call("save_model", model="m", path="m.json")["ok"]
    assert json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))["resources"][0]["name"] == "R"


def test_the_answer_lists_each_problem_with_its_path_and_a_hint(ws):
    layout = _mm1()
    layout["blocks"][0]["params"]["dist_params"] = {"mean": 1}
    layout["resource"] = layout.pop("resources")
    layout["simulation"] = {"run_length": 500, "sd": {"dt": 1, "step": 2}}
    answer = T.call("write_model", model="m", layout=layout)
    found = {p["path"]: p for p in answer["problems"]}
    assert not answer["ok"] and answer["errors"] >= 2
    assert found["/blocks/0/params/dist_params/mean"]["code"] == "E_DIST_PARAM_UNKNOWN"
    assert "rate is 1 / the mean" in found["/blocks/0/params/dist_params/mean"]["hint"]
    assert found["/blocks/1/params/resource_name"]["code"] == "E_RESOURCE_UNKNOWN"
    for path in ("/resource", "/simulation/run_length", "/simulation/sd/step"):
        assert found[path]["code"] == "W_UNKNOWN_KEY", path
    assert "time_limit" in found["/simulation/run_length"]["hint"]
    assert all(set(p) <= {"code", "path", "message", "hint"} for p in answer["problems"])


@pytest.mark.parametrize("layout", [{"blocks": "x"}, {"blocks": [{"type": "CREATE"}]},
                                    {"blocks": [3]}, {"links": 5}], ids=range(4))
def test_a_layout_that_cannot_be_read_is_a_coded_refusal_and_changes_nothing(ws, layout):
    assert T.call("write_model", model="m", layout=_mm1())["ok"]
    refused = T.call("write_model", model="m", layout=layout)
    assert refused["error"]["code"] in ("E_MODEL_SHAPE", "E_INVALID_MODEL"), refused
    assert len(ws.document("m").blocks) == 3
    assert T.call("write_model", model="m", layout=[1])["error"]["code"] == "E_BAD_ARGUMENT"


def test_rewriting_an_open_model_is_one_undo_step_and_drops_its_old_results(ws):
    T.call("write_model", model="m", layout=_mm1())
    T.call("run_model", model="m")
    smaller = _mm1()
    smaller["resources"][0]["capacity"] = 2
    assert T.call("write_model", model="m", layout=smaller)["ok"]
    assert "m" not in ws.results
    assert ws.document("m").resources[0]["capacity"] == 2
    assert T.call("undo", model="m")["label"] == "Write model"
    assert ws.document("m").resources[0].get("capacity", 1) == 1


def test_a_written_model_can_grant_itself_no_permission(ws):
    """Permission is the caller's, never the file's: a data source that asks for the network
    is told the key did nothing, and the key is gone from the model."""
    layout = _mm1()
    layout["data_sources"] = [{"name": "feed", "kind": "csv", "path": "x.csv", "allow_network": True}]
    answer = T.call("write_model", model="m", layout=layout)
    assert "W_DATA_ALLOW_NETWORK_IGNORED" in {p["code"] for p in answer["problems"]}
    assert "allow_network" not in T.call("get_model_json", model="m")["layout"]["data_sources"][0]
    assert ws.network.allow_network is False


def test_the_run_digest_carries_entity_types_levels_and_leaving_customers(ws):
    layout = _mm1()
    layout["blocks"].insert(0, {"name": "c2", "type": "CREATE",
                                "params": {"e_type": "OTHER", "distribution": "Exponential",
                                           "dist_params": {"rate": 0.2}}})
    layout["links"].append({"source": "c2", "target": "p"})
    layout["blocks"][2]["params"]["balk_if"] = 'NQ("R") >= 2'
    layout["blocks"].append({"name": "gone", "type": "DISPOSE"})
    layout["links"].append({"source": "p", "target": "gone"})
    layout["levels"] = [{"name": "Tank", "initial": 5}]
    layout["flows"] = [{"name": "drain", "from": "Tank", "rate": 1}]
    assert T.call("write_model", model="m", layout=layout)["ok"]
    digest = T.call("run_model", model="m")
    assert len(digest["entities"]) == 2 and "OTHER" in digest["entities"]
    assert digest["levels"]["Tank"]["Time Empty %"]["mean"] == pytest.approx(99, abs=0.5)
    assert digest["queues"]["p"]["Balked"]["mean"] > 0
    T.call("write_model", model="one", layout=_mm1())
    assert not {"entities", "levels", "queues", "agents"} & set(T.call("run_model", model="one"))


def test_a_timed_pump_read_from_a_repeating_step_profile_is_exact(ws):
    """The study's tank task: the format and the clock warning point at a TS() profile,
    and that is exact (a TNOW test on a one-minute grid answered 15% for 16.67%)."""
    pump = 'IF(MODULO(TNOW, 60) < 20, 25, 0)'
    layout = {"levels": [{"name": "Tank"}],
              "flows": [{"name": "pump", "to": "Tank", "rate": pump},
                        {"name": "draw", "from": "Tank", "rate": 10}],
              "simulation": {"time_limit": 6000, "time_unit": "minutes"}}
    answer = T.call("write_model", model="t", layout=layout)
    assert [p["code"] for p in answer["problems"]] == ["W_FLOW_CLOCK_NO_GRID"]
    assert "TS(" in answer["problems"][0]["hint"]
    layout["timeseries_inputs"] = [{"name": "pump", "times": [0, 20], "values": [25, 0],
                                    "beyond": "repeat", "period": 60}]
    layout["flows"][0]["rate"] = 'TS("pump")'
    del layout["timeseries_inputs"][0]["beyond"]          # a period alone repeats nothing
    problems = T.call("write_model", model="t", layout=layout)["problems"]
    assert [p["code"] for p in problems] == ["W_SERIES_PERIOD_UNUSED"]
    layout["timeseries_inputs"][0]["beyond"] = "repeat"
    assert T.call("write_model", model="t", layout=layout)["problems"] == []
    empty = T.call("run_model", model="t")["levels"]["Tank"]["Time Empty %"]["mean"]
    assert empty == pytest.approx(100 / 6, abs=0.01)


def test_a_balk_test_on_available_is_told_it_reads_the_capacity(ws):
    """The study's car park task: balk_if AVAILABLE("R") == 0 never fires, since AVAILABLE is
    the capacity in force; the answer says so and names IDLE."""
    layout = _mm1()
    layout["blocks"][1]["params"]["balk_if"] = 'AVAILABLE("R") == 0'
    layout["blocks"].append({"name": "gone", "type": "DISPOSE"})
    layout["links"].append({"source": "p", "target": "gone"})
    problems = T.call("write_model", model="m", layout=layout)["problems"]
    assert [(p["code"], p["path"]) for p in problems] == [("W_AVAILABLE_IS_CAPACITY", "/blocks/1/params/balk_if")]
    assert "IDLE" in problems[0]["hint"]
    layout["blocks"][1]["params"]["balk_if"] = 'IDLE("R") == 0'
    assert T.call("write_model", model="m", layout=layout)["problems"] == []
