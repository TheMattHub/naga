from nagasim import build as nb


def mean_waits(service, seeds):
    waits = []
    for seed in seeds:
        m = nb.Model(time_limit=2000, warmup=0)
        m.add_resource(name="Bench")
        m.create(distribution=nb.Exponential(rate=0.5)) >> m.process(
            resource_name="Bench", distribution=service) >> m.dispose()
        waits.append(m.run(seed=seed).kpi("resource/Bench/Time in Queue Avg"))
    return " ".join(f"{w:7.1f}" for w in waits)


# Both services centre on 1 minute. Cauchy has no mean, so the average over a run never settles: one
# extreme draw ties up the bench for hours. A negative Cauchy draw is cut to 0 with a warning.
seeds = range(1, 6)
print("mean wait over five runs, one per seed (minutes):")
print("  Cauchy(x0=1, gamma=0.1)  ", mean_waits(nb.Cauchy(x0=1, gamma=0.1), seeds))
print("  Normal(1, 0.15)          ", mean_waits(nb.Normal(mean=1, stddev=0.15), seeds))
