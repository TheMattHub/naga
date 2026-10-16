"""A tank filled by a pump on a timer and drawn from steadily (rates): the share of time it is empty.

Derivation. Nothing is random. In the first 20 minutes of an hour the level rises at
25 - 10 = 15 litres a minute, to 300 litres. The pump then stops and the draw empties the tank
at 10 litres a minute, in 30 minutes. It stays empty for the last 10 minutes of the hour, and
the next hour starts as the first did: 10 / 60 = 16.67% of the time, in every hour.

Tolerance. No sampling error; a fixed time step h misplaces the moment the tank runs empty by
at most h in each hour, which is h / 60 of the time: 0.5 percentage points accepts any step up
to 0.3 minutes and refuses a one-minute step that is off by a whole step.
"""
import sys

import simpy

REQUEST = (
    "A water tank starts empty. A pump fills it at 25 litres per minute, but it is on a timer "
    "and only runs during the first 20 minutes of every hour. The site draws a steady 10 litres "
    "per minute from the tank whenever there is water in it, also while the pump runs; when the "
    "tank is empty and the pump is off, nothing is drawn. The tank is large enough never to "
    "overflow. Simulate 100 hours and tell me what percentage of the time the tank is empty.")
PUMP, DRAW, ON, HOUR, RUN = 25.0, 10.0, 20.0, 60.0, 6_000
ANSWER = 100 * (HOUR - ON - (PUMP - DRAW) * ON / DRAW) / HOUR
TOLERANCE = 0.5

NAGA = [
    ("write_model", {"layout": {
        # the pump's timer as a repeating step profile: switched exactly, no step grid
        "timeseries_inputs": [{"name": "pump", "times": [0, ON], "values": [PUMP, 0],
                               "beyond": "repeat", "period": HOUR}],
        "levels": [{"name": "Tank", "initial": 0}],
        "flows": [{"name": "pump", "to": "Tank", "rate": 'TS("pump")'},
                  {"name": "draw", "from": "Tank", "rate": DRAW}],
        "simulation": {"time_limit": RUN, "warmup": 0, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
    ("get_kpi_table", {"frame": "level_summary"}),
]


# The builder arm's reference: a script that builds the model with nagasim.build and runs it.
BUILDER = """\
from nagasim import build as nb

m = nb.Model(time_limit=6_000, warmup=0, replications=1)
m.add_timeseries_input(name="pump", times=[0, 20], values=[25, 0], beyond="repeat", period=60)
m.add_level(name="Tank", initial=0)
m.add_flow(name="pump", to="Tank", rate='TS("pump")')
m.add_flow(name="draw", **{"from": "Tank"}, rate=10)
kpis = m.run().kpis().set_index("metric")["mean"]
print(f"ANSWER: {kpis['level/Tank/Time Empty %']:.4f}")
"""


def naga_answer(results):
    return results[-1]["rows"][0]["Time Empty %"]


def simulate(seed=0):
    """Event by event: the level is linear between the moments a rate changes."""
    env, state = simpy.Environment(), {"level": 0.0, "empty": 0.0}

    def hours():
        while True:
            state["level"] += (PUMP - DRAW) * ON
            yield env.timeout(ON)
            draining = min(state["level"] / DRAW, HOUR - ON)
            state["level"] -= DRAW * draining
            state["empty"] += HOUR - ON - draining
            yield env.timeout(HOUR - ON)

    env.process(hours())
    env.run(until=RUN)
    return 100 * state["empty"] / RUN


if __name__ == "__main__":
    print(f"ANSWER: {simulate():.4f}")
