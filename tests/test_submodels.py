"""Submodels as reusable definitions (ROADMAP 2.3 part B, schema v12).

Pure Python: flattening, `$` scoping, parameters, validation, the document API
and the override paths. The Qt side lives in tests/test_ui_submodels.py.
"""
import json
from pathlib import Path

import pytest

from nagasim import api, experiments
from nagasim.core import schema, submodels
from nagasim.core.document import ModelDocument
from nagasim.core.playback import Timeline, visible_name
from nagasim.core.quantities import fill_units
from nagasim.core.schema import ModelValidationError
from nagasim.core.trace import TraceEvent

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
TWO_CELL = MODELS_DIR / "two_cell_line.json"


# ---------------------------------------------------------------- builders
def block(name, type_name, **params):
    return {"name": name, "description": name, "type": type_name,
            "position": [0, 0], "params": params}


def link(source, target, **extra):
    return {"source": source, "target": target, **extra}


def layout(blocks, links, definitions=None, **sections):
    doc = {"schema_version": schema.CURRENT_VERSION, "blocks": blocks, "links": links,
           "resources": [], "resource_sets": [], "entities": [], "variables": [],
           "schedules": [], "failures": [], "scenarios": [], "annotations": [],
           "submodels": definitions or {},
           "simulation": {"time_limit": None, "warmup": 0, "replications": 1}}
    doc.update(sections)
    return doc


def definition(blocks, links, parameters=None):
    return {"info": {}, "parameters": parameters or [], "blocks": blocks, "links": links,
            "annotations": []}


PASS_THROUGH = definition(
    [block("in", "SUB_ENTRY", port="in"),
     block("work", "DELAY", distribution="Constant", dist_params={"value": 2.0}),
     block("out", "SUB_EXIT", port="out")],
    [link("in", "work"), link("work", "out")],
)


# ---------------------------------------------------------------- flattening
def test_flatten_of_a_single_instance_is_exactly_this_layout():
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"), block("cell", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": PASS_THROUGH},
    ))

    assert flat["blocks"] == [
        {"name": "create_1", "description": "create_1", "type": "CREATE",
         "position": [0, 0], "params": {}},
        {"name": "dispose_1", "description": "dispose_1", "type": "DISPOSE",
         "position": [0, 0], "params": {}},
        {"name": "cell/work", "description": "work", "type": "DELAY", "position": [0, 0],
         "params": {"distribution": "Constant", "dist_params": {"value": 2.0}}},
    ]
    assert flat["links"] == [
        {"source": "create_1", "target": "cell/work"},
        {"source": "cell/work", "target": "dispose_1"},
    ]
    assert "submodels" not in flat


def test_flatten_of_a_nested_instance_prefixes_every_level():
    outer = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("b", "SUBMODEL", definition="Inner", arguments={}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "b"), link("b", "out")],
    )
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"), block("a", "SUBMODEL", definition="Outer", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "a"), link("a", "dispose_1")],
        {"Outer": outer, "Inner": PASS_THROUGH},
    ))

    assert [b["name"] for b in flat["blocks"]] == ["create_1", "dispose_1", "a/b/work"]
    assert flat["links"] == [
        {"source": "create_1", "target": "a/b/work"},
        {"source": "a/b/work", "target": "dispose_1"},
    ]


def test_flatten_routes_multi_port_definitions_through_the_named_ports():
    router = definition(
        [block("in_a", "SUB_ENTRY", port="a"), block("in_b", "SUB_ENTRY", port="b"),
         block("merge", "DELAY", distribution="Constant", dist_params={"value": 0.0}),
         block("split", "DECIDE", mode="Chance", probs=[0.5, 0.5], conditions=[]),
         block("out_ok", "SUB_EXIT", port="ok"), block("out_ko", "SUB_EXIT", port="ko")],
        [link("in_a", "merge"), link("in_b", "merge"), link("merge", "split"),
         link("split", "out_ok"), link("split", "out_ko")],
    )
    flat = submodels.flatten(layout(
        [block("c1", "CREATE"), block("c2", "CREATE"),
         block("r", "SUBMODEL", definition="Router", arguments={}),
         block("d1", "DISPOSE"), block("d2", "DISPOSE")],
        [link("c1", "r", target_port="a"), link("c2", "r", target_port="b"),
         link("r", "d1", source_port="ok"), link("r", "d2", source_port="ko")],
        {"Router": router},
    ))

    assert flat["links"] == [
        {"source": "c1", "target": "r/merge"},
        {"source": "c2", "target": "r/merge"},       # SUB_ENTRY fan-in
        {"source": "r/merge", "target": "r/split"},
        {"source": "r/split", "target": "d1"},       # branch 0 == port "ok"
        {"source": "r/split", "target": "d2"},       # branch 1 == port "ko"
    ]


def test_two_exits_of_one_instance_may_end_at_the_same_outside_block():
    """PHASE4 T4 (D77): the Inspection case -- `ok` and `ko` both give the WIP
    token back through the same outside block, and two outside sources feed one
    entry port. Both ends flatten to the right inner blocks, in branch order,
    with no merge hop in between."""
    router = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("split", "DECIDE", mode="Chance", probs=[0.5, 0.5], conditions=[]),
         block("out_ok", "SUB_EXIT", port="ok"), block("out_ko", "SUB_EXIT", port="ko")],
        [link("in", "split"), link("split", "out_ok"), link("split", "out_ko")],
    )
    flat = submodels.flatten(layout(
        [block("c1", "CREATE"), block("c2", "CREATE"),
         block("r", "SUBMODEL", definition="Router", arguments={}),
         block("back", "DELAY", distribution="Constant", dist_params={"value": 1.0}),
         block("d1", "DISPOSE")],
        [link("c1", "r", target_port="in"), link("c2", "r", target_port="in"),
         link("r", "back", source_port="ok"), link("r", "back", source_port="ko"),
         link("back", "d1")],
        {"Router": router},
    ))

    assert flat["links"] == [
        {"source": "c1", "target": "r/split"},
        {"source": "c2", "target": "r/split"},       # two sources, one entry port
        {"source": "back", "target": "d1"},
        {"source": "r/split", "target": "back"},     # branch 0 == port "ok"
        {"source": "r/split", "target": "back"},     # branch 1 == port "ko"
    ]


def test_branch_order_is_preserved_per_output_port():
    """Swapping the instance's two outgoing links swaps the branches the inner
    DECIDE routes to -- order is per port, not per link list position."""
    router = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("split", "DECIDE", mode="Chance", probs=[0.5, 0.5], conditions=[]),
         block("out_ok", "SUB_EXIT", port="ok"), block("out_ko", "SUB_EXIT", port="ko")],
        [link("in", "split"), link("split", "out_ok"), link("split", "out_ko")],
    )
    base = [block("c1", "CREATE"), block("r", "SUBMODEL", definition="Router", arguments={}),
            block("d1", "DISPOSE"), block("d2", "DISPOSE")]
    swapped = submodels.flatten(layout(
        base,
        [link("c1", "r"), link("r", "d2", source_port="ko"), link("r", "d1", source_port="ok")],
        {"Router": router},
    ))
    assert [(l["source"], l["target"]) for l in swapped["links"] if l["source"] == "r/split"] \
        == [("r/split", "d1"), ("r/split", "d2")]


def test_link_delay_survives_the_rewiring():
    delay = {"distribution": "Constant", "dist_params": {"value": 7.0}, "time_unit": "base"}
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"), block("cell", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell", delay=delay), link("cell", "dispose_1")],
        {"Cell": PASS_THROUGH},
    ))
    assert flat["links"][0] == {"source": "create_1", "target": "cell/work", "delay": delay}


def test_flatten_is_the_identity_for_a_model_without_submodels():
    plain = layout([block("create_1", "CREATE"), block("dispose_1", "DISPOSE")],
                   [link("create_1", "dispose_1")])
    assert submodels.flatten(plain) is plain


def test_example_models_still_flatten_to_themselves_or_validate():
    for model in sorted(path.name for path in MODELS_DIR.glob("*.json")):
        raw = schema.migrate(json.loads((MODELS_DIR / model).read_text()))
        if not submodels.has_submodels(raw):
            assert submodels.flatten(raw) is raw, model
        blocking = [p for p in schema.validate_model(raw) if not p.startswith("warning:")]
        assert blocking == [], model


# ---------------------------------------------------------------- $ scoping
def _scoped_layout():
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="$Machine"),
         block("team", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="$Team"),
         block("hold_1", "HOLD", mode="Signal", signal_name="$go", conditions=[],
               scan_interval=1.0),
         block("signal_1", "SIGNAL", distribution="Constant", dist_params={"value": 0.0},
               signal_name="$go"),
         block("rec", "RECORD", stat_name="$count", kind="tally",
               expr='NQ("$Machine") + NR("$Team") + MR("$Machine") + NS("$hold_1")'),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "team"), link("team", "hold_1"),
         link("hold_1", "signal_1"), link("signal_1", "rec"), link("rec", "out")],
    )
    return layout(
        [block("create_1", "CREATE"), block("cell", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Machine", "capacity": 1, "discipline": "FIFO",
                    "capacity_schedule": "Shift"},
                   {"name": "A", "capacity": 1}, {"name": "B", "capacity": 1}],
        resource_sets=[{"name": "Team", "members": ["A", "B"], "rule": "PreferredOrder"}],
        schedules=[{"name": "Shift", "kind": "capacity", "points": [[60, 1]], "repeat": True}],
    )


def test_dollar_scoping_rewrites_every_reference_kind():
    flat = submodels.flatten(_scoped_layout())
    params = {b["name"]: b["params"] for b in flat["blocks"]}

    assert params["cell/machine"]["resource_name"] == "cell/Machine"
    assert params["cell/team"]["resource_name"] == "cell/Team"
    assert params["cell/hold_1"]["signal_name"] == "cell/go"
    assert params["cell/signal_1"]["signal_name"] == "cell/go"
    assert params["cell/rec"]["stat_name"] == "cell/count"
    assert params["cell/rec"]["expr"] == (
        'NQ("cell/Machine") + NR("cell/Team") + MR("cell/Machine") + NS("cell/hold_1")')


def test_dollar_scoping_clones_resources_sets_and_their_schedules():
    flat = submodels.flatten(_scoped_layout())
    resources = {r["name"]: r for r in flat["resources"]}
    sets = {s["name"]: s for s in flat["resource_sets"]}
    schedules = {s["name"] for s in flat["schedules"]}

    assert resources["cell/Machine"]["capacity"] == 1
    assert resources["cell/Machine"]["capacity_schedule"] == "cell/Shift"
    assert "cell/Shift" in schedules and "Shift" in schedules      # template kept
    assert sets["cell/Team"]["members"] == ["cell/A", "cell/B"]    # members cloned too
    assert {"cell/A", "cell/B"} <= set(resources)


def test_a_name_without_a_dollar_stays_global_and_is_shared():
    shared = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="Machine"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "out")],
    )
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("a", "SUBMODEL", definition="Cell", arguments={}),
         block("b", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "a"), link("a", "b"), link("b", "dispose_1")],
        {"Cell": shared},
        resources=[{"name": "Machine", "capacity": 1}],
    ))
    names = {b["name"]: b["params"]["resource_name"] for b in flat["blocks"]
             if b["name"].endswith("machine")}
    assert names == {"a/machine": "Machine", "b/machine": "Machine"}
    assert [r["name"] for r in flat["resources"]] == ["Machine"]


# ---------------------------------------------------------------- scoped overrides (item 7)
def test_scoped_override_sets_a_cloned_resource_field_from_an_instance_argument():
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="$Machine"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "out")],
        parameters=[{"name": "servers", "default": 1}],
    )
    inner["scoped"] = {"resources": {"Machine": {"capacity": "servers"}}}
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("cell", "SUBMODEL", definition="Cell", arguments={"servers": 3}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Machine", "capacity": 1}],
    ))
    resources = {r["name"]: r for r in flat["resources"]}
    assert resources["cell/Machine"]["capacity"] == 3
    assert isinstance(resources["cell/Machine"]["capacity"], int)  # cast, not the raw expression


def test_scoped_override_is_a_no_op_when_the_record_is_never_cloned():
    """A "scoped" override for a resource this instance never references via
    $ has nothing to apply to -- no clone, no problem."""
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("work", "DELAY", distribution="Constant", dist_params={"value": 1.0}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "work"), link("work", "out")],
        parameters=[{"name": "servers", "default": 1}],
    )
    inner["scoped"] = {"resources": {"Machine": {"capacity": "servers"}}}
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("cell", "SUBMODEL", definition="Cell", arguments={"servers": 3}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": inner},
    ))
    assert flat["resources"] == []


def test_a_failure_on_a_template_resource_is_cloned_per_instance():
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="$Machine"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "out")],
    )
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("a", "SUBMODEL", definition="Cell", arguments={}),
         block("b", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "a"), link("a", "b"), link("b", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Machine", "capacity": 1}],
        failures=[{"name": "breakdown", "resources": ["Machine"],
                   "uptime": {"distribution": "Constant", "dist_params": {"value": 100.0}},
                   "downtime": {"distribution": "Constant", "dist_params": {"value": 10.0}}}],
    ))
    failures = {f["name"]: f for f in flat["failures"]}
    assert failures["breakdown"]["resources"] == ["Machine"]           # the template, untouched
    assert failures["a/breakdown"]["resources"] == ["a/Machine"]       # each instance's own clone
    assert failures["b/breakdown"]["resources"] == ["b/Machine"]
    # each clone's rng stream is distinct: deterministic_hash(name + resource) differs
    assert len({failures["a/breakdown"]["name"] + "a/Machine",
                failures["b/breakdown"]["name"] + "b/Machine"}) == 2


def test_a_dotted_scoped_override_reaches_a_nested_failure_field():
    """PHASE4 B5: a per-instance MTBF/MTTR. The field NAME is dotted so only
    the NUMBER inside the duration spec is an expression -- the distribution's
    name stays a literal, which a whole-spec override could not express."""
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="$Machine"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "out")],
        parameters=[{"name": "mtbf", "default": 0.0}],
    )
    inner["scoped"] = {"failures": {"breakdown": {
        "uptime.dist_params.rate": "1.0 / mtbf if mtbf > 0 else 1e-9"}}}
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("a", "SUBMODEL", definition="Cell", arguments={"mtbf": 50.0}),
         block("b", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "a"), link("a", "b"), link("b", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Machine", "capacity": 1}],
        failures=[{"name": "breakdown", "resources": ["Machine"],
                   "uptime": {"distribution": "Exponential", "dist_params": {"rate": 0.001}},
                   "downtime": {"distribution": "Exponential", "dist_params": {"rate": 0.1}}}],
    ))
    failures = {f["name"]: f for f in flat["failures"]}
    assert failures["a/breakdown"]["uptime"] == {
        "distribution": "Exponential", "dist_params": {"rate": 0.02}}
    assert failures["b/breakdown"]["uptime"]["dist_params"]["rate"] == 1e-9   # mtbf 0: never
    assert failures["breakdown"]["uptime"]["dist_params"]["rate"] == 0.001    # template untouched


def test_add_definition_scoped_round_trips_through_save_and_load(tmp_path):
    doc = ModelDocument()
    doc.add_definition("Cell", parameters=[{"name": "servers", "default": 1}],
                       scoped={"resources": {"Machine": {"capacity": "servers"}}})
    path = tmp_path / "m.json"
    doc.save(path)
    again = ModelDocument.load(path)
    assert again.submodels["Cell"]["scoped"] == {"resources": {"Machine": {"capacity": "servers"}}}
    assert "scoped" not in doc.to_layout()["submodels"].get("Nope", {})  # sanity: only when set


# ---------------------------------------------------------------- parameters
def _parametrised(parameters, arguments, expr="rate", definitions=None):
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("work", "DELAY", distribution="Exponential", dist_params={"rate": expr}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "work"), link("work", "out")],
        parameters=parameters,
    )
    return layout(
        [block("create_1", "CREATE"),
         block("cell", "SUBMODEL", definition="Cell", arguments=arguments),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {**(definitions or {}), "Cell": inner},
    )


def test_arguments_merge_over_defaults_and_land_in_block_locals():
    flat = submodels.flatten(_parametrised(
        [{"name": "rate", "default": 1.0}, {"name": "servers", "default": 2}],
        {"rate": 3.5}))
    work = next(b for b in flat["blocks"] if b["name"] == "cell/work")
    assert work["locals"] == {"rate": 3.5, "servers": 2}


def test_a_string_argument_is_an_expression_evaluated_once_at_flatten_time():
    flat = submodels.flatten(_parametrised([{"name": "rate", "default": 1.0}],
                                           {"rate": "2 * 3 + 1"}))
    work = next(b for b in flat["blocks"] if b["name"] == "cell/work")
    assert work["locals"] == {"rate": 7}


def test_a_nested_argument_expression_is_evaluated_in_the_enclosing_locals():
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("work", "DELAY", distribution="Exponential", dist_params={"rate": "rate"}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "work"), link("work", "out")],
        parameters=[{"name": "rate", "default": 1.0}],
    )
    outer = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("b", "SUBMODEL", definition="Inner", arguments={"rate": "speed * 2"}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "b"), link("b", "out")],
        parameters=[{"name": "speed", "default": 1.0}],
    )
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("a", "SUBMODEL", definition="Outer", arguments={"speed": 5}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "a"), link("a", "dispose_1")],
        {"Outer": outer, "Inner": inner},
    ))
    work = next(b for b in flat["blocks"] if b["name"] == "a/b/work")
    assert work["locals"] == {"rate": 10}       # 5 * 2, in a's scope, not Inner's


def test_a_block_local_shadows_a_model_variable_of_the_same_name():
    """Resolution order: entity, TNOW, true/false, block locals, model variables."""
    from nagasim.core.expressions import ExpressionScope, compile_expression

    class _Ctx:
        variables = {"rate": 99}
        now = 0.0

    expression = compile_expression("rate")
    assert expression.evaluate(ExpressionScope(ctx=_Ctx())) == 99
    assert expression.evaluate(ExpressionScope(ctx=_Ctx(), locals={"rate": 2})) == 2


def test_a_parametrised_definition_runs_with_each_instance_own_argument():
    """Two instances of one definition, two different delays: the only thing
    that can produce different KPIs is the per-instance `locals`."""
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("work", "DELAY", distribution="Constant", dist_params={"value": "d"}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "work"), link("work", "out")],
        parameters=[{"name": "d", "default": 1.0}],
    )
    model = layout(
        [block("create_1", "CREATE", e_type="DEFAULT", distribution="Constant",
               dist_params={"value": 100.0}, entities_per_arrival=1, max_arrivals=1,
               start_time=0),
         block("a", "SUBMODEL", definition="Cell", arguments={"d": 3.0}),
         block("b", "SUBMODEL", definition="Cell", arguments={"d": 4.0}),
         block("dispose_1", "DISPOSE", distribution="Constant", dist_params={"value": 0.0})],
        [link("create_1", "a"), link("a", "b"), link("b", "dispose_1")],
        {"Cell": inner},
    )
    results = api.run_model(model, replications=1, seed=0, run_length=200)
    system = results["system"]
    throughput = float(system.loc[system["Metric"] == "Avg Throughput Time", "Value"].iloc[0])
    assert throughput == pytest.approx(7.0)     # 3 + 4


# ------------------------------------------------- structural arguments (D59)
def test_a_structural_argument_names_a_resource_and_sizes_a_batch():
    """The two things an expression argument could never reach (D59): a record
    NAME and an INTEGER param."""
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("take", "SEIZE", distribution="Constant", dist_params={"value": 0.0},
               priority=0, quantity=1, resource_name={"$arg": "pool"}),
         block("group", "BATCH", batch_size={"$arg": "size"}, batched_entity_type="BATCH"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "take"), link("take", "group"), link("group", "out")],
        parameters=[{"name": "pool", "type": "name", "refers_to": "resource",
                     "default": "Operators"},
                    {"name": "size", "type": "int", "default": 2}],
    )
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("cell", "SUBMODEL", definition="Cell",
               arguments={"pool": "Packers", "size": 5}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Operators", "capacity": 1}, {"name": "Packers", "capacity": 3}]))
    params = {b["name"]: b["params"] for b in flat["blocks"]}
    assert params["cell/take"]["resource_name"] == "Packers"
    assert params["cell/group"]["batch_size"] == 5
    assert isinstance(params["cell/group"]["batch_size"], int)
    # The literal name is NOT compiled as an expression, and it still reaches
    # the expression scope like any other argument.
    assert next(b for b in flat["blocks"] if b["name"] == "cell/take")["locals"]["pool"] == "Packers"


def test_a_structural_argument_passes_down_into_a_nested_instance():
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("take", "SEIZE", distribution="Constant", dist_params={"value": 0.0},
               priority=0, quantity=1, resource_name={"$arg": "pool"}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "take"), link("take", "out")],
        parameters=[{"name": "pool", "type": "name", "default": "Operators"}])
    outer = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("b", "SUBMODEL", definition="Inner", arguments={"pool": {"$arg": "staff"}}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "b"), link("b", "out")],
        parameters=[{"name": "staff", "type": "name", "default": "Operators"}])
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("a", "SUBMODEL", definition="Outer", arguments={"staff": "Packers"}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "a"), link("a", "dispose_1")],
        {"Outer": outer, "Inner": inner},
        resources=[{"name": "Packers", "capacity": 1}]))
    take = next(b for b in flat["blocks"] if b["name"] == "a/b/take")
    assert take["params"]["resource_name"] == "Packers"


def test_a_scoped_override_takes_a_structural_argument_as_a_capacity():
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="$Machine"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "out")],
        parameters=[{"name": "servers", "type": "int", "default": 1}])
    inner["scoped"] = {"resources": {"Machine": {"capacity": {"$arg": "servers"}}}}
    flat = submodels.flatten(layout(
        [block("create_1", "CREATE"),
         block("cell", "SUBMODEL", definition="Cell", arguments={"servers": 4}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Machine", "capacity": 1}]))
    assert {r["name"]: r["capacity"] for r in flat["resources"]}["cell/Machine"] == 4


@pytest.mark.parametrize("code,parameters,arguments", [
    ("E_SUBMODEL_ARG_UNKNOWN", [{"name": "size", "type": "int", "default": 2}], {"size": 2}),
    ("E_SUBMODEL_ARG_TYPE", [{"name": "pool", "type": "int", "default": 2}], {"pool": 2}),
    ("E_SUBMODEL_ARG_REFERENCE", [{"name": "pool", "type": "name", "refers_to": "resource",
                                   "default": "Operators"}], {"pool": "Ghost"}),
], ids=["unknown parameter", "wrong type", "dangling reference"])
def test_a_bad_structural_argument_is_reported_under_its_own_code(code, parameters, arguments):
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("take", "SEIZE", distribution="Constant", dist_params={"value": 0.0},
               priority=0, quantity=1, resource_name={"$arg": "pool"}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "take"), link("take", "out")],
        parameters=parameters)
    model = layout(
        [block("create_1", "CREATE"),
         block("cell", "SUBMODEL", definition="Cell", arguments=arguments),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Operators", "capacity": 1}])
    found = [p for p in schema.validate_model_structured(model) if p.code == code]
    assert found, [p.code for p in schema.validate_model_structured(model)]
    # The message points at the instance, not at the definition's own block.
    assert "cell" in found[0].message or "Cell" in found[0].message


# ---------------------------------------------------------------- validation
def _problems(model):
    return schema.validate_model(model)


def test_unknown_definition_is_reported_with_the_instance_name():
    problems = _problems(layout(
        [block("create_1", "CREATE"), block("cell", "SUBMODEL", definition="Nope", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": PASS_THROUGH}))
    assert any("'cell'" in p and "unknown submodel definition 'Nope'" in p for p in problems)


def test_extra_and_missing_arguments_are_reported():
    parametrised = definition(
        PASS_THROUGH["blocks"], PASS_THROUGH["links"],
        parameters=[{"name": "rate"}])       # no "default" -> required
    problems = _problems(layout(
        [block("create_1", "CREATE"),
         block("cell", "SUBMODEL", definition="Cell", arguments={"nope": 1}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": parametrised}))
    assert any("unknown argument 'nope'" in p and "'cell'" in p for p in problems)
    assert any("missing argument 'rate'" in p and "'cell'" in p for p in problems)


def test_duplicate_port_names_are_reported_per_side():
    clashing = definition(
        [block("in1", "SUB_ENTRY", port="in"), block("in2", "SUB_ENTRY", port="in"),
         block("work", "DELAY"), block("out", "SUB_EXIT", port="out")],
        [link("in1", "work"), link("in2", "work"), link("work", "out")])
    problems = _problems(layout([block("create_1", "CREATE")], [], {"Cell": clashing}))
    assert any("submodel 'Cell': duplicate in port name 'in'" in p for p in problems)


def test_a_link_naming_an_unknown_port_is_reported():
    problems = _problems(layout(
        [block("create_1", "CREATE"), block("cell", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell", target_port="nope"), link("cell", "dispose_1")],
        {"Cell": PASS_THROUGH}))
    assert any("unknown in port 'nope'" in p and "create_1->cell" in p for p in problems)


def test_a_definition_with_no_exit_must_end_in_dispose():
    sink = definition(
        [block("in", "SUB_ENTRY", port="in"), block("work", "DELAY")],
        [link("in", "work")])
    problems = _problems(layout([block("create_1", "CREATE")], [], {"Sink": sink}))
    assert any("submodel 'Sink': block 'work' is a dead end" in p for p in problems)

    ok = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("d", "DISPOSE", distribution="Constant", dist_params={"value": 0.0})],
        [link("in", "d")])
    assert not any("dead end" in p for p in _problems(
        layout([block("create_1", "CREATE")], [], {"Sink": ok})))


def test_a_definition_cycle_is_reported():
    a = definition([block("in", "SUB_ENTRY", port="in"),
                    block("b", "SUBMODEL", definition="B", arguments={}),
                    block("out", "SUB_EXIT", port="out")],
                   [link("in", "b"), link("b", "out")])
    b = definition([block("in", "SUB_ENTRY", port="in"),
                    block("a", "SUBMODEL", definition="A", arguments={}),
                    block("out", "SUB_EXIT", port="out")],
                   [link("in", "a"), link("a", "out")])
    problems = _problems(layout([block("create_1", "CREATE")], [], {"A": a, "B": b}))
    assert any(p.startswith("submodel definition cycle:") for p in problems)


def test_a_port_block_at_the_top_level_is_reported():
    problems = _problems(layout([block("stray", "SUB_ENTRY", port="in")], []))
    assert any("stray: SUB_ENTRY is only allowed inside a submodel definition" in p
               for p in problems)


def test_a_scoped_name_without_a_template_is_reported():
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 1.0}, priority=0, quantity=1, resource_name="$Ghost"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "out")])
    problems = _problems(layout([block("create_1", "CREATE")], [], {"Cell": inner}))
    assert any("'$Ghost' has no top-level template" in p and "submodel 'Cell'" in p
               for p in problems)


def test_problems_inside_a_definition_use_the_flattened_block_name():
    broken = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("work", "DELAY", distribution="NotADistribution", dist_params={}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "work"), link("work", "out")])
    problems = _problems(layout(
        [block("create_1", "CREATE"), block("cell", "SUBMODEL", definition="Cell", arguments={}),
         block("dispose_1", "DISPOSE")],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": broken}))
    assert any("cell/work: unknown distribution 'NotADistribution'" in p for p in problems)


# ---------------------------------------------------------------- document API
def _linear_document() -> ModelDocument:
    """create -> delay -> process(Server) -> dispose, fully deterministic."""
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0), name="create_1")
    doc.set_params("create_1", {"e_type": "DEFAULT", "distribution": "Constant",
                                "dist_params": {"value": 1.0}, "entities_per_arrival": 1,
                                "max_arrivals": 20, "start_time": 0})
    doc.add_block("DELAY", (200, 0), name="delay_1")
    doc.set_params("delay_1", {"distribution": "Constant", "dist_params": {"value": 2.0}})
    doc.add_block("PROCESS", (400, 0), name="process_1")
    doc.set_params("process_1", {"action": "Seize Delay Release", "distribution": "Constant",
                                 "dist_params": {"value": 1.0}, "priority": 0, "quantity": 1,
                                 "resource_name": "Server"})
    doc.add_block("DISPOSE", (600, 0), name="dispose_1")
    doc.set_params("dispose_1", {"distribution": "Constant", "dist_params": {"value": 0.0}})
    for src, tgt in [("create_1", "delay_1"), ("delay_1", "process_1"),
                     ("process_1", "dispose_1")]:
        doc.add_link(src, tgt)
    doc.set_resources([{"name": "Server", "capacity": 1, "discipline": "FIFO"}])
    return doc


def test_add_rename_and_remove_a_definition():
    doc = ModelDocument()
    doc.add_definition("Cell", parameters=[{"name": "rate", "default": 1.0}])
    assert list(doc.submodels) == ["Cell"]

    view = doc.open_definition("Cell")
    view.add_block("DELAY", (0, 0), name="work")
    assert list(view.blocks) == ["work"]
    assert doc.blocks == {}            # the model's own canvas is untouched

    doc.rename_definition("Cell", "Station")
    assert list(doc.submodels) == ["Station"]
    assert list(doc.open_definition("Station").blocks) == ["work"]

    doc.remove_definition("Station")
    assert doc.submodels == {}


def test_remove_definition_is_refused_while_an_instance_exists():
    doc = ModelDocument()
    doc.add_definition("Cell")
    doc.add_block("SUBMODEL", (0, 0), name="cell_1")
    doc.set_params("cell_1", {"definition": "Cell", "arguments": {}})
    with pytest.raises(ValueError, match="still instantiated"):
        doc.remove_definition("Cell")
    assert doc.instances_of("Cell") == [("", "cell_1")]


def test_editing_a_definition_is_one_undo_step_on_the_document_stack():
    doc = ModelDocument()
    doc.add_definition("Cell")
    view = doc.open_definition("Cell")
    view.add_block("DELAY", (0, 0), name="work")
    assert list(view.blocks) == ["work"]
    doc.undo()
    assert list(doc.open_definition("Cell").blocks) == []
    doc.undo()
    assert doc.submodels == {}


def test_definition_content_round_trips_through_the_layout(tmp_path):
    doc = _linear_document()
    doc.create_submodel_from_selection(["delay_1", "process_1"], "Cell")
    path = tmp_path / "m.json"
    doc.save(path)
    again = ModelDocument.load(path)
    assert again.to_layout() == fill_units(doc.to_layout())    # save fills "base" (D159)
    assert list(again.submodels) == ["Cell"]


def test_create_submodel_from_selection_turns_boundary_links_into_ports():
    doc = _linear_document()
    instance = doc.create_submodel_from_selection(["delay_1", "process_1"], "Cell")

    assert doc.blocks[instance].params["definition"] == "Cell"
    assert "delay_1" not in doc.blocks
    assert doc.definition_ports("Cell", "in") == ["create_1"]
    assert doc.definition_ports("Cell", "out") == ["dispose_1"]
    assert doc.link_ports("create_1", instance) == {"target_port": "create_1"}
    assert doc.link_ports(instance, "dispose_1") == {"source_port": "dispose_1"}
    assert doc.problems() == []


def test_create_submodel_from_selection_is_one_undo_step():
    doc = _linear_document()
    before = doc.to_layout()
    doc.create_submodel_from_selection(["delay_1", "process_1"], "Cell")
    doc.undo()
    assert doc.to_layout() == before


def test_create_then_expand_keeps_the_model_behaviour():
    original = _linear_document()
    baseline = api.run_model(original.to_layout(), replications=1, seed=0, run_length=200)

    doc = _linear_document()
    instance = doc.create_submodel_from_selection(["delay_1", "process_1"], "Cell")
    grouped = api.run_model(doc.to_layout(), replications=1, seed=0, run_length=200)

    report = doc.expand_instance(instance)
    assert report["blocks"] == [f"{instance}/delay_1", f"{instance}/process_1"]
    assert doc.problems() == []
    expanded = api.run_model(doc.to_layout(), replications=1, seed=0, run_length=200)

    def kpis(results):
        system = results["system"]
        return {m: v for m, v in zip(system["Metric"], system["Value"]) if m != "Run"}

    assert kpis(grouped) == kpis(baseline)
    assert kpis(expanded) == kpis(baseline)


def test_expand_instance_substitutes_a_bare_parameter_and_keeps_the_rest_as_variables():
    doc = ModelDocument()
    doc.add_definition("Cell", parameters=[{"name": "rate", "default": 2.0}])
    view = doc.open_definition("Cell")
    view.add_block("SUB_ENTRY", (0, 0), name="in")
    view.set_params("in", {"port": "in"})
    view.add_block("DELAY", (200, 0), name="plain")
    view.set_params("plain", {"distribution": "Exponential", "dist_params": {"rate": "rate"}})
    view.add_block("DELAY", (400, 0), name="derived")
    view.set_params("derived", {"distribution": "Exponential",
                                "dist_params": {"rate": "rate * 2"}})
    view.add_block("SUB_EXIT", (600, 0), name="out")
    view.set_params("out", {"port": "out"})
    for src, tgt in [("in", "plain"), ("plain", "derived"), ("derived", "out")]:
        view.add_link(src, tgt)

    doc.add_block("SUBMODEL", (0, 0), name="cell_1")
    doc.set_params("cell_1", {"definition": "Cell", "arguments": {"rate": 5.0}})
    report = doc.expand_instance("cell_1")

    assert report["substituted"] == {"rate": 5.0}
    assert report["variables"] == {"cell_1_rate": 5.0}
    assert doc.blocks["cell_1/plain"].params["dist_params"]["rate"] == 5.0
    assert doc.blocks["cell_1/derived"].params["dist_params"]["rate"] == "cell_1_rate * 2"
    assert {"name": "cell_1_rate", "initial": 5.0} in doc.variables


def test_create_submodel_from_selection_refuses_an_ambiguous_boundary():
    doc = _linear_document()
    doc.add_block("DELAY", (200, 200), name="delay_2")
    doc.set_params("delay_2", {"distribution": "Constant", "dist_params": {"value": 1.0}})
    doc.add_link("create_1", "delay_2")
    with pytest.raises(ValueError, match="several links into the selection"):
        doc.create_submodel_from_selection(["delay_1", "delay_2"], "Cell")


# ---------------------------------------------------------------- override paths
def test_override_paths_reach_instance_arguments_and_definition_blocks():
    base = schema.migrate(json.loads(TWO_CELL.read_text()))
    paths = dict(experiments.list_override_paths(base))

    assert paths["blocks.cell_1.params.arguments.rate"] == 1.2
    assert paths["submodels.Cell.blocks.machine.params.resource_name"] == "$Machine"

    changed = experiments.apply_overrides(base, [
        {"path": "blocks.cell_1.params.arguments.rate", "value": 9.0},
        {"path": "submodels.Cell.blocks.rework.params.probs", "value": [0.2, 0.8]},
    ])
    instance = next(b for b in changed["blocks"] if b["name"] == "cell_1")
    assert instance["params"]["arguments"]["rate"] == 9.0
    rework = next(b for b in changed["submodels"]["Cell"]["blocks"] if b["name"] == "rework")
    assert rework["params"]["probs"] == [0.2, 0.8]


def test_an_unknown_definition_in_an_override_path_is_refused():
    base = schema.migrate(json.loads(TWO_CELL.read_text()))
    with pytest.raises(ModelValidationError, match="no submodel definition named"):
        experiments.apply_overrides(base, [{"path": "submodels.Nope.blocks.x.params.y",
                                            "value": 1}])


# ---------------------------------------------------------------- playback view
def _event(t, kind, block, target=None, entity_id=1):
    return TraceEvent(t=t, kind=kind, entity_id=entity_id, e_type="DEFAULT",
                      block=block, target=target)


EVENTS = [
    _event(0.0, "entity_created", "create_1"),
    _event(0.0, "entity_moved", "create_1", "cell_1/machine"),
    _event(1.0, "entity_moved", "cell_1/machine", "cell_1/rework"),
    _event(2.0, "entity_moved", "cell_1/rework", "cell_2/machine"),
    _event(3.0, "entity_moved", "cell_2/machine", "dispose_1"),
    _event(3.0, "entity_disposed", "dispose_1"),
]


def test_visible_name_applies_the_prefix_rule():
    assert visible_name("cell_1/drill") == "cell_1"
    assert visible_name("create_1") == "create_1"
    assert visible_name("cell_1/drill", "cell_1") == "drill"
    assert visible_name("cell_1/a/b", "cell_1") == "a"
    assert visible_name("cell_2/drill", "cell_1") is None


def test_top_level_playback_counts_entities_on_the_instance_block():
    timeline = Timeline(EVENTS)
    assert timeline.state_at(0.5)["counts"] == {"cell_1": 1}
    # the move from cell_1/machine to cell_1/rework is invisible from outside
    assert timeline.state_at(1.5)["counts"] == {"cell_1": 1}
    assert timeline.state_at(2.5)["counts"] == {"cell_2": 1}
    assert [m[2:4] for m in timeline.moves_between(0.5, 2.5)] == [("cell_1", "cell_2")]


def test_opening_an_instance_shows_its_own_inner_blocks():
    timeline = Timeline(EVENTS, view="cell_1")
    assert timeline.state_at(0.5)["counts"] == {"machine": 1}
    assert timeline.state_at(1.5)["counts"] == {"rework": 1}
    assert timeline.state_at(2.5)["counts"] == {}       # gone to the other instance

    other = Timeline(EVENTS, view="cell_2")
    assert other.state_at(1.5)["counts"] == {}
    assert other.state_at(2.5)["counts"] == {"machine": 1}


# ---------------------------------------------------------------- equivalence
# The queueing THEORY of a submodelled line is already proven elsewhere: the
# Jackson job-shop case study (tests/test_case_study.py) is the routing/rework
# oracle and tests/test_oracles.py owns the M/M/1 and M/M/c ones. What is left
# for submodels to prove is that flattening produces exactly the flat model a
# user would have drawn by hand -- and that is an EXACT statement, so it is
# tested exactly, on a short run, instead of by a 30-second replication study.
def _flat_two_cell_line():
    """two_cell_line.json written out by hand, with no submodels at all: the
    `$Machine` template cloned per instance, the rework loop re-wired per
    instance, and the definition's `rate` parameter substituted literally."""
    def machine(instance, rate):
        return block(f"{instance}/machine", "PROCESS", action="Seize Delay Release",
                     distribution="Exponential", dist_params={"rate": rate},
                     time_unit="minutes", priority=0, quantity=1,
                     resource_name=f"{instance}/Machine")

    def rework(instance):
        return block(f"{instance}/rework", "DECIDE", mode="Chance",
                     probs=[0.1, 0.9], conditions=[])

    return layout(
        [block("create_1", "CREATE", e_type="DEFAULT", arrival_mode="Interarrival",
               schedule_name="", distribution="Exponential", dist_params={"rate": 0.8},
               time_unit="minutes", entities_per_arrival=1, max_arrivals=0, start_time=0),
         block("record_exit", "RECORD", stat_name="time_in_system", kind="tally",
               expr="TNOW - entity.creation_time"),
         block("dispose_1", "DISPOSE", distribution="Constant", dist_params={"value": 0.0}),
         machine("cell_1", 1.2), rework("cell_1"),
         machine("cell_2", 1.5), rework("cell_2")],
        [link("create_1", "cell_1/machine"), link("record_exit", "dispose_1"),
         link("cell_1/machine", "cell_1/rework"), link("cell_1/rework", "cell_1/machine"),
         link("cell_1/rework", "cell_2/machine"),
         link("cell_2/machine", "cell_2/rework"), link("cell_2/rework", "cell_2/machine"),
         link("cell_2/rework", "record_exit")],
        resources=[{"name": "Machine", "capacity": 1, "discipline": "FIFO"},
                   {"name": "cell_1/Machine", "capacity": 1, "discipline": "FIFO"},
                   {"name": "cell_2/Machine", "capacity": 1, "discipline": "FIFO"}],
        entities=[{"name": "DEFAULT", "shape": "circle", "color": "BLACK"}],
    )


def test_two_cell_line_runs_exactly_like_the_same_line_drawn_flat():
    """DEVLOG: the submodelled two_cell_line.json and the hand-written flat
    model above must be the SAME model -- same block names (so the same RNG
    streams), same per-instance `$Machine` clones, same rework wiring. Every
    KPI frame therefore has to agree element for element, which a shared
    resource, a lost argument or a dropped instance prefix all break."""
    kwargs = dict(replications=2, seed=0, run_length=600, warmup=100)
    from_definition = api.run_model(str(TWO_CELL), **kwargs)
    by_hand = api.run_model(_flat_two_cell_line(), **kwargs)

    for frame in ("system", "resource_summary", "user_summary", "entity_summary"):
        assert from_definition[frame].equals(by_hand[frame]), f"{frame} differs"

    # and the run really exercised two separate machines, not one shared pool
    rows = from_definition["resource_summary"]
    busy = {name: rows[rows["Resource"] == name]["Usage % (snapshot)"].max()
            for name in ("cell_1/Machine", "cell_2/Machine")}
    assert all(0 < value < 100 for value in busy.values()), busy


def test_scoped_capacity_override_clones_a_real_two_server_pool():
    """A Cell(rate, servers) instance with servers=2: the scoped override must
    make `$Machine` an actual 2-server pool, not just double the service rate.
    Checked exactly -- the clone's capacity, then two entities arriving
    together and being served at the same time (an M/M/1 run twice as fast
    would queue the second one).
    """
    inner = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("machine", "PROCESS", action="Seize Delay Release", distribution="Constant",
               dist_params={"value": 10.0}, time_unit="base", priority=0, quantity=1,
               resource_name="$Machine"),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "machine"), link("machine", "out")],
        parameters=[{"name": "servers", "default": 1}],
    )
    inner["scoped"] = {"resources": {"Machine": {"capacity": "servers"}}}
    model = layout(
        [block("create_1", "CREATE", e_type="DEFAULT", arrival_mode="Interarrival",
               schedule_name="", distribution="Constant", dist_params={"value": 100.0},
               time_unit="base", entities_per_arrival=2, max_arrivals=2, start_time=0),
         block("cell", "SUBMODEL", definition="Cell", arguments={"servers": 2}),
         block("dispose_1", "DISPOSE", distribution="Constant", dist_params={"value": 0.0})],
        [link("create_1", "cell"), link("cell", "dispose_1")],
        {"Cell": inner},
        resources=[{"name": "Machine", "capacity": 1, "discipline": "FIFO"}],
    )

    flat = submodels.flatten(model)
    clone = next(r for r in flat["resources"] if r["name"] == "cell/Machine")
    assert clone["capacity"] == 2
    assert next(r for r in flat["resources"] if r["name"] == "Machine")["capacity"] == 1

    results = api.run_model(model, replications=1, seed=0, run_length=60, warmup=0)
    rows = results["resource_summary"]
    row = rows[rows["Resource"] == "cell/Machine"].iloc[0]
    assert float(row["Queue Length Max"]) == pytest.approx(0.0, abs=1e-9)
    assert float(row["Total Seizes"]) == 2


# ---------------------------------------------------------------- per-instance variables (D72)
GATE = definition(
    [block("in", "SUB_ENTRY", port="in"),
     block("count", "ASSIGN", assignments=[{"target": "$gate", "expr": 'VAR("$gate") + 1'}]),
     block("seen", "RECORD", stat_name="$seen", kind="tally", expr='VAR("$gate")'),
     block("out", "SUB_EXIT", port="out")],
    [link("in", "count"), link("count", "seen"), link("seen", "out")],
)


def _arrivals(name, count):
    return block(name, "CREATE", e_type="DEFAULT", arrival_mode="Interarrival",
                 schedule_name="", distribution="Constant", dist_params={"value": 1.0},
                 time_unit="base", entities_per_arrival=1, max_arrivals=count, start_time=0)


def test_a_scoped_variable_is_per_instance_and_nests(tmp_path):
    """ARCHITECTURE D72. A `$variable` clones per instance like a resource, and
    `VAR("$gate")` -- the only spelling that survives scoping, since
    `<instance>/gate` is not an identifier -- reads the instance's own copy.
    Two instances counting independently, plus one inside another definition:
    the three counters must not interfere, and the template must stay put."""
    pair = definition(
        [block("in", "SUB_ENTRY", port="in"),
         block("x", "SUBMODEL", definition="Gate", arguments={}),
         block("out", "SUB_EXIT", port="out")],
        [link("in", "x"), link("x", "out")],
    )
    model = layout(
        [_arrivals("create_a", 2), _arrivals("create_b", 5), _arrivals("create_p", 3),
         block("a", "SUBMODEL", definition="Gate", arguments={}),
         block("b", "SUBMODEL", definition="Gate", arguments={}),
         block("p", "SUBMODEL", definition="Pair", arguments={}),
         block("dispose_1", "DISPOSE", distribution="Constant", dist_params={"value": 0.0})],
        [link("create_a", "a"), link("create_b", "b"), link("create_p", "p"),
         link("a", "dispose_1"), link("b", "dispose_1"), link("p", "dispose_1")],
        {"Gate": GATE, "Pair": pair},
        variables=[{"name": "gate", "initial": 0.0}],
    )

    flat = submodels.flatten(model)
    assert [v["name"] for v in flat["variables"]] == ["gate", "a/gate", "b/gate", "p/x/gate"]
    assert schema.validate_model(model) == []

    results = api.run_model(model, replications=1, seed=0, run_length=20, warmup=0)
    rows = results["user_summary"].set_index("Statistic")["Value"]
    # tally of the running count: 2 entities give (1+2)/2, 5 give 3, 3 give 2
    assert float(rows["a/seen"]) == pytest.approx(1.5)
    assert float(rows["b/seen"]) == pytest.approx(3.0)
    assert float(rows["p/x/seen"]) == pytest.approx(2.0)


def test_var_of_an_undeclared_variable_is_reported():
    """The usual literal-name check: VAR("nope") with a name the model does not
    declare is a load-time problem, with its own code."""
    model = layout(
        [_arrivals("create_1", 1),
         block("seen", "RECORD", stat_name="seen", kind="tally", expr='VAR("nope")'),
         block("dispose_1", "DISPOSE", distribution="Constant", dist_params={"value": 0.0})],
        [link("create_1", "seen"), link("seen", "dispose_1")],
        variables=[{"name": "gate", "initial": 0.0}],
    )
    found = schema.validate_model_structured(model)
    assert [p.code for p in found] == ["E_VARIABLE_UNKNOWN"]
    assert "unknown variable 'nope'" in found[0].message


# ---------------------------------------------------------------- shared bindings (D73)
def test_two_instances_draw_from_one_shared_level_through_a_name_parameter():
    """ARCHITECTURE D73. A flow endpoint set from a `name` PARAMETER is a
    BINDING, never a clone trigger: two feeders on one store draw from the SAME
    level, which stays conserved and is not copied per instance."""
    feeder = definition([], [], parameters=[
        {"name": "store", "type": "name", "refers_to": "level", "default": "Store"}])
    feeder["scoped"] = {"flows": {"Draw": {"rate": "rate"}}}
    feeder["parameters"].append({"name": "rate", "default": 1.0})
    model = layout(
        [block("f1", "SUBMODEL", definition="Feeder", arguments={"store": "Store", "rate": 1.0}),
         block("f2", "SUBMODEL", definition="Feeder", arguments={"store": "Store", "rate": 2.0})],
        [], {"Feeder": feeder},
        levels=[{"name": "Store", "capacity": 100.0, "min": 0.0, "initial": 100.0}],
        flows=[{"name": "Draw", "from": {"$arg": "store"}, "rate": 1.0}],
    )

    flat = submodels.flatten(model)
    assert [lv["name"] for lv in flat["levels"]] == ["Store"]         # no per-instance copy
    assert sorted(f["name"] for f in flat["flows"]) == ["f1/Draw", "f2/Draw"]
    assert {f["from"] for f in flat["flows"]} == {"Store"}
    assert schema.validate_model(model) == []

    results = api.run_model(model, replications=1, seed=0, run_length=10, warmup=0)
    store = results["level_summary"].set_index("Level").loc["Store"]
    assert float(store["Total Out"]) == pytest.approx(30.0)          # 1/unit + 2/unit over 10
    assert float(store["Final"]) == pytest.approx(70.0)


BELT = definition(
    [block("in", "SUB_ENTRY", port="in"),
     block("belt", "CONVEYOR", length=10.0, speed=10.0, spacing=1.0, accumulating=True,
           exit_resource={"$arg": "stop"}, from_node="", to_node=""),
     block("out", "SUB_EXIT", port="out")],
    [link("in", "belt"), link("belt", "out")],
    parameters=[{"name": "stop", "type": "name", "refers_to": "resource", "default": ""}],
)
STATION = definition(
    [block("in", "SUB_ENTRY", port="in"),
     block("work", "PROCESS", action="Delay Release", distribution="Constant",
           dist_params={"value": 5.0}, time_unit="base", priority=0, quantity=1,
           resource_name="$Crew"),
     block("out", "SUB_EXIT", port="out")],
    [link("in", "work"), link("work", "out")],
)


def _belt_model(stop):
    return layout(
        [_arrivals("create_1", 6),
         block("belt", "SUBMODEL", definition="Belt", arguments={"stop": stop}),
         block("st", "SUBMODEL", definition="Station", arguments={}),
         block("dispose_1", "DISPOSE", distribution="Constant", dist_params={"value": 0.0})],
        [link("create_1", "belt"), link("belt", "st"), link("st", "dispose_1")],
        {"Belt": BELT, "Station": STATION},
        resources=[{"name": "Crew", "capacity": 1, "discipline": "FIFO"}],
    )


def test_a_conveyor_stops_against_another_instances_scoped_resource():
    """D73, the discrete half: an argument may name another instance's `$`
    clone, so the belt's end stop is the station's own crew. The reference is
    checked against the FLATTENED model, where `st/Crew` exists."""
    model = _belt_model("st/Crew")
    flat = submodels.flatten(model)
    belt = next(b for b in flat["blocks"] if b["name"] == "belt/belt")
    assert belt["params"]["exit_resource"] == "st/Crew"
    assert schema.validate_model(model) == []

    results = api.run_model(model, replications=1, seed=0, run_length=60, warmup=0)
    assert float(results["conveyor_summary"]["Blocked %"].iloc[0]) > 0.0


def test_an_argument_naming_no_record_at_all_is_still_reported():
    found = schema.validate_model_structured(_belt_model("st/Nope"))
    assert [p.code for p in found] == ["E_SUBMODEL_ARG_REFERENCE"]
    assert "no resource named 'st/Nope'" in found[0].message
    assert "st/Crew" in found[0].hint


# ---------------------------------------------------------------- PHASE6 C1: agents per instance
def _person(name, pop, beta, extra_entry=()):
    return {"name": name, "parameters": {"beta": beta, "gamma": 0.1},
            "states": [{"name": "S"}, {"name": "I", "entry": list(extra_entry)}, {"name": "R"}],
            "initial_state": "S", "attributes": [{"name": "x", "init": "0"}],
            "transitions": [
                {"from": "S", "to": "I", "rate": f'beta * COUNT("{pop}", "I") / COUNT("{pop}")'},
                {"from": "I", "to": "R", "timeout": "EXPO(1 / gamma)"},
                {"from": "R", "to": "R", "message": "hi"}]}


def _epi(beta, n):
    return block(f"epi{n}", "SUBMODEL", definition="Epi", arguments={"beta": beta, "N": n})


def _epi_layout(entry=(), def_blocks=(), def_links=()):
    return layout([_epi(0.3, 200), _epi(0.6, 150)], [], {"Epi": {
        "parameters": [{"name": "beta", "default": 0.25}, {"name": "N", "default": 100}],
        "blocks": list(def_blocks), "links": list(def_links),
        "scoped": {"agent_types": {"Person": {"parameters.beta": {"$arg": "beta"}}},
                   "populations": {"People": {"count": {"$arg": "N"}}}}}},
        agent_types=[_person("Person", "$People", 0.25, entry)],
        populations=[{"name": "People", "type": "Person", "count": 100, "max_count": 400,
                      "initial": [{"state": "I", "count": 5}]}],
        simulation={"time_limit": 60, "warmup": 0, "replications": 1})


def test_two_agent_instances_run_bit_identically_to_the_same_model_written_flat():
    """C1: each instance clones its population AND its type (templates consumed),
    the instance's arguments reach `count` and the type's parameters, and agent
    streams are keyed by the full scoped name -- so the flattened model equals a
    hand-written flat one with renamed records, bit for bit (the #123 pattern)."""
    flat = layout([], [], agent_types=[_person("epi200/Person", "epi200/People", 0.3),
                                       _person("epi150/Person", "epi150/People", 0.6)],
                  populations=[{"name": f"epi{n}/People", "type": f"epi{n}/Person", "count": n,
                                "max_count": 400, "initial": [{"state": "I", "count": 5}]}
                               for n in (200, 150)],
                  simulation={"time_limit": 60, "warmup": 0, "replications": 1})
    got = submodels.flatten(_epi_layout())
    assert [p["name"] for p in got["populations"]] == ["epi200/People", "epi150/People"]
    assert [t["name"] for t in got["agent_types"]] == ["epi200/Person", "epi150/Person"]
    frames = [api.run_model(m, seed=3)["agent_summary"] for m in (_epi_layout(), flat)]
    assert frames[0].equals(frames[1])
    finals = dict(zip(frames[0]["Agent State"], frames[0]["Final"]))
    assert finals["epi200/People/R"] != finals["epi150/People/R"]     # independent outbreaks


def test_count_agent_send_spawn_and_a_send_block_inside_an_instance_name_its_own_population():
    entry = [{"target": "self.x", "expr": 'AGENT("$People", 0).x + 1'},
             {"send": "hi", "to": {"population": "$People"}},
             {"spawn": "$People", "count": 0}]
    def_blocks = [block("src", "CREATE", e_type="DEFAULT", arrival_mode="Interarrival",
                        distribution="Constant", dist_params={"value": 5.0}, max_arrivals=2),
                  block("tell", "SEND", message="hi", to={"population": "$People"}),
                  block("out", "DISPOSE")]
    model = _epi_layout(entry, def_blocks, [link("src", "tell"), link("tell", "out")])
    flat = submodels.flatten(model)
    kind = next(t for t in flat["agent_types"] if t["name"] == "epi150/Person")
    text = json.dumps(kind)
    assert "$" not in text and "epi200" not in text
    assert 'COUNT(\\"epi150/People\\", \\"I\\")' in text and 'AGENT(\\"epi150/People\\", 0)' in text
    assert kind["states"][1]["entry"][1]["to"] == {"population": "epi150/People"}
    assert kind["states"][1]["entry"][2]["spawn"] == "epi150/People"
    assert kind["parameters"] == {"beta": 0.6, "gamma": 0.1}
    tell = next(b for b in flat["blocks"] if b["name"] == "epi150/tell")
    assert tell["params"]["to"] == {"population": "epi150/People"}
    assert [p for p in schema.validate_model_structured(model) if p.severity == "error"] == []
    api.run_model(model, seed=1)                  # every scoped name resolves at run time
