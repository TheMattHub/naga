from nagasim import build as nb

# A plant earns 100 a year from year 0. NPV(stream, rate, init, factor) accumulates that stream
# discounted at 5% a year, on the model's step grid.
m = nb.Model(time_limit=20, time_unit="years", sd={"dt": 0.05, "method": "euler"})
m.add_auxiliary(name="Cash", expr="STEP(100, 0)")
m.add_auxiliary(name="Value", expr='NPV(AUX("Cash"), 0.05, 0, 1)')
m.add_level(name="Total", initial=0)
m.add_flow(name="earning", to="Total", rate='AUX("Cash")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
view = series[["level/Total", "aux/Value"]]
view.columns = ["Cash banked", "Present value"]
print(view.reindex([5, 10, 20], method="nearest").round(0).astype(int).rename_axis("Year").to_string())
exact = 100 * (1 - 2.718281828 ** (-0.05 * 20)) / 0.05
print(f"exact continuous discounting over 20 years: {exact:.0f}")
