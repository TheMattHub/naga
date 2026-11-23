"""The item code runtime: hooks through LOGIC and the runtime, the trust gate at model build,
readable hook errors and replications that share nothing."""
from __future__ import annotations

import copy
import os

import pytest

from nagasim import api
from nagasim.core import itemlogic, schema
from nagasim.core.itemlogic import ItemCodeError, ItemCodeUntrusted
from nagasim.core.sim_runner import setup_run, step_until
from nagasim.core.trace import TraceRecorder
from nagasim.settings import itemtrust, trustwrite

ALTERNATE = ("class Gate:\n"
             "    def __init__(self):\n"
             "        self.n = 0\n"
             "    def on_entry(self, entity, item):\n"
             "        self.n += 1\n"
             "        return self.n % 2\n")


def block(name, type_name, **params):
    return {"name": name, "type": type_name, "position": [0, 0], "params": params}


def gate(source, *, hook="entry", **code):
    return {"parameters": [{"name": "limit", "default": 1}], "code": {"class": "Gate", "source": source, **code},
            "blocks": [block("in", "SUB_ENTRY", port="in"), block("route", "LOGIC", hook=hook),
                       block("a", "SUB_EXIT", port="a"), block("b", "SUB_EXIT", port="b")],
            "links": [{"source": "in", "target": "route"}, {"source": "route", "target": "a"},
                      {"source": "route", "target": "b"}]}


def model(source=ALTERNATE, *, instances=(("g1", 3),), arrivals=10, variables=(), extra_blocks=(),
          extra_links=(), time_limit=50, **code):
    blocks, links = [], []
    for name, limit in instances:
        blocks += [block(f"c_{name}", "CREATE", distribution="Constant", dist_params={"value": 1},
                         max_arrivals=arrivals),
                   block(name, "SUBMODEL", definition="Gate", arguments={"limit": limit}),
                   block(f"{name}_a", "DISPOSE"), block(f"{name}_b", "DISPOSE")]
        links += [{"source": f"c_{name}", "target": name},
                  {"source": name, "target": f"{name}_a", "source_port": "a"},
                  {"source": name, "target": f"{name}_b", "source_port": "b"}]
    return {"schema_version": schema.CURRENT_VERSION, "blocks": blocks + list(extra_blocks),
            "links": links + list(extra_links), "resources": [],
            "variables": [{"name": v, "initial": 0} for v in variables],
            "submodels": {"Gate": gate(source, **code)},
            "simulation": {"time_limit": time_limit, "warmup": 0, "replications": 1}}


def routed(results, run=None) -> dict:
    frame = results["material_flow_summary"]
    if run is not None:
        frame = frame[frame["Run"] == run]
    return {(r["From"], r["To"]): int(r["Entities"]) for _, r in frame.iterrows()}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An empty store of this test's own; `trust` stands in for the person's yes."""
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path))
    return lambda source: trustwrite.trust_source(source, label="test", by="test")


def final_variables(layout, until, subscribers=()):
    env, _model = setup_run(layout, seed=0, run_len=until, warmup=0, subscribers=subscribers)
    step_until(env, until)
    return env.ctx.variables


MARKED = ("import os\nos.environ['LOADED'] = '1'\nclass Gate:\n"
          "    def on_entry(self, entity, item):\n        return 0\n")


def test_untrusted_code_is_refused_before_anything_loads(store, monkeypatch):
    monkeypatch.delenv("LOADED", raising=False)
    with pytest.raises(ItemCodeUntrusted, match="Gate") as refused:
        api.run_model(model(MARKED), replications=1)
    assert "LOADED" not in os.environ
    assert refused.value.untrusted[0]["fingerprint"] == itemtrust.fingerprint(MARKED)
    assert itemtrust.short(itemtrust.fingerprint(MARKED)) in str(refused.value)


def test_trust_follows_the_source_not_a_claimed_fingerprint(store, monkeypatch):
    monkeypatch.delenv("LOADED", raising=False)
    trusted_fp = store(ALTERNATE)
    record = {"definition": "Gate", "class": "Gate", "source": MARKED, "fingerprint": trusted_fp,
              "timer": None, "watch": [], "signals": [], "locals": {}}
    with pytest.raises(ItemCodeUntrusted):
        itemlogic.build_items({"g1": record}, ctx=None)
    assert "LOADED" not in os.environ


def test_consent_variable_does_not_unlock_item_code(store, monkeypatch):
    monkeypatch.setenv("NAGASIM_ALLOW_AGENT_PYTHON", "1")
    with pytest.raises(ItemCodeUntrusted):
        api.run_model(model(), replications=1)


def test_an_unreadable_store_refuses_and_says_why(store, tmp_path):
    (tmp_path / "trusted_item_code.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ItemCodeUntrusted, match="does not read"):
        api.run_model(model(), replications=1)


def test_entry_hook_routes_and_keeps_state_across_entities(store):
    store(ALTERNATE)
    counts = routed(api.run_model(model(), replications=1))
    assert counts[("g1/route", "g1_a")] == 5 and counts[("g1/route", "g1_b")] == 5


def test_params_are_the_instance_arguments(store):
    source = ("class Gate:\n    def on_entry(self, entity, item):\n"
              "        return 0 if item.params['limit'] > 4 else 1\n")
    store(source)
    counts = routed(api.run_model(model(source, instances=(("g1", 3), ("g2", 5))), replications=1))
    assert counts.get(("g1/route", "g1_b")) == 10 and ("g1/route", "g1_a") not in counts
    assert counts.get(("g2/route", "g2_a")) == 10 and ("g2/route", "g2_b") not in counts
    writes = ("class Gate:\n    def on_entry(self, entity, item):\n"
              "        item.params['limit'] = 1\n")
    store(writes)
    with pytest.raises(ItemCodeError, match="TypeError"):
        api.run_model(model(writes), replications=1)


HOOKS = ("class Gate:\n"
         "    def on_entry(self, entity, item):\n"
         "        return 0\n"
         "    def on_timer(self, item):\n"
         "        item.set('ticks', item.get('ticks') + 1)\n"
         "        item.set('tick_sum', item.get('tick_sum') + item.now)\n"
         "    def on_state(self, variable, value, item):\n"
         "        item.set('states', item.get('states') + 1)\n"
         "        item.set('state_value', value)\n"
         "        item.set('x', value)\n"
         "    def on_signal(self, name, item):\n"
         "        item.set('signals', item.get('signals') + 1)\n"
         "        item.set('signal_at', item.now)\n")
HOOK_VARS = ("ticks", "tick_sum", "states", "state_value", "signals", "signal_at", "x")


def hooks_model():
    extra = [block("c_x", "CREATE", distribution="Constant", dist_params={"value": 5}, max_arrivals=1, start_time=5),
             block("set_x", "ASSIGN", assignments=[{"target": "x", "expr": "2"}]),
             block("d_x", "DISPOSE"),
             block("c_s", "CREATE", distribution="Constant", dist_params={"value": 7}, max_arrivals=1, start_time=7),
             block("fire", "SIGNAL", distribution="Constant", dist_params={"value": 0}, signal_name="go"),
             block("d_s", "DISPOSE")]
    links = [{"source": "c_x", "target": "set_x"}, {"source": "set_x", "target": "d_x"},
             {"source": "c_s", "target": "fire"}, {"source": "fire", "target": "d_s"}]
    return model(HOOKS, arrivals=1, variables=HOOK_VARS, extra_blocks=extra, extra_links=links, time_limit=35,
                 timer=10, watch=["x"], signals=["go"])


def test_timer_state_and_signal_hooks_fire(store):
    store(HOOKS)
    found = final_variables(hooks_model(), 35)
    assert found["ticks"] == 3 and found["tick_sum"] == 60          # at 10, 20, 30
    assert found["states"] == 1 and found["state_value"] == 2      # its own write of x does not recurse
    assert found["x"] == 2
    assert found["signals"] == 1 and found["signal_at"] == 7


def test_state_hook_fires_with_and_without_a_trace(store):
    store(HOOKS)
    plain = final_variables(hooks_model(), 35)["states"]
    traced = final_variables(hooks_model(), 35, subscribers=(TraceRecorder(),))["states"]
    assert plain == traced == 1


RAISES = "class Gate:\n    def on_entry(self, entity, item):\n        return 1 / 0\n"


def test_a_raising_hook_names_item_hook_and_line(store):
    store(RAISES)
    with pytest.raises(ItemCodeError) as failed:
        api.run_model(model(RAISES), replications=1)
    assert "item 'g1' Gate.on_entry, line 3: ZeroDivisionError" in str(failed.value)


def test_a_branch_out_of_range_is_named(store):
    source = "class Gate:\n    def on_entry(self, entity, item):\n        return 2\n"
    store(source)
    with pytest.raises(ItemCodeError) as failed:
        api.run_model(model(source), replications=1)
    assert "returned 2" in str(failed.value) and "2 outputs" in str(failed.value)


def test_a_bool_branch_is_refused(store):
    source = "class Gate:\n    def on_entry(self, entity, item):\n        return True\n"
    store(source)
    with pytest.raises(ItemCodeError, match="returned True"):
        api.run_model(model(source), replications=1)


def test_class_state_does_not_cross_replications(store):
    source = ("class Gate:\n    total = 0\n    def on_entry(self, entity, item):\n"
              "        Gate.total += 1\n        return 0 if Gate.total <= 3 else 1\n")
    store(source)
    results = api.run_model(model(source), replications=2, seed=0)
    assert routed(results, 0) == routed(results, 1)
    assert routed(results, 0)[("g1/route", "g1_a")] == 3


def test_exit_hook_runs_then_goes_on_by_the_first_link(store):
    source = ("class Gate:\n    def on_exit(self, entity, item):\n"
              "        item.set('seen', item.get('seen') + 1)\n")
    store(source)
    layout = model(source, hook="exit", variables=("seen",))
    assert final_variables(layout, 50)["seen"] == 10
    assert routed(api.run_model(layout, replications=1)) [("g1/route", "g1_a")] == 10


def test_custom_logic_logic_block_unchanged():
    layout = model()
    del layout["submodels"]
    layout["blocks"] = [block("c", "CREATE", distribution="Constant", dist_params={"value": 1}, max_arrivals=4),
                        block("pick", "LOGIC", logic_class="SampleLogic", hook=""),
                        block("d0", "DISPOSE"), block("d1", "DISPOSE")]
    layout["links"] = [{"source": "c", "target": "pick"}, {"source": "pick", "target": "d0"},
                       {"source": "pick", "target": "d1"}]
    assert routed(api.run_model(copy.deepcopy(layout), replications=1)) == {("c", "pick"): 4, ("pick", "d0"): 4}


def test_a_raising_timer_hook_keeps_its_text(store):
    source = ("class Gate:\n    def on_entry(self, entity, item):\n        return 0\n"
              "    def on_timer(self, item):\n        raise ValueError('jammed')\n")
    store(source)
    with pytest.raises(ItemCodeError, match=r"item 'g1' Gate.on_timer, line 5: ValueError: jammed"):
        api.run_model(model(source, timer=4), replications=1)


def test_a_session_runs_trusted_item_code(store):
    from nagasim.core.session import SimSession

    store(ALTERNATE)
    session = SimSession(model())
    assert session.finish()["system"].shape[0] > 0
