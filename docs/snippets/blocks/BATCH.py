from nagasim import build as nb

m = nb.Model(time_limit=200)
m.create(distribution=nb.Constant(value=2), max_arrivals=30) >> m.batch(batch_size=5, batched_entity_type="PALLET") >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "completed: 30 parts leave as 6 pallets of 5")
