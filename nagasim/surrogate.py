"""Surrogate metamodels and datasets over a DOE (ROADMAP 3.7 B).

Top-level module beside `optimize.py`: it needs numpy/pandas, sits above the
engine, and imports no Qt. scikit-learn (`kind="gp"`/`"forest"`) is a lazy,
optional import -- `fit_surrogate` works with none of it installed for
"linear"/"quadratic"/"auto".

    from nagasim import surrogate
    frame = surrogate.export_dataset(design_result, path="points.csv")
    model = surrogate.fit_surrogate(design_result, "system/Avg Throughput Time")
    mean, _std = model.predict([{"factor": 3.0}])
    candidates = model.suggest(5, goal="min")
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

__all__ = ["export_dataset", "fit_surrogate", "Surrogate"]

LINEAR_KINDS = ("linear", "quadratic")
SKLEARN_KINDS = ("gp", "forest")


def _has_sklearn() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- dataset
def export_dataset(design_result, path=None) -> pd.DataFrame:
    """One row per (design point, replication): factors, responses, seed.

    Written to *path* as CSV, or parquet when the suffix is ".parquet" (needs
    pyarrow or fastparquet -- a clear ImportError otherwise, never a silent
    CSV fallback the caller did not ask for). Always returns the frame."""
    rows = []
    for point, per_response in design_result.values.items():
        coords = {f.name: design_result.design.loc[point, f.name] for f in design_result.factors}
        seeds = design_result.seeds.get(point, [])
        for r, seed in enumerate(seeds):
            row = {**coords, "Point": point, "Seed": seed}
            for response in design_result.responses:
                arr = per_response.get(response)
                row[response] = float(arr[r]) if arr is not None and r < len(arr) else float("nan")
            rows.append(row)
    frame = pd.DataFrame(rows, columns=["Point", *design_result.factor_names, "Seed",
                                        *design_result.responses])
    if path is not None:
        path = Path(path)
        if path.suffix.lower() == ".parquet":
            try:
                frame.to_parquet(path, index=False)
            except ImportError as exc:
                raise ImportError("writing .parquet needs pyarrow or fastparquet installed "
                                  "(pip install pyarrow); use a .csv path instead") from exc
        else:
            frame.to_csv(path, index=False)
    return frame


# ---------------------------------------------------------------- encoding
@dataclass
class _Encoding:
    """How one factor enters the design matrix: a standardised numeric column,
    or one-hot dummies (baseline = first level) for a categorical one."""

    name: str
    kind: str
    mean: float = 0.0
    std: float = 1.0
    levels: list | None = None


def _encode_factors(factors, points: pd.DataFrame) -> list[_Encoding]:
    out = []
    for f in factors:
        if f.kind == "categorical":
            out.append(_Encoding(f.name, "categorical", levels=list(f.levels)))
        else:
            values = points[f.name].astype(float).to_numpy()
            std = float(values.std())
            out.append(_Encoding(f.name, f.kind, mean=float(values.mean()), std=std or 1.0))
    return out


def _numeric_names(encodings) -> list[str]:
    return [e.name for e in encodings if e.kind != "categorical"]


def _term_names(encodings, kind: str) -> list[str]:
    numeric = _numeric_names(encodings)
    names = ["Intercept"]
    for e in encodings:
        if e.kind == "categorical":
            names += [f"{e.name}={level}" for level in e.levels[1:]]
    if kind in LINEAR_KINDS:
        names += list(numeric)
    if kind == "quadratic":
        names += [f"{n}^2" for n in numeric]
        names += [f"{a}*{b}" for i, a in enumerate(numeric) for b in numeric[i + 1:]]
    return names


def _z(encodings, point: dict) -> dict[str, float]:
    return {e.name: (float(point[e.name]) - e.mean) / e.std
           for e in encodings if e.kind != "categorical"}


def _row_features(encodings, point: dict, kind: str) -> list[float]:
    """One row of the design matrix for `kind` in ("linear", "quadratic")."""
    numeric = _numeric_names(encodings)
    z = _z(encodings, point)
    feats = [1.0]
    for e in encodings:
        if e.kind == "categorical":
            feats += [1.0 if point[e.name] == level else 0.0 for level in e.levels[1:]]
    if kind in LINEAR_KINDS:
        feats += [z[n] for n in numeric]
    if kind == "quadratic":
        feats += [z[n] ** 2 for n in numeric]
        feats += [z[a] * z[b] for i, a in enumerate(numeric) for b in numeric[i + 1:]]
    return feats


def _sklearn_row(encodings, point: dict) -> list[float]:
    """A plain (no squared/interaction terms) standardised/one-hot row -- GP
    and random-forest models capture nonlinearity themselves."""
    feats = []
    for e in encodings:
        if e.kind == "categorical":
            feats += [1.0 if point[e.name] == level else 0.0 for level in e.levels[1:]]
        else:
            feats.append(_z(encodings, point)[e.name])
    return feats


# ---------------------------------------------------------------- fitting
def _fit_wls(X: np.ndarray, y: np.ndarray, w: np.ndarray):
    """Weighted least squares; returns (coef, std_error)."""
    root_w = np.sqrt(w)
    Xw, yw = X * root_w[:, None], y * root_w
    coef, *_ = np.linalg.lstsq(Xw, yw, rcond=None)
    resid = yw - Xw @ coef
    dof = max(X.shape[0] - X.shape[1], 1)
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.pinv(Xw.T @ Xw)
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    return coef, se


def _weighted_r2(y, yhat, w, n_params: int):
    ybar = float(np.average(y, weights=w))
    ss_res = float(np.sum(w * (y - yhat) ** 2))
    ss_tot = float(np.sum(w * (y - ybar) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    n = len(y)
    r2_adj = 1.0 - (1.0 - r2) * (n - 1) / max(n - n_params - 1, 1) if np.isfinite(r2) else float("nan")
    return r2, r2_adj


def _kfold(n: int, folds: int, seed: int) -> list[np.ndarray]:
    folds = max(2, min(int(folds), n))
    order = np.random.default_rng(seed).permutation(n)
    return np.array_split(order, folds)


def _cv_rmse(fit_predict, X, y, w, cv_folds: int, seed: int) -> float:
    parts = _kfold(len(y), cv_folds, seed)
    errors: list[float] = []
    for k in range(len(parts)):
        test = parts[k]
        train = np.concatenate([parts[j] for j in range(len(parts)) if j != k])
        if len(train) < 2:
            continue
        pred = fit_predict(X[train], y[train], w[train], X[test])
        errors.extend((np.asarray(pred) - y[test]).tolist())
    return float(np.sqrt(np.mean(np.square(errors)))) if errors else float("nan")


def _pooled_replication_noise(rows: pd.DataFrame) -> float:
    """Pooled within-point standard deviation, from points with >= 2
    replications -- the CV RMSE a well-specified surrogate should not exceed
    by much; NaN when no point has replicate data. Floored the same way as
    `_point_weights`, so genuinely-zero replication noise (identical
    replications) does not read as a below-floating-point-precision "noise"
    that flags every honest fit as lack-of-fit."""
    mask = rows["N"] >= 2
    if not mask.any():
        return float("nan")
    y = rows["Mean"].to_numpy(dtype=float)
    floor = 1e-8 * max(float(np.std(y)), float(np.mean(np.abs(y))), 1.0)
    sem = ((rows.loc[mask, "UB"] - rows.loc[mask, "Mean"]) / 1.96).to_numpy(dtype=float)
    sd = np.maximum(sem * np.sqrt(rows.loc[mask, "N"].to_numpy(dtype=float)), floor)
    return float(np.sqrt(np.mean(sd ** 2)))


def _point_weights(rows: pd.DataFrame) -> np.ndarray:
    """1 / variance of the point mean, when estimable (N >= 2); 1.0 otherwise,
    then rescaled so the weights average to 1 (keeps the intercept fit stable
    regardless of the response's own scale).

    `var_mean` is floored relative to the response's own scale: without this,
    a point whose replications are bit-for-bit equal (or a deliberately
    noiseless test) can measure a variance of, say, 1e-31 purely from
    floating-point rounding in the mean, and 1 / 1e-31 then swamps every other
    point's weight -- a fit to one point, not a surface.
    """
    sem = ((rows["UB"] - rows["Mean"]) / 1.96).to_numpy(dtype=float)
    var_mean = sem ** 2
    n = rows["N"].to_numpy(dtype=float)
    y = rows["Mean"].to_numpy(dtype=float)
    scale = max(float(np.std(y)), float(np.mean(np.abs(y))), 1.0)
    floor = (1e-8 * scale) ** 2
    var_mean = np.where(np.isfinite(var_mean), np.maximum(var_mean, floor), np.nan)
    weights = np.where((n >= 2) & np.isfinite(var_mean), 1.0 / var_mean, 1.0)
    return weights / weights.mean()


@dataclass
class Surrogate:
    """A fitted response surface for one DOE response.

    `coefficients` is empty for `kind` "gp"/"forest" (they do not have linear
    terms); use `predict`/`suggest` for those exactly as for linear/quadratic.
    """

    kind: str
    response: str
    factors: list
    terms: list[str]
    coef: np.ndarray
    se: np.ndarray
    r2: float
    r2_adj: float
    cv_rmse: float
    lack_of_fit: bool
    _encodings: list = field(repr=False, default_factory=list)
    _model: object = field(repr=False, default=None)   # sklearn estimator, gp/forest only

    @property
    def coefficients(self) -> pd.DataFrame:
        return pd.DataFrame({"Term": self.terms, "Coefficient": self.coef, "Std Error": self.se})

    def _rows(self, points) -> list[dict]:
        return points.to_dict(orient="records") if isinstance(points, pd.DataFrame) else list(points)

    def predict(self, points) -> tuple[np.ndarray, np.ndarray | None]:
        """(mean, std) over *points* (a DataFrame or list of {factor: value}
        dicts); std is None except for `kind="gp"`."""
        rows = self._rows(points)
        if self.kind in LINEAR_KINDS:
            X = np.array([_row_features(self._encodings, p, self.kind) for p in rows])
            return X @ self.coef, None
        X = np.array([_sklearn_row(self._encodings, p) for p in rows])
        if self.kind == "gp":
            mean, std = self._model.predict(X, return_std=True)
            return np.asarray(mean, dtype=float), np.asarray(std, dtype=float)
        return np.asarray(self._model.predict(X), dtype=float), None

    def effect_curve(self, factor_name: str, points: int = 21):
        """(x, y) for a 1-D slice: *factor_name* swept over its range, every
        other factor held at its centre (ROADMAP 3.7 B item 8)."""
        factor = next(f for f in self.factors if f.name == factor_name)
        centre = {f.name: f.from_unit(0.5) for f in self.factors}
        if factor.kind == "categorical":
            xs = list(factor.levels)
            rows = [{**centre, factor_name: level} for level in xs]
        else:
            xs = list(np.linspace(factor.low, factor.high, points))
            rows = [{**centre, factor_name: x} for x in xs]
        mean, _std = self.predict(rows)
        return xs, mean.tolist()

    def suggest(self, n: int, goal: str = "min", bounds: dict[str, tuple] | None = None) -> list[dict]:
        """Top *n* candidate points by predicted `goal`, from a dense Latin
        hypercube search over the factor space (optionally narrowed by
        *bounds*, `{factor name: (low, high)}`). NOT simulated -- hand these to
        `optimize.optimize(..., warm_start_points=...)` to confirm them."""
        from nagasim.optimize import Factor, latin_hypercube

        search_factors = []
        for f in self.factors:
            lo_hi = (bounds or {}).get(f.name)
            if f.kind == "categorical" or lo_hi is None:
                search_factors.append(f)
            else:
                search_factors.append(Factor(f.paths, f.kind, lo_hi[0], lo_hi[1],
                                             step=f.step, name=f.name))
        candidates = latin_hypercube(search_factors, max(200, int(n) * 20), seed=0)
        mean, _std = self.predict(candidates)
        order = np.argsort(mean if goal == "min" else -mean)[:max(int(n), 1)]
        return candidates.iloc[order].reset_index(drop=True).to_dict(orient="records")


def _fit_linear_family(kind: str, encodings, rows: pd.DataFrame, y, w, cv_folds: int, seed: int):
    X = np.array([_row_features(encodings, rows.iloc[i].to_dict(), kind) for i in range(len(rows))])
    coef, se = _fit_wls(X, y, w)
    r2, r2_adj = _weighted_r2(y, X @ coef, w, X.shape[1])
    cv = _cv_rmse(lambda Xtr, ytr, wtr, Xte: Xte @ _fit_wls(Xtr, ytr, wtr)[0], X, y, w, cv_folds, seed)
    return coef, se, r2, r2_adj, cv


def _fit_sklearn_kind(kind: str, encodings, rows: pd.DataFrame, y, w, cv_folds: int, seed: int):
    try:
        if kind == "gp":
            from sklearn.gaussian_process import GaussianProcessRegressor
            from sklearn.gaussian_process.kernels import RBF, WhiteKernel

            def make_model():
                return GaussianProcessRegressor(kernel=RBF() + WhiteKernel(), normalize_y=True,
                                                random_state=seed)
        else:
            from sklearn.ensemble import RandomForestRegressor

            def make_model():
                return RandomForestRegressor(n_estimators=200, random_state=seed)
    except ImportError as exc:
        raise ImportError(f"kind={kind!r} needs the optional scikit-learn package "
                          f"(pip install \"nagasim[ml]\", or: pip install scikit-learn); "
                          f"linear/quadratic need nothing") from exc

    X = np.array([_sklearn_row(encodings, rows.iloc[i].to_dict()) for i in range(len(rows))])
    model = make_model()
    # ponytail: sample weights are dropped for gp/forest CV refits (sklearn's
    # GP does not take them at all); upgrade to a weighted variant if the
    # replication counts vary a lot across design points.
    model.fit(X, y)
    r2, r2_adj = _weighted_r2(y, model.predict(X), w, X.shape[1])
    cv = _cv_rmse(lambda Xtr, ytr, wtr, Xte: make_model().fit(Xtr, ytr).predict(Xte),
                 X, y, w, cv_folds, seed)
    return model, r2, r2_adj, cv


def fit_surrogate(design_result, response: str, *, kind: str = "quadratic", cv_folds: int = 5,
                  seed: int = 0) -> Surrogate:
    """Fit a response surface for *response* over `design_result`'s design.

    `kind`: "linear", "quadratic" (linear + interactions + squared terms on
    standardised numeric factors, one-hot categoricals), "auto" (picks
    linear/quadratic by k-fold CV RMSE), or -- when scikit-learn is installed
    -- "gp"/"forest" through the same `predict`/`suggest` interface.

    Weighted by 1/variance of each point's mean (equal weight where that is
    not estimable, e.g. one replication). `lack_of_fit` is set when the CV
    RMSE is more than 3x the pooled REPLICATION noise -- the surface is
    missing structure the noise floor does not explain.
    """
    if response not in design_result.responses:
        raise ValueError(f"unknown response: {response!r} (has {design_result.responses})")
    rows = design_result.table
    rows = rows[rows["Response"] == response].reset_index(drop=True)
    if rows.empty:
        raise ValueError(f"design_result has no rows for response {response!r}")
    factors = list(design_result.factors)
    encodings = _encode_factors(factors, rows)
    y = rows["Mean"].to_numpy(dtype=float)
    w = _point_weights(rows)

    if kind == "auto":
        fits = {k: _fit_linear_family(k, encodings, rows, y, w, cv_folds, seed) for k in LINEAR_KINDS}
        kind = min(fits, key=lambda k: fits[k][4] if np.isfinite(fits[k][4]) else np.inf)
        coef, se, r2, r2_adj, cv = fits[kind]
        model = None
    elif kind in LINEAR_KINDS:
        coef, se, r2, r2_adj, cv = _fit_linear_family(kind, encodings, rows, y, w, cv_folds, seed)
        model = None
    elif kind in SKLEARN_KINDS:
        model, r2, r2_adj, cv = _fit_sklearn_kind(kind, encodings, rows, y, w, cv_folds, seed)
        coef, se = np.array([]), np.array([])
    else:
        offered = LINEAR_KINDS + (SKLEARN_KINDS if _has_sklearn() else ())
        raise ValueError(f"unknown surrogate kind: {kind!r} (expected one of {offered})")

    noise = _pooled_replication_noise(rows)
    lack_of_fit = bool(np.isfinite(cv) and np.isfinite(noise) and noise > 0 and cv > 3.0 * noise)
    terms = _term_names(encodings, kind) if kind in LINEAR_KINDS else []
    return Surrogate(kind=kind, response=response, factors=factors, terms=terms, coef=coef, se=se,
                     r2=r2, r2_adj=r2_adj, cv_rmse=cv, lack_of_fit=lack_of_fit,
                     _encodings=encodings, _model=model)
