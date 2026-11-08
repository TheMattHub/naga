"""MCP tool profiles (D1048): named tool lists for `naga mcp --tools NAME`, by situation.

Every tool's description is part of each turn an agent pays for, and a smaller list gives
better tool picks than all of them. A profile only chooses WHICH registered tools a session
publishes: names, arguments and answers are the ones `agent_tools.TOOLS` has, and the choice is
the human's start flag, never a tool argument. `all` (every tool) stays the default.

Data only, no imports: a profile may name a tool a product registers; the engine's server
publishes the names it has. `tests/test_mcp_server.py` checks every name is registered.
"""
from __future__ import annotations

# What a small local model is offered (D179; reshaped after the LLM study, D396): the whole
# model in one write instead of block by block, since every tool's description is re-read on
# every turn and every round trip re-reads the conversation. update_block stays for a small
# change to an open model. `assistant` imports it from here.
COMPACT_TOOL_NAMES = frozenset({
    "get_model_format", "write_model", "update_block", "run_model", "get_kpi_table"})

# What a session that only builds and runs a model needs (D397): that list plus the three
# file tools.
COMPACT = COMPACT_TOOL_NAMES | {"open_model", "get_model_json", "save_model"}

# Make and change a model: whole-file or block by block, reference, validation, a picture.
BUILD = COMPACT | {
    "new_model", "list_models", "get_model_summary", "undo", "redo", "import_xmile",
    "import_mdl", "add_block", "remove_blocks", "connect", "disconnect", "reorder_branches",
    "set_resources", "set_calendar", "set_entity_types", "set_simulation_settings",
    "set_sd_settings", "auto_layout", "get_record_schema", "list_records", "upsert_record",
    "remove_record", "list_block_types", "describe_block_type", "list_distributions",
    "describe_distribution", "get_expression_help", "validate_model", "check_draft",
    "view_model"}

# Run a finished model and read the answer: scenarios, precision, checks, the run debugger.
RUN = frozenset({
    "open_model", "get_model_summary", "validate_model", "run_model", "get_kpi_table",
    "run_scenarios", "upsert_scenario", "what_if", "explain_kpi", "investment_case",
    "run_to_precision", "suggest_warmup", "run_reality_checks", "run_loop_dominance",
    "fit_distribution", "input_uncertainty", "export", "run_facts", "debug_run",
    "debug_inspect", "view_model"})

# A decision study and the searches behind it: the study_* workflow, optimise, calibrate.
STUDY = frozenset({
    "study_start", "study_status", "study_update", "study_add_input", "study_step",
    "study_validate", "study_accept", "study_revert", "study_report", "study_package",
    "study_view", "study_show", "study_page_html", "study_sensitivity", "study_extensions",
    "study_animation", "study_writeup", "study_slides",
    "open_model", "save_model", "validate_model", "run_model", "get_kpi_table",
    "run_scenarios", "upsert_scenario", "optimize", "calibrate", "evaluate_policy",
    "fit_surrogate", "export", "view_model"})

# A live twin: plant data in, forecast and recommendations out (the product adds its MES tools).
TWIN = frozenset({
    "open_model", "get_model_summary", "validate_model", "run_model", "get_kpi_table",
    "resolve_data_bindings", "set_initial_state", "upsert_timeseries_input_from_csv",
    "forecast", "get_plant_timeline", "get_twin_monitor", "get_twin_forecast",
    "get_twin_recommendations"})

PROFILES = {"compact": COMPACT, "build": BUILD, "run": RUN, "study": STUDY, "twin": TWIN}
