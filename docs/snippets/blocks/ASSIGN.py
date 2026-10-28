from nagasim import build as nb

m = nb.Model(time_limit=300)
m.add_resource(name="Press")
arrive = m.create(distribution=nb.Exponential(rate=0.2))
size = m.assign(assignments=[{"target": "entity.size", "expr": "UNIF(1, 3)"}])
press = m.process(resource_name="Press", distribution=nb.Constant(value="entity.size"))
arrive >> size >> press >> m.dispose()

results = m.run(seed=1)
print(round(results.kpi("resource/Press/Usage % (snapshot)"), 1), "% busy; service time is entity.size, 2 on average")
