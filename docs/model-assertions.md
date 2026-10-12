# Model assertions and CI

A model assertion pins one KPI against a
comparison operator and value, checked over the model's own replications -- the same idea as a
unit test, but for a simulation's steady-state behaviour instead of its code:

```json
"assertions": [
    {"name": "server_not_overloaded", "kpi": "resource/Server/Usage % (snapshot)",
     "op": "<=", "value": 80, "replications": 5, "confidence": 0.95}
]
```

`nagasim.modelcheck.check(layout)` runs each assertion's KPI (`nagasim.experiments.flatten_kpis`'s
own key grammar -- see [Model file format](model-file-format.md)) over its `replications` and
computes a Student-t confidence interval of the mean. An assertion is **three-state**: `pass` when
the **whole** interval satisfies the op (`<`/`<=`/`>`/`>=` clears `value`; `==` lies inside `value
+/- tolerance`), `fail` when the whole interval instead violates it, and `inconclusive` when the
interval straddles the boundary -- neither the whole interval nor its complement clears it.
`Result.passed` stays `status == "pass"` for callers that only want the boolean. A single
replication, or one with zero variance, has no interval -- the check falls back to the point
estimate, so it is always `pass` or `fail`, never `inconclusive` on that account alone.

Reality checks (the existing `reality_checks` section, see [System dynamics](system-dynamics.md))
run in the **same pass**, as extra results with no KPI/CI of their own (one deterministic
replication, so they stay plain `pass`/`fail`, never `inconclusive`).

## `naga check`

```console
naga check model.json --junit out.xml
```

It takes several model files, `--workers N` and `--strict`.

Prints a pass/fail/inconclusive table per model. Exit code 0 when nothing failed, 1 when any
assertion or reality check failed (or, with `--strict`, was inconclusive too), 2 on error (a
missing file, invalid JSON). `--junit` writes one JUnit XML `<testcase>` per assertion/reality
check (`classname` is the model file's stem): a `<failure>` element for `fail`, a `<skipped>`
element for `inconclusive` (JUnit has no native third state) -- readable by any CI system's
test-report step.

Assertions across all the given models that share a `replications` count are **not** batched
together (each model file is its own `check()` call); within one model, assertions sharing a
`replications` count run in a single batch of that many `api.run_model` replications, so N
assertions at 5 replications cost 5 runs, not 5N.

`nagasim.modelcheck.assert_model(path, strict=False)` is a pytest-friendly helper: it raises
`AssertionError` naming every failed assertion/reality check (and every inconclusive one too when
`strict=True`), so a model's own test suite can do `assert_model("models/my_model.json")` and get
an ordinary pytest failure report. An inconclusive result that does not raise is reported through
`warnings.warn` instead.

## `naga ci-template`

```console
naga ci-template --path models/
```

Prints a GitHub Actions workflow that installs the engine and runs `naga check` on every
`*.json` under `--path` (default `models/`; a path with spaces is fine) on every push and pull
request, uploading the JUnit report as a build artifact. `--install` is the workflow's install
command; the default, `pip install .`, installs whatever the checked-out repository is, so in a
repository that only holds models, give the command that installs the engine. Redirect the
output into a workflow file of yours, `.github/workflows/model-checks.yml` for example.

## Ceilings

| Ceiling | Why | Upgrade path |
|---|---|---|
| an assertion tests the MODEL's behaviour, not the real system | there is no ground truth to compare against here -- that is what a reality check or a calibration run is for | none: assertions and reality checks are deliberately two different questions in one pass |
| the confidence interval's half-width shrinks only with more `replications` | a Student-t interval over independent replications is the same statistic every scenario comparison already uses (`core/stats.ci95`) | raise `replications` for a tighter interval, at the cost of a longer `naga check` run |
| assertions across several model FILES in one `naga check` call never share a run | each file is a different model, with nothing in common to batch | none needed -- batching happens where it is free, within one model's own assertions |
| an `inconclusive` assertion needs `--strict` (or `assert_model(strict=True)`) to fail a build | a straddling confidence interval is a genuine "don't know yet", not a bug -- CI should not silently treat it as a pass either | `--strict` for the CI job that must not tolerate an open question |
