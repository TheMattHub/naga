"""One link time field (conveyor plan A2 Task 6, D1391): an area link is a link with a `delay`
(any distribution) and a `capacity` > 0; schema 32 moves an old file's `transfer_time` into the
delay. The random-delay case asserts structure, never hand-derived draws: a spy on the hidden
link's draw and leave seams records each part's entry, draw and exit."""
import math

import pytest

from conftest import create, dispose, layout, run_env

from nagasim import agent_tools as T
from nagasim import api, experiments
from nagasim.core.arealink import AreaLink
from nagasim.core.schema import CURRENT_VERSION, migrate, validate_model_structured


def const(value, **unit):
    return {"distribution": "Constant", "dist_params": {"value": value}, **unit}


def sim(**extra):
    return {"time_limit": None, "warmup": 0, "replications": 1, **extra}


def one_link(version=31, **fields):
    doc = layout([create(entities_per_arrival=3, dist_params={"value": 1000.0}, start_time=0),
                  dispose()], [{"source": "create_1", "target": "dispose_1", **fields}],
                 simulation=sim(time_limit=100))
    doc["schema_version"] = version
    return doc


def exits(results):
    return sorted(results["entity_log"]["Exited At"].tolist())


# -- migration 31 -> 32 -----------------------------------------------------------------------
def test_an_old_transfer_time_becomes_a_constant_delay_in_its_unit():
    assert CURRENT_VERSION == 32
    link = migrate(one_link(transfer_time=10, transfer_time_unit="seconds", capacity=2))["links"][0]
    assert link == {"source": "create_1", "target": "dispose_1", "capacity": 2,
                    "delay": const(10, time_unit="seconds")}
    plain = migrate(one_link(transfer_time=4))["links"][0]
    assert plain["delay"]["dist_params"] == {"value": 4} and "transfer_time" not in plain
    zero = migrate(one_link(transfer_time=0, transfer_time_unit="minutes", capacity=1))["links"][0]
    assert zero == {"source": "create_1", "target": "dispose_1", "capacity": 1}  # still instant


def test_an_old_file_runs_as_it_did():
    """The T10 case written the v31 way: delay 10, capacity 2, three parts at 0: 10, 10, 20."""
    old = api.run_model(one_link(transfer_time=10, capacity=2), replications=1)
    assert exits(old) == [10.0, 10.0, 20.0]
    new = api.run_model(one_link(version=32, delay=const(10), capacity=2), replications=1)
    assert old["entity_log"].equals(new["entity_log"])


def test_a_submodel_definitions_old_link_migrates_too():
    doc = one_link()
    doc["submodels"] = {"Cell": {"parameters": [], "blocks": [], "links": [
        {"source": "a", "target": "b", "transfer_time": 2, "capacity": 1}]}}
    assert migrate(doc)["submodels"]["Cell"]["links"] == [
        {"source": "a", "target": "b", "capacity": 1, "delay": const(2)}]


def test_both_fields_keep_the_old_refusal_as_a_migration_problem():
    """An old link with a transfer_time AND a delay: the migration leaves both, and validation
    refuses the link (E_LINK_AREA, case migrate) instead of reading one of them."""
    doc = one_link(transfer_time=1, delay=const(5), capacity=1)
    assert migrate(doc)["links"][0]["transfer_time"] == 1
    found = [p for p in validate_model_structured(doc) if p.code == "E_LINK_AREA"]
    assert [p.fields["case"] for p in found] == ["migrate"]
    assert "schema 32" in found[0].message and found[0].path == "/links/0/transfer_time"
    with pytest.raises(Exception, match="transfer_time"):
        api.run_model(doc, replications=1)


def test_a_negative_old_transfer_time_stays_refused():
    """Final review M6: v31 refused transfer_time -1; migrated into Constant(-1) it ran as 0 with
    a log line. The migration leaves it, and validation refuses it as an old field."""
    doc = one_link(transfer_time=-1, capacity=1)
    assert migrate(doc)["links"][0]["transfer_time"] == -1
    found = [p for p in validate_model_structured(doc) if p.code == "E_LINK_AREA"]
    assert [p.fields["case"] for p in found] == ["migrate"]
    with pytest.raises(Exception, match="transfer_time"):
        api.run_model(doc, replications=1)


# -- a random delay -------------------------------------------------------------------------
def _spied(monkeypatch):
    """Per part: (entered, drawn delay) at the draw, the time it left the link at the leave."""
    drawn, left = {}, {}
    draw, leave = AreaLink._draw, AreaLink._left

    def spy_draw(self, entity):
        value = draw(self, entity)
        drawn[entity.id] = (self.env.now, value)
        return value

    def spy_left(self, entity, entered):
        left[entity.id] = self.env.now
        return leave(self, entity, entered)

    monkeypatch.setattr(AreaLink, "_draw", spy_draw)
    monkeypatch.setattr(AreaLink, "_left", spy_left)
    return drawn, left


def _random_doc(capacity=2):
    blocks = [create(max_arrivals=40, distribution="Exponential", dist_params={"rate": 0.4},
                     start_time=0), dispose()]
    links = [{"source": "create_1", "target": "dispose_1", "capacity": capacity,
              "delay": {"distribution": "Uniform", "dist_params": {"low": 2.0, "high": 9.0}}}]
    return layout(blocks, links, simulation=sim())


def test_a_random_delay_area_link_keeps_capacity_order_and_each_draw(monkeypatch):
    drawn, left = _spied(monkeypatch)
    env, _model = run_env(_random_doc(), 10_000)
    parts = list(env.ctx.all_entities)
    assert sorted(drawn) == sorted(left) == sorted(e.id for e in parts) and len(drawn) == 40
    # each part's time in the link is its own draw (the DISPOSE takes it at once)
    for eid, (entered, value) in drawn.items():
        assert math.isclose(left[eid] - entered, value, abs_tol=1e-9)
    assert len({round(value, 9) for _t, value in drawn.values()}) > 30      # really random
    # FIFO: parts enter in the order they arrived (creation order is id order here)
    by_entry = sorted(drawn, key=lambda eid: (drawn[eid][0], eid))
    assert by_entry == sorted(drawn)
    created = {e.id: e.creation_time for e in parts}
    assert any(drawn[eid][0] > created[eid] + 1e-9 for eid in drawn)       # some waited
    # never more than capacity parts inside; the link does fill up
    events = sorted([(t, 1) for t, _v in drawn.values()] + [(t, -1) for t in left.values()],
                    key=lambda e: (e[0], e[1]))       # a leave before an entry at one instant
    inside, most = 0, 0
    for _t, step in events:
        inside += step
        most = max(most, inside)
    assert most == 2
    row = api.run_model(_random_doc(), replications=1)["link_flow"].iloc[0]
    assert row["Max Occupancy"] <= row["Capacity"] == 2 and row["Carried"] == 40


def test_the_draws_follow_the_seed(monkeypatch):
    drawn, _left = _spied(monkeypatch)
    run_env(_random_doc(), 10_000, seed=3)
    first = dict(drawn)
    drawn.clear()
    run_env(_random_doc(), 10_000, seed=3)
    assert drawn == first
    drawn.clear()
    run_env(_random_doc(), 10_000, seed=4)
    assert drawn != first


# -- overrides and the agent tool ------------------------------------------------------------
def test_a_link_delay_override_reaches_an_area_link():
    doc = one_link(version=32, delay=const(10), capacity=2)
    faster = experiments.apply_overrides(doc, [
        {"path": "links.create_1->dispose_1.delay.dist_params.value", "value": 4}])
    assert exits(api.run_model(faster, replications=1)) == [4.0, 4.0, 8.0]


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def test_connect_takes_a_capacity_with_a_delay_and_no_transfer_time(ws):
    assert "error" not in T.call("new_model", name="m")
    for kind, name in (("CREATE", "src"), ("DISPOSE", "end")):
        assert "error" not in T.call("add_block", model="m", type=kind, name=name)
    assert "error" in T.call("connect", model="m", source="src", target="end", transfer_time=3)
    assert "error" not in T.call("connect", model="m", source="src", target="end",
                                 delay=const(3), capacity=2)
    link = T.call("get_model_json", model="m")["layout"]["links"][0]
    assert link["capacity"] == 2 and link["delay"]["dist_params"] == {"value": 3}
