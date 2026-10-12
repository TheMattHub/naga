"""Event-driven HOLD conditions and MATCH by attribute (PHASE5 O4).

Exact timelines only: a HOLD condition is re-evaluated the instant anything it
reads is written, so its release time is the write time, not the next scan;
an attribute MATCH pairs by value, FIFO within a value.
"""
from nagasim.api import run_model
from nagasim.core.schema import validate_model_structured

from conftest import chain, create, dispose, layout, process


def _hold(condition, scan):
    return {"name": "gate", "description": "gate", "type": "HOLD", "position": [1, 0],
            "params": {"mode": "Condition", "conditions": [condition], "scan_interval": scan}}


def _released_at(condition, scan, *, setter_blocks=(), setter_links=(), **sections):
    """One entity waits at the HOLD from t=1; returns when it leaves."""
    waiter = create("waiter", distribution="Constant", dist_params={"value": 1000.0},
                    max_arrivals=1, start_time=1, e_type="W")
    gate, out = _hold(condition, scan), dispose("out")
    model = layout([waiter, gate, out, *setter_blocks], chain(waiter, gate, out) + list(setter_links),
                   **sections)
    log = run_model(model, replications=1, run_length=500, warmup=0)["entity_log"]
    return log.set_index("Type").loc["W", "Exited At"]


def _assign_at_7():
    setter = create("setter", distribution="Constant", dist_params={"value": 1000.0},
                    max_arrivals=1, start_time=7, e_type="S")
    setter["position"] = [0, 1]
    assign = {"name": "set_go", "description": "set_go", "type": "ASSIGN", "position": [1, 1],
              "params": {"assignments": [{"target": "go", "expr": "1"}]}}
    return [setter, assign], chain(setter, assign) + [{"source": "set_go", "target": "out"}]


def test_a_hold_condition_wakes_on_the_write_not_the_scan():
    """ASSIGN sets `go` at t=7; with a 100-minute scan the old polling released
    at 101. Now: exactly 7.0, however the variable is spelled -- and a
    condition that also reads TNOW is still woken by the write."""
    blocks, links = _assign_at_7()
    variables = [{"name": "go", "initial": 0}]
    for condition in ("go == 1", 'VAR("go") == 1', "go == 1 and TNOW >= 0"):
        assert _released_at(condition, 100.0, setter_blocks=blocks, setter_links=links,
                            variables=variables) == 7.0, condition


def test_a_hold_condition_wakes_when_the_resource_frees():
    """NR() changes on a resource event: a job holds M from 0 to 5, the waiter
    (from t=1) waits for NR("M") == 0 and leaves at exactly 5.0."""
    job = create("job", distribution="Constant", dist_params={"value": 1000.0}, max_arrivals=1)
    job["position"] = [0, 1]
    busy = process("busy", resource="M", value=5.0)
    busy["position"] = [1, 1]
    assert _released_at('NR("M") == 0', 100.0, setter_blocks=[job, busy],
                        setter_links=chain(job, busy) + [{"source": "busy", "target": "out"}],
                        resources=[{"name": "M", "capacity": 1}]) == 5.0


def test_a_clock_condition_still_scans():
    """TNOW has no event, so `scan_interval` is its fallback: TNOW >= 7.5 with
    a 2-minute scan from t=1 is first seen true at 9 -- within one scan."""
    assert _released_at("TNOW >= 7.5", 2.0) == 9.0


def _stream(name, e_type, start, order, y):
    """Four arrivals a minute apart from *start*, `order` set by expression."""
    c = create(name, distribution="Constant", dist_params={"value": 1.0}, max_arrivals=4,
               start_time=start, e_type=e_type)
    c["position"] = [0, y]
    stamp = {"name": f"{name}_order", "description": "", "type": "ASSIGN", "position": [1, y],
             "params": {"assignments": [{"target": "entity.order", "expr": order}]}}
    return c, stamp


def test_match_by_attribute_pairs_equal_values_fifo_within_a_value():
    """Stream A arrives at t=1..4 with order 2, 3, 1, 2; stream B at t=10..13
    with order 3, 2, 1, 3. A pair leaves the instant its second member arrives:

        B@10 (3) takes A@2    B@11 (2) takes A@1, the OLDER of the two 2s
        B@12 (1) takes A@3    B@13 (3) finds no A with 3 left: it waits

    so A@4 and B@13 are still in the MATCH at the end. By type alone, every
    B would have taken the oldest A (10, 11, 12, 13)."""
    a, a_stamp = _stream("a_in", "A", 1, "int(TNOW) % 3 + 1", 0)
    b, b_stamp = _stream("b_in", "B", 10, "(15 - int(TNOW)) % 3 + 1", 1)
    match = {"name": "pair", "description": "pair", "type": "MATCH", "position": [2, 0],
             "params": {"number_to_match": 2, "batch_option": False, "match_on": "entity.order"}}
    out = dispose("out")
    model = layout([a, a_stamp, b, b_stamp, match, out],
                   chain(a, a_stamp, match, out) + chain(b, b_stamp, match))
    assert validate_model_structured(model) == []
    log = run_model(model, replications=1, run_length=100, warmup=0)["entity_log"]
    left = log[log["Type"] == "A"].set_index("Created At")["Exited At"]
    assert left[1.0] == 11.0 and left[2.0] == 10.0 and left[3.0] == 12.0
    assert left.isna()[4.0]
    assert sorted(log[log["Type"] == "B"]["Exited At"].dropna()) == [10.0, 11.0, 12.0]

    model["blocks"][4]["params"]["match_on"] = "entity.2x"
    assert {p.code for p in validate_model_structured(model)} == {"E_MATCH_ATTRIBUTE"}
