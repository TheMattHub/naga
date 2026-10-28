from nagasim import build as nb

m = nb.Model(time_limit=480, warmup=60)
m.add_resource(name="Operator")
arrive = m.create(distribution=nb.Exponential(rate=0.4))
take = m.seize(resource_name="Operator")
work = m.delay(distribution=nb.Exponential(rate=0.6))
arrive >> take >> work >> m.release(resource_name="Operator") >> m.dispose()

results = m.run(seed=1)
print(round(results.kpi("resource/Operator/Usage % (snapshot)"), 1), "% of the time the operator is held")
print(round(results.kpi("resource/Operator/Time in Queue Avg"), 2), "time units mean wait for the operator")
