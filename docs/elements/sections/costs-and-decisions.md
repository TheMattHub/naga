These sections turn a model into a business case. `fixed_costs` are costs of the scenario rather than of the run, such as equipment capex. `investment` defines the case that `naga invest` reports: payback, NPV and return on investment against a baseline scenario. `dispatch` plans storage over a rolling horizon at the lowest grid cost.

## Behaviour and rules

Running costs are not set here. They come from the cost fields of resources, fleets, entity types, flows and RECORD blocks. A fixed cost flagged `capex` is the investment that the saving has to repay. The `investment` section needs a scenario to compare with, and a check warns when only one scenario exists.

The case scales the observed window to `operating_hours_per_year`. The saving over the baseline is counted for `horizon_years` and discounted at `discount_rate`. A `margin_per_part` adds the value of extra good parts to the saving. A value of 0 makes it a pure cost case.

The `dispatch` plan drives only the levels marked as storage. It prices grid flows with their own `price`. Without a `dispatch` section, those storage and grid settings do nothing. Both sections are absent by default.
