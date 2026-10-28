from nagasim import build as nb

m = nb.Model(time_limit=100)
m.add_level(name="Tank", capacity=100, initial=60)
m.create(distribution=nb.Constant(value=10)) >> m.transfer(level="Tank", amount="5", mode="remove", wait=True) >> m.dispose()

results = m.run(seed=1)
print(round(results.kpi("level/Tank/Final"), 1), "units left in the tank after the run")
