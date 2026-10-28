# Coming from Vensim

Naga integrates stocks and flows on a fixed time step, with the functions a Vensim model uses
most, and reads a `.mdl` file for a common subset of the language. It has no sketch: a model is
a JSON file, and its equations are expressions in it.

## Import a model

<!-- no-run: needs a .mdl file of yours -->
```console
naga sdimport model.mdl -o model.json
naga run model.json
```

What the reader translates and what it reports and leaves out is listed under
[Import from XMILE or Vensim](../system-dynamics.md#import-from-xmile-or-vensim). In short:
`INTEG` stocks whose rate is a flow name or a sum and difference of flow names, `WITH LOOKUP`,
`DELAY1`, `DELAY3` and their `I` forms, subscript ranges, subranges and mappings, per-element
equations and `SUM`, `PROD`, `VMIN`, `VMAX`. A variable name with spaces is read with
underscores. A macro, a `GET XLS` or `GET DIRECT` reference, or a function the reader does not
know is reported with the variable's name and left out; the rest of the model is still written.
Read the report before you trust the result.

## What maps to what

| Vensim | Here |
|---|---|
| Level (`INTEG`) | an entry in `levels` |
| Rate | an entry in `flows`, with one source and one target |
| Auxiliary, constant | an entry in `auxiliaries`, read as `AUX("name")` |
| Lookup | an entry in `tables`, read as `TABLE("name", x)` |
| `DELAY1`, `DELAY3`, `DELAY N`, `SMOOTH`, `SMOOTH3`, `DELAY FIXED` | `DELAY1`, `DELAY3`, `DELAYN`, `SMOOTH`, `SMOOTH3`, `DELAYP` |
| `STEP`, `PULSE`, `RAMP`, `IF THEN ELSE` | `STEP`, `PULSE`, `RAMP`, `IF` |
| Subscripts | subscripts on levels, flows and auxiliaries, with `SUM` and `MEAN` over them |
| Units check | unit checking of equations, reported as problems |
| `TIME STEP`, integration type | `simulation.sd`: `dt` and `euler` or `rk4` |
| Reality Check | `reality_checks` in the model, run by `naga check` |
| Optimisation for calibration | `naga calibrate`, to KPIs or to a time series |
| Sensitivity (Monte Carlo) | `naga doe` with a Latin hypercube design |
| Loops tool | `naga loops`: which feedback loop dominates, and when |

The function names and arities are in the
[expression reference](../reference/functions/index.md).

## What is different or missing

- **No diagram.** No sketch, no causal tracing, no graphs on the page. Results are frames; the
  HTML report draws time series.
- **A flow has two ends.** One source and one target, either of which may be outside the model.
  An equation that drains two stocks through one rate is refused.
- **Not translated by the reader:** a level whose rate is a compound expression rather than
  flow names, macros, external data functions, subscript equivalences (`<->`), and any function
  the engine does not have (the allocation functions, for example). Each is reported by name.
- **Only Euler and fourth-order Runge-Kutta**, on a fixed step.
- **No Markov chain Monte Carlo and no Kalman filtering.**

## What you gain

Discrete entities, resources and agents in the same model as the stocks, on the same clock: a
level can be drawn down by entities passing a TRANSFER block, and an agent's action can move an
amount between levels. The bundled `sd_*.json` models are the classic structures, with their
numbers in the [gallery](../gallery/system-dynamics.md).
