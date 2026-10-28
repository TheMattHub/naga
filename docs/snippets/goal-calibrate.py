from nagasim import build as nb, calibrate, optimize

# The model starts with a guess for the service rate. Calibration will correct it.
m = nb.Model(time_limit=5000, warmup=500)
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))         # one customer a minute
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.0))
arrive >> serve >> m.dispose()

# What you measured on the real system: customers waited 3.2 minutes on average. Accept an answer
# within 0.6 minutes of that.
targets = [{"kpi": "resource/Server/Time in Queue Avg", "observed": 3.2, "tolerance": 0.6}]
# What may move: the service rate, anywhere from 0.9 to 2.0 customers a minute.
rate = optimize.Factor(path=f"blocks.{serve.name}.params.dist_params.rate", kind="continuous",
                       low=0.9, high=2.0, name="service rate")

fit = calibrate.calibrate(m.to_dict(), targets, [rate], budget=80, replications_per_point=5, seed=1)
print(f"service rate: {fit.best['service rate']:.2f} per minute, {fit.budget_used} replications used")
row = fit.table.iloc[0]
print(f"simulated wait {row['Simulated']:.2f} minutes against {row['Observed']:.1f} observed, pass: {fit.passed}")

# Keep the tuned value as a scenario of the model.
tuned = fit.as_scenario()["overrides"][0]
print(f"scenario: {tuned['path']} = {tuned['value']:.2f}")

# Which values are consistent with the data? Rejection ABC returns an interval for each factor.
abc = calibrate.calibrate(m.to_dict(), targets, [rate], method="abc", budget=200,
                          replications_per_point=1, seed=1)
low, high = abc.posterior.iloc[0][["P5", "P95"]]
print(f"90% interval for the service rate: {low:.2f} to {high:.2f}")
