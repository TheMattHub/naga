from nagasim import build as nb

# A yield ratio, good parts over parts made, is 0 / 0 while the line is idle. ZIDZ(a, b) returns 0
# for a zero divisor and XIDZ(a, b, x) returns x, so the model never divides by zero. VAR("yield")
# reads the model variable `yield`, a name a bare expression could not spell because it is a Python keyword.
m = nb.Model(time_limit=20, sd={"dt": 0.5, "method": "euler"})
m.add_variable(name="yield", initial=0.9)
m.add_level(name="Made", initial=0)
m.add_level(name="Good", initial=0)
m.add_flow(name="making", to="Made", rate="STEP(10, 5)")
m.add_flow(name="passing", to="Good", rate="STEP(9, 5)")
m.add_auxiliary(name="Ratio", expr='ZIDZ(LEVEL("Good"), LEVEL("Made"))')
m.add_auxiliary(name="Ratio or 1", expr='XIDZ(LEVEL("Good"), LEVEL("Made"), 1)')
m.add_auxiliary(name="Gap to target", expr='VAR("yield") - AUX("Ratio")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
view = series[["aux/Ratio", "aux/Ratio or 1", "aux/Gap to target"]]
view.columns = ["Ratio", "Ratio or 1", "Gap to target"]
print(view.reindex([2, 5, 8, 20], method="nearest").round(2).rename_axis("Time").to_string())
