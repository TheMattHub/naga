from nagasim import build as nb

m = nb.Model(time_limit=60, time_unit="seconds")
arrive = m.create(distribution=nb.Constant(value=1))
feed = m.conveyor(length=2.0, speed=0.5, spacing=0.5)
popup = m.conveyor_transfer(mechanism="popup", exits=[{"port": "straight", "angle": 0},
                                                      {"port": "side", "angle": 90}])
straight = m.conveyor(length=3.0, speed=0.5, spacing=0.5, eyes=[{"at": 0.3}])
side = m.conveyor(length=3.0, speed=0.5, spacing=0.5, eyes=[{"at": 0.3}])
done = m.dispose()
arrive >> feed >> popup
m.link(popup, straight, source_port="straight")
m.link(popup, side, source_port="side")
straight >> done
side >> done

results = m.run(seed=1)
print(results["conveyor_summary"][["Conveyor", "Entered", "Exited"]].to_string(index=False))
