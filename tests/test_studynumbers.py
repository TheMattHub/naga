"""naga study numbers FOLDER (nagasim/studynumbers.py, P9 D1172): tagged numbers against their sources."""
import json

from nagasim import studynumbers, studyrerun

WRITEUP = """# Report

Intro with 3 numbers we do not check: 2026.

## Results

1. Orders: 24 <!-- N1 batch=1 --> and 57 <!-- N1 batch=2 -->.
2. Pallet limit 30.0 <!-- N3 --> kg, machine utilisation 61.2% <!-- util -->, cycle time 2,345 <!-- big -->.
3. A wrong one: 31.0 <!-- N3 --> and an unknown id 7 <!-- N99 -->.
4. An untagged claim: 88.5 min, and `code 42` is not a claim.

### Detail

Still in Results: 12.

## Method

<!-- a plain comment is not a tag --> 300 is outside the checked sections.
"""


def _folder(tmp_path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "bounds.csv").write_text(
        "batch,orders,limit_kg,util\n1,24,30,0.61237\n2,57,25,0.705\n", encoding="utf-8")
    (tmp_path / "results" / "run.json").write_text(json.dumps({"runs": [{"cycle_time": 2345.4}]}),
                                                   encoding="utf-8")
    (tmp_path / "report.md").write_text(WRITEUP, encoding="utf-8")
    manifest = {
        "numbers": {
            "N1": {"file": "results/bounds.csv", "column": "orders"},
            "N3": {"file": "results/bounds.csv", "row": {"batch": "1"}, "column": "limit_kg"},
            "util": {"file": "results/bounds.csv", "row": 0, "column": "util", "scale": 100},
            "big": {"file": "results/run.json", "key": "runs.0.cycle_time", "format": ",.0f"},
        },
        "writeups": [{"file": "report.md", "sections": ["Results"]}],
    }
    (tmp_path / "reproduce.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_tagged_numbers_right_wrong_and_untagged(tmp_path):
    folder = _folder(tmp_path)
    res = studynumbers.check(folder, studyrerun.load_manifest(folder))
    got = {(r["id"], r["written"]): r for r in res["numbers"]}
    assert [got[k]["verdict"] for k in [("N1", "24"), ("N1", "57"), ("N3", "30.0"), ("util", "61.2"),
                                        ("big", "2,345")]] == ["ok"] * 5
    assert got[("N3", "31.0")]["verdict"] == "wrong" and got[("N3", "31.0")]["expected"] == "30.0"
    assert got[("N3", "31.0")]["where"] == "results/bounds.csv batch=1, column limit_kg"
    assert got[("N99", "7")]["verdict"] == "unresolved" and "N99" in got[("N99", "7")]["where"]
    assert [(u["number"], u["section"]) for u in res["untagged"]] == [("88.5", "Results"), ("12", "Results")]
    assert not studynumbers.passed(res)


def test_unresolved_sources_are_named(tmp_path):
    folder = _folder(tmp_path)
    m = studyrerun.load_manifest(folder)
    m["numbers"]["N1"]["file"] = "../elsewhere.csv"          # a source outside the folder is refused
    m["numbers"]["N3"]["row"] = {"batch": "9"}
    res = studynumbers.check(folder, m)
    by = {r["id"]: r for r in res["numbers"]}
    assert by["N1"]["verdict"] == "unresolved" and "outside" in by["N1"]["where"]
    assert by["N3"]["verdict"] == "unresolved" and "0 rows match" in by["N3"]["where"]


def test_cli_numbers(tmp_path, capsys):
    folder = _folder(tmp_path)
    assert studyrerun.main(["numbers", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "report.md:9 N3 31.0: wrong, source 30.0" in out and "untagged 88.5 in section 'Results'" in out
    (folder / "report.md").write_text("## Results\n\n24 <!-- N1 batch=1 -->\n", encoding="utf-8")
    assert studyrerun.main(["numbers", str(folder)]) == 0
    assert "1 of 1 tagged numbers match; 0 untagged" in capsys.readouterr().out
    assert studyrerun.main(["numbers", str(tmp_path / "none")]) == 2
