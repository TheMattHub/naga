"""`nagasim.build` and `naga export --python` (D475, D476): a builder generated from the
registries, with no semantics of its own. THE test: for every bundled model, running its
exported code gives `naga fmt`'s form of it, to the byte."""
import inspect
import json
from pathlib import Path

import pytest

from nagasim import build as nb
from nagasim import modelfmt
from nagasim.core.block_registry import BLOCK_REGISTRY

MODELS = sorted((Path(__file__).resolve().parents[1] / "nagasim" / "models").glob("*.json"))


def _built(code: str) -> nb.Model:
    namespace = {}
    exec(compile(code, "<export>", "exec"), namespace)
    return namespace["m"]


@pytest.mark.parametrize("path", MODELS, ids=lambda p: p.stem)
def test_build_of_export_is_fmt_for_every_bundled_model(path):
    assert (modelfmt.dumps(_built(nb.export_python(path)).to_dict())
            == modelfmt.dumps(modelfmt.minimal(path)))


def test_every_block_type_has_a_method_named_after_it_and_none_hides_one():
    m = nb.Model()
    for name, spec in BLOCK_REGISTRY.items():
        method = getattr(m, name.lower())
        assert method.__name__ == name.lower()
        assert set(inspect.signature(method).parameters) >= {p.name for p in spec.params}
        assert name.lower() not in vars(nb.Model) and name.lower() not in vars(nb.Level)
        assert name.lower() not in vars(m), name        # an attribute would hide the block


def test_names_links_and_a_wrong_keyword():
    m = nb.Model(simulation={"time_limit": 10})
    a, b = m.create(), m.create()
    assert (a.name, b.name) == ("create_1", "create_2")
    assert (a >> m.dispose("out")).name == "out"
    m.link(b, "out", delay={"distribution": "Constant", "dist_params": {"value": 1}})
    assert m.layout["links"][1]["delay"]["dist_params"] == {"value": 1}
    with pytest.raises(TypeError, match="ratee"):
        m.process(ratee=1)
    with pytest.raises(TypeError, match="blocks"):
        nb.Model(blocks=[])
    assert m.add_resource(name="R", capacity=2) == "R"


def test_a_link_across_levels_and_a_type_named_like_a_method_raise(monkeypatch):
    m = nb.Model()
    cell = m.definition("cell")
    with pytest.raises(ValueError, match="this level"):
        cell.sub_entry("in") >> m.process("p")
    monkeypatch.setitem(BLOCK_REGISTRY, "LINK", BLOCK_REGISTRY["DISPOSE"])
    with pytest.raises(TypeError, match="link"):
        nb.Model()


def test_save_writes_only_the_path_given_and_run_runs(tmp_path):
    m = nb.Model(simulation={"time_limit": 20, "replications": 1})
    m.add_resource(name="Server")
    m.create(dist_params={"value": 2.0}) >> m.process(resource_name="Server") >> m.dispose()
    path = m.save(tmp_path / "m.json")
    assert [p.name for p in tmp_path.iterdir()] == ["m.json"]
    assert json.loads(path.read_text(encoding="utf-8")) == m.to_dict()
    created = m.run(seed=0).kpis().set_index("metric").loc["system/Total Entities Created", "mean"]
    assert created == 11            # at 0, 2, ..., 20


def test_no_text_of_a_model_becomes_code():
    """Every value goes out through `repr`: a name with quotes, a newline and a call
    stays a string."""
    m = nb.Model()
    odd = "x'); import os; ('\n\"y"
    m.create(odd) >> m.dispose()
    m.add_flow(name=odd, rate="2", **{"from": "Tank"})
    code = nb.export_python(m.to_dict())
    assert "import os" not in code.replace(repr(odd), "")
    assert _built(code).to_dict() == m.to_dict()
    evil = m.to_dict()
    evil["blocks"][0]["type"] = "CREATE('x'); print('PWNED'); m.create"     # the one non-repr value
    with pytest.raises(ValueError, match="not a registered block type"):
        nb.export_python(evil)


def test_export_cli_writes_only_the_out_file(tmp_path, capsys):
    out = tmp_path / "mm1.py"
    assert nb.main(["single_server", "--python", "-o", str(out)]) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["mm1.py"]
    assert _built(out.read_text(encoding="utf-8")).to_dict() == modelfmt.minimal(MODELS[0].parent
                                                                                 / "single_server.json")
