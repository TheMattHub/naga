Measured 2026-11-23 with Python 3.12.4 on Intel64 Family 6 Model 186 Stepping 2, GenuineIntel, 20 logical CPUs, Windows 11. SimPy 4.1.1, salabim 26.0.8, Ciw 3.2.7, Naga 0.21.0. Each model: 10 replications of 10,000 time units, the first 1,000 discarded, about 100,000 customers in all. CPU seconds, the minimum of 3 runs.

| Model | Library | Mean time in system (95% interval) | Theory | CPU seconds | Customers per second | Times SimPy's |
|---|---|---|---|---|---|---|
| `mm1`: M/M/1 queue | SimPy | 3.996 +/- 0.174 | 4.000 | 0.61 | 164,103 | 1.0 |
| `mm1`: M/M/1 queue | salabim | 3.996 +/- 0.174 | 4.000 | 15.78 | 6,337 | 25.9 |
| `mm1`: M/M/1 queue | Ciw | 4.315 +/- 0.342 | 4.000 | 1.00 | 100,000 | 1.6 |
| `mm1`: M/M/1 queue | Naga | 3.981 +/- 0.221 | 4.000 | 3.00 | 33,333 | 4.9 |
| `mm1`: M/M/1 queue | Naga, logs and sampler off | 3.981 +/- 0.221 | 4.000 | 2.08 | 48,120 | 3.4 |
| `mm3`: M/M/3 queue | SimPy | 6.101 +/- 0.357 | 6.011 | 0.62 | 160,000 | 1.0 |
| `mm3`: M/M/3 queue | salabim | 6.101 +/- 0.357 | 6.011 | 15.95 | 6,268 | 25.5 |
| `mm3`: M/M/3 queue | Ciw | 6.049 +/- 0.380 | 6.011 | 1.06 | 94,118 | 1.7 |
| `mm3`: M/M/3 queue | Naga | 5.943 +/- 0.327 | 6.011 | 3.05 | 32,821 | 4.9 |
| `mm3`: M/M/3 queue | Naga, logs and sampler off | 5.943 +/- 0.327 | 6.011 | 2.12 | 47,059 | 3.4 |
| `tandem`: Line of 3 stations | SimPy | 7.418 +/- 0.457 | 7.000 | 1.42 | 70,330 | 1.0 |
| `tandem`: Line of 3 stations | salabim | 6.930 +/- 0.386 | 7.000 | 20.23 | 4,942 | 14.2 |
| `tandem`: Line of 3 stations | Ciw | 6.988 +/- 0.399 | 7.000 | 2.91 | 34,409 | 2.0 |
| `tandem`: Line of 3 stations | Naga | 7.031 +/- 0.233 | 7.000 | 6.58 | 15,202 | 4.6 |
| `tandem`: Line of 3 stations | Naga, logs and sampler off | 7.031 +/- 0.233 | 7.000 | 4.72 | 21,192 | 3.3 |
| `jackson`: Job shop, 3 stations with rework | SimPy | 8.457 +/- 0.353 | 8.376 | 1.33 | 75,294 | 1.0 |
| `jackson`: Job shop, 3 stations with rework | salabim | 8.495 +/- 0.257 | 8.376 | 21.14 | 4,730 | 15.9 |
| `jackson`: Job shop, 3 stations with rework | Ciw | 8.474 +/- 0.342 | 8.376 | 2.95 | 33,862 | 2.2 |
| `jackson`: Job shop, 3 stations with rework | Naga | 8.348 +/- 0.208 | 8.376 | 6.83 | 14,645 | 5.1 |
| `jackson`: Job shop, 3 stations with rework | Naga, logs and sampler off | 8.348 +/- 0.208 | 8.376 | 4.92 | 20,317 | 3.7 |
