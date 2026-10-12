"""Subscripts (PHASE5 SD-3, ARCHITECTURE 5.24): `sd.expand` flattens a
subscripted level/flow/auxiliary/delay into one scalar record per element.
The oracle is the plan's: a 2-region SIR written with subscripts is
BIT-IDENTICAL to the same model hand-copied per region. Then the rewrite
rules, the codes, the authoring classes, the document, the agent tools, and
the perf guards (a fast evaluation-count proxy, the 1000 x 8760 slow ones).
"""
import time

import pytest

from nagasim.api import run_model
from nagasim.core import sd
from nagasim.core.schema import migrate
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

from conftest import problem_codes

REGIONS = ["north", "south"]
SIR_VARIABLES = [{"name": "beta", "initial": 0.3}, {"name": "gamma", "initial": 0.1},
                 {"name": "m", "initial": 0.05}]


def _sir_subscripted(method):
    return {
        "blocks": [], "links": [], "variables": SIR_VARIABLES,
        "subscripts": [{"name": "region", "elements": REGIONS}],
        "levels": [{"name": "S", "initial": 990.0, "dims": ["region"]},
                   {"name": "I", "initial": 'IF(1, 10, 0)', "dims": ["region"]},
                   {"name": "R", "initial": 0.0, "dims": ["region"]}],
        "auxiliaries": [
            {"name": "N", "dims": ["region"],
             "expr": 'LEVEL("S[region]") + LEVEL("I[region]") + LEVEL("R[region]")'},
            {"name": "total_I", "expr": 'SUM(LEVEL("I[*]"))'},
            {"name": "mean_I", "expr": 'MEAN(LEVEL("I[*]"))'}],
        "flows": [
            {"name": "inf", "dims": ["region"], "from": "S", "to": "I[region]",
             "rate": 'beta * LEVEL("S[region]") * (LEVEL("I[region]") + m * AUX("total_I"))'
                     ' / AUX("N[region]")'},
            {"name": "rec", "dims": ["region"], "from": "I", "to": "R",
             "rate": 'gamma * LEVEL("I[region]") + 0 * AUX("mean_I")'}],
        "simulation": {"time_limit": 60.0, "sd": {"dt": 0.25, "method": method}}}


def _sir_by_hand(method):
    """The same model, typed out once per region (the plan's SD-3 oracle)."""
    def each(make):
        return [make(r) for r in REGIONS]
    return {
        "blocks": [], "links": [], "variables": SIR_VARIABLES,
        "levels": (each(lambda r: {"name": f"S[{r}]", "initial": 990.0})
                   + each(lambda r: {"name": f"I[{r}]", "initial": 'IF(1, 10, 0)'})
                   + each(lambda r: {"name": f"R[{r}]", "initial": 0.0})),
        "auxiliaries": each(lambda r: {
            "name": f"N[{r}]",
            "expr": f'LEVEL("S[{r}]") + LEVEL("I[{r}]") + LEVEL("R[{r}]")'}) + [
            {"name": "total_I", "expr": 'LEVEL("I[north]") + LEVEL("I[south]")'},
            {"name": "mean_I", "expr": '(LEVEL("I[north]") + LEVEL("I[south]")) / 2'}],
        "flows": (each(lambda r: {
            "name": f"inf[{r}]", "from": f"S[{r}]", "to": f"I[{r}]",
            "rate": f'beta * LEVEL("S[{r}]") * (LEVEL("I[{r}]") + m * AUX("total_I"))'
                    f' / AUX("N[{r}]")'})
                  + each(lambda r: {"name": f"rec[{r}]", "from": f"I[{r}]", "to": f"R[{r}]",
                                    "rate": f'gamma * LEVEL("I[{r}]") + 0 * AUX("mean_I")'})),
        "simulation": {"time_limit": 60.0, "sd": {"dt": 0.25, "method": method}}}


def _codes(layout):
    return problem_codes(layout, severity=None)


# ================================================================ the oracle
def test_two_region_sir_is_bit_identical_to_two_hand_copies():
    # Euler only: expansion runs before the integrator, so an RK4 twin adds no claim.
    subscripted = _sir_subscripted("euler")
    assert _codes(subscripted) == set()
    a, b = run_model(subscripted), run_model(_sir_by_hand("euler"))
    for frame in ("level_summary", "flow_summary"):
        assert a[frame].equals(b[frame]), frame
    levels = a["level_summary"]
    assert list(levels["Level"]) == [f"{s}[{r}]" for s in "SIR" for r in REGIONS]
    assert levels["Final"].sum() == pytest.approx(2000.0)       # conserved
    assert levels.loc[levels["Level"] == "I[north]", "Max"].iloc[0] > 10.0   # it spread


# ================================================================ the rewrite
def test_expand_flattens_names_expressions_and_flow_ends_per_element():
    layout = _sir_subscripted("euler")
    before = repr(layout)
    out = sd.expand(layout)
    assert repr(layout) == before                                       # pure
    assert sd.expand(out)["flows"] == out["flows"]                      # idempotent
    flows = {f["name"]: f for f in out["flows"]}
    assert flows["inf[south]"]["from"] == "S[south]" and flows["inf[south]"]["to"] == "I[south]"
    assert 'AUX("N[south]")' in flows["inf[south]"]["rate"]
    assert all("dims" not in f for f in out["flows"])
    aux = {a["name"]: a["expr"] for a in out["auxiliaries"]}
    assert aux["total_I"] == '((LEVEL("I[north]")) + (LEVEL("I[south]")))'
    assert aux["mean_I"] == '(((LEVEL("I[north]")) + (LEVEL("I[south]"))) / 2)'
    no_subscripts = {"levels": [{"name": "L"}]}
    assert sd.expand(no_subscripts) is no_subscripts                    # identity


def test_two_dims_first_slowest_and_star_over_one_dim_keeps_the_other_bound():
    layout = {"subscripts": [{"name": "r", "elements": ["a", "b"]},
                             {"name": "k", "elements": ["x", "y", "z"]}],
              "levels": [{"name": "P", "dims": ["r", "k"], "initial": 1.0}],
              "auxiliaries": [{"name": "byk", "dims": ["k"], "expr": 'SUM(LEVEL("P[*,k]"))'}]}
    out = sd.expand(layout)
    assert [lv["name"] for lv in out["levels"]] == ["P[a,x]", "P[a,y]", "P[a,z]",
                                                   "P[b,x]", "P[b,y]", "P[b,z]"]
    assert {a["name"]: a["expr"] for a in out["auxiliaries"]}["byk[y]"] == \
        '((LEVEL("P[a,y]")) + (LEVEL("P[b,y]")))'


def test_sum_and_mean_values_and_a_1000_term_sum_compiles():
    elements = [f"e{i}" for i in range(1000)]
    layout = {"blocks": [], "links": [],
              "subscripts": [{"name": "i", "elements": elements}],
              "levels": [{"name": "P", "dims": ["i"], "initial": 2.0, "min": None}],
              "auxiliaries": [{"name": "total", "expr": 'SUM(LEVEL("P[*]"))'},
                              {"name": "avg", "expr": 'MEAN(LEVEL("P[*]") * 3)'}],
              "flows": [{"name": "f", "dims": ["i"], "to": "P", "rate": '1 + 0 * AUX("total")'},
                        {"name": "g", "rate": 'AUX("avg")'}],
              "simulation": {"time_limit": 1.0}}
    assert _codes(layout) == set()
    result = run_model(layout)
    flows = result["flow_summary"].set_index("Flow")
    assert flows.loc["g", "Total"] == pytest.approx(6.0, rel=1e-12)     # 3 x mean(2), held (no grid)
    assert len(result["level_summary"]) == 1000


def test_inline_macro_and_delay_record_are_expanded_per_element():
    layout = {"subscripts": [{"name": "r", "elements": ["a", "b"]}],
              "levels": [{"name": "L", "dims": ["r"]}],
              "flows": [{"name": "f", "dims": ["r"], "to": "L", "rate": 'SMOOTH(TNOW, 2)'}],
              "delays": [{"name": "d", "dims": ["r"], "input": 'LEVEL("L[r]")', "delay_time": 3,
                          "kind": "information"}],
              "simulation": {"time_limit": 5.0, "sd": {"dt": 0.5}}}
    out = sd.expand(layout)
    delays = {d["name"]: d for d in out["delays"]}
    assert set(delays) == {"d[a]", "d[b]", "f[a]#1", "f[b]#1"}
    assert delays["d[b]"]["input"] == 'LEVEL("L[b]")'
    assert {"d[a]~s1", "f[b]~s1"} <= {lv["name"] for lv in out["levels"]}
    assert _codes({"blocks": [], "links": [], **layout}) == set()


# ================================================================ codes
@pytest.mark.parametrize("code, extra", [
    ("E_SUBSCRIPT_NAME", {"subscripts": [{"name": "1bad", "elements": ["a"]}]}),
    ("E_SUBSCRIPT_NAME", {"subscripts": [{"name": "r", "elements": ["a", "a"]}]}),
    ("E_SUBSCRIPT_NAME", {"subscripts": [{"name": "r", "elements": ["a[1]"]}]}),
    ("E_SUBSCRIPT_NAME", {"subscripts": [{"name": "r", "elements": ["a"]},
                                         {"name": "r", "elements": ["b"]}]}),
    ("E_SUBSCRIPT_UNKNOWN", {"levels": [{"name": "P", "dims": ["nope"]}]}),
    ("E_SUBSCRIPT_DIM", {"levels": [{"name": "P", "dims": ["r"]}],
                         "auxiliaries": [{"name": "x", "expr": 'LEVEL("P[r]")'}]}),
    ("E_SUBSCRIPT_DIM", {"levels": [{"name": "P", "dims": ["r"]}],
                         "auxiliaries": [{"name": "x", "expr": 'LEVEL("P[*]")'}]}),
    ("E_SUBSCRIPT_DIM", {"levels": [{"name": "P", "dims": ["r"]}],
                         "flows": [{"name": "f", "rate": "1", "to": "P"}]}),
    ("E_SUBSCRIPT_DIM", {"levels": [{"name": "P", "dims": ["r", "r"]}]}),
    ("E_SUBSCRIPT_AGGREGATE", {"auxiliaries": [{"name": "x", "expr": "SUM(3)"}]}),
    ("E_SUBSCRIPT_AGGREGATE", {"levels": [{"name": "P", "dims": ["r"]}],
                               "blocks": [{"name": "A", "type": "ASSIGN", "params": {
                                   "assignments": [{"target": "entity.x",
                                                    "expr": 'SUM(LEVEL("P[*]"))'}]}}]}),
    ("E_AGGREGATE_AGENTS", {"auxiliaries": [{"name": "x", "expr": 'SUM("people", 1)'}]}),
])
def test_subscript_problem_codes(code, extra):
    layout = {"blocks": [], "links": [], "levels": [{"name": "L"}],
              "subscripts": [{"name": "r", "elements": ["a", "b"]}]}
    for key, records in extra.items():
        layout[key] = layout.get(key, []) + records if key == "levels" else records
    assert code in _codes(layout)


def test_a_valid_subscripted_model_has_no_problems_and_migrate_adds_the_section():
    assert _codes(_sir_subscripted("rk4")) == set()
    assert migrate({"blocks": [], "links": []})["subscripts"] == []


# ================================================================ Python API, document, tools
def test_authoring_classes_carry_dims_and_subscripts():
    layout = sd.add({}, sd.Subscript("r", ["a", "b"]), sd.Stock("P", 1.0, dims=["r"]),
                    sd.Flow("f", "1", target="P", dims=["r"]), sd.Aux("x", "2", dims=("r",)),
                    sd.Delay("d", "1", 2, dims=["r"]), sd.Stock("Q"))
    assert layout["subscripts"] == [{"name": "r", "elements": ["a", "b"]}]
    assert layout["auxiliaries"][0]["dims"] == ["r"] and "dims" not in layout["levels"][1]
    assert sd.Stock.from_dict(layout["levels"][0]) == sd.Stock("P", 1.0, dims=["r"])
    assert sd.Subscript.from_dict({"name": "r", "elements": [1, 2]}).elements == ["1", "2"]


def test_document_round_trip(tmp_path):
    from nagasim.core.document import ModelDocument
    doc = ModelDocument()
    doc.upsert_subscript(sd.Subscript("r", ["a", "b"]))
    doc.upsert_level(sd.Stock("P", 1.0, dims=["r"]).to_dict())
    doc.save(tmp_path / "m.json")
    back = ModelDocument.load(tmp_path / "m.json")
    assert back.subscripts == [{"name": "r", "elements": ["a", "b"]}]
    assert back.levels[0]["dims"] == ["r"]
    back.remove_subscript("r")
    assert back.subscripts == []
    assert "E_SUBSCRIPT_UNKNOWN" in _codes(back.to_layout())


def test_agent_tools_upsert_and_remove_subscript(tmp_path):
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    def upsert(section, **record):
        return T.call("upsert_record", model="m", section=section, record=record)

    result = upsert("subscripts", name="region", elements=["north", "south"])
    assert result["record"] == "region" and "error" not in result
    upsert("levels", name="Pop", initial=5.0, dims=["region"])
    upsert("auxiliaries", name="k", expr="0.1", dims=["region"])
    upsert("delays", name="d", input='LEVEL("Pop[region]")', delay_time=2, kind="pipeline",
           dims=["region"])
    upsert("flows", name="grow", to="Pop", dims=["region"],
           rate='AUX("k[region]") * LEVEL("Pop[region]") + 0 * DELAY("d[region]")')
    listed = T.call("list_continuous", model="m")
    assert listed["subscripts"] == [{"name": "region", "elements": ["north", "south"]}]
    assert {r["dims"][0] for key in ("levels", "flows", "auxiliaries", "delays")
            for r in listed[key]} == {"region"}
    removed = T.call("remove_record", model="m", section="subscripts", key="region")
    assert removed["removed"] == "region"
    assert "E_SUBSCRIPT_UNKNOWN" in {p["code"] for p in removed["introduced"]}


def test_agent_tools_upsert_subscript_parent_and_map_to(tmp_path):
    """PHASE6 S10 follow-up: a subscript record carries `parent` (subrange) and
    `map_to` (element-wise mapping, PHASE6 S3). D147: the generic record tool
    stores the file's own shape, so a bare-string `map_to` (the old typed
    tool's shorthand) is reported rather than silently rewritten."""
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")

    def upsert(**record):
        return T.call("upsert_record", model="m", section="subscripts", record=record)

    upsert(name="Region", elements=["North", "South"])
    upsert(name="North", elements=["North"], parent="Region")
    assert upsert(name="Zone", elements=["Z1", "Z2"], map_to={"dim": "Region"})["introduced"] == []
    listed = T.call("list_continuous", model="m")
    by_name = {s["name"]: s for s in listed["subscripts"]}
    assert by_name["North"]["parent"] == "Region"
    assert by_name["Zone"]["map_to"] == {"dim": "Region"}
    bare = upsert(name="Zone", elements=["Z1", "Z2"], map_to="Region")
    assert "E_SUBSCRIPT_MAP" in {p["code"] for p in bare["introduced"]}


# ================================================================ perf
def _grid_model(n, steps, method="euler"):
    return {"blocks": [], "links": [],
            "subscripts": [{"name": "i", "elements": [f"e{k}" for k in range(n)]}],
            "levels": [{"name": "P", "dims": ["i"], "initial": 100.0}],
            "flows": [{"name": "out", "dims": ["i"], "from": "P",
                       "rate": '0.001 * LEVEL("P[i]")'}],
            "simulation": {"time_limit": float(steps), "sd": {"dt": 1.0, "method": method}}}


def _run(layout):
    """Deliberately NOT `conftest.run_env`: every caller monkeypatches the
    level system (`_evaluate`, `_ins`, a perf timer) between `prepare_model`
    and `start`/`step_until`, so it needs the steps split apart, not run to
    completion in one call."""
    run_len = layout["simulation"]["time_limit"]
    env = initialize_sim_env(seed=0)
    prepare_model(env, layout)
    return env, run_len


def test_perf_proxy_one_evaluation_per_element_per_step_and_no_saturation_sums():
    """Fast proxy for the slow guards below: 10 elements x 20 Euler steps cost
    exactly 10 x 21 rate evaluations (t=0 and each grid point), and a level
    nowhere near a bound never has its inflow/outflow totals summed (the SD-3
    saturation skip)."""
    env, run_len = _run(_grid_model(10, 20))
    system = env.ctx.level_system
    evaluated, summed = [0], [0]
    real_evaluate = system._evaluate

    def counting(todo):
        evaluated[0] += len(todo)
        real_evaluate(todo)
    system._evaluate = counting

    class Watch(dict):          # counts the saturation loop's reads of the wiring
        def __getitem__(self, key):
            summed[0] += 1
            return dict.__getitem__(self, key)
    system._ins = Watch(system._ins)
    system.start(run_len)
    step_until(env, run_len)
    system.sync(run_len)
    assert evaluated[0] == 10 * 21
    assert summed[0] == 0
    assert system.levels["P[e3]"].level == pytest.approx(100 * 0.999 ** 20, rel=1e-12)


@pytest.mark.slow
@pytest.mark.parametrize("method, budget", [("euler", 4.0), ("rk4", 10.0)])
def test_perf_1000_elements_x_876_steps(method, budget):
    """Perf guard (PHASE5 design refinements: 1000 elements x 8760 steps,
    Euler <= 20 s, RK4 <= 45 s), run at a TENTH of the steps (budgets a tenth plus setup headroom) so
    the full set stays under its ceiling; the per-step cost is what it guards. Full size measured 2026-09-24 before the SD-3 cheap wins:
    Euler 19.97 s, RK4 44.92 s; after (saturation skip for unbounded-now
    levels, price loop over priced flows only, LEVEL() without a no-op sync):
    Euler 14.1 s, RK4 36.1 s.

    Budgets widened 2026-10-07 (were 2.5 s and 5.5 s). The same commit ran in
    1.9-2.6 s (Euler) and 4.8-7.1 s (RK4) on one machine within a day,
    depending on what else it was doing, so RK4 failed on code nobody had
    touched. 4 s and 10 s are about 1.5x the slowest of those: they still fail
    on a per-step cost that doubles, which is what this guards."""
    steps = 876
    started = time.perf_counter()
    env, run_len = _run(_grid_model(1000, steps, method))
    env.ctx.level_system.start(run_len)
    step_until(env, run_len)
    env.ctx.level_system.sync(run_len)
    elapsed = time.perf_counter() - started
    assert env.ctx.level_system.levels["P[e999]"].level == pytest.approx(
        100 * 0.999 ** steps, rel=1e-6 if method == "euler" else 1e-2)
    assert elapsed < budget, f"{elapsed:.1f} s"
