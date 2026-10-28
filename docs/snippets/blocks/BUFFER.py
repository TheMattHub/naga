from nagasim import build as nb

m = nb.Model(time_limit=480, warmup=60)
m.add_resource(name="Slow")
arrive = m.create(distribution=nb.Exponential(rate=0.5))
slow = m.process(resource_name="Slow", distribution=nb.Exponential(rate=0.6))
arrive >> m.buffer(capacity=3) >> slow >> m.dispose()

results = m.run(seed=1)
print(round(results.kpi("system/Avg WIP"), 2), "entities in the model on average")
print(round(results.kpi("resource/Slow/Usage % (snapshot)"), 1), "% busy")
