import ortools  # the optional solver: pip install "nagasim[plan]"
import json

from nagasim import api, cpsat, examples, planning

# Five orders on a cutter and a packer. One entity per order, so each planned start is exact.
shop = json.loads(examples.path("order_priority_shop").read_text(encoding="utf-8"))
for order in shop["orders"]:
    order["quantity"] = 1
shop["resources"][0]["discipline"] = "FIFO"       # the model as it runs today


def lateness(layout):
    run = api.run_model(layout, replications=1, deterministic=True)     # durations are constants here
    return run["order_summary"].set_index("Order")["Lateness"]


today = lateness(shop)
print("lateness today:", " ".join(f"{order} {late:+.0f}" for order, late in today.items()))

# CP-SAT sequences the order book. Without 'ortools' installed this call raises ImportError.
plan = cpsat.propose(shop, objective="weighted_tardiness", time_limit_s=5)
print(f"solver: {plan.status}, tardiness {plan.objective:.0f}, makespan {plan.makespan:.0f}, "
      f"{len(plan.refused)} refused")

# The plan is a layout. Run it to check the solver's claim against the simulation.
planned = lateness(plan.layout)
print("lateness planned:", " ".join(f"{order} {late:+.0f}" for order, late in planned.items()))

# Hand the plan to a planning system as a CSV.
log = api.run_model(plan.layout, replications=1, deterministic=True)["schedule_log"]
planning.plan_export(log, "plan.csv")
print(open("plan.csv", encoding="utf-8").read().splitlines()[0])
print(len(log), "operations written")
