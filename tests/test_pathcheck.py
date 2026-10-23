"""Entity-path checks (`path_checks`, nagasim/pathcheck.py, D722).

Every case runs the two-product routed shop of `tests/test_sequences.py`, whose timeline is
exact arithmetic (constant times, the two products never contend), so each route below is
derived by hand:

    A (created t=0):  create_a 0, route_1 0, S1 0, p1 0, route_1 2, S2 2, p2 2, route_1 5,
                      dispose_1 5.
    B (created t=20): create_b 20, route_1 20, S2 20, p2 20, route_1 24, S1 24, p1 24,
                      route_1 25, S3 25, p3 25, route_1 30, dispose_1 30.

Every move from route_1 to a station is a route jump (`send_to`), not a drawn link.
"""
import xml.etree.ElementTree as ET

import pytest

from conftest import dispose, layout

from nagasim import modelcheck, pathcheck
from nagasim.core.schema import validate_model_structured


def _create(name, e_type, sequence, start_time):
    return {"name": name, "description": name, "type": "CREATE", "position": [0, 0],
            "params": {"e_type": e_type, "arrival_mode": "Interarrival", "schedule_name": "",
                       "distribution": "Constant", "dist_params": {"value": 1000.0},
                       "entities_per_arrival": 1, "max_arrivals": 1, "start_time": start_time,
                       "sequence": sequence}}


def _station(name):
    return {"name": name, "description": name, "type": "STATION", "position": [0, 0], "params": {}}


def _station_process(name, resource):
    return {"name": name, "description": name, "type": "PROCESS", "position": [0, 0],
            "params": {"action": "Seize Delay Release", "distribution": "Constant",
                       "dist_params": {"value": "entity.process_time"}, "priority": 0,
                       "quantity": 1, "resource_name": resource}}


def _step(station, process_time):
    return {"station": station, "values": {"process_time": process_time}}


def _shop(path_checks, time_limit=None):
    blocks = [_create("create_a", "A", "A", 0), _create("create_b", "B", "B", 20),
              {"name": "route_1", "description": "route_1", "type": "ROUTE", "position": [0, 0],
               "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                          "time_unit": "base"}},
              _station("S1"), _station("S2"), _station("S3"),
              _station_process("p1", "R1"), _station_process("p2", "R2"),
              _station_process("p3", "R3"), dispose()]
    links = [{"source": "create_a", "target": "route_1"},
             {"source": "create_b", "target": "route_1"},
             {"source": "route_1", "target": "dispose_1"},
             {"source": "S1", "target": "p1"}, {"source": "S2", "target": "p2"},
             {"source": "S3", "target": "p3"},
             {"source": "p1", "target": "route_1"}, {"source": "p2", "target": "route_1"},
             {"source": "p3", "target": "route_1"}]
    doc = layout(blocks, links,
                 resources=[{"name": f"R{i}", "capacity": 1, "discipline": "FIFO"} for i in (1, 2, 3)],
                 sequences=[{"name": "A", "steps": [_step("S1", 2.0), _step("S2", 3.0)]},
                            {"name": "B", "steps": [_step("S2", 4.0), _step("S1", 1.0),
                                                    _step("S3", 5.0)]}],
                 simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})
    doc["path_checks"] = path_checks
    return doc


def _rows(checks, time_limit=None):
    return {row["name"]: row for row in pathcheck.check_paths(_shop(checks, time_limit))}


def _first(row):
    example = row["examples"][0]
    return example["e_type"], example["t"]


# ------------------------------------------------------------------ the four rules

def test_order_passes_and_fails_on_the_hand_derived_routes():
    rows = _rows([{"name": "s1_then_s2", "rule": "order", "first": "S1", "then": "S2"},
                  {"name": "s2_then_s1", "rule": "order", "first": "S2", "then": "S1"}])
    # A goes S1 -> S2; B goes S2 (t=20) before S1 (t=24).
    assert not rows["s1_then_s2"]["passed"]
    assert rows["s1_then_s2"]["violations"] == 1
    assert _first(rows["s1_then_s2"]) == ("B", 20.0)
    # A reaches S1 (t=0) before S2 (t=2): the violation is at its S1 arrival.
    assert not rows["s2_then_s1"]["passed"] and _first(rows["s2_then_s1"]) == ("A", 0.0)


def test_order_ignores_an_entity_that_visits_only_one_of_the_two():
    rows = _rows([{"name": "s3_then_s1", "rule": "order", "first": "S3", "then": "S1"}])
    # A never visits S3: not judged. B reaches S1 (24) before S3 (25): a violation.
    assert rows["s3_then_s1"]["violations"] == 1 and _first(rows["s3_then_s1"]) == ("B", 24.0)


def test_never_and_at_most():
    rows = _rows([{"name": "no_s3", "rule": "never", "block": "S3"},
                  {"name": "no_scrap", "rule": "never", "block": "p3", "e_type": "A"},
                  {"name": "route_3", "rule": "at_most", "block": "route_1", "count": 3},
                  {"name": "route_4", "rule": "at_most", "block": "route_1", "count": 4}])
    assert not rows["no_s3"]["passed"] and _first(rows["no_s3"]) == ("B", 25.0)
    assert rows["no_scrap"]["passed"]
    # A reaches route_1 three times, B four: B's fourth visit is at t=30.
    assert not rows["route_3"]["passed"] and _first(rows["route_3"]) == ("B", 30.0)
    assert rows["route_3"]["examples"][0]["block"] == "route_1"
    assert rows["route_4"]["passed"]


def test_always_and_the_per_type_filter():
    rows = _rows([{"name": "all_s3", "rule": "always", "block": "S3"},
                  {"name": "b_s3", "rule": "always", "block": "S3", "e_type": "B"},
                  {"name": "all_s2", "rule": "always", "block": "S2"}])
    # A leaves at t=5 without visiting S3.
    assert not rows["all_s3"]["passed"] and _first(rows["all_s3"]) == ("A", 5.0)
    assert rows["b_s3"]["passed"] and rows["b_s3"]["judged"] == 1
    assert rows["all_s2"]["passed"] and rows["all_s2"]["judged"] == 2


def test_a_route_jump_and_the_creation_block_count_as_arrivals():
    rows = _rows([{"name": "made_at_b", "rule": "never", "block": "create_b"},
                  {"name": "jumped_to_s1", "rule": "never", "block": "S1", "e_type": "B"}])
    assert _first(rows["made_at_b"]) == ("B", 20.0)
    assert _first(rows["jumped_to_s1"]) == ("B", 24.0)


def test_an_entity_still_in_the_system_is_judged_by_never_at_most_and_order_only():
    # At t=22 B is still at S2 (until 24): it has not completed.
    rows = _rows([{"name": "b_s3", "rule": "always", "block": "S3", "e_type": "B"},
                  {"name": "no_s2", "rule": "never", "block": "S2", "e_type": "B"},
                  {"name": "route_0", "rule": "at_most", "block": "route_1", "count": 0,
                   "e_type": "B"},
                  {"name": "s1_then_s2", "rule": "order", "first": "S1", "then": "S2",
                   "e_type": "B"}], time_limit=22)
    assert rows["b_s3"]["passed"] and rows["b_s3"]["judged"] == 0
    assert _first(rows["no_s2"]) == ("B", 20.0)
    assert _first(rows["route_0"]) == ("B", 20.0)
    # B has not visited S1 by t=22: it visits only one of the two, so it is not judged.
    assert rows["s1_then_s2"]["passed"]


def test_examples_are_capped_and_each_names_its_entity():
    rows = _rows([{"name": "no_route", "rule": "never", "block": "route_1"}])
    row = rows["no_route"]
    assert row["violations"] == 2              # one per entity, at its first visit
    assert [e["t"] for e in row["examples"]] == [0.0, 20.0]
    assert all(isinstance(e["entity"], int) for e in row["examples"])
    assert len({e["entity"] for e in row["examples"]}) == 2


# ------------------------------------------------------------------ validation

def _codes(checks):
    return {p.code for p in validate_model_structured(_shop(checks)) if p.severity == "error"}


@pytest.mark.parametrize("checks, code", [
    ([{"name": "x", "rule": "never", "block": "Nowhere"}], "E_PATH_CHECK_BLOCK"),
    ([{"name": "x", "rule": "order", "first": "S1"}], "E_PATH_CHECK_BLOCK"),
    ([{"name": "x", "rule": "order", "first": "S1", "then": "S1"}], "E_PATH_CHECK_BLOCK"),
    ([{"name": "x", "rule": "at_most", "block": "S1", "count": -1}], "E_PATH_CHECK_COUNT"),
    ([{"name": "x", "rule": "at_most", "block": "S1", "count": 1.5}], "E_PATH_CHECK_COUNT"),
    ([{"name": "x", "rule": "at_most", "block": "S1"}], "E_PATH_CHECK_COUNT"),
    ([{"name": "x", "rule": "sometimes", "block": "S1"}], "E_PATH_CHECK_RULE"),
    ([{"name": "x", "rule": "order", "first": "S1", "then": "S2", "block": "S3"}],
     "E_PATH_CHECK_RULE"),
    ([{"name": "x", "rule": "never", "block": "S1", "count": 2}], "E_PATH_CHECK_RULE"),
    ([{"name": "", "rule": "never", "block": "S1"}], "E_PATH_CHECK_NAME"),
    ([{"name": "x", "rule": "never", "block": "S1"},
      {"name": "x", "rule": "never", "block": "S2"}], "E_DUPLICATE_PATH_CHECK"),
    ("not a list", "E_PATH_CHECK_RULE"),
])
def test_validation_errors_by_code(checks, code):
    assert code in _codes(checks)


def test_a_stray_key_is_named():
    found = [p for p in validate_model_structured(_shop([
        {"name": "x", "rule": "order", "first": "S1", "then": "S2", "block": "S3"}]))
        if p.code == "E_PATH_CHECK_RULE"]
    assert len(found) == 1 and "'block'" in found[0].message


def test_valid_checks_raise_no_problem():
    assert not {c for c in _codes([
        {"name": "a", "rule": "order", "first": "S1", "then": "S2", "e_type": "A"},
        {"name": "b", "rule": "never", "block": "S3"},
        {"name": "c", "rule": "at_most", "block": "route_1", "count": 3},
        {"name": "d", "rule": "always", "block": "dispose_1"}]) if "PATH" in c}


# ------------------------------------------------------------------ naga check

def test_naga_check_prints_entity_time_and_one_seed(tmp_path, capsys):
    import json
    path = tmp_path / "shop.json"
    path.write_text(json.dumps(_shop([{"name": "no_s3", "rule": "never", "block": "S3"},
                                      {"name": "all_s2", "rule": "always", "block": "S2"}])))
    junit = tmp_path / "out.xml"
    assert modelcheck.main(["check", str(path), "--junit", str(junit)]) == 1
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "no_s3" in l)
    assert "[FAIL]" in line and "path_check" in line
    assert "entity" in line and "(B)" in line and "t=25" in line and "one seed" in line
    ok = next(l for l in out.splitlines() if "all_s2" in l)
    assert "[PASS]" in ok and "one seed" in ok
    cases = {c.get("name"): c for c in ET.parse(junit).getroot().iter("testcase")}
    failure = cases["no_s3"].find("failure")
    assert failure is not None and "t=25" in failure.get("message")
    assert failure.get("message").count("one seed") == 1
    assert cases["all_s2"].find("failure") is None


def test_check_returns_path_results_beside_the_others():
    results = modelcheck.check(_shop([{"name": "no_s3", "rule": "never", "block": "S3"}]))
    assert [(r.name, r.kind, r.status) for r in results] == [("no_s3", "path_check", "fail")]


def test_a_model_without_path_checks_runs_no_traced_replication(monkeypatch):
    doc = _shop([])
    del doc["path_checks"]
    monkeypatch.setattr(pathcheck, "check_paths",
                        lambda *a, **k: pytest.fail("no path checks: nothing to trace"))
    assert modelcheck.check(doc) == []


def test_the_document_keeps_the_section_and_omits_it_when_empty():
    from nagasim.core import schema
    from nagasim.core.document import ModelDocument
    checks = [{"name": "no_s3", "rule": "never", "block": "S3"}]
    assert ModelDocument(schema.migrate(_shop(checks))).to_layout()["path_checks"] == checks
    assert "path_checks" not in ModelDocument(schema.migrate(_shop([]))).to_layout()
