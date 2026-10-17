"""State in, third round (D381 to D389): blocking after service, finite buffers, fleets and
vehicles. The exit check is the one of `test_state.py`: a run started from a state taken at
t is the uninterrupted run with a warm-up of t."""
import numpy as np
import pytest

from conftest import chain, create, dispose, layout, metric_value, problem_codes, process, \
    resource_row
from test_state import _block, _bundled, _carried, _kpis, _refusal

from nagasim import api, state

TWO = [{"name": "A", "capacity": 1}, {"name": "B", "capacity": 1}]


# ---------------------------------------------------------------- blocking and buffers
def _buffered(capacity=1, *, slow=4.8, queue_capacity=0, time_limit=60, **m2):
    """Arrivals every 2 from 0.5, a station of 1.0, a buffer, a slower station: the buffer
    fills, the first station blocks and its own queue grows. Nothing random."""
    c = create("c", dist_params={"value": 2.0}, start_time=0.5)
    m1, m2, d = process("m1", resource="A"), process("m2", resource="B", value=slow, **m2), \
        dispose("d")
    if queue_capacity:
        m2["params"]["queue_capacity"] = queue_capacity
        blocks = [c, m1, m2, d]
    else:
        blocks = [c, m1, _block("buf", "BUFFER", capacity=capacity), m2, d]
    return layout(blocks, chain(*blocks), resources=TWO,
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})


def test_one_machine_blocked_by_a_buffer_of_one_by_hand():
    """Arrivals at 0.5, 2.5, 4.5 ...; m1 takes 1.0, m2 takes 4.8, one place between them.

        e1  m1 0.5-1.5                      m2  1.5- 6.3
        e2  m1 2.5-3.5   buffer  3.5- 6.3   m2  6.3-11.1
        e3  m1 4.5-5.5   blocked 5.5- 6.3   buffer  6.3-11.1   m2 11.1-15.9
        e4  m1 6.5-7.5   blocked 7.5-11.1   buffer 11.1-15.9   m2 15.9-20.7
        e5  arrives 8.5, m1 11.1-12.1, blocked 12.1-15.9
        e6  arrives 10.5, m1 15.9-16.9, blocked from 16.9

    At 9.0: e2 in service until 11.1; e3 waits at m2 since 6.3 in the buffer's place; e4
    waits for that place, held by m1 since 6.5; e5 waits at m1 since 8.5; five arrivals
    made, the next at 10.5. From there to 20: e2 and e3 leave (11.1, 15.9), and m1 and m2
    hold a part the whole time (m1: e4 to 11.1, e5 to 15.9, then e6)."""
    model = _buffered(time_limit=20)
    doc = state.take(model, 9.0)
    entities = [{k: v for k, v in e.items() if k not in ("attributes", "e_type", "count", "age")}
                for e in doc["state"]["entities"]]
    expected = [
        {"block": "m2", "in_service": 1, "ends": [pytest.approx(11.1)], "since": pytest.approx(6.3)},
        {"block": "m1", "since": 8.5},
        {"block": "m2", "held_by": "buf", "since": pytest.approx(6.3)},
        {"block": "buf", "held_by": "m1", "held_since": 6.5, "place": False}]
    assert len(entities) == 4 and all(e in entities for e in expected)     # in any order
    assert doc["state"]["arrivals"] == {"c": {"count": 5, "next": 10.5, "scheduled": 8.5}}
    started, problems = state.start(model, doc)
    assert problems == []
    kpis = api.run_model(started)
    assert metric_value(kpis, "Total Entities Completed") == 2
    assert resource_row(kpis, "A")["Idle %"] == pytest.approx(0.0)
    assert resource_row(kpis, "B")["Idle %"] == pytest.approx(0.0)


@pytest.mark.parametrize("model", [_buffered(1), _buffered(2), _buffered(0),
                                   _buffered(queue_capacity=1), _buffered(queue_capacity=2)],
                         ids=["one place", "two places", "no place", "queue of 1", "queue of 2"])
@pytest.mark.parametrize("at", [5.75, 9.0, 13.3, 31.7])
def test_a_blocked_machine_and_a_full_buffer_are_reproduced_exactly(model, at):
    taken = _carried(model, at)
    assert at < 13 or any("held_by" in e for e in taken["entities"])


def test_entities_waiting_for_a_place_nobody_holds_keep_their_order():
    """A source straight into a finite queue: the ones outside it are held by no machine."""
    c = create("c", dist_params={"value": 1.0}, start_time=0.5)
    m, d = process("m", resource="A", value=2.6, queue_capacity=2), dispose("d")
    model = layout([c, m, d], chain(c, m, d), resources=TWO,
                   simulation={"time_limit": 60, "warmup": 0, "replications": 1})
    taken = _carried(model, 20.2)
    outside = [e for e in taken["entities"] if e.get("place") is False]
    assert len(outside) > 2 and not any("held_by" in e for e in outside)
    assert [e["age"] for e in outside] == sorted((e["age"] for e in outside), reverse=True)


def test_two_machines_blocked_on_one_buffer_are_served_in_the_order_they_blocked():
    a, b = create("a", dist_params={"value": 2.0}, start_time=0.5), \
        create("b", dist_params={"value": 2.0}, start_time=1.25)
    ma, mb = process("ma", resource="A"), process("mb", resource="B", value=0.5)
    buf, m, d = _block("buf", "BUFFER", capacity=1), process("m", resource="C", value=3.3), \
        dispose("d")
    model = layout([a, b, ma, mb, buf, m, d], chain(a, ma, buf, m, d) + chain(b, mb, buf),
                   resources=TWO + [{"name": "C", "capacity": 1}],
                   simulation={"time_limit": 60, "warmup": 0, "replications": 1})
    for at in (9.0, 20.6):
        taken = _carried(model, at)
        assert {e.get("held_by") for e in taken["entities"]} >= {"ma", "mb", "buf"}


def test_a_transparent_block_between_a_machine_and_its_buffer_runs_once():
    """An ASSIGN after the blocked station: put back where it waits, the part is not
    assigned a second time (D381)."""
    model = _buffered()
    count = _block("count", "ASSIGN", assignments=[{"target": "seen", "expr": "seen + 1"}])
    model["blocks"].insert(2, count)
    model["links"] = chain(*model["blocks"])
    model["variables"] = [{"name": "seen", "initial": 0}]
    taken = _carried(model, 9.0)
    assert taken["variables"] == {"seen": 4}


@pytest.mark.parametrize("at", [250.3, 601.7, 1200.4])
def test_the_bundled_line_with_buffers_is_reproduced_exactly(at):
    taken = _carried(_bundled("line_with_buffers", deterministic=True), at)
    assert taken["entities"]


def test_a_line_with_buffers_is_reproduced_in_distribution():
    """As `test_state`: the state of seed s started with another seed against the run of
    seed s, 30 seeds, each mean difference inside its 99% interval."""
    model = _buffered(2, slow=1.7, time_limit=400)
    for name in ("m1", "m2"):
        block = next(b for b in model["blocks"] if b["name"] == name)
        mean = block["params"]["dist_params"]["value"]
        block["params"].update(distribution="Exponential", dist_params={"rate": 1.0 / mean})
    kpis = ("entity/DEFAULT/Avg Time", "resource/A/Idle %", "system/Total Entities Completed")
    differences = {kpi: [] for kpi in kpis}
    for seed in range(30):
        warm, problems = state.start(model, state.take(model, 150.0, seed=seed))
        assert problems == []
        reference = _kpis(api.run_model(model, warmup=150.0, replications=1, seed=seed))
        started = _kpis(api.run_model(warm, replications=1, seed=1000 + seed))
        for kpi in kpis:
            differences[kpi].append(started[kpi] - reference[kpi])
    for kpi, values in differences.items():
        mean, error = np.mean(values), np.std(values, ddof=1) / np.sqrt(len(values))
        assert abs(mean) <= 2.756 * error and error > 0, kpi


def test_blocking_before_service_and_a_balking_finite_queue_are_refused_by_name():
    model = _buffered()
    model["blocks"][1]["params"]["hold_until_space"] = True
    assert "blocking before service" in _refusal(model, 9.0)
    c = create("c", dist_params={"value": 1.0}, start_time=0.5)
    m, d, out = process("m", resource="A", value=2.6, queue_capacity=1, renege_after="50"), \
        dispose("d"), dispose("out")
    model = layout([c, m, d, out], chain(c, m, d) + chain(m, out), resources=TWO,
                   simulation={"time_limit": 60, "warmup": 0, "replications": 1})
    assert "finite queue that balks or reneges" in _refusal(model, 20.2)


_HELD = {"block": "m2", "count": 1, "held_by": "buf"}


@pytest.mark.parametrize("entities, code", [
    ([{**_HELD, "held_by": "nobody"}], "E_INITIAL_BLOCK"),
    ([{**_HELD, "held_by": 3}], "E_INITIAL_BLOCK"),
    ([{**_HELD, "held_by": "m2"}], "E_INITIAL_BLOCK"),               # it does not block
    ([{**_HELD, "block": "m1"}], "E_INITIAL_BLOCK"),                 # not after the buffer
    ([{**_HELD, "count": 2}], "E_INITIAL_BLOCK"),
    ([_HELD, _HELD], "E_INITIAL_QUEUE_CAPACITY"),                    # two in one place
    ([{"block": "buf", "count": 2}], "E_INITIAL_QUEUE_CAPACITY"),
    ([{"block": "m1", "count": 1, "place": False}], "E_INITIAL_BLOCK"),   # m1 has no places
    ([{"block": "buf", "count": 1, "place": "no"}], "E_INITIAL_BLOCK"),
    ([{"block": "buf", "count": 1, "place": False, "held_by": "m1", "held_since": 99.0}],
     "E_INITIAL_REMAINING"),
    ([{"block": "buf", "count": 1, "place": False, "held_by": "m1"},
      {"block": "m1", "count": 1, "in_service": 1}], "E_INITIAL_IN_SERVICE"),   # one unit
], ids=range(11))
def test_a_bad_place_or_holder_is_refused(entities, code):
    model = _buffered()
    model["initial_state"] = {"clock": 9.0, "entities": entities}
    assert code in problem_codes(model)


def test_a_part_observed_in_a_buffer_arrives_there_again():
    """What a plant says (D382): an entity AT a buffer has one of its places."""
    model = _buffered(time_limit=20)
    model["initial_state"] = {"entities": [{"block": "m2", "count": 1, "in_service": 1,
                                            "remaining": [5.0]}, {"block": "buf", "count": 1}]}
    assert problem_codes(model) == set()
    # m2 takes them one after the other, then the first arrival: 5, 9.8, 14.6, 19.4
    assert metric_value(api.run_model(model), "Total Entities Completed") == 4


# ---------------------------------------------------------------- fleets and vehicles
NET = {"nodes": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 20, "y": 0},
                 {"name": "c", "x": 20, "y": 15}],
       "segments": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]}


def _station(name, node):
    return _block(name, "STATION", node=node)


def _yard(count=1, *, every=30.0, second=None, time_limit=120, chargers=(), **fleet):
    """Loads at a every *every* from 0.5, carried to b by a fleet of *count*: 2 m per time
    unit loaded (10 for the 20 m), 4 empty (5), 1.5 to load, 1 to unload. With *second*,
    loads at c every *second* from 1.25, carried to a. Nothing random."""
    c1 = create("c1", dist_params={"value": every}, start_time=0.5)
    sa, sb, d = _station("S_A", "a"), _station("S_B", "b"), dispose("d")
    move = _block("move", "TRANSPORT", fleet="F", destination="S_B")
    blocks, links = [c1, sa, move, sb, d], chain(c1, sa, move, sb, d)
    if second:
        c2 = create("c2", e_type="BACK", dist_params={"value": second}, start_time=1.25)
        sc, back = _station("S_C", "c"), _block("back", "TRANSPORT", fleet="F", destination="S_A2")
        sa2, d2 = _station("S_A2", "a"), dispose("d2")
        blocks += [c2, sc, back, sa2, d2]
        links += chain(c2, sc, back, sa2, d2)
    spec = {"name": "F", "count": count, "speed": 2.0, "speed_empty": 4.0, "load_time": "1.5",
            "unload_time": "1", "home": "a", **fleet}
    return layout(blocks, links, network=NET, fleets=[spec], chargers=list(chargers),
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})


def test_one_vehicle_on_a_two_node_track_by_hand():
    """One vehicle, home a; loads at a at 0.5 and 30.5 for b, 20 m away.

        load 1  the vehicle is at a: loads 0.5-2.0, carries 2.0-12.0, unloads 12.0-13.0
                and stays at b
        load 2  asks at 30.5: the vehicle drives back empty 30.5-35.5, loads 35.5-37.0,
                carries 37.0-47.0, unloads 47.0-48.0

    At 33.0 it is on its way from b to a since 30.5 for the pickup; at 36.0 it loads until
    37.0; at 40.0 it carries since 37.0 with an unload of 1.0 drawn; at 47.5 it unloads
    until 48.0. Started from any of them, load 2 leaves at 48.0, the only one of the
    window."""
    model = _yard(time_limit=60)
    trip = {"origin": "a", "destination": "b", "target": "S_B", "unit": 0}
    for at, vehicle, stage in [
            (33.0, {"node": "b", "idle_since": 13.0, "drive": {"to": "a", "started": 30.5}},
             {"stage": "pickup"}),
            (36.0, {"node": "a", "idle_since": 13.0, "until": 37.0}, {"stage": "load"}),
            (40.0, {"node": "a", "idle_since": 13.0, "drive": {"to": "b", "started": 37.0}},
             {"stage": "carry", "unload": 1.0}),
            (47.5, {"node": "b", "idle_since": 13.0, "until": 48.0}, {"stage": "unload"})]:
        doc = state.take(model, at)
        assert doc["state"]["fleets"] == {"F": {"vehicles": [vehicle]}}
        (entity,) = doc["state"]["entities"]
        assert entity["block"] == "move" and entity["trip"] == {**trip, **stage}
        started, problems = state.start(model, doc)
        assert problems == []
        log = api.run_model(started)["entity_log"]
        assert list(log["Exited At"]) == [48.0]


@pytest.mark.parametrize("model", [
    _yard(), _yard(2, every=7.0, second=9.0), _yard(2, every=7.0, second=9.0, dispatch="nearest"),
    _yard(3, every=5.0, second=6.0, select="cyclic"), _yard(2, every=11.0, second=13.0, park="home"),
    _yard(1, every=6.0, second=8.0, time_limit=90)],
    ids=["one", "two", "nearest", "cyclic", "park home", "overloaded"])
@pytest.mark.parametrize("at", [33.0, 36.3, 40.2, 47.6, 58.9])
def test_a_fleet_at_work_is_reproduced_exactly(model, at):
    if model["fleets"][0].get("park") == "home" and at == 33.0:
        # called back from its way home: refused for the whole diverted drive to the pickup
        assert "two tasks" in _refusal(model, at)
        return
    taken = _carried(model, at)
    assert taken["fleets"]["F"]["vehicles"]


@pytest.mark.parametrize("at", [20.3, 66.6, 93.2, 131.4])
def test_taking_a_state_leaves_the_paused_run_as_it_was(at):
    """Reading a state settles each vehicle's battery up to now (`Mover.settle`): a session
    that goes on after it ends as the one nobody read, by golden hash (150 take-times of
    the two battery fleets were scanned on 2026-10-13, none differed)."""
    from tools.make_golden import golden_hash, payload_of
    from nagasim.core.session import SimSession

    model = _electric()

    def run(read: bool) -> str:
        tap = state.Tap()
        session = SimSession(model, 0, subscribers=(tap,))
        session.advance(to=at)
        if read:
            assert state._read(session, tap)["fleets"]
        session.advance()
        return golden_hash(payload_of(session.finish()))

    assert run(True) == run(False)


def test_the_dispatch_queue_keeps_its_order():
    taken = _carried(_yard(1, every=6.0, second=8.0, time_limit=90), 58.9)
    waiting = [e for e in taken["entities"] if "trip" in e and "unit" not in e["trip"]]
    assert len(waiting) > 3 and [e["since"] for e in waiting] == sorted(e["since"] for e in waiting)


@pytest.mark.parametrize("at", [20.0, 32.0, 40.0, 44.0])
def test_a_queue_the_last_nearest_grant_reordered_is_put_back_in_that_order(at):
    """Under "nearest" the queue stands in the distance order of its last grant until the
    next request, and the next grant breaks a distance tie by it. At 40 it is [the load at
    a asked 2.0, the load at c asked 1.75] and the next vehicle frees at b, 20 from both."""
    model = _yard(2, every=0.5, second=0.5, dispatch="nearest", time_limit=150)
    model["network"] = {"nodes": [{"name": "a", "x": 0, "y": 0}, {"name": "b", "x": 20, "y": 0},
                                  {"name": "c", "x": 40, "y": 0}],
                        "segments": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]}
    model["blocks"][0]["params"]["max_arrivals"] = 4
    model["blocks"][5]["params"].update(max_arrivals=3, start_time=0.75)
    taken = _carried(model, at)
    if at == 40.0:
        assert [e["since"] for e in taken["entities"] if "trip" in e and "since" in e] == [2.0, 1.75]


BATTERY = {"capacity_kwh": 10.0, "low": 0.5, "target": 0.9}
DOCK = [{"name": "dock", "node": "c", "points": 1, "power_kw": 30.0}]


def _electric(battery=BATTERY):
    model = _yard(2, every=9.0, second=11.0, time_limit=240, chargers=DOCK,
                  power={"busy": 30.0, "idle": 2.0}, battery=battery)
    model["simulation"]["time_unit"] = "minutes"
    return model


@pytest.mark.parametrize("battery", [BATTERY, {**BATTERY, "low": 0.2, "strategy": "opportunity",
                                               "opportunity_below": 0.8}],
                         ids=["threshold", "opportunity"])
@pytest.mark.parametrize("at", [20.3, 41.7, 66.6, 93.2, 131.4, 170.9])
def test_a_battery_fleet_and_its_charger_are_reproduced_exactly(battery, at):
    taken = _carried(_electric(battery), at)
    assert all("soc" in v for v in taken["fleets"]["F"]["vehicles"])


@pytest.mark.parametrize("battery, stages", [
    (BATTERY, {"drive", "wait", "charge"}),
    ({**BATTERY, "low": 0.2, "strategy": "opportunity", "opportunity_below": 0.8},
     {"drive", "charge"})], ids=["threshold", "opportunity"])
def test_every_stage_of_a_charge_is_met_and_reproduced(battery, stages):
    """The times are found, not chosen: one take-time per stage of a charge."""
    model, found = _electric(battery), {}
    for at in np.arange(10.3, 230.0, 1.7):
        try:
            taken = state.take(model, float(at))["state"]
        except state.StateError:
            continue
        for vehicle in taken["fleets"]["F"]["vehicles"]:
            if "charge" in vehicle:
                found.setdefault(vehicle["charge"]["stage"], float(at))
    assert stages <= set(found)
    for at in found.values():
        _carried(model, at)


@pytest.mark.parametrize("change, kind", [
    ({"motion": "gantry", "speed_x": 1.0, "speed_y": 1.0}, "a gantry"),
    ({"loads": 3}, "a vehicle on a tour")])
def test_what_a_vehicle_record_cannot_say_is_refused_by_name(change, kind):
    model = _yard(1, every=3.0, second=4.0, **change)
    refused = set()
    for at in (20.3, 33.3, 47.1):
        try:
            state.take(model, at)
        except state.StateError as error:
            refused.add(error.message)
    assert any(kind in message for message in refused)


def _fleet_state(vehicle=None, trip=None, **entry):
    vehicle = vehicle or {"node": "a"}
    said = {"clock": 9.0, "fleets": {"F": {"vehicles": [vehicle]}}}
    if trip is not None:
        said["entities"] = [{"block": "move", "count": 1, "trip": trip, **entry}]
    return said


_TRIP = {"origin": "a", "destination": "b", "target": "S_B"}


@pytest.mark.parametrize("said", [
    {"clock": 9.0, "fleets": {"G": {"vehicles": [{"node": "a"}]}}},
    {"clock": 9.0, "fleets": {"F": {"vehicles": []}}},
    {"clock": 9.0, "fleets": {"F": {"vehicles": [{"node": "a"}], "cursor": 4}}},
    {"clock": 9.0, "fleets": ["F"]},
    _fleet_state({"node": "nowhere"}),
    _fleet_state({"node": "a", "idle_since": 12.0}),
    _fleet_state({"node": "a", "soc": 0.5}),                          # no battery
    _fleet_state({"node": "a", "drive": {"to": "a", "started": 3.0}}),
    _fleet_state({"node": "a", "drive": {"to": "b", "started": 30.0}}),
    _fleet_state({"node": "a", "until": 3.0}),
    _fleet_state({"node": "a", "until": 12.0}),                       # handling nothing
    _fleet_state({"node": "a", "charge": {"charger": "dock", "until": 12.0}}),
    _fleet_state(trip={**_TRIP, "origin": "nowhere"}),
    _fleet_state(trip={**_TRIP, "target": "move"}),
    _fleet_state(trip={**_TRIP, "unit": 3}),
    _fleet_state(trip={**_TRIP, "unit": 0, "stage": "fly"}),
    _fleet_state(trip={**_TRIP, "unit": 0, "stage": "unload"}),       # not at the destination
    _fleet_state(trip={**_TRIP, "unit": 0, "unload": -1}),
    _fleet_state(trip=_TRIP, count=2),
    _fleet_state(trip="soon"),
], ids=range(20))
def test_a_bad_fleet_record_or_trip_is_refused_with_its_code(said):
    model = _yard()
    model["initial_state"] = said
    assert "E_INITIAL_FLEET" in problem_codes(model)


@pytest.mark.parametrize("said", [
    _fleet_state(trip={**_TRIP, "target": ["S_B"]}),
    _fleet_state(trip={**_TRIP, "target": {"S_B": 1}}),
    _fleet_state({"node": "a", "charge": {"charger": ["dock"]}}),
    _fleet_state({"node": ["a"]}),
    _fleet_state({"node": "a", "drive": {"to": ["b"], "started": 3.0}}),
    _fleet_state(trip={**_TRIP, "origin": ["a"]}),
    {"clock": 9.0, "fleets": {"F": {"vehicles": [{"node": "a"}]}},
     "entities": [{"block": "move", "count": 1, "trip": _TRIP, "held_by": ["buf"]}]},
], ids=range(7))
def test_a_name_that_is_no_text_is_refused_with_its_code_never_raised(said):
    model = _yard(chargers=DOCK, battery=BATTERY)
    model["initial_state"] = said
    assert "E_INITIAL_FLEET" in problem_codes(model)
    doc = state.document(model, said)                   # and as a state document
    started, problems = state.start(model, doc)
    assert started is None and "E_INITIAL_FLEET" in {p.code for p in problems}


_PLUGGED = {"node": "c", "soc": 0.4, "charge": {"charger": "dock", "until": 12.0}}


@pytest.mark.parametrize("vehicles, fits", [
    ([_PLUGGED], True),
    ([{**_PLUGGED, "node": "a"}], False),                                  # not at the charger
    ([{**_PLUGGED, "charge": {"charger": "dock", "until": 3.0}}], False),  # over before the clock
    ([{**_PLUGGED, "charge": {"charger": "dock"}}], False),                # charging until when
    ([{**_PLUGGED, "charge": {"charger": "pump", "until": 12.0}}], False),
    ([{**_PLUGGED, "charge": {"charger": "dock", "stage": "drive"}}], False),      # no drive
    ([{**_PLUGGED, "charge": {"charger": "dock", "stage": "wait", "held": False}}], False),
    ([{**_PLUGGED, "charge": {"charger": "dock", "stage": "wait", "wait": -1}}], False),
    ([{**_PLUGGED, "charge": {"charger": "dock", "until": 12.0, "held": "yes"}}], False),
    ([{**_PLUGGED, "until": 12.0}], False),                                # handling and charging
    ([_PLUGGED, _PLUGGED], False),                                         # two on one point
    ([_PLUGGED, {**_PLUGGED, "charge": {"charger": "dock", "stage": "wait"}}], True),
], ids=range(12))
def test_a_charge_record_is_checked_against_a_model_that_has_the_charger(vehicles, fits):
    model = _yard(len(vehicles), chargers=DOCK, battery=BATTERY)
    model["initial_state"] = {"clock": 9.0, "fleets": {"F": {"vehicles": vehicles}}}
    assert ("E_INITIAL_FLEET" not in problem_codes(model)) is fits


@pytest.mark.parametrize("vehicle, trip, code", [
    ({**_PLUGGED, "charge": {"charger": "dock", "until": float("inf")}}, None, "E_INITIAL_FLEET"),
    ({"node": "b", "drive": {"to": "a", "started": float("-inf")}},
     {**_TRIP, "unit": 0, "stage": "pickup"}, "E_INITIAL_FLEET"),
    ({"node": "a", "until": float("inf")}, {**_TRIP, "unit": 0, "stage": "load"},
     "E_INITIAL_FLEET"),
    ({"node": "a"}, {**_TRIP, "unit": 0, "unload": float("inf")}, "E_INITIAL_FLEET"),
    ({"node": "a", "idle_since": float("nan")}, None, "E_INITIAL_FLEET"),
], ids=range(5))
def test_a_number_that_is_not_finite_is_refused_inside_a_model_file_too(vehicle, trip, code):
    """A state document is refused whole by `state.check`; a model file's own
    `initial_state` had no such guard."""
    model = _yard(chargers=DOCK, battery=BATTERY)
    model["initial_state"] = _fleet_state(vehicle, trip)
    assert code in problem_codes(model)
    model["initial_state"] = {"clock": float("inf")}
    assert "E_INITIAL_CLOCK" in problem_codes(model)


_PICKUP = {"vehicle": {"node": "b", "drive": {"to": "a", "started": 8.0}},
           "trip": {**_TRIP, "unit": 0, "stage": "pickup"}}
ZONED = {**NET, "segments": [{"from": "a", "to": "b", "capacity": 1}, {"from": "b", "to": "c"}]}


@pytest.mark.parametrize("change, said", [
    ({"motion": "gantry", "speed_x": 4.0, "speed_y": 4.0}, _fleet_state()),
    ({"kind": "worker"}, _fleet_state()),
    ({"battery": {**BATTERY, "strategy": "scheduled", "schedule": "night"}}, _fleet_state()),
    ({"network": ZONED}, _fleet_state(**_PICKUP)),
], ids=["gantry", "worker", "scheduled charging", "segment capacity"])
def test_what_a_take_refuses_of_a_fleet_is_refused_on_load_too(change, said):
    """Review finding 6 (D386, D234): a hand-written state of these was accepted and its
    drive ran from the clock, `started` ignored (the gantry's load left at 45.5, not 43)."""
    model = _yard(**{k: v for k, v in change.items() if k != "network"})
    model["network"] = change.get("network", NET)
    model["initial_state"] = said
    assert "E_INITIAL_FLEET" in problem_codes(model)


def test_a_fleet_on_a_zoned_network_is_placed_when_no_drive_is_under_way():
    model = _yard()
    model["network"] = ZONED
    model["initial_state"] = _fleet_state(trip={**_TRIP, "unit": 0, "stage": "carry"})
    assert problem_codes(model) == set()


@pytest.mark.parametrize("fleet, vehicle, trip, fits", [
    ({}, {"node": "b", "drive": {"to": "c", "started": 8.0}}, None, False),        # no park home
    ({"park": "home"}, {"node": "b", "drive": {"to": "c", "started": 8.0}}, None, False),
    ({"park": "home"}, {"node": "b", "drive": {"to": "a", "started": 8.0}}, None, True),
    ({"park": "home"}, {"node": "b", "drive": {"to": "a", "started": 3.9}}, None, False),  # 5 long
    ({"park": "home"}, {"node": "b", "drive": {"to": "a", "started": 4.0}}, None, True),
    ({}, {"node": "b", "drive": {"to": "a", "started": -1000}}, _PICKUP["trip"], False),
    ({}, {"node": "b", "drive": {"to": "a", "started": 8.0}}, _PICKUP["trip"], True),
    ({}, {"node": "a", "drive": {"to": "b", "started": -1.5}},                     # loaded: 10
     {**_TRIP, "unit": 0, "stage": "carry", "unload": 1.0}, False),
    ({}, {"node": "a", "drive": {"to": "b", "started": 3.0}},
     {**_TRIP, "unit": 0, "stage": "carry", "unload": 1.0}, True),
], ids=range(9))
def test_a_drive_is_one_the_vehicle_can_be_on_and_not_overdue(fleet, vehicle, trip, fits):
    """Review finding 7: a drive for no trip and no charge is the return home `park` allows,
    and a drive ends at or after the clock (empty 5, loaded 10 over the 20 m)."""
    model = _yard(**fleet)
    model["initial_state"] = _fleet_state(vehicle, trip)
    assert ("E_INITIAL_FLEET" not in problem_codes(model)) is fits


def test_a_dead_loop_of_transparent_blocks_before_a_station_does_not_hang_from_plant():
    """Review finding 8: DECIDE dd -> p and dd -> ASSIGN la -> dd, nothing feeding dd."""
    dd = _block("dd", "DECIDE", mode="Chance", percentages=[50])
    la = _block("la", "ASSIGN", assignments=[])
    p, d = process("p", resource="A"), dispose("d")
    model = layout([dd, la, p, d], chain(dd, p, d) + chain(dd, la, dd), resources=TWO,
                   simulation={"time_limit": 60, "warmup": 0, "replications": 1})
    seen = {"at": 5.0, "entities": [{"block": "p", "e_type": "", "holds": [], "since": 2.0}]}
    doc, _problems = state.from_plant(model, seen)
    assert doc["state"]["entities"] == [{"block": "p", "count": 1, "since": 2.0}]


@pytest.mark.parametrize("entry, code", [
    ({"since": "x"}, "E_INITIAL_REMAINING"), ({"since": 1e9}, "E_INITIAL_REMAINING"),
    ({"age": "x"}, "E_INITIAL_AGE"), ({"age": -5}, "E_INITIAL_AGE"),
    ({"deadline": 50.0}, "E_INITIAL_REMAINING"), ({"count": 1.5}, "E_INITIAL_COUNT"),
], ids=range(6))
def test_an_entry_with_a_trip_passes_every_check_of_an_entry(entry, code):
    """A trip exempts its entry from one check only: that a TRANSPORT holds no entity."""
    model = _yard()
    model["initial_state"] = _fleet_state(trip=_TRIP, **entry)
    assert code in problem_codes(model)
    model["initial_state"] = _fleet_state(trip=_TRIP, since=4.0, age=5.0)
    assert problem_codes(model) == set()


def test_a_vehicle_a_plant_places_starts_its_stage_afresh():
    """No `drive`, no `until`: the vehicle stands at a with the load on it, so the carry
    starts now: 10 to b and an unload of 1."""
    model = _yard(time_limit=60)
    model["blocks"][0]["params"]["start_time"] = 500
    model["initial_state"] = _fleet_state(trip={**_TRIP, "unit": 0, "stage": "carry"})
    model["initial_state"]["clock"] = 0
    assert problem_codes(model) == set()
    assert list(api.run_model(model)["entity_log"]["Exited At"]) == [11.0]


# ---------------------------------------------------------------- the JSON Schema and old states
def test_a_taken_state_of_the_new_kinds_fits_the_model_file_schema():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema

    validator = jsonschema.Draft202012Validator(build_json_schema())
    for model, at in ((_buffered(), 9.0), (_electric(), 66.6), (_yard(1, every=6.0, second=8.0), 58.9)):
        started, problems = state.start(model, state.take(model, at))
        assert problems == []
        errors = [e.message for e in validator.iter_errors(started)
                  if "initial_state" in [str(p) for p in e.absolute_path]]
        assert errors == []


def test_a_state_written_before_these_kinds_still_loads_and_check_says_what_it_lacks(
        tmp_path, capsys):
    """A document of the format as 0.6.1 wrote it: no `taken.engine`, no `fleets`."""
    import json

    model = _yard()
    old = state.document(model, {"clock": 9.0}, source="observed", at=9.0)
    del old["taken"]["engine"]
    started, problems = state.start(model, old)
    assert problems == [] and started["initial_state"] == {"clock": 9.0}
    (warning,) = state.coverage(model, old)
    assert warning.code == "W_STATE_NOT_COVERED" and warning.severity == "warning"
    assert "a version before 0.6.2" in warning.message and "fleets and vehicles" in warning.message
    assert state.coverage(model, state.take(model, 33.0)) == []
    for engine, shown in (("0.6.2", "nagasim 0.6.2"), ("\x1b[31m0.6.2\n", "an unnamed version"),
                          (["0.6.2"], "an unnamed version"), ("9" * 41, "an unnamed version")):
        old["taken"]["engine"] = engine         # the document is untrusted: nothing raw is shown
        (warning,) = state.coverage(model, old)
        assert f"written by {shown})" in warning.message and "\x1b" not in warning.message
    del old["taken"]["engine"]
    path, file = tmp_path / "yard.json", tmp_path / "old.state.json"
    path.write_text(json.dumps(model), encoding="utf-8")
    file.write_text(json.dumps(old), encoding="utf-8")
    assert state.main(["check", str(path), str(file)]) == 0
    said = capsys.readouterr().out
    assert "may start this model" in said and "W_STATE_NOT_COVERED" in said


# ---------------------------------------------------------------- from a plant (D387)
from nagasim import plant, twin                                    # noqa: E402

EPOCH = {"epoch": "1970-01-01T00:00:00+00:00", "unit": "seconds"}


def _yard_mapping(units=(0,)):
    signals = [{"key": "item/{id}", "as": "at", "blocks": {"dock": "S_A", "truck": "move"}}]
    for unit in units:
        signals += [{"key": f"agv/{unit}/node", "as": "vehicle", "fleet": "F", "unit": unit,
                     "nodes": {"A": "a", "B": "b", "C": "c"}},
                    {"key": f"agv/{unit}/load", "as": "load", "fleet": "F", "unit": unit}]
    return {"format": 1, "time": EPOCH, "signals": signals}


def _said(key, value, t):
    return {"source": "plant", "key": key, "value": value, "source_time": t, "received": t}


def test_a_plant_places_each_vehicle_and_its_load():
    """The vehicle reports node A and item 7 on it: it stands at a carrying it to the
    TRANSPORT block's destination, 10 away and 1 to unload, so the item leaves at 15."""
    model = _yard(time_limit=600)
    model["simulation"]["time_unit"] = "seconds"
    model["blocks"][0]["params"]["start_time"] = 5000
    log = [_said("item/7", "truck", 2.0), _said("agv/0/node", "B", 2.0),
           _said("agv/0/load", "7", 3.0), _said("agv/0/node", "A", 4.0)]
    events, mapper = plant.replay(_yard_mapping(), model, log)
    assert [(e.kind, e.entity_id, e.target, e.path) for e in events if e.kind == "mover_moved"] \
        == [("mover_moved", 0, "F", ("b",)), ("mover_moved", 0, "F", ("a",))]
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert [p.code for p in problems if p.severity == "error"] == []
    assert doc["state"]["fleets"] == {"F": {"vehicles": [{"node": "a"}]}}
    assert doc["state"]["entities"] == [{"block": "move", "count": 1, "trip": {
        "origin": "a", "destination": "b", "target": "S_B", "unit": 0, "stage": "carry"},
        "attributes": {"station": "S_A"}}]                 # D458: the station it came through
    started, refused = state.start(model, doc)
    assert refused == []
    assert list(api.run_model(started)["entity_log"]["Exited At"]) == [15.0]


def test_a_load_a_plant_sees_waiting_for_a_vehicle_waits_at_the_station_it_came_through():
    """D457: item 7 is seen at the TRANSPORT at 3 with no vehicle carrying it: it waits at
    S_A (node a), where vehicle 0 stands. Dispatched at 3, it loads 3-4.5, rides 4.5-14.5,
    unloads to 15.5. With no destination and no routing its trip has no target: said."""
    model = _yard(2, time_limit=600)
    model["simulation"]["time_unit"] = "seconds"
    model["blocks"][0]["params"]["start_time"] = 5000
    log = [_said("agv/0/node", "A", 2.0), _said("agv/1/node", "B", 2.0), _said("item/7", "truck", 3.0)]
    _events, mapper = plant.replay(_yard_mapping((0, 1)), model, log)
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert [p.code for p in problems if p.severity == "error"] == []
    assert doc["state"]["entities"] == [{"block": "move", "count": 1, "trip": {
        "origin": "a", "destination": "b", "target": "S_B"}, "since": 3.0,
        "attributes": {"station": "S_A"}}]
    started, refused = state.start(model, doc)
    assert refused == []
    assert list(api.run_model(started)["entity_log"]["Exited At"]) == [15.5]
    model["blocks"][2]["params"]["destination"] = ""
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert "fleets" not in doc["state"] and any("a load waits for a vehicle" in p.message
                                                for p in problems)


@pytest.mark.parametrize("log, why", [
    ([_said("agv/0/node", "A", 2.0)], "not every vehicle reports a node"),          # 1 of 2
    ([_said("agv/0/node", "A", 2.0), _said("agv/1/node", "B", 2.0), _said("agv/1/load", "9", 3.0)],
     "an item no signal places"),
], ids=["unit not observed", "unseen load"])
def test_what_a_plant_does_not_say_of_a_fleet_is_named_and_the_twin_refuses(log, why, tmp_path):
    model = _yard(2)
    model["simulation"]["time_unit"] = "seconds"
    mapping = _yard_mapping((0, 1))
    _events, mapper = plant.replay(mapping, model, log)
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert "fleets" not in doc["state"] and any(why in p.message for p in problems)

    def never(*_args, **_kwargs):
        raise AssertionError("a start with the fleet as declared")

    source = twin.PlantSource(model, mapping, twin.LogFeed(log), name="yard", clock=lambda: 4.0)
    result = twin.Cycle(source, horizon=60.0, out_dir=tmp_path, runner=never).step()
    assert result["forecast"] == {"status": "refused", "reason": "E_TWIN_STATE"}
    assert any("the plant mapping does not observe" in p["message"] for p in result["problems"])


@pytest.mark.parametrize("mapped", [False, True], ids=["G not mapped", "one of G's two silent"])
def test_a_twin_with_one_fleet_observed_and_one_not_refuses(mapped, tmp_path):
    """Review finding 4: fleet F is observed whole, fleet G not (or half): G would start as
    declared under a plant that says otherwise (D270), so every fleet not written is said
    on its own path and the twin refuses on that path."""
    model = _yard(1, second=9.0)
    model["simulation"]["time_unit"] = "seconds"
    model["fleets"].append({**model["fleets"][0], "name": "G", "count": 2})
    model["blocks"][7]["params"]["fleet"] = "G"
    mapping, log = _yard_mapping(), [_said("agv/0/node", "A", 2.0)]
    if mapped:
        mapping["signals"] += [{"key": f"g/{unit}/node", "as": "vehicle", "fleet": "G", "unit": unit,
                                "nodes": {"A": "a", "C": "c"}} for unit in (0, 1)]
        log.append(_said("g/0/node", "C", 2.0))
    _events, mapper = plant.replay(mapping, model, log)
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert list(doc["state"]["fleets"]) == ["F"]
    assert [p.path for p in problems if "fleets/" in p.path] == ["/initial_state/fleets/G"]

    def never(*_args, **_kwargs):
        raise AssertionError("a start with fleet G as declared")

    source = twin.PlantSource(model, mapping, twin.LogFeed(log), name="yard", clock=lambda: 4.0)
    result = twin.Cycle(source, horizon=60.0, out_dir=tmp_path, runner=never).step()
    assert result["forecast"] == {"status": "refused", "reason": "E_TWIN_STATE"}


@pytest.mark.parametrize("change, code", [
    ({"fleet": "G"}, "E_PLANTMAP_NAME"), ({"unit": 5}, "E_PLANTMAP_NAME"),
    ({"unit": -1}, "E_PLANTMAP_FORMAT"), ({"unit": "one"}, "E_PLANTMAP_FORMAT"),
    ({"nodes": {"A": "nowhere"}}, "E_PLANTMAP_NAME"), ({"nodes": None}, "E_PLANTMAP_FORMAT"),
], ids=range(6))
def test_a_bad_vehicle_signal_is_refused(change, code):
    mapping = _yard_mapping()
    mapping["signals"][1] = {k: v for k, v in {**mapping["signals"][1], **change}.items()
                             if v is not None}
    assert code in {p.code for p in plant.check(mapping, _yard())}
    mapping = _yard_mapping()
    mapping["signals"] += mapping["signals"][1:2]           # one vehicle told twice
    assert "E_PLANTMAP_DOUBLE" in {p.code for p in plant.check(mapping, _yard())}


def test_a_part_a_plant_sees_in_a_buffer_or_held_by_its_station_is_put_back_so():
    """Item 1 is worked on at m2, item 2 sits in the buffer, item 3 is reported at the
    buffer while m1 still holds its unit: m1 is blocked, and stays so until m2 takes 2."""
    model = _buffered(time_limit=600)
    model["simulation"]["time_unit"] = "seconds"
    model["blocks"][0]["params"]["start_time"] = 5000
    mapping = {"format": 1, "time": EPOCH, "signals": [
        {"key": "item/{id}", "as": "at", "blocks": {"m1": "m1", "buf": "buf", "m2": "m2"}},
        {"key": "m1/job", "as": "job", "resource": "A"},
        {"key": "m2/job", "as": "job", "resource": "B"}]}
    log = [_said("item/1", "m2", 1.0), _said("m2/job", "1", 1.0), _said("item/2", "buf", 2.0),
           _said("item/3", "m1", 3.0), _said("m1/job", "3", 3.0), _said("item/3", "buf", 4.0)]
    _events, mapper = plant.replay(mapping, model, log)
    doc, problems = state.from_plant(model, mapper.snapshot())
    assert [p.code for p in problems if p.severity == "error"] == []
    assert doc["state"]["entities"] == [
        {"block": "m2", "count": 1, "in_service": 1},
        {"block": "buf", "count": 1},
        {"block": "buf", "count": 1, "held_by": "m1", "place": False}]
    started, refused = state.start(model, doc)
    assert refused == []
    # m2 takes 4.8 each from 4: item 1 leaves at 8.8, 2 at 13.6, 3 at 18.4
    assert list(api.run_model(started)["entity_log"]["Exited At"]) == pytest.approx([8.8, 13.6, 18.4])


def test_a_plant_that_reports_a_mirrored_level_and_its_parts_counts_them_once():
    """A part put back AT a BUFFER counts itself into the level the buffer mirrors, one that
    has its place while it waits at the next station does not: the level is made of the
    parts, whatever the plant reports for it, and a report that disagrees is said."""
    model = _buffered(2, time_limit=600)
    model["blocks"][2]["params"]["level"] = "wip"
    model["levels"] = [{"name": "wip", "initial": 0, "capacity": 10}]
    model["blocks"][0]["params"]["start_time"] = 5000
    parts = [{"block": "m2", "e_type": "", "holds": ["B"], "since": None},     # in service
             {"block": "m2", "e_type": "", "holds": [], "since": None},        # in the buffer
             {"block": "buf", "e_type": "", "holds": [], "since": None}]       # reported there
    for said, warned in (({"wip": 2.0}, False), ({"wip": 5.0}, True), ({}, False)):
        doc, problems = state.from_plant(model, {"at": 5.0, "entities": parts, "levels": said})
        assert doc["state"]["levels"] == {"wip": 1}         # the one that will not arrive again
        assert any(p.code == "W_PLANT_CONTRADICTION" for p in problems) is warned
        started, refused = state.start(model, doc)
        assert refused == []
        session = state.SimSession(started, 0)
        session.advance(to=5.5)
        assert session.ctx.level_system.levels["wip"].level == 2.0
        session.advance(to=11.0)                            # m2 took one at 9.8
        assert session.ctx.level_system.levels["wip"].level == 1.0
    doc, problems = state.from_plant(model, {"at": 5.0, "entities": [], "levels": {"wip": 2.0}})
    assert doc["state"]["levels"] == {"wip": 2.0}                   # no part seen: the level stands
    assert not any(p.code == "W_PLANT_CONTRADICTION" for p in problems)


# ---------------------------------------------------------------- the twin (D388)
def test_a_twin_of_the_bundled_line_with_buffers_forecasts_from_its_plant_and_is_scored(tmp_path):
    """The asset-built line on a recorded log of its own run standing in for the plant: the
    forecast used to be refused by name (blocking and finite buffers). It starts from the
    plant's state, parts in buffers included, is scored against what the log then shows,
    and the stored score equals its recomputation from the log."""
    from nagasim import score
    from nagasim.core.trace import TraceRecorder

    model = _bundled("line_with_buffers", time_limit=420)
    mapping = plant.scaffold(model, counters=True)
    recorder = TraceRecorder()
    api.run_model(model, replications=1, seed=1, trace=recorder, record_logs=False)
    log = plant.log_of(recorder.events, mapping)

    class Clock:
        wall, mono = 1_800_000_000.0, 0.0

    clock = Clock()
    feed = twin.LogFeed(log, speed=60.0, monotonic=lambda: clock.mono)      # a minute a second
    source = twin.PlantSource(model, mapping, feed, name="line", clock=feed.clock)
    cycle = twin.Cycle(source, clock=lambda: clock.wall, monotonic=lambda: clock.mono,
                       out_dir=tmp_path, horizon=60.0, replications=5, seed=3,
                       forecast_every=1000.0)
    clock.mono = 250.5
    doc = cycle.step()
    assert doc["forecast"]["status"] == "ok", doc["problems"]
    taken, _problems = state.from_plant(model, dict(source.mapper.snapshot(), at=doc["at"]))
    assert doc["forecast"]["state"] == taken["hash"]
    flat = state.schema.prepare(api.load_layout(model))[0]
    buffers = {b["name"] for b in flat["blocks"] if b["type"] == "BUFFER"}
    assert sum(e["count"] for e in taken["state"]["entities"]
               if e["block"] in buffers or e.get("held_by") in buffers) >= 4
    assert not any(p["code"] == "E_TWIN_STATE" for p in doc["problems"])
    for clock.mono in (270.5, 290.5, 311.0):
        doc = cycle.step()
    assert doc["scores"]["basis"]["points"] > 0 and doc["scores"]["rows"]
    lines, differences = score.recompute(tmp_path, model, mapping, log)
    assert differences == 0, lines
