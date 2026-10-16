"""Results: what `api.run_model` returns (PHASE5 O6, notebook API).

A `dict` subclass, not a wrapper -- every existing caller (`results["system"]`,
`isinstance(results, dict)`, a pool worker pickling a plain KPI dict) keeps
working unchanged. `Results` is built once, in `api.run_model`, from the same
`dict[str, pd.DataFrame]` it always returned; nothing here changes an engine
output, only how it displays in a notebook.

    results = run_model("models/single_server.json", replications=5)
    results                    # _repr_html_ in Jupyter: the KPI summary table
    results.kpis()             # tidy DataFrame: metric, mean, std, ci95
    results.kpi("system/Avg Queue Length")   # one KPI's mean
    results.frame("system")    # alias for results["system"]
    results.plot("system/Avg Queue Length")   # lazy matplotlib
    results.to_excel("out.xlsx")

Top-level module beside `api.py`/`experiments.py` (PHASE5 decision 6: the
notebook API is engine/analysis, not UI) -- pandas only; matplotlib is
imported lazily inside `.plot()`, never a hard dependency.
"""
from __future__ import annotations

import pandas as pd

from nagasim.core.stats import ci95

__all__ = ["Results"]


class Results(dict):
    """`dict[str, pd.DataFrame]` (the nine-to-twenty KPI frames, one "Run"
    column each) plus notebook conveniences."""

    def frame(self, name: str) -> pd.DataFrame:
        """Alias for `results[name]`, for readability in a notebook."""
        return self[name]

    def kpis(self) -> pd.DataFrame:
        """Tidy KPI DataFrame -- metric, mean, std, ci95 (half-width) over
        runs -- built from `experiments.flatten_kpis`'s stable "family/name"
        keys (e.g. "system/Avg Queue Length", "resource/Server/Usage %
        (snapshot)")."""
        # Local import: experiments.py imports nagasim.api, so importing it at
        # module level here would make api.py -> results.py -> experiments.py
        # -> api.py a cycle.
        from nagasim.experiments import flatten_kpis

        flat = flatten_kpis(self)
        rows = []
        for metric, grp in flat.groupby("KPI", sort=False):
            st = ci95(grp["Value"])
            half = st["ub"] - st["mean"]
            rows.append({"metric": metric, "mean": st["mean"], "std": st["sd"],
                        "ci95": half})
        return pd.DataFrame(rows, columns=["metric", "mean", "std", "ci95"])

    def kpi(self, key: str) -> float:
        """The mean over runs of one KPI, by its "family/name" key
        (`results.kpi("system/Avg Queue Length")`); `KeyError` naming the key if absent.
        The spread is in `kpis()`."""
        table = self.kpis()
        hit = table.loc[table["metric"] == key, "mean"]
        if hit.empty:
            raise KeyError(f"{key!r} is not a KPI key in these results; see results.kpis()")
        return float(hit.iloc[0])

    def _repr_html_(self) -> str:
        """Jupyter notebook display: the KPI table, Run-averaged, with a
        replications count."""
        sys_df = self.get("system")
        n = int(sys_df["Run"].nunique()) if sys_df is not None and not sys_df.empty else 0
        table = self.kpis().to_html(index=False, float_format=lambda v: f"{v:,.4g}", na_rep="-")
        return f"<p>{n} replication{'s' if n != 1 else ''}</p>\n{table}"

    def plot(self, key: str, ax=None, **kw):
        """A bar of the mean with a 95% CI whisker over runs for a KPI key
        (e.g. "system/Avg Queue Length"), or a line over time for a
        `timeseries` series (e.g. "level/Tank"/an AUX/a flow name) -- whichever
        `key` matches. Returns the `Axes`. Needs matplotlib (not a core
        dependency): raises with an install hint if it is missing."""
        try:
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise ImportError("Results.plot needs matplotlib: "
                              "pip install \"nagasim[plot]\" (or: pip install matplotlib)") from exc

        if ax is None:
            _, ax = plt.subplots()

        ts = self.get("timeseries")
        if ts is not None and not ts.empty and key in set(ts["Series"]):
            sub = ts[ts["Series"] == key].sort_values("Time")
            ax.plot(sub["Time"], sub["Value"], **kw)
            ax.set_xlabel("Time")
            ax.set_ylabel(key)
            return ax

        from nagasim.experiments import flatten_kpis

        values = flatten_kpis(self)
        values = values.loc[values["KPI"] == key, "Value"]
        if values.empty:
            raise KeyError(f"{key!r} is not a KPI key or a timeseries series in these results")
        st = ci95(values)
        half = st["ub"] - st["mean"]
        ax.bar([key], [st["mean"]], yerr=[half if half == half else 0.0], **kw)
        ax.set_ylabel(key)
        return ax

    def to_excel(self, path) -> None:
        """Write the results workbook (ROADMAP 2.4 C's xlsx exporter)."""
        from nagasim.exporters import EXPORTER_REGISTRY

        EXPORTER_REGISTRY["xlsx"].write(self, path)
