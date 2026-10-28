from nagasim import build as nb

m = nb.Model(time_limit=3600, time_unit="seconds")
m.add_resource(name="Cell")
m.add_motion_profile(name="Cobot", kind="robot", speed=1.5, accel=4.0, payload_max=10.0, payload_derate=0.7)
arrive = m.create(distribution=nb.Constant(value=60))
cycle = m.motion(profile="Cobot", resource_name="Cell", segments=[
    {"kind": "move", "distance_m": 1.0},
    {"kind": "pick", "time": "1.0"},
    {"kind": "move", "distance_m": 1.0, "payload_kg": 2.0},
    {"kind": "place", "time": "1.0"},
])
arrive >> cycle >> m.dispose()

results = m.run(seed=1)
print(results["motion_summary"].round(2).to_string(index=False))
