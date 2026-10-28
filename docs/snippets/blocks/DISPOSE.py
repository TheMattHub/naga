from nagasim import build as nb

m = nb.Model(time_limit=100)
m.create(distribution=nb.Exponential(rate=0.5)) >> m.delay(distribution=nb.Constant(value=3)) >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "entities left the model")
print(round(results.kpi("system/Avg Throughput Time"), 2), "time units in the model, on average")
