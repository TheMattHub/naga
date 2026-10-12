"""PHASE6 W4 P4 (D122): the "Plan" queue discipline and `cpsat.propose`.

The discipline tests need no solver; the proposer tests `importorskip("ortools")`.
Every expected number is hand arithmetic on constant times. The key property:
a deterministic run of the proposed layout reproduces the solver's makespan.
"""
import pytest

from conftest import create, dispose, layout
from test_sequences import _route, _station, _station_process, _step
from nagasim import api
from nagasim.core.schema import validate_layout_structured


def _shop(jobs, capacity=None, releases=None, orders_extra=None, **sections):
    """Orders-mode job shop: *jobs* = {order id: [(station, time), ...]}, one
    PROCESS p_<station> seizing R_<station> per station, all FIFO."""
    stations = sorted({s for steps in jobs.values() for s, _ in steps})
    blocks = [create(name="release", arrival_mode="Orders", max_arrivals=0), _route()]
    links = [{"source": "release", "target": "route_1"}, {"source": "route_1", "target": "dispose_1"}]
    for s in stations:
        blocks += [_station(s), _station_process(f"p_{s}", f"R_{s}")]
        links += [{"source": s, "target": f"p_{s}"}, {"source": f"p_{s}", "target": "route_1"}]
    blocks.append(dispose())
    orders = [{"id": j, "release": (releases or {}).get(j, 0.0), "due": 100.0, "quantity": 1,
               "entity_type": j, "sequence": j, **(orders_extra or {}).get(j, {})} for j in jobs]
    resources = [{"name": f"R_{s}", "capacity": (capacity or {}).get(s, 1), "discipline": "FIFO"}
                 for s in stations]
    return layout(blocks, links, resources=resources, orders=orders,
                  sequences=[{"name": j, "steps": [_step(s, float(t)) for s, t in steps]}
                             for j, steps in jobs.items()],
                  simulation={"time_limit": 100, "warmup": 0, "replications": 1,
                              "deterministic": True}, **sections)


def _rows(model, resource):
    log = api.run_model(model)["schedule_log"]
    rows = log[log["Resource"] == resource].sort_values("Start")
    return list(rows["Order"]), list(rows["Start"])


# ------------------------------------------------------------ the discipline
def test_plan_keeps_the_machine_idle_for_the_planned_job():
    """A arrives at 0, B at 1; the plan puts B first. Ranking alone would
    start A at 0 (it is alone in the queue); the dispatch list holds it:
    B 1-3, A 3-7."""
    model = _shop({"A": [("M", 4)], "B": [("M", 2)]}, releases={"B": 1.0},
                  orders_extra={"A": {"plan": {"M": 3.0}}, "B": {"plan": {"M": 1.0}}})
    model["resources"][0]["discipline"] = "Plan"
    assert _rows(model, "R_M") == (["B", "A"], [1.0, 3.0])
    model["resources"][0]["discipline"] = "FIFO"               # the plan is inert without it
    assert _rows(model, "R_M") == (["A", "B"], [0.0, 4.0])


def test_unplanned_jobs_queue_after_planned_ones():
    """C has no plan: it waits behind B (planned) although it queued first."""
    model = _shop({"A": [("M", 2)], "B": [("M", 2)], "C": [("M", 2)]},
                  releases={"C": 0.5, "B": 1.0},
                  orders_extra={"A": {"plan": {"M": 0.0}}, "B": {"plan": {"M": 2.0}}})
    model["resources"][0]["discipline"] = "Plan"
    assert _rows(model, "R_M") == (["A", "B", "C"], [0.0, 2.0, 4.0])


def test_a_job_that_never_reaches_its_planned_station_does_not_stall_the_rest():
    """A is planned at N but its routing never goes there; once A leaves the
    model, B (planned after it at N) runs."""
    model = _shop({"A": [("M", 1)], "B": [("N", 1)]},
                  orders_extra={"A": {"plan": {"M": 0.0, "N": 0.0}}, "B": {"plan": {"N": 5.0}}})
    for r in model["resources"]:
        r["discipline"] = "Plan"
    assert _rows(model, "R_N") == (["B"], [1.0])


def test_plan_validation():
    model = _shop({"A": [("M", 1)]}, orders_extra={"A": {"plan": {"Nowhere": 1.0}}})
    codes = [p.code for p in validate_layout_structured(model)]
    assert "W_PLAN_STATION_UNKNOWN" in codes
    model["orders"][0]["plan"] = [1, 2]
    assert "E_ORDER_FIELD" in [p.code for p in validate_layout_structured(model)]


# ------------------------------------------------------------ the proposer
def _propose(model, **kw):
    pytest.importorskip("ortools")
    from nagasim import cpsat
    return cpsat.propose(model, **kw)


def test_proposal_is_an_ordinary_scenario():
    from nagasim.experiments import apply_overrides
    model = _shop({"A": [("M", 3)], "B": [("M", 2)]}, releases={"B": 1.0})
    result = _propose(model)
    assert result.status == "OPTIMAL" and result.makespan == pytest.approx(5.0)
    assert apply_overrides(model, result.overrides) == result.layout
    assert result.layout["resources"][0]["discipline"] == "Plan"
    assert model["resources"][0]["discipline"] == "FIFO"            # caller's dict untouched


def test_cumulative_capacity_and_replay():
    """Three 3-minute jobs on a 2-unit machine: makespan 6, and the replay agrees."""
    model = _shop({"A": [("M", 3)], "B": [("M", 3)], "C": [("M", 3)]}, capacity={"M": 2})
    result = _propose(model)
    assert result.makespan == pytest.approx(6.0)
    assert api.run_model(result.layout)["schedule_log"]["End"].max() == pytest.approx(6.0)


def test_sequence_dependent_setup_groups_like_keys():
    """Keys A, B, A (by entity type via setup_key), changeover 5, none for the
    first job: the best order pays ONE changeover, 1+1+1+5 = 8 (FIFO pays two:
    13); the deterministic replay of the plan gives 8 too."""
    model = _shop({"X": [("M", 1)], "Y": [("M", 1)], "Z": [("M", 1)]})
    for order, key in zip(model["orders"], ("A", "B", "A")):
        order["attributes"] = {"family": key}
    model["blocks"][3]["params"].update(setup="5", setup_key="entity.family")
    assert api.run_model(model)["schedule_log"]["End"].max() == pytest.approx(13.0)
    result = _propose(model)
    assert result.makespan == pytest.approx(8.0)
    assert api.run_model(result.layout)["schedule_log"]["End"].max() == pytest.approx(8.0)


def test_resource_set_members_are_alternatives():
    model = _shop({"A": [("M", 4)], "B": [("M", 4)]})
    model["resources"].append({"name": "R_M2", "capacity": 1, "discipline": "FIFO"})
    model["resource_sets"] = [{"name": "S", "members": ["R_M", "R_M2"], "rule": "PreferredOrder"}]
    model["blocks"][3]["params"]["resource_name"] = "S"
    result = _propose(model)
    assert result.makespan == pytest.approx(4.0)
    assert {r["name"]: r["discipline"] for r in result.layout["resources"]} == {
        "R_M": "Plan", "R_M2": "Plan"}


def test_weights_are_priorities_and_refusals_are_listed():
    """One machine, both due at 2: the heavier (priority 3) order goes first,
    the other is 2 late at weight 1."""
    model = _shop({"A": [("M", 2)], "B": [("M", 2)]},
                  orders_extra={"A": {"due": 2.0, "priority": 1}, "B": {"due": 2.0, "priority": 3}})
    # An ASSIGN between the station and its PROCESS: refused, still executed.
    model["blocks"].append({"name": "tag", "description": "tag", "type": "ASSIGN",
                            "position": [0, 0], "params": {"assignments": []}})
    model["links"] = [l for l in model["links"] if l["source"] != "M"] + [
        {"source": "M", "target": "tag"}, {"source": "tag", "target": "p_M"}]
    result = _propose(model, objective="weighted_tardiness")
    assert result.objective == pytest.approx(2.0)
    plan = {o["id"]: o["plan"]["M"] for o in result.layout["orders"]}
    assert plan["B"] < plan["A"]
    assert any("ASSIGN 'tag'" in r for r in result.refused)
