"""E1 (performance report 2026-10-07): ASSIGN, DECIDE and RECORD compile once per
block, and a changed assignments/conditions/expr list takes effect at once."""
import pytest

from nagasim.core.base_process import Block
from nagasim.core.blocks import Assign, Decide, Dispose, Record
from nagasim.core.expressions import ExpressionError
from nagasim.core.sim_classes import Entity


def _entity(env, **attrs):
    entity = Entity()
    entity.creation_time = 0
    for k, v in attrs.items():
        setattr(entity, k, v)
    env.ctx.all_entities.append(entity)
    env.ctx.live_entities.append(entity)
    return entity


def _assign(env, assignments):
    assign = Assign(env, Block(position=(0, 0), type="ASSIGN", name="a1"), assignments=assignments)
    assign.next_process = [Dispose(env, Block(position=(1, 0), type="DISPOSE", name="d1"))]
    return assign


@pytest.mark.parametrize("assignment, message", [
    ({"target": "entity.", "expr": "1"}, "Assign block 'a1': entity. = 1: "),
    ({"target": "entity.x", "expr": "1 +"}, "Assign block 'a1': entity.x = 1 +: "),
    ({"target": "self.x", "expr": "1"}, "Assign block 'a1': self.x = 1: self.<attribute> is only valid"),
])
def test_bad_assignment_raises_at_run_time_not_construction(env, assignment, message):
    assign = _assign(env, [{"target": "entity.ok", "expr": "1"}, assignment])   # built without error
    entity = _entity(env)
    with pytest.raises(ExpressionError) as err:
        assign.self_run(entity)
    assert str(err.value).startswith(message)
    assert entity.ok == 1          # the earlier assignment ran before the failure, as before


def test_replacing_or_editing_assignments_takes_effect(env):
    assign = _assign(env, [{"target": "entity.v", "expr": "1"}])
    first = _entity(env)
    assign.self_run(first)
    assert first.v == 1

    assign.assignments = [{"target": "entity.v", "expr": "2"}]              # replaced
    second = _entity(env)
    assign.self_run(second)
    assert second.v == 2

    assign.assignments[0]["expr"] = "3"                                     # edited in place
    third = _entity(env)
    assign.self_run(third)
    assert third.v == 3

    assign.assignments.append({"target": "entity.w", "expr": "entity.v + 1"})   # appended
    fourth = _entity(env)
    assign.self_run(fourth)
    assert (fourth.v, fourth.w) == (3, 4)

    assign.set_params_from_dict({"assignments": [{"target": "entity.v", "expr": "9"}]})
    fifth = _entity(env)
    assign.self_run(fifth)
    assert fifth.v == 9


def test_decide_conditions_replaced_after_construction_take_effect(env):
    decide = Decide(env, Block(position=(0, 0), type="DECIDE", name="dec"),
                    mode="Condition", conditions=["entity.k == 1"])
    hit = Dispose(env, Block(position=(1, 0), type="DISPOSE", name="hit"))
    other = Dispose(env, Block(position=(2, 0), type="DISPOSE", name="other"))
    decide.next_process = [hit, other]

    decide.self_run(_entity(env, k=2))
    env.run(until=1)
    assert (hit.disposed_entities, other.disposed_entities) == (0, 1)

    decide.conditions = ["entity.k == 2"]
    decide.self_run(_entity(env, k=2))
    env.run(until=2)
    assert (hit.disposed_entities, other.disposed_entities) == (1, 1)

    decide.conditions[0] = "entity.k == 3"                                  # edited in place
    decide.self_run(_entity(env, k=2))
    env.run(until=3)
    assert (hit.disposed_entities, other.disposed_entities) == (1, 2)


def test_decide_bad_condition_raises_at_run_time(env):
    decide = Decide(env, Block(position=(0, 0), type="DECIDE", name="dec"),
                    mode="Condition", conditions=["entity.k +"])            # built without error
    decide.next_process = [Dispose(env, Block(position=(1, 0), type="DISPOSE", name="d"))]
    with pytest.raises(ExpressionError):
        decide.self_run(_entity(env, k=1))


def test_record_expr_edit_takes_effect(env):
    record = Record(env, Block(position=(0, 0), type="RECORD", name="r"), stat_name="s",
                    kind="count", expr="1")
    record.next_process = [Dispose(env, Block(position=(1, 0), type="DISPOSE", name="d"))]
    record.self_run(_entity(env))
    record.expr = "10"
    record.self_run(_entity(env))
    assert record._stat.collector.total == 11
    record.expr = "1 +"
    with pytest.raises(ExpressionError):
        record.self_run(_entity(env))
