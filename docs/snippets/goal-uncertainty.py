import numpy as np
from nagasim import build as nb, uncertainty

# Only 30 observed handling times, so the fit itself is uncertain. Here they are drawn from a seeded
# lognormal; in a study they are your own data.
times = np.random.default_rng(7).lognormal(mean=-0.4, sigma=0.5, size=30)

m = nb.Model(time_limit=480, warmup=0)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25))   # replaced by the fit
arrive >> serve >> m.dispose()

answer = uncertainty.input_uncertainty(
    m.to_dict(), [{"path": f"blocks.{serve.name}.params.distribution", "values": list(times)}],
    kpis=["resource/Server/Time in Queue Avg"], resamples=20, replications=5, seed=1)

row = answer["kpis"][0]
print(f"{answer['runs']} runs: {answer['resamples_used']} resamples of the data x {answer['replications']} replications, plus the fit itself")
print(f"mean wait {row['mean']:.2f} minutes")
print(f"interval from run-to-run noise only: {row['run_ci'][0]:.2f} to {row['run_ci'][1]:.2f}")
print(f"interval with the inputs' uncertainty: {row['input_interval'][0]:.2f} to {row['input_interval'][1]:.2f}")
print(f"share of the spread that comes from the inputs: {row['input_share']:.0%}")
