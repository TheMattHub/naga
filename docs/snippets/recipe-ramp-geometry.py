from nagasim import build as nb

# A ramp 20 m long rises 1.5 m. The math functions turn that into an angle, a run and the number of
# 1.2 m pallet lengths it takes; the hyperbolic ones shape a soft start for a conveyor speed.
m = nb.Model(time_limit=1, time_unit="hours", sd={"dt": 1, "method": "euler"})
m.add_auxiliary(name="rise", expr="1.5")
m.add_auxiliary(name="length", expr="20")
m.add_auxiliary(name="angle_deg", expr='DEG(ASIN(AUX("rise") / AUX("length")))')
m.add_auxiliary(name="run_m", expr='sqrt(AUX("length") ** 2 - AUX("rise") ** 2)')
m.add_auxiliary(name="slope_check_deg", expr='DEG(ATAN(AUX("rise") / AUX("run_m")))')
m.add_auxiliary(name="grade_pct", expr='100 * TAN(RAD(AUX("angle_deg")))')
m.add_auxiliary(name="pallets", expr='ceil(AUX("length") / 1.2)')
m.add_auxiliary(name="soft_start", expr="TANH(1.5)")
m.add_auxiliary(name="catenary_sag", expr="COSH(0.5) - 1")
m.add_auxiliary(name="growth", expr="SINH(0.5)")
m.add_auxiliary(name="decades", expr="log10(5000)")
m.add_auxiliary(name="doublings", expr="log(5000, 2)")
m.add_level(name="Clock", initial=0)
m.add_flow(name="tick", to="Clock", rate="1")

series = m.run(seed=1)["timeseries"]
last = series[series["Time"] == series["Time"].max()].set_index("Series")["Value"]
for name in ("angle_deg", "run_m", "slope_check_deg", "grade_pct", "pallets", "soft_start",
             "catenary_sag", "growth", "decades", "doublings"):
    print(f"{name:<16} {last['aux/' + name]:8.3f}")
