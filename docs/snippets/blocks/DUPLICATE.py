from nagasim import build as nb

m = nb.Model(time_limit=100)
m.create(distribution=nb.Constant(value=5), max_arrivals=10) >> m.duplicate(number_of_duplicates=3) >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "entities left: 10 arrivals, each leaving as 3 (the original and 2 copies)")
