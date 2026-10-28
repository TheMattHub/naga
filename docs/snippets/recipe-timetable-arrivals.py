from nagasim import build as nb

# Five deliveries are booked at minutes 10, 25, 30, 90 and 150. A FixedSchedule CREATE makes one
# truck at each listed time and nothing else.
m = nb.Model(time_limit=240, warmup=0)
m.add_resource(name="Dock")
trucks = m.create(distribution=nb.FixedSchedule(schedule=[10, 25, 30, 90, 150]))
trucks >> m.process(resource_name="Dock", distribution=nb.Constant(value=20)) >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "trucks unloaded")
print(f"mean wait {results.kpi('resource/Dock/Time in Queue Avg'):.1f} minutes "
      "(the trucks at 25 and 30 queue behind the one before)")
