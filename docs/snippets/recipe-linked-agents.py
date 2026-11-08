from nagasim import build as nb

# Four shops sit on a line, each linked to its two ring neighbours, each with a stock equal to its
# id + 1. Every day each shop reads its links (LINKED, LINK_SUM, LINK_MEAN, IS_LINKED), its two
# nearest shops (KNN_SUM, KNN_MEAN, NEAREST) and whether shop 0 is still there (ALIVE). DISTANCE
# measures the road from the depot to the yard on the path network.
m = nb.Model(time_limit=2,
             network={"nodes": [{"name": "depot", "x": 0, "y": 0}, {"name": "yard", "x": 30, "y": 40}],
                      "segments": [{"from": "depot", "to": "yard", "two_way": True}]})
reads = {"links": 'LINKED("Shops")', "link_sum": 'LINK_SUM("stock")', "link_mean": 'LINK_MEAN("stock")',
         "next_linked": 'IS_LINKED(AGENT("Shops", (self.id + 1) % 4))',
         "knn_sum": 'KNN_SUM("Shops", "stock", 2)', "knn_mean": 'KNN_MEAN("Shops", "stock", 2)',
         "nearest": 'NEAREST("Shops")', "first_alive": 'ALIVE(AGENT("Shops", 0))',
         "road": 'DISTANCE("depot", "yard")'}
m.add_agent_type(
    name="Shop", initial_state="Open", states=[{"name": "Open"}],
    attributes=[{"name": "stock", "init": "self.id + 1"}] + [{"name": n, "init": "0"} for n in reads],
    space={"kind": "continuous", "bounds": [0, 0, 100, 100]},
    tick={"dt": 1}, on_tick=[{"target": f"self.{n}", "expr": e} for n, e in reads.items()])
m.add_population(name="Shops", type="Shop", count=4, network={"kind": "small_world", "k": 2, "beta": 0},
                 placement={"kind": "table", "points": [[0, 0], [10, 0], [20, 0], [40, 0]]})

log = m.run(seed=1)["agent_attribute_log"]          # each attribute at the end, shop by shop
print("shop       " + "".join(f"{i:>6}" for i in range(4)))
for name in ["stock", *reads]:
    values = log[log["Attribute"] == name]["Value"]
    print(f"{name:<11}" + "".join(f"{v:6g}" for v in values))
