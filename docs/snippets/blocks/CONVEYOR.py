from nagasim import build as nb

m = nb.Model(time_limit=300, time_unit="seconds")
m.create(distribution=nb.Constant(value=5)) >> m.conveyor(length=30, speed=1.0, spacing=1.0) >> m.dispose()

results = m.run(seed=1)
print(results["conveyor_summary"].round(2).to_string(index=False))
