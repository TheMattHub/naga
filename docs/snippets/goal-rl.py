import numpy as np

from nagasim import examples, rl

# 1. The environment: the routing model, paused at every decision of its "routing" policy.
#    An episode is one run of 200 minutes. The reward is minus the work in progress since the last
#    decision, so a policy that keeps the line short earns more.
model = examples.path("parallel_routing_policy")
env = rl.SimEnv(model, policy="routing", reward="neg_wip", horizon=200.0)
print("actions:", env.action_space.n, " observation values:", env.observation_space.shape[0])


def state(obs):
    """Round each queue and each busy flag down to 0..4, so the table stays small."""
    queue_a, queue_b, busy_a, busy_b = (min(int(v), 4) for v in obs)
    return (queue_a + busy_a, queue_b + busy_b)


# 2. Learn. Tabular Q-learning: a table of expected future reward for each (state, action),
#    nudged after every decision. The agent explores a random action 20% of the time.
rng = np.random.default_rng(0)
q = np.zeros((9, 9, env.action_space.n))
for episode in range(60):
    obs, _info = env.reset(seed=episode)
    s, done = state(obs), False
    while not done:
        a = int(rng.integers(env.action_space.n)) if rng.random() < 0.2 else int(q[s].argmax())
        obs, reward, terminated, truncated, _info = env.step(a)
        done = terminated or truncated
        s_next = state(obs)
        target = reward + (0 if done else 0.95 * q[s_next].max())
        q[s + (a,)] += 0.1 * (target - q[s + (a,)])
        s = s_next

# 3. Judge it on seeds it never trained on, against two rules of thumb.


def learned(request):
    return int(q[state(request.observation)].argmax())


for name, controller in (("random", "random"), ("shortest queue", "shortest"), ("learned", learned)):
    r = rl.evaluate_policy(model, controller, 10, seed=1000, policy="routing", horizon=200.0)
    print(f"{name:<15}{r['mean']:9.0f}  95% interval {r['lb']:.0f} to {r['ub']:.0f}")
