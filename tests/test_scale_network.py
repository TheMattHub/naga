"""The scale-network generator (conveyor plan D, Task 5): N pieces through the agent tools, a clean
layout, and a model that runs. The 2000-piece timing is tools/measure_scale.py, run by hand."""
import pytest
from nagasim.api import run_model

# The export does not ship the generator: the module skips in the engine clone.
pytest.importorskip("tools.gen_scale_network")


def test_a_small_network_has_n_pieces_a_clean_layout_and_runs(tmp_path):
    from tools.gen_scale_network import write
    path, stats = write(60, tmp_path)
    assert stats["pieces"] == 60 and stats["check_layout_ok"]
    results = run_model(str(path), replications=1, run_length=600, log_level="kpi")
    assert list(results.run_problems) == []
    system = results["system"].set_index("Metric")["Value"]
    assert float(system["Total Entities Completed"]) > 0
