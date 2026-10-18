import math

import mesa

N, SICK, BETA, GAMMA, DAYS = 500, 10, 0.25, 0.1, 150      # rates are per day


class Person(mesa.Agent):
    def __init__(self, model, state):
        super().__init__(model)
        self.state = state

    def step(self):
        if self.state == "S":
            infected = sum(a.state == "I" for a in self.model.agents)
            if self.random.random() < 1 - math.exp(-BETA * infected / N):
                self.state = "I"
        elif self.state == "I" and self.random.random() < 1 - math.exp(-GAMMA):
            self.state = "R"


class Epidemic(mesa.Model):
    def __init__(self, seed):
        super().__init__(rng=seed)
        for i in range(N):
            Person(self, "I" if i < SICK else "S")

    def step(self):
        self.agents.shuffle_do("step")


recovered = []
for seed in range(10):
    model = Epidemic(seed)
    for _ in range(DAYS):
        model.step()
    recovered.append(sum(a.state == "R" for a in model.agents) / N)
print(f"Mesa: share ever infected {sum(recovered) / len(recovered):.3f}")
