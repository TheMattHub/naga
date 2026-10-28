"""ModelDocument.save writes a temp file and swaps it in (D920)."""
import json

import pytest

from nagasim.core import document as document_module
from nagasim.core.document import ModelDocument


def test_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "model.json"
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0), name="arrivals")
    doc.save(path)
    before = path.read_bytes()

    doc.add_block("DISPOSE", (100, 0), name="leave")

    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(document_module.os, "replace", broken_replace)
    with pytest.raises(OSError):
        doc.save(path)

    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["model.json"]   # no temp left behind
    assert doc.dirty                                                 # still unsaved


def test_save_writes_the_whole_model(tmp_path):
    path = tmp_path / "model.json"
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0), name="arrivals")
    doc.save(path)
    blocks = json.loads(path.read_text(encoding="utf-8"))["blocks"]
    assert [b["name"] for b in blocks] == ["arrivals"]
    assert not doc.dirty and doc.path == path
    assert [p.name for p in tmp_path.iterdir()] == ["model.json"]
