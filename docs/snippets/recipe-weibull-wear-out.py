from nagasim import build as nb


def breakdown_run(uptime):
    m = nb.Model(time_limit=2000, warmup=0)
    m.add_resource(name="Press")
    arrive = m.create(distribution=nb.Exponential(rate=0.5))
    arrive >> m.process(resource_name="Press", distribution=nb.Exponential(rate=1.0)) >> m.dispose()
    m.add_failure(name="Wear", resources=["Press"], uptime=uptime,
                  downtime={"distribution": "Constant", "dist_params": {"value": 20}})
    return m.run(replications=10, seed=1)


# Both uptimes average 100 minutes. Weibull shape 3 is wear-out: failures bunch around 100 minutes,
# while the exponential's scatter from almost 0 to over 400.
wear_out = {"distribution": "Weibull", "dist_params": {"scale": 112, "shape": 3}}
memoryless = {"distribution": "Exponential", "dist_params": {"rate": 0.01}}
for label, uptime in (("Weibull shape 3", wear_out), ("Exponential", memoryless)):
    results = breakdown_run(uptime)
    print(f"{label:<16} mean wait {results.kpi('resource/Press/Time in Queue Avg'):5.1f} minutes")
