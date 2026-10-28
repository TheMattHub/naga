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

import pandas as pd

from nagasim.core.stats import ci95
from nagasim.runchecks import RunChecks

__all__ = ["Results"]

log = logging.getLogger(__name__)


class Results(dict):
    """What a run returns: KPI frames by name, with notebook conveniences.

    A ``dict[str, pd.DataFrame]`` (the nine-to-twenty KPI frames, each with a
    "Run" column), so ``results["system"]`` works as on any dict; the methods
    add the mean over runs, a plot and an Excel workbook. Made by
    `nagasim.api.run_model`, ``Model.run`` and the experiment functions.

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
            The frame, one row per metric and run.

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
            raise KeyError(f"{key!r} is not a KPI key in these results; see results.kpis()")
        return float(hit.iloc[0])

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
