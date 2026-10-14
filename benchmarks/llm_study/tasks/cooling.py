"""A cup of coffee cooling (system dynamics, one stock): its temperature after 15 minutes.

Derivation. dT/dt = -k (T - room) has the solution T(t) = room + (T0 - room) exp(-k t):
20 + 70 exp(-1.5) = 35.62 degrees.

Tolerance. No sampling error; the error is the integration step's. Euler with a step h gives
room + (T0 - room) (1 - k h)^(t / h): 35.32 with h = 0.25 (0.30 low), 34.41 with h = 1 (1.21
low). 0.5 accepts a quarter-minute Euler step or anything finer and refuses a one-minute step.
"""
import math
import sys

import simpy

REQUEST = (
    "A cup of coffee at 90 degrees Celsius is left in a room at 20 degrees. It loses heat in "
    "proportion to how much hotter it is than the room: every minute, at a rate of 10% of the "
    "current difference per minute (so 7 degrees per minute at the start, and less and less as "
    "it cools). Model this as a continuous process and tell me the coffee's temperature after "
    "15 minutes, in degrees Celsius.")
START, ROOM, RATE, END = 90.0, 20.0, 0.1, 15.0
ANSWER = ROOM + (START - ROOM) * math.exp(-RATE * END)
TOLERANCE = 0.5

NAGA = [
    ("write_model", {"layout": {
        "levels": [{"name": "Temperature", "initial": START}],
        "flows": [{"name": "cooling", "from": "Temperature",
                   "rate": f'{RATE} * (LEVEL("Temperature") - {ROOM})'}],
        "simulation": {"time_limit": END, "warmup": 0, "replications": 1, "time_unit": "minutes",
                       "sd": {"dt": 0.05, "method": "rk4"}}}}),
    ("run_model", {}),
    ("get_kpi_table", {"frame": "level_summary"}),
]


def naga_answer(results):
    return results[-1]["rows"][0]["Final"]


def simulate(seed=0, step=0.001):
    env, coffee = simpy.Environment(), [START]

    def cool():
        while True:
            yield env.timeout(step)
            coffee[0] -= RATE * (coffee[0] - ROOM) * step

    env.process(cool())
    env.run(until=END + step / 2)       # past the last step, whatever its rounding
    return coffee[0]


if __name__ == "__main__":
    print(f"ANSWER: {simulate():.4f}")
