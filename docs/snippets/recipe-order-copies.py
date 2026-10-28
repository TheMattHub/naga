from nagasim import build as nb

# One customer order is three dishes. DUPLICATE with number_of_duplicates=3 sends three entities on
# (the original and two copies), so the kitchen sees 3 jobs per order. Orders come every 6 minutes
# and a dish takes 1.5 minutes on average: 3 x 1.5 / 6 = 75% busy.
m = nb.Model(time_limit=2000, warmup=0)
m.add_resource(name="Kitchen")
orders = m.create(distribution=nb.Exponential(rate=1 / 6))
dishes = m.duplicate(number_of_duplicates=3)
orders >> dishes >> m.process(resource_name="Kitchen", distribution=nb.Exponential(rate=1 / 1.5)) >> m.dispose()

results = m.run(replications=5, seed=1)
print(f"kitchen busy {results.kpi('resource/Kitchen/Usage % (snapshot)'):.0f}%")
print(f"jobs per order: {results.kpi('system/Total Entities Completed') / (2000 / 6):.1f}")
