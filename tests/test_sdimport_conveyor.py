"""PHASE6 depth bucket C2: XMILE conveyor import (`nagasim/sdimport.py`).

Each fixture (`tests/data/conveyor_{a,b,c}.xmile`) is one of the research
note's worked examples (docs/history/STELLA-CONVEYOR-SEMANTICS-2026-09-27.md
section 4); the expected numbers are copied from its tables, not from the
engine. B uses the `<leak>expr</leak>` form, C uses real Stella's form (the
fraction in the leak flow's own `<eqn>`, `<leak/>` bare).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nagasim import sdimport
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

DATA = Path(__file__).parent / "data"


def _prepare(layout):
    env = initialize_sim_env(seed=0)
    prepare_model(env, layout)
    env.ctx.level_system.start(layout["simulation"]["time_limit"])
    return env


def _sample(env, t):
    step_until(env, t or 1e-12)
    env.ctx.level_system.sync(t)


# ------------------------------------------------------------- A: no leak
def test_conveyor_a_no_leak_and_rk4_downgraded_to_euler():
    layout, problems = sdimport.load_xmile(DATA / "conveyor_a.xmile")
    assert not any(p.severity == "error" for p in problems), problems
    assert any(p.code == "W_SDIMPORT_APPROX" and "RK4" in p.message for p in problems)
    assert layout["simulation"]["sd"]["method"] == "euler"

    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [(0.0, 0.0, 0.0), (0.5, 50.0, 0.0), (1.0, 100.0, 0.0),
            (1.5, 150.0, 0.0), (2.0, 200.0, 100.0), (2.5, 200.0, 100.0)]
    for t, stock, outflow in rows:
        _sample(env, t)
        assert system.levels["Training"].level == pytest.approx(stock), t
        assert system.flow_index["graduate"].actual == pytest.approx(outflow), t


# ------------------------------------------------- B: linear leak, <leak>expr</leak>
def test_conveyor_b_linear_leak_expr_form():
    layout, problems = sdimport.load_xmile(DATA / "conveyor_b.xmile")
    assert not problems, problems
    assert layout["flows"][2]["leak"]["fraction"] == "0.5"

    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [(0.0, 0.0, 0.0, 0.0), (0.5, 50.0, 0.0, 12.5), (1.0, 93.75, 0.0, 25.0),
            (1.5, 131.25, 0.0, 37.5), (2.0, 162.5, 50.0, 50.0), (2.5, 162.5, 50.0, 50.0)]
    for t, stock, outflow, leak in rows:
        _sample(env, t)
        assert system.levels["Training"].level == pytest.approx(stock), t
        assert system.flow_index["graduate"].actual == pytest.approx(outflow), t
        assert system.flow_index["dropout"].actual == pytest.approx(leak), t


# --------------------------------------------- C: exponential leak, Stella's <eqn> form
def test_conveyor_c_exponential_leak_stella_eqn_form():
    layout, problems = sdimport.load_xmile(DATA / "conveyor_c.xmile")
    assert not problems, problems
    assert layout["levels"][0]["conveyor"]["leak_kind"] == "exponential"
    assert layout["flows"][2]["leak"]["fraction"] == "0.2"     # from <eqn>, not <leak>

    env = _prepare(layout)
    system = env.ctx.level_system
    rows = [(0.0, 0.0, 0.0, 0.0), (0.5, 50.0, 0.0, 10.0), (1.0, 95.0, 81.0, 19.0),
            (1.5, 95.0, 81.0, 19.0)]
    for t, stock, outflow, leak in rows:
        _sample(env, t)
        assert system.levels["Training"].level == pytest.approx(stock), t
        assert system.flow_index["graduate"].actual == pytest.approx(outflow), t
        assert system.flow_index["spoilage"].actual == pytest.approx(leak), t


# ------------------------------------------------------- refusals (C2 scope gaps)
def _refused(variables_xml: str, conveyor_attrs: str = "", conveyor_body: str = "<len>2</len>"):
    doc = (f'<xmile version="1.0" xmlns="http://docs.oasis-open.org/xmile/ns/XMILE/v1.0">'
          f"<sim_specs><start>0</start><stop>1</stop><dt>0.5</dt></sim_specs>"
          f"<model><variables>{variables_xml}</variables></model></xmile>")
    layout, problems = sdimport.import_xmile(doc)
    return layout, problems


def test_queue_stock_refused():
    _, problems = _refused('<stock name="Q"><eqn>0</eqn><queue/></stock>')
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and "queue" in p.message for p in problems)


def test_oven_stock_refused():
    _, problems = _refused('<stock name="O"><eqn>0</eqn><oven/></stock>')
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and "oven" in p.message for p in problems)


# One belt-shaped stock per case (name/attrs/eqn vary), each refused for a
# different reason, each dropping the level from the layout -- same shape,
# so tabled instead of copy-pasted (merged in the suite reduction pass).
_BELT_REFUSALS = [
    ("discrete", '<flow name="g"><eqn>0</eqn></flow>'
     '<stock name="Belt"><eqn>0</eqn><outflow>g</outflow>'
     '<conveyor discrete="true"><len>2</len></conveyor></stock>'),
    ("sample", '<flow name="g"><eqn>0</eqn></flow>'
     '<stock name="Belt"><eqn>0</eqn><outflow>g</outflow>'
     '<conveyor><len>2</len><sample>1</sample></conveyor></stock>'),
    ("spreadflow", '<flow name="g"><eqn>0</eqn></flow>'
     '<stock name="Belt"><eqn>0</eqn><outflow>g</outflow>'
     '<conveyor isee:spreadflow="even"><len>2</len></conveyor></stock>'),
    ("list initial", '<flow name="g"><eqn>0</eqn></flow>'
     '<stock name="Belt"><eqn>1, 2, 3</eqn><outflow>g</outflow>'
     '<conveyor><len>2</len></conveyor></stock>'),
]


@pytest.mark.parametrize("message, variables_xml", _BELT_REFUSALS)
def test_belt_stock_refused(message, variables_xml):
    layout, problems = _refused(variables_xml)
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and message in p.message for p in problems)
    assert not layout["levels"]


def test_leak_integers_refused():
    layout, problems = _refused(
        '<flow name="g"><eqn>0</eqn></flow>'
        '<flow name="leak"><eqn>0.1</eqn><leak/><leak_integers/></flow>'
        '<stock name="Belt"><eqn>0</eqn><outflow>g</outflow><outflow>leak</outflow>'
        '<conveyor><len>2</len></conveyor></stock>')
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and "leak_integers" in p.message
              for p in problems)
    assert not any(f["name"] == "leak" for f in layout["flows"])


def test_subscripted_conveyor_refused():
    doc = ('<xmile version="1.0" xmlns="http://docs.oasis-open.org/xmile/ns/XMILE/v1.0">'
          "<sim_specs><start>0</start><stop>1</stop><dt>0.5</dt></sim_specs>"
          '<dimensions><dim name="Region"><elem name="north"/><elem name="south"/></dim>'
          "</dimensions>"
          '<model><variables>'
          '<flow name="g"><eqn>0</eqn></flow>'
          '<stock name="Belt"><eqn>0</eqn><outflow>g</outflow>'
          '<dimensions><dim name="Region"/></dimensions>'
          "<conveyor><len>2</len></conveyor></stock>"
          "</variables></model></xmile>")
    layout, problems = sdimport.import_xmile(doc)
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and "subscripted" in p.message
              for p in problems)
    assert not layout["levels"]
