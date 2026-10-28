from nagasim import build as nb


def line_with_buffer(capacity):
    m = nb.Model(time_limit=2000, warmup=200)
    m.add_resource(name="Cutter")
    m.add_resource(name="Welder")
    cut = m.process(resource_name="Cutter", distribution=nb.Exponential(rate=1.0))
    weld = m.process(resource_name="Welder", distribution=nb.Exponential(rate=1.0))
    # Parts arrive at 0.8 a minute, each machine averages 1 a minute, and the buffer holds `capacity` parts.
    m.create(distribution=nb.Exponential(rate=0.8)) >> cut >> m.buffer(capacity=capacity) >> weld >> m.dispose()
    return m.run(replications=5, seed=1)


print("buffer  parts finished  cutter blocked %")
for capacity in (1, 5, 50):
    results = line_with_buffer(capacity)
    summary = results["buffer_summary"]
    blocked = summary["Blocked %"].dropna().mean()
    print(f"{capacity:>6}  {results.kpi('system/Total Entities Completed'):14.0f}  {blocked:15.1f}")
