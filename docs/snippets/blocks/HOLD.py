from nagasim import build as nb

m = nb.Model(time_limit=100)
m.create("Parts", distribution=nb.Constant(value=1), max_arrivals=10) >> m.hold(signal_name="Open") >> m.dispose()
m.create("Gate", distribution=nb.Constant(value=1), max_arrivals=1, start_time=30) >> m.signal(signal_name="Open") >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "entities completed: the 10 held parts and the one that sent the signal")
print(round(results.kpi("store/hold_1_hold/Average Size"), 2), "parts held on average")
