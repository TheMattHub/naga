from nagasim import build as nb

LAM, MU, RUN, WARMUP = 1.0, 1.25, 5500, 500      # per minute, and minutes

m = nb.Model(time_limit=RUN, warmup=WARMUP, replications=5)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=LAM))
arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=MU)) >> m.dispose()

row = m.run(seed=0).kpis().set_index("metric").loc["system/Avg Throughput Time"]
print(f"Naga: mean time in system {row['mean']:.2f} +/- {row['ci95']:.2f} minutes")
print(f"exact: {1 / (MU - LAM):.2f} minutes")
