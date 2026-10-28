from nagasim import build as nb


def busy_with(rework):
    """Percent of time the test bench is busy when each attempt or reject takes one minute."""
    m = nb.Model(time_limit=4000, warmup=0)
    m.add_resource(name="Bench")
    parts = m.create(distribution=nb.Exponential(rate=1 / 4))
    parts >> m.process(resource_name="Bench", distribution=rework) >> m.dispose()
    return m.run(replications=10, seed=1).kpi("resource/Bench/Usage % (snapshot)")


# Geometric: attempts until the first pass, mean 1 / 0.5 = 2 minutes, so 2 in 4 is 50% busy.
print(f"Geometric(p=0.5):              {busy_with(nb.Geometric(p=0.5)):.0f}% busy")
# NegativeBinomial: rejects before 3 good parts, mean 3 * 0.2 / 0.8 = 0.75 minutes, so 19% busy.
print(f"NegativeBinomial(r=3, p=0.8):  {busy_with(nb.NegativeBinomial(r=3, p=0.8)):.0f}% busy")
