"""Tool search: an eval table of intents and guessed names -> the tool that must be in the top 3,
plus the unit contracts (deterministic order, choices filter, rerank hook, empty query)."""
import pytest

from nagasim import agent_tools, toolsearch
from nagasim.agent_tools.discover import catalogue_index

# (query, acceptable tools: any of them in the top 3 counts)
EVAL = [
    ("validate the study against a formula", {"study_validate"}),
    ("check the model against a textbook answer", {"study_validate"}),
    ("study_validation", {"study_validate"}),
    ("run what-if options", {"study_step", "run_scenarios"}),
    ("study_scenario", {"study_step"}),
    ("run_study", {"study_step", "study_start"}),
    ("add a goal", {"study_update"}),
    ("add_goal", {"study_update"}),
    ("make slides for the meeting", {"study_slides"}),
    ("presentation deck", {"study_slides"}),
    ("which assumptions matter", {"study_sensitivity"}),
    ("undo the last change", {"undo", "study_revert"}),
    ("roll back the study to an earlier version", {"study_revert"}),
    ("export everything for the client", {"study_package"}),
    ("deliverable for the client", {"study_package"}),
    ("show the model picture", {"study_view"}),
    ("paper report", {"study_writeup"}),
    ("write up the study as a document", {"study_writeup"}),
    ("run the model", {"run_model"}),
    ("simulate the model", {"run_model"}),
    ("load a model file", {"open_model"}),
    ("list block types", {"list_block_types"}),
    ("what blocks can I place", {"list_block_types"}),
    ("which distributions exist", {"list_distributions"}),
    ("how do expressions work", {"get_expression_help"}),
    ("check the model for problems", {"validate_model"}),
    ("find the best parameter values", {"optimize"}),
    ("animated gif of a replication", {"study_animation"}),
    ("begin a new study", {"study_start"}),
    ("where does the study stand", {"study_status"}),
    ("the person approves the study", {"study_accept"}),
    ("save the model to disk", {"save_model"}),
    ("connect two blocks", {"connect"}),
    ("how many replications until the interval is narrow", {"run_to_precision"}),
    ("price each scenario against the baseline", {"investment_case"}),
]

# Written blind 2026-10-19 (without seeing the synonym table); its misses were then fixed by adding words to
# six tool descriptions, so it is now tuned like EVAL and overstates. HELD_OUT_2 is the honest number.
HELD_OUT = [
    ("compare two layouts side by side", {"study_step", "run_scenarios"}),
    ("how confident are the results", {"study_status", "study_step"}),
    ("give me an animation of the line", {"study_animation"}),
    ("record what the manager told me", {"study_add_input", "study_update"}),
    ("save the model to disk", {"save_model"}),
    ("what blocks can I use", {"list_block_types", "get_model_format"}),
    ("check the model for mistakes", {"validate_model"}),
    ("pick the best number of servers", {"optimize", "study_step"}),
    ("start a new simulation study", {"study_start"}),
    ("where does the study stand", {"study_status", "study_show"}),
    ("try adding machine breakdowns", {"study_extensions"}),
    ("statistical warm up period", {"suggest_warmup"}),
    ("fit a distribution to data", {"fit_distribution"}),
    ("the person accepts the result", {"study_accept"}),
    ("deliver the final files", {"study_package"}),
    ("show the dashboard", {"study_show"}),
    ("redo with fewer replications", {"study_update", "study_step"}),
    ("explain why the queue grows", {"debug_run", "study_view"}),
    ("full html report of a version", {"study_report"}),
]

# FROZEN benchmark, written 2026-10-19 by an agent that saw only the tool names (no descriptions, no
# synonym table): never tune descriptions, synonyms or weights against it.
HELD_OUT_2 = [
    ("how long do parts wait at the saw", {"get_kpi_table"}),
    ("give me a quick overview of this model", {"get_model_summary"}),
    ("start a fresh empty model", {"new_model"}),
    ("what happens if I add a second shift", {"run_scenarios", "upsert_scenario"}),
    ("run the simulation long enough to trust the numbers", {"run_to_precision"}),
    ("how many warm-up hours should I throw away", {"suggest_warmup"}),
    ("which settings give the best throughput", {"optimize"}),
    ("is it worth buying another machine", {"investment_case"}),
    ("fit a curve to our measured cycle times", {"fit_distribution"}),
    ("bring in our MES event history", {"import_event_log"}),
    ("make the line shown on screen look tidy", {"auto_layout"}),
    ("take back my last change", {"undo"}),
    ("what will the plant look like next week", {"get_twin_forecast", "forecast"}),
    ("what should operators do right now", {"get_twin_recommendations"}),
    ("load the customer orders from a spreadsheet", {"upsert_orders_from_csv"}),
    ("check the model makes physical sense before I show it", {"run_reality_checks", "validate_model"}),
    ("make a slide deck of the findings", {"study_slides"}),
    ("write up the results as a report", {"study_report", "study_writeup"}),
    ("which inputs matter most for output", {"study_sensitivity"}),
    ("begin a new study for this question", {"study_start"}),
    ("go back to the previous study version", {"study_revert"}),
    ("make an animation of how the study changed", {"study_animation"}),
    ("how many operators and machines are available", {"set_resources"}),
    ("step through the run and look at it", {"debug_run", "debug_inspect"}),
    ("what is the progress of the study", {"study_status", "study_show"}),
]
HELD_OUT_2_FLOOR = 0.55   # the top-3 when it was written (14 of 25), rounded down to 5%


def _ranks(table=EVAL):
    idx = catalogue_index()
    for query, wanted in table:
        names = [h["name"] for h in toolsearch.search(idx, query, limit=3)]
        yield query, wanted, next((i for i, n in enumerate(names) if n in wanted), None)


def test_the_eval_table_finds_the_right_tool_in_the_top_three(capsys):
    ranks = list(_ranks())
    top1 = sum(r == 0 for _, _, r in ranks) / len(ranks)
    top3 = sum(r is not None for _, _, r in ranks) / len(ranks)
    misses = [q for q, _, r in ranks if r is None]
    with capsys.disabled():
        print(f"\ntoolsearch eval: top-1 {top1:.0%}, top-3 {top3:.0%}, misses {misses}")
    assert len(EVAL) >= 25 and top3 >= 0.9, misses


def test_the_held_out_tables_keep_their_top_three(capsys):
    # HELD_OUT is tuned now, a regression guard: 0.9, as the product's nine extra tools cost it one query.
    for name, table, floor in (("HELD_OUT", HELD_OUT, 0.9), ("HELD_OUT_2", HELD_OUT_2, HELD_OUT_2_FLOOR)):
        ranks = list(_ranks(table))
        top3 = sum(r is not None for _, _, r in ranks) / len(ranks)
        with capsys.disabled():
            print(f"\ntoolsearch {name}: top-3 {top3:.0%}")
        assert top3 >= floor, [q for q, _, r in ranks if r is None]


def test_a_result_names_the_summary_and_required_arguments():
    hit = toolsearch.search(catalogue_index(), "run the model", limit=1)[0]
    assert hit["name"] == "run_model" and hit["summary"].startswith("Run a model")
    assert hit["args"] == agent_tools.TOOLS["run_model"].input_schema["required"]


def test_the_order_is_deterministic_and_ties_break_by_name():
    specs = [{"name": n, "description": "Count widgets."} for n in ("b_tool", "a_tool", "c_tool")]
    idx = toolsearch.index(specs)
    assert [h["name"] for h in toolsearch.search(idx, "widgets")] == ["a_tool", "b_tool", "c_tool"]
    assert toolsearch.search(idx, "widgets") == toolsearch.search(idx, "widgets")


def test_choices_filter_hides_every_other_tool():
    idx = catalogue_index()
    hits = toolsearch.search(idx, "run the model", limit=20, choices={"run_scenarios", "undo"})
    assert {h["name"] for h in hits} <= {"run_scenarios", "undo"}
    assert toolsearch.search(idx, "run the model", choices=set()) == []


def test_rerank_receives_the_top_candidates_and_decides_the_order():
    seen = []

    def reverse(query, candidates):
        seen.append((query, [c["name"] for c in candidates]))
        return candidates[::-1]

    idx = catalogue_index()
    plain = toolsearch.search(idx, "validate", limit=3)
    out = toolsearch.search(idx, "validate", limit=3, rerank=reverse)
    assert seen[0][0] == "validate" and seen[0][1][:3] == [h["name"] for h in plain]
    assert [h["name"] for h in out] == seen[0][1][::-1][:3]


@pytest.mark.parametrize("query", ["", "   ", "the of a"])
def test_an_empty_query_finds_nothing(query):
    assert toolsearch.search(catalogue_index(), query) == []


def test_find_tool_answers_through_the_registry():
    out = agent_tools.call("find_tool", query="make slides", limit=2)
    assert out["tools"][0]["name"] == "study_slides" and len(out["tools"]) == 2


def test_an_unknown_tool_name_gets_a_typo_match_and_a_semantic_match():
    err = agent_tools.call("run_study")["error"]
    assert err["code"] == "E_UNKNOWN_TOOL" and {"study_step", "study_start"} & set(err["did_you_mean"])
    assert agent_tools.call("run_mdoel")["error"]["did_you_mean"][0] == "run_model"
    only = agent_tools.core.unknown_tool_error("run_study", {"undo", "redo"}).as_dict()["error"]
    assert set(only.get("did_you_mean", [])) <= {"undo", "redo"}


def test_find_tool_only_returns_tools_the_mcp_server_exposes(tmp_path):
    pytest.importorskip("mcp")
    from nagasim import mcp_server
    from nagasim.agent_tools import discover
    try:
        mcp_server.build_server(tmp_path, tools=mcp_server.COMPACT_TOOLS)
        names = {h["name"] for h in agent_tools.call("find_tool", query="run the model", limit=20)["tools"]}
        assert names and names <= set(mcp_server.COMPACT_TOOLS)
    finally:
        discover.exposed = None
        agent_tools.use_workspace(agent_tools.Workspace(None))
