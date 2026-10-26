"""`--asked FILE` (D815): `naga run --player`, `naga scenarios --player` and `naga viewer` read a
recorded answers file (a study version's asked.json) through `netguard.read_caller_file`, keep
the allow-listed rows (`viewer._quoted`) and feed them to the page's trust card. Headless."""
import json

import pytest

from nagasim import api, experiments, netguard, viewer
from nagasim.experiments import BASELINE
from test_player import split
from test_tape import MODELS

MODEL = str(MODELS / "single_server.json")
INPUT = {"tool": "input_uncertainty", "measure": "level", "resamples_used": 4, "replications": 2,
         "inputs": ["C:/secret/times.csv"],
         "kpis": [{"kpi": "system/Avg WIP (Little)", "mean": 2.0, "input_interval": [1.5, 2.6],
                   "input_share": 0.5}]}
ABC = {"tool": "calibrate", "method": "abc", "samples": 50, "accepted": 5, "abc_tolerance": 0.2}


def _file(tmp_path, rows, name="asked.json"):
    path = tmp_path / name
    path.write_text(rows if isinstance(rows, str) else json.dumps(rows), encoding="utf-8")
    return path


def test_a_study_asked_file_is_read_through_the_allow_list(tmp_path):
    rows = viewer.read_asked(_file(tmp_path, [{"tool": "fit_distribution", "data": "x.csv"}, INPUT, ABC]))
    assert [r["tool"] for r in rows] == ["input_uncertainty", "calibrate"]
    assert "inputs" not in rows[0] and "secret" not in json.dumps(rows)


@pytest.mark.parametrize("text, says", [
    ("{not json", "not JSON"),
    ('{"rows": []}', "not a JSON list"),
    ("[1]", "row 0"),
    (json.dumps([ABC, {**INPUT, "resamples_used": "four"}]), "row 1"),
    (json.dumps([{**ABC, "accepted": True}]), "row 0"),
    (json.dumps([{**INPUT, "kpis": [{**INPUT["kpis"][0], "input_interval": [1, "x"]}]}]), "row 0"),
    (json.dumps([{**INPUT, "kpis": None}]), "row 0"),
])
def test_a_malformed_file_is_refused_by_code(tmp_path, text, says):
    with pytest.raises(netguard.GuardError) as refused:
        viewer.read_asked(_file(tmp_path, text))
    assert refused.value.code == "E_ASKED_MALFORMED" and says in refused.value.message


def test_a_missing_or_oversized_file_is_netguards_refusal(tmp_path, monkeypatch):
    with pytest.raises(netguard.GuardError) as missing:
        viewer.read_asked(tmp_path / "none.json")
    assert missing.value.code == "E_FILE_MISSING"
    monkeypatch.setattr(viewer, "ASKED_BYTES", 10)
    with pytest.raises(netguard.GuardError) as big:
        viewer.read_asked(_file(tmp_path, [ABC]))
    assert big.value.code == "E_FILE_REFUSED"


def test_a_malformed_row_from_a_tool_never_reaches_a_page():
    """The same check guards every page, not only a file: a string where the card formats a
    number would break its trust card."""
    assert viewer._quoted([{**ABC, "abc_tolerance": "0.2"}, ABC]) == [ABC]


def test_run_player_quotes_the_file_on_its_trust_card(tmp_path, capsys):
    page = tmp_path / "p.html"
    code = api.main([MODEL, "--replications", "1", "--run-length", "60", "--player", str(page),
                     "--asked", str(_file(tmp_path, [INPUT, ABC]))])
    assert code == 0
    card = split(page.read_text(encoding="ascii"))[1]["trust"][BASELINE]
    assert card["input_text"].startswith("level, 4 resamples x 2 replications")
    assert card["params_text"].startswith("5 of 50 samples accepted")
    assert "secret" not in page.read_text(encoding="ascii")


def test_run_refuses_asked_without_a_player_or_a_bad_file_before_running(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(api, "run_model", lambda *a, **k: pytest.fail("ran"))
    assert api.main([MODEL, "--asked", str(_file(tmp_path, [ABC]))]) == 2
    assert "--player" in capsys.readouterr().out
    assert api.main([MODEL, "--player", str(tmp_path / "p.html"),
                     "--asked", str(_file(tmp_path, "[1]", "bad.json"))]) == 2
    assert "E_ASKED_MALFORMED" in capsys.readouterr().out


def test_scenarios_player_gets_the_rows(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("nagasim.player.build_player", lambda *a, **k: seen.update(k))
    code = experiments.main([MODEL, "--replications", "1", "--run-length", "30",
                             "--player", str(tmp_path / "p.html"),
                             "--asked", str(_file(tmp_path, [ABC]))])
    assert code == 0 and seen["asked"] == [ABC]


def test_viewer_passes_the_rows_and_refuses_a_bad_file(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake(layout, path, **kwargs):
        seen.update(kwargs)
        return {"warnings": [], "bytes": 1, "estimate": 0.1}
    monkeypatch.setattr(viewer, "build_viewer", fake)
    out = str(tmp_path / "v.html")
    assert viewer.main([MODEL, "-o", out, "--asked", str(_file(tmp_path, [INPUT]))]) == 0
    assert seen["asked"][0]["kpis"][0]["input_share"] == 0.5 and "inputs" not in seen["asked"][0]
    seen.clear()
    assert viewer.main([MODEL, "-o", out, "--asked", str(tmp_path / "none.json")]) == 2
    assert "E_FILE_MISSING" in capsys.readouterr().err and not seen
