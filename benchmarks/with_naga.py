"""The models in Naga: the network is written as a model (blocks, links, resources) and
`run_model` runs the replications and returns the statistics."""
from nagasim.api import run_model

from models import ARRIVAL_RATE


def layout(stations, run_length, warmup, replications, lean=False):
    """The model document. *lean* switches off what the other libraries do not do in this
    run: the per-event logs and the time series sampler. The summary statistics of every
    block, resource and queue are always collected; there is no switch for them."""
    blocks = [{"name": "arrive", "type": "CREATE",
               "params": {"distribution": "Exponential", "dist_params": {"rate": ARRIVAL_RATE}}},
              {"name": "leave", "type": "DISPOSE", "params": {}}]
    links, resources = [{"source": "arrive", "target": "station0"}], []
    for i, (count, rate, to) in enumerate(stations):
        blocks.append({"name": f"station{i}", "type": "PROCESS",
                       "params": {"action": "Seize Delay Release", "resource_name": f"Server{i}",
                                  "distribution": "Exponential", "dist_params": {"rate": rate}}})
        resources.append({"name": f"Server{i}", "capacity": count})
        targets, chances = [f"station{j}" for j in to], list(to.values())
        if sum(chances) < 1 - 1e-9:
            targets.append("leave")
            chances.append(round(1 - sum(chances), 9))
        source = f"station{i}"
        if len(targets) > 1:                # a certain next step needs no DECIDE block
            blocks.append({"name": f"route{i}", "type": "DECIDE",
                           "params": {"mode": "Chance", "probs": chances}})
            links.append({"source": source, "target": f"route{i}"})
            source = f"route{i}"
        links += [{"source": source, "target": target} for target in targets]
    simulation = {"time_limit": run_length, "warmup": warmup, "replications": replications}
    if lean:
        simulation.update(record_logs=False, timeseries_points=0)
    return {"blocks": blocks, "links": links, "resources": resources, "simulation": simulation}


def run(stations, run_length, warmup, replications, lean=False):
    """Mean time in system of each replication (customers who left after the warm-up)."""
    system = run_model(layout(stations, run_length, warmup, replications, lean), seed=0)["system"]
    return [float(value) for value in system.loc[system["Metric"] == "Avg Throughput Time", "Value"]]
