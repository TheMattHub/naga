from nagasim import build as nb

# A batch of 20 parts, each defective with probability 0.1 (Binomial). Every defect costs one minute
# of rework, so the rework delay is the defect count. A single part is the Bernoulli case: p of
# one minute, otherwise none.
m = nb.Model(time_limit=4000, warmup=0)
m.add_resource(name="Rework")
batches = m.create(distribution=nb.Exponential(rate=1 / 4))
batches >> m.process(resource_name="Rework", distribution=nb.Binomial(n=20, p=0.1)) >> m.dispose()

results = m.run(replications=10, seed=1)
print(f"rework bench busy {results.kpi('resource/Rework/Usage % (snapshot)'):.0f}% (2 defects in 4 minutes is 50%)")

single = nb.Model(time_limit=4000, warmup=0)
single.add_resource(name="Rework")
single.create(distribution=nb.Exponential(rate=1 / 4)) >> single.process(
    resource_name="Rework", distribution=nb.Bernoulli(p=0.5)) >> single.dispose()
busy = single.run(replications=10, seed=1).kpi("resource/Rework/Usage % (snapshot)")
print(f"single part, Bernoulli(0.5): busy {busy:.0f}% (0.5 minute in 4 is 12.5%)")
