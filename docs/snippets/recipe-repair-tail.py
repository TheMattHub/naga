from nagasim import build as nb
from nagasim.core.distributions import distribution_mean


def wait_with(repair):
    m = nb.Model(time_limit=2000, warmup=0)
    m.add_resource(name="Bay")
    m.create(distribution=nb.Exponential(rate=1 / 5)) >> m.process(
        resource_name="Bay", distribution=repair) >> m.dispose()
    return m.run(replications=10, seed=1).kpi("resource/Bay/Time in Queue Avg")


# Three right-skewed families, each set to a 3-minute mean. They differ in how long the tail is.
candidates = {
    "LogLogistic":   {"c": 4, "scale": 2.7},
    "PearsonTypeV":  {"a": 5, "scale": 12},
    "PearsonTypeVI": {"a": 3, "b": 8, "scale": 7},
}
for name, params in candidates.items():
    mean = distribution_mean(name, params)
    print(f"{name:<14} mean {mean:.2f}  mean wait {wait_with(nb.Distribution(name, params)):.2f} minutes")
