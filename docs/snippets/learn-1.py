from nagasim import build as nb

m = nb.Model(time_limit=480, warmup=0)         # one 8-hour day, in minutes
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))          # a customer a minute
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25))
arrive >> serve >> m.dispose()

results = m.run(seed=1)
print(results.kpi("system/Total Entities Completed"), "customers served")
print(round(results.kpi("resource/Server/Time in Queue Avg"), 2), "minutes mean wait")
