"""Speed review round 2 (2026-11-24): the engine reads SimPy internals that are not its public
API (R1: `Environment._queue` and `_now` in the step loop; R2: `_capacity`, `_env`, an event's
`_value` against `PENDING`, `get_queue`; R3: `_now` at the per-visit sites). This fails first,
and plainly, if a SimPy release renames one of them."""
import simpy
from simpy.events import PENDING

from nagasim.core.resource_manager import _Resource


def test_the_simpy_internals_the_engine_reads_are_there():
    env = simpy.Environment(initial_time=2.5)
    assert env._now == env.now == 2.5 and env._queue == []
    resource = _Resource(env, 2)
    assert resource._capacity == resource.capacity == 2 and resource._env is env
    request = resource.request()
    assert request._value is not PENDING and request.triggered    # granted at once
    release = resource.release(request)
    assert release._value is not PENDING and not resource.get_queue and not resource.users
    timeout = env.timeout(1.0)
    assert timeout._value is not PENDING and env._queue[0][0] == 2.5
    event = env.event()
    assert event._value is PENDING and not event.triggered
    # The step loop's copy of `Environment.step` pops (time, priority, id, event) entries
    assert all(len(entry) == 4 for entry in env._queue)


# The SimPy functions the engine copies, and where: the source fingerprint of each in SimPy
# 4.1.2, the version the copies follow (D1398). This is how the engine stays current: a SimPy
# release that changes one fails here, and the upgrade re-reads the copy, updates the copy and
# the fingerprint, and checks the digest 63/63 (docs/RECIPES.md, "Upgrade SimPy").
_COPIES = {
    "Environment.step": ("nagasim/core/sim_runner.py (_step_until)",
                         {"6ac0d04dcd2c": "4.1.2"}),
    "Environment.peek": ("nagasim/core/sim_runner.py (_step_until)",
                         {"5bbb676a47b5": "4.1.2"}),
    "Resource._do_put": ("nagasim/core/resource_manager.py (_Resource._do_put)",
                         {"d2c3f6f9ec23": "4.1.2"}),
    "Resource._do_get": ("nagasim/core/resource_manager.py (_Resource._trigger_get)",
                         {"276135330ef1": "4.1.2"}),
    "BaseResource._trigger_put": ("nagasim/core/resource_manager.py (_Resource._trigger_put)",
                                  {"c6072a662af8": "4.1.2"}),
    "BaseResource._trigger_get": ("nagasim/core/resource_manager.py (_Resource._trigger_get)",
                                  {"b3f1c13d6739": "4.1.2"}),
}


def _fingerprint(fn) -> str:
    """The function's syntax tree without its docstring: comments and layout are not behaviour."""
    import ast
    import hashlib
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and ast.get_docstring(node):
            node.body = node.body[1:]
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()[:12]


def test_every_copied_simpy_function_is_one_the_copy_was_reviewed_against():
    from simpy.resources import base, resource
    functions = {"Environment.step": simpy.Environment.step, "Environment.peek": simpy.Environment.peek,
                 "Resource._do_put": resource.Resource._do_put,
                 "Resource._do_get": resource.Resource._do_get,
                 "BaseResource._trigger_put": base.BaseResource._trigger_put,
                 "BaseResource._trigger_get": base.BaseResource._trigger_get}
    for name, fn in functions.items():
        where, reviewed = _COPIES[name]
        assert _fingerprint(fn) in reviewed, (
            f"SimPy {simpy.__version__} changed {name}: re-read the copy in {where} against "
            f"SimPy {simpy.__version__}, then update the copy and this fingerprint")


def test_the_step_loop_handles_a_stop_simulation_as_simpy_does():
    """R1's copy against SimPy's own `step`: the callbacks behind a `StopSimulation`, which
    4.1.2 keeps and reschedules, and the queue afterwards."""
    import pytest
    from simpy.core import StopSimulation
    from nagasim.core.sim_runner import _step_until

    class _Ctx:                         # what `_step_until` reads of a context
        sampler = None
        route_holds = None
        blocking_nodes = ()

    def state(run):
        env = simpy.Environment()
        env.ctx = _Ctx()
        seen = []
        event = env.event()
        event.callbacks += [StopSimulation.callback, lambda e: seen.append("after")]
        event.succeed("v")
        with pytest.raises(StopSimulation):
            run(env)
        left = [len(e[3].callbacks or ()) for e in env._queue]
        while env._queue:
            env.step()
        return seen, left

    assert state(lambda env: _step_until(env, 10)) == state(lambda env: env.step())
