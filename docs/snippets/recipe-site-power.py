from nagasim import build as nb


def mean_charge_minutes(site_limit):
    m = nb.Model(time_limit=3000, time_unit="minutes")
    cars = m.create(distribution=nb.Exponential(rate=1 / 25), max_arrivals=100)
    # 20 kWh a car at up to 50 kW a car takes 24 minutes alone; the site limit caps the total power.
    bay = m.charge(energy="20", max_power="50", site_limit=site_limit)
    cars >> bay >> m.record(stat_name="charge_minutes", kind="tally", expr="entity.charge_time") >> m.dispose()
    results = m.run(seed=1)
    return float(results["user_summary"].set_index("Statistic").loc["charge_minutes", "Value"])


# Cars arrive about every 25 minutes, so two sometimes overlap. A 100 kW site feeds both at full power.
# A 60 kW site shares its power between them, which stretches those charges.
print(f"100 kW site limit: {mean_charge_minutes(100):.1f} minutes a charge")
print(f" 60 kW site limit: {mean_charge_minutes(60):.1f} minutes a charge")
