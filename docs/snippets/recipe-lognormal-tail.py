import math

from nagasim import build as nb


def wait_with(service):
    m = nb.Model(time_limit=480, warmup=0)
    m.add_resource(name="Packer")
    arrive = m.create(distribution=nb.Exponential(rate=0.5))
    arrive >> m.process(resource_name="Packer", distribution=service) >> m.dispose()
    return m.run(replications=20, seed=1).kpi("resource/Packer/Time in Queue Avg")


# mu and sigma belong to the underlying normal, so the mean is exp(mu + sigma^2 / 2).
mu, sigma = 0.1, 0.8
mean = math.exp(mu + sigma ** 2 / 2)
print(f"lognormal(mu={mu}, sigma={sigma}) has mean {mean:.2f} minutes")
print(f"lognormal service: mean wait {wait_with(nb.Lognormal(mu=mu, sigma=sigma)):.2f} minutes")
print(f"constant service:  mean wait {wait_with(nb.Constant(value=mean)):.2f} minutes")
