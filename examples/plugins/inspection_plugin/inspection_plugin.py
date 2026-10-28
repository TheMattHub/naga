"""Example Naga plugin (ROADMAP 2.4 E): a worked example of the three
plugin kinds `nagasim.plugins.load_plugins` discovers, kept in the repo both
as documentation and as the test fixture for `tests/test_plugins.py`.

- Block INSPECT: an inspection delay, then pass/fail routing by `defect_rate`
  (two outputs: 0 = pass, 1 = fail); a defect increments a `count` user
  statistic named by `stat_name`.
- Distribution ShiftedExponential: `location + Exponential(rate)`.
- Exporter markdown: the system KPI table as a Markdown file.

See README.md in this folder for both ways to load this file (pip install,
or NAGASIM_PLUGIN_PATH) -- this single module supports both: SPEC_INSPECT/
DIST_SHIFTED_EXPONENTIAL/EXPORTER_MARKDOWN for entry-point loading, and the
NAGASIM_BLOCKS/NAGASIM_DISTRIBUTIONS/NAGASIM_EXPORTERS lists for the
no-install NAGASIM_PLUGIN_PATH path.

SECURITY: same trust level as any other Naga plugin (see
`nagasim/plugins.py`'s module docstring) -- arbitrary Python, run only from a
source you trust.
"""
from __future__ import annotations

from pathlib import Path

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, DIST_PARAMS, ParamSpec
from nagasim.core.distribution_spec import DistParam, DistributionSpec
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.stats import UserStat
from nagasim.exporters import Exporter
from nagasim.plugins import BlockUi

__all__ = ["Inspect", "SPEC_INSPECT", "DIST_SHIFTED_EXPONENTIAL", "EXPORTER_MARKDOWN",
          "NAGASIM_BLOCKS", "NAGASIM_DISTRIBUTIONS", "NAGASIM_EXPORTERS"]


# ---------------------------------------------------------------- block
class Inspect(BaseProcess):
    """Delay (the inspection time), then route pass (`next_process[0]`) or
    fail (`next_process[1]`) by `defect_rate`; a fail increments a `count`
    user statistic named `stat_name`."""

    def __init__(self, env, block, next_process=None, distribution="Constant",
                dist_params=None, time_unit="base", defect_rate=0.05, stat_name="defects"):
        super().__init__(env, block, next_process)
        self.delay = StochasticDistribution(distribution, dist_params or {"value": 1.0}, time_unit=time_unit)
        self.defect_rate = defect_rate
        self.stat_name = stat_name

    def _stat(self) -> UserStat:
        stats = self.ctx.user_stats
        existing = stats.get(self.stat_name)
        if existing is None:
            existing = UserStat.new(self.stat_name, "count", self.env.now)
            stats[self.stat_name] = existing
        return existing

    def self_process(self, entity):
        self.active_entity = entity
        yield self.env.timeout(self.sample_duration(self.delay, entity))
        self.remove_active_entity(entity)
        if self.rng.random() < self.defect_rate:
            self._stat().collector.add(1)
            self.forward(entity, 1)
        else:
            self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "distribution": self.delay.dist_type, "dist_params": self.delay.params,
            "time_unit": self.delay.time_unit, "defect_rate": self.defect_rate,
            "stat_name": self.stat_name,
        }

    def set_params_from_dict(self, param_dict):
        dist = param_dict.get("distribution", "Constant")
        params = param_dict.get("dist_params", {"value": 1.0})
        time_unit = param_dict.get("time_unit", "base")
        self.delay = StochasticDistribution(dist, params, time_unit=time_unit)
        self.defect_rate = param_dict.get("defect_rate", self.defect_rate)
        self.stat_name = param_dict.get("stat_name", self.stat_name)


SPEC_INSPECT = BlockSpec(
    type_name="INSPECT", cls=Inspect,
    params=DIST_PARAMS + (
        ParamSpec("defect_rate", float, 0.05),
        ParamSpec("stat_name", str, "defects"),
    ),
    factory=lambda env, block: Inspect(env, block),
    ui=BlockUi(color="#D9B8FF", shape="diamond", category="Quality"),
    multi_output=True,   # two outputs: 0 = pass, 1 = fail
)


# ---------------------------------------------------------------- distribution
def _sample_shifted_exponential(rng, p, **_):
    return float(p.get("location", 0.0) + rng.exponential(1.0 / p.get("rate", 1.0)))


DIST_SHIFTED_EXPONENTIAL = DistributionSpec(
    name="ShiftedExponential",
    params=(DistParam("location", 0.0), DistParam("rate", 1.0)),
    sampler=_sample_shifted_exponential,
    category="continuous",
    use_case="Exponential with a minimum value (location).\nParams: location (minimum), rate (lambda) > 0.",
    arrival=True, delay=True, support_lower=0.0,
)


# ---------------------------------------------------------------- exporter
def _write_markdown(results, path, **_):
    """System KPI table as Markdown."""
    df = results.get("system")
    lines = ["# Naga system KPIs", "", "| Metric | Value |", "|---|---|"]
    if df is not None:
        for _, row in df.iterrows():
            if row.get("Metric") == "Run":
                continue
            lines.append(f"| {row['Metric']} | {row['Value']} |")
    out = Path(path)
    out.write_text("\n".join(lines) + "\n")
    return out


EXPORTER_MARKDOWN = Exporter(name="markdown", extension=".md", write=_write_markdown)


# -- no-install NAGASIM_PLUGIN_PATH loading picks these up ------------------
NAGASIM_BLOCKS = [SPEC_INSPECT]
NAGASIM_DISTRIBUTIONS = [DIST_SHIFTED_EXPONENTIAL]
NAGASIM_EXPORTERS = [EXPORTER_MARKDOWN]
