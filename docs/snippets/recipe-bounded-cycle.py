from nagasim import build as nb
from nagasim.core.distributions import distribution_mean


def cell_run(cycle):
    m = nb.Model(time_limit=4000, warmup=0)
    m.add_resource(name="Cell")
    parts = m.create(distribution=nb.Exponential(rate=0.4))
    parts >> m.process(resource_name="Cell", distribution=cycle) >> m.dispose()
    return m.run(replications=10, seed=1).kpi("resource/Cell/Time in Queue Avg")


# Mean 2 minutes, sd 1. A Normal can draw a negative time, which the engine cuts to 0 with a warning.
# BoundedNormal keeps every draw between 0.5 and 4 minutes, which also shifts its mean a little.
normal = nb.Normal(mean=2, stddev=1)
bounded = nb.BoundedNormal(mean=2, stddev=1, min=0.5, max=4)
print(f"Normal(2, 1)                 mean wait {cell_run(normal):.2f} minutes")
print(f"BoundedNormal(2, 1, 0.5..4)  mean wait {cell_run(bounded):.2f} minutes, "
      f"mean time {distribution_mean('BoundedNormal', bounded.params):.2f} minutes")
