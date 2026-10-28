from nagasim import build as nb, optimize

m = nb.Model(time_limit=480, warmup=0)         # one 8-hour day, in minutes
m.add_resource(name="Agents", capacity=6)
arrive = m.create(distribution=nb.Exponential(rate=2.0))                           # two calls a minute
serve = m.process(resource_name="Agents", distribution=nb.Exponential(rate=0.5))   # 2 minutes each
arrive >> serve >> m.dispose()
layout = m.to_dict()
wait = "resource/Agents/Time in Queue Avg"

factors = [
    optimize.Factor(path="resources.Agents.capacity", kind="integer", low=5, high=7, name="agents"),
    optimize.Factor(path=f"blocks.{arrive.name}.params.dist_params.rate", low=1.6, high=2.0, name="arrival rate"),
    optimize.Factor(path=f"blocks.{serve.name}.params.dist_params.rate", low=0.45, high=0.55, name="service rate"),
]

# One-at-a-time first: which factor moves the wait most, one run each.
print(optimize.tornado(layout, factors, wait, replications=5, seed=1)
      [["Factor", "KPI at low", "KPI at high", "Swing"]].round(2).to_string(index=False))

# Then a full factorial, every combination of low and high, on common random numbers.
design = optimize.full_factorial(factors, levels=2)
result = optimize.run_design(layout, factors, design, responses=[wait], replications=5, seed=1)
print(result.table[["Point", "agents", "arrival rate", "service rate", "Mean"]].round(2).to_string(index=False))
print(result.main_effects(wait)[["Factor", "Level", "Mean", "Effect", "Effect LB", "Effect UB"]].round(2).to_string(index=False))
