from nagasim import build as nb

m = nb.Model(time_limit=100)
m.create(distribution=nb.Exponential(rate=0.5), max_arrivals=20) >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Created")), "entities created")
print(int(results.kpi("system/Total Entities Completed")), "entities completed")
