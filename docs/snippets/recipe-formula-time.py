from nagasim import build as nb

# A repair is a fixed 2-minute diagnosis plus an exponential fix with a mean of 3 minutes. No single
# distribution says that, but an expression does. UNIF, EXPO, NORM, TRIA and DISC are available.
repair = nb.CustomExpression(expression="2 + EXPO(3)")

m = nb.Model(time_limit=4000, warmup=0)
m.add_resource(name="Bay")
cars = m.create(distribution=nb.Exponential(rate=1 / 8))
cars >> m.process(resource_name="Bay", distribution=repair) >> m.dispose()

results = m.run(replications=10, seed=1)
print(f"bay busy {results.kpi('resource/Bay/Usage % (snapshot)'):.0f}% (mean repair 5 minutes in 8 is 62%)")
