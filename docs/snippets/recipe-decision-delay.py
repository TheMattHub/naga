from nagasim import build as nb
from nagasim.core.distributions import distribution_mean

# Approval time: right-skewed, never below zero. mu is the mean (3 minutes); lambda is the shape,
# and a larger lambda makes the times less spread out.
approval = nb.InverseGaussian(mu=3, lambda_=6)
print("mean approval time:", round(distribution_mean("InverseGaussian", {"mu": 3, "lambda": 6}), 2), "minutes")

m = nb.Model(time_limit=4000, warmup=0)
m.add_resource(name="Approver")
requests = m.create(distribution=nb.Exponential(rate=1 / 5))
requests >> m.process(resource_name="Approver", distribution=approval) >> m.dispose()

results = m.run(replications=10, seed=1)
print(f"approver busy {results.kpi('resource/Approver/Usage % (snapshot)'):.0f}% (3 minutes in 5 is 60%)")
