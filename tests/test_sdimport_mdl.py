"""PHASE6 S5: `nagasim.mdlimport` (docs/PHASE6_PLAN.md S5, ARCHITECTURE 5.24). Covers:
the hand-written fixture runs bit-identical to its JSON twin and actually runs, the
refused-construct codes (macro, GET XLS DATA, unknown function/name, name with a
space, per-element/subrange subscripts, a non-flow INTEG rate, a flow feeding two
stocks), the encoding (UTF-8 primary, cp1252 fallback) and size-cap refusals, CLI
dispatch by extension (`naga sdimport ... .mdl`), and the `import_mdl` agent
tool's workspace confinement.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nagasim import agent_tools as T
from nagasim import mdlimport, sdimport
from nagasim.api import run_model

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data"


# --------------------------------------------------------------- bit-identical fixture
def test_fixture_bit_identical_to_its_json_twin_and_runs():
    imported, problems = mdlimport.load_mdl(DATA / "sd_vensim.mdl")
    twin = json.loads((DATA / "sd_vensim.json").read_text())
    assert not problems
    assert imported == twin
    results = run_model(imported, replications=1, workers=1)
    final = results["level_summary"]
    pop = float(final.loc[final["Level"] == "Population", "Final"].iloc[0])
    assert pop > 10.0        # logistic growth: strictly increasing from its initial 10


# --------------------------------------------------------------- expression translator
@pytest.mark.parametrize("eqn,expected", [
    ("1 + 2 * 3", "(1.0 + (2.0 * 3.0))"),
    ("IF THEN ELSE(1 > 2, 3, 4)", "IF((1.0 > 2.0), 3.0, 4.0)"),
    ("1 > 0 :AND: 2 > 0", "((1.0 > 0.0) and (2.0 > 0.0))"),
    ("1 > 0 :OR: 2 > 0", "((1.0 > 0.0) or (2.0 > 0.0))"),
    (":NOT: 1 > 0", "(not (1.0 > 0.0))"),
    ("ZIDZ(1, 0)", "ZIDZ(1.0, 0.0)"),
    ("XIDZ(1, 0, 5)", "XIDZ(1.0, 0.0, 5.0)"),
    ("MODULO(7, 3)", "MODULO(7.0, 3.0)"),
    ("DELAY1I(x, 2, 3)", "DELAY1(AUX(\"x\"), 2.0, 3.0)"),
    ("DELAY3I(x, 2, 3)", "DELAY3(AUX(\"x\"), 2.0, 3.0)"),
    ("DELAY N(x, 2, 3, 1)", "DELAYN(AUX(\"x\"), 2.0, 1.0, 3.0)"),   # (in, tau, init, n): #170
    ("ACTIVE INITIAL(x, 1)", "ACTIVE_INITIAL(AUX(\"x\"), 1.0)"),
    # SDXorg cleanup round: builtins with an engine equivalent
    ("SMOOTHI(x, 2, 3)", "SMOOTH(AUX(\"x\"), 2.0, 3.0)"),
    ("POWER(x, 2)", "(AUX(\"x\") ** 2.0)"),
    ("INTEGER(x)", "int(AUX(\"x\"))"),
    ("LOG(8, 2)", "(log(8.0) / log(2.0))"),
    ("ARCSIN(x) + SINH(x)", "(ASIN(AUX(\"x\")) + SINH(AUX(\"x\")))"),
    ("PULSE(1, 2)", "PULSE(1, 1.0, 2.0)"),
    ("PULSE TRAIN(1, 2, 3, 4)", "IF((TNOW >= 1.0) and (TNOW <= 4.0) and "
                                "(MODULO(TNOW - 1.0, 3.0) < 2.0), 1, 0)"),
    ("IF THEN ELSE(x > 0, :NA:, 1)", "IF((AUX(\"x\") > 0.0), (-6.490371073168535e+32), 1.0)"),
])
def test_translator_functions(eqn, expected):
    # x (referenced by some of the equations above) gets its own equation too.
    layout, problems = mdlimport.import_mdl(
        f"x= 0\n\t~\t\n\t~\t\t|\n\ny= {eqn}\n\t~\t\n\t~\t\t|\n")
    assert not problems, problems
    y = next(a for a in layout["auxiliaries"] if a["name"] == "y")
    assert y["expr"] == expected


# --------------------------------------------------------------- refusals
_EQ = "\n\t~\t\n\t~\t\t|\n"


@pytest.mark.parametrize("text, needle, section", [
    pytest.param("x= FROBNICATE(5)" + _EQ, "FROBNICATE", None, id="unknown-function"),
    pytest.param("x= Nowhere" + _EQ, "Nowhere", None, id="unknown-name"),
    pytest.param("x= GET XLS DATA('file.xlsx', 'Sheet1', 'A', 'B2')" + _EQ, "external data",
                 None, id="get-xls-data"),
    pytest.param("x= GET DIRECT DATA('file.xlsx', 'A', 'B', 'C2')" + _EQ, "external data",
                 None, id="get-direct-data"),
    pytest.param("Region: East, West" + _EQ + "\nSub: East <- Region" + _EQ, "subrange", None,
                 id="subrange"),
    pytest.param("x[Nowhere]= 5" + _EQ, "Nowhere", None, id="unknown-dimension"),
    pytest.param("x= INTEG(2*y, 0)" + _EQ + "\ny= 1" + _EQ, "not a plain flow name", "levels",
                 id="integ-not-a-flow"),
    pytest.param("x= INTEG(nowhere, 0)" + _EQ, "nowhere", "levels", id="integ-undeclared"),
    pytest.param("a= INTEG(f, 0)" + _EQ + "\nb= INTEG(f, 0)" + _EQ + "\nf= 1" + _EQ,
                 "more than one stock", None, id="flow-two-stocks"),
])
def test_refused_constructs(text, needle, section):
    layout, problems = mdlimport.import_mdl(text)
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and needle in p.message for p in problems)
    if section is not None:
        assert layout[section] == []


def test_macro_is_refused_and_stripped():
    text = (":MACRO: FOO(x)\ny = x*2\n\t~\t\n\t~\t\t|\n:END OF MACRO:\n\n"
            "x= 5\n\t~\t\n\t~\t\t|\n")
    layout, problems = mdlimport.import_mdl(text)
    assert any(p.code == "E_SDIMPORT_UNSUPPORTED" and "MACRO" in p.message for p in problems)
    assert [a["name"] for a in layout["auxiliaries"]] == ["x"]


def test_with_lookup_becomes_a_table():
    text = "x= WITH LOOKUP(y, ((0,0),(1,1),(2,4)))\n\t~\t\n\t~\t\t|\n\ny= 1\n\t~\t\n\t~\t\t|\n"
    layout, problems = mdlimport.import_mdl(text)
    assert not problems, problems
    assert layout["tables"][0]["keys"] == [0.0, 1.0, 2.0]
    assert layout["tables"][0]["values"] == [0.0, 1.0, 4.0]
    x = next(a for a in layout["auxiliaries"] if a["name"] == "x")
    assert x["expr"].startswith('AUX(')


# --------------------------------------------------------------- encoding / size cap
def test_nul_byte_is_refused():
    with pytest.raises(mdlimport.MDLError) as excinfo:
        mdlimport.import_mdl(b"x = 5\x00\x00|")
    assert excinfo.value.code == "E_SDIMPORT_MDL"


def test_size_cap_is_enforced():
    with pytest.raises(mdlimport.MDLError) as excinfo:
        mdlimport.import_mdl(b"a" * (mdlimport.MAX_BYTES + 1))
    assert excinfo.value.code == "E_SDIMPORT_MDL"


def test_cp1252_fallback_decodes_a_non_utf8_file():
    # 0x93 (a curly open-quote in cp1252) is not valid as a lone UTF-8 byte.
    raw = "x= 5\n\t~\tunits\n\t~\tcomment |\n".encode("utf-8").replace(b"comment", b"comment \x93")
    layout, problems = mdlimport.import_mdl(raw)
    assert not problems
    assert layout["auxiliaries"][0]["name"] == "x"


def test_sdxorg_constructs_lookups_ranges_and_clear_refusals():
    """SDXorg cleanup round: a standalone lookup, a numeric range, a range name with
    spaces, a `\\` continuation; a repeated dim and a non-literal DELAY N order are
    refused on their own variable, not left to fail the whole run."""
    mdl = ("my table(\n\t[(0,0)-(2,2)],(0,0),(2,\\\n\t\t2))\n\t~\t~\t|\n"
           "y = my table(Time)\n\t~\t~\t|\n"
           "Some Dim: (d1-d3)\n\t~\t~\t|\n"
           "v[Some Dim] = 1, 2,\\\n 3\n\t~\t~\t|\n"
           "m[Some Dim, Some Dim] = 1\n\t~\t~\t|\n"
           "n = 2\n\t~\t~\t|\n"
           "z = DELAY N(y, 1, 0, n)\n\t~\t~\t|\n")
    layout, problems = mdlimport.import_mdl(mdl)
    assert layout["tables"][0] == {"name": "my_table", "keys": [0.0, 2.0],
                                   "values": [0.0, 2.0], "key_mode": "linear"}
    assert next(a for a in layout["auxiliaries"] if a["name"] == "y")["expr"] == \
        'TABLE("my_table", TNOW)'
    assert layout["subscripts"] == [{"name": "Some_Dim", "elements": ["d1", "d2", "d3"]}]
    assert {a["name"] for a in layout["auxiliaries"]} == {"y", "v", "n"}
    messages = " | ".join(p.message for p in problems)
    assert "repeats the dimension" in messages and "DELAY N's order" in messages


# --------------------------------------------------------------- CLI dispatch by extension
def test_cli_dispatches_mdl_by_extension(tmp_path):
    out = tmp_path / "out.json"
    rc = sdimport.main([str(DATA / "sd_vensim.mdl"), "-o", str(out)])
    assert rc == 0
    written = json.loads(out.read_text())
    assert written["levels"][0]["name"] == "Population"


# --------------------------------------------------------------- agent tool confinement
@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def test_import_mdl_reads_the_workspace_fixture_into_a_new_model(ws, tmp_path):
    shutil.copy(DATA / "sd_vensim.mdl", tmp_path / "sd_vensim.mdl")
    result = T.call("import_mdl", path="sd_vensim.mdl")
    assert "error" not in result, result.get("error")
    assert result["model"] == "sd_vensim"
    assert result["import_problems"] == []
    assert result["errors"] == 0


def test_import_mdl_refuses_a_path_outside_the_workspace(ws, tmp_path):
    outside = tmp_path.parent / "outside.mdl"
    outside.write_text("x= 5\n\t~\t\n\t~\t\t|\n", encoding="utf-8")
    result = T.call("import_mdl", path="../outside.mdl")
    assert "error" in result
