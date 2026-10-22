"""PHASE6 A7 (D105): Python agent behaviour. The gate is the caller's
(NAGASIM_ALLOW_AGENT_PYTHON=1, never the model file); the load-time checks are
defence in depth. Each security PoC below states the outcome it pins."""
import os

import pytest

from nagasim import experiments
from nagasim.core import agents as A
from nagasim.core.expressions import ExpressionError
from nagasim.core.schema import ModelValidationError, validate_model_structured
from test_agents_tick import _model, _run

WALK = """
def step(agent, api):
    agent.set("x", agent.get("x") + api.normal(0, 1))
    agent.set("v", api.agent("Ring", (agent.id + 1) % api.params["n"]).get("v"))
"""


@pytest.fixture
def consent(monkeypatch):
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")


def _ring(python, activation="synchronous", n=6, time_limit=3.5):
    if python:
        extra = {"on_tick": [A.Action.python("step")], "python": WALK}
    else:
        extra = {"on_tick": [A.Action.set("self.x", "self.x + NORM(0, 1)"),
                             A.Action.set("self.v", 'AGENT("Ring", (self.id + 1) % n).v')]}
    kind = A.AgentType("W", parameters={"n": n}, states=["S"], tick=A.Tick(1, activation),
                       attributes=["x", A.Attribute("v", "self.id")], **extra)
    return _model(kind, A.Population("Ring", "W", n), time_limit=time_limit)


def _one(code, action="f", time_limit=1.5, **kind):
    agent = A.AgentType("W", attributes=["x"], states=["S"], tick=A.Tick(1),
                        on_tick=[A.Action.python(action)], python=code, **kind)
    return _model(agent, A.Population("P", "W", 2), time_limit=time_limit)


def _errors(layout):
    return [p for p in validate_model_structured(layout) if p.severity == "error"]


@pytest.mark.parametrize("activation", ["synchronous", "random"])
def test_python_on_tick_equals_declarative_bit_for_bit(consent, activation):
    """A random walk on the agent's own stream plus a ring copy (the
    synchronous tick's snapshot rule) give the same columns either way."""
    assert not _errors(_ring(True, activation))
    py = _run(_ring(True, activation)).ctx.agent_system.by_name["Ring"].attrs
    decl = _run(_ring(False, activation)).ctx.agent_system.by_name["Ring"].attrs
    assert py["x"].tolist() == decl["x"].tolist() and any(py["x"])
    assert py["v"].tolist() == decl["v"].tolist()
    if activation == "synchronous":                 # a rotation by 3 ticks
        assert py["v"].tolist() == [3, 4, 5, 0, 1, 2]


@pytest.mark.slow
def test_a_surface_that_dropped_consent_is_refused_in_every_worker(monkeypatch):
    """D186: the variable is dropped in the parent, so pool workers inherit a clean
    environment and the engine's own gate refuses there too."""
    from nagasim import agent_tools, api
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    agent_tools.drop_python_consent()
    for workers in (1, 2):
        with pytest.raises(A.PythonRefused):
            api.run_model(_ring(python=True), replications=2, workers=workers)


def test_consent_comes_back_after_a_scoped_drop_even_on_an_error(monkeypatch):
    from nagasim import agent_tools
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    with pytest.raises(RuntimeError):
        with agent_tools.python_consent_dropped():
            assert A.PYTHON_CONSENT not in os.environ
            raise RuntimeError("tool failed")
    assert os.environ[A.PYTHON_CONSENT] == "1"


def test_a_scoped_drop_never_invents_consent(monkeypatch):
    from nagasim import agent_tools
    monkeypatch.delenv(A.PYTHON_CONSENT, raising=False)
    with agent_tools.python_consent_dropped():
        pass
    assert A.PYTHON_CONSENT not in os.environ


def test_nested_scoped_drops_restore_only_when_the_outer_one_exits(monkeypatch):
    from nagasim import agent_tools
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    with agent_tools.python_consent_dropped():
        with agent_tools.python_consent_dropped():
            assert A.PYTHON_CONSENT not in os.environ
        assert A.PYTHON_CONSENT not in os.environ       # the inner exit must not restore it
    assert os.environ[A.PYTHON_CONSENT] == "1"


def test_overlapping_scoped_drops_on_two_threads_stay_closed_until_the_last_exits(monkeypatch):
    """A enters, B enters, A exits: B is still inside, so the variable must still be absent."""
    import threading
    from nagasim import agent_tools
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    a_in, b_in, a_out = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def thread_a():
        with agent_tools.python_consent_dropped():
            a_in.set()
            b_in.wait(10)
        a_out.set()

    def thread_b():
        a_in.wait(10)
        with agent_tools.python_consent_dropped():
            b_in.set()
            a_out.wait(10)
            seen["b_after_a_left"] = A.PYTHON_CONSENT in os.environ

    threads = [threading.Thread(target=thread_a), threading.Thread(target=thread_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads)
    assert seen == {"b_after_a_left": False}
    assert os.environ[A.PYTHON_CONSENT] == "1"


def test_a_variable_written_while_a_window_is_open_does_not_survive_into_a_later_window(monkeypatch):
    """Every entry pops the variable, not only the first: something that wrote it while an
    earlier window was open (another thread, a plugin) cannot leak consent into a new window."""
    from nagasim import agent_tools
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    with agent_tools.python_consent_dropped():
        os.environ[A.PYTHON_CONSENT] = "1"              # written while the first window is open
        with agent_tools.python_consent_dropped():
            assert A.PYTHON_CONSENT not in os.environ   # the second window starts closed
        assert A.PYTHON_CONSENT not in os.environ       # and the first is still closed
    assert os.environ[A.PYTHON_CONSENT] == "1"          # the value comes back once, at the last exit


def test_a_permanent_drop_inside_a_scoped_one_is_not_undone(monkeypatch):
    from nagasim import agent_tools
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    with agent_tools.python_consent_dropped():
        agent_tools.drop_python_consent()
    assert A.PYTHON_CONSENT not in os.environ


def test_no_consent_means_no_execution(monkeypatch):
    """PoC: code that would raise the moment it loaded is never reached --
    validation only parses it, and a run is refused before anything compiles.
    Only the exact value "1" consents; the model file has no say."""
    boom = "def f(agent, api):\n    pass\n\ndef g(agent, api, x=1 // 0):\n    pass\n"
    layout = _one(boom)
    layout["agent_types"][0]["allow_python"] = True           # a file cannot grant it
    assert not [p for p in _errors(layout) if p.code == "E_AGENT_PYTHON"]
    for value in (None, "0", "true", "yes"):
        if value is None:
            monkeypatch.delenv(A.PYTHON_CONSENT, raising=False)
        else:
            monkeypatch.setenv(A.PYTHON_CONSENT, value)
        with pytest.raises(PermissionError, match=A.PYTHON_CONSENT):
            _run(_one(boom))
    monkeypatch.setenv(A.PYTHON_CONSENT, "1")
    with pytest.raises(ZeroDivisionError):                  # consented: it really loads
        _run(_one(boom))


@pytest.mark.parametrize("body", [
    "import os", "from os import environ", "x = agent.__class__", "x = api._sys",
    "x = api.send.__self__", "x = (y for y in ()).gi_frame", "x = '{0._sys}'.format(api)",
    "x = __import__('os')", "x = __builtins__", "x = (lambda: 0).__globals__",
    "x = api.params.__class__", "global y", "class X: pass", "with api: pass",
    "yield 1", "x = agent.pos.__class__.__bases__",
])
def test_escape_attempts_are_refused_at_load(consent, body):
    """PoC: imports, dunders, private slots, frames and str.format never load:
    E_AGENT_PYTHON at validation, so a run refuses the model, and the loader
    re-checks on its own (a caller that skips validation) -- consent or not."""
    code = f"def f(agent, api):\n    {body}\n"
    layout = _one(code)
    assert {p.code for p in _errors(layout)} == {"E_AGENT_PYTHON"}
    assert "line 2" in _errors(layout)[0].message
    with pytest.raises(ModelValidationError, match="line 2"):
        _run(layout)
    with pytest.raises(ExpressionError, match="line 2"):
        A._load_python("W", code)


@pytest.mark.parametrize("call", ["open('x')", "getattr(agent, 'x')", "eval('1')", "exec('1')",
                                  "type(agent)", "vars(api)", "globals()", "compile('1', '', 'eval')",
                                  "input()", "breakpoint()", "print(1)"])
def test_dangerous_builtins_do_not_exist(consent, call):
    """PoC: what the checker lets through still finds no open/getattr/eval/
    type/vars/globals -- the builtins are a 23-name allow-list."""
    with pytest.raises(ExpressionError, match=r"python f\(\) of P\[0\] at t=1, line 2: NameError"):
        _run(_one(f"def f(agent, api):\n    {call}\n"))


def test_api_returns_plain_values_only(consent):
    """The api hands out ints, floats, strings, tuples, lists of AgentView and
    a read-only mapping: no numpy object, no population, no system."""
    kind = A.AgentType("W", parameters={"k": 2}, attributes=["x"], states=["S"],
                       space=A.Space("continuous", [0, 0, 10, 10]))
    env = _run(_model(kind, A.Population("P", "W", 3), time_limit=1))
    system = env.ctx.agent_system
    pop = system.by_name["P"]
    api = A.AgentApi(system, pop, 0, pop.scope(0))
    me, other = A.AgentView(system, pop, 0, True), api.agent("P", 1)
    assert type(api.count("P")) is int and type(api.count("P", "S")) is int
    assert type(api.sum("P", "x")) is float and type(api.mean("P", "x")) is float
    assert type(api.now) is float and type(api.random()) is float
    assert type(api.integers(0, 5)) is int and api.payload is None
    assert [type(v) for v in api.near("P", 100)] == [A.AgentView, A.AgentView]
    assert [v.id for v in api.near("P", 100)] == [1, 2]              # ascending index
    assert type(me.pos) is tuple and all(type(c) is float for c in me.pos)
    assert (me.get("state"), me.state, other.id, other.type) == ("S", "S", 1, "W")
    with pytest.raises(TypeError):
        api.params["k"] = 3
    with pytest.raises(AttributeError):
        me.__dict__                                                  # noqa: B018 -- slots
    with pytest.raises(ExpressionError, match="read-only"):
        other.set("x", 1)
    with pytest.raises(ExpressionError, match="no attribute 'state'"):
        me.set("state", "S")


def test_syntax_error_and_signature_problems():
    syntax = _errors(_one("def f(agent, api):\n    x = (\n"))
    assert [p.code for p in syntax] == ["E_AGENT_PYTHON"] and "line 2" in syntax[0].message
    top = _errors(_one("x = 1\ndef f(agent, api):\n    pass\n"))
    assert [p.code for p in top] == ["E_AGENT_PYTHON"] and "line 1" in top[0].message
    arity = _errors(_one("def f(agent):\n    pass\n"))
    assert [p.code for p in arity] == ["E_AGENT_PYTHON"] and "exactly (agent, api)" in \
        arity[0].message
    missing = _errors(_one("def f(agent, api):\n    pass\n", action="g"))
    assert [p.code for p in missing] == ["E_AGENT_PYTHON"] and "no top-level def g" in \
        missing[0].message
    no_code = _one("")
    del no_code["agent_types"][0]["python"]
    assert "declares no python" in _errors(no_code)[0].message


def test_runtime_error_names_type_function_agent_time_and_line(consent):
    code = "def f(agent, api):\n    y = 1\n    return y / agent.get('x')\n"
    with pytest.raises(ExpressionError, match=r"agent type 'W': python f\(\) of P\[0\] at t=1, "
                                              r"line 3: ZeroDivisionError"):
        _run(_one(code))


def test_send_payload_spawn_remove(consent):
    """A Python send (its literal name counts for W_AGENT_MESSAGE_UNUSED), the
    payload in a message transition's Python action, spawn with init, remove."""
    code = """
def tick(agent, api):
    if agent.id == 0:
        api.send("ping", api.agent("P", 1), 7)
        born = api.spawn("P", 1, init={"x": 40})
        api.remove(born)
        api.spawn("P", init={"x": api.count("P")})

def got(agent, api):
    agent.set("x", api.payload)
"""
    kind = A.AgentType("W", attributes=["x"], states=["S"], tick=A.Tick(1),
                       on_tick=[A.Action.python("tick")], python=code,
                       transitions=[{"from": "S", "to": "S", "message": "ping",
                                     "actions": [{"python": "got"}]}])
    layout = _model(kind, A.Population("P", "W", 2, max_count=4), time_limit=1.5)
    assert not [p for p in validate_model_structured(layout)]
    pop = _run(layout).ctx.agent_system.by_name["P"]
    assert (pop.live, pop.attrs["x"][:pop.n].tolist()) == (3, [0, 7, 2])


def test_overrides_cannot_rewrite_python():
    """A scenario, data binding or agent tool edits through the override
    grammar; none may swap consented code for other code."""
    layout = _one("def f(agent, api):\n    pass\n")
    with pytest.raises(Exception, match="python code cannot be overridden"):
        experiments.apply_overrides(layout, [{"path": "agent_types.W.python",
                                              "value": "def f(agent, api):\n    pass\n"}])
    assert not [path for path, _ in experiments.list_override_paths(layout) if "python" in path]
