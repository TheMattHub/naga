from nagasim import agent_tools
from nagasim.agent_tools import call

# The workspace is the folder an agent may reach. `naga mcp --root .` does the same line.
agent_tools.use_workspace(agent_tools.Workspace("."))

# 1. What an agent does first: build a one-server queue, one tool call per edit.
call("new_model", name="shop")
call("upsert_record", model="shop", section="resources", record={"name": "Server", "capacity": 1})
call("add_block", model="shop", type="CREATE", name="arrive",
     params={"distribution": "Exponential", "dist_params": {"rate": 1.0}})
call("add_block", model="shop", type="PROCESS", name="serve",
     params={"resource_name": "Server", "distribution": "Exponential", "dist_params": {"rate": 1.25}})
call("add_block", model="shop", type="DISPOSE", name="leave")
call("connect", model="shop", source="arrive", target="serve")
linked = call("connect", model="shop", source="serve", target="leave")
call("set_simulation_settings", model="shop", run_length=2000, warmup=200, replications=10)
print("errors after the last link:", linked["errors"], " valid:", call("validate_model", model="shop")["ok"])

# 2. An error comes back as a value with a code and a hint, never as a traceback.
refused = call("open_model", path="../somewhere/else.json")
print("refused:", refused["error"]["code"])

# 3. Run it. The answer is a digest: each KPI with the half-width of its 95% interval.
digest = call("run_model", model="shop", seed=1)
wait = digest["resources"]["Server"]["Time in Queue Avg"]
print(f"mean wait: {wait['mean']:.2f} +/- {wait['ci_half_width']:.2f} minutes")

# 4. Ask a question without editing: what if there were two servers? The tool doubles the
#    replications on paired seeds until the answer is clear.
asked = call("what_if", model="shop", overrides=[{"path": "resources.Server.capacity", "value": 2}],
             kpis=["resource/Server/Time in Queue Avg"], start=5, cap=20, seed=1)
row = asked["kpis"][0]
print(f"two servers: wait changes by {row['diff']:.2f} (95% interval {row['diff_lb']:.2f} to "
      f"{row['diff_ub']:.2f}): {row['verdict']}, after {asked['n']} replications")
