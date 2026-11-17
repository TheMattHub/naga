"""Q2: the paired difference of an option against the Baseline (D1263, D1264), the steadier-KPI note
(D1265), the goal tables led by the option (D1252) and study_validate's progress (D1262)."""
import json
import math
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
from scipy import stats as sp

from nagasim import agent_tools as T
from nagasim import experiments, studypage, studywriteup
from nagasim.agent_tools import study as study_tools
from nagasim.core.stats import pitman_morgan
from nagasim.pagefmt import REFERENCE

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"
SHARE = "Nurse/Share waiting over 15"
STEADIER = "A statistical observation, not a measured cause."

# A tiny CRN sample: the option (x) moves with the Baseline (y) but swings less.
Y = [10.0, 12.0, 9.0, 14.0, 11.0, 13.0]
X = [8.0, 9.5, 7.0, 10.0, 8.5, 9.0]


# --- the statistics, against hand-computed values ---------------------------------------------------

def test_pitman_morgan_matches_two_hand_computed_forms():
    out = pitman_morgan(X, Y)
    x, y = np.array(X), np.array(Y)
    n = len(x)
    r = float(np.corrcoef(x - y, x + y)[0, 1])
    t = r * math.sqrt(n - 2) / math.sqrt(1 - r * r)
    assert out["n"] == n and out["df"] == n - 2
    assert out["r"] == pytest.approx(r) and out["t"] == pytest.approx(t)
    assert out["p"] == pytest.approx(2 * sp.t.sf(abs(t), n - 2))
    # The textbook form through F = var_x / var_y and r_xy gives the same t.
    f = x.var(ddof=1) / y.var(ddof=1)
    rxy = float(np.corrcoef(x, y)[0, 1])
    assert t == pytest.approx((f - 1) * math.sqrt(n - 2) / (2 * math.sqrt(f * (1 - rxy ** 2))))
    assert out["r"] < 0 and out["p"] < 0.05         # the option's variance is lower here


@pytest.mark.parametrize("a, b, why", [
    ([1.0, 2.0], [2.0, 3.0], "fewer than 3"),            # n < 3
    ([1.0, 2.0, 4.0], [1.0, 2.0, 4.0], "equal"),         # equal runs: x - y is constant
    ([5.0, 5.0, 5.0], [1.0, 2.0, 4.0], "constant"),      # one side constant: |r| = 1
])
def test_pitman_morgan_degenerate_cases_give_a_reason(a, b, why):
    out = pitman_morgan(a, b)
    assert out["p"] is None and why in out["why"]


def test_the_pair_is_the_first_n_common_seeds():
    base = {s: v for s, v in zip(range(6), Y)}
    scen = {s: v for s, v in zip(range(4), X)}      # the option stopped after 4 replications
    out = study_tools._paired(base, scen)
    d = np.array(X[:4]) - np.array(Y[:4])
    half = sp.t.ppf(0.975, 3) * d.std(ddof=1) / 2
    diff = out["diff_vs_baseline"]
    assert diff["n"] == 4 and diff["mean"] == pytest.approx(d.mean())
    assert (diff["lo"], diff["hi"]) == (pytest.approx(d.mean() - half), pytest.approx(d.mean() + half))
    spread = out["spread"]
    assert spread["sd"] == pytest.approx(np.std(X[:4], ddof=1))
    assert spread["base_sd"] == pytest.approx(np.std(Y[:4], ddof=1))
    assert spread["p"] == pytest.approx(pitman_morgan(X[:4], Y[:4])["p"])
    json.dumps(out, allow_nan=False)                 # stored as plain JSON numbers


def test_no_paired_interval_without_common_seeds():
    base = {s: v for s, v in zip(range(6), Y)}
    assert study_tools._paired(base, {s + 100: v for s, v in zip(range(6), X)}) is None   # other seeds
    assert study_tools._paired(base, {0: X[0]}) is None                                  # one replication
    assert study_tools._paired(base, {1: X[0], 2: X[1], 3: X[2]}) is None                # not the first n
    assert study_tools._paired(base, None) is None


# --- the step record, from a real two-scenario run ------------------------------------------------

@pytest.mark.slow
def test_step_record_stores_a_paired_difference(tmp_path):
    shutil.copy(MODELS / "single_server.json", tmp_path / "m.json")
    ws = T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Two servers?", size="quick")["study"]
    kpi = "Avg Throughput Time"
    # An absolute precision this loose is met by the first pass: 10 replications, a quick test.
    goal = {"id": "g1", "text": "time", "kpi": kpi, "high": 1000, "precision": 1000}
    T.call("study_update", study=sid, changes={"goals": [goal]})
    layout = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    layout["scenarios"] = [{"name": "two", "overrides": [{"path": "resources.Server.capacity", "value": 2}]}]
    r = T.call("study_step", study=sid, layout=layout, note="two servers", seed=3)
    assert r["kind"] == "version" and r["replications"] == 10, r
    entry = r["scenarios"]["two"]["kpis"][kpi]
    # Hand-computed from the per-replication values of both runs on seeds 3..12 (Run is the seed, not an index).
    run = experiments.run_scenarios(layout, ["two"], replications=10, seed=3, baseline=True)
    flat = {n: experiments.flatten_kpis(res) for n, res in run.results.items()}
    series = {n: f[f["KPI"] == f"system/{kpi}"].sort_values("Run")["Value"].to_numpy() for n, f in flat.items()}
    base, scen = series[experiments.BASELINE], series["two"]
    assert entry["mean"] == pytest.approx(scen.mean())          # the series is the stored KPI
    assert r["kpis"][kpi] == pytest.approx(base.mean())
    d = scen - base
    half = sp.t.ppf(0.975, 9) * d.std(ddof=1) / math.sqrt(10)
    diff = entry["diff_vs_baseline"]
    assert diff["n"] == 10 and diff["mean"] == pytest.approx(d.mean())
    assert diff["lo"] == pytest.approx(d.mean() - half) and diff["hi"] == pytest.approx(d.mean() + half)
    assert entry["spread"]["sd"] == pytest.approx(scen.std(ddof=1))
    assert entry["spread"]["base_sd"] == pytest.approx(base.std(ddof=1))
    stored = json.loads((tmp_path / sid / "versions" / "v001" / "step.json").read_text(encoding="utf-8"))
    assert stored["scenarios"]["two"]["kpis"][kpi]["diff_vs_baseline"] == diff


# --- the pages ------------------------------------------------------------------------------------

def _with_pair(nurse_study, diff=None, spread=None):
    folder, st = nurse_study
    e = st["versions"][-1]["scenarios"]["Fourth nurse"]["kpis"][SHARE]
    if diff is not None:
        e["diff_vs_baseline"] = diff
    if spread is not None:
        e["spread"] = spread
    return folder, st


PAIR = {"mean": -0.222, "lo": -0.264, "hi": -0.180, "n": 20}


def test_headline_gives_the_paired_interval(nurse_study):
    folder, st = _with_pair(nurse_study, PAIR)
    want = "a difference of -22.2 points (95% interval -26.4 to -18.0)"
    head = studywriteup._headline(studywriteup.facts(folder, st))
    assert want in head and "difference of the means" not in head
    md = studywriteup.writeup(folder, st)[1].read_text(encoding="utf-8")
    deck = studywriteup.slides(folder, st).read_text(encoding="utf-8")
    room = studypage.render(folder, st, study_id="s", in_room=True)
    client = studypage.render_client(folder, st, [])
    for name, text in (("write-up", md), ("deck", deck), ("room", room), ("client", client)):
        assert want in text, name
    # An interval that crosses zero says so.
    folder, st = _with_pair(nurse_study, {"mean": 0.01, "lo": -0.02, "hi": 0.04, "n": 20})
    head = studywriteup._headline(studywriteup.facts(folder, st))
    assert "no clear difference: +1.0 points (95% interval -2.0 to +4.0)" in head


def test_an_old_step_record_keeps_the_means_wording(nurse_study):
    folder, st = nurse_study
    head = studywriteup._headline(studywriteup.facts(folder, st))
    assert "a difference of the means of 22.2 points" in head and "95% interval -" not in head


STEADY = {"sd": 0.02, "base_sd": 0.05, "mean": 0.054, "base_mean": 0.276, "p": 0.001, "n": 20}


@pytest.mark.parametrize("spread, shown, proportion", [
    (STEADY, True, True),                                     # both hold; the CV did not fall (0.37 vs 0.18)
    ({**STEADY, "p": 0.2}, False, False),                     # (a) fails: not significant
    ({**STEADY, "sd": 0.04}, False, False),                   # (b) fails: 64% of the replications
    ({**STEADY, "base_mean": 0.054}, True, False),            # same mean: the spread itself fell
    ({**STEADY, "sd": 0.06, "base_sd": 0.05}, False, False),  # higher variance, significant: no note
    ({k: v for k, v in STEADY.items() if k != "p"}, False, False),   # the test could not run
])
def test_steadier_kpi_line_needs_the_paired_test_and_half_the_replications(nurse_study, spread, shown, proportion):
    folder, st = _with_pair(nurse_study, PAIR, spread)
    f = studywriteup.facts(folder, st)
    note = f["goals"][0]["option"]["steadier"]
    assert bool(note) is shown and ("in proportion to its lower mean" in note) is proportion
    if shown:
        k = round(100 * (spread["sd"] / spread["base_sd"]) ** 2)
        assert note.startswith("Run-to-run variation of Share waiting over 15 min was lower with Fourth nurse")
        assert f"with about {k}% of the replications" in note and note.endswith(STEADIER)
        assert "standard deviation 2 points against 5 points in the Baseline" in note
    md = studywriteup.writeup(folder, st)[1].read_text(encoding="utf-8")
    room = studypage.render(folder, st, study_id="s", in_room=True)
    client = studypage.render_client(folder, st, [])
    goals_panel = room[room.index('id="goals"'):room.index('id="model"')]
    client_goals = client[client.index("<h2>Goals</h2>"):client.index("How far to trust it")]
    for text in (md, goals_panel, client_goals):
        assert (STEADIER in text) is shown
    # Never in the headline or the deck.
    deck = studywriteup.slides(folder, st).read_text(encoding="utf-8")
    assert STEADIER not in deck and STEADIER not in studywriteup._headline(f)
    assert STEADIER not in room[:room.index('id="goals"')]


def test_the_steadier_note_is_escaped(nurse_study):
    folder, st = _with_pair(nurse_study, PAIR, STEADY)
    sc = st["versions"][-1]["scenarios"]
    sc['<b>x</b>'] = sc.pop("Fourth nurse")
    room = studypage.render(folder, st, study_id="s", in_room=True)
    client = studypage.render_client(folder, st, [])
    for text in (room, client):
        assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt;" in text


def test_writeup_and_room_goal_tables_lead_with_the_option(nurse_study):
    folder, st = nurse_study
    md = studywriteup.writeup(folder, st)[1].read_text(encoding="utf-8")
    assert f"| Goal | KPI | Target | Fourth nurse value | Status | {REFERENCE} value |" in md
    room = studypage.render(folder, st, study_id="s", in_room=True)
    panel = room[room.index('id="goals"'):room.index('id="model"')]
    heads = re.findall(r"<th>(.*?)</th>", re.sub(r"</?abbr[^>]*>", "", panel))     # without the hover glosses (D1324)
    assert heads[:6] == ["Goal", "KPI", "Target", "Fourth nurse value", "Status", f"{REFERENCE} value"], heads
    assert panel.index("5.4%") < panel.index("27.6%")
    # With no option recommended the tables show the Baseline as before.
    st["versions"][-1]["scenarios"] = {}
    shutil.rmtree(folder / "package", ignore_errors=True)
    md = studywriteup.writeup(folder, st)[1].read_text(encoding="utf-8")
    assert f"| Goal | KPI | Target | {REFERENCE} value | Status |" in md
    plain = studypage.render(folder, st, study_id="s", in_room=True)
    assert "<th>Last value</th>" in plain


# --- study_validate reports progress, so the dock's Stop reaches it (D1262) -----------------------

@pytest.mark.slow     # a 10-replication run: about 1 s alone, over 3 s under load
def test_study_validate_reports_progress(tmp_path):
    from nagasim import study as study_mod

    T.use_workspace(T.Workspace(tmp_path))
    sid = T.call("study_start", question="Formula?", size="quick")["study"]
    layout = json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))
    layout["simulation"].update(time_limit=20, warmup=0)    # the check runs 100x this: keep it short
    # A version recorded without a run: study_validate reads only its model.json.
    study_mod.record_step(tmp_path / sid, study_mod.load(tmp_path / sid), layout, {"system": {}},
                          [{"name": "c", "verdict": "passed", "why": "", "kind": "law"}], "first", None)
    seen = []
    with T.core.reporting(lambda done, total=None, message="": seen.append((done, total))):
        r = T.call("study_validate", study=sid)
    assert "error" not in r, r
    assert seen and seen[-1][0] == seen[-1][1]
