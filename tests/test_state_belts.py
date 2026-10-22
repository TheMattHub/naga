"""State in, fourth round (D429 to D434): conveyors, resources held between blocks, a batch's
maximum wait. The exit check is the one of `test_state.py`: a run started from a state taken at
t is the uninterrupted run with a warm-up of t, and the state taken again at the clock is the
same document."""
import json
from pathlib import Path

import numpy as np
import pytest

from conftest import chain, create, delay, dispose, layout, problem_codes, process
from test_state import SERVER, _batch_line, _block, _bundled, _carried, _kpis, _refusal

from nagasim import api, plant, state


# ---------------------------------------------------------------- conveyors (D429 to D431)
def _belt(name, length=6.0, spacing=1.5, exit_resource="", accumulating=True):
    return _block(name, "CONVEYOR", length=length, speed=1.0, spacing=spacing,
                  accumulating=accumulating, exit_resource=exit_resource)


def _belt_line(variant="stop", *, accumulating=True, time_limit=60):
    """Twelve parts every 0.5 from 0.25 onto a belt of 6 at speed 1 with 4 cells and an
    entry every 1.5; its end stop is the one unit of "Stop", which a "Delay Release" holds
    3.0 after the belt. "station": a machine of 0.4 feeds the belt and blocks while it is
    full. "two belts": the machine feeds a belt of 3 (3 cells, an entry every 1) that hands
    its parts over to the first. Nothing random."""
    c = create("c", dist_params={"value": 0.5}, start_time=0.25, max_arrivals=12)
    belt = _belt("belt", exit_resource="Stop", accumulating=accumulating)
    load = process("load", resource="Stop", value=3.0, action="Delay Release")
    d = dispose("d")
    blocks = [c, belt, load, d]
    if variant in ("station", "two belts"):
        blocks.insert(1, process("m", resource="A", value=0.4))
    if variant == "two belts":
        blocks.insert(2, _belt("feeder", length=3.0, spacing=1.0))
    return layout(blocks, chain(*blocks), resources=[{"name": "Stop", "capacity": 1},
                                                     {"name": "A", "capacity": 1}],
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})


def test_two_parts_on_a_short_belt_by_hand():
    """A part at 0.5 boards at once and rides 6 to 6.5; one at 1.0 waits for the entry slot
    1.5 after the first (2.0), rides to 8.0, and the next slot is 3.5. At 1.25 the state
    says exactly that, and started from it the two leave at 6.5 and 8.0."""
    c = create("c", dist_params={"value": 0.5}, start_time=0.5, max_arrivals=2)
    belt, d = _belt("belt"), dispose("d")
    model = layout([c, belt, d], chain(c, belt, d),
                   simulation={"time_limit": 20, "warmup": 0, "replications": 1})
    doc = state.take(model, 1.25)
    assert [e["belt"] for e in doc["state"]["entities"]] == [
        {"stage": "ride", "until": 6.5, "entered": 0.5}, {"stage": "slot", "until": 2.0}]
    assert doc["state"]["blocks"] == {"belt": {"next_entry": 3.5}}
    started, problems = state.start(model, doc)
    assert problems == []
    log = api.run_model(started)["entity_log"]
    assert list(log["Exited At"]) == [6.5, 8.0]


@pytest.mark.parametrize("variant, times", [
    ("stop", [1.3, 2.7, 9.7, 11.1, 12.5, 20.9]), ("station", [1.3, 6.2, 8.3, 11.8, 13.2]),
    ("two belts", [1.3, 4.8, 6.9, 11.8, 14.6])])
def test_every_stage_of_a_belt_is_met_and_carried_exactly(variant, times):
    """The parts are put back at each stage of the belt (and, with a machine or a belt before
    it, held by that one: D381, D430) and the run goes on as the uninterrupted one."""
    met = set()
    for at in times:
        for entry in _carried(_belt_line(variant), at)["entities"]:
            met |= {entry["belt"]["stage"]} if "belt" in entry else set()
            met |= {"held by " + entry["held_by"]} if "held_by" in entry else set()
    assert met >= {"cell", "slot", "ride", "packed", "move", "stop"}
    assert {"stop": set(), "station": {"held by m"},
            "two belts": {"held by m", "held by feeder"}}[variant] <= met


@pytest.mark.slow
def test_a_belt_line_is_reproduced_in_distribution():
    """As `test_state`: the state of seed s started with another seed against the run of seed
    s, 30 seeds, each mean difference inside its 99% interval."""
    model = _belt_line("station", time_limit=400)
    c, m = model["blocks"][0]["params"], model["blocks"][1]["params"]
    c.update(distribution="Exponential", dist_params={"rate": 1 / 1.6}, max_arrivals=0)
    m.update(distribution="Exponential", dist_params={"rate": 1 / 0.4})
    model["blocks"][3]["params"]["dist_params"] = {"value": 1.4}
    kpis = ("entity/DEFAULT/Avg Time", "resource/Stop/Idle %", "system/Total Entities Completed")
    differences = {kpi: [] for kpi in kpis}
    for seed in range(30):
        try:
            doc = state.take(model, 150.0, seed=seed)
        except state.StateError:        # an event due at 150 in this seed: none is
            continue
        warm, problems = state.start(model, doc)
        assert problems == []
        reference = _kpis(api.run_model(model, warmup=150.0, replications=1, seed=seed))
        started = _kpis(api.run_model(warm, replications=1, seed=1000 + seed))
        for kpi in kpis:
            differences[kpi].append(started[kpi] - reference[kpi])
    assert len(differences[kpis[0]]) >= 25
    for kpi, values in differences.items():
        mean, error = np.mean(values), np.std(values, ddof=1) / np.sqrt(len(values))
        assert abs(mean) <= 2.756 * error and error > 0, kpi


@pytest.mark.parametrize("at", [30.3, 95.1, 170.2, 333.3, 560.4, 901.3])
def test_the_bundled_warehouse_with_forklifts_and_a_belt_is_carried_exactly(at):
    """Its belt, the end stop's unit carried into the "Delay Release" (D432), its fleet."""
    taken = _carried(_bundled("warehouse_forklifts", deterministic=True), at)
    assert any("belt" in e for e in taken["entities"]) or any("holds" in e for e in taken["entities"])


@pytest.mark.parametrize("at", [61.7, 170.2, 455.5, 850.1])
def test_the_bundled_warehouse_with_forklifts_is_taken_again_the_same(at):
    model = _bundled("warehouse_forklifts")
    doc = state.take(model, at)
    started, problems = state.start(model, doc)
    assert problems == [] and state.take(started, at)["hash"] == doc["hash"]


@pytest.mark.parametrize("at", [95.1, 205.5])
def test_the_bundled_warehouse_library_is_carried_with_its_belt(at):
    """Its belt, its pickers blocked by the belt, the shipping batch's maximum wait. Its
    trucks and truckloads: `test_state_batches.py`."""
    taken = _carried(_bundled("warehouse_library", deterministic=True), at)
    assert taken["blocks"]["shipping/consolidate"]["due"] > at


def test_a_non_accumulating_belt_with_parts_on_it_is_refused_by_name():
    assert "a non-accumulating belt" in _refusal(_belt_line(accumulating=False), 2.7)
    model = _belt_line(accumulating=False)
    model["initial_state"] = {"clock": 2.7, "entities": [
        {"block": "belt", "count": 1, "belt": {"stage": "ride", "until": 3.0, "entered": 1.0}}]}
    assert "E_INITIAL_BELT" in problem_codes(model)


def test_a_non_accumulating_conveyor_under_a_plant_stays_refused_by_name(tmp_path, capsys):
    """By the twin (`refused_kind`) and by `naga plant replay|watch --state` (#450); an
    accumulating belt is carried from when each part entered it (D460)."""
    assert state.refused_kind(_belt_line()) is None
    assert state.refused_kind(_belt_line(accumulating=False)) == "a non-accumulating conveyor"
    model, mapping, log, taken = (str(tmp_path / name) for name in (
        "line.json", "line.plantmap.json", "plant.jsonl", "s.json"))
    Path(model).write_text(json.dumps(_belt_line(accumulating=False)), encoding="utf-8")
    assert plant.main(["init", model, "--out", mapping]) == 0
    assert plant.main(["mock", model, "--map", mapping, "--out", log, "--seed", "1"]) == 0
    capsys.readouterr()
    assert plant.main(["replay", mapping, model, log, "--state", taken]) == 1
    assert "E_STATE_UNSUPPORTED: a state from a plant cannot carry a non-accumulating conveyor" \
        in capsys.readouterr().out


_RIDE = {"block": "belt", "count": 1, "belt": {"stage": "ride", "until": 7.0, "entered": 1.0}}


@pytest.mark.parametrize("entities, code", [
    ([{"block": "belt", "count": 1}], "E_INITIAL_BELT"),                          # no stage
    ([{**_RIDE, "belt": {"stage": "fly"}}], "E_INITIAL_BELT"),
    ([{**_RIDE, "belt": {"stage": "ride", "until": 7.0}}], "E_INITIAL_BELT"),       # entered when
    ([{**_RIDE, "belt": {"stage": "ride", "until": 1.0, "entered": 0.5}}], "E_INITIAL_BELT"),
    ([{**_RIDE, "belt": {"stage": "slot", "until": 3.0, "entered": 1.0}}], "E_INITIAL_BELT"),
    ([{**_RIDE, "count": 2}], "E_INITIAL_BELT"),
    ([{**_RIDE, "block": "load", "in_service": 0}], "E_INITIAL_BELT"),             # not a belt
    ([{**_RIDE, "belt": {"stage": "packed", "entered": 1.0}}], "E_INITIAL_BELT"),  # nobody ahead
    ([{**_RIDE, "belt": {"stage": "stop", "entered": 1.0}},
      {**_RIDE, "belt": {"stage": "stop", "entered": 2.0}}], "E_INITIAL_BELT"),    # two at the end
    ([_RIDE] * 5, "E_INITIAL_QUEUE_CAPACITY"),                                      # 4 cells
    ([{**_RIDE, "belt": "ride"}], "E_INITIAL_BELT"),
], ids=range(11))
def test_a_bad_part_on_a_belt_is_refused(entities, code):
    model = _belt_line()
    model["initial_state"] = {"clock": 5.0, "entities": entities}
    assert code in problem_codes(model)


def _part(stage, **times):
    return {"block": "belt", "count": 1, "belt": {"stage": stage, **times}}


# At 9 on the belt of 6 at speed 1 (an entry every 1.5, 4 cells): a part moving up to the end
# stop, two riding, one boarding at 9.5, the next entry at 11.
_TIMED = [_part("move", until=9.5, entered=1.0), _part("ride", until=9.0, entered=3.0),
          _part("ride", until=10.5, entered=4.5), _part("slot", until=9.5)]


@pytest.mark.parametrize("change", [
    {1: _part("ride", until=1e12, entered=3.0)},            # not its travel time
    {0: _part("move", until=1e9, entered=1.0)},             # beyond one spacing
    {"next_entry": 1e15},                                   # beyond the belt's own time
    {"next_entry": 9.6},                                    # a new part boards before the slot
    {"next_entry": None},                                   # nor does anything stop it
    {2: _part("slot", until=9.5)},                          # two side by side
    {0: _part("move", until=9.5, entered=4.0)},             # still riding at the clock
    {0: _part("move", until=9.5, entered=-1e9)},            # before time began
    {2: _part("ride", until=10.0, entered=4.0)},            # entered one second after the last
], ids=range(9))
def test_a_belt_whose_times_do_not_fit_it_is_refused(change):
    """Review finding 4: complete or refused (D234), the times of a belt included."""
    model = _belt_line()
    model["initial_state"] = {"clock": 9.0, "entities": _TIMED,
                              "blocks": {"belt": {"next_entry": 11.0}}}
    assert problem_codes(model) == set()
    entities = list(_TIMED)
    for key, value in change.items():
        if key == "next_entry":
            model["initial_state"]["blocks"] = {} if value is None \
                else {"belt": {"next_entry": value}}
        else:
            entities[key:key + 1] = [value]
    model["initial_state"]["entities"] = entities
    assert "E_INITIAL_BELT" in problem_codes(model)


def test_an_end_stop_other_jobs_queue_for_is_refused_on_load():
    """Review finding 5 (D431): a part at the end stop, and a job waiting at a station that
    seizes the same unit."""
    model = _belt_line()
    model["blocks"].append(process("other", resource="Stop", value=1.0))
    stop = {**_part("stop", entered=1.0), "since": 7.2}
    load = {"block": "load", "count": 1, "in_service": 1, "ends": [9.0], "holds": {"Stop": {}}}
    model["initial_state"] = {"clock": 8.0, "entities": [load, stop]}
    assert problem_codes(model) == set()
    model["initial_state"]["entities"].append({"block": "other", "count": 1, "since": 7.5})
    assert "E_INITIAL_BELT" in problem_codes(model)


def test_a_part_handed_over_by_a_belt_is_checked():
    model = _belt_line("two belts")
    cell = {"block": "belt", "count": 1, "belt": {"stage": "cell"}, "held_by": "feeder"}
    model["initial_state"] = {"clock": 5.0, "entities": [{**cell, "held_since": 2.0}]}
    assert problem_codes(model) == set()
    for bad, code in (({}, "E_INITIAL_REMAINING"), ({"held_since": 9.0}, "E_INITIAL_REMAINING"),
                      ({"held_since": 2.0, "held_by": "belt"}, "E_INITIAL_BLOCK")):
        model["initial_state"] = {"clock": 5.0, "entities": [{**cell, **bad}]}
        assert code in problem_codes(model)


# ---------------------------------------------------------------- a resource held between blocks
def _held_between(*, sets=False, time_limit=20):
    """Arrivals every 2 from 0; p seizes the server for 0.5 and keeps it, w waits 1.0, r gives
    it back: each job holds the server from p to r. With *sets*, p seizes a set of two."""
    c, d = create("c", dist_params={"value": 2.0}), dispose("d")
    p = process("p", resource="Team" if sets else "Server", value=0.5, action="Seize Delay")
    w = delay("w", 1.0)
    r = _block("r", "RELEASE", resource_name="Team" if sets else "Server")
    extra = {"resources": [{"name": "S1", "capacity": 1}, {"name": "S2", "capacity": 1}],
             "resource_sets": [{"name": "Team", "members": ["S1", "S2"], "rule": "PreferredOrder"}]} \
        if sets else {"resources": SERVER}
    return layout([c, p, w, r, d], chain(c, p, w, r, d), **extra,
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})


@pytest.mark.parametrize("sets", [False, True], ids=["resource", "set"])
@pytest.mark.parametrize("at", [0.75, 8.75, 13.2])
def test_a_job_that_holds_a_resource_between_blocks_is_carried(sets, at):
    """At 8.75 the job that arrived at 8 is at w since 8.5, holding what p seized at 8."""
    taken = _carried(_held_between(sets=sets), at)
    (job,) = [e for e in taken["entities"] if e["block"] == "w"]
    assert job["holds"] == {"Team": {"since": at // 2 * 2, "member": "S1"}} if sets \
        else {"Server": {"since": at // 2 * 2}}


def test_a_bundled_line_whose_jobs_hold_a_wip_token_is_carried():
    taken = _carried(_bundled("mfg_line_library", deterministic=True), 207.9)
    assert sum("holds" in e for e in taken["entities"]) == 7


@pytest.mark.parametrize("at", [60.3, 133.7])
def test_the_bundled_clinic_is_taken_again_the_same(at):
    """Its walk-ins arrive by a schedule, thinned afresh from the clock: exact in
    distribution only (D233). The state taken again at the clock is the same."""
    model = _bundled("clinic_library")
    doc = state.take(model, at)
    assert any("holds" in e for e in doc["state"]["entities"])
    started, problems = state.start(model, doc)
    assert problems == [] and state.take(started, at)["hash"] == doc["hash"]


@pytest.mark.parametrize("holds", [
    {"Nobody": {}}, {"Server": {"since": 99.0}}, {"Server": {"units": 0}}, {"Server": []},
    {"Server": {"member": "S1"}}, {"Server": {"since": 8.0, "why": 1}}, {}, "Server",
], ids=range(8))
def test_a_bad_hold_is_refused(holds):
    model = _held_between()
    model["initial_state"] = {"clock": 8.75, "entities": [
        {"block": "w", "count": 1, "in_service": 1, "ends": [9.5], "holds": holds}]}
    assert "E_INITIAL_RESOURCE" in problem_codes(model)


def test_holds_count_into_the_capacity_and_its_own_resource_is_no_hold():
    model = _held_between()
    job = {"block": "w", "count": 1, "in_service": 1, "ends": [9.5], "holds": {"Server": {}}}
    model["initial_state"] = {"clock": 8.75, "entities": [job, job]}     # one unit, two holders
    assert "E_INITIAL_IN_SERVICE" in problem_codes(model)
    model["initial_state"] = {"clock": 8.75, "entities": [
        {"block": "p", "count": 1, "in_service": 1, "ends": [9.5], "holds": {"Server": {}}}]}
    assert "E_INITIAL_RESOURCE" in problem_codes(model)


# ---------------------------------------------------------------- a batch's maximum wait (D433)
@pytest.mark.parametrize("max_wait, at, due", [("2.5", 9.5, 11.5), ("2.5", 10.25, 11.5),
                                               ("3.5", 2.5, 3.5), ("3.5", 5.25, 7.5)],
                         ids=["partial", "partial later", "filled first", "filled later"])
def test_a_running_maximum_wait_is_carried_by_the_time_it_falls_due(max_wait, at, due):
    """Arrivals at 0, 1, 2 ...; a batch of 4. With 2.5 every group leaves partial, three at a
    time (0-2 at 2.5, 3-5 at 5.5, 9-11 at 11.5); with 3.5 every group fills first (0-3 at 3)
    and its timer releases nothing."""
    taken = _carried(_batch_line(max_wait=max_wait), at)
    assert taken["blocks"] == {"b": {"due": due}}


@pytest.mark.parametrize("blocks", [{"b": {"due": 1.0}}, {"b": {"due": "soon"}},
                                    {"b": {"next_entry": 3.0}}, {"d": {"due": 12.0}}, {"b": {}}],
                         ids=["before the clock", "no number", "not a belt", "not a batch", "empty"])
def test_a_bad_due_time_is_refused(blocks):
    model = _batch_line(max_wait="2.5")
    model["initial_state"] = {"clock": 9.5, "blocks": blocks}
    assert "E_INITIAL_BLOCK" in problem_codes(model)
