"""`naga trust`: a person's typed trust of item code, at a terminal only, with no flag that
answers for them; and `naga run`'s one-line refusal of untrusted code."""
from __future__ import annotations

import json

import pytest

from nagasim import cli, trustcli
from nagasim.core import itemlogic
from nagasim.settings import itemtrust, trustwrite
from test_item_logic import ALTERNATE, model


@pytest.fixture
def model_file(tmp_path, monkeypatch):
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path / "user"))

    def write(source=ALTERNATE, placed=None, library=None):
        layout = model(source)
        definition = layout["submodels"]["Gate"]
        if placed is not None:
            definition["code"]["placed"] = placed
        if library is not None:
            definition["info"] = {"library": library}
        path = tmp_path / "m.json"
        path.write_text(json.dumps(layout), encoding="utf-8")
        return path
    return write


def run(path, answer="y", tty=True):
    lines = []

    def ask(question):
        lines.append(question)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    code = trustcli.trust_model(path, by="m", ask=ask, isatty=lambda: tty, out=lines.append)
    return code, lines


def test_shows_the_code_and_trusts_on_y(model_file):
    path = model_file(placed={"author": "Naga", "fingerprint": itemtrust.fingerprint(ALTERNATE)},
                      library={"library": "automation", "asset": "gate", "version": "2.2.0"})
    fp = itemtrust.fingerprint(ALTERNATE)
    code, lines = run(path)
    assert code == 0
    text = "\n".join(lines)
    assert "The model file says it comes from library automation 2.2.0 by Naga." in text
    assert itemtrust.short(fp) in text and "class Gate:" in text
    assert "does not sandbox" in text
    assert itemtrust.is_trusted(fp)
    assert [e.label for e in itemtrust.entries()] == ["Gate in Gate"]
    assert run(path)[0] == 0        # nothing left to ask


def test_anything_but_y_is_no(model_file):
    path = model_file()
    assert run(path, answer="sure")[0] == 1
    assert not itemtrust.is_trusted(itemtrust.fingerprint(ALTERNATE))


def test_end_of_input_is_no(model_file):
    code, lines = run(model_file(), answer=EOFError())
    assert code == 1 and not itemtrust.entries()
    assert "The model file names no library: it was written in this model." in lines


def test_piped_input_is_refused_before_asking(model_file):
    def ask(_question):
        pytest.fail("asked although the input is not a terminal")
    lines = []
    code = trustcli.trust_model(model_file(), by="m", ask=ask, isatty=lambda: False, out=lines.append)
    assert code == 1 and lines[0].startswith("E_TRUST_TERMINAL")
    assert not itemtrust.entries()


def test_there_is_no_yes_flag(model_file):
    for flag in ("--yes", "-y", "--y"):
        with pytest.raises(SystemExit) as refused:
            trustcli.main([flag, str(model_file())])
        assert refused.value.code == 2


def test_an_edited_copy_says_so(model_file):
    path = model_file(placed={"author": "Tester", "fingerprint": itemtrust.fingerprint(ALTERNATE + "# old\n")})
    _code, lines = run(path, answer="n")
    assert any("changed after it was placed" in line for line in lines)
    assert itemlogic.code_blocks(json.loads(path.read_text(encoding="utf-8")))[0]["edited"] is True


def test_list_and_remove(model_file, capsys):
    run(model_file())
    short = itemtrust.short(itemtrust.fingerprint(ALTERNATE))
    assert trustcli.main(["--list"]) == 0
    assert short in capsys.readouterr().out
    assert trustcli.main(["--remove", short]) == 0
    assert itemtrust.entries() == []
    assert trustcli.main(["--remove", short]) == 1
    assert trustcli.main(["--remove", "abc"]) == 1


def test_a_bundled_fingerprint_is_never_asked(model_file, monkeypatch):
    path = model_file()
    itemtrust.bundled_fingerprints.cache_clear()
    monkeypatch.setattr(itemtrust, "bundled_fingerprints", lambda: frozenset({itemtrust.fingerprint(ALTERNATE)}))
    code, lines = run(path, answer=EOFError())
    assert code == 0 and not any("Trust it?" in line for line in lines)


def test_naga_run_names_the_untrusted_item(model_file, capsys):
    path = model_file()
    assert cli.main(["run", str(path)]) == 1
    said = capsys.readouterr()
    text = said.out + said.err
    assert "not trusted on this computer" in text and "Gate" in text
    trustwrite.trust_source(ALTERNATE, label="t", by="t")
    assert cli.main(["run", str(path)]) == 0
