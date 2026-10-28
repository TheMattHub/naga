from nagasim import build as nb, optimize

m = nb.Model(time_limit=480, warmup=0)         # one 8-hour day, in minutes
m.add_resource(name="Agents", capacity=5, cost_busy=20, cost_idle=20, cost_time_unit="hours")
m.add_entity(name="Call", holding_cost=0.5)    # every caller in the system costs 0.5 a minute
arrive = m.create(e_type="Call", distribution=nb.Exponential(rate=2.0))     # two calls a minute
serve = m.process(resource_name="Agents", distribution=nb.Exponential(rate=0.5))   # 2 minutes each
arrive >> serve >> m.dispose()

staff = optimize.Factor(path="resources.Agents.capacity", kind="integer", low=5, high=12, name="agents")
found = optimize.optimize(m.to_dict(), [staff], response="system/Total Cost", goal="min",
                          budget=80, replications_per_point=4, seed=1)

print(f"best: {found.best['agents']} agents, mean cost {found.best_mean:.0f}")
tried = found.history[["agents", "Mean", "N"]].drop_duplicates("agents").sort_values("agents")
tried["Mean"] = tried["Mean"].round().astype(int)
print(tried.to_string(index=False))
