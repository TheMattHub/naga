"""Timeline: the pure-Python playback model behind the canvas animation (M4e).

Hand-built traces here, no engine: the point is that the occupancy rebuilt from
the events matches what the model means, independently of how a run produced them.
"""
import time

import pytest

from nagasim.core.playback import Timeline
from nagasim.core.trace import TraceEvent


def created(t, eid, block, e_type="A"):
    return TraceEvent(t=t, kind="entity_created", entity_id=eid, e_type=e_type,
                      block=block, target=None)


def moved(t, eid, block, target, e_type="A", duration=0.0):
    return TraceEvent(t=t, kind="entity_moved", entity_id=eid, e_type=e_type,
                      block=block, target=target, duration=duration)


def disposed(t, eid, block, e_type="A"):
    return TraceEvent(t=t, kind="entity_disposed", entity_id=eid, e_type=e_type,
                      block=block, target=None)


def seized(t, eid, block, resource, e_type="A"):
    return TraceEvent(t=t, kind="entity_seized", entity_id=eid, e_type=e_type,
                      block=block, target=resource)


def released(t, eid, block, resource, e_type="A"):
    return TraceEvent(t=t, kind="entity_released", entity_id=eid, e_type=e_type,
                      block=block, target=resource)


def resource_state(t, resource, state, value):
    return TraceEvent(t=t, kind="resource_state", entity_id=-1, e_type="", block="",
                      target=None, resource=resource, state=state, value=value)


def create_delay_dispose(eid, t_create, service):
    """Create -> Delay (busy for `service`) -> Dispose, as the engine emits it."""
    return [
        created(t_create, eid, "create_1"),
        moved(t_create, eid, "create_1", "delay_1"),
        moved(t_create + service, eid, "delay_1", "dispose_1"),
        disposed(t_create + service, eid, "dispose_1"),
    ]


# -- occupancy ------------------------------------------------------------

def test_state_at_counts_the_entity_inside_the_delay_block():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    assert timeline.state_at(2.0)["counts"] == {"delay_1": 1}
    assert timeline.state_at(2.0)["entities"] == {1: ("delay_1", "A")}


def test_a_created_entity_leaves_the_create_block_at_the_same_instant():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    # created and moved both at t=0: the zero-length visit must not linger.
    assert timeline.state_at(0.0)["counts"] == {"delay_1": 1}


def test_disposal_ends_the_life_and_empties_every_block():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    assert timeline.state_at(5.0)["counts"] == {}
    assert timeline.state_at(5.0)["entities"] == {}


def test_state_before_the_first_event_is_empty():
    timeline = Timeline(create_delay_dispose(1, 3.0, 5.0))

    assert timeline.state_at(1.0)["counts"] == {}


def test_seize_queue_holds_every_waiting_entity_in_the_seize_block():
    # Three arrivals at t=0,1,2; the server takes 10 each, so they pile up in
    # seize_1 (queue + service look the same from the trace -- there is no event
    # for "service started").
    events = []
    for i, arrival in enumerate([0.0, 1.0, 2.0], start=1):
        events += [created(arrival, i, "create_1"), moved(arrival, i, "create_1", "seize_1")]
    events += [moved(10.0, 1, "seize_1", "release_1"), moved(10.0, 1, "release_1", "dispose_1"),
               disposed(10.0, 1, "dispose_1")]
    timeline = Timeline(events)

    assert timeline.state_at(0.0)["counts"] == {"seize_1": 1}
    assert timeline.state_at(2.0)["counts"] == {"seize_1": 3}
    assert timeline.state_at(9.9)["counts"] == {"seize_1": 3}
    assert timeline.state_at(10.0)["counts"] == {"seize_1": 2}


def test_in_service_counts_only_the_entity_holding_the_resource():
    # Entity 1 seizes immediately and is served until t=10; entities 2 and 3
    # arrive later and queue in the same block without an entity_seized.
    events = []
    for i, arrival in enumerate([0.0, 1.0, 2.0], start=1):
        events += [created(arrival, i, "create_1"), moved(arrival, i, "create_1", "seize_1")]
    events += [seized(0.0, 1, "seize_1", "Server")]
    events += [moved(10.0, 1, "seize_1", "release_1"), released(10.0, 1, "release_1", "Server"),
               moved(10.0, 1, "release_1", "dispose_1"), disposed(10.0, 1, "dispose_1")]
    events += [seized(10.0, 2, "seize_1", "Server")]
    timeline = Timeline(events)

    assert timeline.state_at(2.0)["counts"] == {"seize_1": 3}
    assert timeline.state_at(2.0)["in_service"] == {"seize_1": 1}   # entity 1 serving, 2 and 3 waiting

    # Entity 1 leaves the block at t=10: in_service follows it out.
    assert timeline.state_at(10.0)["counts"] == {"seize_1": 2}
    assert timeline.state_at(10.0)["in_service"] == {"seize_1": 1}  # entity 2 now serving


def test_entity_released_does_not_change_occupancy():
    # entity_released fires on the Release block, a different block from the
    # Seize that holds the entity -- it must not touch either block's counts.
    events = [created(0.0, 1, "create_1"), moved(0.0, 1, "create_1", "seize_1"),
              seized(0.0, 1, "seize_1", "Server"),
              moved(5.0, 1, "seize_1", "release_1"), released(5.0, 1, "release_1", "Server")]
    timeline = Timeline(events)

    state = timeline.state_at(5.0)
    assert state["counts"] == {"release_1": 1}
    assert state["in_service"] == {}


# -- moves ---------------------------------------------------------------

def test_moves_between_is_half_open_on_the_left():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    at_zero = timeline.moves_between(-1.0, 0.0)
    assert [(m[2], m[3]) for m in at_zero] == [("create_1", "delay_1")]
    assert timeline.moves_between(0.0, 4.0) == []  # t=0 already consumed above


def test_consecutive_windows_tile_the_run_without_double_counting():
    events = create_delay_dispose(1, 0.0, 5.0) + create_delay_dispose(2, 1.0, 5.0)
    timeline = Timeline(events)

    edges = [-1.0, 0.5, 1.5, 3.0, 6.0, 10.0]
    windowed = []
    for t0, t1 in zip(edges, edges[1:]):
        windowed += timeline.moves_between(t0, t1)

    assert windowed == timeline.moves_between(-1.0, 10.0)
    assert len(windowed) == 4  # two moves per entity, disposals are not moves


def test_moves_carry_entity_id_type_source_target_and_time():
    timeline = Timeline(create_delay_dispose(7, 2.0, 3.0))

    # ... and the BRANCH taken (D77): -1 here, since the hand-built events in
    # this module name no branch.
    assert timeline.moves_between(2.5, 5.0) == [(7, "A", "delay_1", "dispose_1", 5.0, -1)]


# -- counters and duration ------------------------------------------------

def test_created_and_disposed_counters_are_cumulative():
    events = create_delay_dispose(1, 0.0, 5.0) + create_delay_dispose(2, 1.0, 5.0)
    timeline = Timeline(events)

    assert (timeline.created_until(0.5), timeline.disposed_until(0.5)) == (1, 0)
    assert (timeline.created_until(1.0), timeline.disposed_until(1.0)) == (2, 0)
    assert (timeline.created_until(5.0), timeline.disposed_until(5.0)) == (2, 1)
    assert (timeline.created_until(99.0), timeline.disposed_until(99.0)) == (2, 2)


def test_duration_is_the_last_event_time_and_zero_for_an_empty_trace():
    assert Timeline(create_delay_dispose(1, 0.0, 5.0)).duration == 5.0
    assert Timeline([]).duration == 0.0
    assert Timeline([]).state_at(10.0) == {
        "entities": {}, "counts": {}, "in_service": {}, "in_transit": {}, "movers": {},
        "resources": {}, "queues": {}, "levels": {}, "agents": {},
        "agent_links": [], "entity_agents": {}, "soc": {}, "chargers": {},
        "charging": set(), "phases": {}, "tending": {}, "handling": {},
        "placed": {}, "phases_all": {}}


def test_next_time_returns_the_first_event_strictly_after_t():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    assert timeline.next_time(0.0) == 5.0
    assert timeline.next_time(5.0) is None


# -- cursor ---------------------------------------------------------------

def test_backwards_seek_matches_a_fresh_forward_scan():
    events = []
    for i in range(1, 30):
        events += create_delay_dispose(i, float(i), 4.0)
    timeline = Timeline(events)

    timeline.state_at(50.0)              # push the cursor to the end
    rewound = timeline.state_at(12.0)    # ... then seek back

    assert rewound == Timeline(events).state_at(12.0)


def test_repeated_calls_at_the_same_time_are_stable():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    assert timeline.state_at(2.0) == timeline.state_at(2.0)


def test_state_at_returns_copies_a_caller_may_keep():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    snapshot = timeline.state_at(2.0)
    timeline.state_at(5.0)

    assert snapshot["counts"] == {"delay_1": 1}


# -- performance ----------------------------------------------------------

# -- in transit (ROADMAP 1.5: link transfer delays) -----------------------

def test_in_transit_entity_is_in_no_block_during_the_delay():
    events = [
        created(0.0, 1, "create_1"),
        moved(0.0, 1, "create_1", "delay_1"),
        moved(3.0, 1, "delay_1", "dispose_1", duration=5.0),
        disposed(8.0, 1, "dispose_1"),
    ]
    timeline = Timeline(events)

    state = timeline.state_at(5.0)
    assert state["counts"] == {}
    assert state["entities"] == {}
    src, dst, progress, e_type, _path, _branch = state["in_transit"][1]
    assert (src, dst, e_type) == ("delay_1", "dispose_1", "A")
    assert progress == pytest.approx((5.0 - 3.0) / 5.0)


def test_in_transit_progress_is_linear_and_entity_enters_dst_at_t_plus_duration():
    events = [
        created(0.0, 1, "create_1"),
        moved(0.0, 1, "create_1", "delay_1"),
        moved(2.0, 1, "delay_1", "dispose_1", duration=4.0),
    ]
    timeline = Timeline(events)

    assert timeline.state_at(2.0)["in_transit"][1][2] == pytest.approx(0.0)
    assert timeline.state_at(4.0)["in_transit"][1][2] == pytest.approx(0.5)
    assert timeline.state_at(6.0)["in_transit"] == {}
    assert timeline.state_at(6.0)["counts"] == {"dispose_1": 1}
    assert timeline.state_at(6.0)["entities"] == {1: ("dispose_1", "A")}


def test_zero_duration_move_never_enters_in_transit():
    """Existing (pre-1.5) behaviour, unchanged: an instant move never shows up
    in `in_transit`, it is simply counted in the target block right away."""
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    assert timeline.state_at(2.0)["in_transit"] == {}
    assert timeline.state_at(2.0)["counts"] == {"delay_1": 1}


def test_backwards_seek_through_a_transit_window_replays_correctly():
    events = [
        created(0.0, 1, "create_1"),
        moved(0.0, 1, "create_1", "delay_1"),
        moved(10.0, 1, "delay_1", "dispose_1", duration=250.0),
    ]
    timeline = Timeline(events)

    timeline.state_at(270.0)                       # past the arrival at t=260
    assert timeline.state_at(270.0)["in_transit"] == {}
    assert timeline.state_at(270.0)["counts"] == {"dispose_1": 1}

    rewound = timeline.state_at(100.0)             # seek back into the transit window
    assert rewound["counts"] == {}
    assert 1 in rewound["in_transit"]
    assert rewound == Timeline(events).state_at(100.0)  # matches a fresh forward scan


# -- live mode (PHASE6 L1: bounded-memory canvas for a game in progress) --

def test_live_timeline_fires_a_same_instant_arrival_across_a_chunk_boundary():
    """A delayed arrival becomes due at the LAST event of one chunk, and the
    next chunk continues at that same instant. An arrival fires before every
    event at or after its instant (#231), so the live Timeline needs no undo
    and matches a one-pass build at every chunk."""
    chunk1 = [created(0.0, 1, "A"), moved(3.0, 1, "A", "B", duration=2.0),  # arrival due t=5.0
             created(5.0, 4, "Z")]                                          # chunk 1 also ends at t=5.0
    chunk2 = [created(5.0, 2, "B")]                                         # same instant, chunk 2
    chunk3 = [created(6.0, 3, "C")]                                         # closes the instant

    live = Timeline([], live=True)
    prefix = []
    for chunk in (chunk1, chunk2, chunk3):
        live.append(chunk)
        prefix += chunk
        t = chunk[-1].t
        assert live.state_at(t) == Timeline(prefix).state_at(t)
    assert live.state_at(6.0)["queues"]["B"] == [1, 2]     # the arrival first


def test_live_timeline_retains_no_event_log_however_much_is_fed():
    """The whole point of live mode: unlike the capped `TraceRecorder` the
    canvas used to stream from, feeding it many times DEFAULT_MAX_EVENTS
    worth of events costs O(1) memory, not O(events) -- it never stops."""
    from nagasim.core.trace import DEFAULT_MAX_EVENTS

    live = Timeline([], live=True)
    n = DEFAULT_MAX_EVENTS * 3
    for i in range(n):
        t = float(i)
        live.append([created(t, i, "A"), disposed(t, i, "A")])

    assert len(live) == 2 * n              # a plain counter: cheap regardless of value
    assert live._events == []              # nothing retained to replay from
    assert live._entities == {}            # every entity disposed right away: no build-up
    assert live.duration == float(n - 1)


# -- resources (ROADMAP 2.3 part C) ---------------------------------------

def test_resource_state_tracks_busy_and_idle_from_seize_and_release():
    events = [
        resource_state(0.0, "Server", "capacity", 1),
        created(0.0, 1, "create_1"), moved(0.0, 1, "create_1", "seize_1"),
        seized(0.0, 1, "seize_1", "Server"),
        moved(5.0, 1, "seize_1", "release_1"), released(5.0, 1, "release_1", "Server"),
    ]
    timeline = Timeline(events)

    assert timeline.state_at(2.0)["resources"] == {"Server": {"state": "busy", "busy": 1, "capacity": 1}}
    assert timeline.state_at(5.0)["resources"] == {"Server": {"state": "idle", "busy": 0, "capacity": 1}}


def test_resource_off_when_the_scheduled_capacity_is_zero():
    events = [
        resource_state(0.0, "Server", "capacity", 1),
        resource_state(5.0, "Server", "capacity", 0),
        resource_state(10.0, "Server", "capacity", 1),
    ]
    timeline = Timeline(events)

    assert timeline.state_at(2.0)["resources"]["Server"]["state"] == "idle"
    assert timeline.state_at(7.0)["resources"]["Server"] == {"state": "off", "busy": 0, "capacity": 0}
    assert timeline.state_at(12.0)["resources"]["Server"]["state"] == "idle"


def test_resource_down_wins_over_busy_and_capacity():
    events = [
        resource_state(0.0, "Server", "capacity", 1),
        created(0.0, 1, "create_1"), moved(0.0, 1, "create_1", "seize_1"),
        seized(0.0, 1, "seize_1", "Server"),
        resource_state(3.0, "Server", "down", 1),
        resource_state(8.0, "Server", "up", 0),
    ]
    timeline = Timeline(events)

    assert timeline.state_at(1.0)["resources"]["Server"]["state"] == "busy"
    assert timeline.state_at(5.0)["resources"]["Server"] == {"state": "down", "busy": 1, "capacity": 1}
    assert timeline.state_at(9.0)["resources"]["Server"]["state"] == "busy"     # still holding Server


def test_a_resource_with_no_event_at_all_is_simply_absent():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))
    assert timeline.state_at(2.0)["resources"] == {}


# -- queues (ROADMAP 2.3 part C) -------------------------------------------

def test_queues_hold_waiting_entities_in_arrival_order_excluding_in_service():
    events = []
    for i, arrival in enumerate([0.0, 1.0, 2.0], start=1):
        events += [created(arrival, i, "create_1"), moved(arrival, i, "create_1", "seize_1")]
    events += [seized(0.0, 1, "seize_1", "Server")]
    timeline = Timeline(events)

    assert timeline.state_at(2.0)["queues"] == {"seize_1": [2, 3]}

    events += [moved(10.0, 1, "seize_1", "release_1"), released(10.0, 1, "release_1", "Server"),
              disposed(10.0, 1, "release_1"), seized(10.0, 2, "seize_1", "Server")]
    timeline = Timeline(events)
    assert timeline.state_at(10.0)["queues"] == {"seize_1": [3]}   # 1 gone, 2 now serving


def test_a_block_that_never_seizes_shows_its_full_occupant_list_as_the_queue():
    """HOLD/BATCH/MATCH never emit entity_seized: their whole store is the queue."""
    events = [created(0.0, 1, "create_1"), moved(0.0, 1, "create_1", "hold_1"),
              created(1.0, 2, "create_1"), moved(1.0, 2, "create_1", "hold_1")]
    timeline = Timeline(events)
    assert timeline.state_at(1.0)["queues"] == {"hold_1": [1, 2]}


def test_backwards_seek_matches_forward_replay_for_resources_and_queues():
    events = [
        resource_state(0.0, "Server", "capacity", 1),
        created(0.0, 1, "create_1"), moved(0.0, 1, "create_1", "seize_1"),
        seized(0.0, 1, "seize_1", "Server"),
        created(1.0, 2, "create_1"), moved(1.0, 2, "create_1", "seize_1"),
        resource_state(3.0, "Server", "down", 1),
        moved(5.0, 1, "seize_1", "release_1"), released(5.0, 1, "release_1", "Server"),
        resource_state(8.0, "Server", "up", 0),
    ]
    timeline = Timeline(events)

    timeline.state_at(9.0)                          # push the cursor to the end
    rewound = timeline.state_at(2.0)                # ... then seek back

    assert rewound == Timeline(events).state_at(2.0)
    assert rewound["resources"]["Server"]["state"] == "busy"
    assert rewound["queues"] == {"seize_1": [2]}


def test_an_older_trace_with_no_resource_state_events_still_loads():
    """A TraceEvent built the pre-2.3c way (resource/state/value all left to
    their defaults) must not crash Timeline, and simply contributes nothing
    to `resources`."""
    events = create_delay_dispose(1, 0.0, 5.0)
    assert all(e.resource is None and e.state is None and e.value == 0.0 for e in events)
    timeline = Timeline(events)
    assert timeline.state_at(2.0)["resources"] == {}


def test_fifty_thousand_events_answer_a_thousand_state_calls_quickly():
    events = []
    for i in range(1, 12_501):  # 4 events each -> 50k, a fifth of the recorder's cap
        events += create_delay_dispose(i, i * 0.01, 5.0)
    timeline = Timeline(events)
    end = timeline.duration

    start = time.perf_counter()
    for k in range(1000):
        timeline.state_at(end * k / 1000.0)
    elapsed = time.perf_counter() - start

    assert len(events) == 50_000
    assert elapsed < 1.0, f"1000 state_at calls took {elapsed:.2f}s"


# -- agent-linked entity tokens (PHASE6 H4) --------------------------------

def created_with_agent(t, eid, block, pop, index, e_type="A"):
    """An entity born with `entity.agent` set (CREATE `population`, or an
    agent's `enter`): trace.py rides (population, index) on entity_created's
    otherwise-unused target/value."""
    return TraceEvent(t=t, kind="entity_created", entity_id=eid, e_type=e_type,
                      block=block, target=pop, value=float(index))


def agent_state(t, pop, index, state):
    return TraceEvent(t=t, kind="agent_state", entity_id=index, e_type="", block="",
                      target=pop, state=state)


def test_entity_agents_reports_and_follows_its_agents_state():
    """`entity_agents` reads the agent's OWN agent_state trace (the one
    `agents[...]`'s category also comes from), so it works with no
    agent_moved segment at all -- an agent with no `space` never gets one."""
    events = [
        created_with_agent(0.0, 1, "delay_1", "Workers", 0),
        agent_state(0.0, "Workers", 0, "Idle"),
        agent_state(3.0, "Workers", 0, "Busy"),
    ]
    timeline = Timeline(events)

    assert timeline.state_at(1.0)["entity_agents"] == {1: ("Idle", "")}
    assert timeline.state_at(3.0)["entity_agents"] == {1: ("Busy", "")}   # follows the change


def test_entity_agents_is_empty_for_an_entity_with_no_agent():
    timeline = Timeline(create_delay_dispose(1, 0.0, 5.0))

    assert timeline.state_at(2.0)["entity_agents"] == {}


def test_entity_agents_survives_a_move_and_drops_the_entity_once_disposed():
    events = [
        created_with_agent(0.0, 1, "create_1", "Workers", 0),
        agent_state(0.0, "Workers", 0, "Idle"),
        *create_delay_dispose(1, 0.0, 5.0)[1:],   # move to delay_1, then dispose_1 + dispose
    ]
    timeline = Timeline(events)

    assert timeline.state_at(1.0)["entity_agents"] == {1: ("Idle", "")}   # mid-move: still there
    assert timeline.state_at(5.0)["entity_agents"] == {}                 # disposed: gone


# -- cycle phases and tending (animated 3D, D175) -----------------------------

def _phase(t, eid, block, kind, index, duration):
    return TraceEvent(t=t, kind="cycle_phase", entity_id=eid, e_type="A", block=block,
                      target=None, state=kind, value=float(index), duration=duration)


def test_phases_give_the_cycle_segment_a_motion_block_is_in():
    timeline = Timeline([
        created(0.0, 1, "cell_1/work"),
        _phase(10.0, 1, "cell_1/work", "pick", 1, 4.0),
        _phase(20.0, 1, "cell_1/work", "move", 2, 2.0),
        _phase(22.0, 1, "cell_1/work", "place", 3, 100.0),
        moved(30.0, 1, "cell_1/work", "sink"),
    ])
    assert timeline.state_at(5.0)["phases"] == {}
    assert timeline.state_at(12.0)["phases"] == {"cell_1": ("pick", 1, 0.5)}
    assert timeline.state_at(14.5)["phases"] == {}              # the segment's time is over
    assert timeline.state_at(21.0)["phases"] == {"cell_1": ("move", 2, 0.5)}
    assert (timeline.state_at(21.0)["placed"], timeline.state_at(23.0)["placed"]) == (
        {}, {"cell_1": 1})
    assert timeline.state_at(31.0)["phases"] == {}              # the part left mid-segment
    assert timeline.state_at(12.0)["phases"] == {"cell_1": ("pick", 1, 0.5)}    # a seek back
    assert Timeline([]).state_at(1.0)["tending"] == {}


def test_two_parts_in_one_block_keep_a_slot_each():
    timeline = Timeline([
        created(0.0, 1, "cell"), created(0.0, 2, "cell"),
        _phase(1.0, 1, "cell", "pick", 1, 4.0),
        _phase(2.0, 2, "cell", "pick", 1, 4.0),
        _phase(5.0, 1, "cell", "place", 3, 4.0),
        moved(7.0, 1, "cell", "sink"),
        created(7.0, 3, "cell"),
        _phase(8.0, 3, "cell", "pick", 1, 4.0),
    ])
    assert timeline.state_at(3.0)["phases_all"] == {"cell": {0: ("pick", 1, 0.5),
                                                             1: ("pick", 1, 0.25)}}
    assert timeline.state_at(5.5)["phases_all"]["cell"][0][0] == "place"     # same slot
    assert timeline.state_at(9.0)["phases_all"] == {"cell": {0: ("pick", 1, 0.25)}}  # freed, reused
    assert timeline.state_at(3.0)["phases"] == {"cell": ("pick", 1, 0.25)}   # the latest, as before


def test_a_part_gives_up_its_arm_when_it_moves_on_inside_the_instance():
    """Seen from outside, a move between two inner blocks stays in the instance; the
    part has still left the block it was cycling in, so the next part gets slot 0."""
    timeline = Timeline([
        created(0.0, 1, "cell_1/work"),
        _phase(1.0, 1, "cell_1/work", "pick", 1, 4.0),
        moved(3.0, 1, "cell_1/work", "cell_1/buffer"),
        created(3.0, 2, "cell_1/work"),
        _phase(4.0, 2, "cell_1/work", "pick", 1, 4.0),
    ])
    assert timeline.state_at(2.0)["phases_all"] == {"cell_1": {0: ("pick", 1, 0.25)}}
    assert timeline.state_at(3.5)["phases_all"] == {} and timeline.state_at(3.5)["phases"] == {}
    assert timeline.state_at(5.0)["phases_all"] == {"cell_1": {0: ("pick", 1, 0.25)}}


def test_tending_gives_the_unit_at_work_on_a_station():
    tend = TraceEvent(t=5.0, kind="tend", entity_id=0, e_type="", block="", target="F",
                      resource="S", state="place", duration=2.0)
    timeline = Timeline([tend])
    assert timeline.state_at(6.0)["tending"] == {("F", 0): ("S", "place", 0.5)}
    assert timeline.state_at(7.5)["tending"] == {}
    assert timeline.state_at(4.0)["tending"] == {}


def _leg(t, unit, state, path, duration):
    return TraceEvent(t=t, kind="mover_moved", entity_id=unit, e_type="", block="", target="F",
                      resource="F", state=state, duration=duration, travel=duration, path=path)


def test_a_handling_leg_keeps_the_vehicle_where_it_stopped_and_says_which_way():
    """Loading follows an empty drive, unloading a loaded one; the vehicle stays
    at the end of the path it arrived by (so it keeps its heading)."""
    timeline = Timeline([
        _leg(0.0, 0, "empty", ("A", "B"), 10.0),
        _leg(10.0, 0, "handling", ("B",), 4.0),
        _leg(14.0, 0, "loaded", ("B", "C"), 10.0),
        _leg(24.0, 0, "handling", ("C",), 2.0),
        _leg(26.0, 0, "idle", ("C",), 0.0),
    ])
    state = timeline.state_at(11.0)
    assert state["movers"][("F", 0)] == (("A", "B"), 1.0, "handling")
    assert state["handling"] == {("F", 0): (False, 0.25)}           # loading
    assert timeline.state_at(15.0)["handling"] == {}
    assert timeline.state_at(25.0)["handling"] == {("F", 0): (True, 0.5)}   # unloading
    assert timeline.state_at(27.0)["handling"] == {}
    # A handling leg nothing follows (a cobot that stays, a trace cut at the cap) ends
    # with its own time: the vehicle is idle after it, not handling for ever.
    cut = Timeline([_leg(0.0, 0, "loaded", ("A", "B"), 10.0), _leg(10.0, 0, "handling", ("B",), 4.0)])
    assert cut.state_at(12.0)["movers"][("F", 0)][2] == "handling"
    late = cut.state_at(20.0)
    assert late["movers"][("F", 0)] == (("A", "B"), 1.0, "idle") and late["handling"] == {}
    # Parked at the end of its last drive: still on that path, so it keeps its heading.
    assert timeline.state_at(27.0)["movers"][("F", 0)] == (("B", "C"), 1.0, "idle")
    assert timeline.state_at(11.0)["handling"] == {("F", 0): (False, 0.25)}


# -- #336: an arrival recomputed a float after the entity's own next move ---------------
def _late_arrival():
    """A delayed move the engine ended at `(t0 + travel) + handling` = 1.2, which
    `Timeline` recomputes as `t0 + (travel + handling)` = 1.2000000000000002."""
    t0, travel, handling = 0.1, 0.1, 1.0
    arrived = (t0 + travel) + handling
    assert arrived < t0 + (travel + handling)
    leg = TraceEvent(t=t0, kind="entity_moved", entity_id=1, e_type="A", block="create",
                     target="dock", duration=travel + handling, travel=travel)
    return [created(0.0, 1, "create"), leg], arrived


def test_an_entity_that_moves_on_as_it_arrives_is_counted_once():
    start, arrived = _late_arrival()
    events = start + [moved(arrived, 1, "dock", "shelf"), moved(5.0, 1, "shelf", "out"),
                      disposed(5.0, 1, "out")]
    for times in ([0.5, 3.0, 6.0], [6.0], [1.2, 1.2000000000000002, 3.0, 6.0]):
        timeline = Timeline(events)
        states = {t: timeline.state_at(t) for t in times}
        assert states[6.0]["counts"] == {} and states[6.0]["entities"] == {}
        if 3.0 in states:
            assert states[3.0]["counts"] == {"shelf": 1} and states[3.0]["in_transit"] == {}
    assert Timeline(events).state_at(0.5)["in_transit"][1][:2] == ("create", "dock")


def test_an_entity_that_sets_off_again_as_it_arrives_stays_on_its_second_link():
    """The first leg's late arrival may not end the second leg, started in the same
    instant: that leg ends when its own time has passed."""
    start, arrived = _late_arrival()
    events = start + [moved(arrived, 1, "dock", "shelf", duration=4.0)]
    timeline = Timeline(events)
    on_the_way = timeline.state_at(arrived + 1.0)
    assert on_the_way["counts"] == {} and on_the_way["in_transit"][1][:2] == ("dock", "shelf")
    assert on_the_way["in_transit"][1][2] == pytest.approx(0.25)
    assert timeline.state_at(arrived + 4.0)["counts"] == {"shelf": 1}
    assert Timeline(events).state_at(arrived + 4.0)["in_transit"] == {}


def test_an_entity_disposed_as_it_arrives_is_gone():
    start, arrived = _late_arrival()
    events = start + [disposed(arrived, 1, "dock")]
    state = Timeline(events).state_at(2.0)
    assert state["counts"] == {} and state["entities"] == {} and state["in_transit"] == {}


# -- #337: a live timeline answers each chunk's moves once ------------------------------
def test_a_live_timeline_has_no_moves_after_a_chunk_with_nothing_to_show():
    live = Timeline([], live=True)
    live.append([created(1.0, 1, "create_1"), moved(1.0, 1, "create_1", "delay_1")])
    assert [move[2:4] for move in live.moves_between(0.0, 1.0)] == [("create_1", "delay_1")]
    live.append([])                                 # the clock moved on, nothing happened
    assert live.moves_between(1.0, 2.0) == []
    live.append([moved(3.0, 1, "delay_1", "dispose_1")])
    assert [move[2:4] for move in live.moves_between(2.0, 3.0)] == [("delay_1", "dispose_1")]
    live.append([resource_state(4.0, "Server", "capacity", 1)])     # a chunk with no move
    assert live.moves_between(3.0, 4.0) == []


def test_a_live_timeline_has_no_moves_after_a_chunk_from_another_level():
    """With a submodel open, a chunk from elsewhere in the model shows nothing here."""
    live = Timeline([], view="cell_1", live=True)
    live.append([created(1.0, 1, "cell_1/in"), moved(1.0, 1, "cell_1/in", "cell_1/drill")])
    assert [move[2:4] for move in live.moves_between(0.0, 1.0)] == [("in", "drill")]
    live.append([moved(2.0, 2, "cell_2/in", "cell_2/drill")])
    assert live.moves_between(1.0, 2.0) == []
