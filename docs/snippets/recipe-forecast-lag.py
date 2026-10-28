from nagasim import build as nb

# Demand jumps from 100 to 150 a week at week 5. Production follows a forecast that catches up with
# demand through a first-order delay with a 4-week time constant, so stock drains while it does.
m = nb.Model(time_limit=40, time_unit="weeks", sd={"dt": 0.25, "method": "euler"})
m.add_auxiliary(name="Demand", expr="100 + STEP(50, 5)")
m.add_auxiliary(name="Forecast", expr='DELAY1(AUX("Demand"), 4)')
m.add_level(name="Stock", initial=400)
m.add_flow(name="production", to="Stock", rate='AUX("Forecast")')
m.add_flow(name="sales", **{"from": "Stock"}, rate='AUX("Demand")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
view = series[["aux/Demand", "aux/Forecast", "level/Stock"]]
view.columns = ["Demand", "Forecast", "Stock"]
print(view.reindex([4, 8, 12, 20, 40], method="nearest").round(1).rename_axis("Week").to_string())
