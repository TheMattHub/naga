# Investment case

An investment case compares each scenario with a baseline and reports whether the
extra capital pays for itself: payback, net present value (NPV) and return on
investment (ROI). It is computed from a scenario run, so set the scenarios up
first (see [Experiments](../experiments.md)).

## Setting it up

The model's `investment` section:

| Field | Meaning |
|---|---|
| `baseline` | the scenario everything is compared with; empty means the model as saved |
| `horizon_years` | years the saving is counted over, default 5 |
| `discount_rate` | per year, default 0.08 |
| `operating_hours_per_year` | default 4000; the run is scaled to this many hours |
| `margin_per_part` | money earned per extra good part, default 0 (a pure cost case) |
| `throughput` | the KPI that counts good parts; empty means completed entities |

Mark each upfront investment with `capex: true` on its row of `fixed_costs`. A
`capex` cost still appears in `Total Cost` as before; the investment case alone treats
it as capital instead of operating cost. A fixed cost without it is scaled to a year
with the rest of the run's cost, so give it as the amount for the run window, not for a
year.

A scenario can change a fixed cost but cannot add one. Give the saved model the
investment's row with amount 0, and let the scenario set the amount:

```json
"fixed_costs": [{"name": "Robot cell", "category": "Capital", "amount": 0, "capex": true}],
"scenarios": [{"name": "Robot cell",
               "overrides": [{"path": "fixed_costs.Robot cell.amount", "value": 85000}]}]
```

## What is computed

For each scenario, with its own observed run window scaled to a year:

- **Capex**: the sum of its `capex` fixed costs.
- **Operating cost per year**: (`Total Cost` minus capex) scaled to a year.
- **Parts per year**: the throughput scaled to a year.

Against the baseline:

- **Extra capex**: capex minus the baseline's.
- **Saving per year**: the baseline's operating cost minus the scenario's, plus
  `margin_per_part` times the extra parts per year.
- **Payback years**: extra capex divided by the yearly saving, not discounted.
  "never" when there is no saving; 0 when the scenario needs no extra capital.
- **NPV**: minus the extra capex, plus each year's saving discounted at
  `discount_rate` over the horizon (a part year counts pro rata).
- **ROI %**: (horizon times saving, minus extra capex) over extra capex.

Saving and NPV carry a 95 % confidence interval over the replications; scenarios
share random numbers, so the interval is on the difference, replication by
replication. One replication gives no interval.

## Where to read it

- `naga invest MODEL`, or the "Investment" sheet of `naga scenarios --out file.xlsx`.
- `nagasim.investment.investment_case(result, layout)` in a notebook.
- The agent tool `investment_case`.

The bundled model `automation_cell` is a worked example.

## Not included

Tax, depreciation, financing, discounted payback and a sensitivity table. Run the
study again with other inputs to see how the case moves.
