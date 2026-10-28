from nagasim import build as nb

m = nb.Model(time_limit=100)
logic = m.logic()                              # the stock SampleLogic: always link 0
m.create(distribution=nb.Constant(value=2)) >> logic
logic >> m.delay(distribution=nb.Constant(value=1)) >> m.dispose()
logic >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "entities, all sent down link 0")
