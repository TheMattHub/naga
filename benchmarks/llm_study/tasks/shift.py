"""A robot that works 65 minutes and charges 35 (a capacity schedule): the mean time of a parcel.

Derivation. Nothing is random: a parcel every 10 minutes, 4 minutes of packing, the robot
available during the first 65 minutes of every 100. Take one cycle with parcels at 0, 10, ...,
90. Those at 0 to 60 are packed at once (the one at 60 is done at 64, before the robot leaves at
65): 4 minutes each. Those at 70, 80 and 90 wait until 100, where the next cycle's parcel at 100
joins them; they are packed one after the other and leave at 104, 108 and 112 (34, 28 and 22
minutes). The backlog delays the parcels at 100 and 110, which leave at 116 and 120 (16 and 10
minutes), and the parcel at 120 starts at once. So a cycle's ten parcels take
34 + 28 + 22 + 16 + 10 + 5 * 4 = 130 minutes: 13 minutes on average.

Tolerance. No sampling error; only the ends of a 10,000-minute run differ from the cycle (the
first cycle starts with no backlog, the last parcels are cut off). Each end moves the sum over
about 1,000 parcels by at most the longest stay (34 minutes), so the mean by at most 0.07;
0.25 leaves room for where an implementation puts its first arrival.
"""
import sys

import simpy

REQUEST = (
    "A packing robot works around the clock in a fixed rhythm: it packs for 65 minutes, then "
    "recharges for 35 minutes, and so on. A parcel arrives exactly every 10 minutes and packing "
    "one parcel takes exactly 4 minutes; parcels that arrive while the robot is recharging or "
    "busy wait in line, first come first served. Simulate 10,000 minutes and tell me the "
    "average time a parcel spends from its arrival until it is packed, in minutes.")
GAP, PACK, ON, OFF = 10.0, 4.0, 65.0, 35.0
RUN = 10_000
ANSWER = (34 + 28 + 22 + 16 + 10 + 5 * PACK) / 10
TOLERANCE = 0.25

NAGA = [
    ("write_model", {"layout": {
        "schedules": [{"name": "Rhythm", "kind": "capacity", "repeat": True,
                       "points": [[ON, 1], [OFF, 0]]}],
        "resources": [{"name": "Robot", "capacity": 1, "capacity_schedule": "Rhythm"}],
        "blocks": [{"name": "parcels", "type": "CREATE", "params": {"distribution": "Constant", "dist_params": {"value": GAP}}},
                   {"name": "pack", "type": "PROCESS",
                    "params": {"resource_name": "Robot", "distribution": "Constant", "dist_params": {"value": PACK}}},
                   {"name": "packed", "type": "DISPOSE"}],
        "links": [{"source": "parcels", "target": "pack"},
               {"source": "pack", "target": "packed"}],
        "simulation": {"time_limit": RUN, "warmup": 0, "replications": 1, "time_unit": "minutes"}}}),
    ("run_model", {}),
]


# The builder arm's reference: a script that builds the model with nagasim.build and runs it.
BUILDER = """\
from nagasim import build as nb

m = nb.Model(time_limit=10_000, warmup=0, replications=1)
m.add_schedule(name="Rhythm", kind="capacity", repeat=True, points=[[65, 1], [35, 0]])
m.add_resource(name="Robot", capacity_schedule="Rhythm")
parcels = m.create(distribution=nb.Constant(value=10.0))
pack = m.process(resource_name="Robot", distribution=nb.Constant(value=4.0))
parcels >> pack >> m.dispose()
kpis = m.run().kpis().set_index("metric")["mean"]
print(f"ANSWER: {kpis['system/Avg Throughput Time']:.4f}")
"""


def naga_answer(results):
    return results[-1]["system"]["Avg Throughput Time"]["mean"]


def simulate(seed=0):
    env, times = simpy.Environment(), []
    robot = simpy.PriorityResource(env, capacity=1)

    def parcel():
        arrived = env.now
        with robot.request(priority=1) as turn:
            yield turn
            yield env.timeout(PACK)
        times.append(env.now - arrived)

    def arrivals():
        while True:
            yield env.timeout(GAP)
            env.process(parcel())

    def rhythm():
        while True:
            yield env.timeout(ON)
            with robot.request(priority=0) as charging:     # ahead of every waiting parcel
                yield charging
                yield env.timeout(OFF)

    env.process(arrivals())
    env.process(rhythm())
    env.run(until=RUN)
    return sum(times) / len(times)


if __name__ == "__main__":
    print(f"ANSWER: {simulate():.4f}")
