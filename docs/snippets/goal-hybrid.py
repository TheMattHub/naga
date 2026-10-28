from nagasim import build as nb


def help_desk(staff):
    """A help desk in three layers. `staff` agents each alternate between working and a break."""
    m = nb.Model(time_limit=2000, warmup=200)
    m.add_resource(name="Desk", capacity=staff)

    # Discrete events: tickets arrive, wait for a desk, and are worked. Arrivals speed up with
    # the SD level 'Frustration' (capped at 10), and a ticket takes longer when fewer staff are working.
    arrive = m.create(distribution=nb.CustomExpression(
        expression='EXPO(1 / (0.9 + 0.03 * min(LEVEL("Frustration"), 10)))'))
    work = m.process(resource_name="Desk", distribution=nb.CustomExpression(
        expression=f'EXPO(2.4 * {staff} / max(1, COUNT("Team", "Working")))'))
    arrive >> work >> m.dispose()

    # Agents: each staff member alternates between two states, with exponential dwell times.
    m.add_agent_type(name="Staff", initial_state="Working",
                     states=[{"name": "Working"}, {"name": "OnBreak"}],
                     transitions=[{"from": "Working", "to": "OnBreak", "timeout": "EXPO(60)"},
                                  {"from": "OnBreak", "to": "Working", "timeout": "EXPO(15)"}])
    m.add_population(name="Team", type="Staff", count=staff)

    # System dynamics: frustration rises with the number of tickets waiting and fades by itself.
    m.add_level(name="Frustration", initial=0, min=0)
    m.add_flow(name="annoyance", to="Frustration", rate='0.1 * NQ("Desk")')
    m.add_flow(name="calming", **{"from": "Frustration"}, rate='0.05 * LEVEL("Frustration")')
    return m


for staff in (4, 5):
    results = help_desk(staff).run(replications=5, seed=1)
    print(f"{staff} staff: {results.kpi('agent/Team/Working/Avg Count'):.2f} working, "
          f"mean wait {results.kpi('resource/Desk/Time in Queue Avg'):.1f} minutes, "
          f"frustration {results.kpi('level/Frustration/Average'):.2f}")