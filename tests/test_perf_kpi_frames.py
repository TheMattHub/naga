"""D690: calc_kpis copies its empty frames from cached templates and builds a
non-empty log with its "Run" column; neither may change a frame's shape or leak
between replications (the golden digests hash CSV text, which misses dtypes)."""
import json
import os

from nagasim import api
from nagasim.core.outputs import calc_kpis
from nagasim.core.sim_runner import setup_run, step_until

MODEL = os.path.join(os.path.dirname(api.__file__), "models", "single_server.json")


def _kpis(seed):
    with open(MODEL) as fh:
        layout = json.load(fh)
    env, _ = setup_run(layout, seed=seed, run_len=50.0, warmup=5.0)
    step_until(env, 50.0)
    return calc_kpis(env, warmup=5.0, run_id=seed)


def test_empty_frames_do_not_share_state_and_run_is_tagged_either_way():
    first = _kpis(1)
    assert first["order_summary"].empty
    first["order_summary"]["Extra"] = 1          # a caller mutating its frame...
    second = _kpis(2)
    assert "Extra" not in second["order_summary"].columns   # ...never reaches the next run
    assert second["order_summary"] is not first["order_summary"]

    # Non-empty log: "Run" built with the rows; empty log: inserted after. Both
    # trail and are int64, as the column insert always gave.
    entity_log, store_log = second["entity_log"], second["store_log"]
    assert len(entity_log) > 0 and store_log.empty
    for df in (entity_log, store_log, second["resource_log"], second["system"]):
        assert df.columns[-1] == "Run"
        assert str(df["Run"].dtype) == "int64"
    assert (entity_log["Run"] == 2).all()
