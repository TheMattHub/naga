"""Conveyor system A, checkpoint after Task 8: the fixes of the whole-branch review (R33-R39).

Every expected value is hand-derived in the test's docstring."""
import math

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_divert_spec import belt


def _run_unchecked(doc, until):
    """Run *doc* past validation, as a file the validator was not asked about runs: what the
    run itself refuses."""
    from nagasim.core.schema import prepare
    from nagasim.core.sim_runner import setup_run, step_until
    flat, _problems = prepare(doc)
    env, model = setup_run(flat, seed=0, run_len=until, warmup=0.0, prepared=True)
    step_until(env, until)
    return env, model


def _close(got, want, tol=1e-9):
    assert len(got) == len(want), got
    assert all(math.isclose(g, w, abs_tol=tol) for g, w in zip(got, want)), got


# ----------------------------------------------------------------- R39: params as written
def test_a_belt_gives_back_its_explicit_speed_unit():
    """R39: 30 m/min in a minutes model comes back as 30 m/min, and setting it again keeps the
    belt's base speed (it came back as the base number, unit dropped, before)."""
    conveyor = belt(length=10.0, speed=30.0)
    conveyor["params"]["speed_unit"] = "m/min"
    doc = layout([create(max_arrivals=1), conveyor, dispose()],
                 [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"}])
    doc["simulation"]["time_unit"] = "seconds"
    _env, model = run_env(doc, 1)
    one = model.blocks["belt"]
    params = one.get_params_dict()
    assert (params["speed"], params["speed_unit"]) == (30.0, "m/min")
    base = one.speed
    _close([base], [0.5])
    one.set_params_from_dict(params)
    assert one.speed == base and one.get_params_dict() == params


# ------------------------------------------------- R33 (C1): eyes on a zoned belt
# The Task 4 spec belt (tests/test_conveyor_zones.py): 1.8 m at 0.5, 0.41 m cases, 3 zones of
# 0.6 m, delay 1, five parts boarding every 5 s from 0, exit held to t0 = 20. The parts stand
# at fronts 1.8 (from 3.6), 1.39 (7.78), 0.98 (11.96), 0.57 (16.14) and 0.16 (20.32) and restart
# at 21 (the lead), 21, 22, 23, 23 (part 5 packs in after the release: zone 3's 23); exits 21.0,
# 21.82, 23.64, 25.46, 26.28, dieback 17.4, 10.04, 6.86. A front eye at x fires at boarding +
# x / 0.5 for a part standing past x, else at its restart + (x - front) / 0.5:
EYE_TIMES = {
    0.2: [0.4, 5.4, 10.4, 15.4, 23.0 + 0.04 / 0.5],
    0.9: [1.8, 6.8, 11.8, 23.0 + 0.33 / 0.5, 23.0 + 0.74 / 0.5],
    1.0: [2.0, 7.0, 22.0 + 0.02 / 0.5, 23.0 + 0.43 / 0.5, 23.0 + 0.84 / 0.5],
    1.5: [3.0, 21.0 + 0.11 / 0.5, 22.0 + 0.52 / 0.5, 23.0 + 0.93 / 0.5, 23.0 + 1.34 / 0.5],
}


def _zoned(eye_at=None, edge="front"):
    from test_conveyor_zones import _doc
    doc = _doc({"count": 3, "release_delay": 1.0}, 5)
    if eye_at is not None:
        doc["blocks"][1]["params"]["eyes"] = [{"at": eye_at, "edge": edge}]
    return doc


def _zoned_run(doc):
    from nagasim.core.trace import TraceRecorder
    rec = TraceRecorder()
    env, model = run_env(doc, 200, subscribers=[rec])
    exits = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "CASE")
    fired = sorted(ev.t for ev in rec.events if ev.kind == "eye" and ev.block == "belt")
    return exits, model.blocks["belt"].zone_dieback_time, fired, model


@pytest.mark.parametrize("at", sorted(EYE_TIMES))
def test_an_eye_on_a_zoned_belt_changes_no_exit_and_no_dieback(at):
    """R33, the scripted check: exit times and every zone's dieback are those of the belt with
    no eye, wherever the eye stands, before or inside the queue."""
    plain_exits, plain_dieback, _fired, _model = _zoned_run(_zoned())
    _close(plain_exits, [21.0, 21.82, 23.64, 25.46, 26.28])
    _close(plain_dieback, [17.4, 10.04, 6.86], 1e-6)
    exits, dieback, _fired, _model = _zoned_run(_zoned(at))
    _close(exits, plain_exits)
    _close(dieback, plain_dieback, 1e-6)


@pytest.mark.parametrize("at", sorted(EYE_TIMES))
def test_an_eye_on_a_zoned_belt_fires_from_the_zone_chains_times(at):
    """R33: every part fires the eye once, as its front gets there by the chain (above)."""
    _exits, _dieback, fired, model = _zoned_run(_zoned(at))
    _close(fired, EYE_TIMES[at])
    assert model.blocks["belt"]._eyes[0].fired == 5


def test_a_back_eye_on_a_zoned_belt_fires_as_the_back_clears():
    """A back eye at 0.49 fires when the front is at 0.9 (0.49 + 0.41): the 0.9 times."""
    _exits, _dieback, fired, _model = _zoned_run(_zoned(0.49, "back"))
    _close(fired, EYE_TIMES[0.9])


def test_a_transfer_hands_to_a_zoned_belt_at_its_eye_inside_the_queue():
    """R33 with the T8 handshake: a pop-up's straight exit (no move) hands loads 5 s apart to a
    zoned 1.8 m belt (untyped, spacing 0.41, 3 zones, delay 1, exit held to 20) with an eye at
    1.5. Load 1 boards at 0: eye 3.0. Load 2 boards at 5 and stands at 1.39 from 7.78, short of
    the eye, until its restart at 21: eye 21 + 0.11 / 0.5 = 21.22, so the module is busy to
    21.22. Load 3, waiting since 10, boards at 21.22 and rides free (load 2 left at 21.82):
    eye 21.22 + 3.0 = 24.22."""
    from test_conveyor_divert_spec import hog, pick
    from test_transfer_piece import ITEM, SPEED, build, fronts, run, xfer
    recv = belt(name="recv", length=1.8, speed=SPEED, spacing=ITEM, exit_resource="Op")
    recv["params"].update(zones={"count": 3, "release_delay": 1.0}, eyes=[{"at": 1.5}])
    doc = build(xfer("popup", [{"port": "out", "angle": 0}]), {"out": recv}, loads=3, spaced=5.0)
    doc["links"] = [link for link in doc["links"] if link["source"] != "recv"]
    hog_blocks, hog_links = hog("Op", 20.0)
    doc["blocks"] += [pick("Op"), dispose(), *hog_blocks]
    doc["links"] += [{"source": "recv", "target": "pick"},
                     {"source": "pick", "target": "dispose_1"}, *hog_links]
    doc["resources"] = [{"name": "Op", "capacity": 1}]
    _results, rec = run(doc)
    _close(fronts(rec, "recv"), [3.0, 21.22, 24.22])


# ------------------------------------------------- R34 (I1): counts end, targets checked
def _route_points(model):
    for one in model.blocks.values():
        for divert in getattr(one, "_diverts", []):
            if divert.route is not None:
                yield divert.route
        if getattr(one, "_route", None) is not None:
            yield one._route


def _branch_line(middle: str, rule=None):
    """c (8 loads, 1 s apart) -> T (10 m at 1) whose divert at 3 is routed by *rule*, default
    round robin, limit 1, over d0 (to L1, counted to its named target L1b) and out (T's end,
    counted nowhere). L1 (2 m) -> *middle* -> L1b (2 m) -> dispose: a Rule DECIDE cycling to
    a scrap DISPOSE and to L1b, or a BATCH of one."""
    from conftest import decide
    rule = rule or {"kind": "round_robin", "limit": 1, "exits": ["d0:L1b", "out"]}
    blocks = [create(name="c", max_arrivals=8, dist_params={"value": 1.0}, start_time=0),
              belt("T", length=10.0, diverts=[{"at": 3, "rule": rule}]),
              belt("L1", length=2.0), belt("L1b", length=2.0), dispose(), dispose("scrap")]
    links = [{"source": "c", "target": "T"}, {"source": "T", "target": "dispose_1"},
             {"source": "T", "target": "L1"}, {"source": "L1b", "target": "dispose_1"}]
    if middle == "decide":
        blocks.append(decide("dec", "Rule", rule={"kind": "cyclic"}))
        links += [{"source": "L1", "target": "dec"}, {"source": "dec", "target": "scrap"},
                  {"source": "dec", "target": "L1b"}]
    else:
        blocks.append({"name": "bat", "description": "bat", "type": "BATCH", "position": [0, 0],
                       "params": {"batch_size": 1, "batched_entity_type": "PAL"}})
        links += [{"source": "L1", "target": "bat"}, {"source": "bat", "target": "L1b"}]
    return layout(blocks, links)


@pytest.mark.parametrize("middle", ["decide", "batch"])
def test_every_count_is_0_after_a_drained_run(middle):
    """R34, the reviewer's probe: a load counted under way to L1b and scrapped by the DECIDE
    (or batched before it) never reaches L1b, so its count ended only at the run's end: d0
    stayed full after the first such load and L1 took that one load only. Disposed or batched,
    the count ends: every count is 0 after the drained run, no watch is left, and L1 takes
    more than one load."""
    env, model = run_env(_branch_line(middle), 200)
    assert model.blocks["T"].exited == 8
    assert model.blocks["L1"].exited > 1
    for route in _route_points(model):
        assert route.rule._count == [0] * len(route.rule.exits), route.block.name
        assert route._pending == {}
    assert env.ctx.route_ends == {}
    assert all(not b._reach and not b._offs for b in env.ctx.conveyors.values())


def test_a_load_routed_again_to_an_exit_that_counts_nothing_leaves_its_old_count():
    """R34: routed again (a loop) to an exit with no target piece, the load's old count ends
    (it stayed [1, 0])."""
    from types import SimpleNamespace
    from nagasim.core.blocks._routing import RoutePoint
    from nagasim.core.routing import RouteRule

    class Belt:
        def __init__(self, name):
            self.name, self.next_process = name, []
            self.block = SimpleNamespace(block_type="CONVEYOR")
            self.watching = set()

        def watch(self, entity, fn, off_only=False):
            self.watching.add(entity.id)

        def unwatch(self, entity, fn):
            self.watching.discard(entity.id)

    belt_l = Belt("L")
    ctx = SimpleNamespace(route_ends={}, route_holds={}, route_points=[], conveyors={"L": belt_l})
    owner = SimpleNamespace(name="D", ctx=ctx, next_process=[belt_l, SimpleNamespace(name="X")],
                            block=SimpleNamespace(block_type="DECIDE"),
                            env=SimpleNamespace(now=0.0))
    point = RoutePoint(owner, RouteRule("round_robin", ["a", "b"], limit=1), [0, 1])
    load = type("Load", (), {"id": 7})()
    point.routed(load, 0)
    assert point.rule._count == [1, 0] and belt_l.watching == {7} and ctx.route_ends
    point.routed(load, 1)                   # exit b leads to no CONVEYOR: counts nothing
    assert point.rule._count == [0, 0] and belt_l.watching == set() and ctx.route_ends == {}


def _problems(doc):
    from nagasim.core.schema import validate_model_structured
    return validate_model_structured(doc)


def _cases(doc, code):
    return sorted(p.fields["case"] for p in _problems(doc) if p.code == code)


@pytest.mark.parametrize("target, why", [("NoSuch", "not a CONVEYOR"),
                                         ("far", "no load of that exit reaches")])
def test_a_named_target_is_a_conveyor_on_the_exits_way(target, why):
    """R34: "d0:NoSuch" stopped the run with a plain RuntimeError at the first routed load, and
    "d0:far" (a belt on another line) was counted for good. Both are refused in validation
    (E_CONVEYOR_DIVERT, route_target, with its plain line) and stop the run with the code."""
    from nagasim.core import plainlines
    from nagasim.core.errors import ModelRunError
    doc = _branch_line("decide", {"kind": "round_robin", "limit": 1,
                                  "exits": [f"d0:{target}", "out"]})
    doc["blocks"] += [create(name="c2", max_arrivals=1), belt("far", length=2.0)]
    doc["links"] += [{"source": "c2", "target": "far"}, {"source": "far", "target": "dispose_1"}]
    found = [p for p in _problems(doc) if p.fields.get("case") == "route_target"]
    assert [p.code for p in found] == ["E_CONVEYOR_DIVERT"]
    assert plainlines.plain(found[0]) == \
        f"Block T: exit d0:{target} names {target}, no conveyor on its way"
    with pytest.raises(ModelRunError, match=f"{why}.*E_CONVEYOR_DIVERT"):
        _run_unchecked(doc, 50)


def _rule_users(rule):
    """A belt divert, a Rule DECIDE and a CONVEYOR_TRANSFER, each routed by *rule*."""
    from conftest import decide
    from test_transfer_piece import xfer
    divert = _branch_line("decide", dict(rule, exits=["d0", "out"]))
    dec = layout([create(max_arrivals=1), decide("d", "Rule", rule=rule), dispose(),
                  dispose("d2")], [{"source": "create_1", "target": "d"},
                                   {"source": "d", "target": "dispose_1"},
                                   {"source": "d", "target": "d2"}])
    module = layout([create(max_arrivals=1),
                     xfer("popup", [{"port": "a", "angle": 0}, {"port": "b", "angle": 0}],
                          rule=rule), dispose(), dispose("d2")],
                    [{"source": "create_1", "target": "xfer"},
                     {"source": "xfer", "target": "dispose_1", "source_port": "a"},
                     {"source": "xfer", "target": "d2", "source_port": "b"}])
    return [(divert, "E_CONVEYOR_DIVERT", "route_rule"), (dec, "E_DECIDE_RULE", "rule"),
            (module, "E_CONVEYOR_DIVERT", "transfer_rule")]


def test_a_misspelt_rule_key_is_refused_for_every_caller():
    """R34: {"kind": "round_robin", "limits": 2} ran with no limit; now the validator refuses
    it on a divert, a DECIDE and a transfer, and a run stops with the code named."""
    from nagasim.core.errors import ModelRunError
    for doc, code, case in _rule_users({"kind": "round_robin", "limits": 2}):
        assert case in _cases(doc, code), code
        with pytest.raises(ModelRunError, match=f"unknown key 'limits'.*{code}"):
            _run_unchecked(doc, 50)


# ------------------------------------------------- R35 (I2): the transfer's waits are holds
def _loop(eye_at, loads):
    """c (loads 1 s apart; 0: open-ended) -> F (4 m at 0.5, spacing 0.41) -> a pop-up's
    straight exit -> R (the same, an eye at *eye_at*) -> F: a closed loop of belts."""
    from test_transfer_piece import ITEM, SPEED, xfer
    feed = belt("F", length=4.0, speed=SPEED, spacing=ITEM)
    back = belt("R", length=4.0, speed=SPEED, spacing=ITEM)
    back["params"]["eyes"] = [{"at": eye_at}]
    doc = layout([create(name="c", e_type="BOX", max_arrivals=loads,
                         dist_params={"value": 1.0}, start_time=0), feed,
                  xfer("popup", [{"port": "out", "angle": 0}]), back, dispose()],
                 [{"source": "c", "target": "F"}, {"source": "F", "target": "xfer"},
                  {"source": "xfer", "target": "R", "source_port": "out"},
                  {"source": "R", "target": "F"}],
                 simulation={"time_limit": None, "warmup": 0, "replications": 1,
                             "time_unit": "seconds"})
    doc["entities"] = [{"name": "BOX", "size": {"length": 0.41, "width": 0.3, "height": 0.2}}]
    return doc


def test_a_loop_whose_transfer_waits_for_an_eye_names_the_transfer():
    """15 loads fill the loop with R's queue standing short of its eye at 2.5. Since T11 (R41)
    the live check sees it as the last load stands at t = 33, with R's free cells of no use to
    the module waiting for its handshake: the loop is named while the run is on."""
    with pytest.raises(RuntimeError, match=r"deadlock at t=33: .*loop F -> xfer -> R -> F"):
        run_env(_loop(2.5, 15), 300)


def test_a_live_loop_whose_transfer_waits_for_an_eye_runs_on():
    """17 loads and an eye at 0.2 that every load reaches: the loop keeps moving to the run's
    end; the module's eye wait is a hold but never a deadlock here."""
    env, model = run_env(_loop(0.2, 17), 300)
    assert env.now == 300 and model.blocks["R"].exited > 17


def test_a_routing_hold_open_at_a_time_limited_runs_end_is_warned(caplog):
    """R35: T's divert at 3 routes fixed to d0 with limit 1; d0's load stands at L's end, its
    exit_resource held by a hog to 1000, so it stays under way. Load 2 (boarding at 1) reaches
    the divert at 4 and is held there to the end of a 50 run, which a drained check never
    sees (the hog's release is still due): a warning names T and t = 4."""
    from test_conveyor_divert_spec import hog, pick
    hog_blocks, hog_links = hog("Op", 1000.0)
    doc = layout([create(name="c", max_arrivals=3, dist_params={"value": 1.0}, start_time=0),
                  belt("T", length=10.0, diverts=[{"at": 3, "rule": {"kind": "fixed",
                                                                      "limit": 1}}]),
                  belt("L", length=2.0, exit_resource="Op"), pick("Op"), dispose(),
                  *hog_blocks],
                 [{"source": "c", "target": "T"}, {"source": "T", "target": "dispose_1"},
                  {"source": "T", "target": "L"}, {"source": "L", "target": "pick"},
                  {"source": "pick", "target": "dispose_1"}, *hog_links],
                 resources=[{"name": "Op", "capacity": 1}])
    with caplog.at_level("WARNING", logger="nagasim.core.blocking"):
        env, _model = run_env(doc, 50)
    assert env.ctx.route_holds == {"T": [4.0]}
    assert any("Block 'T' still holds 1 load(s)" in r.message and "t=4" in r.message
               for r in caplog.records)


# ------------------------------------------------- R36 (I3, I4)
def _merge_line(eye_at):
    """2 loads at 0 -> a pop-up's straight exit -> R (10 m at 0.5, spacing 0.41), which they
    join by a merge at 4.0 -> dispose; R has an eye at *eye_at* (None: none)."""
    from test_transfer_piece import ITEM, SPEED, xfer
    recv = belt("R", length=10.0, speed=SPEED, spacing=ITEM)
    recv["params"]["merges"] = [{"at": 4.0, "from": "xfer"}]
    recv["params"]["eyes"] = [] if eye_at is None else [{"at": eye_at}]
    doc = layout([create(name="c", e_type="BOX", entities_per_arrival=2, max_arrivals=1,
                         start_time=0), xfer("popup", [{"port": "out", "angle": 0}]), recv,
                  dispose()],
                 [{"source": "c", "target": "xfer"},
                  {"source": "xfer", "target": "R", "source_port": "out"},
                  {"source": "R", "target": "dispose_1"}],
                 simulation={"time_limit": None, "warmup": 0, "replications": 1,
                             "time_unit": "seconds"})
    doc["entities"] = [{"name": "BOX", "size": {"length": ITEM, "width": 0.3, "height": 0.2}}]
    return doc


@pytest.mark.parametrize("eye_at, exits", [
    (None, [12.0, 12.82]),      # handshake: clear of the merge by 0.41 / 0.5 = 0.82
    (1.0, [12.0, 12.82]),       # an eye before the merge is none for a joining load
    (5.0, [12.0, 14.0]),        # the eye 1.0 past the merge: 2.0 after joining
])
def test_a_transfer_exit_joining_at_a_merge_waits_only_for_an_eye_past_it(eye_at, exits):
    """R36 (I3): load 1 joins at 4.0 at t = 0 and leaves R's end at (10 - 4) / 0.5 = 12; load 2
    joins as the module frees. With an eye at 1.0, before the merge, the module stayed busy
    until load 1 left R's end (load 2 out at 24)."""
    env, _model = run_env(_merge_line(eye_at), 100)
    _close(sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "BOX"), exits)


def test_validation_reads_only_the_eyes_past_a_merge():
    """R36 (I3) in validation: a limit counts to R; R's only eye, at 1.0, is before the merge,
    so the exit does not count only to the handshake (no transfer_target warning)."""
    doc = _merge_line(1.0)
    doc["blocks"][1]["params"]["rule"] = {"kind": "fixed", "limit": 1}
    assert "transfer_target" not in _cases(doc, "W_ROUTE_RULE")
    doc["blocks"][2]["params"]["eyes"] = [{"at": 5.0}]
    assert "transfer_target" in _cases(doc, "W_ROUTE_RULE")


def test_a_limits_default_target_runs_through_a_one_exit_transfer():
    """R36 (I4): T's divert at 3 (limit 2) -> L -> a turntable with one exit -> W (eye at 1.0)
    -> dispose. The default target piece is W, the last conveyor of the branch (the walk
    ended at L before), in the run and in validation (no uncounted warning)."""
    from test_transfer_piece import xfer
    w = belt("W", length=2.0)
    w["params"]["eyes"] = [{"at": 1.0}]
    doc = layout([create(name="c", max_arrivals=3, dist_params={"value": 1.0}, start_time=0),
                  belt("T", length=10.0, diverts=[{"at": 3, "rule": {"kind": "fixed",
                                                                      "limit": 2}}]),
                  belt("L", length=2.0), xfer("turntable", [{"port": "w", "angle": 90}]), w,
                  dispose()],
                 [{"source": "c", "target": "T"}, {"source": "T", "target": "dispose_1"},
                  {"source": "T", "target": "L"}, {"source": "L", "target": "xfer"},
                  {"source": "xfer", "target": "W", "source_port": "w"},
                  {"source": "W", "target": "dispose_1"}])
    assert _cases(doc, "W_ROUTE_RULE") == []
    env, model = run_env(doc, 200)
    route = model.blocks["T"]._diverts[0].route
    assert [b.name for b in route.chain(0)] == ["L", "W"]
    assert model.blocks["W"].exited == 3 and route.rule._count == [0]


# ------------------------------------------------- R38: a DECIDE's recirculation
def _recirculation(via_belt: bool):
    """c (3 loads at 0, 1, 2) -> T (5 m at 1) -> DECIDE, Rule cyclic, limit 1: exit 0 back to
    T (through belt L, 5 m, when *via_belt*), exit 1 out to dispose."""
    from conftest import decide
    blocks = [create(name="c", max_arrivals=3, dist_params={"value": 1.0}, start_time=0),
              belt("T", length=5.0), decide("dec", "Rule", rule={"kind": "cyclic", "limit": 1}),
              dispose()]
    links = [{"source": "c", "target": "T"}, {"source": "T", "target": "dec"}]
    if via_belt:
        blocks.append(belt("L", length=5.0))
        links += [{"source": "dec", "target": "L"}, {"source": "L", "target": "T"}]
    else:
        links.append({"source": "dec", "target": "T"})
    links.append({"source": "dec", "target": "dispose_1"})
    return layout(blocks, links)


def test_a_decide_recirculating_through_a_belt_counts_and_holds():
    """R38: exit 0's target piece is L, not T (the belt handing the DECIDE its loads, whose end
    each load had just left, so its count ended at once and never held). Load 1 at 5 -> L,
    back on T at 10, out at 15. Load 2 at 6 -> out. Load 3 at 7 finds exit 0 full and is held
    until load 1 leaves L at 10 -> L, back on T at 15 (T's end at 20), exit 0 again -> L, T at
    25, out at 30."""
    env, model = run_env(_recirculation(True), 100)
    _close([e.deletion_time for e in sorted(env.ctx.all_entities, key=lambda e: e.id)],
           [15.0, 6.0, 30.0])
    assert model.blocks["dec"]._route.rule._count == [0, 0]


def test_a_decide_exit_straight_back_to_its_feeding_belt_is_warned_uncounted():
    """R38: exit 0 links straight back to T, which feeds the DECIDE: no target piece, so its
    limit reads 0, and validation says so as the run counts it (exit 1, to the DISPOSE, is
    uncounted in both lines)."""
    def uncounted(doc):
        return sorted(p.fields["exit"] for p in _problems(doc)
                      if p.code == "W_ROUTE_RULE" and p.fields["case"] == "uncounted")
    assert uncounted(_recirculation(False)) == ["T", "dispose_1"]
    assert uncounted(_recirculation(True)) == ["dispose_1"]


# ------------------------------------------------- R40: diverts on a zoned belt
def test_a_divert_on_a_zoned_belt_is_warned_not_refused():
    """R40: a zoned belt may have a divert (no refusal), with a warning that the zone timing
    behind its point is not modelled; a zoned belt without one has no such warning."""
    from nagasim.core import plainlines
    doc = _zoned()
    assert "zones_divert" not in _cases(doc, "W_ROUTE_RULE")
    doc["blocks"][1]["params"]["diverts"] = [{"at": 0.9, "condition": "False"}]
    doc["links"].append({"source": "belt", "target": "dispose_1"})
    found = [p for p in _problems(doc) if p.fields.get("case") == "zones_divert"]
    assert [(p.code, p.severity) for p in found] == [("W_ROUTE_RULE", "warning")]
    assert plainlines.plain(found[0]) == "Belt belt: no zone timing behind its last divert or eye"
    assert not [p for p in _problems(doc) if p.severity == "error"]


def test_the_rereview_probe_of_an_idle_divert_and_an_eye_is_warned():
    """R40 re-review: an idle divert at 0.1 and an eye at 1.0 on the Task 4 belt. The divert
    makes the eye a point again, so the cascade runs only past the eye (the probe's exits and
    dieback are not the zone model's); the model loads with the warning that says so."""
    doc = _zoned(1.0)
    doc["blocks"][1]["params"]["diverts"] = [{"at": 0.1, "condition": "False"}]
    doc["links"].append({"source": "belt", "target": "dispose_1"})
    found = [p for p in _problems(doc) if p.fields.get("case") == "zones_divert"]
    assert [p.code for p in found] == ["W_ROUTE_RULE"]
    assert "behind the last divert or eye" in found[0].message
    assert not [p for p in _problems(doc) if p.severity == "error"]


# ------------------------------------------------- R38: the other minors
def _simple(conveyor):
    return layout([create(max_arrivals=1), conveyor, dispose()],
                  [{"source": "create_1", "target": conveyor["name"]},
                   {"source": conveyor["name"], "target": "dispose_1"}])


@pytest.mark.parametrize("field", ["gap", "cell"])
def test_a_negative_gap_or_cell_is_refused(field):
    """R38: gap -1 on a typed belt validated clean and four items left 0.02 s apart."""
    from nagasim.core import plainlines
    conveyor = belt(length=5.0)
    conveyor["params"].update(type="belt", **{field: -1})
    found = [p for p in _problems(_simple(conveyor)) if p.code == "E_CONVEYOR_PARAM"]
    assert [p.fields["case"] for p in found] == ["at_least_0"]
    assert plainlines.plain(found[0]) == f"Belt belt: {field} must be 0 or more"
    with pytest.raises(ValueError, match="gap and cell must be 0 or more"):
        _run_unchecked(_simple(conveyor), 10)


def test_a_transfer_exit_with_an_extra_key_is_refused_by_the_validator():
    """R38: the JSON schema refused it, the validator did not."""
    from test_transfer_piece import xfer
    module = xfer("popup", [{"port": "a", "angle": 0, "side": "left"}])
    doc = layout([create(max_arrivals=1), module, dispose()],
                 [{"source": "create_1", "target": "xfer"},
                  {"source": "xfer", "target": "dispose_1", "source_port": "a"}])
    assert "transfer_exit" in _cases(doc, "E_CONVEYOR_PARAM")


def test_a_merge_inside_a_rule_group_is_warned():
    """R38, the reviewer's probe: diverts at 3 and 7 in one group and a merge at 5: the merged
    parts never meet the rule (it runs at 3), so they ride to the end: a warning. A merge
    before the group's first divert is none."""
    from nagasim.core import plainlines
    rule = {"kind": "round_robin"}
    trunk = belt("T", length=10.0, diverts=[{"at": 3, "rule": rule, "rule_group": "g"},
                                            {"at": 7, "rule_group": "g"}],
                 merges=[{"at": 5, "from": "m"}])
    doc = layout([create(max_arrivals=1), create(name="c2", max_arrivals=1), belt("m"), trunk,
                  belt("L0"), belt("L1"), dispose()],
                 [{"source": "create_1", "target": "T"}, {"source": "c2", "target": "m"},
                  {"source": "m", "target": "T"}, {"source": "T", "target": "dispose_1"},
                  {"source": "T", "target": "L0"}, {"source": "T", "target": "L1"},
                  {"source": "L0", "target": "dispose_1"}, {"source": "L1", "target": "dispose_1"}])
    found = [p for p in _problems(doc) if p.fields.get("case") == "merge_in_group"]
    assert [plainlines.plain(p) for p in found] == ["Belt T: merge 0 joins inside rule group g"]
    trunk["params"]["merges"][0]["at"] = 2
    assert not [p for p in _problems(doc) if p.fields.get("case") == "merge_in_group"]


def test_the_schema_writes_the_rule_once_and_on_divert_items_and_types_a_belt():
    """R38: one route_rule under $defs, read by a DECIDE, a transfer and a divert item; a
    CONVEYOR's `type` is an enum of the conveyor types."""
    import jsonschema
    from nagasim.core.conveyor_types import TYPES
    from nagasim.modelschema import build_json_schema
    schema = build_json_schema()
    variants = {v["title"]: v["properties"]["params"]["properties"]
                for v in schema["$defs"]["block"]["oneOf"]}
    ref = {"$ref": "#/$defs/route_rule"}
    assert variants["DECIDE"]["rule"]["anyOf"][0]["$ref"] == ref["$ref"]
    assert variants["CONVEYOR_TRANSFER"]["rule"]["anyOf"][0]["$ref"] == ref["$ref"]
    diverts = variants["CONVEYOR"]["diverts"]["anyOf"][0]
    assert diverts["items"]["properties"]["rule"] == ref
    assert variants["CONVEYOR"]["type"]["anyOf"][0]["enum"] == ["", *TYPES]
    rule = {"$defs": schema["$defs"], **schema["$defs"]["route_rule"]}
    jsonschema.validate({"kind": "round_robin", "limit": 2}, rule)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"kind": "round_robin", "limits": 2}, rule)
