"""`naga fmt` (D471, D472): the shortest file that is the same model. The proof is
`migrate(fmt(m)) == migrate(m)`, which every reader (a run, validation, `naga diff`, the
model hash) builds on."""
import json
from pathlib import Path

import pytest

from nagasim import modelfmt, state
from nagasim.core.schema import migrate
from nagasim.modelschema import SCHEMA_ID

from conftest import chain, create, dispose, layout, process

MODELS = sorted((Path(__file__).resolve().parents[1] / "nagasim" / "models").glob("*.json"))


@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_a_bundled_model_formats_to_the_same_model_and_is_already_formatted(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    text = modelfmt.dumps(modelfmt.minimal(raw))
    short = json.loads(text)
    assert migrate(short) == migrate(raw)
    assert list(short)[:2] == ["$schema", "schema_version"] and short["$schema"] == SCHEMA_ID
    assert modelfmt.dumps(modelfmt.minimal(short)) == text            # idempotent
    assert path.read_text(encoding="utf-8") == text, "run `naga fmt` on it (D474)"


def test_defaults_units_and_empty_sections_are_left_out_and_the_rest_is_kept():
    c, p, d = create("c"), process("p", resource="R"), dispose("d")
    model = layout([c, p, d], chain(c, p, d), resources=[{"name": "R", "capacity": 2}],
                   simulation={"time_limit": 50, "time_unit": "hours"})
    model["blocks"][1]["position"] = [10, 20]
    short = modelfmt.minimal(model)
    assert {k for k in short} == {"$schema", "schema_version", "blocks", "links", "resources",
                                  "simulation"}
    assert short["simulation"] == {"time_limit": 50, "time_unit": "hours"}
    by_name = {b["name"]: b for b in short["blocks"]}
    assert by_name["p"]["position"] == [10, 20]                       # layout is kept
    assert "time_unit" not in by_name["p"]["params"]                  # "hours", the model's
    assert by_name["p"]["params"]["resource_name"] == "R"             # required: always kept
    assert "description" not in by_name["d"] and "params" not in by_name["d"]
    assert list(by_name["p"]) == ["name", "type", "position", "params"]
    assert migrate(short) == migrate(model)


@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_the_model_hash_does_not_depend_on_the_form(path):
    model = json.loads(path.read_text(encoding="utf-8"))
    assert state.model_hash(modelfmt.minimal(model)) == state.model_hash(model)


def test_fmt_rewrites_only_the_files_named_and_check_reports_them(tmp_path, capsys):
    c, d = create("c"), dispose("d")
    path = tmp_path / "m.json"
    path.write_text(json.dumps(layout([c, d], chain(c, d)), indent=2), encoding="utf-8")
    assert modelfmt.main(["--check", str(path)]) == 1
    assert "m.json: not formatted" in capsys.readouterr().out
    assert modelfmt.main([str(path)]) == 0
    assert modelfmt.main(["--check", str(path)]) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["m.json"]


def test_fmt_refuses_a_file_that_is_not_a_model(tmp_path):
    path = tmp_path / "lib.nagalib.json"
    text = json.dumps({"format_version": 1, "library": {"name": "x"}, "assets": []})
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="not a model file"):
        modelfmt.main([str(path)])
    assert path.read_text(encoding="utf-8") == text


def test_counter_offsets_follow_label_offset_in_a_blocks_key_order():
    c, d = create("c"), dispose("d")
    c["counter_offsets"] = {"out": [4, -6]}
    c["label_offset"] = [1, 2]
    out = json.loads(modelfmt.dumps(modelfmt.minimal(layout([c, d], chain(c, d)))))
    keys = list(out["blocks"][0])
    assert keys.index("label_offset") < keys.index("counter_offsets")
    assert out["blocks"][0]["counter_offsets"] == {"out": [4, -6]}
    assert "counter_offsets" in modelfmt.BLOCK_KEYS
