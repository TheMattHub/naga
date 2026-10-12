"""D1 (performance report 2026-10-07): DISPOSE with a plain Constant 0 delay
finishes the entity in the caller, with no SimPy process; any other delay,
however it was set, still waits."""
import math

from nagasim.core.base_process import Block
from nagasim.core.blocks import Dispose
from nagasim.core.sim_classes import Entity


def _entity(env):
    entity = Entity()
    entity.creation_time = env.now
    env.ctx.all_entities.append(entity)
    env.ctx.live_entities.append(entity)
    return entity


def _dispose(env):
    return Dispose(env, Block(position=(0, 0), type="DISPOSE", name="out"))


def test_zero_delay_disposes_in_the_caller_and_schedules_nothing(env):
    dispose, events = _dispose(env), []
    env.ctx.bus.subscribe(lambda name, **payload: events.append(name))
    entity = _entity(env)
    assert dispose.self_run(entity) is None
    assert env.peek() == math.inf                       # no process, no timeout
    assert entity.finished and entity.deletion_time == 0 and entity.throughput_time == 0
    assert entity not in env.ctx.live_entities and dispose.disposed_entities == 1
    assert events == ["entity_disposed"]


def test_a_delay_set_after_construction_still_waits(env):
    dispose = _dispose(env)
    dispose.set_params_from_dict({"distribution": "Constant", "dist_params": {"value": 2}})
    entity = _entity(env)
    dispose.self_run(entity)
    assert entity in env.ctx.live_entities
    env.run()
    assert entity.deletion_time == 2 and entity not in env.ctx.live_entities


def test_an_expression_that_is_zero_is_not_a_plain_zero(env):
    dispose = _dispose(env)
    dispose.set_params_from_dict({"distribution": "Constant", "dist_params": {"value": "0"}})
    entity = _entity(env)
    assert dispose.self_run(entity) is not None         # a process, as before
    assert entity in env.ctx.live_entities
    env.run()
    assert entity.deletion_time == 0 and entity not in env.ctx.live_entities
