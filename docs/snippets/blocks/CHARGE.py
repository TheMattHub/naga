from nagasim import build as nb

m = nb.Model(time_limit=600, time_unit="minutes")
cars = m.create(distribution=nb.Constant(value=15), max_arrivals=12)
bay = m.charge(energy="20", max_power="50", site_limit=60)      # 20 kWh a car, 50 kW a car, 60 kW for the site, so two cars share it
cars >> bay >> m.record(stat_name="charge_minutes", kind="tally", expr="entity.charge_time") >> m.dispose()

results = m.run(seed=1)
print(results["user_summary"].round(2).to_string(index=False))
