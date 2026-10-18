import math

from nagasim import build as nb

N, SICK, BETA, GAMMA, DAYS = 500, 10, 0.25, 0.1, 150      # rates are per day

m = nb.Model(time_limit=DAYS, time_unit="days", replications=10)
m.add_agent_type(
    name="Person", initial_state="S", parameters={"beta": BETA, "gamma": GAMMA},
    states=[{"name": "S"}, {"name": "I"}, {"name": "R"}],
    transitions=[{"from": "S", "to": "I", "rate": 'beta * COUNT("People", "I") / COUNT("People")'},
                 {"from": "I", "to": "R", "timeout": "EXPO(1 / gamma)"}])
m.add_population(name="People", type="Person", count=N, initial=[{"state": "I", "count": SICK}])

recovered = m.run(seed=0)["agent_summary"].query("State == 'R'")["Final"]
print(f"Naga: share ever infected {recovered.mean() / N:.3f}")

z = 0.5                                    # the final-size relation z = 1 - exp(-R0 z), solved by iteration
for _ in range(100):
    z = 1 - math.exp(-BETA / GAMMA * z)
print(f"exact, for a large population: {z:.3f}")
