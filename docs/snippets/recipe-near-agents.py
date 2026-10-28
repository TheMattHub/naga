from nagasim import build as nb

# Three sensors sit 2 m apart and each carries a load equal to its x position (10, 12 and 14).
# Every tick each one reads the loads of the others within 5 m: their sum, smallest, largest and mean.
# `self` is left out of the neighbourhood. With nobody in range MIN and MAX are NaN, so guard them
# with an IF on NEAR("Field", 5) > 0 when agents can be alone.
m = nb.Model(time_limit=3)
m.add_agent_type(
    name="Sensor", initial_state="On", states=[{"name": "On"}],
    attributes=[{"name": "load", "init": "self.x"}, {"name": "crowd", "init": "0"},
                {"name": "lightest", "init": "0"}, {"name": "heaviest", "init": "0"},
                {"name": "average", "init": "0"}],
    space={"kind": "continuous", "bounds": [0, 0, 100, 100]},
    tick={"dt": 1},
    on_tick=[{"target": "self.crowd", "expr": 'NEAR_SUM("Field", "load", 5)'},
             {"target": "self.lightest", "expr": 'NEAR_MIN("Field", "load", 5)'},
             {"target": "self.heaviest", "expr": 'NEAR_MAX("Field", "load", 5)'},
             {"target": "self.average", "expr": 'NEAR_MEAN("Field", "load", 5)'}])
m.add_population(name="Field", type="Sensor", count=3,
                 placement={"kind": "table", "points": [[10, 10], [12, 10], [14, 10]]})

attributes = m.run(seed=1)["agent_attributes"]
everyone = attributes[attributes["State"] == "All"]
print(everyone[["Attribute", "End Min", "End Max"]].to_string(index=False))
