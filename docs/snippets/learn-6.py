from nagasim import build as nb, experiments

m = nb.Model(time_limit=480, warmup=0)         # one 8-hour day, in minutes
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))          # a customer a minute
serve = m.process(resource_name="Desks", distribution=nb.Exponential(rate=1.25))
arrive >> serve >> m.dispose()
m.add_schedule(name="afternoon", kind="capacity", points=[[240, 0], [240, 1]])   # closed 4 h, open 4 h
m.add_resource(name="Second", capacity=1, capacity_schedule="afternoon")
m.add_resource_set(name="Desks", members=["Server", "Second"], rule="PreferredOrder")
m.add_entity(name="Watcher")
m.add_level(name="Tank", capacity=100, initial=20,
            thresholds=[{"value": 50, "direction": "rising", "signal": "half full"}])
m.add_flow(name="fill", to="Tank", rate="5")                       # litres a minute in
m.add_flow(name="draw", **{"from": "Tank"}, rate="2")              # and out
watch = m.create(e_type="Watcher", distribution=nb.Constant(value=0.0), max_arrivals=1)
watch >> m.hold(mode="Signal", signal_name="half full") >> m.dispose()
m.add_agent_type(name="Staff", initial_state="Working",
                 states=[{"name": "Working"}, {"name": "OnBreak"}],
                 transitions=[{"from": "Working", "to": "OnBreak", "timeout": "EXPO(120)"},
                              {"from": "OnBreak", "to": "Working", "timeout": "EXPO(15)"}])
m.add_population(name="Team", type="Staff", count=4)
m.add_scenario(name="Faster server",
               overrides=[{"path": f"blocks.{serve.name}.params.dist_params.rate", "value": 1.6}])

results = m.run(replications=20, seed=1)
kpis = results.kpis().set_index("metric")
print(kpis.loc[["resource/Desks/Time in Queue Avg", "resource/Desks/Usage % (snapshot)"], ["mean", "ci95"]])
log = results["entity_log"]
print(log[log["Run"] == 1].head(4)[["Entity ID", "Type", "Created At", "Exited At"]].to_string(index=False))
print(results["resource_log"].head(3)[["Resource", "time", "in_use", "queue_length", "event_type"]].to_string(index=False))
