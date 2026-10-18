import statistics

import salabim as sim

LAM, MU, RUN, WARMUP = 1.0, 1.25, 5500, 500      # per minute, and minutes

means = []
for seed in range(5):
    env = sim.Environment(random_seed=seed, trace=False)
    server = sim.Resource("server", capacity=1)
    times = sim.Monitor("time in system")

    class Customer(sim.Component):
        def process(self):
            arrived = env.now()
            self.request(server)
            self.hold(sim.Exponential(rate=MU).sample())
            self.release()
            if arrived >= WARMUP:
                times.tally(env.now() - arrived)

    sim.ComponentGenerator(Customer, iat=sim.Exponential(rate=LAM))
    env.run(till=RUN)
    means.append(times.mean())
print(f"salabim: mean time in system {statistics.mean(means):.2f} minutes")
