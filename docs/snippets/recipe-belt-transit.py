from nagasim import build as nb


def belt_run(speed):
    m = nb.Model(time_limit=600, time_unit="seconds")
    m.add_resource(name="Packer")
    parts = m.create(distribution=nb.Constant(value=10))
    # A 30 m belt; `spacing` is the least gap between parts on it, in the same length unit.
    belt = m.conveyor(length=30, speed=speed, spacing=1.0)
    parts >> belt >> m.process(resource_name="Packer", distribution=nb.Constant(value=4)) >> m.dispose()
    return m.run(seed=1)["conveyor_summary"].iloc[0]


for speed in (1.0, 2.0):
    row = belt_run(speed)
    print(f"belt at {speed} m/s: transit {row['Avg Transit Time']:.1f} s, "
          f"{row['Avg Occupancy']:.1f} parts on the belt on average")
