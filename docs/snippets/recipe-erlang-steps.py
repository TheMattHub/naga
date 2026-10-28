from nagasim import build as nb


def wait_with(service):
    m = nb.Model(time_limit=480, warmup=0)
    m.add_resource(name="Oven")
    arrive = m.create(distribution=nb.Exponential(rate=0.8))
    arrive >> m.process(resource_name="Oven", distribution=service) >> m.dispose()
    return m.run(replications=20, seed=1).kpi("resource/Oven/Time in Queue Avg")


# A bake is four steps, each exponential with a mean of 0.3 minutes: Erlang(k=4, rate=1/0.3).
# The same 1.2-minute mean as one exponential step, but with a quarter of the variance.
print(f"Erlang, 4 steps:  mean wait {wait_with(nb.Erlang(k=4, rate=1 / 0.3)):.2f} minutes")
print(f"one exponential:  mean wait {wait_with(nb.Exponential(rate=1 / 1.2)):.2f} minutes")
