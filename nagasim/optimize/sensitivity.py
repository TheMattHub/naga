from __future__ import annotations

from nagasim import api
import pandas as pd
from .designs import latin_hypercube
from .evaluation import _candidate_layout
from .factors import overrides_for

# ---------------------------------------------------------------- sensitivity
def sensitivity(layout, factors, n: int, seed: int = 0, quantiles=(0.05, 0.5, 0.95), *,
                run_length=None, warmup=None, on_progress=None, should_stop=None):
    """How much the model's time series move over *factors*' ranges (PHASE5
    SD-4): a Latin hypercube of *n* points, ONE run per point on the SAME seed
    (CRN -- differences are the factors, not the random stream), and the
    empirical *quantiles* of every series across the points at each sample
    time, through the same `forecast.bands` a Monte Carlo forecast uses.

    Returns a `forecast.ForecastResult` (its `kpis`/`events`/`orders` are
    empty -- there is no replication distribution here, only a factor sweep)
    so the results window's existing fan chart (`ui.charts.band_chart`) draws
    it unchanged. Only *quantiles*' lowest and highest entries become the
    band's Q05/Q95 edges; a middle one (the default's 0.5) is for the caller's
    own reading of the design, not a third column -- `bands()` draws Mean as
    the centre line, same as a forecast's.
    """
    from nagasim import forecast

    base = api.load_layout(layout)
    design = latin_hypercube(factors, n, seed)
    total = len(design)
    frames = []
    for i, row in design.iterrows():
        if should_stop is not None and should_stop():
            break
        candidate = _candidate_layout(base, overrides_for(factors, row.to_dict()))
        results = api.run_model(candidate, replications=1, seed=seed, run_length=run_length,
                                warmup=warmup, record_logs=False)
        if results is not None and results.get("timeseries") is not None:
            frame = results["timeseries"].copy()
            frame["Run"] = i     # disambiguates points; bands() does not key on it
            frames.append(frame)
        if on_progress is not None:
            on_progress(i + 1, total)
    combined = {"timeseries": pd.concat(frames, ignore_index=True) if frames
               else pd.DataFrame(columns=["Run", "Series", "Time", "Value"])}
    edges = (float(quantiles[0]), float(quantiles[-1]))
    horizon = float(run_length or (base.get("simulation") or {}).get("time_limit") or 0)
    return forecast.ForecastResult(
        horizon=horizon, replications=len(frames), bands=forecast.bands(combined, edges),
        kpis=pd.DataFrame(columns=forecast.KPI_COLUMNS),
        events=pd.DataFrame(columns=forecast.EVENT_COLUMNS), results=combined, quantiles=edges)

