"""Section 7 item 1, "cheaper" (performance report 2026-10-07): what cannot
change during a run is worked out once -- a PROCESS's `seizes`/`releases`, a
resource's busy-cost conversion, an entity type's holding-cost rate."""
import pytest

from nagasim.core.base_process import Block
from nagasim.core.blocks import Dispose, Process
from nagasim.core.sim_classes import Entity


def _entity(env, e_type="DEFAULT"):
    entity = Entity()
    entity.e_type = e_type
    entity.creation_time = 0
    env.ctx.all_entities.append(entity)
    env.ctx.live_entities.append(entity)
    return entity


def _dispose(env):
    return Dispose(env, Block(position=(1, 0), type="DISPOSE", name="out"))


def test_process_seizes_and_releases_follow_the_action(env):
    proc = Process(env, Block(position=(0, 0), type="PROCESS", name="p"))
    assert (proc.action, proc.seizes, proc.releases) == ("Seize Delay Release", True, True)
    for action, flags in (("Seize Delay", (True, False)), ("Delay Release", (False, True)),
                          ("Delay", (False, False))):
        proc.action = action                            # set after construction
        assert (proc.seizes, proc.releases) == flags
        assert (proc.space_passthrough, proc.can_block) == flags
    proc.set_params_from_dict({"action": "Seize Delay", "resource_name": "Server"})
    assert (proc.seizes, proc.releases) == (True, False)
    assert proc.get_params_dict()["action"] == "Seize Delay"


def test_busy_cost_conversion_is_worked_out_at_the_first_charge(env):
    server = env.ctx.resource_manager.register("Server", cost_busy=60.0, cost_time_unit="hours")
    proc = Process(env, Block(position=(0, 0), type="PROCESS", name="p"),
                   resource_name="Server", delay=30)
    proc.next_process = [_dispose(env)]
    assert server.busy_cost_conv is None
    first, second = _entity(env), _entity(env)
    proc.self_run(first)
    proc.self_run(second)
    env.run()
    assert server.busy_cost_conv == 60.0                # an hour, in the model's minutes
    assert (first.cost, second.cost) == (pytest.approx(30.0), pytest.approx(30.0))


def test_holding_cost_rate_is_kept_per_entity_type(env):
    types = env.ctx.entity_manager
    types.register("A", holding_cost=2.0)
    types.register("B", holding_cost=0.5)
    dispose = _dispose(env)
    entities = [_entity(env, e_type) for e_type in ("A", "B", "UNDECLARED", "A")]
    env.run(until=4)
    for entity in entities:
        dispose.self_run(entity)
    env.run()
    assert [entity.cost for entity in entities] == [8.0, 2.0, 0.0, 8.0]
