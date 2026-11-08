"""The plugin results hook (E8, D1119): a plugin block's `ctx.record_kpi` and `ctx.add_artifact`
reach `Results["plugin_kpis"]` and `Results.artifacts`. The in-process test registers the block
for itself only (monkeypatch); the pool test loads a real plugin folder in a subprocess
(tests/test_plugins.py's isolation rule)."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from nagasim import api
from nagasim.core.block_registry import BLOCK_REGISTRY
from nagasim.core.export import ARTIFACTS_KEY, concat_runs

REPO_ROOT = Path(__file__).resolve().parents[1]

PLUGIN = '''
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec


class Tally(BaseProcess):
    """Passes entities on; records how many it saw and their ids."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.seen = []

    def self_process(self, entity):
        self.seen.append(entity.id)
        self.ctx.record_kpi("tallied", len(self.seen))
        self.ctx.add_artifact("ids", list(self.seen))
        yield self.env.timeout(0)
        self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {}

    def set_params_from_dict(self, param_dict):
        pass


NAGASIM_BLOCKS = [BlockSpec(type_name="TALLY", cls=Tally, params=(),
                            factory=lambda env, block: Tally(env, block))]
'''

LAYOUT = {"schema_version": 29,
          "blocks": [{"name": "In", "type": "CREATE",
                      "params": {"distribution": "Constant", "dist_params": {"value": 1.0}}},
                     {"name": "T", "type": "TALLY"},
                     {"name": "Out", "type": "DISPOSE", "params": {}}],
          "links": [{"source": "In", "target": "T"}, {"source": "T", "target": "Out"}],
          "simulation": {"time_limit": 5.5, "replications": 2}}

SCRIPT = '''
import json, sys
from nagasim import api
res = api.run_model(json.loads(sys.argv[1]), seed=3, workers=2)
print(json.dumps({"kpis": res["plugin_kpis"][["Metric", "Value", "Run"]].values.tolist(),
                  "artifacts": {str(k): v for k, v in res.artifacts.items()},
                  "frames": "plugin_artifacts" in res}))
'''


def _plugin_file(tmp_path) -> Path:
    folder = tmp_path / "plugs"
    folder.mkdir()
    path = folder / "tally.py"
    path.write_text(PLUGIN, encoding="utf-8")
    return path


def _found(res) -> dict:
    return {"kpis": res["plugin_kpis"][["Metric", "Value", "Run"]].values.tolist(),
            "artifacts": {str(k): v for k, v in res.artifacts.items()},
            "frames": ARTIFACTS_KEY in res}


def _expected(found: dict) -> None:
    # Entities at t = 0..5 in a 5.5 run: six per replication, runs 3 and 4 (the seeds).
    assert found["kpis"] == [["tallied", 6.0, 3], ["tallied", 6.0, 4]]
    assert sorted(found["artifacts"]) == ["3", "4"]
    assert found["artifacts"]["3"] == {"ids": [1, 2, 3, 4, 5, 6]}
    assert found["frames"] is False                     # the artifacts are not a frame


def test_a_plugin_records_kpis_and_artifacts_per_replication(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("_test_tally_plugin", _plugin_file(tmp_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(BLOCK_REGISTRY, "TALLY", module.NAGASIM_BLOCKS[0])
    _expected(_found(api.run_model(LAYOUT, seed=3, workers=1)))


def test_a_run_without_plugin_results_has_none():
    layout = {**LAYOUT, "blocks": [LAYOUT["blocks"][0], LAYOUT["blocks"][2]],
              "links": [{"source": "In", "target": "Out"}]}
    res = api.run_model(layout, seed=3, workers=1)
    assert "plugin_kpis" not in res and res.artifacts == {}


@pytest.mark.slow
def test_artifacts_come_back_from_pool_workers(tmp_path):
    """A real plugin folder in a subprocess, two pool workers: the artifacts pickle back."""
    folder = _plugin_file(tmp_path).parent
    env = {**os.environ, "NAGASIM_PLUGIN_PATH": str(folder), "PYTHONIOENCODING": "utf-8",
           "NAGASIM_USER_DIR": str(tmp_path / "user")}
    proc = subprocess.run([sys.executable, "-c", SCRIPT, json.dumps(LAYOUT)], cwd=str(REPO_ROOT),
                          env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    _expected(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_concat_runs_takes_a_frame_some_replications_lack_and_drops_artifacts():
    def rep(run, kpi=None):
        out = {"system": pd.DataFrame({"Metric": ["m"], "Value": [1.0], "Run": [run]})}
        if kpi is not None:
            out["plugin_kpis"] = pd.DataFrame({"Metric": ["k"], "Value": [kpi], "Run": [run]})
            out[ARTIFACTS_KEY] = (run, {"plan": [run]})
        return out
    merged = concat_runs([rep(0), rep(1, 2.5), rep(2)])
    assert list(merged) == ["system", "plugin_kpis"]
    assert merged["plugin_kpis"]["Run"].tolist() == [1] and len(merged["system"]) == 3


def test_plugin_kpis_are_study_kpis_and_a_workbook_sheet(tmp_path, monkeypatch):
    """`plugin/<name>` names a recorded KPI in a study; the workbook gets a "Plugin KPIs" sheet."""
    from openpyxl import load_workbook

    from nagasim import experiments
    from nagasim.core.export import export_workbook

    spec = importlib.util.spec_from_file_location("_test_tally_plugin", _plugin_file(tmp_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(BLOCK_REGISTRY, "TALLY", module.NAGASIM_BLOCKS[0])
    res = api.run_model(LAYOUT, seed=3, workers=1)
    flat = experiments.flatten_kpis(res)
    assert flat[flat["KPI"] == "plugin/tallied"][["Run", "Value"]].values.tolist() == [[3, 6.0], [4, 6.0]]
    export_workbook(res, tmp_path / "w.xlsx")
    sheets = load_workbook(tmp_path / "w.xlsx", read_only=True).sheetnames
    assert "Plugin KPIs" in sheets and "Queue slots" not in sheets
