from nagasim import build as nb

# A drone leaves the depot at a heading of 53.13 degrees from the x axis. Where is it after a 5 km leg,
# and what heading would take it back? RAD and DEG convert, COS and SIN give the offsets, ATAN2(y, x)
# gives the heading from two offsets. A level holds the distance flown so the model has a step grid.
m = nb.Model(time_limit=1, time_unit="hours", sd={"dt": 0.5, "method": "euler"})
m.add_auxiliary(name="x km", expr="5 * COS(RAD(53.13))")
m.add_auxiliary(name="y km", expr="5 * SIN(RAD(53.13))")
m.add_auxiliary(name="Outbound heading", expr='DEG(ATAN2(AUX("y km"), AUX("x km")))')
m.add_auxiliary(name="Heading home", expr='DEG(ATAN2(-AUX("y km"), -AUX("x km")))')
m.add_auxiliary(name="Check by ACOS", expr='DEG(ACOS(AUX("x km") / 5))')
m.add_level(name="Flown", initial=0)
m.add_flow(name="flying", to="Flown", rate="5")

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
last = series.iloc[-1]
for name in ("x km", "y km", "Outbound heading", "Heading home", "Check by ACOS"):
    print(f"{name:<17} {last['aux/' + name]:8.2f}")
