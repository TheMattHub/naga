from nagasim import build as nb

m = nb.Model(time_limit=100)
m.add_agent_type(
    name="Patient", states=[{"name": "Waiting"}, {"name": "Treated"}], initial_state="Waiting",
    transitions=[{"from": "Waiting", "to": "Treated", "message": "treated"}])
m.add_population(name="Patients", type="Patient", count=0, max_count=50)
m.create(distribution=nb.Constant(value=5), population="Patients", max_arrivals=10) \
    >> m.delay(distribution=nb.Constant(value=2)) >> m.send(message="treated") >> m.dispose()

results = m.run(seed=1)
print(results["agent_summary"].round(2).to_string(index=False))
