"""The models in raw SimPy: a process per customer, a list for the one statistic."""
import random

import simpy

from models import ARRIVAL_RATE


def run(stations, run_length, warmup, replications):
    """Mean time in system of each replication (customers who arrived after the warm-up)."""
    means = []
    for seed in range(replications):
        random.seed(seed)
        env = simpy.Environment()
        servers = [simpy.Resource(env, capacity=count) for count, _rate, _to in stations]
        times = []

        def customer():
            arrived, at = env.now, 0
            while at is not None:
                _count, rate, to = stations[at]
                with servers[at].request() as request:
                    yield request
                    yield env.timeout(random.expovariate(rate))
                # No draw where the next step is certain.
                draw, at = (random.random() if to and min(to.values()) < 1 else 0.0), None
                for station, probability in to.items():
                    if draw < probability:
                        at = station
                        break
                    draw -= probability
            if arrived >= warmup:
                times.append(env.now - arrived)

        def source():
            while True:
                yield env.timeout(random.expovariate(ARRIVAL_RATE))
                env.process(customer())

        env.process(source())
        env.run(until=run_length)
        means.append(sum(times) / len(times))
    return means
