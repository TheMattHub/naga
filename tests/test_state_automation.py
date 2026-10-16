"""State in, sixth round (D444 to D449): a MOTION job mid-cycle, a cobot tending a machine.
Each case: taken, started, taken again at the clock (same hash), exact against the
uninterrupted run with a warm-up at the take-time (`_carried`)."""
import numpy as np
import pytest

from conftest import create, problem_codes, run_env
from test_motion import LOADED, UR10E, cell_model, motion
from test_state import _bundled, _carried, _kpis

from nagasim import api, state
from nagasim.core.trace import TraceRecorder

CYCLE_TIMES = [1.3, 1.0, LOADED, 0.5, 2.0]      # the cell's segments, seconds


def _cell(hrc="none", every=4.0, time_limit=60, **params):
    model = cell_model(arrivals=0, hrc=hrc, **params)
    model["blocks"][0]["params"]["dist_params"] = {"value": every}
    model["simulation"] = {**model["simulation"], "time_limit": time_limit}
    return model


# ---------------------------------------------------------------- MOTION mid-cycle (D444)
@pytest.mark.parametrize("hrc", ["none", "collaborative"])
@pytest.mark.parametrize("at", [0.7, 2.9, 5.95, 13.3, 20.35, 33.7, 47.1])
def test_a_motion_job_mid_cycle_is_carried(hrc, at):
    taken = _carried(_cell(hrc), at)
    cycle = next(e["cycle"] for e in taken["entities"] if e.get("in_service"))
    assert cycle["until"] >= at and len(cycle["times"]) == 5
    assert ("slow" in cycle) == (hrc == "collaborative" and cycle["stage"] >= 0)


@pytest.mark.parametrize("at", [382.8, 5737.8, 21037.8])
def test_the_bundled_cell_is_carried_mid_cycle(at):
    taken = _carried(_bundled("automation_cell", deterministic=True), at)
    assert any("cycle" in e for e in taken["entities"])


def test_a_motion_job_mid_cycle_by_hand():
    """The loaded move (stage 2) ends at 10.4; place (0.5) and process (2.0) follow, so the
    job's phases start at 10.4 and 10.9 and it leaves at 12.9. The cycle counts once, whole:
    its time is the sum of the five segments, the two before the clock included."""
    model = _cell(time_limit=30)
    model["blocks"][0]["params"]["start_time"] = 100.0       # no arrival in the window
    model["initial_state"] = {"clock": 10.0, "entities": [_in_cell()]}
    assert problem_codes(model) == set()
    recorder = TraceRecorder()
    env, built = run_env(model, 30, subscribers=[recorder])
    phases = [(e.state, e.t) for e in recorder.events if e.kind == "cycle_phase"]
    assert phases == [("place", pytest.approx(10.4)), ("process", pytest.approx(10.9))]
    left = [e.t for e in recorder.events if e.kind == "entity_disposed"]
    assert left == [pytest.approx(12.9)]
    cell = built.blocks["cell"]
    assert cell.cycles == 1 and cell.cycle_time_total == pytest.approx(sum(CYCLE_TIMES))
    assert cell.motion_stats["move"][0] == 2


def test_a_motion_job_mid_cycle_is_reproduced_in_distribution():
    """As `test_state`: the state of seed s started with another seed against the run of seed
    s, 30 seeds, each mean difference inside its 99% interval."""
    segments = [{"kind": "move", "distance_m": 0.8}, {"kind": "process", "time": "EXPO(2.5)"}]
    model = cell_model(segments, arrivals=0)
    model["blocks"][0]["params"].update(distribution="Exponential", dist_params={"rate": 0.25})
    model["simulation"] = {**model["simulation"], "time_limit": 400}
    kpis = ("entity/DEFAULT/Avg Time", "resource/Robot/Idle %", "system/Total Entities Completed")
    differences = {kpi: [] for kpi in kpis}
    for seed in range(30):
        warm, problems = state.start(model, state.take(model, 150.3, seed=seed))
        assert problems == []
        reference = _kpis(api.run_model(model, warmup=150.3, replications=1, seed=seed))
        started = _kpis(api.run_model(warm, replications=1, seed=1000 + seed))
        for kpi in kpis:
            differences[kpi].append(started[kpi] - reference[kpi])
    for kpi, values in differences.items():
        mean, error = np.mean(values), np.std(values, ddof=1) / np.sqrt(len(values))
        assert abs(mean) <= 2.756 * error and error > 0, kpi


def test_a_motion_job_changing_over_is_refused_by_name():
    model = _cell(every=3.0, setup="0.6")
    model["blocks"].append(create("b", e_type="B", dist_params={"value": 4.5}, start_time=1.0))
    model["links"].append({"source": "b", "target": "cell"})
    found = set()
    for at in (x / 8 + 0.01 for x in range(8, 200, 4)):
        try:
            _carried(model, at)         # and every take accepted is exact
        except state.StateError as exc:
            found.add(exc.kind)
    assert "a job preempted or changing over" in found
    assert found <= {"a job preempted or changing over", "an instant with an event due"}


def _in_cell(age=5.0, **cycle):
    spec = {"stage": 2, "until": 10.4, "times": [1.3, 1.0, LOADED, 0.5, 2.0], **cycle}
    return {"block": "cell", "count": 1, "age": age, "in_service": 1, "cycle": spec}


@pytest.mark.parametrize("entry", [
    _in_cell(until=11.5),                                   # beyond the segment in progress
    _in_cell(until=9.9),                                    # before the clock
    _in_cell(until=float("nan")),
    _in_cell(stage=5), _in_cell(stage=-1), _in_cell(stage=True), _in_cell(stage="2"),
    _in_cell(times=[1.3, 1.0, LOADED, 0.5]),                # one per segment
    _in_cell(times=[1.3, 1.0, LOADED, None, 2.0]),          # a timed segment's draw is known
    _in_cell(times=[1.3, 1.0, LOADED, 0.5, -2.0]),
    _in_cell(times=[1.3, 1.0, LOADED, 0.5, 1e308]),         # "2" is the process's time
    _in_cell(times=[1.3, 9.0, LOADED, 0.5, 2.0]),           # and "1.0" the pick's
    _in_cell(times=[1.3, 1.0, 9.9, 0.5, 2.0], until=10.4),  # a move the profile does not give
    _in_cell(slow=[0]),                                     # hrc none: no move is slowed
    _in_cell(slow=[1]),                                     # a pick is no move
    _in_cell(stage=0, until=10.4, times=[1.3, 1.0, None, 0.5, 2.0], slow=[2]),     # not started
    _in_cell(other=1),
    {**_in_cell(), "count": 2},
    {**_in_cell(), "ends": [10.4]},
    {"block": "cell", "count": 1, "in_service": 1, "ends": [10.4]},     # D445: a cycle, not ends
    {"block": "cell", "count": 1, "in_service": 1, "remaining": [0.4]},
    _in_cell(age=0.5),                                      # began before the job was made
    {"block": "cell", "count": 1, "cycle": _in_cell()["cycle"]},          # not in service
], ids=lambda e: str(e)[:60])
def test_a_bad_cycle_is_refused(entry):
    model = _cell(time_limit=30)
    model["initial_state"] = {"clock": 10.0, "entities": [entry]}
    assert "E_INITIAL_CYCLE" in problem_codes(model)


def test_a_motion_job_from_a_plant_still_runs_a_whole_cycle():
    """D445 with `from_plant`: a job seen in service at a MOTION runs its cycle drawn afresh."""
    model = _cell(time_limit=30)
    seen = {"at": 10.0, "entities": [{"block": "cell", "e_type": "", "holds": ["Robot"],
                                      "since": 9.0}], "resources": {}}
    doc, problems = state.from_plant(model, seen)
    assert not [p for p in problems if p.severity == "error"]
    warm, refused = state.start(model, doc)
    assert refused == [] and warm["initial_state"]["entities"][0]["in_service"] == 1


# ---------------------------------------------------------------- a cobot tending (D446-D449)
from test_tending import PICK, PLACE, SIM, cobot, model as tended_line      # noqa: E402

RACK = {"name": "rack", "description": "rack", "type": "BUFFER", "position": [1, 0],
        "params": {"capacity": 1}}


def _home():
    """Two cobots that go home to In after each trip: they drive to pick (10 m, 10 s)."""
    return tended_line(cobot(count=2, park="home"), jobs=0, every=60.0,
                       sim={**SIM, "time_limit": 400})


def _rack():
    """One cobot and a rack before the lathe: a finished part waits for the cobot."""
    return tended_line(cobot(), jobs=0, every=30.0, service=12.0, before=[RACK],
                       sim={**SIM, "time_limit": 400})


def _queued(model):
    """#470: the lathe's machine with a finite input queue, which takes a placed part
    before its unit."""
    next(b for b in model["blocks"] if b["name"] == "machine")["params"]["queue_capacity"] = 2
    return model


def test_the_machine_of_a_station_is_one_block_or_none():
    """`machine_of` (the take refuses its None by name): the one block after the station,
    across a DECIDE; none after it, or two, is no machine."""
    from types import SimpleNamespace as NS

    from nagasim.core import tending

    def proc(kind, *after):
        return NS(block=NS(block_type=kind), next_process=list(after))

    lathe, mill = proc("PROCESS"), proc("PROCESS")
    assert tending.machine_of(proc("STATION", proc("DECIDE", lathe))) is lathe
    assert tending.machine_of(proc("STATION")) is None
    assert tending.machine_of(proc("STATION", proc("DECIDE", lathe, mill))) is None


def test_an_entry_a_transport_takes_on_says_its_station():
    """A TRANSPORT leaves from `entity.station`: an entry at the lathe's machine with none
    was accepted and the run failed when the part reached `to_out`."""
    doc = tended_line()
    entry = {"block": "machine", "count": 1, "age": 12.0, "in_service": 1, "ends": [30.0]}
    for attributes, refused in (({}, True), ({"station": "Nowhere"}, True),
                                ({"station": "Lathe"}, False)):
        doc["initial_state"] = {"clock": 15.0, "entities": [{**entry, "attributes": attributes}]}
        assert ("E_INITIAL_BLOCK" in problem_codes(doc)) == refused, attributes


def test_a_place_into_a_machine_with_a_queue_is_refused_on_load():
    """#470: the placed part holds no unit of a machine with a `queue_capacity`."""
    doc = _placed_by_hand()
    assert problem_codes(doc) == set()
    assert "E_INITIAL_FLEET" in problem_codes(_queued(doc))


def _stage(taken):
    return {(e["block"], e["trip"].get("stage", "wait"), "held_by" in e, "cycle" in e["trip"])
            for e in taken.get("entities") or [] if "trip" in e}


@pytest.mark.parametrize("line, at, stage", [
    (_home, 0.37, ("to_lathe", "carry", False, False)),     # carried to a tending station
    (_home, 10.54, ("to_lathe", "unload", False, True)),    # placed (D447)
    (_home, 34.27, ("to_out", "pickup", True, False)),      # a cobot drives to pick it (D446)
    (_home, 43.31, ("to_out", "load", True, True)),         # picked
    (_rack, 26.36, ("to_out", "load", True, True)),
    (_rack, 82.86, ("to_out", "wait", True, False)),        # waits for the cobot, held
], ids=lambda x: x if isinstance(x, float) else "")
def test_a_cobot_tending_is_carried_at_each_stage(line, at, stage):
    taken = _carried(line(), at)
    assert stage in _stage(taken)
    assert taken.get("tended") == ([["Lathe", "Cobot"]] if at > 10.0 else None)


def _placing(**trip):
    return {"block": "to_lathe", "count": 1, "age": 11.0, "attributes": {"station": "Lathe"},
            "trip": {"origin": "In", "destination": "Lathe", "target": "Lathe", "unit": 0,
                     "stage": "unload", "cycle": 3.3, **trip}}


def _placed_by_hand(entry=None, until=13.3):
    doc = tended_line()
    doc["initial_state"] = {"clock": 11.0, "arrivals": {"create_1": {"count": 1}},
                            "tended": [["Lathe", "Cobot"]],
                            "fleets": {"Cobot": {"vehicles": [{"node": "Lathe", "until": until}]}},
                            "entities": [entry or _placing()]}
    return doc


def test_a_part_a_cobot_places_by_hand():
    """`test_tending`'s one part, taken at 11 while the cobot places it (10 to 13.3): the
    lathe holds it from the clock and machines it from 13.3 to 33.3, the pick ends at 34.8
    and the part leaves Out at 44.8; the place counts once, with its whole 3.3 s."""
    from test_tending import at, run, stats

    doc = {**_placed_by_hand(), "outputs": {"schedule": True}}
    assert problem_codes(doc) == set()
    env, _built, events = run(doc)
    assert at(events, "entity_seized", target="Chuck") == pytest.approx([11.0])
    held = [r["Start"] for r in env.ctx.schedule_log if r["Resource"] == "Chuck"]
    assert held == pytest.approx([10.0])        # seized when the place began (from 13.3 - 3.3)
    assert at(events, "entity_disposed") == pytest.approx([44.8])
    assert stats(env).place_time.values == pytest.approx([3.3])
    assert stats(env).pick_time.values == pytest.approx([1.5])
    assert (stats(env).places, stats(env).picks) == (1, 1)


@pytest.mark.parametrize("at", [22.8, 109.9, 425.0, 1055.2, 1685.4])
def test_the_bundled_floor_is_carried(at):
    taken = _carried(_bundled("automation_floor", deterministic=True), at)
    assert taken["tended"]


@pytest.mark.parametrize("line, kind", [
    (lambda: tended_line(cobot(count=2), jobs=0, every=25.0, sim={**SIM, "time_limit": 150}),
     "a cobot waiting loaded for its machine"),
    (lambda: tended_line(cobot(count=2), jobs=0, every=40.0, sim={**SIM, "time_limit": 400},
                         tend={"place": PLACE, "pick": PICK, "cobot": "stay"}),
     "a cobot that stays with its part"),
    (_rack, "a cobot placing a part where no machine seizes it"),
    (lambda: _queued(tended_line(cobot(count=2), jobs=0, every=12.0, service=30.0)),
     "a cobot placing a part where no machine seizes it"),         # #470
    (lambda: tended_line(cobot(count=2, exchange=True), jobs=0, every=40.0,
                         sim={**SIM, "time_limit": 400}),
     "a cobot fleet that exchanges parts"),
])
def test_what_tending_still_refuses_is_named(line, kind):
    model, found = line(), set()
    for i in range(120):
        try:
            state.take(model, 0.37 + i * 1.13)
        except state.StateError as exc:
            found.add(exc.kind)
    assert kind in found


def test_tending_from_a_plant_is_refused_by_name_only_for_a_cobot_that_stays():
    """D463: a cobot's pick and place come from its `tend` signal; one that stays with its
    part is a second hold no signal says."""
    floor = _bundled("automation_floor")
    assert state.refused_kind(floor) is None
    assert state.refused_kind(_bundled("automation_cell")) is None
    next(b for b in floor["blocks"] if b["name"] == "tended_machine_1")["params"]["arguments"][
        "cobot"] = "stay"
    assert state.refused_kind(floor) == "a cobot that stays with its part"


def _picked(**entry):
    trip = {"origin": "Lathe", "destination": "Out", "target": "S_Out", "unit": 0,
            "stage": "load", "cycle": 1.5}
    return {"block": "to_out", "count": 1, "age": 40.0, "attributes": {"station": "Lathe"},
            "held_by": "machine", "held_since": 13.3, **entry,
            "trip": {**trip, **entry.pop("trip", {})}}


@pytest.mark.parametrize("entry, until", [
    (_picked(trip={"cycle": None}), 34.0),          # a pick in progress carries its cycle
    (_picked(), 36.0),                              # it ends within its cycle of the clock
    (_picked(trip={"cycle": -1.0}), 34.0),
    ({k: v for k, v in _picked().items() if k not in ("held_by", "held_since")}, 34.0),
    (_picked(held_by="nobody"), 34.0),
    (_placing(cycle=None), 13.3),                   # a place in progress carries its cycle
    (_placing(), 15.0),
    ({**_placing(), "attributes": {"station": "S_In"}}, 13.3),  # it entered the station
    ({**_placing(), "held_by": "machine"}, 13.3),   # a placed part is held by no machine
    (_placing(stage="carry", cycle=None, unload=2.0), None),    # a place unloads nothing
    (_placing(cycle=1e300), 13.3),                  # #469: the place's segments fix 3.3 s
    (_placing(cycle=100.0), 13.3),
    (_placing(cycle=0.0), 11.0),
    (_picked(trip={"cycle": 1e300}), 34.0),         # and the pick's 1.5 s
    ({**_placing(), "age": 0.5}, 13.3),             # the place began before the part was made
], ids=lambda x: str(x)[:50])
def test_a_bad_tending_trip_is_refused(entry, until):
    doc = _placed_by_hand(entry, until)
    if entry["trip"].get("stage") == "load":
        doc["initial_state"]["clock"] = 34.0
        doc["initial_state"]["fleets"]["Cobot"]["vehicles"][0]["node"] = "Lathe"
    if until is None:
        doc["initial_state"]["fleets"]["Cobot"]["vehicles"][0] = {
            "node": "In", "drive": {"to": "Lathe", "started": 5.0, "loaded": True}}
    assert "E_INITIAL_FLEET" in problem_codes(doc)


def test_a_good_pick_by_hand_is_accepted_and_bad_rows_are_refused():
    doc = _placed_by_hand(_picked(), 34.0)
    doc["initial_state"]["clock"] = 34.0
    assert problem_codes(doc) == set()
    for rows in ([["Lathe"]], [["S_In", "Cobot"]], [["Lathe", "Nobody"]], "Lathe",
                 [["Lathe", ["Cobot"]]], [["Lathe", "Cobot"], ["Lathe", "Cobot"]]):
        doc["initial_state"]["tended"] = rows
        assert "E_INITIAL_FLEET" in problem_codes(doc), rows
    early = _placed_by_hand(_picked(held_since=-1e9), 34.0)    # before the part was made
    early["initial_state"]["clock"] = 34.0
    assert "E_INITIAL_REMAINING" in problem_codes(early)


def test_a_move_slowed_in_separation_mode_is_accepted_with_its_slowed_time():
    slow = motion.move_time(0.8, UR10E["collaborative_speed"], UR10E["accel"])
    model = _cell(hrc="separation", zone={"node": "cell", "radius_m": 1.5})
    for times, flags, refused in (([slow, 1.0, None, 0.5, 2.0], [0], False),
                                  ([1.3, 1.0, None, 0.5, 2.0], [], False),
                                  ([1.3, 1.0, None, 0.5, 2.0], [0], True)):
        model["initial_state"] = {"clock": 10.0, "entities": [_in_cell(
            stage=0, until=10.0 + times[0] / 2, times=times, slow=flags)]}
        assert ("E_INITIAL_CYCLE" in problem_codes(model)) == refused, flags
