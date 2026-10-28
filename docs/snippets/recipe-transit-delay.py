from nagasim import build as nb

# Dispatch jumps from 0 to 10 a day at day 5, and the goods take 4 days on average to arrive.
# DELAYP is a pipeline: every unit arrives exactly 4 days later. DELAYN (here 2 stages) and DELAY3
# spread the arrivals around day 9, DELAY3 more tightly than the 2-stage one.
m = nb.Model(time_limit=30, time_unit="days", sd={"dt": 0.25, "method": "euler"})
m.add_auxiliary(name="Dispatch", expr="STEP(10, 5)")
for name, expr in (("Pipeline", 'DELAYP(AUX("Dispatch"), 4)'),
                   ("Two stages", 'DELAYN(AUX("Dispatch"), 4, 2)'),
                   ("Three stages", 'DELAY3(AUX("Dispatch"), 4)')):
    m.add_auxiliary(name=name, expr=expr)
m.add_level(name="Received", initial=0)
m.add_flow(name="arrivals", to="Received", rate='AUX("Pipeline")')

series = m.run(seed=1)["timeseries"].pivot(index="Time", columns="Series", values="Value")
view = series[["aux/Dispatch", "aux/Pipeline", "aux/Two stages", "aux/Three stages"]]
view.columns = ["Dispatch", "Pipeline", "Two stages", "Three stages"]
print(view.reindex([6, 8, 10, 14, 20], method="nearest").round(1).rename_axis("Day").to_string())
