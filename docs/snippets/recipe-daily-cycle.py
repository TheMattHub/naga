from nagasim import build as nb

# A pump fills a tank at 10 m3 an hour, swinging 50% up and down over a 24-hour day (SIN, PI), and
# only runs at 20% on the two days of every seven that are weekend (MODULO). Time is in hours.
m = nb.Model(time_limit=7 * 24, time_unit="hours", sd={"dt": 0.5, "method": "euler"})
m.add_auxiliary(name="Daily", expr="10 * (1 + 0.5 * SIN(2 * PI() * TNOW / 24))")
m.add_auxiliary(name="Weekend", expr="IF(MODULO(TNOW / 24, 7) >= 5, 0.2, 1)")
m.add_level(name="Tank", initial=0)
m.add_flow(name="pump", to="Tank", rate='AUX("Daily") * AUX("Weekend")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
rate = series["aux/Daily"] * series["aux/Weekend"]
print(f"peak rate {rate.max():.1f} at hour {rate.idxmax():.0f}, lowest {rate.min():.1f} at hour {rate.idxmin():.0f}")
for day in (0, 4, 5, 6):
    hours = series.loc[(series.index >= day * 24) & (series.index < day * 24 + 24)]
    print(f"day {day}: tank gains {hours['level/Tank'].iloc[-1] - hours['level/Tank'].iloc[0]:6.1f} m3")
