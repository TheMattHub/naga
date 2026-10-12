"""Independent oracle for W3-M1 "Energy per state" (docs/history/PHASE6-6D-W3-PLAN-2026-09-25.md).

Every expected number below is derived BY HAND from the spec text and, for
case 2, from queueing theory (PASTA / Little's law: a work-conserving M/G/1
server's long-run busy fraction is exactly rho = lambda * E[service],
independent of the service-time distribution). None of it comes from running
the implementation -- at the time this file was written M1 does not exist
(`schema.CURRENT_VERSION` is still 23, the plan's "v23 -> v24" bump), so every
case here is expected to fail (schema rejection of the unknown "power"/
"energy_price" keys, or a missing "energy_summary" key in the results dict).

Wire-format GUESS, reconciled centrally by the coordinator once M1 lands:
  - A resource dict takes a "power" key: {"busy", "idle", "blocked", "down",
    "off_shift"} in kW PER UNIT (capacity > 1 scales linearly, case 3).
    Absent keys are 0 kW, except "blocked", which falls back to "busy"
    (D78: a blocked unit is still seized) -- case 5.
  - The model dict takes a top-level "energy_price" (per kWh, evaluated once
    at run start, same convention as "fixed_costs[].amount").
  - `cost_summary` gains Kind == "energy" rows that join Total Cost.
  - Frame "energy_summary": Resource, one kWh column per state (5), Total
    kWh, Avg kW (= Total kWh / observed hours), Energy Cost. EMPTY with these
    columns for a model that declares no power anywhere (A3/E1 precedent).

All state columns partition the resource's unit-time: busy excludes blocked
(spec), so for a resource with no down/off-shift, busy + blocked + idle
covers every unit-hour of the observed window exactly.
"""
import pytest

from conftest import chain, create, dispose, metric_value, process
from conftest import layout as _layout
from nagasim import api

# ---------------------------------------------------------------- wire format
POWER_KEY = "power"
POWER_STATES = ("busy", "idle", "blocked", "down", "off_shift")
ENERGY_PRICE_KEY = "energy_price"          # lives under "simulation" (coordinator, wire only)
COST_KIND_ENERGY = "energy"                # cost_summary "Kind"

ENERGY_FRAME = "energy_summary"
RESOURCE_COL = "Resource"
STATE_KWH_COLS = ["Busy kWh", "Idle kWh", "Blocked kWh", "Down kWh", "Off Shift kWh"]
TOTAL_KWH_COL = "Total kWh"
AVG_KW_COL = "Avg kW"
ENERGY_COST_COL = "Energy Cost"
ENERGY_SUMMARY_COLS = ["Run", RESOURCE_COL, *STATE_KWH_COLS, TOTAL_KWH_COL, AVG_KW_COL,
                       ENERGY_COST_COL]


def layout(blocks, links, **sections):
    """Coordinator wire reconciliation, no expected value touched: `energy_price`
    lives under `simulation` (the run-wide settings' home), and a keeper arrival
    far past the limit keeps the event queue non-empty, so the KPI horizon is the
    time limit rather than the drain time (DEVLOG #32) -- the horizon the hand
    derivations below assume."""
    price = sections.pop(ENERGY_PRICE_KEY, None)
    if price is not None:
        sections["simulation"] = {**sections["simulation"], ENERGY_PRICE_KEY: price}
    keeper = [create(name="keeper_create", start_time=10**9, max_arrivals=1),
              dispose(name="keeper_dispose")]
    return _layout([*blocks, *keeper], [*links, *chain(*keeper)], **sections)


def _row(energy_df, resource):
    hit = energy_df[energy_df[RESOURCE_COL] == resource]
    assert len(hit) == 1, f"expected exactly one energy_summary row for {resource!r}"
    return hit.iloc[0]


# ---------------------------------------------------------- 1: deterministic
def test_deterministic_single_server_exact_kwh():
    """Arrivals every A=2h, service D=1h < A (no queueing), horizon T=10h
    (hour-aligned: T = N*A). N=5 cycles tile [0, 10) exactly, so busy time is
    exactly N*D=5h and idle is exactly N*(A-D)=5h -- no rounding either way."""
    A, D, N, T = 2.0, 1.0, 5, 10.0
    Pb, Pi, price = 10.0, 2.0, 0.20
    blocks = [create(dist_params={"value": A}, max_arrivals=N),
              process(resource="Server", value=D),
              dispose()]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "Server", "capacity": 1,
                              POWER_KEY: {"busy": Pb, "idle": Pi}}],
                   **{ENERGY_PRICE_KEY: price},
                   simulation={"time_limit": T, "warmup": 0, "replications": 1,
                              "time_unit": "hours"})
    results = api.run_model(model, seed=0)
    row = _row(results[ENERGY_FRAME], "Server")

    busy_kwh, idle_kwh = N * D * Pb, N * (A - D) * Pi
    total_kwh = busy_kwh + idle_kwh
    assert row["Busy kWh"] == pytest.approx(busy_kwh)
    assert row["Idle kWh"] == pytest.approx(idle_kwh)
    assert row["Blocked kWh"] == pytest.approx(0.0)
    assert row[TOTAL_KWH_COL] == pytest.approx(total_kwh)
    assert row[AVG_KW_COL] == pytest.approx(total_kwh / T)
    assert row[ENERGY_COST_COL] == pytest.approx(total_kwh * price)
    # Energy is the only cost declared -> Total Cost is exactly the energy cost.
    assert metric_value(results, "Total Cost") == pytest.approx(total_kwh * price)


# --------------------------------------------------------------- 2: M/D/1
@pytest.mark.slow
def test_mdl_long_run_matches_rho_formula():
    """M/D/1 (Poisson arrivals, deterministic service D): by PASTA + Little's
    law the long-run busy fraction of a work-conserving single server is
    EXACTLY rho = lambda*D, whatever the service distribution's shape. kWh
    should track (rho*Pb + (1-rho)*Pi) * observed_hours. 8 replications of a
    moderate horizon, warm-up excludes the startup transient; averaged to
    tame the single-run variance (generous 10% relative tolerance -- this is
    a sanity oracle, not a CLT-precise statistical test)."""
    lam, D = 0.5, 1.2
    rho = lam * D
    Pb, Pi, price = 8.0, 1.5, 0.25
    reps, T, warmup = 8, 2500.0, 250.0
    blocks = [create(distribution="Exponential", dist_params={"rate": lam}, max_arrivals=0),
              process(resource="Server", value=D),
              dispose()]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "Server", "capacity": 1,
                              POWER_KEY: {"busy": Pb, "idle": Pi}}],
                   **{ENERGY_PRICE_KEY: price},
                   simulation={"time_limit": T, "warmup": warmup, "replications": reps,
                              "time_unit": "hours"})
    results = api.run_model(model, seed=0)
    rows = results[ENERGY_FRAME]
    rows = rows[rows[RESOURCE_COL] == "Server"]
    assert len(rows) == reps

    expected_avg_kw = rho * Pb + (1 - rho) * Pi
    assert rows[AVG_KW_COL].mean() == pytest.approx(expected_avg_kw, rel=0.10)
    expected_cost_rate = expected_avg_kw * price   # Energy Cost per observed hour
    observed_hours = T - warmup
    assert rows[ENERGY_COST_COL].mean() == pytest.approx(
        expected_cost_rate * observed_hours, rel=0.10)


# --------------------------------------------------------- 3: per-unit scale
def test_two_units_capacity_two_scales_power_per_unit():
    """Two entities created together (entities_per_arrival=2) both seize the
    capacity-2 resource at t=0 (no queueing): both units busy for D=2h, then
    both idle until T=5h. Power is declared PER UNIT (spec), so busy kWh is
    Pb * 2 units * D hours, not Pb * D -- this is exactly what distinguishes
    a fleet/multi-capacity resource from a single server (case 1)."""
    D, T = 2.0, 5.0
    Pb, Pi, price = 6.0, 1.0, 0.10
    blocks = [create(entities_per_arrival=2, max_arrivals=1),
              process(resource="Server", value=D),
              dispose()]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "Server", "capacity": 2,
                              POWER_KEY: {"busy": Pb, "idle": Pi}}],
                   **{ENERGY_PRICE_KEY: price},
                   simulation={"time_limit": T, "warmup": 0, "replications": 1,
                              "time_unit": "hours"})
    results = api.run_model(model, seed=0)
    row = _row(results[ENERGY_FRAME], "Server")

    busy_kwh = Pb * 2 * D            # 2 units busy for D hours each
    idle_kwh = Pi * 2 * (T - D)      # 2 units idle for the remainder
    total_kwh = busy_kwh + idle_kwh
    assert row["Busy kWh"] == pytest.approx(busy_kwh)
    assert row["Idle kWh"] == pytest.approx(idle_kwh)
    assert row[TOTAL_KWH_COL] == pytest.approx(total_kwh)
    assert row[AVG_KW_COL] == pytest.approx(total_kwh / T)


# ------------------------------------------------------------- 4: no power
def test_model_with_no_power_gives_empty_energy_summary():
    """A model that never declares "power" or "energy_price" anywhere must
    still get the energy_summary frame back, EMPTY with its fixed columns
    (A3/E1 precedent) -- and Total Cost must be exactly what it was before
    M1 existed (0, since this model declares no other cost either)."""
    blocks = [create(dist_params={"value": 1.0}, max_arrivals=5),
              process(resource="Server", value=0.5),
              dispose()]
    model = layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                   simulation={"time_limit": 10.0, "warmup": 0, "replications": 1})
    results = api.run_model(model, seed=0)

    energy = results[ENERGY_FRAME]
    assert energy.empty
    assert list(energy.columns) == ENERGY_SUMMARY_COLS
    assert not (results["cost_summary"]["Kind"] == COST_KIND_ENERGY).any()
    assert metric_value(results, "Total Cost") == pytest.approx(0.0)


# --------------------------------------------------------- 5: blocked=busy
def test_blocked_defaults_to_busy_power():
    """Two stations, D1=1h < D2=2h, zero-capacity buffer between them, a
    saturated source (interarrival 0): M1 finishes part n, hands it straight
    to an idle M2, then immediately starts part n+1 -- but M2 (slower) is
    still busy when M1 finishes THAT one, so M1 is held BLOCKED (buffer
    capacity 0) until M2 frees up. Steady state from t=1: 1h busy, 1h
    blocked, repeating. Over the hour-aligned horizon T=11h:
      [0,1) busy(part1) [1,2) busy(part2) [2,3) blocked [3,4) busy(part3)
      [4,5) blocked [5,6) busy [6,7) blocked [7,8) busy [8,9) blocked
      [9,10) busy [10,11) blocked
    -> M1 busy 6h, blocked 5h, idle 0h (busy+blocked=11h=T, no idle: the
    source never runs dry). M1 declares only "busy" power, so per the spec's
    default rule blocked kWh uses that same rate."""
    D1, D2, T = 1.0, 2.0, 11.0
    Pb1, price = 5.0, 0.15
    blocks = [
        {"name": "src", "description": "src", "type": "CREATE", "position": [0, 0],
         "params": {"e_type": "DEFAULT", "distribution": "Constant",
                    "dist_params": {"value": 0.0}, "entities_per_arrival": 1,
                    "max_arrivals": 20, "start_time": 0}},
        process("m1", resource="M1", value=D1),
        {"name": "buf", "description": "buf", "type": "BUFFER", "position": [2, 0],
         "params": {"capacity": 0}},
        process("m2", resource="M2", value=D2),
        dispose("out"),
    ]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "M1", "capacity": 1, POWER_KEY: {"busy": Pb1}},
                             {"name": "M2", "capacity": 1}],
                   **{ENERGY_PRICE_KEY: price},
                   simulation={"time_limit": T, "warmup": 0, "replications": 1,
                              "time_unit": "hours"})
    results = api.run_model(model, seed=0)
    row = _row(results[ENERGY_FRAME], "M1")

    busy_h, blocked_h = 6.0, 5.0
    assert row["Busy kWh"] == pytest.approx(busy_h * Pb1)
    assert row["Blocked kWh"] == pytest.approx(blocked_h * Pb1)   # fallback to busy power
    assert row["Idle kWh"] == pytest.approx(0.0)
    total_kwh = (busy_h + blocked_h) * Pb1
    assert row[TOTAL_KWH_COL] == pytest.approx(total_kwh)
    assert row[ENERGY_COST_COL] == pytest.approx(total_kwh * price)


# --------------------------------------------------------- 6: warm-up reset
def test_warmup_resets_energy():
    """Same deterministic line as case 1 (A=2h, D=1h, N=5 arrivals, T=10h)
    but with warmup=4h: the observed window [4,10) is 6h long and contains
    exactly 3 full service intervals ([4,5],[6,7],[8,9], since arrivals land
    on 0,2,4,6,8 and each service is fully inside or outside the window) ->
    busy=3h, idle=3h in the OBSERVED window, not the 5h/5h of the full run."""
    A, D, N, T, warmup = 2.0, 1.0, 5, 10.0, 4.0
    Pb, Pi, price = 10.0, 2.0, 0.20
    blocks = [create(dist_params={"value": A}, max_arrivals=N),
              process(resource="Server", value=D),
              dispose()]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "Server", "capacity": 1,
                              POWER_KEY: {"busy": Pb, "idle": Pi}}],
                   **{ENERGY_PRICE_KEY: price},
                   simulation={"time_limit": T, "warmup": warmup, "replications": 1,
                              "time_unit": "hours"})
    results = api.run_model(model, seed=0)
    row = _row(results[ENERGY_FRAME], "Server")

    observed = T - warmup   # 6h
    busy_kwh, idle_kwh = 3.0 * Pb, 3.0 * Pi
    total_kwh = busy_kwh + idle_kwh
    assert row["Busy kWh"] == pytest.approx(busy_kwh)
    assert row["Idle kWh"] == pytest.approx(idle_kwh)
    assert row[AVG_KW_COL] == pytest.approx(total_kwh / observed)


# ------------------------------------------------------------ 7: time units
def test_time_unit_minutes_converts_to_hours():
    """Base time unit is minutes (simulation.time_unit default). A=60min,
    D=30min < A, N=5 arrivals, T=300min. In HOURS: A=1h, D=0.5h, T=5h --
    identical shape to case 1 scaled down, so busy=N*D=150min=2.5h and
    idle=N*(A-D)=150min=2.5h. kWh must be computed in hours (factor
    hours/minutes = 60), not in raw base-unit (minute) counts."""
    A, D, N, T_min = 60.0, 30.0, 5, 300.0
    Pb, Pi, price = 4.0, 1.0, 0.30
    blocks = [create(dist_params={"value": A}, max_arrivals=N),
              process(resource="Server", value=D),
              dispose()]
    model = layout(blocks, chain(*blocks),
                   resources=[{"name": "Server", "capacity": 1,
                              POWER_KEY: {"busy": Pb, "idle": Pi}}],
                   **{ENERGY_PRICE_KEY: price},
                   simulation={"time_limit": T_min, "warmup": 0, "replications": 1,
                              "time_unit": "minutes"})
    results = api.run_model(model, seed=0)
    row = _row(results[ENERGY_FRAME], "Server")

    busy_h, idle_h = 2.5, 2.5    # NOT 150.0 -- must be converted to hours
    busy_kwh, idle_kwh = busy_h * Pb, idle_h * Pi
    total_kwh = busy_kwh + idle_kwh
    assert row["Busy kWh"] == pytest.approx(busy_kwh)
    assert row["Idle kWh"] == pytest.approx(idle_kwh)
    assert row[AVG_KW_COL] == pytest.approx(total_kwh / (T_min / 60.0))
    assert row[ENERGY_COST_COL] == pytest.approx(total_kwh * price)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
