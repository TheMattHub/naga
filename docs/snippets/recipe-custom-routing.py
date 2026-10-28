import types

from nagasim import build as nb


class ByWeight:
    """Light parcels go down link 0, heavy ones down link 1. The links are numbered in the order
    they are drawn from the LOGIC block."""

    def execute(self, entity, ctx):
        return 0 if entity.weight <= 5 else 1


rules = types.ModuleType("my_rules")           # in a project this is your own module: run(logic=my_rules)
rules.ByWeight = ByWeight

m = nb.Model(time_limit=200)
parcels = m.create(distribution=nb.Constant(value=2))
weigh = m.assign(assignments=[{"target": "entity.weight", "expr": "UNIF(0, 10)"}])
sorter = m.logic("Sorter", logic_class="ByWeight")
parcels >> weigh >> sorter
sorter >> m.dispose("Light lane")
sorter >> m.dispose("Heavy lane")

results = m.run(seed=1, logic=rules)
flow = results["material_flow_summary"]
print(flow[flow["From"] == "Sorter"][["From", "To", "Entities"]].to_string(index=False))
