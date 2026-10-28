from nagasim import build as nb

# A lot of 50 parts holds 5 defectives. The inspector draws 10 parts without putting any back, and
# each defective found costs one minute of paperwork. Hypergeometric(N, K, n) is the defective count
# of that sample; its mean is n * K / N = 1. Binomial would draw with replacement and overstate the spread.
m = nb.Model(time_limit=4000, warmup=0)
m.add_resource(name="Inspector")
lots = m.create(distribution=nb.Exponential(rate=1 / 4))
found = nb.Hypergeometric(N=50, K=5, n=10)
lots >> m.process(resource_name="Inspector", distribution=found) >> m.dispose()

results = m.run(replications=10, seed=1)
print(f"inspector busy {results.kpi('resource/Inspector/Usage % (snapshot)'):.0f}% (1 minute in 4 is 25%)")
