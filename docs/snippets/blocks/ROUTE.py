from nagasim import build as nb

m = nb.Model(time_limit=500)
m.add_resource(name="Saw")
m.add_resource(name="Drill")
m.add_sequence(name="Bracket", steps=[
    {"station": "Cutting", "values": {"process_time": 2.0}},
    {"station": "Drilling", "values": {"process_time": 3.0}},
])
route = m.route()
cutting = m.station("Cutting")
drilling = m.station("Drilling")
saw = m.process(resource_name="Saw", distribution=nb.Constant(value="entity.process_time"))
drill = m.process(resource_name="Drill", distribution=nb.Constant(value="entity.process_time"))
m.create(distribution=nb.Exponential(rate=0.2), sequence="Bracket") >> route
cutting >> saw >> route
drilling >> drill >> route
route >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("resource/Saw/Total Seizes")), "parts cut,", int(results.kpi("resource/Drill/Total Seizes")), "drilled")
print(round(results.kpi("system/Avg Throughput Time"), 2), "time units from start to finish, on average")
