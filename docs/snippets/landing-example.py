from nagasim import build as nb

lam, mu = 1.0, 1.25                                  # customers arrive, and are served, per minute
m = nb.Model(time_limit=5500, warmup=500, replications=20)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=lam))
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=mu))
arrive >> serve >> m.dispose()

row = m.run(seed=0).kpis().set_index("metric").loc["resource/Server/Time in Queue Avg"]
print(f"simulated mean wait {row['mean']:.2f} +/- {row['ci95']:.2f} minutes (95% interval)")
print(f"exact, M/M/1        {lam / mu / (mu - lam):.2f} minutes")
