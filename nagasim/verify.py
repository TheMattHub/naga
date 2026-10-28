"""`naga verify`: the engine against textbook queueing theory (D689).

Each `Case` builds a small model, computes its theory from the case parameters
(never a pasted number) and names the KPIs to compare. A KPI passes when
|mean - theory| <= max(3 x CI half-width, 2% of theory), the mean and the 95%
half-width taken over seeded replications. `tests/test_oracles.py` imports
`CASES` so the suite and the command share one source.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from typing import Callable

import numpy as np

from nagasim.core.base_process import deterministic_hash
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.stats import ci95

_QUICK_REPLICATIONS = 8
_RESULT = {True: "pass", False: "FAIL"}


@dataclass
class Case:
    name: str
    layout_fn: Callable[[], dict]
    theory: dict[str, float]
    kpis: dict[str, str]          # label -> spec, see `_read`
    run_len: float = 3000
    warmup: float = 300
    deterministic: bool = False   # one replication is exact


# ---------------------------------------------------------------- model builders
def _create(dist, params, **extra):
    return {"name": "create_1", "description": "c", "type": "CREATE", "position": [0, 0],
            "params": {"e_type": "DEFAULT", "distribution": dist, "dist_params": params,
                       "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0, **extra}}


def _dispose(name="dispose_1"):
    return {"name": name, "description": "d", "type": "DISPOSE", "position": [9, 0],
            "params": {"distribution": "Constant", "dist_params": {"value": 0.0}}}


def _station(name, dist, params, resource, **extra):
    return {"name": name, "description": name, "type": "PROCESS", "position": [1, 0],
            "params": {"action": "Seize Delay Release", "distribution": dist,
                       "dist_params": params, "priority": 0, "quantity": 1,
                       "resource_name": resource, **extra}}


def _layout(blocks, links, resources, **sections):
    return {"blocks": blocks, "links": links,
            "resources": [{"name": n, "capacity": c, "discipline": "FIFO"} for n, c in resources],
            "entities": [], "variables": [],
            "simulation": {"time_limit": None, "warmup": 0, "replications": 1}, **sections}


def _single(arrival, service):
    """Create -> one single-server Process station -> Dispose; both args are (dist, params)."""
    blocks = [_create(*arrival), _station("station", *service, "Server"), _dispose()]
    return _layout(blocks, [["create_1", "station"], ["station", "dispose_1"]], [("Server", 1)])


def _exp(rate):
    return ("Exponential", {"rate": rate})


_SINGLE_KPIS = {"utilization": "resource:Server:Usage % (snapshot)",
                "Lq": "resource:Server:Queue Length Avg", "W": "system:Avg Throughput Time"}


# ---------------------------------------------------------------- theory
def _erlang_c_lq(lam, mu, c):
    """Lq of M/M/c (Erlang C; Gross & Harris, Fundamentals of Queueing Theory, 2.6)."""
    a = lam / mu
    rho = a / c
    p0 = 1.0 / (sum(a ** n / math.factorial(n) for n in range(c))
                + a ** c / (math.factorial(c) * (1 - rho)))
    return p0 * a ** c * rho / (math.factorial(c) * (1 - rho) ** 2)


def _pk_wq(lam, mean_s, var_s):
    """Wq of M/G/1 (Pollaczek-Khinchine): lam E[S^2] / (2 (1 - rho))."""
    return lam * (var_s + mean_s ** 2) / (2 * (1 - lam * mean_s))


def _mm1k_blocking(rho, k):
    """P(system full) of M/M/1/K: (1 - rho) rho^K / (1 - rho^(K+1))."""
    return (1 - rho) * rho ** k / (1 - rho ** (k + 1))


def _tandem_markov(mu1, mu2, places):
    """Stationary (throughput, P(machine 1 blocked)) of a saturated two-machine
    line with `places` between them. State (n, blocked): n = buffer + machine 2
    contents, blocked = machine 1 holds a finished part it cannot put down
    (only at n = places + 1). Solved as pi Q = 0, sum(pi) = 1."""
    states = [(n, 0) for n in range(places + 2)] + [(places + 1, 1)]
    index = {s: i for i, s in enumerate(states)}
    rates = np.zeros((len(states), len(states)))
    for state in states:
        n, blocked = state
        i = index[state]
        if not blocked:
            rates[i, index[(n + 1, 0) if n < places + 1 else (places + 1, 1)]] += mu1
        if n >= 1:
            # a blocked part goes straight into the place that just freed
            rates[i, index[(places + 1, 0) if blocked else (n - 1, 0)]] += mu2
    generator = rates - np.diag(rates.sum(axis=1))
    lhs = np.vstack([generator.T, np.ones(len(states))])
    rhs = np.zeros(len(states) + 1)
    rhs[-1] = 1.0
    pi = np.linalg.lstsq(lhs, rhs, rcond=None)[0]
    throughput = mu2 * sum(p for state, p in zip(states, pi) if state[0] >= 1)
    return throughput, float(pi[index[(places + 1, 1)]])


# ---------------------------------------------------------------- cases
def _mm1() -> Case:
    lam, mu = 1.0, 1.25
    rho = lam / mu
    return Case("M/M/1", lambda: _single(_exp(lam), _exp(mu)),
                {"utilization": rho, "Lq": rho ** 2 / (1 - rho),
                 "W": 1 / (mu - lam), "L": rho / (1 - rho)},
                {**_SINGLE_KPIS, "L": "system:Avg WIP"})


def _mm2() -> Case:
    lam, mu, c = 1.5, 1.0, 2
    sets = [{"name": "Tellers", "members": ["TellerA", "TellerB"], "rule": "SmallestNumberBusy"}]

    def layout():
        # Two single servers behind a set: a waiting entity is counted once, on the set's row.
        blocks = [_create(*_exp(lam)), _station("station", *_exp(mu), "Tellers"), _dispose()]
        return _layout(blocks, [["create_1", "station"], ["station", "dispose_1"]],
                       [("TellerA", 1), ("TellerB", 1)], resource_sets=sets)

    return Case("M/M/2 (Erlang C)", layout,
                {"utilization": lam / (c * mu), "Lq": _erlang_c_lq(lam, mu, c)},
                {"utilization": "resource:Tellers:Usage % (snapshot)",
                 "Lq": "resource:Tellers:Queue Length Avg"})


def _mg1(name, service, mean_s, var_s) -> Case:
    lam = 1.0
    wq = _pk_wq(lam, mean_s, var_s)
    return Case(name, lambda: _single(_exp(lam), service),
                {"utilization": lam * mean_s, "Lq": lam * wq, "W": wq + mean_s}, _SINGLE_KPIS)


def _dd1() -> Case:
    return Case("D/D/1", lambda: _single(("Constant", {"value": 2.0}), ("Constant", {"value": 1.0})),
                {"utilization": 0.5, "Lq": 0.0, "W": 1.0}, _SINGLE_KPIS,
                run_len=1000, warmup=0, deterministic=True)


def _mm1k() -> Case:
    lam, mu, k = 1.0, 1.25, 5

    def layout():
        # System capacity K = 1 in service + (K - 1) waiting: balk when K - 1 are already waiting.
        blocks = [_create(*_exp(lam)),
                  _station("station", *_exp(mu), "Server", balk_if=f"queue_length >= {k - 1}"),
                  _dispose(), _dispose("lost")]
        return _layout(blocks, [["create_1", "station"], ["station", "dispose_1"],
                                ["station", "lost"]], [("Server", 1)])

    return Case("M/M/1/K", layout, {"P(blocked)": _mm1k_blocking(lam / mu, k)},
                {"P(blocked)": "queue:station:Balked / system:Total Entities Created"})


def _jackson() -> Case:
    lam, mu1, mu2 = 1.0, 1.5, 1.25

    def layout():
        blocks = [_create(*_exp(lam)), _station("m1", *_exp(mu1), "M1"),
                  _station("m2", *_exp(mu2), "M2"), _dispose()]
        return _layout(blocks, [["create_1", "m1"], ["m1", "m2"], ["m2", "dispose_1"]],
                       [("M1", 1), ("M2", 1)])

    # Jackson: each node of an open M/M/1 tandem is an independent M/M/1, sojourn times add.
    return Case("Jackson tandem", layout, {"W": 1 / (mu1 - lam) + 1 / (mu2 - lam)},
                {"W": "system:Avg Throughput Time"}, run_len=4000)


def _blocked_tandem(places) -> Case:
    def layout():
        # Saturated source: every part released at t=0, so only a full buffer stops machine 1.
        blocks = [{**_create("Constant", {"value": 0.0}, max_arrivals=1800), "name": "src"},
                  _station("m1", *_exp(1.0), "M1"),
                  {"name": "buf", "description": "buf", "type": "BUFFER", "position": [2, 0],
                   "params": {"capacity": places}},
                  _station("m2", *_exp(1.0), "M2"), _dispose("out")]
        return _layout(blocks, [["src", "m1"], ["m1", "buf"], ["buf", "m2"], ["m2", "out"]],
                       [("M1", 1), ("M2", 1)])

    rate, blocked = _tandem_markov(1.0, 1.0, places)
    return Case(f"Blocked tandem, buffer {places}", layout,
                {"throughput": rate, "P(machine 1 blocked)": blocked},
                {"throughput": "resource:M2:Total Seizes / system:Simulation Time",
                 "P(machine 1 blocked)": "buffer:m1:Blocked %"},
                run_len=1500, warmup=150)


CASES: list[Case] = [
    _mm1(), _mm2(),
    _mg1("M/D/1", ("Constant", {"value": 0.8}), 0.8, 0.0),
    # gamma(alpha=4, beta=0.2): mean alpha*beta, variance alpha*beta^2
    _mg1("M/G/1 (gamma service)", ("Gamma", {"alpha": 4.0, "beta": 0.2}), 0.8, 4 * 0.2 ** 2),
    _dd1(), _mm1k(), _jackson(), _blocked_tandem(0), _blocked_tandem(2),
]


# ---------------------------------------------------------------- running
def _read(kpi: dict, spec: str) -> float:
    """One number from a replication's KPI frames. Specs: `system:Metric`,
    `resource:Name:Column`, `buffer:Block:Column`, `queue:Block:Column`; a column
    with `%` in its name is returned as a fraction; `a / b` divides two specs."""
    if " / " in spec:
        top, bottom = spec.split(" / ")
        return _read(kpi, top) / _read(kpi, bottom)
    kind, _, rest = spec.partition(":")
    if kind == "system":
        frame = kpi["system"]
        return float(frame.loc[frame["Metric"] == rest, "Value"].iloc[0])
    name, _, column = rest.partition(":")
    frame, key = {"resource": ("resource_summary", "Resource"), "buffer": ("buffer_summary", "Block"),
                  "queue": ("queue_summary", "Block")}[kind]
    value = float(kpi[frame].set_index(key).loc[name, column])
    return value / 100.0 if "%" in column else value


def _interval(samples) -> tuple[float, float]:
    """(mean, 95% half-width) of the samples; the half-width is 0 for one sample."""
    stats = ci95(samples)
    return stats["mean"], 0.0 if math.isnan(stats["ub"]) else stats["ub"] - stats["mean"]


def _row(case, label, theory, blocks) -> dict:
    """One report row from per-block samples. `passed` is the pass rule on block 0;
    `covered` counts the blocks whose 95% interval holds the theory."""
    mean, half = _interval(blocks[0])
    tolerance = max(3 * half, 0.02 * abs(theory), 1e-6)
    covered = 0
    for samples in blocks:
        m, h = _interval(samples)
        covered += abs(m - theory) <= h + 1e-9  # exact cases have h = 0
    return {"case": case.name, "kpi": label, "theory": theory, "mean": mean,
            "half_width": half, "passed": bool(abs(mean - theory) <= tolerance),
            "covered": int(covered), "blocks": len(blocks)}


def run(cases=None, replications: int = 20, seed: int = 0, blocks: int = 1) -> list[dict]:
    """Rows `{case, kpi, theory, mean, half_width, passed, covered, blocks}`.

    Each case has its own stream base `seed + deterministic_hash(name) % 10**6`, so
    cases do not share random numbers. Block b, replication r uses seed
    `base + b * replications + r`: blocks are disjoint seed ranges. `mean`,
    `half_width` and `passed` come from block 0, as before; `covered` counts the
    blocks whose 95% interval holds the theory (a correct engine covers ~95%).
    """
    rows = []
    for case in CASES if cases is None else cases:
        layout = case.layout_fn()
        reps = 1 if case.deterministic else replications
        base = seed + deterministic_hash(case.name) % 10 ** 6
        runs = [[run_single_simulation(layout, seed=base + b * reps + r, run_len=case.run_len,
                                       warmup=case.warmup) for r in range(reps)]
                for b in range(blocks)]
        for label, theory in case.theory.items():
            rows.append(_row(case, label, theory,
                             [[_read(k, case.kpis[label]) for k in block] for block in runs]))
    return rows


# ---------------------------------------------------------------- output
def _html(rows) -> str:
    from nagasim.report import _CSS, esc, fmt, table
    body = table(["Case", "KPI", "Theory", "Mean", "95% half-width", "Result", "Inside"],
                 [[esc(r["case"]), esc(r["kpi"]), fmt(r["theory"]), fmt(r["mean"]),
                   fmt(r["half_width"]), _RESULT[r["passed"]],
                   f"{r['covered']}/{r['blocks']}"] for r in rows])
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>naga verify</title>"
            f"<style>{_CSS}</style></head><body><h1>naga verify</h1>"
            "<p>The engine against textbook queueing theory. A KPI passes when the replication mean is "
            "within the larger of three 95% half-widths and 2% of the theory.</p>"
            f"{body}</body></html>")


def main(argv=None) -> int:
    """`naga verify [--quick] [--blocks K] [--json] [--html PATH]`; exit 1 when any KPI fails."""
    parser = argparse.ArgumentParser(prog="naga verify", description="Check the engine against textbook queueing theory.")
    parser.add_argument("--quick", action="store_true",
                        help=f"{_QUICK_REPLICATIONS} replications instead of 20")
    parser.add_argument("--blocks", type=int, default=1, metavar="K",
                        help="K independent seed blocks; reports how many intervals hold the theory")
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    parser.add_argument("--html", metavar="PATH", help="also write an HTML report here")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rows = run(replications=_QUICK_REPLICATIONS if args.quick else 20,
               blocks=max(1, args.blocks))
    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(_html(rows))
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{_RESULT[r['passed']]:7} {r['case']:28} {r['kpi']:22} theory {r['theory']:.4f}  "
                  f"mean {r['mean']:.4f} +/- {r['half_width']:.4f}  "
                  f"inside {r['covered']}/{r['blocks']}")
    return 0 if all(r["passed"] for r in rows) else 1
