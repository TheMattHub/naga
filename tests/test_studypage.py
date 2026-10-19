"""The study control room page (nagasim.studypage)."""
import json
import re
import time

import pytest

from nagasim import study, studypage

_OK = [{"name": "c", "verdict": "passed", "why": "", "kind": "law"}]


def _layout(*names):
    return {"blocks": [{"name": n, "type": "Delay"} for n in names], "links": []}


def _digest(**kpis):
    return {"system": {k: {"mean": v} for k, v in (kpis or {"Throughput": 7.0}).items()}}


def _new(tmp_path, **keys):
    folder = tmp_path / "s"
    st = study.create(folder, "Should we add a crane?")
    st.update(keys)
    return folder, st


def _timeline(page):
    return page.split('id="timeline"')[1].split('id="assumptions"')[0]


def test_page_of_an_empty_study(tmp_path):
    folder, st = _new(tmp_path)
    page = studypage.render(folder, st)
    assert 'id="cover"' in page and "no model yet" in page
    assert "<svg" not in _timeline(page)


def test_page_shows_verdict_goals_and_versions(tmp_path):
    goals = [{"id": "g1", "text": "Throughput", "kpi": "Throughput", "low": 5, "high": 9}]
    folder, st = _new(tmp_path, goals=goals, target=[{"name": "crane"}])
    study.record_step(folder, st, _layout("a"), _digest(), _OK, "first", None)
    study.record_step(folder, st, _layout("a", "b"), _digest(Throughput=8.0), _OK, "second", None)
    page = studypage.render(folder)
    assert f'class="banner b-wait">{study.sufficiency(st)["label"]}' in page
    assert 'id="v001"' in page and 'id="v002"' in page and "revert to v001" in page
    assert _timeline(page).count("<svg") >= 3     # a flowchart per version and a KPI chart
    assert "Goals met: 1 of 1" in page and "none recorded" in page


def test_validation_level_is_the_strongest_passed(tmp_path):
    val = [{"level": "data", "passed": True}, {"level": "expert", "passed": True},
           {"level": "known_answer", "passed": False}]
    folder, st = _new(tmp_path, validation=val)
    assert "Validation level reached: expert" in studypage.render(folder, st)


def test_strings_are_escaped(tmp_path):
    evil = '<script>alert(1)</script> & "q"'
    folder, st = _new(tmp_path, assumptions=[{"text": evil, "source": evil}],
                      target=[{"name": evil}], decision=evil)
    study.record_step(folder, st, _layout("a"), _digest(), _OK, evil, None)
    page = studypage.render(folder, st)
    assert evil not in page and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;" in page
    m = re.search(r'<script type="application/json" id="study-json">(.*?)</script>', page, re.S)
    assert "</script" not in m.group(1)
    assert json.loads(m.group(1)) == st


def test_page_has_no_script_and_a_csp(tmp_path):
    folder, st = _new(tmp_path)
    study.record_step(folder, st, _layout("a"), _digest(), _OK, "n", None)
    page = studypage.render(folder, st)
    assert page.count("<script") == 1 and 'type="application/json"' in page
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "http://" not in page.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in page


def test_kpi_missing_in_some_versions_charts(tmp_path):
    goals = [{"id": "g1", "kpi": "Late", "low": 0}]
    folder, st = _new(tmp_path, goals=goals)
    study.record_step(folder, st, _layout("a"), _digest(), _OK, "one", None)
    study.record_step(folder, st, _layout("a", "b"), _digest(Throughput=7.0, Late=2.0), _OK, "two", None)
    assert "Late" in studypage.render(folder, st)


def test_page_is_ascii(tmp_path):
    folder, st = _new(tmp_path, decision="café — ok")
    study.record_step(folder, st, _layout("a"), _digest(), _OK, "naïve", None)
    studypage.render(folder, st).encode("ascii")


@pytest.mark.slow
def test_large_study_stays_small_and_fast(tmp_path):
    folder, st = _new(tmp_path, goals=[{"id": "g1", "kpi": "Throughput", "low": 1}])
    names = [f"b{i}" for i in range(200)]
    layout = _layout(*names)
    layout["links"] = [{"source": a, "target": b} for a, b in zip(names, names[1:])]
    for i in range(30):
        layout["blocks"][i]["type"] = f"T{i}"    # a change per version
        study.record_step(folder, st, json.loads(json.dumps(layout)), _digest(Throughput=i + 1.0),
                          _OK, f"n{i}", None)
    t0 = time.perf_counter()
    page = studypage.render(folder, st)
    took = time.perf_counter() - t0
    print(f"large page: {len(page) / 1e6:.2f} MB in {took:.2f} s")
    assert len(page) < 5_000_000 and took < 5


def test_page_shows_scenario_results_per_goal(tmp_path):
    goals = [{"id": "g1", "text": "Throughput", "kpi": "Throughput", "low": 5}]
    folder, st = _new(tmp_path, goals=goals)
    sc = {"Fast": {"kpis": {"Throughput": {"mean": 8.0, "ci_half_width": 0.5}}, "goals": {"g1": "met"}},
          "Slow": {"kpis": {"Throughput": {"mean": 3.0}}, "goals": {"g1": "not_met"}}}
    study.record_step(folder, st, _layout("a"), _digest(), _OK, "first", None, scenarios=sc)
    page = studypage.render(folder, st)
    tl = _timeline(page)
    assert "<th>Scenario</th>" in tl and "8 +- 0.5" in tl and "Slow" in tl and "not met" in tl
    cover = page.split('id="cover"')[1].split("</section>")[0]
    assert "Scenarios: Fast meets 1 of 1; Slow meets 0 of 1." in cover
