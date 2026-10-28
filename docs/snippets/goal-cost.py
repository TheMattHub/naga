from nagasim import build as nb, optimize

m = nb.Model(time_limit=480, warmup=0)         # one 8-hour day, in minutes
# An agent costs 24 per hour while busy and 18 per hour while idle. These are quoted per hour
# because `cost_time_unit` says so.
m.add_resource(name="Agents", capacity=6, cost_busy=24, cost_idle=18, cost_time_unit="hours")
# A caller in the system costs 0.5 per MINUTE: `holding_cost` is per model time unit, which is
# minutes here, whatever `cost_time_unit` says.
m.add_entity(name="Call", holding_cost=0.5)
arrive = m.create(e_type="Call", distribution=nb.Exponential(rate=2.0))     # two calls a minute
serve = m.process(resource_name="Agents", distribution=nb.Exponential(rate=0.5))   # 2 minutes each
arrive >> serve >> m.dispose()

results = m.run(replications=10, seed=1)
busy = results.kpi("resource/Agents/Busy Cost")
idle = results.kpi("resource/Agents/Idle Cost")
holding = results.kpi("system/Total Entity Holding Cost")
total = results.kpi("system/Total Cost")
for name, part in (("agents busy", busy), ("agents idle", idle), ("callers waiting and served", holding)):
    print(f"{name:<27}{part:8.0f}  {100 * part / total:3.0f}%")
print(f"{'total per day':<27}{total:8.0f}")
# Check the holding line by hand: 0.5 a minute for each caller in the system, for 480 minutes.
wip = results.kpi("system/Avg WIP")
print(f"by hand: 0.5 x {wip:.2f} callers x 480 minutes = {0.5 * wip * 480:.0f}")

# Which input moves the total most? One at a time, each across a plausible range.
factors = [
    optimize.Factor(path="resources.Agents.capacity", kind="integer", low=5, high=7, name="agents"),
    optimize.Factor(path=f"blocks.{arrive.name}.params.dist_params.rate", low=1.6, high=2.0, name="arrival rate"),
    optimize.Factor(path="resources.Agents.cost_busy", low=20, high=28, name="busy cost"),
    optimize.Factor(path=f"blocks.{serve.name}.params.dist_params.rate", low=0.45, high=0.55, name="service rate"),
]
print(optimize.tornado(m.to_dict(), factors, "system/Total Cost", replications=5, seed=1)
      [["Factor", "KPI at low", "KPI at high", "Swing"]].round(0).to_string(index=False))
