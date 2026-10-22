"""check_draft (D687): a generated model against the log it came from."""
import shutil

import pytest

from nagasim import agent_tools as T
from nagasim import discover


@pytest.fixture
def ws(tmp_path):
    shutil.copy(discover.sample_path("job_shop_log"), tmp_path / "log.csv")
    return T.use_workspace(T.Workspace(tmp_path))


def _draft(ws):
    assert T.call("import_event_log", path="log.csv", name="draft").get("ok")
    return ws.document("draft")


def test_discovered_draft_passes_against_its_log(ws):
    _draft(ws)
    out = T.call("check_draft", model="draft", source="log.csv")
    assert out["verdict"] in ("passed", "partial"), out
    assert all(c["within"] for c in out["compared"]), out["compared"]
    assert {c["kpi"] for c in out["compared"]} >= {"cycle_time", "throughput", "service:Cutting"}
    assert out["sample"]["source_cases"] > 400 and out["sample"]["draft_replications"] >= 3


def test_wrong_service_time_fails_against_its_log(ws):
    doc = _draft(ws)
    params = dict(doc.blocks["Drilling"].params)
    params["dist_params"] = {k: v * 3 for k, v in params["dist_params"].items()}
    doc.set_params("Drilling", params)
    out = T.call("check_draft", model="draft", source="log.csv")
    assert out["verdict"] == "failed", out
    assert not next(c for c in out["compared"] if c["kpi"] == "service:Drilling")["within"]


def test_short_log_says_not_distinguishable(ws, tmp_path):
    _draft(ws)
    lines = (tmp_path / "log.csv").read_text().splitlines()
    (tmp_path / "short.csv").write_text("\n".join(lines[:60]) + "\n")
    out = T.call("check_draft", model="draft", source="short.csv")
    assert out["verdict"] == "partial" and "not distinguishable" in out["note"], out


def test_no_source_is_an_answer_not_a_guess(ws):
    _draft(ws)
    assert T.call("check_draft", model="draft")["error"]["code"] == "E_NO_SOURCE"
