from nagasim import build as nb

m = nb.Model(time_limit=480, warmup=60)
m.add_resource(name="Dock")
arrive = m.create(distribution=nb.Exponential(rate=0.2))
arrive >> m.seize(resource_name="Dock") >> m.delay(distribution=nb.Constant(value=3)) >> m.release(resource_name="Dock") >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("resource/Dock/Total Seizes")), "seizes; each ended by the RELEASE block")
