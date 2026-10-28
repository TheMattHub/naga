from nagasim import build as nb


def busy_with(sizes):
    """A packer needs one minute per item in the order."""
    m = nb.Model(time_limit=4000, warmup=0)
    m.add_resource(name="Packer")
    orders = m.create(distribution=nb.Exponential(rate=1 / 4))
    orders >> m.process(resource_name="Packer", distribution=sizes) >> m.dispose()
    return m.run(replications=10, seed=1).kpi("resource/Packer/Usage % (snapshot)")


# Any whole number from 1 to 6, each equally likely: mean 3.5, so 3.5 in 4 is 88% busy.
print(f"DiscreteUniform(1, 6):           {busy_with(nb.DiscreteUniform(low=1, high=6)):.0f}% busy")
# Your own counts: half the orders have 1 item, 30% have 2, 20% have 5. Mean 2.1, so 53% busy.
own = nb.EmpiricalDiscrete(values=[1, 2, 5], probs=[0.5, 0.3, 0.2])
print(f"EmpiricalDiscrete 1/2/5:         {busy_with(own):.0f}% busy")
