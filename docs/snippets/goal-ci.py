from nagasim import build as nb, cli

# A queue with three claims about how it behaves. Each is an assertion: a KPI, a comparison and a
# value, checked over the model's own replications with a confidence interval.
m = nb.Model(time_limit=480, warmup=0, assertions=[
    {"name": "server_not_saturated", "kpi": "resource/Server/Usage % (snapshot)", "op": "<=",
     "value": 95, "replications": 20},
    {"name": "wait_under_five_minutes", "kpi": "resource/Server/Time in Queue Avg", "op": "<=",
     "value": 5, "replications": 20},
    {"name": "wait_under_two_minutes", "kpi": "resource/Server/Time in Queue Avg", "op": "<=",
     "value": 2, "replications": 20},
])
m.add_resource(name="Server")
arrive = m.create(distribution=nb.Exponential(rate=1.0))
arrive >> m.process(resource_name="Server", distribution=nb.Exponential(rate=1.25)) >> m.dispose()
m.save("model.json")

# What a CI job runs. The exit code is the job's verdict: 0 passes the build, 1 fails it.
print("$ naga check model.json --junit report.xml")
code = cli.main(["check", "model.json", "--junit", "report.xml"])
print("exit code:", code)

# The workflow file that runs it on every push.
print("$ naga ci-template --path models/")
cli.main(["ci-template", "--path", "models/"])
