from nagasim import build as nb
from nagasim.core.distributions import distribution_mean


def busy_with(fill):
    m = nb.Model(time_limit=4000, warmup=0)
    m.add_resource(name="Filler")
    m.create(distribution=nb.Exponential(rate=1 / 12)) >> m.process(
        resource_name="Filler", distribution=fill) >> m.dispose()
    return m.run(replications=10, seed=1).kpi("resource/Filler/Usage % (snapshot)")


# A fill takes between 2 and 10 minutes, mostly towards the long end: JohnsonSB lives on a fixed
# interval. JohnsonSU, set to a similar centre, has no bounds, so its tails can pass 2 and 10.
bounded = nb.JohnsonSB(a1=1, a2=1, min=2, max=10)
unbounded = nb.JohnsonSU(a=0, b=1.5, loc=7, scale=2)
for label, dist in (("JohnsonSB", bounded), ("JohnsonSU", unbounded)):
    mean = distribution_mean(dist.name, dist.params)
    print(f"{label}: mean {mean:.2f} minutes, filler busy {busy_with(dist):.0f}%")
