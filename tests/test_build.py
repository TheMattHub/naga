"""`nagasim.build` and `naga export --python` (D475, D476): a builder generated from the
registries, with no semantics of its own. THE test: for every bundled model, running its
exported code gives `naga fmt`'s form of it, to the byte."""
import ast
import inspect
import json
import sys
from pathlib import Path

import pytest

from nagasim import build as nb
from nagasim import modelfmt
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.distributions import DISTRIBUTION_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
MODELS = sorted((ROOT / "nagasim" / "models").glob("*.json"))


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


def test_a_distribution_object_is_the_files_pair_wherever_it_goes():
    """D477: one constructor per registry entry; `distribution=` takes the object and writes
    the pair, any other keyword (a failure's uptime, a link's delay) the pair's dict."""
    assert all(callable(getattr(nb, name)) for name in DISTRIBUTION_REGISTRY)
    m = nb.Model()
    m.add_resource(name="R")
    m.process("p", resource_name="R", distribution=nb.Exponential(rate=1.25))
    m.create("c", distribution="Triangular", dist_params={"low": 1, "high": 3, "mode": 2})
    assert m.layout["blocks"][0]["params"] == {"resource_name": "R", "distribution": "Exponential",
                                               "dist_params": {"rate": 1.25}}
    m.add_failure(name="f", resources=["R"], uptime=nb.Exponential(rate=0.01),
                  downtime=nb.Constant(value=5))
    m.link("c", "p", delay=nb.Uniform(low=1, high="2 * x"))
    assert m.layout["failures"][0]["downtime"] == {"distribution": "Constant",
                                                   "dist_params": {"value": 5}}
    assert m.layout["links"][0]["delay"] == {"distribution": "Uniform",
                                             "dist_params": {"low": 1, "high": "2 * x"}}
    assert nb.Poisson(lambda_=2).params == {"lambda": 2}     # a reserved word takes a "_"
    code = nb.export_python(m.to_dict())
    assert "distribution=nb.Exponential(rate=1.25)" in code
    # fmt keeps a duration's unit, so the pair is a key beside it there.
    assert "uptime={'distribution': nb.Exponential(rate=0.01), 'time_unit'" in code
    assert "dist_params" not in code
    assert _built(code).to_dict() == m.to_dict()


def test_a_wrong_distribution_parameter_or_both_forms_raise():
    with pytest.raises(TypeError, match="'ratee'.*'rate'"):
        nb.Exponential(ratee=1.0)
    with pytest.raises(TypeError):
        nb.Exponential(1.0)                 # by name only: a rate is never taken for a mean
    with pytest.raises(TypeError, match="dist_params"):
        nb.Model().create(distribution=nb.Exponential(rate=1.0), dist_params={"rate": 2.0})


def test_run_settings_are_model_keywords_and_the_dict_still_works():
    """D478: every key of `simulation` is a keyword of Model; export writes that form."""
    assert set(nb.SIMULATION) >= {"time_limit", "warmup", "replications"}
    assert not set(nb.SIMULATION) & nb._TOP          # no setting has a section's name
    by_keyword = nb.Model(time_limit=1000, warmup=100, replications=5)
    mixed = nb.Model(simulation={"time_limit": 1000, "warmup": 100}, replications=5)
    assert by_keyword.to_dict() == mixed.to_dict()
    assert by_keyword.layout["simulation"] == {"time_limit": 1000, "warmup": 100, "replications": 5}
    with pytest.raises(TypeError, match="time_limit"):
        nb.Model(time_limit=10, simulation={"time_limit": 20})
    assert "nb.Model(time_limit=1000, warmup=100, replications=5)" in nb.export_python(
        by_keyword.to_dict())


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


def test_the_stub_is_the_generators_and_names_every_method():
    """`build.pyi` is what `tools/gen_engine_docs.py` writes now (else: run it, commit)."""
    import subprocess

    # A fresh interpreter: a test that registers a plugin block in this one would change it.
    text = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); "
         "from tools.gen_engine_docs import build_stub; sys.stdout.write(build_stub())"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    stub = ROOT / "nagasim" / "build.pyi"
    assert stub.read_text(encoding="utf-8") == text, "run `python tools/gen_engine_docs.py`"
    names = {n.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.FunctionDef)}
    assert names >= ({t.lower() for t in BLOCK_REGISTRY} | set(DISTRIBUTION_REGISTRY)
                     | set(nb._RECORDS))
    assert all(f"    {key}: " in text for key in nb.SIMULATION)
    # D480: a unit keyword names the units the engine accepts, so an editor flags "secs".
    assert "setup_unit: Literal['base', 'milliseconds', 'seconds'," in text
    assert "speed_unit: Literal['base', 'mm/ms'," in text and "'m/s'" in text
    assert "        time_unit: Literal['milliseconds'," in text      # the model's own: no "base"
