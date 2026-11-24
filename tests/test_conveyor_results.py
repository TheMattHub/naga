"""Results that explain a slow conveyor line (conveyor system A, Task 12; R3, R35, D1373-D1376).

New frames, each present only when a model uses what it reads: `conveyor_flow` (a typed, zoned,
released, reversible or path-laid belt), `conveyor_zones` (a zoned belt), `transfer_summary` (a
CONVEYOR_TRANSFER), `route_holds` (a routing rule) and `link_flow` (an area link). Every value
below is derived by hand in its docstring from the timings the Task 4, 5, 7, 8 and 10 tests
already pin; each run gets an explicit time limit so a share's denominator is known.
"""
import math

import pytest

from conftest import create, dispose, layout
from test_area_links import burst, const, lnk
from test_conveyor_release_rules import _belt_doc
from test_conveyor_reverse import _doc as reverse_doc
from test_conveyor_types import sized
from test_conveyor_zones import _doc as zone_doc
from test_routing_at_points import _spike_model
from test_transfer_piece import build, receiving, xfer

from nagasim import api, experiments

NEW = {"conveyor_flow", "conveyor_zones", "transfer_summary", "route_holds", "link_flow"}


def run(doc, time_limit, replications=1, warmup=0.0, **options):
    """Run *doc* to *time_limit*, which is the window's end: a CREATE whose first part comes
    after it keeps the run from draining sooner (a drained run's window ends at the drain)."""
    doc["simulation"].update({"time_limit": time_limit, "warmup": warmup,
                              "replications": replications})
    doc["blocks"] += [create(name="clock", e_type="CLOCK", max_arrivals=1, start_time=1e6),
                      dispose(name="clock_out")]
    doc["links"].append({"source": "clock", "target": "clock_out"})
    return api.run_model(doc, replications=replications, **options)


def row(results, frame, key, value):
    data = results[frame]
    return data[data[key] == value].iloc[0]


# -- conveyor_flow and conveyor_zones -----------------------------------------------------

def test_zone_dieback_per_zone_and_dieback_length():
    """The Task 4 case (tests/test_conveyor_zones.py, test_one_second_delay_cascades_by_zone):
    belt 1.8 m at 0.5 m/s, cells 0.41, 3 zones of 0.6 m, release delay 1, Op holds the exit to
    t0 = 20; parts board at 0, 5, 10 and stand with noses 1.8 (from 3.6), 1.39 (from 7.78), 0.98
    (from 11.96). Zone 1 restarts at 21, zone 2 at 22; exits 21, 21.82, 23.64. Window 30.
      Zone 1: a front stands there from 3.6 to 21: 17.4 s (58 %). Zone 2: 11.96 to 22: 10.04 s
      (33.47 %). Zone 3: none.
      Dieback length: each part stood its time on the belt less its 3.6 s ride: 21 - 3.6 = 17.4,
      16.82 - 3.6 = 13.22, 13.64 - 3.6 = 10.04, 40.66 s in all, each 0.41 m long:
      0.41 x 40.66 / 30 = 0.555687 m. Four cells, never more than three parts: never full."""
    results = run(zone_doc({"count": 3, "release_delay": 1.0}, 3), 30)
    zones = results["conveyor_zones"]
    assert list(zones["Zone"]) == [1, 2, 3]
    assert list(zones["Dieback Time"]) == pytest.approx([17.4, 10.04, 0.0])
    assert list(zones["Dieback %"]) == pytest.approx([58.0, 100 * 10.04 / 30, 0.0])
    flow = row(results, "conveyor_flow", "Conveyor", "belt")
    assert flow["Dieback Length"] == pytest.approx(0.41 * 40.66 / 30)
    assert flow["Time Full %"] == 0
    assert math.isnan(flow["Released"]) and math.isnan(flow["Backward %"])


def test_dieback_of_a_queue_still_standing_at_the_windows_end():
    """No zones, Op holds the exit past the window (hog 100), window 30: nothing leaves. The
    parts stand with noses 1.8 from 3.6, 1.39 from 5 + 2.78 = 7.78 and 0.98 from 10 + 1.96 =
    11.96 (R63: a part still on the belt counts to the window's end from where its nose is).
    0.41 x (26.4 + 22.22 + 18.04) / 30 = 0.91102 m, against a held exit that the blocked share
    shows too."""
    results = run(zone_doc(None, 3, hold=100.0), 30)
    flow = row(results, "conveyor_flow", "Conveyor", "belt")
    assert flow["Dieback Length"] == pytest.approx(0.41 * (26.4 + 22.22 + 18.04) / 30)
    assert row(results, "conveyor_summary", "Conveyor", "belt")["Blocked %"] > 0
    assert "conveyor_zones" not in results


def _mixed_doc():
    """The Task 5 mixed-size metering case (tests/test_conveyor_release_rules.py): A (0.41) at 0,
    B (0.70) at 1, A at 2 on a 3.0 m belt at 0.5 m/s with gap 0.09, metering gap 0.40."""
    doc = _belt_doc({"metering": {"gap": 0.40}}, 2, 2.0, gap=0.09)
    doc["blocks"][0] = create(name="c", e_type="A", max_arrivals=2, dist_params={"value": 2.0},
                              start_time=0)
    doc["blocks"].append(create(name="b", e_type="B", max_arrivals=1, start_time=1.0))
    doc["links"][0]["source"] = "c"
    doc["links"].append({"source": "b", "target": "belt"})
    doc["entities"] = [sized("A", 0.41), sized("B", 0.70)]
    return doc


def test_metering_loss_reads_the_release_holds():
    """Its release holds (release_waits, D1357) are 0, 0.62, 0.62: exits 6.0, 7.62, 9.82. The
    metering loss is what the end held ready parts: 1.24 s in a 20 s window, 6.2 %, a mean of
    0.4133 s over 3 parts. (headway - item_length / speed would give 0.22 s for B: not read.)"""
    results = run(_mixed_doc(), 20)
    flow = row(results, "conveyor_flow", "Conveyor", "belt")
    assert flow["Released"] == 3
    assert flow["Avg Release Hold"] == pytest.approx(1.24 / 3)
    assert flow["Release Hold %"] == pytest.approx(6.2)


def test_reversible_belt_backward_share_and_reversals():
    """The Task 9 belt (4 m, 1 m/s) with one part at 0 and the signal raised at 1 and 3: it runs
    backward from 1 to 3, 2 s of a 10 s window (20 %), two reversals. The part rides 1 m out and
    1 m back and leaves the start at 2: it never stood, dieback 0."""
    results = run(reverse_doc([0.0], [1.0, 3.0]), 10)
    flow = row(results, "conveyor_flow", "Conveyor", "belt")
    assert flow["Backward %"] == pytest.approx(20.0)
    assert flow["Reversals"] == 2
    assert flow["Dieback Length"] == 0


# -- transfer_summary and route_holds ------------------------------------------------------

def test_turntable_busy_move_and_handshake_shares():
    """The Task 8 turntable case (tests/test_transfer_piece.py, test_turntable_90_cycle_is_13_4
    _seconds): per load entry 1.0 m at 0.5 = 2.0 s, turn 5.3, eye wait 0.8, turn back 5.3, a
    13.4 s cycle from 0, 13.4 and 26.8. In a 50 s window: busy 3 x 13.4 = 40.2 (80.4 %), moving
    3 x 10.6 = 31.8 (63.6 %), handshake 3 x 0.8 = 2.4 (4.8 %), no rule hold, never blocked; the
    entry travel is the rest, 6.0 s (12 %): 63.6 + 4.8 + 12 = 80.4."""
    doc = build(xfer("turntable", [{"port": "east", "angle": 90}], entry_length=1.0,
                     turn_rate=18, turn_ramp=0.3),
                {"east": receiving("recv", eye_at=0.4)}, loads=3)
    results = run(doc, 50)
    t = row(results, "transfer_summary", "Transfer", "xfer")
    assert t["Mechanism"] == "turntable" and t["Loads"] == 3
    assert t["Busy %"] == pytest.approx(80.4)
    assert t["Move %"] == pytest.approx(63.6)
    assert t["Handshake %"] == pytest.approx(4.8)
    assert t["Rule Hold %"] == 0 and t["Blocked %"] == 0
    assert t["Move %"] + t["Handshake %"] + 12.0 == pytest.approx(t["Busy %"])
    hold = row(results, "route_holds", "Block", "xfer")
    assert hold["Kind"] == "transfer" and hold["Holds"] == 0 and hold["Hold Time"] == 0


def test_no_eye_handshake_is_the_load_clearing_the_entry():
    """Straight pop-up, no eye: each load boards at once and the module waits 0.41 / 0.5 = 0.82 s
    for it to clear the entry; accepts 0, 0.82, 1.64. Window 20: busy = handshake = 2.46 s,
    12.3 %, no move (a straight pop-up neither lifts nor crosses)."""
    doc = build(xfer("popup", [{"port": "straight", "angle": 0}]),
                {"straight": receiving("recv", eye_at=None)}, loads=3)
    t = row(run(doc, 20), "transfer_summary", "Transfer", "xfer")
    assert t["Busy %"] == pytest.approx(12.3)
    assert t["Handshake %"] == pytest.approx(12.3)
    assert t["Move %"] == 0


def test_divert_routing_hold():
    """The Task 7 spike router (tests/test_routing_at_points.py): I3 is held at divert 0 from 6 to
    7, every branch at its limit. One hold, 1 s, in a 30 s window: 3.33 %."""
    results = run(_spike_model(), 30)
    hold = row(results, "route_holds", "Block", "trunk")
    assert (hold["Kind"], hold["Point"], hold["Holds"]) == ("divert", 0, 1)
    assert hold["Hold Time"] == pytest.approx(1.0)
    assert hold["Hold %"] == pytest.approx(100 / 30)
    assert "conveyor_flow" not in results       # an untyped belt with no new feature


# -- link_flow ---------------------------------------------------------------------------

def test_area_link_occupancy_and_time_full():
    """The Task 10 case (tests/test_area_links.py): delay 10, capacity 2, three parts at
    0. Two are in the link over [0, 10), one over [10, 20); exits 10, 10, 20. Window 100:
    occupancy (2 x 10 + 1 x 10) / 100 = 0.3, max 2; full over [0, 10): 10 %; 3 carried, 10 s
    each, each drawing a delay of 10."""
    link = row(burst({"delay": const(10), "capacity": 2}), "link_flow", "Link",
               "create_1 -> dispose_1")
    assert (link["Carried"], link["Capacity"], link["Max Occupancy"]) == (3, 2, 2)
    assert link["Avg Time"] == pytest.approx(10.0) and link["Avg Delay"] == pytest.approx(10.0)
    assert link["Avg Occupancy"] == pytest.approx(0.3)
    assert link["Time Full %"] == pytest.approx(10.0)


def test_a_delay_alone_is_no_area_link():
    """D1391: a delay with no capacity is a plain delayed link, so no link_flow frame."""
    assert "link_flow" not in burst({"delay": const(10)})


def test_link_flow_after_a_warm_up_and_over_two_replications():
    """Warm-up 5: the window is [5, 100]; two parts are in the link over [5, 10), one over
    [10, 20): (2 x 5 + 10) / 95; all three leave after 5, 10 s each. Two replications give one
    row per run (Run 0 and 1), concatenated as every summary frame, and the KPI keys the scenario
    comparison reads (`link/<link>/<column>`)."""
    blocks = [create(entities_per_arrival=3, dist_params={"value": 1000.0}, start_time=0),
              dispose()]
    model = layout(blocks, [lnk("create_1", "dispose_1", delay=const(10), capacity=2)],
                   simulation={"time_limit": 100, "warmup": 5, "replications": 2})
    results = api.run_model(model, replications=2)
    frame = results["link_flow"]
    assert list(frame["Run"]) == [0, 1]
    assert list(frame["Avg Occupancy"]) == pytest.approx([20 / 95] * 2)
    assert list(frame["Carried"]) == [3, 3]
    keys = set(experiments.flatten_kpis(results)["KPI"])
    assert "link/create_1 -> dispose_1/Avg Occupancy" in keys


# -- where the frames appear -----------------------------------------------------------------

def test_a_plain_belt_model_gets_none_of_the_new_frames():
    """An untyped belt with no zones, release, reversal, path, routing or area link keeps exactly
    the frames it had (R3), the empty ones too."""
    from test_conveyor_divert_spec import belt, model
    doc = model([create(max_arrivals=2, dist_params={"value": 1.0}, start_time=0),
                 belt(length=3.0, speed=1.0), dispose()],
                [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "dispose_1"}],
                time_limit=10)
    results = api.run_model(doc, replications=1, keep_empty=True)
    assert not NEW & set(results)


def test_decide_rule_hold():
    """The Task 7 DECIDE case (tests/test_routing_at_points_rules.py): round robin, limit 1 over
    B1 and B2; E2 arrives at 1.5 with both under way and waits in the DECIDE until E0 leaves B1 at
    2: one hold of 0.5 s, 5 % of a 10 s window."""
    from test_routing_at_points_rules import _decide_model
    results = run(_decide_model([0.0, 1.0, 1.5], {"kind": "round_robin", "limit": 1}), 10)
    hold = row(results, "route_holds", "Block", "route")
    assert (hold["Kind"], hold["Holds"]) == ("decide", 1)
    assert hold["Hold Time"] == pytest.approx(0.5)
    assert hold["Hold %"] == pytest.approx(5.0)


def test_transfer_shares_after_a_warm_up():
    """The turntable case with warm-up 10, window [10, 50]: load 1 is handed over at 7.3, before it,
    and turns back from 8.1 to 13.4, so 3.4 s of it count; loads 2 and 3 count whole. Busy
    3.4 + 2 x 13.4 = 30.2 (75.5 %), move 3.4 + 2 x 10.6 = 24.6 (61.5 %), handshake 2 x 0.8 = 1.6
    (4 %), 2 loads."""
    doc = build(xfer("turntable", [{"port": "east", "angle": 90}], entry_length=1.0,
                     turn_rate=18, turn_ramp=0.3),
                {"east": receiving("recv", eye_at=0.4)}, loads=3)
    t = row(run(doc, 50, warmup=10.0), "transfer_summary", "Transfer", "xfer")
    assert t["Loads"] == 2
    assert t["Busy %"] == pytest.approx(75.5)
    assert t["Move %"] == pytest.approx(61.5)
    assert t["Handshake %"] == pytest.approx(4.0)


def test_new_frames_reach_the_workbook_and_the_csv_folder(tmp_path):
    """The export writes each new frame as a sheet and a CSV file, as it does the belt frames."""
    from openpyxl import load_workbook
    from nagasim.core.export import export_csv_folder, export_workbook
    results = run(zone_doc({"count": 3, "release_delay": 1.0}, 3), 30)
    sheets = load_workbook(export_workbook(results, tmp_path / "out.xlsx")).sheetnames
    assert {"Conveyor flow", "Conveyor zones"} <= set(sheets)
    folder = export_csv_folder(results, tmp_path / "csv")
    assert {"conveyor_flow.csv", "conveyor_zones.csv"} <= {p.name for p in folder.iterdir()}


def test_transfer_rule_hold_with_every_exit_at_its_limit():
    """Straight pop-up, one exit "out" with round robin limit 1, onto a 2.0 m belt at 0.5 m/s
    with no eye (the count ends as the load leaves its end, R5); two loads at 0. Load 1 is routed
    at 0 and clears the entry at 0.82 (0.41 / 0.5). Load 2 takes the module at 0.82 with the exit
    at its limit and is held until load 1 leaves the belt at 4.0: 3.18 s; it clears the entry at
    4.82. Window 10: busy 4.82 (48.2 %) = rule hold 3.18 (31.8 %) + handshake 2 x 0.82 (16.4 %);
    route_holds: one hold, 3.18 s."""
    doc = build(xfer("popup", [{"port": "out", "angle": 0}],
                     rule={"kind": "round_robin", "limit": 1}),
                {"out": receiving("recv", eye_at=None, length=2.0)}, loads=2)
    results = run(doc, 10)
    t = row(results, "transfer_summary", "Transfer", "xfer")
    assert t["Busy %"] == pytest.approx(48.2)
    assert t["Rule Hold %"] == pytest.approx(31.8)
    assert t["Handshake %"] == pytest.approx(16.4)
    assert t["Rule Hold %"] + t["Handshake %"] + t["Move %"] == pytest.approx(t["Busy %"])
    hold = row(results, "route_holds", "Block", "xfer")
    assert hold["Holds"] == 1 and hold["Hold Time"] == pytest.approx(3.18)


# -- fix round 1 (R63-R66): window edges, reversible dieback, model rows ---------------------

def _held_line(parts, length, hold):
    """`parts` CASE items (0.41 m, cell 0.41, gap 0) one per second onto a typed belt `length`
    m at 0.5 m/s whose exit Op is held to t = `hold` (the Task 4 shape without zones)."""
    doc = zone_doc(None, parts, hold=hold)
    conveyor = next(b for b in doc["blocks"] if b["name"] == "belt")
    conveyor["params"]["length"] = length
    feed = next(b for b in doc["blocks"] if b["name"] == "c")
    feed["params"]["dist_params"] = {"value": 1.0}
    return doc


def test_a_deep_queue_straddling_the_warm_up():
    """10 parts on a 4.1 m belt (10 cells): part k boards at k and stands nose 4.1 - 0.41 k from
    k + (4.1 - 0.41 k) / 0.5 = 8.2 + 0.18 k, all by 9.82. Op frees at 30: the queue moves off
    nose to tail, part k leaves at 30 + 0.82 k. Warm-up 25, window [25, 35]: each part stands
    from 25 to 30, 5 s, and moves after (parts 7 to 9 still on the belt at 35, moving): 10 x 0.41
    x 5 / 10 = 2.05 m, never more than the belt's 4.1 m (R63: what each stood by 25 is banked)."""
    results = run(_held_line(10, 4.1, 30.0), 35, warmup=25.0)
    flow = row(results, "conveyor_flow", "Conveyor", "belt")
    assert flow["Dieback Length"] == pytest.approx(10 * 0.41 * 5 / 10)
    assert flow["Dieback Length"] <= 4.1
    assert flow["Time Full %"] == pytest.approx(50.0)     # 10 cells held over [25, 30)


def test_zones_and_dieback_after_a_warm_up():
    """The Task 4 zone case with warm-up 10, window [10, 30]: zone 1 stands from 10 to 21, 11 s
    (55 %); zone 2 from 11.96 to 22, 10.04 s (50.2 %). Dieback: the lead stands 10 to 21, the
    second (from 7.78) 10 to 21, the third 11.96 to 22: 0.41 x 32.04 / 20 = 0.65682 m."""
    results = run(zone_doc({"count": 3, "release_delay": 1.0}, 3), 30, warmup=10.0)
    assert list(results["conveyor_zones"]["Dieback Time"]) == pytest.approx([11.0, 10.04, 0.0])
    assert list(results["conveyor_zones"]["Dieback %"]) == pytest.approx([55.0, 50.2, 0.0])
    flow = row(results, "conveyor_flow", "Conveyor", "belt")
    assert flow["Dieback Length"] == pytest.approx(0.41 * 32.04 / 20)


def test_reversible_dieback_from_the_true_places():
    """The Task 9 typed queue (tests/test_conveyor_reverse_rules.py, BIG 0.6 m, SMALL 0.3 m):
    BIG1 stands at the end from 4.0 (held to 50); SMALL at 3.4 from 4.4 and BIG2 at 3.1 from
    5.1, until the turn at 10, then they ride to the start (13.4, 13.1). Window 20: 0.6 x 16 +
    0.3 x 5.6 + 0.6 x 4.9 = 14.22 m s, 0.711 m."""
    from test_conveyor_reverse_rules import _typed_queue
    flow = row(run(_typed_queue(), 20), "conveyor_flow", "Conveyor", "belt")
    assert flow["Dieback Length"] == pytest.approx(14.22 / 20)


def _metered_four():
    """The Task 5 gap case: 4 items every 0.82 s on the 3.0 m belt, metering gap 0.40. Holds at
    the end: 6.82 -> 7.62, 8.44 -> 9.24, 10.06 -> 10.86."""
    return _belt_doc({"metering": {"gap": 0.40}}, 4, 0.82)


def test_release_hold_share_counts_an_open_hold():
    """Window 10.5: the third hold is open, 10.06 to 10.5: 0.8 + 0.8 + 0.44 = 2.04 s, 19.43 %.
    Released and Avg read the holds that ended: 3, (0 + 0.8 + 0.8) / 3."""
    flow = row(run(_metered_four(), 10.5), "conveyor_flow", "Conveyor", "belt")
    assert flow["Release Hold %"] == pytest.approx(100 * 2.04 / 10.5)
    assert flow["Released"] == 3 and flow["Avg Release Hold"] == pytest.approx(1.6 / 3)


def test_release_hold_share_cuts_a_hold_at_the_warm_up():
    """Warm-up 9, window [9, 20]: the second hold counts from 9 to 9.24, the third whole:
    1.04 s, 9.45 %."""
    flow = row(run(_metered_four(), 20, warmup=9.0), "conveyor_flow", "Conveyor", "belt")
    assert flow["Release Hold %"] == pytest.approx(100 * 1.04 / 11)


def test_route_holds_rows_come_from_the_model():
    """A Rule DECIDE whose only load comes at 50, after the 10 s window: its row is there, zeros
    (R66). Two replications of the DECIDE hold case give the same rows in each, 0.5 s held."""
    from test_routing_at_points_rules import _decide_model
    unused = run(_decide_model([50.0], {"kind": "round_robin", "limit": 1}), 10)
    hold = row(unused, "route_holds", "Block", "route")
    assert (hold["Holds"], hold["Hold Time"], hold["Hold %"]) == (0, 0, 0)
    twice = run(_decide_model([0.0, 1.0, 1.5], {"kind": "round_robin", "limit": 1}), 10,
                replications=2)["route_holds"]
    assert list(twice["Run"]) == [0, 1] and list(twice["Block"]) == ["route", "route"]
    assert list(twice["Hold Time"]) == pytest.approx([0.5, 0.5])


def test_route_holds_after_a_warm_up():
    """The DECIDE hold (1.5 -> 2) with warm-up 1.75, window [1.75, 10]: 0.25 s held, 3.03 %; the
    hold began before the warm-up, so it is not one of the window's holds."""
    from test_routing_at_points_rules import _decide_model
    results = run(_decide_model([0.0, 1.0, 1.5], {"kind": "round_robin", "limit": 1}), 10,
                  warmup=1.75)
    hold = row(results, "route_holds", "Block", "route")
    assert hold["Holds"] == 0
    assert hold["Hold Time"] == pytest.approx(0.25)
    assert hold["Hold %"] == pytest.approx(100 * 0.25 / 8.25)
