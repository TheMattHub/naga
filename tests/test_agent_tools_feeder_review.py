"""Fix round after the final review of the agent-tool-gaps plan (get_ports on a merge, the
merge hint, capacity without a delay, a feeder named as an existing block, the info hint)."""
import pytest

from nagasim import agent_tools as T


@pytest.fixture
def ws(tmp_path):
    T.use_workspace(T.Workspace(tmp_path))
    T.call("new_model", name="m")
    T.call("place_piece", model="m", name="T", segments=[{"straight": 10.0}])
    return tmp_path


def ports(name):
    out = T.call("get_ports", model="m", block=name)
    return {p["name"]: p for p in out["ports"]}


def test_get_ports_names_the_merge_on_the_feeder_side(ws):
    assert "error" not in T.call("place_feeder", model="m", name="F",
                                 segments=[{"straight": 2.5}], trunk="T", at=4.0)
    assert ports("F")["out"]["linked_to"] == ["T (merge at 4.0)"]
    # The trunk's "in" is the belt's entry: a merge does not join it.
    assert ports("T")["in"]["linked_to"] == []


def test_a_plain_link_still_reads_block_and_port(ws):
    T.call("place_piece", model="m", name="N", segments=[{"straight": 2.0}], snap_to="T.out")
    assert ports("T")["out"]["linked_to"] == ["N.in"]


def test_the_merge_hint_follows_the_case(ws):
    T.call("update_block", model="m", name="T", params={"zones": {"count": 2}})
    error = T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}],
                   trunk="T", at=4.0)["error"]
    assert error["code"] == "E_PLACE_MERGE" and "pick another at" not in error["hint"]
    T.call("update_block", model="m", name="T", params={"zones": {}})
    T.call("place_feeder", model="m", name="G", segments=[{"straight": 2.5}], trunk="T", at=4.0)
    error = T.call("place_feeder", model="m", name="F", segments=[{"straight": 2.5}],
                   trunk="T", at=4.0)["error"]
    assert error["code"] == "E_PLACE_MERGE" and "pick another at" in error["hint"]


def test_capacity_needs_a_delay(ws):
    before = T.call("get_model_json", model="m")["layout"]
    error = T.call("place_piece", model="m", name="P", segments=[{"straight": 2.0}],
                   snap_to="T.out", capacity=3)["error"]
    assert error["code"] == "E_PLACE_GAP" and "\n" not in error["message"]
    assert T.call("get_model_json", model="m")["layout"] == before


def test_a_feeder_named_as_a_block_is_refused_first(ws):
    before = T.call("get_model_json", model="m")["layout"]
    error = T.call("place_feeder", model="m", name="T", segments=[{"straight": 2.5}],
                   trunk="T", at=4.0)["error"]
    assert error["code"] == "E_PLACE_NAME" and "already exists" in error["message"]
    assert T.call("get_model_json", model="m")["layout"] == before


def test_a_non_text_info_value_gets_a_type_hint(ws):
    error = T.call("set_model_info", model="m", title=5)["error"]
    assert error["code"] == "E_INFO" and "shorter" not in error["hint"]
    error = T.call("set_model_info", model="m", title="x" * 2001)["error"]
    assert "shorter" in error["hint"]
