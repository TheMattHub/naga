from nagasim import api, build as nb, experiments

m = nb.Model(time_limit=480, warmup=0)         # one 8-hour day, in minutes
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))          # a customer a minute
serve = m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25))
arrive >> serve >> m.dispose()
layout = m.to_dict()
wait = "resource/Server/Time in Queue Avg"

# 1. Ask for a precision instead of guessing a count: stop when the half-width is within 10% of the mean.
results, report = experiments.run_until_precision(layout, wait, relative=0.10, n0=5, seed=1)
print(f"{report['replications']} replications: mean wait {report['mean']:.2f} minutes, "
      f"half-width {report['half_width']:.2f}, target met: {report['met']}")

# 2. Ask MSER-5 where the start-up bias ends, on 50 runs each twice as long as the day.
long_runs = api.run_model(layout, replications=50, seed=1, run_length=1000, warmup=0)
suggestion = experiments.suggest_warmup(long_runs)
print(f"suggested warm-up {suggestion.warmup:.0f} minutes, reliable: {suggestion.reliable}")

# 3. Run the day again, now with that warm-up, to the same precision.
results, report = experiments.run_until_precision(layout, wait, relative=0.10, n0=5, seed=1,
                                                  warmup=suggestion.warmup)
print(f"with the warm-up: {report['replications']} replications: mean wait {report['mean']:.2f}, "
      f"half-width {report['half_width']:.2f}")
