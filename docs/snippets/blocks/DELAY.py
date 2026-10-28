from nagasim import build as nb

m = nb.Model(time_limit=200)
m.create(distribution=nb.Exponential(rate=0.2)) >> m.delay(distribution=nb.Triangular(low=4, mode=5, high=9)) >> m.dispose()

results = m.run(seed=1)
print(round(results.kpi("entity/DEFAULT/Avg Time"), 2), "mean time in the model (a triangular delay, mean 6)")
