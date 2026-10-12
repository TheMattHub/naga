"""Independent oracle for the MOTION block (automation sub-project 1, D156-D158).

Written ONLY from docs/superpowers/specs/2026-09-29-motion-block-design.md, the
public result contract (frame "motion_summary"; trace kind "cycle_phase" with
state=segment kind, value=segment index, duration=segment time) and the model
format it documents. `nagasim/core/motion.py` and `nagasim/core/blocks/motion.py`
were never opened while writing this file -- every expected number below is
derived by hand, in each test's own docstring, straight from the spec's stated
formulas (never by calling the implementation's own helpers).

Two tiny local functions (`_oracle_derate`, `_oracle_move_time`) restate the
spec's two closed-form equations independently, so the arithmetic in a test is
not retyped decimal-by-decimal (and so a mis-keyed digit does not silently
become "the" expected value); they are not imports from the engine.
"""
import math

import pytest

from nagasim import api
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.stats import ci95
from nagasim.core.trace import TraceRecorder

from conftest import chain, create, dispose, layout, metric_value

# ---------------------------------------------------------------- oracle math
# Spec "## Timing": v = profile.speed * derate(payload) * hrc_factor; trapezoid
# t = d/v + v/a when d >= v^2/a (the move reaches v), else the triangle
# t = 2*sqrt(d/a). Spec field table: "payload_derate ... linear from 1.0 at
# 0 kg [to payload_derate] at payload_max".


def _oracle_derate(payload_kg, payload_max, floor):
    if not payload_max:
        return 1.0
    return 1.0 - (1.0 - floor) * min(payload_kg / payload_max, 1.0)


def _oracle_move_time(distance_m, speed, accel):
    if distance_m <= 0:
        return 0.0
    if distance_m >= speed * speed / accel:
        return distance_m / speed + speed / accel
    return 2.0 * math.sqrt(distance_m / accel)


def _cycle_phases(recorder, kind=None):
    events = [e for e in recorder.events if e.kind == "cycle_phase"]
    if kind is not None:
        events = [e for e in events if e.state == kind]
    return events


# ------------------------------------------------------------------ builders
def motion_block(name="motion_1", profile="P", hrc="none", segments=None, zone=None,
                  resource="Robot"):
    """A MOTION block: PROCESS's usual seize/service fields ("everything
    PROCESS has except `distribution`", spec Model format) plus the new ones."""
    params = {"action": "Seize Delay Release", "resource_name": resource, "priority": 0,
              "quantity": 1, "profile": profile, "hrc": hrc,
              "segments": segments if segments is not None else [{"kind": "dwell", "time": "1"}]}
    if zone is not None:
        params["zone"] = zone
    return {"name": name, "description": name, "type": "MOTION", "position": [1, 0],
            "params": params}


def _run(doc, seed=0, trace=None):
    return api.run_model(doc, replications=1, seed=seed, trace=trace)


# Seconds: the oracle states every expected value in seconds, the spec's unit for moves.
SIM_100 = {"time_limit": 100, "warmup": 0, "replications": 1, "time_unit": "seconds"}


# ------------------------------------------------------------------- Task 8.1
def test_trapezoid_full_speed():
    """Spec worked example: v=1.0 m/s, a=2.0 m/s^2 -> v^2/a = 0.5 m. d=0.8 m
    is >= 0.5 m, so the move reaches v: t = d/v + v/a = 0.8/1.0 + 1.0/2.0
    = 1.3 s."""
    profile = {"name": "P", "speed": 1.0, "accel": 2.0}
    blocks = [create(max_arrivals=1), motion_block(segments=[{"kind": "move", "distance_m": 0.8}]),
              dispose()]
    doc = layout(blocks, chain(*blocks), motion_profiles=[profile],
                 resources=[{"name": "Robot", "capacity": 1}], simulation=SIM_100)
    recorder = TraceRecorder()
    _run(doc, trace=recorder)
    moves = _cycle_phases(recorder, "move")
    assert len(moves) == 1
    assert moves[0].duration == pytest.approx(1.3)
    assert moves[0].value == pytest.approx(0)  # segment index 0


# ------------------------------------------------------------------- Task 8.2
def test_trapezoid_triangle():
    """Same profile (v^2/a = 0.5 m); d=0.2 m is below it, so the move never
    reaches v: t = 2*sqrt(d/a) = 2*sqrt(0.2/2.0) = 2*sqrt(0.1) =~ 0.632455 s."""
    profile = {"name": "P", "speed": 1.0, "accel": 2.0}
    blocks = [create(max_arrivals=1), motion_block(segments=[{"kind": "move", "distance_m": 0.2}]),
              dispose()]
    doc = layout(blocks, chain(*blocks), motion_profiles=[profile],
                 resources=[{"name": "Robot", "capacity": 1}], simulation=SIM_100)
    recorder = TraceRecorder()
    _run(doc, trace=recorder)
    moves = _cycle_phases(recorder, "move")
    assert len(moves) == 1
    assert moves[0].duration == pytest.approx(2.0 * math.sqrt(0.1))


# ------------------------------------------------------------------- Task 8.3
def test_payload_derating():
    """One profile, payload_max=12.5 kg, payload_derate=0.6 (the spec's own
    field example); three moves of distance_m=10 (always trapezoid: even the
    slowest derated speed, 0.6, gives v^2/a = 0.18 << 10) at payload 0 kg
    (factor 1.0), 6.25 kg (half of payload_max -> factor (1+0.6)/2 = 0.8) and
    12.5 kg (the floor -> factor 0.6):

    0 kg:    v=1.0 -> t = 10/1.0 + 1.0/2.0 = 10.5
    6.25 kg: v=0.8 -> t = 10/0.8 + 0.8/2.0 = 12.9
    12.5 kg: v=0.6 -> t = 10/0.6 + 0.6/2.0 =~ 16.966667
    """
    profile = {"name": "P", "speed": 1.0, "accel": 2.0, "payload_max": 12.5, "payload_derate": 0.6}
    segments = [{"kind": "move", "distance_m": 10, "payload_kg": payload}
                for payload in (0, 6.25, 12.5)]
    blocks = [create(max_arrivals=1), motion_block(segments=segments), dispose()]
    doc = layout(blocks, chain(*blocks), motion_profiles=[profile],
                 resources=[{"name": "Robot", "capacity": 1}], simulation=SIM_100)
    recorder = TraceRecorder()
    _run(doc, trace=recorder)
    moves = _cycle_phases(recorder, "move")
    assert len(moves) == 3
    expected = [_oracle_move_time(10, 1.0 * _oracle_derate(p, 12.5, 0.6), 2.0)
                for p in (0, 6.25, 12.5)]
    assert expected == pytest.approx([10.5, 12.9, 10.0 / 0.6 + 0.3])
    assert [m.duration for m in moves] == pytest.approx(expected)


# ------------------------------------------------------------------- Task 8.4
def test_collaborative_mode():
    """hrc="collaborative": every move always uses collaborative_speed, "still
    derated" (D158) by payload exactly like the nominal speed would be.
    collaborative_speed=0.25, payload_derate=0.6, payload_max=12.5, payload=5:
    factor = 1 - 0.4*(5/12.5) = 1 - 0.16 = 0.84, v = 0.25*0.84 = 0.21.
    v^2/a = 0.0441/2 = 0.02205 << d=0.8, so trapezoid:
    t = 0.8/0.21 + 0.21/2.0 =~ 3.914524 s."""
    profile = {"name": "P", "speed": 1.0, "accel": 2.0, "collaborative_speed": 0.25,
               "payload_max": 12.5, "payload_derate": 0.6}
    blocks = [create(max_arrivals=1),
              motion_block(hrc="collaborative",
                          segments=[{"kind": "move", "distance_m": 0.8, "payload_kg": 5}]),
              dispose()]
    doc = layout(blocks, chain(*blocks), motion_profiles=[profile],
                 resources=[{"name": "Robot", "capacity": 1}], simulation=SIM_100)
    recorder = TraceRecorder()
    _run(doc, trace=recorder)
    moves = _cycle_phases(recorder, "move")
    assert len(moves) == 1
    v = 0.25 * _oracle_derate(5, 12.5, 0.6)
    assert moves[0].duration == pytest.approx(_oracle_move_time(0.8, v, 2.0))


# ------------------------------------------------------------------- Task 8.5
# Separation mode (D158): collaborative speed for a move "if any worker-fleet
# unit is inside the zone when the move starts", "measured by network
# distance from the zone node". A parked worker-fleet unit that nothing ever
# seizes stays at its "home" node for the whole run (fleets are homed-at-rest
# until a job takes them -- the same mechanic PROCESS/SEIZE workers already
# use), so its distance to the zone is fixed and known up front.
#
# Assumption stated here (not in the spec in so many words, per the plan's own
# review focus): a worker sitting EXACTLY at radius_m counts as inside (a
# closed "<= radius_m" zone) -- the natural reading of "inside the zone" at
# its own boundary. A worker whose home node has no path TO the zone node
# (segment usable only zone -> node, never node -> zone) cannot reach the
# zone at all and counts as outside regardless of the numeric distance a
# straight line would give -- this is the whole reason distance is "network
# distance", not Euclidean.
def _zone_model(node_name, x, y, *, reachable=True):
    network = {"nodes": [{"name": "Z", "x": 0, "y": 0}, {"name": node_name, "x": x, "y": y}],
               "segments": [{"from": "Z", "to": node_name, "two_way": reachable}]}
    fleet = {"name": "People", "kind": "worker", "speed": 1.0, "home": node_name,
             "units": [{"name": "Sam"}]}
    profile = {"name": "P", "speed": 1.0, "accel": 2.0, "collaborative_speed": 0.25}
    blocks = [create(max_arrivals=1),
              motion_block(hrc="separation", zone={"node": "Z", "radius_m": 1.5},
                          segments=[{"kind": "move", "distance_m": 0.8}]),
              dispose()]
    return layout(blocks, chain(*blocks), motion_profiles=[profile],
                  resources=[{"name": "Robot", "capacity": 1}], simulation=SIM_100,
                  network=network, fleets=[fleet])


@pytest.mark.parametrize("node_name, coords, reachable, expect_collaborative", [
    ("inside", (1.0, 0.0), True, True),        # distance 1.0 < 1.5: inside
    ("at_radius", (1.5, 0.0), True, True),     # distance 1.5 == 1.5: inside (assumption above)
    ("outside", (5.0, 0.0), True, False),      # distance 5.0 > 1.5: outside
    ("unreachable", (1.0, 0.0), False, False),  # numerically 1.0, but no path back to Z: outside
])
def test_separation_worker_in_and_out(node_name, coords, reachable, expect_collaborative):
    doc = _zone_model(node_name, coords[0], coords[1], reachable=reachable)
    recorder = TraceRecorder()
    _run(doc, trace=recorder)
    moves = _cycle_phases(recorder, "move")
    assert len(moves) == 1
    expected = (_oracle_move_time(0.8, 0.25, 2.0) if expect_collaborative
                else _oracle_move_time(0.8, 1.0, 2.0))
    assert moves[0].duration == pytest.approx(expected)


# ------------------------------------------------------------------- Task 8.6
@pytest.mark.slow
def test_deterministic_cycle_is_an_md1_service():
    """One robot (capacity 1), Poisson arrivals (rate lambda=0.5), a MOTION
    cycle that is the SAME every time -- one "dwell" segment whose "time" is
    the plain literal "1" (a fixed value, not a distribution draw) -- is an
    M/D/1 queue with deterministic service D=1.0. rho = lambda*D = 0.5 <= 0.6.

    Pollaczek-Khinchine mean time IN SYSTEM (matches the KPI "Avg Throughput
    Time", the same metric the existing M/M/1 oracle in test_oracles.py
    compares against 1/(mu-lambda)):
        W = D + lambda*D^2 / (2*(1 - rho))
          = 1.0 + 0.5*1.0 / (2*0.5) = 1.0 + 0.5 = 1.5
    """
    lam, service = 0.5, 1.0
    rho = lam * service
    assert rho <= 0.6
    theory_w = service + lam * service ** 2 / (2.0 * (1.0 - rho))
    assert theory_w == pytest.approx(1.5)

    profile = {"name": "P", "speed": 1.0, "accel": 1.0}
    blocks = [create(distribution="Exponential", dist_params={"rate": lam}, max_arrivals=0),
              motion_block(segments=[{"kind": "dwell", "time": "1"}]), dispose()]
    doc = layout(blocks, chain(*blocks), motion_profiles=[profile],
                 resources=[{"name": "Robot", "capacity": 1}])
    # Seconds everywhere: arrivals per second and the dwell's "1" are the same unit.
    doc.setdefault("simulation", {})["time_unit"] = "seconds"

    replications, run_len, warmup = 10, 2000, 200
    times = [metric_value(run_single_simulation(doc, seed=seed, run_len=run_len, warmup=warmup),
                          "Avg Throughput Time")
             for seed in range(replications)]
    stats = ci95(times, confidence=0.99)
    assert stats["lb"] <= theory_w <= stats["ub"], (
        f"M/D/1 theory {theory_w} outside 99% CI [{stats['lb']}, {stats['ub']}] "
        f"(mean {stats['mean']})")
    assert (stats["ub"] - stats["mean"]) < 0.5 * theory_w, "CI too wide to be a useful oracle"


# ------------------------------------------------------------------- Task 8.7
def _pick_model(distance_m):
    """CREATE (Constant gap, finite count) -> MOTION (a fixed move + a
    TRIA "pick", spec's own example distribution) -> DISPOSE. The move is
    never a random draw; only "pick" consumes the block's random stream."""
    profile = {"name": "P", "speed": 1.0, "accel": 2.0}
    segments = [{"kind": "move", "distance_m": distance_m}, {"kind": "pick", "time": "TRIA(0.8,1.0,1.4)"}]
    blocks = [create(max_arrivals=5, dist_params={"value": 10.0}), motion_block(segments=segments),
              dispose()]
    return layout(blocks, chain(*blocks), motion_profiles=[profile],
                 resources=[{"name": "Robot", "capacity": 1}], simulation=SIM_100)


def _pick_durations(doc, seed):
    recorder = TraceRecorder()
    _run(doc, seed=seed, trace=recorder)
    return [e.duration for e in _cycle_phases(recorder, "pick")]


def test_reproducible_for_a_seed_and_different_across_seeds():
    """Spec Timing: other segments "draw from their distribution on the
    block's random stream in segment order (reproducible ...)". Same model,
    same seed, twice: the 5 entities' "pick" draws must be bit-identical.
    A different seed must not reproduce them -- 5 independent TRIA draws all
    matching by chance is negligible."""
    doc = _pick_model(0.8)
    first = _pick_durations(doc, seed=0)
    second = _pick_durations(doc, seed=0)
    third = _pick_durations(doc, seed=1)
    assert len(first) == 5
    assert first == second
    assert first != third


def test_common_random_numbers_across_scenarios():
    """Spec Timing: "... common random numbers across scenarios as for
    PROCESS". Two layouts differ ONLY in the move's distance_m (0.8 vs 5.0),
    a value that never draws from the RNG -- so the "pick" segments, which
    do, must get identical draws in both scenarios at the same seed."""
    near = _pick_durations(_pick_model(0.8), seed=0)
    far = _pick_durations(_pick_model(5.0), seed=0)
    assert len(near) == 5
    assert near == far
