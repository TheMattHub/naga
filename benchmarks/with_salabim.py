"""The models in salabim, at its defaults: a component per customer, resources that keep
their own monitors, one monitor for the time in system."""
import salabim as sim

from models import ARRIVAL_RATE


def run(stations, run_length, warmup, replications):
    """Mean time in system of each replication (customers who arrived after the warm-up)."""
    means = []
    for seed in range(replications):
        env = sim.Environment(random_seed=seed, trace=False)
        servers = [sim.Resource(capacity=count) for count, _rate, _to in stations]
        service = [sim.Exponential(rate=rate) for _count, rate, _to in stations]
        # -1 is "leave the system"; no draw where the next step is certain.
        routes = [sim.Pdf([*to, -1], [*to.values(), 1 - sum(to.values())])
                  if to and min(to.values()) < 1 else sim.Constant(next(iter(to), -1))
                  for _count, _rate, to in stations]
        times = sim.Monitor("time in system")

        class Customer(sim.Component):
            def process(self):
                arrived, at = env.now(), 0
                while at >= 0:
                    self.request(servers[at])
                    self.hold(service[at].sample())
                    self.release()
                    at = routes[at].sample()
                if arrived >= warmup:
                    times.tally(env.now() - arrived)

        sim.ComponentGenerator(Customer, iat=sim.Exponential(rate=ARRIVAL_RATE))
        env.run(till=run_length)
        means.append(times.mean())
    return means
