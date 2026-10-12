"""A bundled model by name (`nagasim/examples.py`): `naga examples`, and the fallback every
command's model argument has when nothing is at the path given."""
import json
import shutil

import pytest

from nagasim import cli, examples


def test_a_plain_name_with_no_file_there_is_the_bundled_model(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert examples.model_path("single_server") == str(examples.DIR / "single_server.json")
    assert cli.main(["validate", "single_server"]) == 0
    assert cli.main(["run", "single_server", "--replications", "1", "--run-length", "50"]) == 0
    assert "Total Entities Completed" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["../models/single_server", "single_server.json", "data/x",
                                   "..", "", "single server", "C:single_server", "no_such_model"])
def test_anything_that_is_not_a_plain_bundled_name_is_left_as_given(value, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert examples.model_path(value) == value
    with pytest.raises(ValueError, match="no bundled example"):
        examples.path(value)


def test_a_file_of_the_same_name_wins(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    model = json.loads((examples.DIR / "single_server.json").read_text(encoding="utf-8"))
    model["blocks"] = []                       # no longer a model that validates
    (tmp_path / "single_server").write_text(json.dumps(model), encoding="utf-8")
    assert examples.model_path("single_server") == "single_server"
    assert cli.main(["validate", "single_server"]) == 1
    shutil.rmtree(tmp_path / "single_server", ignore_errors=True)


def test_examples_lists_every_bundled_model_with_its_title(capsys):
    assert cli.main(["examples"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == examples.names() and len(lines) > 40
    assert any(line.startswith("single_server") and "M/M/1" in line for line in lines)
    assert cli.main(["examples", "--path"]) == 0
    assert capsys.readouterr().out.strip() == str(examples.DIR)
