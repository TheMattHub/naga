"""Section 7 item 5 (performance report 2026-10-07): the set and store logs are
appended to directly, a set's open waits are found by bisection and the
`active_entity` setter reads its fields once. Behaviour is what it was."""
from collections import deque

import pytest

from nagasim.core.base_process import Block
from nagasim.core.blocks import Delay, Seize
from nagasim.core.sim_classes import Entity
from nagasim.core.utils import ResourceSnapshot, StoreSnapshot


def _team(env):
    manager = env.ctx.resource_manager
    manager.register("A")
    manager.register("B")
    return manager.register_set("Team", ["A", "B"])


def test_set_log_record_cap_and_truncation_flag(env):
    team = _team(env)
    team.log = deque(maxlen=3)
    for _ in range(3):
        team.log_usage(env.now, event_type="release")
    assert len(team.log) == 3 and not team.log_truncated      # full, nothing dropped yet
    team.enqueue()
    assert len(team.log) == 3 and team.log_truncated
    assert team.log[-1] == ResourceSnapshot(time=0, in_use=0, queue_length=1, event_type="request")
    assert team.log_events == 4


def test_set_log_is_not_built_with_record_logs_off(env):
    env.ctx.record_logs = False
    team = _team(env)
    team.enqueue()
    assert not team.log and team.log_events == 1


def test_set_open_waits_are_removed_by_value_in_order(env):
    team = _team(env)
    team.enqueue()
    team.enqueue()
    env.run(until=1)
    team.enqueue()
    env.run(until=2)
    team.enqueue()
    assert team._open == [0, 0, 1, 2]
    team.left(1)
    assert team._open == [0, 0, 2]
    team.granted(0, 1)
    assert team._open == [0, 2]
    team.left(1.5)                      # not waiting: nothing to remove
    assert team._open == [0, 2]
    assert team.open_waits(3.0) == [3.0, 1.0]


def test_store_log_record_cap_and_truncation_flag(env):
    store = env.ctx.store_manager.register("s")
    store.log = deque(maxlen=2)
    for _ in range(2):
        store.put(Entity())
    env.run()
    assert len(store.log) == 2 and not store.log_truncated
    store.put(Entity())
    env.run()
    assert len(store.log) == 2 and store.log_truncated
    assert store.log[-1] == StoreSnapshot(time=0, items=3, event_type="put")
    assert store.log_events == 3


def test_active_entity_setter_appends_and_checks_the_resource_capacity(env):
    delay = Delay(env, Block(position=(0, 0), type="DELAY", name="d"))
    first, second = Entity(), Entity()
    delay.active_entity = first
    delay.active_entity = second                      # no resource: no limit
    assert delay.active_entity == [first, second]

    server = env.ctx.resource_manager.register("Server", capacity=1)
    seize = Seize(env, Block(position=(1, 0), type="SEIZE", name="s"), resource=server)
    seize.active_entity = first
    with pytest.raises(ValueError, match="Capacity of .* exceeded!"):
        seize.active_entity = second
    assert seize.active_entity == [first]
