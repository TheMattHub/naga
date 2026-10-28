from nagasim import build as nb

# A time-study histogram: 50% of jobs took 0-2 minutes, 30% took 2-5, 20% took 5-10. `values` are the
# bin edges (one more than `probs`); a draw picks a bin by its weight, then a point inside it.
histogram = nb.EmpiricalContinuous(values=[0, 2, 5, 10], probs=[0.5, 0.3, 0.2])
mean = 0.5 * 1 + 0.3 * 3.5 + 0.2 * 7.5      # each bin's midpoint, weighted

m = nb.Model(time_limit=4000, warmup=0)
m.add_resource(name="Bench")
jobs = m.create(distribution=nb.Exponential(rate=1 / 6))
jobs >> m.process(resource_name="Bench", distribution=histogram) >> m.dispose()

results = m.run(replications=10, seed=1)
print(f"mean service by the bin midpoints: {mean:.2f} minutes, so {mean / 6 * 100:.0f}% busy")
print(f"bench busy in the run:             {results.kpi('resource/Bench/Usage % (snapshot)'):.0f}%")
