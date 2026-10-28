from nagasim import build as nb

m = nb.Model(time_limit=480, warmup=60)
m.add_resource(name="Lathe")
arrive = m.create(distribution=nb.Exponential(rate=0.5))
turn = m.process(resource_name="Lathe", distribution=nb.Exponential(rate=0.65))
arrive >> turn >> m.dispose()

results = m.run(seed=1)
print(round(results.kpi("resource/Lathe/Usage % (snapshot)"), 1), "% of the time the lathe is busy")
print(round(results.kpi("resource/Lathe/Time in Queue Avg"), 2), "time units mean wait")
