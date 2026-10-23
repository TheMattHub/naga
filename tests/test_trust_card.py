"""The trust card's three uncertainties (Phase 12 C2, D739): run noise, input data, parameters."""
import json

from nagasim import study, studypage, studywriteup, uncertainty
from nagasim.agent_tools import core

_OK = [{"name": "c", "verdict": "passed", "why": "", "kind": "law"}]
_INPUT = {"tool": "input_uncertainty", "measure": "level", "resamples_used": 40, "replications": 5,
          "kpis": [{"kpi": "system/Avg WIP", "mean": 2.0, "input_interval": [1.5, 2.6], "input_share": 0.62}]}
_ABC = {"tool": "calibrate", "method": "abc", "samples": 200, "accepted": 12, "abc_tolerance": 0.3}


def _study(tmp_path, asked=None):
    folder = tmp_path / "s"
    st = study.create(folder, "Is one server enough?")
    study.record_step(folder, st, {"blocks": [{"name": "a", "type": "Delay"}], "links": []},
                      {"system": {"Throughput": {"mean": 7.0}}}, _OK, "first", None)
    if asked is not None:
        vid = st["versions"][-1]["id"]
        (folder / "versions" / vid / "asked.json").write_text(json.dumps(asked), encoding="utf-8")
    return folder, st


def test_texts_say_not_measured_and_carry_their_basis():
    assert uncertainty.trust_texts(None) == {"input": "not measured", "params": "not measured"}
    both = uncertainty.trust_texts([_INPUT, _ABC, {"tool": "calibrate", "method": "grid"}])
    assert "40 resamples x 5 replications" in both["input"] and "62%" in both["input"]
    assert both["params"] == "12 of 200 samples accepted, tolerance 0.3"


def test_the_player_card_carries_both_texts_and_never_a_sum():
    from test_player import split
    from test_tape import recorded
    from nagasim import api, player
    from nagasim.experiments import BASELINE

    layout, recorder = recorded("single_server.json")
    results = api.run_model(layout, replications=2, run_length=200, warmup=0)
    _b, data = split(player.build_player(layout, {BASELINE: recorder}, results=results, asked=[_INPUT, _ABC]))
    card = data["trust"][BASELINE]
    assert card["input_text"].startswith("level, 40 resamples") and card["params_text"].startswith("12 of 200")
    _b, data = split(player.build_player(layout, {BASELINE: recorder}, results=results))
    assert data["trust"][BASELINE]["input_text"] == data["trust"][BASELINE]["params_text"] == "not measured"


def test_the_control_room_shows_three_rows_and_an_old_folder_renders(tmp_path):
    folder, st = _study(tmp_path)           # no asked.json at all
    rows = studypage.trust_rows(folder, st, {})
    assert [r[0] for r in rows] == ["Run noise", "Input data", "Parameters"]
    assert rows[1][1] == rows[2][1] == "not measured"
    assert 'id="trust"' in studypage.render(folder, st)

    folder2, st2 = _study(tmp_path / "b", [dict(_INPUT, kpis=[dict(_INPUT["kpis"][0], kpi="a<b>&")]), _ABC])
    page = studypage.render(folder2, st2)
    assert "a&lt;b&gt;&amp;" in page and "a<b>" not in page and "12 of 200 samples accepted" in page


def test_the_deck_facts_carry_the_same_rows(tmp_path):
    folder, st = _study(tmp_path, [_INPUT, _ABC])
    f = studywriteup.facts(folder, st)
    assert f["uncertainties"] == studypage.trust_rows(folder, st, {})
    titles = [t for t, _blocks in studywriteup._slide_list(f)]
    assert "Validation and precision" in titles
    assert ["table", ["Uncertainty", "Basis"], f["uncertainties"]] in dict(studywriteup._slide_list(f))[
        "Validation and precision"]


def test_a_fresh_process_reads_the_kept_answers_for_a_study_slot(tmp_path, monkeypatch):
    kept = {"load": None, "asked": None, "recorded": lambda ws, name: [_INPUT]}
    monkeypatch.setitem(core.SLOTS, "zz:", kept)
    assert core.recorded("zz:x") == [_INPUT]
    monkeypatch.setitem(core.SLOTS, "zz:", {"recorded": lambda ws, name: 1 / 0})
    assert core.recorded("zz:x") == []              # a slot that cannot read falls back to memory
