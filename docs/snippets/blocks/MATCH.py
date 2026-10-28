from nagasim import build as nb

m = nb.Model(time_limit=300)
m.add_entity(name="FRAME")
m.add_entity(name="WHEEL")
frames = m.create("Frames", e_type="FRAME", distribution=nb.Constant(value=4), max_arrivals=20)
wheels = m.create("Wheels", e_type="WHEEL", distribution=nb.Constant(value=3), max_arrivals=20)
pair = m.match("Pair", number_to_match=2)     # waits for one FRAME and one WHEEL, then releases both
frames >> pair
wheels >> pair
pair >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "entities released, in 20 pairs")
print(round(results.kpi("entity/WHEEL/Avg Time"), 2), "time units a wheel waits for its frame, on average (wheels arrive faster)")
