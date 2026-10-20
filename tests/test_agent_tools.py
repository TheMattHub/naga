"""ROADMAP 3.2: the agent tool surface, driven end to end WITHOUT an LLM.

A scripted sequence of tool calls stands in for the agent: it builds M/M/1 from
nothing, repairs a deliberately broken model using only the returned problem
codes and hints, runs it and checks the utilization against theory. The rest
guards the contract an agent depends on -- every tool documented and
schema'd, answers small enough to fit a context window, paths confined to the
workspace root, undo/redo through the tools.
"""
import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "nagasim" / "models"
SIZE_CAP = 20_000       # bytes; an answer bigger than this crowds out the conversation


@pytest.fixture
def ws(tmp_path):
    """A workspace rooted in a scratch directory, with the examples copied in."""
    for path in EXAMPLES.glob("*.json"):
        shutil.copy(path, tmp_path / path.name)
    return T.use_workspace(T.Workspace(tmp_path))


def ok(result):
    """Assert a tool answered rather than refused, and return the answer."""
    assert "error" not in result, result.get("error")
    return result


def upsert(model, section, **record):
    """One generic record edit (D147), the way an agent writes it."""
    return T.call("upsert_record", model=model, section=section, record=record)


def remove(model, section, key):
    return T.call("remove_record", model=model, section=section, key=key)


def records(model, section):
    return ok(T.call("list_records", model=model, section=section))["records"]


def build_mm1(model="mm1", *, rate=1.0, service_rate=1.25, capacity=1):
    """The scripted 'agent': build M/M/1 from an empty model."""
    ok(T.call("new_model", name=model))
    ok(T.call("add_block", model=model, type="CREATE", name="arrive",
              params={"distribution": "Exponential", "dist_params": {"rate": rate},
                      "max_arrivals": 0}))
    ok(T.call("add_block", model=model, type="PROCESS", name="serve",
              params={"resource_name": "Server", "distribution": "Exponential",
                      "dist_params": {"rate": service_rate}}))
    ok(T.call("add_block", model=model, type="DISPOSE", name="leave"))
    ok(T.call("connect", model=model, source="arrive", target="serve"))
    ok(T.call("connect", model=model, source="serve", target="leave"))
    ok(upsert(model, "resources", name="Server", capacity=capacity))
    ok(T.call("set_simulation_settings", model=model, run_length=2000, warmup=300,
              replications=5))
    return model


# -- the scripted agent session ----------------------------------------------

def test_an_agent_builds_mm1_from_scratch_and_the_utilization_matches_theory(ws):
    model = build_mm1()
    report = ok(T.call("validate_model", model=model))
    assert report["ok"] and report["errors"] == 0
    assert report["requires_run_length"] is True     # endless CREATE

    digest = ok(T.call("run_model", model=model))
    assert digest["replications"] == 5
    utilization = digest["resources"]["Server"]["Usage % (snapshot)"]["mean"] / 100.0
    assert utilization == pytest.approx(0.8, abs=0.08)   # rho = lambda/mu = 1.0/1.25
    assert digest["system"]["Avg Throughput Time"]["ci_half_width"] > 0


def test_every_edit_reports_the_problems_it_introduced_and_fixed(ws):
    ok(T.call("new_model", name="m"))
    added = ok(T.call("add_block", model="m", type="CREATE", name="arrive",
                      params={"max_arrivals": 1}))
    assert added["errors"] > 0
    assert any(p["code"] == "E_CREATE_NO_OUTPUT" for p in added["introduced"])

    ok(T.call("add_block", model="m", type="DISPOSE", name="leave"))
    linked = ok(T.call("connect", model="m", source="arrive", target="leave"))
    assert "arrive: CREATE has no outgoing link" in linked["fixed"]
    assert linked["errors"] == 0


def test_a_second_link_between_one_pair_is_addressed_by_its_index(ws):
    """PHASE4 T4 (D77): two DECIDE branches may end at the same block. The
    agent gets the new link's index back, and an unqualified disconnect of an
    ambiguous pair is refused with the candidates instead of guessing."""
    ok(T.call("new_model", name="p"))
    ok(T.call("add_block", model="p", type="CREATE", name="arrive", params={"max_arrivals": 1}))
    ok(T.call("add_block", model="p", type="DECIDE", name="pick",
              params={"mode": "Chance", "probs": [1.0], "conditions": []}))
    ok(T.call("add_block", model="p", type="DISPOSE", name="leave"))
    ok(T.call("connect", model="p", source="arrive", target="pick"))
    assert ok(T.call("connect", model="p", source="pick", target="leave"))["index"] == 0
    assert ok(T.call("connect", model="p", source="pick", target="leave"))["index"] == 1

    refused = T.call("disconnect", model="p", source="pick", target="leave")
    assert refused["error"]["code"] == "E_AMBIGUOUS_LINK"
    assert ok(T.call("disconnect", model="p", source="pick", target="leave", index=1))["errors"] == 0
    assert ok(T.call("validate_model", model="p"))["ok"]


def test_an_agent_repairs_a_broken_model_using_only_the_returned_hints(ws):
    """The deliberate errors: a PROCESS seizing a resource nobody declared
    (#284: now E_RESOURCE_UNKNOWN, not a silent runtime fallback) and no link
    to the DISPOSE. The agent reads the codes, not the prose."""
    ok(T.call("new_model", name="broken"))
    ok(T.call("add_block", model="broken", type="CREATE", name="arrive",
              params={"distribution": "Exponential", "dist_params": {"rate": 1.0},
                      "max_arrivals": 0}))
    ok(T.call("add_block", model="broken", type="PROCESS", name="serve",
              params={"resource_name": "Server", "distribution": "Exponential",
                      "dist_params": {"rate": 2.0}}))
    ok(T.call("add_block", model="broken", type="DISPOSE", name="leave"))
    ok(T.call("connect", model="broken", source="arrive", target="serve"))
    ok(T.call("set_simulation_settings", model="broken", run_length=2000, replications=3))

    report = ok(T.call("validate_model", model="broken"))
    codes = {p["code"]: p for p in report["problems"]}
    assert "E_TERMINAL_NOT_DISPOSE" in codes
    assert "E_RESOURCE_UNKNOWN" in codes
    assert not report["ok"]

    # A run must be refused while the model has errors.
    refused = T.call("run_model", model="broken")
    assert refused["error"]["code"] == "E_INVALID_MODEL"

    # Repair driven by the hint attached to each problem.
    assert "DISPOSE" in codes["E_TERMINAL_NOT_DISPOSE"]["hint"]
    fixed = ok(T.call("connect", model="broken", source="serve", target="leave"))
    assert fixed["errors"] == 1     # E_RESOURCE_UNKNOWN still open
    ok(upsert("broken", "resources", name="Server", capacity=1))
    assert ok(T.call("validate_model", model="broken"))["ok"]
    assert ok(T.call("run_model", model="broken"))["replications"] == 3


def test_an_agent_compares_two_scenarios_with_significance(ws):
    model = build_mm1(capacity=1)
    ok(T.call("set_simulation_settings", model=model, run_length=1200, warmup=200,
              replications=6))
    ok(T.call("upsert_scenario", model=model, name="Second server",
              overrides=[{"path": "resources.Server.capacity", "value": 2}]))
    comparison = ok(T.call("run_scenarios", model=model,
                           kpis=["system/Avg Throughput Time"]))
    assert set(comparison["scenarios"]) == {"Baseline", "Second server"}
    assert comparison["crn"] is True
    rows = {r["scenario"]: r for r in comparison["comparison"]}
    assert rows["Baseline"]["diff"] == pytest.approx(0.0, abs=1e-9)
    # A second server is a large, unambiguous improvement at rho = 0.8.
    assert rows["Second server"]["diff"] < 0
    assert rows["Second server"]["significant"] is True


@pytest.mark.slow
def test_the_analysis_tools_plumb_through_to_experiments_and_optimize(ws):
    """run_to_precision / suggest_warmup / optimize are thin wrappers, but the
    keyword plumbing into experiments.py and optimize.py is exactly what breaks
    silently -- so drive each one once, cheaply."""
    model = build_mm1(service_rate=1.5)
    ok(T.call("set_simulation_settings", model=model, run_length=1500, warmup=100,
              replications=4))

    precision = ok(T.call("run_to_precision", model=model, kpi="system/Avg Throughput Time",
                          relative=0.15, max_replications=20, max_seconds=120))
    assert precision["met"] is True
    assert precision["half_width"] <= precision["target"]

    warmup = ok(T.call("suggest_warmup", model=model, replications=3, max_seconds=120))
    assert warmup["warmup"] >= 0 and isinstance(warmup["reliable"], bool)

    search = ok(T.call("optimize", model=model,
                       factors=[{"path": "resources.Server.capacity", "kind": "integer",
                                 "low": 1, "high": 3}],
                       response="system/Avg Throughput Time", goal="min", budget=12,
                       replications_per_point=3, max_seconds=180))
    # The factor has three points (1, 2, 3 servers) and the only claim is that
    # the plumbing ran and ranked them, so 12 evaluations is four per point --
    # budget 40 spent 28 more on a question already answered (P4-D5).
    assert search["best"]["resources.Server.capacity"] > 1   # one server is the worst option
    assert search["budget_used"] <= 12 and search["history"]

    assert T.call("optimize", model=model, factors=[{"nonsense": 1}],
                  response="system/Avg Throughput Time")["error"]["code"] == "E_BAD_FACTOR"


def test_undo_and_redo_go_through_the_tools(ws):
    ok(T.call("new_model", name="m"))
    ok(T.call("add_block", model="m", type="CREATE", name="arrive"))
    ok(T.call("add_block", model="m", type="DISPOSE", name="leave"))
    assert len(ok(T.call("get_model_summary", model="m"))["blocks"]) == 2

    ok(T.call("undo", model="m"))
    assert [b["name"] for b in ok(T.call("get_model_summary", model="m"))["blocks"]] == ["arrive"]
    redone = ok(T.call("redo", model="m"))
    assert redone["can_redo"] is False
    assert len(ok(T.call("get_model_summary", model="m"))["blocks"]) == 2

    ok(T.call("new_model", name="fresh"))
    assert T.call("undo", model="fresh")["error"]["code"] == "E_NOTHING_TO_UNDO"


def test_open_save_and_round_trip_through_the_workspace(ws):
    opened = ok(T.call("open_model", path="single_server.json", name="base"))
    assert opened["blocks"] == 3 and opened["errors"] == 0
    ok(upsert("base", "resources", name="Server", capacity=2))
    ok(T.call("auto_layout", model="base"))
    saved = ok(T.call("save_model", model="base", path="copy.json"))
    assert Path(saved["path"]).is_file()
    reopened = ok(T.call("open_model", path="copy.json", name="copy"))
    assert reopened["errors"] == 0
    resources = ok(T.call("get_model_summary", model="copy"))["resources"]
    assert resources[0]["capacity"] == 2
    # list_models is named by no other engine test; the exported engine suite has no
    # product test to quote it (test_every_agent_tool_name_is_quoted_in_some_test).
    listing = ok(T.call("list_models"))
    assert [m["name"] for m in listing["open"]] == ["base", "copy"] and "copy.json" in listing["files"]


# -- the contract an agent relies on -----------------------------------------

def test_every_tool_is_documented_for_an_llm_and_has_an_input_schema():
    assert len(T.TOOLS) >= 35
    for name, spec in T.TOOLS.items():
        assert len(spec.description) > 80, f"{name}: docstring too thin for an agent"
        schema = spec.input_schema
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"]), name


def test_tool_names_are_snake_case_verbs():
    verbs = ("list", "describe", "get", "new", "open", "save", "write", "add", "update", "remove",
             "connect", "disconnect", "reorder", "set", "upsert", "auto", "validate", "run",
             "suggest", "optimize", "fit", "export", "undo", "redo",
             "resolve", "calibrate", "forecast", "evaluate", "place", "install", "import",
             "investment",      # investment_case: the name the CLI and spec use
             "debug",           # debug_run, debug_inspect: the names the 7.5 spec uses
             "study",          # the study_* tools
             "find")            # find_tool
    for name in T.TOOLS:
        assert name == name.lower() and " " not in name
        assert name.split("_")[0] in verbs, name


def test_unknown_tools_and_bad_arguments_answer_with_structured_errors(ws):
    assert T.call("no_such_tool")["error"]["code"] == "E_UNKNOWN_TOOL"
    missing = T.call("get_model_summary", model="nope")["error"]
    assert missing["code"] == "E_UNKNOWN_MODEL" and missing["hint"]
    bad_type = T.call("describe_block_type", type_name="NOPE")["error"]
    assert bad_type["code"] == "E_UNKNOWN_BLOCK_TYPE" and "CREATE" in bad_type["hint"]


def test_file_access_is_confined_to_the_workspace_root(ws):
    for path in ("../escape.json", "../../etc/passwd", str(ROOT / "pyproject.toml")):
        refused = T.call("open_model", path=path)
        assert refused["error"]["code"] in ("E_PATH_OUTSIDE_ROOT", "E_FILE_NOT_FOUND"), path
    assert T.call("save_model", model="x", path="../out.json")["error"]["code"] in (
        "E_PATH_OUTSIDE_ROOT", "E_UNKNOWN_MODEL")


def test_no_tool_lets_the_model_grant_itself_access(ws):
    """Permission is the human's, set on the Workspace at server start. A tool
    argument an injected prompt could set would hand it straight back."""
    banned = {"allow_network", "network", "allow_private", "allowed_hosts", "confine_to", "root"}
    for spec in T.tool_schemas():
        assert not banned & set(spec["input_schema"]["properties"]), spec["name"]


def test_data_bindings_under_an_agent_reach_neither_the_network_nor_outside_the_root(ws, tmp_path):
    outside = tmp_path.parent / "outside.csv"
    outside.write_text("svc\n1\n2\n", encoding="utf-8")
    model = build_mm1()
    doc = ws.models[model]
    doc.set_data_sources([{"name": "remote", "kind": "json_url",
                           "location": "https://example.invalid/live.json"},
                          {"name": "escape", "kind": "csv", "location": "../outside.csv"}])
    doc.set_data_bindings([
        {"name": "live", "source": "remote", "column": "svc", "transform": "mean",
         "target": "resources.Server.capacity", "enabled": True},
        {"name": "sneak", "source": "escape", "column": "svc", "transform": "mean",
         "target": "resources.Server.capacity", "enabled": True}])

    answer = ok(T.call("resolve_data_bindings", model=model, apply=True))
    problems = [row["problem"] for row in answer["bindings"]]
    assert answer["problems"] == 2 and all(problems)
    assert "network access is off" in problems[0] and "outside" in problems[1]
    assert ws.models[model].resources[0]["capacity"] == 1        # nothing was applied


def _build_ts_delay_model(model="ts_model"):
    """CREATE (one arrival at t=0) -> DELAY(TS("lead")) -> DISPOSE: one
    deterministic replication, so "Avg Throughput Time" equals the "lead"
    profile's value at t=0 exactly -- the hand-derived KPI for the bind_data
    tests below (D146)."""
    ok(T.call("new_model", name=model))
    ok(T.call("add_block", model=model, type="CREATE", name="arrive",
              params={"distribution": "Constant", "dist_params": {"value": 1.0},
                      "max_arrivals": 1}))
    ok(T.call("add_block", model=model, type="DELAY", name="wait",
              params={"distribution": "Constant", "dist_params": {"value": "TS(\"lead\")"}}))
    ok(T.call("add_block", model=model, type="DISPOSE", name="leave"))
    ok(T.call("connect", model=model, source="arrive", target="wait"))
    ok(T.call("connect", model=model, source="wait", target="leave"))
    ok(upsert(model, "timeseries_inputs", name="lead", times=[0, 1], values=[0.0, 0.0]))
    ok(T.call("set_simulation_settings", model=model, run_length=50, replications=1))
    return model


def test_run_model_bind_data_uses_the_bound_series_profile_at_run_time(ws, tmp_path):
    """D146: `run_model(bind_data=True)` resolves a `series:` binding at run
    time, like the UI/CLI's `--bind`, without writing the CSV's rows into the
    model file. The CSV's first row (t=0, 1.0) becomes the "lead" profile's
    value at t=0, so the deterministic KPI moves from 0.0 to exactly 1.0."""
    model = _build_ts_delay_model()
    source = tmp_path / "lead.csv"
    source.write_text("hour,kw\n0,1.0\n1,4.0\n2,2.0\n", encoding="utf-8")
    doc = ws.models[model]
    doc.set_data_sources([{"name": "meter", "kind": "csv", "location": "lead.csv"}])
    doc.set_data_bindings([{"name": "lead", "source": "meter", "column": "kw",
                            "transform": "series:hour", "target": "timeseries_inputs.lead.values",
                            "enabled": True}])

    unbound = ok(T.call("run_model", model=model, bind_data=False))
    assert unbound["system"]["Avg Throughput Time"]["mean"] == pytest.approx(0.0)
    # The model file itself is untouched -- no bulk CSV rows were written into it.
    assert ws.models[model].timeseries_inputs[0]["values"] == [0.0, 0.0]

    bound = ok(T.call("run_model", model=model, bind_data=True))
    assert bound["system"]["Avg Throughput Time"]["mean"] == pytest.approx(1.0)
    assert ws.models[model].timeseries_inputs[0]["values"] == [0.0, 0.0]   # still untouched


def test_run_model_bind_data_still_refuses_a_network_source(ws):
    """D146: bind_data has no permission argument -- a workspace with no
    network access (the default, netguard.NetworkPolicy()) still refuses a
    json_url source, and the run falls back to the model's own stored value
    instead of raising (same non-fatal-refusal contract as resolve_bindings)."""
    model = _build_ts_delay_model()
    doc = ws.models[model]
    doc.set_data_sources([{"name": "remote", "kind": "json_url",
                           "location": "https://example.invalid/live.json"}])
    doc.set_data_bindings([{"name": "lead", "source": "remote", "column": "kw",
                            "transform": "series:hour", "target": "timeseries_inputs.lead.values",
                            "enabled": True}])

    bound = ok(T.call("run_model", model=model, bind_data=True))
    assert bound["system"]["Avg Throughput Time"]["mean"] == pytest.approx(0.0)


def test_answers_stay_small_on_the_job_shop_model(ws):
    """The biggest bundled model: no tool answer may crowd out an agent's
    context. Frames are paged, never returned whole."""
    ok(T.call("open_model", path="job_shop_jackson.json", name="shop"))
    ok(T.call("set_simulation_settings", model="shop", run_length=700, warmup=100,
              replications=3))
    answers = {
        "summary": T.call("get_model_summary", model="shop"),
        "json": T.call("get_model_json", model="shop"),
        "validate": T.call("validate_model", model="shop"),
        "blocks": T.call("list_block_types"),
        "distributions": T.call("list_distributions"),
        "expressions": T.call("get_expression_help"),
        "run": T.call("run_model", model="shop"),
        "log": T.call("get_kpi_table", model="shop", frame="entity_log", limit=200),
    }
    for name, answer in answers.items():
        size = len(json.dumps(ok(answer)))
        assert size < SIZE_CAP, f"{name} answered with {size} bytes"
    assert answers["log"]["has_more"] is True      # paged, not truncated silently


def test_listing_summaries_are_full_sentences():
    # An assistant picks a block or distribution from these one-liners alone.
    for b in ok(T.call("list_block_types"))["block_types"]:
        assert len(b["summary"]) >= 40 and b["summary"].endswith("."), b
    for d in ok(T.call("list_distributions"))["distributions"]:
        assert d["summary"].strip(), d


def test_reference_tools_describe_the_live_registries():
    types = {b["type"] for b in ok(T.call("list_block_types"))["block_types"]}
    assert {"CREATE", "PROCESS", "DISPOSE", "DECIDE"} <= types
    decide = ok(T.call("describe_block_type", type_name="DECIDE"))
    assert decide["multi_output"] is True
    assert {p["name"] for p in decide["params"]} >= {"mode", "probs", "conditions"}

    arrival = {d["name"] for d in ok(T.call("list_distributions", block_type="CREATE"))
               ["distributions"]}
    assert "Exponential" in arrival
    assert ok(T.call("describe_distribution", name="Exponential"))["params"][0]["name"] == "rate"
    assert "NQ" in {f["name"] for f in ok(T.call("get_expression_help"))["functions"]}


def test_fit_distribution_accepts_inline_values_and_returns_a_pasteable_spec(ws):
    import numpy as np

    sample = np.random.default_rng(0).exponential(2.0, 400).tolist()
    result = ok(T.call("fit_distribution", values=sample, top=3))
    assert result["n"] == 400 and len(result["fits"]) == 3
    best = result["fits"][0]
    assert set(best["spec"]) == {"distribution", "dist_params"}
    assert best["name"] in {d["name"] for d in ok(T.call("list_distributions"))["distributions"]}
    assert T.call("fit_distribution")["error"]["code"] == "E_BAD_INPUT"


def test_export_writes_through_the_exporter_registry(ws):
    model = build_mm1("export_me")
    ok(T.call("set_simulation_settings", model=model, run_length=500, warmup=0,
              replications=2))
    ok(T.call("run_model", model=model))
    written = ok(T.call("export", model=model, format="json", path="results.json"))
    assert json.loads(Path(written["path"]).read_text(encoding="utf-8"))["system"]
    assert T.call("export", model=model, format="nope", path="x")["error"]["code"] \
        == "E_UNKNOWN_FORMAT"


# -- lookup tables (PHASE4 A1) ----------------------------------------------

def test_an_agent_builds_a_lookup_table_reads_it_back_and_imports_one_from_csv(ws, tmp_path):
    model = build_mm1()
    ok(upsert(model, "tables", name="ProcessTime",
              keys=["WIDGET", "GADGET"], columns=["Cut", "Pack"], values=[[3, 2], [4, 6]]))
    assert records(model, "tables") == [{"name": "ProcessTime", "keys": ["WIDGET", "GADGET"],
                                         "columns": ["Cut", "Pack"], "values": [[3, 2], [4, 6]]}]

    # an expression may now name it; a wrong key count comes back coded
    result = ok(T.call("update_block", model=model, name="serve",
                       params={"distribution": "Constant",
                               "dist_params": {"value": "TABLE('ProcessTime', entity.e_type)"}}))
    assert {p["code"] for p in result["introduced"]} == {"E_TABLE_KEYS"}

    csv_path = tmp_path / "times.csv"
    csv_path.write_text(",Cut,Pack\nWIDGET,5,2\nGADGET,4,6\n", encoding="utf-8")
    ok(T.call("upsert_table_from_csv", model=model, name="ProcessTime", path="times.csv"))
    assert ws.models[model].tables[0]["values"] == [[5.0, 2.0], [4.0, 6.0]]

    ok(remove(model, "tables", "ProcessTime"))
    assert records(model, "tables") == []


def test_importing_a_table_from_outside_the_workspace_root_is_refused(ws, tmp_path):
    """Confinement is the workspace's, not the tool's: there is no argument
    that widens it (agent_tools rule 5)."""
    outside = tmp_path.parent / "outside.csv"
    outside.write_text("key,value\nA,1\n", encoding="utf-8")
    model = build_mm1()
    refused = T.call("upsert_table_from_csv", model=model, name="T", path="../outside.csv")
    assert refused["error"]["code"] in ("E_PATH_OUTSIDE_ROOT", "E_FILE_NOT_FOUND")
    assert ws.models[model].tables == []


# -- routings (PHASE4 A2) ----------------------------------------------------

def test_an_agent_builds_a_routing_and_reads_back_its_stations(ws):
    model = build_mm1()
    for station in ("Mill", "Drill"):
        ok(T.call("add_block", model=model, type="STATION", name=station))

    ok(upsert(model, "sequences", name="Widget",
              steps=[{"station": "Mill", "values": {"process_time": 3}},
                     {"station": "Drill"}]))
    listed = ok(T.call("list_sequences", model=model))
    assert listed["stations"] == ["Drill", "Mill"]
    assert listed["sequences"] == [{"name": "Widget", "steps": [
        {"station": "Mill", "values": {"process_time": 3}}, {"station": "Drill"}]}]

    # a CREATE naming a sequence the model does not declare comes back coded
    result = ok(T.call("update_block", model=model, name="arrive",
                       params={"sequence": "Gadget"}))
    assert {p["code"] for p in result["introduced"]} == {"E_SEQUENCE_UNKNOWN"}

    ok(remove(model, "sequences", "Widget"))
    assert ok(T.call("list_sequences", model=model))["sequences"] == []


# -- lump-sum and fixed costs (PHASE4 D5) ------------------------------------

def test_an_agent_adds_a_fixed_cost_and_reads_it_back_as_a_kpi(ws):
    """The capex an optimizer trades against operating cost is an ordinary
    section edit, and `cost/<item>/Amount` an ordinary KPI key."""
    model = build_mm1()
    ok(upsert(model, "variables", name="servers_bought", initial=3))
    ok(upsert(model, "fixed_costs", name="Machine capex",
              amount="servers_bought * 12000", category="Capital"))

    assert records(model, "fixed_costs") == [{"name": "Machine capex", "category": "Capital",
                                              "amount": "servers_bought * 12000"}]

    digest = ok(T.call("run_model", model=model, replications=1, run_length=50))
    assert digest["system"]["Total Cost"]["mean"] == 36000.0

    ok(remove(model, "fixed_costs", "Machine capex"))
    assert records(model, "fixed_costs") == []


# -- orders and due dates (PHASE4 A3) ----------------------------------------

def test_an_agent_releases_orders_and_imports_an_order_book_from_csv(ws, tmp_path):
    model = build_mm1()
    ok(upsert(model, "orders", id="WO-1", release=0.0, due=480.0, quantity=2))
    assert ok(T.call("list_orders", model=model))["releases"] == []   # nothing releases them yet
    problems = {p["code"] for p in ok(T.call("validate_model", model=model))["problems"]}
    assert "W_ORDERS_NOT_CONSUMED" in problems

    ok(T.call("update_block", model=model, name="arrive", params={"arrival_mode": "Orders"}))
    listed = ok(T.call("list_orders", model=model))
    assert listed["releases"] == ["arrive"]
    assert listed["orders"] == [{"id": "WO-1", "release": 0.0, "quantity": 2, "due": 480.0}]

    csv_path = tmp_path / "book.csv"
    csv_path.write_text("id,release,due,quantity,customer\nWO-7,0,100,1,ACME\n",
                        encoding="utf-8")
    ok(T.call("upsert_orders_from_csv", model=model, path="book.csv"))
    assert [o["id"] for o in ws.models[model].orders] == ["WO-7"]
    assert ws.models[model].orders[0]["attributes"] == {"customer": "ACME"}

    ok(remove(model, "orders", "WO-7"))
    assert ok(T.call("list_orders", model=model))["orders"] == []


# -- tools with no other test coverage (O8 smoke) ----------------------------
# Every other tool is exercised somewhere above or in test_regressions.py /
# test_network.py; these ~17 were not. One cheap call each, through the same
# T.call path an agent uses, checking the "ok" result and the document change
# it made (either in the result itself or read back through the tool surface).

def test_list_continuous_and_conversions_and_remove_each_record(ws):
    model = "m"
    ok(T.call("new_model", name=model))
    ok(upsert(model, "levels", name="Tank", capacity=100.0))
    ok(upsert(model, "flows", name="Fill", rate="1", to="Tank"))
    ok(upsert(model, "timeseries_inputs", name="Load", values=[1.0, 2.0], step=1.0))
    ok(upsert(model, "conversions", name="Digest",
              substrates=[{"name": "feed", "feed": "1.0", "yield": 200.0, "half_life": 60}]))

    continuous = ok(T.call("list_continuous", model=model))
    assert {lvl["name"] for lvl in continuous["levels"]} == {"Tank"}
    assert {flow["name"] for flow in continuous["flows"]} == {"Fill"}
    assert {s["name"] for s in continuous["timeseries_inputs"]} == {"Load"}
    assert [c["name"] for c in records(model, "conversions")] == ["Digest"]

    assert ok(remove(model, "flows", "Fill"))["removed"] == "Fill"
    assert ok(remove(model, "levels", "Tank"))["removed"] == "Tank"
    assert ok(remove(model, "timeseries_inputs", "Load"))["removed"] == "Load"
    assert ok(remove(model, "conversions", "Digest"))["removed"] == "Digest"

    after = ok(T.call("list_continuous", model=model))
    assert after["levels"] == after["flows"] == after["timeseries_inputs"] == []
    assert records(model, "conversions") == []


def test_upsert_timeseries_input_from_csv_and_remove_fleet(ws, tmp_path):
    model = "m"
    ok(T.call("new_model", name=model))

    csv_path = tmp_path / "load.csv"
    csv_path.write_text("time,value\n0,1.0\n1,4.0\n", encoding="utf-8")
    added = ok(T.call("upsert_timeseries_input_from_csv", model=model, name="Load",
                      path="load.csv"))
    assert added["series"] == "Load" and added["points"] == 2
    assert ws.models[model].timeseries_inputs[0]["values"] == [1.0, 4.0]

    ok(upsert(model, "fleets", name="Forklifts", count=2, speed=5.0))
    assert records(model, "fleets")[0]["name"] == "Forklifts"
    assert ok(remove(model, "fleets", "Forklifts"))["removed"] == "Forklifts"
    assert records(model, "fleets") == []


def test_upsert_resource_and_fleet_accept_power_and_go_through_model_validation(ws):
    """ARCHITECTURE D112/D114/D116/D118 (PHASE6 W3): resource and fleet
    records carry the per-state power and worker/loads/gantry fields, and a
    bad value is caught by the same schema validation as every other field --
    no bespoke checking in the tool itself."""
    model = "m"
    ok(T.call("new_model", name=model))
    ok(upsert(model, "resources", name="Server", capacity=1, power={"busy": 5.0, "idle": 1.0}))
    assert ws.models[model].resources[0]["power"] == {"busy": 5.0, "idle": 1.0}

    bad_power = upsert(model, "resources", name="Server", capacity=1, power={"nope": 1.0})
    assert ok(bad_power)["introduced"][0]

    ok(T.call("upsert_node", model=model, name="dock", x=0.0, y=0.0))
    ok(T.call("upsert_node", model=model, name="station", x=10.0, y=0.0))
    ok(T.call("upsert_segment", model=model, source="dock", target="station"))

    ok(upsert(model, "fleets", name="Ann", count=1, speed=2.0, home="dock",
              kind="worker", units=[{"name": "Ann", "skills": ["weld"]}], loads=1))
    fleet = records(model, "fleets")[0]
    assert fleet["kind"] == "worker" and fleet["units"][0]["skills"] == ["weld"]

    ok(upsert(model, "fleets", name="Crane", count=1, speed=2.0, speed_empty=2.0,
              home="dock", motion="gantry", speed_x=3.0, speed_y=3.0, hoist_time=1.5,
              power={"busy": 4.0}))
    crane = records(model, "fleets")[1]
    assert crane["motion"] == "gantry" and crane["speed_x"] == 3.0 and crane["power"]["busy"] == 4.0

    # loads > 1 is invalid on a worker fleet (E_FLEET_LOADS) -- caught by the
    # existing fleet validation, not by the tool.
    broken = ok(upsert(model, "fleets", name="Ann", count=1, speed=2.0, home="dock",
                       kind="worker", units=[{"name": "Ann"}], loads=2))
    assert any(p["code"] == "E_FLEET_LOADS" for p in broken["introduced"])


def test_reorder_branches_changes_the_documents_link_order(ws):
    model = "m"
    ok(T.call("new_model", name=model))
    ok(T.call("add_block", model=model, type="CREATE", name="arrive"))
    ok(T.call("add_block", model=model, type="DECIDE", name="branch"))
    ok(T.call("add_block", model=model, type="DISPOSE", name="a"))
    ok(T.call("add_block", model=model, type="DISPOSE", name="b"))
    ok(T.call("connect", model=model, source="arrive", target="branch"))
    ok(T.call("connect", model=model, source="branch", target="a"))
    ok(T.call("connect", model=model, source="branch", target="b"))
    assert ws.models[model].out_links("branch") == ["a", "b"]

    result = ok(T.call("reorder_branches", model=model, source="branch", order=["b", "a"]))
    assert result["order"] == ["b", "a"]
    assert ws.models[model].out_links("branch") == ["b", "a"]


def test_set_calendar_resource_set_schedule_and_failure(ws):
    model = "m"
    ok(T.call("new_model", name=model))
    calendar = ok(T.call("set_calendar", model=model, start="2026-01-05T06:00:00",
                         timezone="UTC"))
    assert calendar["calendar"]["start"] == "2026-01-05T06:00:00"
    assert calendar["calendar"]["timezone"] == "UTC"

    ok(upsert(model, "resources", name="Op1", capacity=1))
    ok(upsert(model, "resources", name="Op2", capacity=1))
    ok(upsert(model, "resource_sets", name="Team", members=["Op1", "Op2"], rule="Random"))
    assert ws.models[model].resource_sets == [
        {"name": "Team", "members": ["Op1", "Op2"], "rule": "Random"}]

    ok(upsert(model, "schedules", name="Shift", kind="capacity",
              points=[[8.0, 1], [16.0, 0]], repeat=True))
    assert ws.models[model].schedules[0]["name"] == "Shift"

    ok(upsert(model, "failures", name="Break", resources=["Op1"],
              uptime={"distribution": "Constant", "dist_params": {"value": 100.0}},
              downtime={"distribution": "Constant", "dist_params": {"value": 10.0}}))
    assert ws.models[model].failures[0]["name"] == "Break"


# -- generic record tools (D147) ---------------------------------------------

def test_record_tools_cover_every_record_section_and_refuse_the_rest(ws):
    """One surface for every keyed record section: an unknown section, a
    record without its key and a missing key are coded refusals naming the
    valid choices; list_records' description names every section."""
    ok(T.call("new_model", name="m"))
    description = T.TOOLS["list_records"].description
    for section in T.RECORD_TOOL_SECTIONS:
        assert section in description, section
        assert ok(T.call("get_record_schema", section=section))["schema"]["type"] == "object"
        assert records("m", section) == []

    bad = T.call("list_records", model="m", section="blocks")["error"]
    assert bad["code"] == "E_UNKNOWN_SECTION" and "resources" in bad["hint"]
    assert upsert("m", "resources", capacity=1)["error"]["code"] == "E_BAD_RECORD"
    ok(upsert("m", "resources", name="A", capacity=1))
    missing = remove("m", "resources", "B")["error"]
    assert missing["code"] == "E_UNKNOWN_RECORD" and "A" in missing["hint"]
    assert ok(remove("m", "resources", "A"))["removed"] == "A"


def test_set_entity_types_replaces_the_entity_table(ws):
    model = "m"
    ok(T.call("new_model", name=model))
    result = ok(T.call("set_entity_types", model=model,
                       entities=[{"name": "PALLET", "holding_cost": 0.1}]))
    assert result["entities"] == ["PALLET"]
    assert ws.models[model].entities[0]["name"] == "PALLET"

    # REPLACE, not merge: a second call with a different list drops the first.
    ok(T.call("set_entity_types", model=model, entities=[{"name": "TOTE"}]))
    assert [e["name"] for e in ws.models[model].entities] == ["TOTE"]


def test_evaluate_policy_runs_the_built_in_controllers(ws):
    model = "m"
    ok(T.call("new_model", name=model))
    ok(T.call("add_block", model=model, type="CREATE", name="arrive",
              params={"distribution": "Constant", "dist_params": {"value": 2.0},
                      "max_arrivals": 3}))
    ok(T.call("add_block", model=model, type="DECIDE", name="branch",
              params={"mode": "Policy", "policy": "routing", "fallback": "first"}))
    ok(T.call("add_block", model=model, type="DISPOSE", name="a"))
    ok(T.call("add_block", model=model, type="DISPOSE", name="b"))
    ok(T.call("connect", model=model, source="arrive", target="branch"))
    ok(T.call("connect", model=model, source="branch", target="a"))
    ok(T.call("connect", model=model, source="branch", target="b"))

    result = ok(T.call("evaluate_policy", model=model, policy="routing", controller="random",
                       episodes=2, horizon=20.0, seed=0))
    assert result["episodes"] == 2
    assert len(result["returns"]) == 2


def test_set_resources_replaces_the_table_and_remove_blocks_drops_them(ws):
    model = build_mm1()
    replaced = ok(T.call("set_resources", model=model,
                         resources=[{"name": "Server", "capacity": 2}]))
    assert replaced["resources"] == ["Server"]
    assert ws.models[model].resources == [{"name": "Server", "capacity": 2}]

    removed = ok(T.call("remove_blocks", model=model, names=["serve", "does_not_exist"]))
    assert removed["removed"] == ["serve"]
    assert "serve" not in ws.models[model].blocks


def test_fit_surrogate_fits_a_response_surface_over_a_tiny_design(ws):
    model = build_mm1()
    ok(T.call("set_simulation_settings", model=model, run_length=200, warmup=0,
              replications=1))

    result = ok(T.call("fit_surrogate", model=model,
                       factors=[{"path": "resources.Server.capacity", "kind": "integer",
                                 "low": 1, "high": 2}],
                       response="system/Avg Throughput Time", design="full:2", kind="linear",
                       replications=2, max_seconds=60))
    assert result["n_points"] == 2
    assert result["kind"] == "linear"
    assert {c["term"] for c in result["coefficients"]} >= {"resources.Server.capacity"}


def test_import_xmile_reads_the_workspace_fixture_into_a_new_model(ws, tmp_path):
    shutil.copy(ROOT / "tests" / "data" / "sd_logistic.xmile", tmp_path / "sd_logistic.xmile")
    result = ok(T.call("import_xmile", path="sd_logistic.xmile"))
    assert result["model"] == "sd_logistic"
    assert result["import_problems"] == []
    assert result["errors"] == 0
    run = ok(T.call("run_model", model="sd_logistic", replications=1))
    assert run["system"]


def test_import_xmile_refuses_a_path_outside_the_workspace(ws, tmp_path):
    outside = tmp_path.parent / "outside.xmile"
    outside.write_text("<xmile><model><variables/></model></xmile>", encoding="utf-8")
    result = T.call("import_xmile", path="../outside.xmile")
    assert "error" in result


def test_every_agent_tool_name_is_quoted_in_some_test():
    """A guard against the next tool joining TOOLS with no test at all (O8):
    every name is a distinct dict key, so a plain substring search for its
    quoted form is cheap and not brittle -- it is exactly how every T.call
    site above names its tool."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in Path(__file__).parent.glob("test_*.py"))
    untested = sorted(name for name in T.TOOLS if f'"{name}"' not in text)
    assert not untested


# -- investment_case (automation library, Task 5) ------------------------------

def _investment_model(tmp_path, **section):
    """A 1-min line; the "Robot" scenario doubles the speed for 50 000 of capex."""
    from conftest import chain, create, dispose, layout, process
    blocks = [create(dist_params={"value": 2.0}), process("process_1", "Machine", 1.0), dispose()]
    doc = layout(
        blocks, chain(*blocks), resources=[{"name": "Machine", "capacity": 1}],
        fixed_costs=[{"name": "Robot cell", "category": "Capital", "amount": 0, "capex": True}],
        scenarios=[{"name": "Robot", "overrides": [
            {"path": "fixed_costs.Robot cell.amount", "value": 50_000},
            {"path": "blocks.create_1.params.dist_params.value", "value": 1.0}]}],
        **section)
    doc["simulation"].update(time_limit=300.5, time_unit="minutes")
    (tmp_path / "inv.json").write_text(json.dumps(doc), encoding="utf-8")
    return ok(T.call("open_model", path="inv.json", name="inv"))["model"]


def test_investment_case_returns_the_table_and_settings(ws, tmp_path):
    from nagasim.investment import COLUMNS
    model = _investment_model(tmp_path, investment={"horizon_years": 3, "margin_per_part": 50.0})
    out = ok(T.call("investment_case", model=model, replications=2))
    assert out["settings"]["horizon_years"] == 3
    assert [r["Scenario"] for r in out["table"]] == ["Baseline", "Robot"]
    assert all(list(r) == COLUMNS for r in out["table"])
    robot = out["table"][1]
    assert robot["Capex"] == 50_000 and robot["Pays Back"] is True
    json.dumps(out, allow_nan=False)        # NaN became null


def test_investment_case_without_the_section_is_a_structured_error(ws, tmp_path):
    model = _investment_model(tmp_path)
    err = T.call("investment_case", model=model)["error"]
    assert err["code"] == "E_NO_INVESTMENT" and err["hint"]
    bad = _investment_model(tmp_path, investment={"baseline": "Nope"})
    assert T.call("investment_case", model=bad, replications=1)["error"]["code"] == "E_INVALID_MODEL"


def test_investment_case_takes_no_path_or_permission_argument():
    spec = next(t for t in T.tool_schemas() if t["name"] == "investment_case")
    assert set(spec["input_schema"]["properties"]) == {"model", "replications", "seed"}
    assert spec["input_schema"]["required"] == ["model"]


def test_run_tools_refuse_a_python_model_with_a_code_addressed_to_the_person(ws, monkeypatch):
    from nagasim.core import agents as A
    from test_agents_python import _ring
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    T.drop_python_consent()
    (ws.root / "py.json").write_text(json.dumps(_ring(python=True)), encoding="utf-8")
    assert "error" not in T.call("open_model", path="py.json", name="py")
    for name, extra in (("run_model", {"replications": 2}), ("suggest_warmup", {}),
                        ("debug_run", {})):
        out = T.call(name, model="py", **extra)
        assert out["error"]["code"] == "E_AGENT_PYTHON_REFUSED", name
        assert "naga run" in out["error"]["hint"]
    assert "py" not in ws.debug             # a refused start starts nothing
    assert T.call("validate_model", model="py")["model"] == "py"   # checked, not run
    ok(T.call("set_simulation_settings", model="py", run_length=4))   # editing stays allowed


# -- run debugger (PHASE7 7.5, D194) -------------------------------------------

def _debugged(name="single_server"):
    return ok(T.call("open_model", path=f"{name}.json"))["model"]


def _plain_frames(ws, model, seed=0):
    from nagasim.core.session import SimSession
    return SimSession(ws.models[model].to_layout(), seed=seed).finish()


def test_debug_run_stops_at_a_block_and_debug_inspect_reads_it(ws):
    model = _debugged()
    out = ok(T.call("debug_run", model=model, stops=[{"block": "process_1"}],
                    watches=['NQ("Server")', "TNOW"]))
    assert out["state"] == "stop" and out["model_edited"] is False and out["pending"] == []
    assert out["pending_total"] == 0
    assert out["hits"] == [{"stop": 0, "block": "process_1", "entity_id": 1,
                            "entity_type": "DEFAULT"}]
    assert out["watches"] == {'NQ("Server")': 0, "TNOW": out["t"]}
    seen = ok(T.call("debug_inspect", model=model, block="process_1", entity=1))
    assert seen["t"] == out["t"] and seen["counts"] == {"process_1": 1}
    assert seen["resources"]["Server"]["capacity"] == 1 and seen["levels"] == {}
    assert seen["block"]["name"] == "process_1" and seen["block"]["entities"] == [1]
    assert seen["entity"]["id"] == 1 and seen["entity"]["attributes"]["cost"] == 0.0
    assert ok(T.call("debug_inspect", model=model, entity=99))["entity"] is None
    json.dumps(seen, allow_nan=False)
    assert len(json.dumps(seen)) < SIZE_CAP


def test_debug_run_steps_runs_to_a_time_and_stays_put_at_the_end(ws):
    model = _debugged()
    first = ok(T.call("debug_run", model=model, step=True))
    second = ok(T.call("debug_run", model=model, step=True))
    assert first["state"] == second["state"] == "step" and second["t"] >= first["t"]
    timed = ok(T.call("debug_run", model=model, to=50))
    assert (timed["state"], timed["t"]) == ("time", 50.0)
    end = ok(T.call("debug_run", model=model))
    assert (end["state"], end["t"]) == ("horizon", 1000.0)
    # Review focus: called again when the run is over, it says so and nothing moves.
    for extra in ({"step": True}, {"to": 2000}, {}):
        again = ok(T.call("debug_run", model=model, **extra))
        assert (again["state"], again["t"]) == ("horizon", 1000.0)
    assert ok(T.call("debug_inspect", model=model))["t"] == 1000.0


def test_a_run_model_between_two_debug_runs_changes_neither(ws):
    """D195 through the tools: the debugged run ends as an uninterrupted one."""
    from test_debugger import _frames_equal
    model = _debugged()
    expected = _plain_frames(ws, model, seed=3)
    assert ok(T.call("debug_run", model=model, seed=3, to=200,
                     stops=[{"block": "dispose_1"}]))["state"] == "stop"
    plain = ok(T.call("run_model", model=model, replications=2, run_length=300))
    ok(T.call("debug_run", model=model, to=600, stops=[]))
    assert ok(T.call("run_model", model=model, replications=2, run_length=300)) == plain
    assert ok(T.call("debug_run", model=model))["state"] == "horizon"
    _frames_equal(ws.debug[model]["session"].finish(), expected)


def test_debug_run_back_returns_exactly_where_the_previous_call_was(ws):
    """D598: back rebuilds the run by replay; the run then ends as an uninterrupted one."""
    from test_debugger import _frames_equal
    model = _debugged()
    expected = _plain_frames(ws, model)
    assert T.call("debug_run", model=model, back=True)["error"]["code"] == "E_NO_DEBUG_RUN"
    first = ok(T.call("debug_run", model=model, to=100, watches=['NQ("Server")']))
    second = ok(T.call("debug_run", model=model, to=300))
    back = ok(T.call("debug_run", model=model, back=True))
    assert back["state"] == "back" and back["t"] == first["t"] == 100.0
    assert back["watches"] == first["watches"]
    assert ok(T.call("debug_run", model=model, to=300))["watches"] == second["watches"]
    assert ok(T.call("debug_run", model=model, back=True))["t"] == 100.0
    assert ok(T.call("debug_run", model=model, back=True))["t"] == 0.0
    assert T.call("debug_run", model=model, back=True)["error"]["code"] == "E_BAD_ARGUMENT"
    assert T.call("debug_run", model=model, back=True, step=True)["error"]["code"] == \
        "E_BAD_ARGUMENT"
    assert ok(T.call("debug_run", model=model))["state"] == "horizon"
    _frames_equal(ws.debug[model]["session"].finish(), expected)


def test_620_back_hands_its_replay_over_in_chunks_and_keeps_to_its_budget(ws, monkeypatch):
    """DEVLOG #620: back held the whole replay in one list and ignored max_seconds."""
    from test_debugger import _frames_equal
    import nagasim.agent_tools.debug as debug_tools
    model = _debugged()
    expected = _plain_frames(ws, model)
    ok(T.call("debug_run", model=model, to=400))
    ok(T.call("debug_run", model=model, to=800))
    sizes = []
    real_append = debug_tools.Timeline.append
    monkeypatch.setattr(debug_tools, "FLUSH_EVENTS", 50)
    monkeypatch.setattr(debug_tools.Timeline, "append",
                        lambda self, events: sizes.append(len(events)) or real_append(self, events))
    with monkeypatch.context() as patched:
        patched.setattr(debug_tools, "_budget", lambda _seconds: lambda: True)
        refused = T.call("debug_run", model=model, back=True)
    assert refused["error"]["code"] == "E_BUDGET"
    assert ok(T.call("debug_inspect", model=model))["t"] == 800.0      # the run is as it was
    sizes.clear()
    assert ok(T.call("debug_run", model=model, back=True))["t"] == 400.0
    assert len(sizes) > 2 and max(sizes) <= 51
    assert ok(T.call("debug_run", model=model))["state"] == "horizon"
    _frames_equal(ws.debug[model]["session"].finish(), expected)


def test_621_back_returns_to_a_decision_answered_by_a_call_that_did_not_move(ws):
    """DEVLOG #621: the saved points merged on (step, t) alone, so back after a call
    that only answered a decision skipped to the call before it."""
    model = _debugged("parallel_routing_policy")
    first = ok(T.call("debug_run", model=model))
    assert first["state"] == "decision"
    request = first["pending"][0]
    answered = ok(T.call("debug_run", model=model, to=first["t"], decisions=[
        {"policy": request["policy"], "option": 0, "request_id": request["id"]}]))
    assert answered["t"] == first["t"]
    back = ok(T.call("debug_run", model=model, back=True))
    assert back["t"] == first["t"] and back["pending"][0]["id"] == request["id"]
    assert ws.debug[model]["session"].game_log == []


def test_619_a_scoped_step_reads_the_events_that_trigger_a_flush(ws, monkeypatch):
    """DEVLOG #619: the tool flushed its recorder before reading the newest events, so
    an event inside the scope that crossed the flush size was never checked."""
    from nagasim.core.session import SimSession
    from nagasim.core.trace import TraceRecorder
    monkeypatch.setattr("nagasim.agent_tools.debug.FLUSH_EVENTS", 0)    # every event flushes
    model = _debugged("two_cell_line")
    trace = TraceRecorder()
    s = SimSession(ws.models[model].to_layout(), seed=0, trace=trace)
    s.advance(until=lambda: any((e.block or "").startswith("cell_2/")
                                or (e.target or "").startswith("cell_2/") for e in trace.events))
    out = ok(T.call("debug_run", model=model, step=True, scope="cell_2"))
    assert out["t"] == s.now


def test_debug_run_out_of_budget_returns_and_the_run_goes_on_unchanged(ws, monkeypatch):
    from test_debugger import _frames_equal
    model = _debugged()
    expected = _plain_frames(ws, model)
    asked = []

    def short_budget(_seconds):
        return lambda: asked.append(1) or len(asked) > 500

    with monkeypatch.context() as patched:      # the suite's own guards stay armed afterwards
        patched.setattr("nagasim.agent_tools.debug._budget", short_budget)
        out = ok(T.call("debug_run", model=model))
    assert out["state"] == "budget" and 0 < out["t"] < 1000
    assert ok(T.call("debug_run", model=model))["state"] == "horizon"
    _frames_equal(ws.debug[model]["session"].finish(), expected)


def test_debug_run_waits_for_a_decision_and_takes_the_answer(ws):
    """Review focus: while a decision waits, every call answers "decision", the clock still."""
    model = _debugged("parallel_routing_policy")
    out = ok(T.call("debug_run", model=model))
    assert out["state"] == "decision"
    request = out["pending"][0]
    assert request["policy"] == "routing" and request["options"] == ["process_a", "process_b"]
    held = ok(T.call("debug_run", model=model, to=out["t"] + 50, step=True))
    assert (held["state"], held["t"], held["pending"]) == ("decision", out["t"], out["pending"])
    refused = T.call("debug_run", model=model, decisions=[{"policy": "nope", "option": 0}])
    assert refused["error"]["code"] == "E_VALUEERROR"
    moved = ok(T.call("debug_run", model=model, stops=[{"block": "process_b"}],
                      decisions=[{"policy": "routing", "option": 1, "request_id": request["id"]}]))
    assert moved["state"] == "stop" and moved["hits"][0]["entity_id"] == request["entity_id"]


def test_a_refused_decision_keeps_the_stops_and_watches_but_not_the_decisions_before_it(ws):
    """The call's order: new stops are built, decisions applied, and only then installed."""
    model = _debugged("parallel_routing_policy")
    ok(T.call("debug_run", model=model, stops=[{"block": "dispose_1"}], watches=["TNOW"]))
    run = ws.debug[model]
    armed, bus = run["stops"], run["session"].ctx.bus
    listeners = list(bus._subscribers)
    refused = T.call("debug_run", model=model, stops=[{"block": "process_b"}],
                     watches=['NQ("ServerA")'],
                     decisions=[{"policy": "routing", "option": 1}, {"policy": "nope", "option": 0}])
    assert refused["error"]["code"] == "E_VALUEERROR"
    assert ws.debug[model] is run and run["stops"] is armed
    assert (run["stop_specs"], run["watches"]) == ([{"block": "dispose_1"}], ["TNOW"])
    assert list(bus._subscribers) == listeners          # the stops it built are off the bus again
    assert ok(T.call("debug_inspect", model=model))["pending_total"] == 0   # the first one stayed
    assert list(ok(T.call("debug_run", model=model, step=True))["watches"]) == ["TNOW"]


def test_the_same_stops_sent_again_keep_the_armed_ones_and_the_flip_they_have_not_read(
        ws, monkeypatch):
    """An agent that re-sends its stop list at every call must not lose a flip: arming takes
    each expression stop's baseline anew, and a stop is read only before an event (after the
    budget check, and not at all while a decision waits), so the flip made by the last event
    of a call that returned "budget" is still unread."""
    model = _debugged()
    stops = [{"expr": "TNOW > 0"}]      # false at t = 0, true once the first event past it is done

    def over_once_the_clock_moves(_seconds):
        return lambda: ws.debug[model]["session"].now > 0

    with monkeypatch.context() as patched:
        patched.setattr("nagasim.agent_tools.debug._budget", over_once_the_clock_moves)
        out = ok(T.call("debug_run", model=model, stops=stops))
    assert (out["state"], out["hits"]) == ("budget", []) and out["t"] > 0   # true, not read yet
    armed = ws.debug[model]["stops"]
    again = ok(T.call("debug_run", model=model, stops=[dict(stop) for stop in stops]))
    assert again["state"] == "stop" and again["hits"] == [{"stop": 0, "expr": "TNOW > 0"}]
    assert ws.debug[model]["stops"] is armed                        # not rebuilt
    ok(T.call("debug_run", model=model, stops=[{"expr": "TNOW > 1"}, {"block": "dispose_1"}]))
    assert ws.debug[model]["stops"] is not armed                    # a changed list is armed anew


def test_debug_tools_refuse_with_a_code_and_keep_what_was_armed(ws):
    model = _debugged()
    assert T.call("debug_inspect", model=model)["error"]["code"] == "E_NO_DEBUG_RUN"
    assert T.call("debug_run", model="nope")["error"]["code"] == "E_UNKNOWN_MODEL"
    for stops in ([{"block": "nope"}], [{"expr": "RAND() > 0.5"}], [{"expr": "1 +"}],
                  ["process_1"], [{"python": "import os"}]):
        error = T.call("debug_run", model=model, stops=stops)["error"]
        assert error["code"] == "E_BAD_STOP" and "process_1" in error["hint"], stops
    assert model not in ws.debug                    # a refused start starts nothing
    ok(T.call("debug_run", model=model, to=5, stops=[{"block": "dispose_1"}]))
    refused = T.call("debug_run", model=model, stops=[{"block": "nope"}])
    assert refused["error"]["code"] == "E_BAD_STOP"
    assert ok(T.call("debug_run", model=model))["hits"][0]["block"] == "dispose_1"
    assert T.call("debug_inspect", model=model, block="nope")["error"]["code"] == "E_UNKNOWN_BLOCK"


def test_a_stop_that_cannot_be_read_shows_its_error_among_the_watches(ws):
    """Review focus: a misspelt name never fires, so the answer must say why."""
    model = _debugged()
    out = ok(T.call("debug_run", model=model, to=5, stops=[{"expr": 'NQ("Servr") > 3'}]))
    assert (out["state"], out["hits"]) == ("time", [])
    assert out["watches"] == {'NQ("Servr") > 3': "error: unknown resource 'Servr'"}


def test_debug_run_says_the_model_was_edited_and_restart_debugs_the_new_one(ws):
    model = _debugged()
    ok(T.call("debug_run", model=model, to=10, stops=[{"block": "dispose_1"}], watches=["TNOW"]))
    ok(upsert(model, "variables", name="v", initial=7))
    assert ok(T.call("debug_run", model=model, to=11, stops=[]))["model_edited"] is True
    fresh = ok(T.call("debug_run", model=model, restart=True, seed=1, to=1, watches=["v", "TNOW"]))
    assert fresh["model_edited"] is False and fresh["watches"] == {"v": 7, "TNOW": 1.0}
    # Review focus: a restart on a model that no longer validates keeps the run there was.
    ok(T.call("disconnect", model=model, source="process_1", target="dispose_1"))
    refused = T.call("debug_run", model=model, restart=True)
    assert refused["error"]["code"] == "E_INVALID_MODEL"
    kept = ok(T.call("debug_run", model=model, to=2))
    assert (kept["state"], kept["t"], kept["model_edited"]) == ("time", 2.0, True)


def test_a_flood_of_hits_and_a_long_queue_come_back_capped(ws):
    """Review focus: 200 parts arriving at once into a stopped block."""
    ok(T.call("new_model", name="flood"))
    ok(T.call("add_block", model="flood", type="CREATE", name="arrive",
              params={"max_arrivals": 1, "entities_per_arrival": 200}))
    ok(T.call("add_block", model="flood", type="PROCESS", name="serve",
              params={"resource_name": "Server", "distribution": "Constant",
                      "dist_params": {"value": 1.0}}))
    ok(T.call("add_block", model="flood", type="DISPOSE", name="leave"))
    ok(T.call("connect", model="flood", source="arrive", target="serve"))
    ok(T.call("connect", model="flood", source="serve", target="leave"))
    ok(upsert("flood", "resources", name="Server", capacity=1))
    out = ok(T.call("debug_run", model="flood", to=0.5, stops=[{"block": "serve"}]))
    assert out["state"] == "stop" and len(out["hits"]) == 50 and out["hits_total"] == 200
    out = ok(T.call("debug_run", model="flood", to=0.5))
    seen = ok(T.call("debug_inspect", model="flood", block="serve"))
    assert seen["block"]["count"] == 200 and len(seen["block"]["entities"]) == 50
    assert len(seen["block"]["queue"]) == 50
    assert len(json.dumps(out)) < SIZE_CAP and len(json.dumps(seen)) < SIZE_CAP


def test_a_crowd_of_waiting_decisions_comes_back_capped_with_its_full_count(ws):
    """300 parts reach a Policy DECIDE in one batch: 300 requests wait, 50 come back."""
    layout = json.loads((ws.root / "parallel_routing_policy.json").read_text(encoding="utf-8"))
    layout["blocks"][0]["params"].update(entities_per_arrival=300, max_arrivals=1)
    (ws.root / "crowd.json").write_text(json.dumps(layout), encoding="utf-8")
    model = _debugged("crowd")
    out = ok(T.call("debug_run", model=model))
    assert out["state"] == "decision" and out["pending_total"] == 300
    assert [p["id"] for p in out["pending"]] == sorted(p["id"] for p in out["pending"])
    assert len(out["pending"]) == 50
    seen = ok(T.call("debug_inspect", model=model))
    assert seen["pending"] == out["pending"] and seen["pending_total"] == 300
    assert len(json.dumps(out)) < SIZE_CAP and len(json.dumps(seen)) < SIZE_CAP


def test_the_debug_tools_take_no_path_endpoint_or_permission_argument():
    by_name = {t["name"]: t for t in T.tool_schemas()}
    assert set(by_name["debug_run"]["input_schema"]["properties"]) == {
        "model", "stops", "watches", "to", "step", "decisions", "restart", "seed", "max_seconds",
        "back", "scope"}
    assert set(by_name["debug_inspect"]["input_schema"]["properties"]) == {
        "model", "block", "entity"}
    assert by_name["debug_inspect"]["read_only"] and not by_name["debug_run"]["read_only"]


# -- run debugger, fix round 1: the arguments, and what the first twelve tests left unpinned --

@pytest.mark.parametrize("bad, name", [
    ({"stops": 0}, "stops"),                    # used to close the armed stops, then raise
    ({"stops": "ab"}, "stops"),
    ({"stops": {"block": "dispose_1"}}, "stops"),
    ({"stops": [{"block": "dispose_1"}] * 51}, "stops"),
    ({"stops": [{"expr": "1" * 501}]}, "stops"),
    ({"stops": [{"block": "b" * 501}]}, "stops"),
    ({"watches": "ab"}, "watches"),             # used to become two watches
    ({"watches": 5}, "watches"),
    ({"watches": ["TNOW"] * 51}, "watches"),    # 10 000 of them made every answer 228 kB
    ({"watches": ["1" * 501]}, "watches"),
    ({"watches": [["TNOW"]]}, "watches"),
    ({"to": float("nan")}, "to"),               # used to run to the horizon and answer "time"
    ({"to": float("inf")}, "to"),
    ({"to": "soon"}, "to"),
    ({"to": True}, "to"),
    ({"to": 10 ** 400}, "to"),                  # too large for a float: no OverflowError
])
def test_a_malformed_argument_is_refused_before_anything_changes(ws, bad, name):
    model = _debugged()
    ok(T.call("debug_run", model=model, to=5, stops=[{"block": "dispose_1"}], watches=["TNOW"]))
    run = ws.debug[model]
    bus = run["session"].ctx.bus

    def stood():
        return (run["session"].now, [dict(stop) for stop in run["stop_specs"]],
                list(run["watches"]), run["stops"], len(bus._subscribers))

    before = stood()
    for extra in ({}, {"restart": True}, {"decisions": [{"policy": "nope", "option": 0}]}):
        error = T.call("debug_run", model=model, **bad, **extra)["error"]
        assert error["code"] == "E_BAD_ARGUMENT" and name in error["message"], (bad, extra)
        assert ws.debug[model] is run and stood() == before, (bad, extra)
    other = ok(T.call("open_model", path="parallel_routing_policy.json"))["model"]
    assert T.call("debug_run", model=other, **bad)["error"]["code"] == "E_BAD_ARGUMENT"
    assert other not in ws.debug                    # a refused start starts nothing
    assert ok(T.call("debug_run", model=model))["state"] == "stop"   # the armed stop still works


def test_arguments_at_their_limits_are_accepted_and_a_bad_stop_is_echoed_short(ws):
    model = _debugged()
    at_the_limit = ["TNOW + " + "1" * 493]                  # 500 characters
    assert len(at_the_limit[0]) == 500
    answer = ok(T.call("debug_run", model=model, to=1, watches=at_the_limit * 1 + ["TNOW"] * 49,
                       stops=[{"block": "dispose_1"}] * 50))
    assert len(answer["watches"]) == 2                      # 50 entries, two distinct texts
    error = T.call("debug_run", model=model, stops=[{"expr": "1", "more": ["x" * 400] * 250}])["error"]
    assert error["code"] == "E_BAD_STOP" and len(error["message"]) < 300   # not a 100 kB echo


def test_a_watch_list_sent_to_a_continuing_run_replaces_the_old_one_at_once(ws):
    model = _debugged()
    ok(T.call("debug_run", model=model, to=5, watches=["TNOW"]))
    out = ok(T.call("debug_run", model=model, to=6, watches=["TNOW + 1", "2 * 3"]))
    assert out["watches"] == {"TNOW + 1": 7.0, "2 * 3": 6}
    assert ws.debug[model]["watches"] == ["TNOW + 1", "2 * 3"]
    assert ok(T.call("debug_run", model=model, to=7, watches=[]))["watches"] == {}


def test_a_restart_that_sends_no_stops_or_watches_keeps_the_old_ones(ws):
    model = _debugged()
    first = ok(T.call("debug_run", model=model, stops=[{"block": "dispose_1"}], watches=["TNOW"]))
    assert first["state"] == "stop" and first["watches"] == {"TNOW": first["t"]}
    assert ok(T.call("debug_run", model=model))["state"] == "stop"      # on, to the next one
    again = ok(T.call("debug_run", model=model, restart=True))
    assert again == first                           # the same stop hits, the same watch is read


def test_new_stops_replace_the_old_ones_on_the_bus(ws):
    model = _debugged()
    ok(T.call("debug_run", model=model, to=1, stops=[{"block": "dispose_1"}]))
    bus = ws.debug[model]["session"].ctx.bus
    with_one = len(bus._subscribers)
    ok(T.call("debug_run", model=model, to=2, stops=[{"block": "process_1"}]))
    assert len(bus._subscribers) == with_one        # the old stops left, the new ones joined
    ok(T.call("debug_run", model=model, to=3, stops=[]))
    assert len(bus._subscribers) == with_one - 1
    ok(T.call("debug_run", model=model, to=4, stops=[{"block": "process_1"}]))
    ok(T.call("debug_run", model=model, to=5, restart=True))
    assert len(bus._subscribers) == with_one - 1    # a restart lets the old run's stops go too


def test_hits_come_back_beside_the_state_the_call_ended_in(ws, monkeypatch):
    """Hits are taken at every return, not only on "stop". Through the tools no small model
    fires a stop in the very step that ends a call (the stop predicate is asked after every
    step), so the reader is patched to have one waiting."""
    from nagasim.core.debugger import Stops
    hit = {"stop": 0, "block": "dispose_1", "entity_id": 7, "entity_type": "DEFAULT"}
    model = _debugged()
    with monkeypatch.context() as patched:
        patched.setattr(Stops, "take_hits", lambda self: [dict(hit)] * 60)
        out = ok(T.call("debug_run", model=model, to=5, stops=[{"expr": "TNOW > 1000"}]))
    assert (out["state"], out["t"]) == ("time", 5.0)
    assert out["hits"] == [hit] * 50 and out["hits_total"] == 60


def test_a_step_that_lands_on_an_armed_stop_reports_the_hit(ws):
    """Stops are read while stepping: the call whose event moves the clock past 3 says so."""
    model = _debugged()
    out = ok(T.call("debug_run", model=model, step=True, stops=[{"expr": "TNOW > 3"}]))
    before = out["t"]
    while out["state"] == "step":
        before = out["t"]
        out = ok(T.call("debug_run", model=model, step=True))
    assert out["state"] == "stop" and out["hits"] == [{"stop": 0, "expr": "TNOW > 3"}]
    assert before <= 3 < out["t"]


def test_a_watch_that_is_not_a_finite_number_comes_back_as_null(ws):
    model = _debugged()
    out = ok(T.call("debug_run", model=model, to=1,
                    watches=["1e308 * 10", "1e308 * 10 - 1e308 * 10", "TNOW"]))
    assert out["watches"] == {"1e308 * 10": None, "1e308 * 10 - 1e308 * 10": None, "TNOW": 1.0}
    json.dumps(out, allow_nan=False)


def test_a_step_is_one_step_even_when_it_emits_more_events_than_a_chunk(ws, monkeypatch):
    """200 parts arrive in one step; handing the trace on mid-call must not end the step's
    own test (it asks whether the recorder holds an event)."""
    ok(T.call("new_model", name="batch"))
    ok(T.call("add_block", model="batch", type="CREATE", name="arrive",
              params={"max_arrivals": 1, "entities_per_arrival": 200}))
    ok(T.call("add_block", model="batch", type="DISPOSE", name="leave"))
    ok(T.call("connect", model="batch", source="arrive", target="leave"))
    with monkeypatch.context() as patched:
        patched.setattr("nagasim.agent_tools.debug.FLUSH_EVENTS", 20)
        out = ok(T.call("debug_run", model="batch", step=True))
    assert out["state"] == "step" and ws.debug["batch"]["session"]._steps == 1


def test_a_long_call_and_the_same_span_in_short_calls_leave_the_same_run(ws, monkeypatch):
    """One call hands its trace to the timeline in chunks, so it holds a bounded number of
    events; the answers cannot differ from those of the same span made of short calls."""
    from nagasim.core.playback import Timeline
    long_run = ok(T.call("open_model", path="single_server.json", name="long"))["model"]
    short_run = ok(T.call("open_model", path="single_server.json", name="short"))["model"]
    chunks, real_append = [], Timeline.append

    def spy(self, events):
        chunks.append(len(events))
        return real_append(self, events)

    def play(model, targets):
        hits, stopped = [], None
        for number, target in enumerate(targets):
            extra = {"stops": [{"expr": "TNOW > 250"}]} if number == 0 else {}
            out = ok(T.call("debug_run", model=model, to=target, **extra))
            hits += out["hits"]
            seen = ok(T.call("debug_inspect", model=model, block="process_1"))
            seen.pop("model")
            if out["state"] == "stop":
                stopped = seen
        return hits, stopped, seen

    with monkeypatch.context() as patched:
        patched.setattr("nagasim.agent_tools.debug.FLUSH_EVENTS", 20)
        patched.setattr(Timeline, "append", spy)
        long_answers = play(long_run, [300, 300])
    assert len(chunks) > 10 and max(chunks) < 50    # hundreds of events came in small chunks
    short_answers = play(short_run, [50, 100, 150, 200, 250, 300, 300])
    assert long_answers[0] == [{"stop": 0, "expr": "TNOW > 250"}]
    assert long_answers == short_answers


def test_debug_inspect_says_when_the_model_is_no_longer_the_one_debugged(ws):
    model = _debugged()
    ok(T.call("debug_run", model=model, to=5))
    assert ok(T.call("debug_inspect", model=model))["model_edited"] is False
    ok(upsert(model, "variables", name="v", initial=7))
    assert ok(T.call("debug_inspect", model=model))["model_edited"] is True
    other = _debugged("parallel_routing_policy")
    ok(T.call("debug_run", model=other, to=5))
    ok(T.call("open_model", path="single_server.json", name=other))    # the name holds another file
    assert ok(T.call("debug_inspect", model=other))["model_edited"] is True


def test_export_refuses_an_exporter_that_needs_a_trace(ws, monkeypatch):
    """D201: the tool keeps the results of a run, never its trace, so a format drawn
    from one is not offered and not written. Its path stays under the workspace root
    as for every other format: nothing here adds a path or a permission argument."""
    from nagasim.exporters import EXPORTER_REGISTRY, Exporter

    written = []
    monkeypatch.setitem(EXPORTER_REGISTRY, "tape", Exporter(
        "tape", ".txt", lambda *args, **kwargs: written.append(args), needs_trace=True))
    model = build_mm1("no_tape")
    ok(T.call("set_simulation_settings", model=model, run_length=50, warmup=0, replications=1))
    ok(T.call("run_model", model=model))
    refused = T.call("export", model=model, format="tape", path="tape.txt")["error"]
    assert refused["code"] == "E_UNKNOWN_FORMAT" and "recorded trace" in refused["message"]
    assert "tape" not in refused["hint"] and "json" in refused["hint"]
    assert "tape" not in T.call("export", model=model, format="nope", path="x")["error"]["hint"]
    assert written == [] and not (ws.root / "tape.txt").exists()


def test_a_bindings_value_is_bounded_in_a_tool_answer(ws, tmp_path):
    """Review M11: what a binding read comes back to an LLM in the tool's answer. A whole
    order book, or a long text from a source, is cut to `MAX_ROWS` rows and 200 characters
    a text, and the row says how much was cut."""
    from nagasim.agent_tools import data as data_tools

    rows = ["id,note"] + [f"WO-{n},{'x' * 300}" for n in range(data_tools.MAX_ROWS + 25)]
    (tmp_path / "orders.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    model = build_mm1()
    doc = ws.models[model]
    doc.set_data_sources([{"name": "book", "kind": "csv", "location": "orders.csv"}])
    doc.set_data_bindings([
        {"name": "orders", "source": "book", "transform": "orders", "target": "orders",
         "map": {"source": "note"}, "enabled": True},
        {"name": "count", "source": "book", "column": "id", "transform": "count",
         "target": "simulation.replications", "enabled": True}])
    answer = ok(T.call("resolve_data_bindings", model=model))
    book, count = answer["bindings"]
    assert len(book["value"]) == data_tools.MAX_ROWS and book["rows"] == data_tools.MAX_ROWS + 25
    assert len(book["value"][0]["source"]) == 200
    assert book["value_cut"] == {"rows": 25, "characters": 100 * data_tools.MAX_ROWS}
    assert (count["value"], count["value_cut"]) == (data_tools.MAX_ROWS + 25, None)
    assert len(json.dumps(answer)) < 80_000


def test_upsert_scenario_stores_a_goal():
    """D577: the question a scenario answers rides along with its overrides."""
    from nagasim.agent_tools.core import _open
    model = build_mm1("goal_mm1", capacity=1)
    ok(T.call("upsert_scenario", model=model, name="S", goal="Can two servers do it?",
              overrides=[{"path": "resources.Server.capacity", "value": 2}]))
    assert _open(model)[0].scenarios[0]["goal"] == "Can two servers do it?"


def test_unknown_tool_suggests():
    err = T.call("validate_modle")["error"]
    assert err["code"] == "E_UNKNOWN_TOOL"
    assert "validate_model" in err["did_you_mean"]
    assert err["hint"].startswith("did you mean: ")


def test_unknown_argument_suggests():
    props = T.TOOLS["new_model"].input_schema["properties"]
    wrong = next(iter(props)) + "s"
    err = T.call("new_model", **{wrong: "x"})["error"]
    assert err["code"] == "E_BAD_ARGUMENT" and wrong in err["message"]
    assert next(iter(props)) in err["did_you_mean"]
