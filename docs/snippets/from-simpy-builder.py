from nagasim import build as nb

m = nb.Model(time_limit=10000, warmup=1000, replications=5)    # the settings of mm1.json above
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25)) >> m.dispose()

kpis = m.run(seed=0).kpis().set_index("metric")
print("Naga from Python: mean time in system", round(kpis.loc["system/Avg Throughput Time", "mean"], 2))
