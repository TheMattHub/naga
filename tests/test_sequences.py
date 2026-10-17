"""Routings and sequence-dependent setups (PHASE4 A2).

Two levels, as in tests/test_tables.py: exact hand-computed behaviour for the
mechanics (route jumps, step values, setup changeovers, validation codes) and
one analytical oracle for the routed job-shop example.
"""
import json
from pathlib import Path

import pytest

from nagasim.core.document import ModelDocument
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.trace import TraceRecorder
from nagasim.core.playback import Timeline

from conftest import layout, dispose, problem_codes, run_layout

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
ROUTED_SHOP = MODELS_DIR / "job_shop_routed.json"


# ---------------------------------------------------------------- builders
def _create(name, e_type, sequence, start_time):
    return {"name": name, "description": name, "type": "CREATE", "position": [0, 0],
            "params": {"e_type": e_type, "arrival_mode": "Interarrival", "schedule_name": "",
                       "distribution": "Constant", "dist_params": {"value": 1000.0},
                       "entities_per_arrival": 1, "max_arrivals": 1, "start_time": start_time,
                       "sequence": sequence}}


def _station(name):
    return {"name": name, "description": name, "type": "STATION", "position": [0, 0], "params": {}}


def _station_process(name, resource, **extra):
    """A station's PROCESS: the service time travels with the job, as
    `entity.process_time` assigned by the routing step."""
    params = {"action": "Seize Delay Release", "distribution": "Constant",
              "dist_params": {"value": "entity.process_time"}, "priority": 0, "quantity": 1,
              "resource_name": resource, "setup": "", "setup_key": "", "initial_key": ""}
    params.update(extra)
    return {"name": name, "description": name, "type": "PROCESS", "position": [0, 0],
            "params": params}


def _route(name="route_1", value=0.0):
    return {"name": name, "description": name, "type": "ROUTE", "position": [0, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": value},
                       "time_unit": "base"}}


def _step(station, process_time, **values):
    return {"station": station, "values": {"process_time": process_time, **values}}


def _two_product_shop(setup=""):
    """Two products through three single-server stations, every time a constant.

    A: S1(2) -> S2(3).    B: S2(4) -> S1(1) -> S3(5).
    B arrives at t=20, long after A has left, so the two never contend and the
    whole timeline is exact arithmetic -- the only interaction left is the
    CHANGEOVER at S1, which is the point of the test.
    """
    blocks = [
        _create("create_a", "A", "A", 0),
        _create("create_b", "B", "B", 20),
        _route(),
        _station("S1"), _station("S2"), _station("S3"),
        _station_process("p1", "R1", setup=setup, initial_key="A"),
        _station_process("p2", "R2"),
        _station_process("p3", "R3"),
        dispose(),
    ]
    links = [{"source": "create_a", "target": "route_1"},
             {"source": "create_b", "target": "route_1"},
             {"source": "route_1", "target": "dispose_1"},
             {"source": "S1", "target": "p1"}, {"source": "S2", "target": "p2"},
             {"source": "S3", "target": "p3"},
             {"source": "p1", "target": "route_1"}, {"source": "p2", "target": "route_1"},
             {"source": "p3", "target": "route_1"}]
    return layout(
        blocks, links,
        resources=[{"name": f"R{i}", "capacity": 1, "discipline": "FIFO"} for i in (1, 2, 3)],
        sequences=[{"name": "A", "steps": [_step("S1", 2.0), _step("S2", 3.0)]},
                   {"name": "B", "steps": [_step("S2", 4.0), _step("S1", 1.0),
                                           _step("S3", 5.0)]}],
        tables=[{"name": "Setups", "keys": ["A", "B"], "columns": ["A", "B"],
                 "values": [[0.0, 7.0], [7.0, 0.0]], "key_mode": "exact",
                 "column_mode": "exact"}])


def _write(tmp_path, doc, name="routed.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


def _by_name(model, name):
    return model.blocks[name]


# ---------------------------------------------------------------- exact mechanics
def test_two_product_routed_shop_is_exact_including_the_changeover(env, tmp_path):
    """Hand-computed to the minute (PHASE4 A2).

    A (t=0):  route -> S1 service 2 -> route -> S2 service 3 -> finished at 5.
              No setup at S1: initial_key "A" is what the station already holds.
    B (t=20): route -> S2 service 4 (ends 24) -> route -> S1, which is still set
              up for A, so TABLE("Setups", "A", "B") = 7 is charged first:
              24 + 7 + 1 = 32 -> route -> S3 service 5 -> finished at 37.
    """
    path = _write(tmp_path, _two_product_shop(setup='TABLE("Setups", previous_key, key)'))
    model = run_layout(env, path, 50)

    a, b = list(env.ctx.all_entities)
    assert (a.e_type, b.e_type) == ("A", "B")
    assert a.creation_time == 0 and b.creation_time == 20
    assert a._throughput_time == 5.0
    assert b._throughput_time == 17.0            # 4 service + 7 setup + 1 + 5

    # The step index walked to the end of each routing, and the last station
    # each job visited is the one it was stamped with on arrival.
    assert (a.step, a.station) == (2, "S2")
    assert (b.step, b.station) == (3, "S3")
    # Step values reached the entity: B's last S1 operation, not A's.
    assert b.process_time == 5.0

    p1 = _by_name(model, "p1")
    assert (p1.setup_count, p1.setup_time_total) == (1, 7.0)
    # Setup time is held time: R1 is busy for 2 (A) + 7 (setup) + 1 (B) = 10.
    assert env.ctx.resource_manager.get("R1").busy.area(50.0) == 10.0


def test_without_a_setup_the_same_shop_loses_exactly_the_changeover(env, tmp_path):
    path = _write(tmp_path, _two_product_shop())
    run_layout(env, path, 50)
    a, b = list(env.ctx.all_entities)
    assert (a._throughput_time, b._throughput_time) == (5.0, 10.0)


def test_a_setup_is_charged_once_per_changeover_not_per_job(env, tmp_path):
    """Three jobs A, A, B at one station: only the A -> B switch is charged."""
    doc = layout(
        [_create("create_a", "A", "A", 0), _create("create_a2", "A", "A", 10),
         _create("create_b", "B", "B", 20), _route(), _station("S1"),
         _station_process("p1", "R1", setup='TABLE("Setups", previous_key, key)',
                          initial_key="A"),
         dispose()],
        [{"source": "create_a", "target": "route_1"},
         {"source": "create_a2", "target": "route_1"},
         {"source": "create_b", "target": "route_1"},
         {"source": "route_1", "target": "dispose_1"},
         {"source": "S1", "target": "p1"}, {"source": "p1", "target": "route_1"}],
        resources=[{"name": "R1", "capacity": 1, "discipline": "FIFO"}],
        sequences=[{"name": "A", "steps": [_step("S1", 2.0)]},
                   {"name": "B", "steps": [_step("S1", 1.0)]}],
        tables=[{"name": "Setups", "keys": ["A", "B"], "columns": ["A", "B"],
                 "values": [[0.0, 7.0], [7.0, 0.0]], "key_mode": "exact",
                 "column_mode": "exact"}])
    model = run_layout(env, _write(tmp_path, doc), 60)
    p1 = _by_name(model, "p1")
    assert (p1.setup_count, p1.setup_time_total) == (1, 7.0)


def _changeover_shop(*, initial_key="A", setup_cost='TABLE("Costs", previous_key, key)'):
    """Five jobs A, A, B, B, A at ONE station, arriving ten apart so nothing
    contends: the only changeovers are A->B and B->A."""
    creates = [_create(f"job_{i}", e_type, e_type, 10 * i)
               for i, e_type in enumerate(("A", "A", "B", "B", "A"))]
    return layout(
        creates + [_route(), _station("S1"),
                   _station_process("p1", "R1", setup='TABLE("Setups", previous_key, key)',
                                    setup_cost=setup_cost, initial_key=initial_key),
                   dispose()],
        [{"source": c["name"], "target": "route_1"} for c in creates]
        + [{"source": "route_1", "target": "dispose_1"},
           {"source": "S1", "target": "p1"}, {"source": "p1", "target": "route_1"}],
        resources=[{"name": "R1", "capacity": 1, "discipline": "FIFO"}],
        sequences=[{"name": "A", "steps": [_step("S1", 2.0)]},
                   {"name": "B", "steps": [_step("S1", 2.0)]}],
        tables=[{"name": "Setups", "keys": ["A", "B"], "columns": ["A", "B"],
                 "values": [[0.0, 3.0], [3.0, 0.0]], "key_mode": "exact",
                 "column_mode": "exact"},
                {"name": "Costs", "keys": ["A", "B"], "columns": ["A", "B"],
                 "values": [[0.0, 40.0], [25.0, 0.0]], "key_mode": "exact",
                 "column_mode": "exact"}])


def test_a_changeover_cost_is_charged_per_changeover_and_joins_total_cost(tmp_path):
    """A, A, B, B, A with initial_key "A": two changeovers, A->B at 40 and
    B->A at 25, so 65 exactly -- not five jobs' worth of anything."""
    path = _write(tmp_path, _changeover_shop())
    kpi = run_single_simulation(str(path), seed=0, run_len=100, warmup=0)

    queue = kpi["queue_summary"].set_index("Block")
    assert queue.loc["p1", "Changeovers"] == 2
    assert queue.loc["p1", "Setup Time"] == pytest.approx(6.0)       # 3 + 3

    costs = kpi["cost_summary"].set_index("Item")
    assert costs.loc["Changeover p1", "Amount"] == pytest.approx(65.0)
    assert costs.loc["Changeover p1", "Category"] == "Changeover"
    total = kpi["system"].set_index("Metric")["Value"]
    assert float(total["Total Cost"]) == pytest.approx(65.0)


def test_the_initial_key_decides_whether_the_first_job_is_a_changeover(tmp_path):
    """Set up for B and the first A job IS a changeover: three, at 25+40+25."""
    path = _write(tmp_path, _changeover_shop(initial_key="B"))
    kpi = run_single_simulation(str(path), seed=0, run_len=100, warmup=0)

    assert kpi["queue_summary"].set_index("Block").loc["p1", "Changeovers"] == 3
    assert kpi["cost_summary"].set_index("Item").loc["Changeover p1", "Amount"] ==         pytest.approx(90.0)


def test_a_free_changeover_is_still_counted_and_charged(tmp_path):
    """A changeover with no TIME still costs money: the event is the changeover,
    not the delay it happens to need."""
    doc = _changeover_shop(setup_cost="11.0")
    for block in doc["blocks"]:
        if block["name"] == "p1":
            block["params"]["setup"] = ""        # cost only, no setup time
    path = _write(tmp_path, doc)
    kpi = run_single_simulation(str(path), seed=0, run_len=100, warmup=0)

    queue = kpi["queue_summary"].set_index("Block")
    assert (queue.loc["p1", "Changeovers"], queue.loc["p1", "Setup Time"]) == (2, 0.0)
    assert kpi["cost_summary"].set_index("Item").loc["Changeover p1", "Amount"] ==         pytest.approx(22.0)


def test_a_warm_up_restarts_the_changeover_count_and_its_cost(tmp_path):
    """Warm-up ends at t=25, after the A->B changeover at t=20 and before the
    B->A one at t=40: one changeover left, 25 of cost."""
    path = _write(tmp_path, _changeover_shop())
    kpi = run_single_simulation(str(path), seed=0, run_len=100, warmup=25.0)

    assert kpi["queue_summary"].set_index("Block").loc["p1", "Changeovers"] == 1
    assert kpi["cost_summary"].set_index("Item").loc["Changeover p1", "Amount"] ==         pytest.approx(25.0)


def test_route_transfer_time_sees_the_from_and_to_station(env, tmp_path):
    """A from-to distance table drives the travel time: `from_station` is where
    the entity is, `to_station` where the next step sends it."""
    doc = _two_product_shop()
    for block in doc["blocks"]:
        if block["name"] == "route_1":
            block["params"]["dist_params"] = {
                "value": 'TABLE("Distance", from_station, to_station)'}
    doc["tables"].append({"name": "Distance", "keys": ["S1", "S2"], "columns": ["S1", "S2", "S3"],
                          "values": [[0.0, 6.0, 0.0], [9.0, 0.0, 0.0]],
                          "key_mode": "exact", "column_mode": "exact", "default": 0.0})
    run_layout(env, _write(tmp_path, doc), 80)
    a, b = list(env.ctx.all_entities)
    # A: 0 (entering, no from-station -> the table default) + 2 + 6 + 3 = 11.
    assert a._throughput_time == 11.0
    # B: 0 + 4 + 9 (S2 -> S1) + 1 + 0 (S1 -> S3) + 5 = 19.
    assert b._throughput_time == 19.0


def test_a_route_jump_is_a_real_entity_move_that_playback_animates(env, tmp_path):
    """Item 8: a jump has no drawn link, but it emits the same `entity_moved`
    the animator already replays, so travel between the ROUTE block and the
    station is drawn with no new machinery."""
    doc = _two_product_shop()
    for block in doc["blocks"]:
        if block["name"] == "route_1":
            block["params"]["dist_params"] = {"value": 2.0}
    recorder = TraceRecorder()
    env.ctx.bus.subscribe(recorder)
    run_layout(env, _write(tmp_path, doc), 80)

    jumps = [(e.block, e.target, e.t, e.duration) for e in recorder.events
             if e.kind == "entity_moved" and e.block == "route_1"]
    assert ("route_1", "S1", 0.0, 2.0) in jumps
    assert ("route_1", "S2", 4.0, 2.0) in jumps        # A: 2 travel + 2 service

    timeline = Timeline(recorder.events)
    # Mid-transfer the entity is in no block, and its progress is linear.
    state = timeline.state_at(1.0)
    assert state["counts"].get("S1") is None
    transit = list(state["in_transit"].values())
    assert transit and transit[0][:2] == ("route_1", "S1") and transit[0][2] == pytest.approx(0.5)
    # Arrived at S1 at 2.0 and passed straight on to p1 (#231: not left in S1).
    arrived = timeline.state_at(2.0)
    assert arrived["entities"][1][0] == "p1" and "S1" not in arrived["counts"]


def test_an_entity_with_no_sequence_is_a_loud_model_error(env, tmp_path):
    doc = _two_product_shop()
    for block in doc["blocks"]:
        if block["name"] == "create_a":
            block["params"]["sequence"] = ""
    with pytest.raises(RuntimeError, match="has no sequence"):
        run_layout(env, _write(tmp_path, doc), 10)


def test_assign_sets_a_sequence_and_resets_the_step(env, tmp_path):
    """Item 4: assigning `entity.sequence` starts that routing from the top.

    The ASSIGN deliberately sets a stale `step` FIRST: without the reset the
    job would be past the end of "B" and leave in 0 minutes, so the 10 minutes
    below is the reset, not a coincidence.
    """
    doc = _two_product_shop()
    for block in doc["blocks"]:
        if block["name"] == "create_a":
            block["params"]["sequence"] = ""        # routing comes from the ASSIGN
    doc["blocks"].append(
        {"name": "assign_1", "description": "assign_1", "type": "ASSIGN", "position": [0, 0],
         "params": {"assignments": [{"target": "entity.step", "expr": "5"},
                                    {"target": "entity.sequence", "expr": "'B'"}]}})
    doc["blocks"] = [b for b in doc["blocks"] if b["name"] != "create_b"]
    doc["links"] = [{"source": "create_a", "target": "assign_1"},
                    {"source": "assign_1", "target": "route_1"}] + [
        l for l in doc["links"] if l["source"] not in ("create_a", "create_b")]
    run_layout(env, _write(tmp_path, doc), 200)
    a = list(env.ctx.all_entities)[0]
    assert a._throughput_time == 10.0       # B's 4 + 1 + 5, from step 0
    assert a.step == 3


# ---------------------------------------------------------------- validation
def _codes(doc):
    return problem_codes(doc, severity=None)


def test_validation_reports_every_routing_fault():
    doc = _two_product_shop()
    doc["sequences"] = [
        {"name": "A", "steps": [{"station": "Nowhere"}]},           # unknown station
        {"name": "A", "steps": [_step("S1", 1.0)]},                  # duplicate name
        {"name": "", "steps": [_step("S1", 1.0)]},                   # empty name
        {"name": "Empty", "steps": []},                              # no steps
        {"name": "Bad", "steps": [{"station": "S1", "values": {"ok": [1, 2]}}]},
    ]
    codes = _codes(doc)
    assert {"E_SEQUENCE_STATION_UNKNOWN", "E_SEQUENCE_NAME", "E_SEQUENCE_EMPTY",
            "E_SEQUENCE_STEP"} <= codes
    # S2/S3 are now visited by nothing, and the CREATE blocks name "B", which
    # this broken section no longer declares.
    assert {"W_STATION_UNUSED", "E_SEQUENCE_UNKNOWN"} <= codes


def test_a_place_on_the_network_is_not_an_unused_station():
    """Hardening pass: a STATION with a `node` is where TRANSPORT delivers, and
    a model with no sequences at all routes by nothing -- neither warns. The
    warehouse example used to print five of these on every run."""
    doc = _two_product_shop()
    doc["sequences"] = [{"name": "A", "steps": [_step("S1", 1.0)]},
                        {"name": "B", "steps": [_step("S1", 1.0)]}]
    assert "W_STATION_UNUSED" in _codes(doc)                    # S2, S3 unvisited
    doc["network"] = {"nodes": [{"name": "n2", "x": 0, "y": 0}, {"name": "n3", "x": 1, "y": 0}],
                      "segments": [{"from": "n2", "to": "n3"}]}
    _by_dict(doc, "S2")["params"] = {"node": "n2"}
    _by_dict(doc, "S3")["params"] = {"node": "n3"}
    assert "W_STATION_UNUSED" not in _codes(doc)
    plain = _two_product_shop()
    plain["sequences"] = []
    for block in plain["blocks"]:
        block["params"].pop("sequence", None)
    assert "W_STATION_UNUSED" not in _codes(plain)


def test_a_station_alias_is_a_routing_target_and_a_duplicate_is_reported(env, tmp_path):
    """PHASE4 B6: `station` renames a STATION for the routing sheet, which is
    what lets one live inside an asset. Both names resolve; `entity.station` is
    the alias; two blocks claiming one name is an error."""
    doc = _two_product_shop()
    _by_dict(doc, "S1")["params"] = {"station": "Mill"}
    doc["sequences"] = [
        {"name": "A", "steps": [_step("S2", 3.0), _step("Mill", 2.0)]},      # by alias
        {"name": "B", "steps": [_step("S2", 4.0), _step("S1", 1.0),          # by block name
                                _step("S3", 5.0)]}]
    assert not {"E_SEQUENCE_STATION_UNKNOWN", "W_STATION_UNUSED", "W_UNREACHABLE",
                "E_DUPLICATE_STATION"} & _codes(doc)

    model = run_layout(env, _write(tmp_path, doc), 50)
    a = next(e for e in model.env.ctx.all_entities if e.e_type == "A")
    assert a.station == "Mill"              # the alias, not the block name
    assert a._throughput_time == 5.0
    b = next(e for e in model.env.ctx.all_entities if e.e_type == "B")
    assert b._throughput_time == 10.0       # 4 + 1 at the aliased station + 5

    _by_dict(doc, "S2")["params"] = {"station": "Mill"}
    assert "E_DUPLICATE_STATION" in _codes(doc)


def _by_dict(doc, name):
    return next(b for b in doc["blocks"] if b["name"] == name)


def test_a_route_block_with_no_sequences_at_all_is_reported():
    doc = _two_product_shop()
    doc["sequences"] = []
    assert "E_ROUTE_NO_SEQUENCES" in _codes(doc)


def test_an_assign_to_a_literal_unknown_sequence_is_reported():
    doc = _two_product_shop()
    doc["blocks"].append(
        {"name": "assign_1", "description": "a", "type": "ASSIGN", "position": [0, 0],
         "params": {"assignments": [{"target": "entity.sequence", "expr": "'Nope'"}]}})
    doc["links"].append({"source": "assign_1", "target": "route_1"})
    assert "E_SEQUENCE_UNKNOWN" in _codes(doc)


def test_a_computed_assign_expression_is_left_to_run_time():
    """Only a literal name can be checked, exactly like a literal TABLE() name."""
    doc = _two_product_shop()
    doc["blocks"].append(
        {"name": "assign_1", "description": "a", "type": "ASSIGN", "position": [0, 0],
         "params": {"assignments": [
             {"target": "entity.sequence", "expr": "'A' if entity.e_type == 'A' else 'B'"}]}})
    doc["links"].append({"source": "assign_1", "target": "route_1"})
    assert "E_SEQUENCE_UNKNOWN" not in _codes(doc)


def test_stations_reached_only_by_a_route_are_not_reported_unreachable():
    """Item 7: a station has no incoming link at all, so the reachability walk
    has to follow route jumps or a routed job shop is one long warning list."""
    assert "W_UNREACHABLE" not in _codes(_two_product_shop())


def test_a_route_block_still_needs_its_own_outgoing_link():
    """The drawn link is the finished-routing exit, so a ROUTE without one is a
    terminal block -- the route jumps must NOT satisfy that rule."""
    doc = _two_product_shop()
    doc["links"] = [l for l in doc["links"] if l != {"source": "route_1", "target": "dispose_1"}]
    assert "E_TERMINAL_NOT_DISPOSE" in _codes(doc)


def test_the_setup_expression_sees_previous_key_and_key_but_nothing_else_does():
    doc = _two_product_shop(setup="previous_key + key")
    assert "E_EXPRESSION" not in _codes(doc)
    doc = _two_product_shop()
    for block in doc["blocks"]:
        if block["name"] == "p2":
            block["params"]["dist_params"] = {"value": "previous_key"}
    assert "E_EXPRESSION" in _codes(doc)


def test_a_station_inside_a_submodel_is_named_by_its_flattened_path():
    """Item 6: a station's name IS its block's name, so `$` scoping never
    enters into it -- the flattener's `instance/block` path is what a sequence
    step must say (ARCHITECTURE D51)."""
    doc = _two_product_shop()
    doc["blocks"] = [b for b in doc["blocks"] if b["name"] not in ("S3", "p3")]
    doc["links"] = [l for l in doc["links"]
                    if l["source"] not in ("S3", "p3") and l["target"] != "S3"]
    doc["blocks"].append({"name": "cell", "description": "cell", "type": "SUBMODEL",
                          "position": [0, 0], "params": {"definition": "Cell", "arguments": {}}})
    doc["submodels"] = {"Cell": {
        "info": {}, "parameters": [],
        "blocks": [_station("S3"), _station_process("p3", "$R3"),
                   {"name": "out", "description": "out", "type": "SUB_EXIT",
                    "position": [0, 0], "params": {"port": "out"}}],
        "links": [{"source": "S3", "target": "p3"}, {"source": "p3", "target": "out"}],
        "annotations": []}}
    doc["links"].append({"source": "cell", "target": "route_1", "source_port": "out"})
    doc["sequences"][1]["steps"][2]["station"] = "cell/S3"

    assert _codes(doc) == set()
    # Naming the un-scoped inner name instead is the mistake validation catches.
    doc["sequences"][1]["steps"][2]["station"] = "S3"
    assert "E_SEQUENCE_STATION_UNKNOWN" in _codes(doc)


# ---------------------------------------------------------------- the example model
def test_the_routed_example_model_loads_with_zero_problems():
    assert ModelDocument.load(ROUTED_SHOP).problems() == []


# Offered load per station = sum over products of (arrival rate x mean service
# time of that product's visits), straight off the routing sheet in the model
# file. Utilization is that divided by the server count -- work conservation,
# so it is exact whatever the service distribution or the queue discipline.
_ARRIVALS = {"Widget": 0.5, "Bracket": 0.3, "Shaft": 0.2}
_SERVERS = {"Cutting": 1, "Drilling": 2, "AssemblyStation": 2}
_RESOURCE_OF = {"Cutting": "Cutting", "Drilling": "Drilling", "Assembly": "AssemblyStation"}


def _theory_utilizations() -> dict:
    sequences = {s["name"]: s["steps"] for s in json.loads(ROUTED_SHOP.read_text())["sequences"]}
    load = {name: 0.0 for name in _SERVERS}
    for product, rate in _ARRIVALS.items():
        for step in sequences[product]:
            load[_RESOURCE_OF[step["station"]]] += rate * step["values"]["process_time"]
    return {name: load[name] / servers for name, servers in _SERVERS.items()}


def test_the_routed_shop_offered_load_is_in_the_useful_band():
    for name, rho in _theory_utilizations().items():
        assert 0.6 <= rho <= 0.85, f"{name}: rho={rho:.3f} outside [0.6, 0.85]"


def test_the_setup_scenario_makes_cutting_measurably_busier():
    """The model's one scenario switches the sequence-dependent setup on. It can
    only ADD busy time, and only at Cutting.

    One short seed, not a `slow` replication sweep: the effect is about ten
    utilization points against a couple of points of noise, and the setup's
    exact semantics are already pinned by the hand-computed tests above.

    run_len 1000, warmup 200, seed 0 (suite passes M3D-5 and 4): the Cutting
    gap is 9.38 points against a 5.0-point requirement (seeds 1-3: 9.8-10.1),
    and the other two resources stay under 0.9 points apart against a 2.0
    tolerance.
    """
    from nagasim import api, experiments

    layouts = experiments._scenario_layouts(api.load_layout(str(ROUTED_SHOP)), None)
    usage = {}
    for scenario, lay in layouts.items():           # one run per (scenario, seed)
        rows = [run_single_simulation(lay, seed=seed, run_len=1000, warmup=200)["resource_summary"]
                for seed in (0,)]
        for resource in ("Cutting", "Drill1", "AssemblyStation"):
            usage[scenario, resource] = sum(
                r[r["Resource"] == resource]["Usage % (snapshot)"].iloc[0] for r in rows) / len(rows)

    setups, base = "Sequence-dependent setups", experiments.BASELINE
    assert usage[setups, "Cutting"] > usage[base, "Cutting"] + 5.0
    for other in ("Drill1", "AssemblyStation"):
        assert usage[setups, other] == pytest.approx(usage[base, other], abs=2.0)
