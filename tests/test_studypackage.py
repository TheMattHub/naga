"""The study package: workbook, RO-Crate, the five files (nagasim.studypackage)."""
import json

import openpyxl
import pytest

from nagasim import study, studypackage

_OK = [{"name": "c", "verdict": "passed", "why": "", "kind": "law"}]


def _layout(*names):
    return {"blocks": [{"name": n, "type": "Delay"} for n in names], "links": []}


def _study(tmp_path):
    folder = tmp_path / "s"
    st = study.create(folder, "Should we add a crane?")
    st["goals"] = [{"id": "g1", "text": "Throughput", "kpi": "A", "low": 5}]
    st["assumptions"] = [{"id": "a1", "text": "Arrivals are Poisson", "source": "log"}]
    study.record_step(folder, st, _layout("a"), {"system": {"A": {"mean": 7.0}}}, _OK, "first", None)
    study.record_step(folder, st, _layout("a", "b"), {"system": {"A": {"mean": 8.0}, "B": {"mean": 1.0}}},
                      _OK, "second", None)
    return folder, st


def test_workbook_sheets_and_kpi_columns(tmp_path):
    folder, st = _study(tmp_path)
    studypackage.workbook(folder, st, tmp_path / "w.xlsx")
    wb = openpyxl.load_workbook(tmp_path / "w.xlsx")
    assert wb.sheetnames == ["Goals", "Versions", "Checklist", "Assumptions"]
    rows = list(wb["Versions"].values)
    assert rows[0][-2:] == ("A", "B") and rows[1][-1] is None and rows[2][-1] == 1.0
    assert list(wb["Goals"].values)[1][4:] == (8.0, "met")


def test_ro_crate_lists_files():
    crate = studypackage.ro_crate({"question": "q", "decision": "d"}, ["a.html", "b.xlsx"])
    ids = {e["@id"]: e for e in crate["@graph"]}
    assert crate["@context"] == "https://w3id.org/ro/crate/1.1/context"
    assert ids["./"]["name"] == "q" and ids["a.html"]["encodingFormat"] == "text/html"
    assert ids["b.xlsx"]["@type"] == "File"


def test_package_writes_five_files(tmp_path):
    folder, st = _study(tmp_path)
    names = studypackage.package(folder, st)
    assert len(names) == 5 and all((folder / n).is_file() for n in names)
    assert json.loads((folder / "package" / "model.json").read_text()) == _layout("a", "b")


def test_package_without_versions_is_a_coded_error(tmp_path):
    folder = tmp_path / "s"
    st = study.create(folder, "q")
    with pytest.raises(study.StudyError) as e:
        studypackage.package(folder, st)
    assert e.value.code == "E_STUDY_NO_VERSION"


def test_workbook_keeps_formula_like_text_as_text(tmp_path):
    from openpyxl import Workbook, load_workbook
    from nagasim.studypackage import _sheet
    wb = Workbook()
    _sheet(wb, "S", ["a"], [["=HYPERLINK(\"x\")"]])
    wb.save(tmp_path / "w.xlsx")
    cell = load_workbook(tmp_path / "w.xlsx")["S"]["A2"]
    assert cell.data_type == "s" and cell.value == '=HYPERLINK("x")'


def test_package_lists_writeup_and_slides_when_they_exist(tmp_path):
    folder, st = _study(tmp_path)
    assert not any("writeup" in f or "slides" in f for f in studypackage.package(folder, st))
    for n in ("writeup.html", "writeup.md", "slides.html"):
        (folder / "package" / n).write_text("x", encoding="utf-8")
    outputs = folder / "package" / "outputs.json"
    outputs.write_text(json.dumps({"writeup": "v001", "slides": "v001"}), encoding="utf-8")
    assert not any("writeup" in f or "slides" in f for f in studypackage.package(folder, st))   # built from v001, last is v002
    outputs.write_text(json.dumps({"writeup": "v002", "slides": "v002"}), encoding="utf-8")
    files = studypackage.package(folder, st)
    assert {"package/writeup.html", "package/writeup.md", "package/slides.html"} <= set(files)
    crate = json.loads((folder / "package" / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    ids = {e["@id"] for e in crate["@graph"]}
    assert {"writeup.html", "writeup.md", "slides.html"} <= ids


def test_page_lists_deliverables_and_sensitivity_table(tmp_path):
    from nagasim import studypage
    folder, st = _study(tmp_path)
    page = studypage.render(folder, st)
    assert "planned" not in page and 'data-deliverable="sensitivity"' in page and "data-empty" in page
    vdir = folder / "versions" / st["versions"][-1]["id"]
    (vdir / "sensitivity.json").write_text(json.dumps({
        "charts": {"A": "<svg onload=evil></svg>"}, "changes_answer": ["x"],
        "factors": [{"path": "x", "label": "Arrival rate", "low": 1, "high": 2,
                     "kpis": {"A": {"swing": 3.5, "flips": True}}}]}), encoding="utf-8")
    page = studypage.render(folder, st)
    assert 'id="sensitivity"' in page and "Arrival rate" in page and "evil" not in page


def test_1789_package_page_states_nothing_it_contradicts(nurse_study):
    """S4-03: the package holds the client page and the modeller's control room (ruling (b)); neither
    contradicts itself, and the client page speaks no modeller (D1238)."""
    import re

    folder, st = nurse_study
    (folder / "versions" / "v001" / "sensitivity.json").write_text(json.dumps({
        "changes_answer": [], "factors": [{"path": "x", "label": "Arrival rate", "low": 1, "high": 2,
                                           "kpis": {"Nurse/Share waiting over 15": {"swing": 0.1}}}]}),
        encoding="utf-8")
    step = json.loads((folder / "versions" / "v001" / "step.json").read_text(encoding="utf-8"))
    study.record_step(folder, st, _layout("a"), {"system": {"Avg WIP": {"mean": 3.0}},
                                                 "tails": {"Nurse/Share waiting over 15": {"mean": 0.276}}},
                      _OK, "second", None, scenarios=step["scenarios"])
    names = studypackage.package(folder, st)
    assert {"package/client.html", "package/control-room.html"} <= set(names)
    room = (folder / "package" / "control-room.html").read_text(encoding="utf-8")
    client = (folder / "package" / "client.html").read_text(encoding="utf-8")
    for page in (room, client):
        assert "Deliverable package: not made yet" not in page        # it is the package
        assert "not run yet" not in page                              # a sensitivity exists, for v001
        assert not re.search(r"\b\d\d:\d\d\b(?! UTC)", page)          # one time zone
    for word in ("-inf", "naga ", "system/", "Log Truncated", "revert to", "Avg WIP (Little)", "+- ",
                 "study-json", "changed resources", "UTC"):
        assert word not in client, word
    assert "at most 10%" in client and "5.4%" in client and "Baseline" in client
    assert "client.html" in client and "study.xlsx" in client      # what the package holds


def test_study_package_command_writes_only_inside_the_folder_it_names(nurse_study, tmp_path, capsys):
    """D1238, netguard review: `naga study package FOLDER --out DIR` writes the package's own file
    names inside DIR and nothing else; where to write is the person's, so the agent tool has no path."""
    from nagasim import agent_tools as T
    import nagasim.agent_tools.study  # noqa: F401  registers the tool
    from nagasim import studyrerun

    folder, _st = nurse_study
    out = tmp_path / "hand-over"
    before = {p for p in tmp_path.rglob("*")}
    assert studyrerun.main(["package", str(folder), "--out", str(out)]) == 0
    written = {p for p in tmp_path.rglob("*")} - before
    assert {p.name for p in written if p.parent == out} == {
        "client.html", "control-room.html", "study.xlsx", "model.json", "ro-crate-metadata.json"}
    assert all(p == out or p.parent == out for p in written)          # nothing beside or below it
    assert "client.html" in capsys.readouterr().out
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    assert studyrerun.main(["package", str(folder), "--out", str(tmp_path / "a-file")]) == 2
    assert set(T.TOOLS["study_package"].input_schema["properties"]) == {"study"}
