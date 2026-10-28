from nagasim import build as nb

# A daily reading of 100 with noise: NOISE_NORMAL(mean, sd, lo, hi) is a normal draw cut to [lo, hi],
# fixed for a given seed. SMOOTH and SMOOTH3 average it over about 5 days. The level only gives the
# model a step grid to run on.
m = nb.Model(time_limit=100, time_unit="days", sd={"dt": 0.5, "method": "euler"})
m.add_auxiliary(name="Reading", expr="NOISE_NORMAL(100, 20, 40, 160)")
m.add_auxiliary(name="Smooth", expr='SMOOTH(AUX("Reading"), 5)')
m.add_auxiliary(name="Smooth3", expr='SMOOTH3(AUX("Reading"), 5)')
m.add_level(name="Total", initial=0)
m.add_flow(name="intake", to="Total", rate='AUX("Smooth")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
settled = series.loc[20:, ["aux/Reading", "aux/Smooth", "aux/Smooth3"]]     # after the filters have warmed up
print("standard deviation after day 20:")
for name in ("Reading", "Smooth", "Smooth3"):
    print(f"  {name:<8} {settled['aux/' + name].std():5.1f}")
