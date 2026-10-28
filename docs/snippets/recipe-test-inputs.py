from nagasim import build as nb

# Three standard test inputs into the same tank, which drains 10% of its content an hour:
# STEP(h, t0) jumps to h at t0, RAMP(s, t0) climbs by s an hour from t0, and PULSE(h, t0, w)
# is h for w hours from t0. Time is in hours.
m = nb.Model(time_limit=20, time_unit="hours", sd={"dt": 0.1, "method": "euler"})
for name, inflow in (("Step", "STEP(5, 2)"), ("Ramp", "RAMP(1, 2)"), ("Pulse", "PULSE(20, 2, 3)")):
    m.add_level(name=name, initial=0)
    m.add_flow(name=f"{name} in", to=name, rate=inflow)
    m.add_flow(name=f"{name} out", **{"from": name}, rate=f'0.1 * LEVEL("{name}")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
view = series[["level/Step", "level/Ramp", "level/Pulse"]]
view.columns = ["Step", "Ramp", "Pulse"]
print(view.reindex([2, 4, 5, 8, 12, 20], method="nearest").round(1).rename_axis("Hour").to_string())
