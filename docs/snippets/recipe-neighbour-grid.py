from nagasim import build as nb

# Three houses stand side by side in one row of a 6 x 6 grid. A house's heat is its x position at the
# cell centre (1.5, 2.5, 3.5). Each tick it adds up and averages the heat of the houses in the eight
# cells around it. Neighbours are cells, not distances, and the agent's own cell is never counted.
m = nb.Model(time_limit=3)
m.add_agent_type(
    name="House", initial_state="On", states=[{"name": "On"}],
    attributes=[{"name": "heat", "init": "self.x"}, {"name": "around", "init": "0"},
                {"name": "mean_around", "init": "0"}],
    space={"kind": "grid", "size": [6, 6], "torus": False, "neighbourhood": "moore"},
    tick={"dt": 1},
    on_tick=[{"target": "self.around", "expr": 'NEIGHBOUR_SUM("Street", "heat")'},
             {"target": "self.mean_around", "expr": 'NEIGHBOUR_MEAN("Street", "heat")'}])
m.add_population(name="Street", type="House", count=3,
                 placement={"kind": "table", "points": [[1, 2], [2, 2], [3, 2]]})

attributes = m.run(seed=1)["agent_attributes"]
everyone = attributes[attributes["State"] == "All"]
print(everyone[["Attribute", "End Min", "End Max"]].to_string(index=False))
