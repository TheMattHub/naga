from nagasim import build as nb

m = nb.Model(
    time_limit=600, time_unit="seconds",
    network={"nodes": [{"name": "dock", "x": 0, "y": 0}, {"name": "yard", "x": 100, "y": 0}],
             "segments": [{"from": "dock", "to": "yard", "length": 100, "two_way": True}]})
m.add_fleet(name="Trucks", count=1, speed=5.0, home="dock", load_time="10", unload_time="10")
dock = m.station(station="Dock", node="dock")
yard = m.station(station="Yard", node="yard")
m.create(distribution=nb.Exponential(rate=1 / 120)) >> dock >> m.transport(fleet="Trucks", destination="Yard") >> yard >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("system/Total Entities Completed")), "entities reached the Yard station")
