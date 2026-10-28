These sections add the rest of a stock-and-flow model to the levels and flows. `auxiliaries` are named expressions. `delays` are material or information delays with a delay time. `subscripts` are dimensions that repeat a record once for each element, such as regions or age groups. System dynamics models use them together with `simulation.sd`.

## Behaviour and rules

An auxiliary is read with `AUX("name")` and is computed on each read. Auxiliaries that read each other in a cycle are an error. A delay is read with `DELAY("name")`. Its `kind` is `material`, `information` or `pipeline`. A material delay conserves the quantity, an information delay smooths a signal, and a pipeline returns the input exactly `delay_time` later.

Material and information delays need the step grid in `simulation.sd`. A pipeline does not. A delay time of 0 or less is an error. The inline forms such as `SMOOTH` and `DELAY3` write the same records for you, and they are allowed only in a flow rate, an auxiliary or a delay input.

A record with `dims` is expanded into one record for each element, named `Pop[north]`. Inside it, a name with the dimension in brackets reads the same element. `SUM(x)` and `MEAN(x)` over `x[*]` aggregate across the elements. A record can give `elements` to replace its first expression for chosen elements.
