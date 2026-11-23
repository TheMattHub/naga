"""CONVEYOR_TRANSFER (pop-up, turntable, lift): conveyor system A, task 8, written from the brief,
spec section 5 and rulings R2/R4/R5 before the code.

Every expected value is hand-derived in its docstring. Speed 0.5 m/s, items 0.41 m.

API assumptions (not read from the implementation):
  - Block type "CONVEYOR_TRANSFER", params as in the brief plus `entry_length` (R2, default 0).
    `exits` = [{"port": name, "angle": deg}, ...]; link k leaves by exit `port` name when the
    link has `source_port` == that name, else by position among the block's outgoing links
    (exit order). `rule` defaults to round robin, `return_home` to true.
  - A load is "accepted" the moment it enters the module (entry_length starts then). Busy time
    runs from acceptance to the handshake plus any lift-down / turn-back; the next load is
    accepted at the end of it. With no wait in front, load 1 is accepted at t = 0.
  - The load boards the receiving belt's entry the moment the module's own move (cross, turn,
    lift) finishes; a popup straight exit has no lift and no cross, the load boards at once.
  - The handshake time is read from the receiving CONVEYOR's photo-eye: a trace event
    kind "eye", state "front", block = receiving belt name. With no eye, exits are read from
    the entity log ("Exited At") of the dispose behind the receiving belt.
  - Lift ignores `lift_time` and `cross_length`: travel is lift_height / lift_speed each way.
  - turntable `return_home` false (controller ruling): the table stays at the exit angle after
    the handshake and turns back (a / turn_rate + turn_ramp) only once the next load waits,
    accepting it after that turn.
Timing is read with the engine's usual belt rules: a free load boards at b, its front is at x
at b + x / speed, and leaves a belt of length L at b + L / speed.
"""
import math

from conftest import create, dispose
from test_conveyor_divert_spec import belt, model

from nagasim import api
from nagasim.core.trace import TraceRecorder

SPEED = 0.5
ITEM = 0.41


def close(actual, expected):
    assert len(actual) == len(expected), (actual, expected)
    for a, e in zip(actual, expected):
        assert math.isclose(a, e, abs_tol=1e-9), (actual, expected)


def receiving(name, eye_at=None, length=5.0):
    """Free-flow belt, speed 0.5, optional eye. Spacing = the item length 0.41 (an untyped belt
    refuses an item longer than its cell, D1351), so the entry rhythm 0.82 s never makes a
    load wait: the shortest boarding interval below is 0.82 s."""
    conveyor = belt(name=name, length=length, speed=SPEED, spacing=ITEM)
    conveyor["params"]["eyes"] = [] if eye_at is None else [{"at": eye_at}]
    return conveyor


def xfer(mechanism, exits, **params):
    base = {"mechanism": mechanism, "exits": exits, "transfer_speed": SPEED}
    base.update(params)
    return {"name": "xfer", "description": "xfer", "type": "CONVEYOR_TRANSFER",
            "position": [1, 0], "params": base}


def build(transfer, targets, loads, feeder=None, ported=True, link_order=None, spaced=None):
    """CREATE (loads at t = 0, size 0.41) -> [feeder belt] -> transfer -> one receiving belt
    per exit (-> its own DISPOSE). `targets` maps exit port name -> receiving belt block."""
    if spaced:  # `loads` single loads, `spaced` seconds apart, the first at t = 0
        blocks = [create(e_type="BOX", max_arrivals=loads, dist_params={"value": spaced},
                         start_time=0)]
    else:
        blocks = [create(e_type="BOX", entities_per_arrival=loads, max_arrivals=1,
                         start_time=0)]
    links = []
    head = "create_1"
    if feeder is not None:
        blocks.append(feeder)
        links.append({"source": "create_1", "target": feeder["name"]})
        head = feeder["name"]
    blocks.append(transfer)
    links.append({"source": head, "target": "xfer"})
    out = []
    for i, (port, belt_block) in enumerate(targets.items()):
        blocks += [belt_block, dispose(name=f"dispose_{i}")]
        link = {"source": "xfer", "target": belt_block["name"]}
        if ported:
            link["source_port"] = port
        out.append(link)
        links.append({"source": belt_block["name"], "target": f"dispose_{i}"})
    links += [out[i] for i in (link_order or range(len(out)))]
    # The times above are seconds (speed 0.5 m/s); the module's measured defaults carry
    # their own units (R30), so the model says its time unit.
    doc = model(blocks, links, time_unit="seconds")
    doc["entities"] = [{"name": "BOX", "size": {"length": ITEM, "width": 0.3, "height": 0.2}}]
    return doc


def run(doc):
    recorder = TraceRecorder()
    results = api.run_model(doc, replications=1, trace=recorder)
    return results, recorder


def fronts(recorder, block):
    return sorted(e.t for e in recorder.events
                  if e.kind == "eye" and e.state == "front" and e.block == block)


def popup_side(**params):
    return xfer("popup", [{"port": "side", "angle": 90}], lift_time=0.1, cross_length=0.6, **params)


def test_popup_side_cycle_is_2_seconds_per_load():
    """Load 1 accepted at 0. Up 0.1, cross 0.6 / 0.5 = 1.2, so it boards the receiving belt at
    1.3; its front reaches the eye at 0.3 m at 1.3 + 0.3 / 0.5 = 1.9 (the handshake); down 0.1
    frees the module at 2.0. Loads wait, so loads 2 and 3 are accepted at 2.0 and 4.0 and the
    eye sees them at 3.9 and 5.9."""
    doc = build(popup_side(), {"side": receiving("recv", eye_at=0.3)}, loads=3)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [1.9, 3.9, 5.9])


def test_popup_side_cycle_through_a_feeding_conveyor():
    """Feeder length 1.0 at 0.5: the single load reaches its end, so the module, at 2.0. Then as
    above: eye at 2.0 + 1.9 = 3.9."""
    feeder = belt(name="feeder", length=1.0, speed=SPEED, spacing=ITEM)
    doc = build(popup_side(), {"side": receiving("recv", eye_at=0.3)}, loads=1, feeder=feeder)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [3.9])


def test_popup_straight_has_no_lift_and_no_cross():
    """Straight exit (angle 0): no lift up/down and no cross; the load boards at the accept
    time. Eye at 0.5 m: handshake 0.5 / 0.5 = 1.0 = the busy time. Accepts 0, 1.0, 2.0; eye
    events 1.0, 2.0, 3.0."""
    doc = build(xfer("popup", [{"port": "straight", "angle": 0}], lift_time=0.1,
                     cross_length=0.6),
                {"straight": receiving("recv", eye_at=0.5)}, loads=3)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [1.0, 2.0, 3.0])


def test_turntable_90_cycle_is_13_4_seconds():
    """entry_length 1.0 at 0.5 = 2.0 s; turn 90 / 18 + 0.3 = 5.3 s, so the load boards at 7.3;
    eye at 0.4 m: 0.4 / 0.5 = 0.8 s, handshake 8.1; turn back 5.3: free at 13.4 = 2.0 + 5.3 +
    0.8 + 5.3. Load 2 is accepted at 13.4 and its eye fires at 13.4 + 8.1 = 21.5; load 3 at
    26.8, eye at 34.9."""
    doc = build(xfer("turntable", [{"port": "east", "angle": 90}], entry_length=1.0,
                     turn_rate=18, turn_ramp=0.3),
                {"east": receiving("recv", eye_at=0.4)}, loads=3)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [8.1, 21.5, 34.9])


def test_turntable_queued_loads_cost_the_same_with_or_without_return_home():
    """Loads waiting back to back: with return_home false the table turns back only because the
    next load waits, 5.3 s, then accepts it. Same 13.4 s cycle as return_home true: eye events
    8.1, 21.5, 34.9."""
    doc = build(xfer("turntable", [{"port": "east", "angle": 90}], entry_length=1.0,
                     turn_rate=18, turn_ramp=0.3, return_home=False),
                {"east": receiving("recv", eye_at=0.4)}, loads=3)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [8.1, 21.5, 34.9])


def test_turntable_return_home_false_turns_back_only_when_the_next_load_waits():
    """Load 1 at 0, load 2 reaching the module at 30. Load 1: entry 2.0, turn 5.3, eye 0.8:
    handshake 8.1 (eye event 8.1) either way. return_home true: table back home at
    8.1 + 5.3 = 13.4, accepts load 2 at 30, eye at 30 + 8.1 = 38.1. return_home false: the
    table sits at 90 until 30, turns back 30 -> 35.3, accepts then, eye at 35.3 + 8.1 = 43.4."""
    for home, expected in ((True, [8.1, 38.1]), (False, [8.1, 43.4])):
        doc = build(xfer("turntable", [{"port": "east", "angle": 90}], entry_length=1.0,
                         turn_rate=18, turn_ramp=0.3, return_home=home),
                    {"east": receiving("recv", eye_at=0.4)}, loads=2, spaced=30)
        _results, rec = run(doc)
        close(fronts(rec, "recv"), expected)


def test_lift_two_levels_cycle():
    """lift_height 1.2 / lift_speed 0.3 = 4.0 s each way. Accept 0, up 4.0, board at 4.0; eye
    at 0.5 m: 1.0 s, handshake 5.0; down 4.0: free at 9.0 (= 4.0 + 1.0 + 4.0). Accepts 0, 9.0,
    18.0; eye events 5.0, 14.0, 23.0."""
    doc = build(xfer("lift", [{"port": "upper", "angle": 0}], lift_height=1.2, lift_speed=0.3),
                {"upper": receiving("recv", eye_at=0.5)}, loads=3)
    _results, rec = run(doc)
    close(fronts(rec, "recv"), [5.0, 14.0, 23.0])


def test_no_eye_frees_the_module_when_the_load_clears_the_entry_by_item_length():
    """Straight popup, receiving belt without an eye. Load boards at the accept time and
    clears the entry by item_length 0.41 after 0.41 / 0.5 = 0.82 s (R5), when the next load is
    accepted. Accepts 0, 0.82, 1.64; each leaves the 5.0 m belt 10.0 s after boarding: exits
    at 10.0, 10.82, 11.64."""
    doc = build(xfer("popup", [{"port": "straight", "angle": 0}]),
                {"straight": receiving("recv", eye_at=None)}, loads=3)
    results, _rec = run(doc)
    log = results["entity_log"]
    close(sorted(log[log["Type"] == "BOX"]["Exited At"].tolist()), [10.0, 10.82, 11.64])


def two_exit_doc(**build_args):
    exits = [{"port": "a", "angle": 90}, {"port": "b", "angle": -90}]
    return build(xfer("popup", exits, lift_time=0.1, cross_length=0.6),
                 {"a": receiving("recv_a", eye_at=0.3), "b": receiving("recv_b", eye_at=0.3)},
                 loads=4, **build_args)


def test_default_rule_is_round_robin_over_two_exits():
    """No `rule`: loads 1..4 go to a, b, a, b. Each cycle is 2.0 s (as the first test), so
    accepts 0, 2.0, 4.0, 6.0 and eye fronts at accept + 1.9: a sees 1.9 and 5.9, b sees 3.9
    and 7.9."""
    _results, rec = run(two_exit_doc())
    close(fronts(rec, "recv_a"), [1.9, 5.9])
    close(fronts(rec, "recv_b"), [3.9, 7.9])


def test_links_follow_the_exit_port_names_not_the_link_order():
    """Links listed b first then a, with source_port set: load 1 still goes to exits[0] = a
    (eye 1.9 on recv_a), load 2 to b."""
    _results, rec = run(two_exit_doc(link_order=[1, 0]))
    close(fronts(rec, "recv_a"), [1.9, 5.9])
    close(fronts(rec, "recv_b"), [3.9, 7.9])


def test_links_without_ports_follow_the_exit_order():
    """No source_port on the links: link 0 = exits[0] (a), link 1 = exits[1] (b); the same
    times as above."""
    _results, rec = run(two_exit_doc(ported=False))
    close(fronts(rec, "recv_a"), [1.9, 5.9])
    close(fronts(rec, "recv_b"), [3.9, 7.9])
