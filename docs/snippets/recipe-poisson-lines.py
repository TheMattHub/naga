from nagasim import build as nb

# A picking order has a Poisson number of lines (mean 6). The pick takes one minute a line, so the
# delay is the count itself. The Python keyword `lambda` is the parameter name, hence Distribution().
m = nb.Model(time_limit=4000, warmup=0)
m.add_resource(name="Picker")
orders = m.create(distribution=nb.Exponential(rate=1 / 8))
lines = nb.Distribution("Poisson", {"lambda": 6})
orders >> m.process(resource_name="Picker", distribution=lines) >> m.dispose()

results = m.run(replications=10, seed=1)
print(f"picker busy {results.kpi('resource/Picker/Usage % (snapshot)'):.0f}% of the time (6 of every 8 minutes is 75%)")
print(f"mean wait for the picker {results.kpi('resource/Picker/Time in Queue Avg'):.1f} minutes")
