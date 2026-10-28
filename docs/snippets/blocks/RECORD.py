from nagasim import build as nb

m = nb.Model(time_limit=100)
arrive = m.create(distribution=nb.Exponential(rate=1.0))
arrive >> m.record(stat_name="orders", kind="count") >> m.dispose()

results = m.run(seed=1)
print(results["user_summary"].to_string(index=False))
