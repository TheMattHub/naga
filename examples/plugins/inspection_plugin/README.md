# Naga inspection plugin (example)

Worked example of Naga's plugin system: one block
(`INSPECT`), one distribution (`ShiftedExponential`), one exporter
(`markdown`), all in `inspection_plugin.py`.

## Install path 1: pip

```
pip install -e examples/plugins/inspection_plugin
```

`pyproject.toml` declares the three entry points Naga looks for
(`nagasim.blocks`, `nagasim.distributions`, `nagasim.exporters`). Naga
picks them up the next time `nagasim.plugins.load_plugins()` runs (the
launcher's first call, or your own script's first `run_model()` call) --
no other configuration.

## Install path 2: no install, NAGASIM_PLUGIN_PATH

```
set NAGASIM_PLUGIN_PATH=C:\path\to\examples\plugins\inspection_plugin
product
```

Every `*.py` file directly inside a directory listed in `NAGASIM_PLUGIN_PATH`
(`os.pathsep`-separated, several directories allowed) is imported; Naga
reads its `NAGASIM_BLOCKS` / `NAGASIM_DISTRIBUTIONS` / `NAGASIM_EXPORTERS`
module-level lists, if present. This module defines all three, so it works
from either path with no changes.

## What you get

- **INSPECT**: an inspection delay (any distribution, like DELAY), then
  routes to output 0 (pass) or output 1 (fail) with probability
  `defect_rate`; a fail increments a `count` user statistic named
  `stat_name` (default `"defects"`).
- **ShiftedExponential**: `location + Exponential(rate)`; offered anywhere
  a distribution combo appears.
- **markdown**: `naga run model.json --export markdown:out.md` writes the
  system KPI table as a Markdown table.

## Security

A plugin is arbitrary Python, imported and run with the same trust as
`nagasim/core/custom_logic.py`. Only install or point `NAGASIM_PLUGIN_PATH`
at code you trust.
