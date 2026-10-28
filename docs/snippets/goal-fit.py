import numpy as np
from nagasim import build as nb, inputfit

# 200 handling times in minutes. Here they are drawn from a seeded lognormal; in a study they come from
# a column of your own data: `inputfit.load_column("times.csv").values`.
times = np.random.default_rng(7).lognormal(mean=-0.4, sigma=0.5, size=200)

fits = inputfit.fit_candidates(times)
for fit in fits[:3]:
    params = {name: round(float(value), 3) for name, value in fit.dist_params.items()}
    print(f"{fit.distribution:<15} AIC {fit.aic:6.1f}  KS p-value {fit.ks_pvalue:.2f}  {params}")

best = inputfit.best_fit(times)
print("winner:", best.distribution)


def mean_wait(service):
    m = nb.Model(time_limit=480, warmup=0)
    m.add_resource(name="Server")
    arrive = m.create(distribution=nb.Exponential(rate=1.0))
    arrive >> m.process(resource_name="Server", distribution=service) >> m.dispose()
    return m.run(replications=20, seed=1).kpi("resource/Server/Time in Queue Avg")


# The same queue with the fitted distribution, and with an exponential of the same mean.
print(f"fitted {best.distribution}: mean wait {mean_wait(nb.Distribution(best.distribution, best.dist_params)):.2f} minutes")
print(f"exponential, same mean: mean wait {mean_wait(nb.Exponential(rate=1 / times.mean())):.2f} minutes")
