from nagasim import build as nb

# Sales start at 100 and grow by 5 a week. TREND is the fractional growth rate against the 4-week
# smoothed value, and FORECAST extends the series 5 weeks ahead along that trend.
m = nb.Model(time_limit=30, time_unit="weeks", sd={"dt": 0.25, "method": "euler"})
m.add_auxiliary(name="Sales", expr="100 + RAMP(5, 0)")
m.add_auxiliary(name="Trend", expr='TREND(AUX("Sales"), 4)')
m.add_auxiliary(name="Forecast", expr='FORECAST(AUX("Sales"), 4, 5)')
m.add_level(name="Shipped", initial=0)
m.add_flow(name="shipping", to="Shipped", rate='AUX("Sales")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
view = series[["aux/Sales", "aux/Trend", "aux/Forecast"]]
view.columns = ["Sales", "Trend per week", "Forecast +5"]
print(view.reindex([4, 10, 20, 30], method="nearest").round(3).rename_axis("Week").to_string())
print("actual sales 5 weeks after week 20:", 100 + 5 * 25)
