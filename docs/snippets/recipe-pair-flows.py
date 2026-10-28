from nagasim import build as nb

# Riders and drivers arrive at random. MATCH with number_to_match=2 holds each one until the other
# kind is there, then releases the pair together. Riders come every 3 minutes and drivers every 4,
# so riders queue for a driver and some are still waiting when the run ends.
m = nb.Model(time_limit=300)
m.add_entity(name="RIDER")
m.add_entity(name="DRIVER")
riders = m.create("Riders", e_type="RIDER", distribution=nb.Exponential(rate=1 / 3))
drivers = m.create("Drivers", e_type="DRIVER", distribution=nb.Exponential(rate=1 / 4))
ride = m.match("Ride", number_to_match=2)
riders >> ride
drivers >> ride
ride >> m.delay(distribution=nb.Constant(value=1)) >> m.dispose()

summary = m.run(seed=1)["entity_summary"]
print(summary[["Entity Type", "Total Created", "Total Completed", "Avg Time"]].round(1).to_string(index=False))
