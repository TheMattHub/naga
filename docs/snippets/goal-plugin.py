import os
import subprocess
import sys
from pathlib import Path

# 1. The plugin is one Python file. It defines a block called INSPECT: wait, then send the entity
#    out of port 0 (passed) or port 1 (rejected).
Path("plugins").mkdir()
Path("plugins/inspect_plugin.py").write_text('''
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec


class Inspect(BaseProcess):
    def __init__(self, env, block, next_process=None, minutes=1.0, reject_rate=0.1):
        super().__init__(env, block, next_process)
        self.minutes = minutes
        self.reject_rate = reject_rate

    def self_process(self, entity):
        self.active_entity = entity
        yield self.env.timeout(self.minutes)
        self.remove_active_entity(entity)
        self.forward(entity, 1 if self.rng.random() < self.reject_rate else 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {"minutes": self.minutes, "reject_rate": self.reject_rate}

    def set_params_from_dict(self, params):
        self.minutes = params.get("minutes", self.minutes)
        self.reject_rate = params.get("reject_rate", self.reject_rate)


SPEC_INSPECT = BlockSpec(
    type_name="INSPECT", cls=Inspect, multi_output=True,
    params=(ParamSpec("minutes", float, 1.0), ParamSpec("reject_rate", float, 0.1)),
    factory=lambda env, block: Inspect(env, block),
)
NAGASIM_BLOCKS = [SPEC_INSPECT]
''', encoding="utf-8")

# 2. A script that uses it. It runs in a fresh Python, as your own scripts would. The engine reads
#    NAGASIM_PLUGIN_PATH and imports every file in that folder, once.
Path("use_plugin.py").write_text('''
from nagasim import api, build as nb, plugins

report = plugins.load_plugins()
print("loaded:", report.loaded, "failed:", report.failed)

# The new block is now a builder method, like the built-in ones. A rejected entity goes to a
# five-minute rework step; a passed one leaves at once.
m = nb.Model(time_limit=2000, warmup=0)
arrive = m.create(distribution=nb.Exponential(rate=0.5))
check = m.inspect(minutes=0.5, reject_rate=0.2)
rework = m.delay(distribution=nb.Constant(value=5))
arrive >> check
m.link(check, m.dispose(), source_port=0)
m.link(check, rework, source_port=1)
rework >> m.dispose()

result = api.run_model(m.to_dict(), replications=5, seed=1)
print(f"mean time in the model: {result.kpi('system/Avg Throughput Time'):.2f} minutes")
print(f"by hand: 0.5 + 0.2 x 5 = {0.5 + 0.2 * 5:.2f} minutes")
''', encoding="utf-8")
env = dict(os.environ, NAGASIM_PLUGIN_PATH=os.path.abspath("plugins"))
done = subprocess.run([sys.executable, "use_plugin.py"], env=env, capture_output=True, text=True)
print(done.stdout, end="")
print(done.stderr, end="")
