"""PHASE5 SD-5: `nagasim.sdimport` (docs/PHASE5_PLAN.md "Import" paragraph,
ARCHITECTURE 5.24). Covers: the hand-written fixture imports equal to its
JSON twin, the expression translator's precedence and function coverage, every
refused-construct code, the DOCTYPE/ENTITY and size-cap security refusals, and
dimensions -> subscripts / graphical function -> table mapping.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nagasim import sdimport
from nagasim.api import run_model

DATA = Path(__file__).parent / "data"


def _doc(variables_xml: str, sim_specs: str = "", dimensions: str = "") -> str:
    specs = sim_specs or "<sim_specs><start>0</start><stop>1</stop><dt>1</dt></sim_specs>"
    return (f'<xmile version="1.0" xmlns="http://docs.oasis-open.org/xmile/ns/XMILE/v1.0">'
            f"{specs}{dimensions}<model><variables>{variables_xml}</variables></model></xmile>")


def _aux_expr(eqn: str) -> str:
    """Import a single auxiliary "x" with *eqn* and return its translated expr."""
    layout, problems = sdimport.import_xmile(_doc(f'<aux name="x"><eqn>{eqn}</eqn></aux>'))
    assert not problems, problems
    return layout["auxiliaries"][0]["expr"]


# --------------------------------------------------------------- bit-identical fixture
def test_fixture_is_its_json_twin_and_matches_the_closed_form_logistic():
    import math
    imported, problems = sdimport.load_xmile(DATA / "sd_logistic.xmile")
    twin = json.loads((DATA / "sd_logistic.json").read_text())
    assert not problems
    assert imported == twin              # equal layouts, so equal runs: no twin run needed
    results = run_model(imported, replications=1, workers=1)
    final = float(results["level_summary"].loc[
        results["level_summary"]["Level"] == "Population", "Final"].iloc[0])
    closed = 1000 / (1 + 99 * math.exp(-0.2 * 50))
    assert final == pytest.approx(closed, rel=1e-6)


# --------------------------------------------------------------- translator precedence/functions
@pytest.mark.parametrize("eqn, expected", [
    ("2+3*4", "(2.0 + (3.0 * 4.0))"),
    ("(2+3)*4", "((2.0 + 3.0) * 4.0)"),
    ("2^3^2", "(2.0 ** (3.0 ** 2.0))"),              # right-associative
    ("-2^2", "(-(2.0 ** 2.0))"),                     # unary binds looser than ^
    ("1=1", "(1.0 == 1.0)"),
    ("1&lt;&gt;2", "(1.0 != 2.0)"),
    ("IF 1>0 THEN 2 ELSE 3", "IF((1.0 > 0.0), 2.0, 3.0)"),
    ("1>0 AND 2>1", "((1.0 > 0.0) and (2.0 > 1.0))"),
    ("1>0 OR 2>1", "((1.0 > 0.0) or (2.0 > 1.0))"),
    ("NOT 1>0", "(not (1.0 > 0.0))"),
    ("LN(2)", "log(2.0)"),
    ("ABS(-2)", "abs((-2.0))"),
    ("EXP(1)", "exp(1.0)"),
    ("SQRT(4)", "sqrt(4.0)"),
    ("INT(3.7)", "int(3.7)"),
    ("MIN(1,2)", "min(1.0, 2.0)"),
    ("MAX(1,2)", "max(1.0, 2.0)"),
    ("STEP(1,2)", "STEP(1.0, 2.0)"),
    ("RAMP(1,2,3)", "RAMP(1.0, 2.0, 3.0)"),
    ("PULSE(1,2,3)", "PULSE(1.0, 2.0, 3.0)"),
    ("TIME", "TNOW"),
    ("SMTH1(1,2)", "SMOOTH(1.0, 2.0)"),
    ("SMTH3(1,2)", "SMOOTH3(1.0, 2.0)"),
    ("DELAY1(1,2)", "DELAY1(1.0, 2.0)"),
    ("DELAY3(1,2)", "DELAY3(1.0, 2.0)"),
    ("DELAYN(1,2,3)", "DELAYN(1.0, 2.0, 3.0)"),
    # PHASE6 S2: INIT/SAFEDIV renamed, TREND/FORECAST/NPV passed through.
    ("INIT(1)", "INITIAL(1.0)"),
    ("SAFEDIV(1,2)", "XIDZ(1.0, 2.0)"),
    ("SAFEDIV(1,2,3)", "XIDZ(1.0, 2.0, 3.0)"),
    ("TREND(1,2,3)", "TREND(1.0, 2.0, 3.0)"),
    ("FORECAST(1,2,3)", "FORECAST(1.0, 2.0, 3.0)"),
    ("NPV(1,2,3,4)", "NPV(1.0, 2.0, 3.0, 4.0)"),
    # PHASE6 S11 (SDXorg corpus): SIN/COS/TAN pass through unchanged.
    ("SIN(1)", "SIN(1.0)"),
    ("COS(1)", "COS(1.0)"),
    ("TAN(1)", "TAN(1.0)"),
])
def test_translator_precedence_and_functions(eqn, expected):
    assert _aux_expr(eqn) == expected


def test_bare_delay_is_a_pipeline_whose_default_initial_is_the_input():
    layout, problems = sdimport.import_xmile(
        _doc('<aux name="x"><eqn>DELAY(1,2)</eqn></aux><aux name="y"><eqn>DELAY(1,2,3)</eqn></aux>'))
    assert [a["expr"] for a in layout["auxiliaries"]] == [
        "DELAYP(1.0, 2.0, INITIAL(1.0))", "DELAYP(1.0, 2.0, 3.0)"]
    assert not problems


def test_names_resolve_to_level_flow_aux_by_declared_kind():
    xml = _doc('<stock name="S"><eqn>1</eqn><inflow>F</inflow></stock>'
               '<flow name="F"><eqn>A</eqn></flow>'
               '<aux name="A"><eqn>S</eqn></aux>')
    layout, problems = sdimport.import_xmile(xml)
    assert not problems
    assert layout["flows"][0]["rate"] == 'AUX("A")'
    assert layout["auxiliaries"][0]["expr"] == 'LEVEL("S")'


# --------------------------------------------------------------- modules (PHASE6 S10)
def _module_doc(root_vars: str, *models: str) -> str:
    body = "".join(f'<model name="{n}"><variables>{v}</variables></model>' for n, v in models)
    return (f'<xmile version="1.0"><sim_specs><start>0</start><stop>1</stop><dt>1</dt></sim_specs>'
            f"<model><variables>{root_vars}</variables></model>{body}</xmile>")


@pytest.fixture(scope="module")
def modules():
    return sdimport.load_xmile(DATA / "sd_modules.xmile")


def test_modules_fixture_imports_clean_and_runs_bit_identical_to_the_twin(modules):
    imported, problems = modules
    assert not problems
    twin = json.loads((DATA / "sd_modules.json").read_text())
    r1 = run_model(imported, replications=1, workers=1)
    r2 = run_model(twin, replications=1, workers=1)
    # The two layouts name their per-instance records differently ("mod1/
    # mod1::level" vs "mod1/Mod1 Level"); both sort the same way because the
    # instance names ("mod1"/"mod2") are the ones that matter and match.
    l1 = r1["level_summary"].sort_values("Level").reset_index(drop=True)
    l2 = r2["level_summary"].sort_values("Level").reset_index(drop=True)
    assert list(l1["Final"]) == list(l2["Final"])
    t1 = r1["timeseries"]
    t2 = r2["timeseries"]
    total1 = t1.loc[t1["Series"] == "aux/Total", "Value"].reset_index(drop=True)
    total2 = t2.loc[t2["Series"] == "aux/Total", "Value"].reset_index(drop=True)
    assert (total1 == total2).all() and len(total1) > 0


def test_module_connections_and_outputs_map_to_arguments_and_dotted_names(modules):
    imported, _ = modules
    by_name = {b["name"]: b for b in imported["blocks"]}
    assert by_name["mod1"]["params"] == {"definition": "mod1",
                                         "arguments": {"input": 'AUX("OuterInput")'}}
    assert by_name["mod2"]["params"] == {"definition": "mod2",
                                         "arguments": {"input": 'AUX("OuterInput")'}}
    total = next(a for a in imported["auxiliaries"] if a["name"] == "Total")
    assert total["expr"] == '(AUX("mod1/mod1::output") + AUX("mod2/mod2::output"))'


# --------------------------------------------------------------- refused constructs
_REGION = ('<dimensions><dim name="Region"><elem name="north"/><elem name="south"/></dim>'
           '</dimensions>')
_INPUT = '<aux name="input" access="input"><eqn>0</eqn></aux>'


@pytest.mark.parametrize("xml, needle, section, left", [
    pytest.param('<xmile version="1.0"><sim_specs><start>0</start><stop>1</stop><dt>1</dt>'
                 '</sim_specs><model><variables><aux name="x"><eqn>1</eqn></aux></variables>'
                 '</model><model name="mod1"><variables></variables></model></xmile>',
                 "module/macro", "auxiliaries", [{"name": "x", "expr": "1.0"}], id="two-models"),
    pytest.param(_doc('<module name="sub1"></module>'), "module instance", None, None,
                 id="module-instance"),
    pytest.param(_module_doc('<module name="outer"></module>',
                             ("outer", '<module name="inner"></module>'),
                             ("inner", '<aux name="x"><eqn>1</eqn></aux>')),
                 "nested modules", None, None, id="nested-modules"),
    pytest.param(_module_doc('<module name="mod1"></module>', ("mod1", _INPUT)),
                 "no <connect>", None, None, id="unconnected-input"),
    pytest.param(_module_doc('<module name="mod1"><connect to="mod1.input" from="Nowhere"/>'
                             '</module>', ("mod1", _INPUT)),
                 "names no known variable", None, None, id="connect-unknown"),
    pytest.param(_module_doc('<module name="mod1"><connect to="wrong.input" from="1"/></module>',
                             ("mod1", _INPUT)),
                 "does not name one of its own", None, None, id="connect-foreign"),
    pytest.param(_doc('<stock name="S"><eqn>1</eqn><conveyor/></stock>'), "", "levels", [],
                 id="conveyor"),
    pytest.param(_doc('<stock name="S"><eqn>1</eqn><queue/></stock>'), "", "levels", [],
                 id="queue"),
    pytest.param(_doc('<stock name="S"><eqn>1</eqn><gf><ypts>0,1</ypts></gf></stock>'), "",
                 "levels", [], id="gf-on-stock"),
    pytest.param(_doc('<aux name="x"><eqn>1</eqn><dimensions><dim name="Nope"/></dimensions>'
                      '</aux>'), "", "auxiliaries", [], id="unknown-dimension"),
    pytest.param(_doc("", dimensions='<dimensions><dim name="B"><mapping>A</mapping></dim>'
                                     '</dimensions>'), "mapping", "subscripts", [], id="mapping"),
    pytest.param(_doc('<aux name="x"><eqn>FROBNICATE(1)</eqn></aux>'), "FROBNICATE",
                 "auxiliaries", [], id="unknown-function"),
    pytest.param(_doc('<aux name="x"><eqn>Nowhere</eqn></aux>'), "Nowhere", "auxiliaries", [],
                 id="unknown-name"),
    pytest.param(_doc('<stock name="S1"><eqn>1</eqn><inflow>F</inflow></stock>'
                      '<stock name="S2"><eqn>1</eqn><inflow>F</inflow></stock>'
                      '<flow name="F"><eqn>1</eqn></flow>'), "more than one stock", "flows", [],
                 id="flow-two-stocks"),
    pytest.param(_doc('<stock name="Pop"><eqn>1</eqn><dimensions><dim name="Region"/>'
                      '</dimensions></stock><aux name="total"><eqn>Pop*2</eqn></aux>',
                      dimensions=_REGION), "arrayed 'Pop'", "auxiliaries", [],
                 id="arrayed-in-scalar"),
])
def test_refused_constructs(xml, needle, section, left):
    layout, problems = sdimport.import_xmile(xml)
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and needle in p.message for p in problems)
    if section is not None:
        assert layout[section] == left


# --------------------------------------------------------------- security refusals
@pytest.mark.parametrize("decl", [
    '<!DOCTYPE xmile [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>', '<!ENTITY foo "bar">',
    "<!doctype x>", "<!DocType x>", "<! DOCTYPE x>", "<!\n\tDOCTYPE x>", '<!entity e "v">'])
def test_doctype_variants_are_refused_without_calling_the_parser(decl, monkeypatch):
    def never(*_args, **_kwargs):
        raise AssertionError("the parser must not be reached")
    monkeypatch.setattr(sdimport.ET, "fromstring", never)
    for payload in (decl + "<xmile/>", ("﻿" + decl + "<xmile/>").encode("utf-8")):
        with pytest.raises(sdimport.XMILEError) as excinfo:
            sdimport.import_xmile(payload)
        assert excinfo.value.code == "E_SDIMPORT_XML"


def test_size_cap_is_enforced(monkeypatch):
    monkeypatch.setattr(sdimport, "MAX_BYTES", 10)
    with pytest.raises(sdimport.XMILEError) as excinfo:
        sdimport.import_xmile("<xmile>" + " " * 20 + "</xmile>")
    assert excinfo.value.code == "E_SDIMPORT_XML"


def test_malformed_xml_is_refused():
    with pytest.raises(sdimport.XMILEError):
        sdimport.import_xmile("<xmile><model>")


# --------------------------------------------------------------- dimensions -> subscripts
# A named dimension -> subscript is test_regressions.py::test_127_...
def test_size_only_dimension_gets_numbered_elements():
    xml = _doc("", dimensions='<dimensions><dim name="Line" size="3"/></dimensions>')
    layout, _ = sdimport.import_xmile(xml)
    assert layout["subscripts"] == [{"name": "Line", "elements": ["1", "2", "3"]}]


# --------------------------------------------------------------- graphical function -> table
def test_graphical_function_becomes_a_table():
    xml = _doc('<aux name="foo"><eqn>TIME</eqn><gf><xpts>0,1,2</xpts>'
              "<ypts>0,2,3</ypts></gf></aux>")
    layout, problems = sdimport.import_xmile(xml)
    assert not problems
    assert layout["tables"] == [{"name": "foo_gf", "keys": [0.0, 1.0, 2.0],
                                 "values": [0.0, 2.0, 3.0], "key_mode": "linear"}]
    assert layout["auxiliaries"][0]["expr"] == 'TABLE("foo_gf", TNOW)'


def test_an_imported_graphical_function_runs():
    """A flow y = gf(TIME) with the line 0 -> 20 over 0..10 is rate 2t: Euler
    with dt 1 over 10 days sums 2k for k = 0..9 = 90."""
    xml = _doc('<flow name="f"><eqn>TIME</eqn><gf><xpts>0,10</xpts><ypts>0,20</ypts></gf>'
              '<non_negative/></flow><stock name="S"><eqn>0</eqn><inflow>f</inflow>'
              '<non_negative/></stock>',
              sim_specs='<sim_specs time_units="days"><start>0</start><stop>10</stop>'
                        '<dt>1</dt></sim_specs>')
    layout, problems = sdimport.import_xmile(xml)
    assert not problems
    frame = run_model(layout, replications=1, workers=1)["level_summary"]
    assert float(frame.loc[frame["Level"] == "S", "Final"].iloc[0]) == pytest.approx(90.0)


def test_graphical_function_with_xscale():
    xml = _doc('<aux name="foo"><eqn>TIME</eqn><gf><xscale min="0" max="10"/>'
              "<ypts>0,5,10</ypts></gf></aux>")
    layout, _ = sdimport.import_xmile(xml)
    assert layout["tables"][0]["keys"] == [0.0, 5.0, 10.0]


def test_graphical_function_without_an_equation_defaults_to_time():
    xml = _doc('<aux name="foo"><gf><xpts>0,1</xpts><ypts>0,1</ypts></gf></aux>')
    layout, problems = sdimport.import_xmile(xml)
    assert layout["auxiliaries"][0]["expr"] == 'TABLE("foo_gf", TNOW)'
    assert any(p.code == "W_SDIMPORT_APPROX" for p in problems)


# --------------------------------------------------------------- non_negative / biflow mapping
def test_non_negative_maps_to_min_and_biflow():
    unbounded, _ = sdimport.import_xmile(_doc('<stock name="S"><eqn>1</eqn><inflow>F</inflow>'
                                              '</stock><flow name="F"><eqn>1</eqn></flow>'))
    assert unbounded["levels"][0]["min"] is None
    assert unbounded["flows"][0]["biflow"] is True
    layout, _ = sdimport.import_xmile(_doc(
        '<stock name="S"><eqn>1</eqn><inflow>F</inflow><non_negative/></stock>'
        '<flow name="F"><eqn>1</eqn><non_negative/></flow>'))
    assert "min" not in layout["levels"][0]        # 0.0 is the level default, omitted
    assert "biflow" not in layout["flows"][0]       # False is the flow default, omitted
