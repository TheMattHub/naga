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

import logging
import math
from typing import NamedTuple

import pandas as pd

from nagasim.core.stats import ci95
from nagasim.core.suggest import did_you_mean
from nagasim.runchecks import RunChecks

__all__ = ["ALWAYS_KEPT", "Interval", "Results"]

log = logging.getLogger(__name__)

# D1321: the frames `api.run_model` returns even with no rows; every other empty one is left out.
ALWAYS_KEPT = ("system",)


class Interval(NamedTuple):
    """One KPI over the replications: the mean, the 95% half-width (NaN with one replication)
    and the number of finite replication values behind them."""
    mean: float
    half_width: float
    n: int


def _unknown_key(key: str, known, what: str = "a KPI key") -> KeyError:
    """#1899: a misspelt key names the nearest one; where the keys are listed only when none is close."""
    said = did_you_mean(key, known)
    return KeyError(f"{key!r} is not {what} in these results; {said or 'see results.kpis()'}")


class Results(dict):
    """What a run returns: KPI frames by name, with notebook conveniences.

    A ``dict[str, pd.DataFrame]`` (the KPI frames that have rows, each with a
    "Run" column; ``"system"`` is always there), so ``results["system"]`` works
    as on any dict; a frame the run left empty is absent, so test for it with
    ``"store_summary" in results`` or ``results.get("store_summary")``. The methods
    add the mean over runs, a plot and an Excel workbook. Made by
    `nagasim.api.run_model`, ``Model.run`` and the experiment functions (which keep
    every frame of a scenario, empty or not).

    See Also
    --------
    nagasim.api.run_model : Makes it.
    nagasim.build.Model : ``Model.run`` returns it.
    nagasim.experiments.flatten_kpis : The frames as one long table.

    Examples
    --------
    >>> from pathlib import Path
    >>> import nagasim
    >>> from nagasim import api
    >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
    >>> results = api.run_model(str(path), replications=2, run_length=200, seed=0)
    >>> sorted(results["system"].columns)
    ['Metric', 'Run', 'Value']
    >>> results.kpi("system/Total Entities Completed") > 0
    True
    """

    # D726: a paced run's `{"paced", "late_events", "max_lag_s"}`; an attribute,
    # not a frame, so no frame or digest of a run depends on the pace.
    pace: dict | None = None

    # D906: the layout `run_model` ran and the warm-up it used, so `checks()` can
    # judge the run; attributes, not frames, for the same reason as `pace`. A
    # hand-built or reloaded Results keeps the None and has no checks.
    _layout: dict | None = None
    _warmup: float = 0.0
    # E8 (D1119): {run id: {name: object}} a plugin handed over with `ctx.add_artifact`.
    _artifacts: dict | None = None
    # D1321: the names of the frames `run_model` left out because they had no rows.
    _dropped: tuple = ()

    def __missing__(self, name):
        # D1321: a frame the run left empty is absent; say so rather than a bare KeyError.
        if name in self._dropped:
            raise KeyError(f"{name!r} has no rows in this run, so run_model left it out; test with "
                           f"{name!r} in results or results.get({name!r})")
        raise KeyError(name)

    @property
    def artifacts(self) -> dict:
        """The objects plugin blocks handed over, ``{run: {name: object}}``.

        A plugin block calls ``self.ctx.add_artifact(name, obj)`` during a
        replication, for instance with the schedule it executed; ``run`` is the
        same id as the frames' "Run" column. Empty for a run no plugin added to,
        and for a Results built by hand or loaded back. Numbers a plugin records
        with ``self.ctx.record_kpi(name, value)`` are the ``"plugin_kpis"`` frame.

        Returns
        -------
        dict
            ``{run: {name: object}}``, one entry per replication that added one.
        """
        return self._artifacts or {}

    def checks(self) -> RunChecks:
        """Return the law checks of this run, computed once and kept.

        The checks of `nagasim.laws.checks` (utilisation, offered load,
        Little's law, flow conservation, censoring, initial bias), judged with
        the warm-up the run used. They are shown, never enforced: a check that
        cannot be computed gives an empty `RunChecks` with a ``note``, never an
        exception.

        Returns
        -------
        RunChecks
            ``.partial`` holds the warnings, ``.passed`` counts the rest.
            Results not made by `nagasim.api.run_model` (built by hand, loaded
            back) have no layout: ``note == "no layout"`` and no checks.

        See Also
        --------
        nagasim.laws.checks : The checks themselves, on any layout and results.
        Results.kpis : The KPI table the checks qualify.

        Examples
        --------
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim import api
        >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
        >>> results = api.run_model(str(path), replications=2, run_length=200, seed=0)
        >>> results.checks().passed > 0
        True
        """
        cached = self.__dict__.get("_checks")
        if cached is not None:
            return cached
        if self._layout is None:
            found = RunChecks((), note="no layout")
        else:
            try:
                # Local import: laws imports experiments, which imports api (the
                # cycle noted in `kpis`).
                from nagasim import laws
                found = RunChecks(tuple(laws.checks(self._layout, self, warmup=self._warmup)))
            except Exception as exc:     # D907: a check never raises into a run or a display
                log.debug("law checks failed: %s", exc, exc_info=True)
                found = RunChecks((), note=f"checks failed: {exc}")
        self._checks = found
        return found

    def frame(self, name: str) -> pd.DataFrame:
        """Return the KPI frame *name*; an alias for ``results[name]``.

        Parameters
        ----------
        name : str
            A frame name, such as ``"system"`` or ``"resource_summary"``.

        Returns
        -------
        pandas.DataFrame
            The frame, one row per metric and run. A frame the run left empty is
            absent and raises KeyError (D1321).

        See Also
        --------
        Results.kpis : The mean over runs, one row per KPI.

        Examples
        --------
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim import api
        >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
        >>> results = api.run_model(str(path), replications=2, run_length=200, seed=0)
        >>> list(results.frame("system").columns)
        ['Metric', 'Value', 'Run']
        """
        return self[name]

    def kpis(self) -> pd.DataFrame:
        """Return the KPIs as one table: mean, spread and interval over runs.

        Built from the stable "family/name" keys of
        `nagasim.experiments.flatten_kpis`, such as ``"system/Avg Queue
        Length"`` or ``"resource/Server/Usage % (snapshot)"``.

        Returns
        -------
        pandas.DataFrame
            Columns ``metric``, ``mean``, ``std`` and ``ci95`` (the 95%
            half-width), one row per KPI.

        See Also
        --------
        Results.kpi : One KPI's mean.
        nagasim.experiments.flatten_kpis : One row per KPI and run.

        Examples
        --------
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim import api
        >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
        >>> results = api.run_model(str(path), replications=2, run_length=200, seed=0)
        >>> list(results.kpis().columns)
        ['metric', 'mean', 'std', 'ci95']
        """
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
        """Return the mean over runs of one KPI.

        Parameters
        ----------
        key : str
            The KPI's "family/name" key, for example ``"system/Avg Queue
            Length"``.

        Returns
        -------
        float
            The mean over runs. The spread is in `Results.kpis`.

        Raises
        ------
        KeyError
            If *key* is not a KPI of these results (the message names it).

        See Also
        --------
        Results.kpis : All KPIs, with spread.

        Examples
        --------
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim import api
        >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
        >>> results = api.run_model(str(path), replications=2, run_length=200, seed=0)
        >>> results.kpi("system/Total Entities Completed") > 0
        True
        """
        table = self.kpis()
        hit = table.loc[table["metric"] == key, "mean"]
        if hit.empty:
            raise _unknown_key(key, table["metric"])
        return float(hit.iloc[0])

    def interval(self, key: str) -> Interval:
        """Return one KPI's mean with its 95% interval over runs.

        Parameters
        ----------
        key : str
            The KPI's "family/name" key, as in `Results.kpi`.

        Returns
        -------
        Interval
            ``(mean, half_width, n)``; the half-width is NaN with one replication. It is the
            ``ci95`` of `Results.kpis` and of ``naga run --json``.

        Raises
        ------
        KeyError
            If *key* is not a KPI of these results (the message names it).

        See Also
        --------
        Results.kpi : The mean alone.
        Results.kpis : Every KPI, with its interval.

        Examples
        --------
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim import api
        >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
        >>> results = api.run_model(str(path), replications=3, run_length=200, seed=0)
        >>> results.interval("system/Total Entities Completed").n
        3
        """
        from nagasim.experiments import flatten_kpis     # late: experiments imports this module

        flat = flatten_kpis(self)
        values = flat.loc[flat["KPI"] == key, "Value"]
        if values.empty:
            raise _unknown_key(key, flat["KPI"].unique())
        st = ci95(values)
        return Interval(float(st["mean"]), float(st["ub"] - st["mean"]),
                        int(values.map(math.isfinite).sum()))

    def __repr__(self) -> str:
        """The frame names and the first KPIs, not the dict of 30 frames (about 10,000 characters
        at a prompt); `results["system"]` and the other frames are unchanged."""
        if not self:
            return "Results()"
        table = self.kpis()
        sys_df = self.get("system")
        n = int(sys_df["Run"].nunique()) if sys_df is not None and not sys_df.empty else 0
        more = len(table) - 10
        return (f"Results({n} replication{'s' if n != 1 else ''}; frames: {', '.join(self)})\n"
                + table.head(10).to_string(index=False, float_format=lambda v: f"{v:.4g}")
                + (f"\n... {more} more KPIs: results.kpis()" if more > 0 else ""))

    def _repr_html_(self) -> str:
        """Jupyter notebook display: the partial law checks (D907), then the
        KPI table, Run-averaged, with a replications count."""
        sys_df = self.get("system")
        n = int(sys_df["Run"].nunique()) if sys_df is not None and not sys_df.empty else 0
        table = self.kpis().to_html(index=False, float_format=lambda v: f"{v:,.4g}", na_rep="-")
        found = self.checks()
        warn = found._repr_html_() + "\n" if found.partial else ""
        return f"{warn}<p>{n} replication{'s' if n != 1 else ''}</p>\n{table}"

    def plot(self, key: str, ax=None, **kw):
        """Plot one KPI or one time series.

        A KPI key gives a bar of the mean with a 95% interval whisker over
        runs; a ``timeseries`` series (a level, an auxiliary, a flow) gives a
        line over time. Whichever *key* matches is drawn.

        Parameters
        ----------
        key : str
            A KPI key such as ``"system/Avg Queue Length"``, or a series name
            such as ``"level/Tank"``.
        ax : matplotlib.axes.Axes, optional
            Where to draw; a new figure when omitted.
        **kw
            Passed to matplotlib's ``bar`` or ``plot``.

        Returns
        -------
        matplotlib.axes.Axes
            The axes drawn on.

        Raises
        ------
        KeyError
            If *key* is neither a KPI key nor a series of these results.
        ImportError
            If matplotlib (not a core dependency) is missing; the message says
            how to install it.

        See Also
        --------
        Results.kpis : The numbers behind the bar.

        Examples
        --------
        >>> import matplotlib
        >>> matplotlib.use("Agg")
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim import api
        >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
        >>> results = api.run_model(str(path), replications=2, run_length=200, seed=0)
        >>> ax = results.plot("system/Total Entities Completed")
        >>> ax.get_ylabel()
        'system/Total Entities Completed'
        """
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

        flat = flatten_kpis(self)
        values = flat.loc[flat["KPI"] == key, "Value"]
        if values.empty:
            series = [] if ts is None or ts.empty else list(ts["Series"].unique())
            raise _unknown_key(key, [*flat["KPI"].unique(), *series], "a KPI key or a timeseries series")
        st = ci95(values)
        half = st["ub"] - st["mean"]
        ax.bar([key], [st["mean"]], yerr=[half if half == half else 0.0], **kw)
        ax.set_ylabel(key)
        return ax

    def to_excel(self, path) -> None:
        """Write the results as an Excel workbook, one sheet per frame.

        Parameters
        ----------
        path : str or os.PathLike
            The ``.xlsx`` file to write.

        Returns
        -------
        None

        See Also
        --------
        Results.frame : One frame, as a DataFrame.

        Examples
        --------
        >>> import tempfile
        >>> from pathlib import Path
        >>> from pathlib import Path
        >>> import nagasim
        >>> from nagasim import api
        >>> path = Path(nagasim.__file__).parent / "models" / "single_server.json"
        >>> results = api.run_model(str(path), replications=2, run_length=200, seed=0)
        >>> with tempfile.TemporaryDirectory() as folder:
        ...     results.to_excel(Path(folder) / "results.xlsx")
        ...     (Path(folder) / "results.xlsx").is_file()
        True
        """
        from nagasim.exporters import EXPORTER_REGISTRY

        EXPORTER_REGISTRY["xlsx"].write(self, path)
