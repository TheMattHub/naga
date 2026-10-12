"""Subranges, mappings and per-element equations (PHASE6 S3, ARCHITECTURE
5.24). The oracle is SD-3's: each subscripted model is BIT-IDENTICAL to the
same model hand-written per element; the XMILE and Vensim fixtures import clean
and run like their hand-written twins.
"""
import pytest

from nagasim import mdlimport, sdimport
from nagasim.api import run_model
from nagasim.core import sd

from conftest import problem_codes

SIM = {"time_limit": 10.0, "sd": {"dt": 0.5, "method": "euler"}}


def _codes(layout):
    return problem_codes({"blocks": [], "links": [], **layout}, severity=None)


def _same_run(a, b):
    assert _codes(a) == set() and _codes(b) == set()
    ra, rb = run_model(a), run_model(b)
    for frame in ("level_summary", "flow_summary"):
        assert ra[frame].equals(rb[frame]), frame
    return ra


# ================================================================ subranges
def test_subrange_sum_and_owner_bindings_equal_the_hand_model():
    subscripted = {
        "subscripts": [{"name": "Region", "elements": ["n1", "n2", "s1"]},
                       {"name": "North", "elements": ["n1", "n2"], "parent": "Region"}],
        "levels": [{"name": "Pop", "dims": ["Region"], "initial": 10.0, "min": None}],
        "auxiliaries": [{"name": "north", "expr": 'SUM(LEVEL("Pop[North*]"))'}],
        # over the subrange: [North] and the parent's [Region] both read the owner's element
        "flows": [{"name": "g", "dims": ["North"], "to": "Pop",
                   "rate": '0.1 * LEVEL("Pop[North]") + 0 * LEVEL("Pop[Region]")'},
                  {"name": "h", "dims": ["Region"], "to": "Pop", "rate": '0.01 * AUX("north")'}],
        "simulation": SIM}
    flat = sd.expand(subscripted)
    assert {a["name"]: a["expr"] for a in flat["auxiliaries"]}["north"] == \
        '((LEVEL("Pop[n1]")) + (LEVEL("Pop[n2]")))'
    by_hand = {
        "levels": [{"name": f"Pop[{e}]", "initial": 10.0, "min": None} for e in ("n1", "n2", "s1")],
        "auxiliaries": [{"name": "north", "expr": 'LEVEL("Pop[n1]") + LEVEL("Pop[n2]")'}],
        "flows": [{"name": f"g[{e}]", "to": f"Pop[{e}]",
                   "rate": f'0.1 * LEVEL("Pop[{e}]") + 0 * LEVEL("Pop[{e}]")'} for e in ("n1", "n2")]
        + [{"name": f"h[{e}]", "to": f"Pop[{e}]", "rate": '0.01 * AUX("north")'}
           for e in ("n1", "n2", "s1")],
        "simulation": SIM}
    levels = _same_run(subscripted, by_hand)["level_summary"].set_index("Level")
    assert levels.loc["Pop[n1]", "Final"] > levels.loc["Pop[s1]", "Final"] > 10.0


# ================================================================ mappings
def test_mapped_reference_equals_the_hand_mapped_model():
    """Region maps onto Zone r1->z2, r2->z1: an owner over Region reads
    `[Zone]` (and a flow ends on `Stock[Zone]`) through the map; an owner over
    Zone reads `[Region]` through its inverse."""
    subscripted = {
        "subscripts": [{"name": "Region", "elements": ["r1", "r2"],
                        "map_to": {"dim": "Zone", "elements": ["z2", "z1"]}},
                       {"name": "Zone", "elements": ["z1", "z2"]}],
        "levels": [{"name": "P", "dims": ["Region"], "initial": 5.0},
                   {"name": "Q", "dims": ["Zone"], "initial": 1.0}],
        "auxiliaries": [{"name": "k", "dims": ["Zone"], "expr": "0.1",
                         "elements": {"z2": "0.3"}},
                        {"name": "back", "dims": ["Zone"], "expr": 'LEVEL("P[Region]")'}],
        "flows": [{"name": "move", "dims": ["Region"], "from": "P", "to": "Q[Zone]",
                   "rate": 'AUX("k[Zone]") * LEVEL("P[Region]")'},
                  {"name": "echo", "dims": ["Zone"], "to": "Q",
                   "rate": '0.01 * AUX("back[Zone]")'}],
        "simulation": SIM}
    zone_of = {"r1": "z2", "r2": "z1"}
    region_of = {z: r for r, z in zone_of.items()}
    by_hand = {
        "levels": [{"name": f"P[{r}]", "initial": 5.0} for r in zone_of]
        + [{"name": f"Q[{z}]", "initial": 1.0} for z in ("z1", "z2")],
        "auxiliaries": [{"name": "k[z1]", "expr": "0.1"}, {"name": "k[z2]", "expr": "0.3"}]
        + [{"name": f"back[{z}]", "expr": f'LEVEL("P[{region_of[z]}]")'} for z in ("z1", "z2")],
        "flows": [{"name": f"move[{r}]", "from": f"P[{r}]", "to": f"Q[{z}]",
                   "rate": f'AUX("k[{z}]") * LEVEL("P[{r}]")'} for r, z in zone_of.items()]
        + [{"name": f"echo[{z}]", "to": f"Q[{z}]", "rate": f'0.01 * AUX("back[{z}]")'}
           for z in ("z1", "z2")],
        "simulation": SIM}
    _same_run(subscripted, by_hand)
    positional = {"subscripts": [{"name": "A", "elements": ["a1", "a2"], "map_to": {"dim": "B"}},
                                 {"name": "B", "elements": ["b1", "b2"]}],
                  "auxiliaries": [{"name": "x", "dims": ["B"], "expr": "1"},
                                  {"name": "y", "dims": ["A"], "expr": 'AUX("x[B]")'}]}
    assert [a["expr"] for a in sd.expand(positional)["auxiliaries"][2:]] == \
        ['AUX("x[b1]")', 'AUX("x[b2]")']


# ================================================================ per-element equations
def test_per_element_equations_equal_a_hand_written_scalar_model():
    subscripted = {
        "subscripts": [{"name": "r", "elements": ["a", "b"]},
                       {"name": "k", "elements": ["x", "y"]}],
        "levels": [{"name": "S", "dims": ["r", "k"], "initial": 1.0,
                    "elements": {"b, y": 4.0}}],       # spaces after the comma are fine
        "flows": [{"name": "f", "dims": ["r", "k"], "to": "S", "rate": 'LEVEL("S[r,k]") * 0.1',
                   "elements": {"a,y": '2 + LEVEL("S[a,x]")', "b,x": "0"}}],
        "simulation": SIM}
    rates = {("a", "x"): 'LEVEL("S[a,x]") * 0.1', ("a", "y"): '2 + LEVEL("S[a,x]")',
             ("b", "x"): "0", ("b", "y"): 'LEVEL("S[b,y]") * 0.1'}
    by_hand = {
        "levels": [{"name": f"S[{r},{k}]", "initial": 4.0 if (r, k) == ("b", "y") else 1.0}
                   for r, k in rates],
        "flows": [{"name": f"f[{r},{k}]", "to": f"S[{r},{k}]", "rate": rate}
                  for (r, k), rate in rates.items()],
        "simulation": SIM}
    _same_run(subscripted, by_hand)


# ================================================================ codes, API
@pytest.mark.parametrize("code, subscripts, records", [
    ("E_SUBSCRIPT_RANGE", [{"name": "N", "elements": ["a", "zz"], "parent": "R"}], {}),
    ("E_SUBSCRIPT_RANGE", [{"name": "N", "elements": ["a"], "parent": "nope"}], {}),
    ("E_SUBSCRIPT_MAP", [{"name": "Z", "elements": ["z1"]},
                         {"name": "N", "elements": ["q"], "map_to": {"dim": "R"}}], {}),
    ("E_SUBSCRIPT_MAP", [{"name": "Z", "elements": ["z1"]},
                         {"name": "N", "elements": ["q"], "map_to": {"dim": "Z",
                                                                     "elements": ["z9"]}}], {}),
    ("E_SUBSCRIPT_ELEMENT", [], {"auxiliaries": [{"name": "x", "dims": ["R"], "expr": "1",
                                                  "elements": {"c": "2"}}]}),
    ("E_SUBSCRIPT_ELEMENT", [], {"auxiliaries": [{"name": "x", "expr": "1",
                                                  "elements": {"a": "2"}}]}),
    ("E_SUBSCRIPT_DIM", [{"name": "N", "elements": ["a"], "parent": "R"}],
     {"levels": [{"name": "P", "dims": ["R"]}],
      "auxiliaries": [{"name": "x", "expr": 'LEVEL("P[N*]")'}]}),
])
def test_s3_problem_codes(code, subscripts, records):
    layout = {"subscripts": [{"name": "R", "elements": ["a", "b"]}] + subscripts,
              "levels": [{"name": "L"}], **records, "simulation": {"time_limit": 1.0}}
    if "levels" in records:
        layout["levels"] = [{"name": "L"}] + records["levels"]
    assert code in _codes(layout)


def test_python_api_carries_parent_map_to_and_elements():
    layout = sd.add({}, sd.Subscript("R", ["a", "b"]),
                    sd.Subscript("N", ["a"], parent="R", map_to="Z"),
                    sd.Aux("x", "1", dims=["R"], elements={"b": "2"}))
    assert layout["subscripts"][1] == {"name": "N", "elements": ["a"], "parent": "R",
                                       "map_to": {"dim": "Z"}}
    assert sd.Subscript.from_dict(layout["subscripts"][1]) == sd.Subscript(
        "N", ["a"], parent="R", map_to={"dim": "Z"})
    assert layout["auxiliaries"][0]["elements"] == {"b": "2"}


# ================================================================ import
XMILE = """<xmile><sim_specs time_units="days"><start>0</start><stop>10</stop><dt>0.5</dt>
</sim_specs><dimensions><dim name="Region"><elem name="n1"/><elem name="n2"/><elem name="s1"/>
</dim></dimensions><model><variables>
<stock name="Pop"><dimensions><dim name="Region"/></dimensions><inflow>growth</inflow>
  <element subscript="n1"><eqn>10</eqn></element><element subscript="n2"><eqn>20</eqn></element>
  <element subscript="s1"><eqn>30</eqn></element></stock>
<flow name="growth"><dimensions><dim name="Region"/></dimensions><non_negative/>
  <element subscript="n1"><eqn>rate_n*Pop[n1]</eqn></element>
  <element subscript="n2"><eqn>rate_n*Pop</eqn></element>
  <element subscript="s1"><eqn>rate_s*Pop[s1] + 0.001*total</eqn></element></flow>
<aux name="rate_n"><eqn>0.05</eqn></aux><aux name="rate_s"><eqn>0.02</eqn></aux>
<aux name="total"><eqn>SUM(Pop[*])</eqn></aux>
</variables></model></xmile>"""


def test_xmile_non_apply_to_all_runs_like_its_hand_written_twin():
    imported, problems = sdimport.import_xmile(XMILE)
    assert problems == []
    assert imported["levels"][0]["elements"] == {"n1": "10.0", "n2": "20.0", "s1": "30.0"}
    twin = {
        "levels": [{"name": f"Pop[{e}]", "initial": v, "min": None}
                   for e, v in (("n1", 10.0), ("n2", 20.0), ("s1", 30.0))],
        "auxiliaries": [{"name": "rate_n", "expr": "0.05"}, {"name": "rate_s", "expr": "0.02"},
                        {"name": "total",
                         "expr": 'LEVEL("Pop[n1]") + LEVEL("Pop[n2]") + LEVEL("Pop[s1]")'}],
        "flows": [{"name": f"growth[{e}]", "to": f"Pop[{e}]",
                   "rate": f'AUX("rate_n") * LEVEL("Pop[{e}]")'} for e in ("n1", "n2")]
        + [{"name": "growth[s1]", "to": "Pop[s1]",
            "rate": 'AUX("rate_s") * LEVEL("Pop[s1]") + 0.001 * AUX("total")'}],
        "simulation": imported["simulation"]}
    _same_run(imported, twin)


MDL = """{UTF-8}
Region: n1, n2, s1 -> (Zone: z3, z1, z2)
\t~\t|
North: n1, n2
\t~\t|
Zone: z1, z2, z3
\t~\t|
Pop[Region]= INTEG(growth[Region], init_pop[Region])
\t~\tpeople\t~\t|
init_pop[Region]= 10, 20, 30
\t~\tpeople\t~\t|
growth[North]= rate_n*Pop[North]
\t~\tpeople/Day\t~\t|
growth[s1]= rate_s*Pop[s1] + boost[Zone] + 0.001*north_total
\t~\tpeople/Day\t~\t|
boost[Zone]= 1, 2, 3
\t~\tpeople/Day\t~\t|
rate_n= 0.05
\t~\t1/Day\t~\t|
rate_s= 0.02
\t~\t1/Day\t~\t|
north_total= SUM(Pop[North!])
\t~\tpeople\t~\t|
FINAL TIME= 10
\t~\tDay\t~\t|
INITIAL TIME= 0
\t~\tDay\t~\t|
TIME STEP= 0.5
\t~\tDay\t~\t|
"""


def test_vensim_subrange_mapping_and_per_element_run_like_the_hand_written_twin():
    imported, problems = mdlimport.import_mdl(MDL)
    assert problems == []
    subs = {s["name"]: s for s in imported["subscripts"]}
    assert subs["North"]["parent"] == "Region"
    assert subs["Region"]["map_to"] == {"dim": "Zone", "elements": ["z3", "z1", "z2"]}
    zone_of = {"n1": "z3", "n2": "z1", "s1": "z2"}
    twin = {
        "levels": [{"name": f"Pop[{e}]", "initial": f'AUX("init_pop[{e}]")'} for e in zone_of],
        "auxiliaries": [{"name": f"init_pop[{e}]", "expr": v}
                        for e, v in (("n1", "10.0"), ("n2", "20.0"), ("s1", "30.0"))]
        + [{"name": f"boost[{z}]", "expr": v}
           for z, v in (("z1", "1.0"), ("z2", "2.0"), ("z3", "3.0"))]
        + [{"name": "rate_n", "expr": "0.05"}, {"name": "rate_s", "expr": "0.02"},
           {"name": "north_total", "expr": 'LEVEL("Pop[n1]") + LEVEL("Pop[n2]")'}],
        "flows": [{"name": f"growth[{e}]", "to": f"Pop[{e}]",
                   "rate": f'AUX("rate_n") * LEVEL("Pop[{e}]")'} for e in ("n1", "n2")]
        + [{"name": "growth[s1]", "to": "Pop[s1]",
            "rate": 'AUX("rate_s") * LEVEL("Pop[s1]") + AUX("boost[z2]")'
                    ' + 0.001 * AUX("north_total")'}],
        "simulation": imported["simulation"]}
    _same_run(imported, twin)


def test_vensim_per_element_refusals():
    head = "R: a, b\n~|\n"
    _, problems = mdlimport.import_mdl(head + "x[R]= 1, 2, 3\n~|\n")
    assert any("3 numbers for 2 elements" in p.message for p in problems)
    _, problems = mdlimport.import_mdl(head + "x= 1\n~|\nx= 2\n~|\n")
    assert any("duplicate" in p.message for p in problems)
