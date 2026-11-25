Measured 2026-11-24 with Python 3.12.4 on Intel64 Family 6 Model 186 Stepping 2, GenuineIntel, 20 logical CPUs, Windows 11. SimPy 4.1.2, salabim 26.0.8, Ciw 3.2.7, Naga 0.21.0. Each model: 10 replications of 10,000 time units, the first 1,000 discarded, about 100,000 customers in all. CPU seconds, the minimum of 3 runs.

| Model | Library | Mean time in system (95% interval) | Theory | CPU seconds | Customers per second | Times SimPy's |
|---|---|---|---|---|---|---|
| `mm1`: M/M/1 queue | SimPy | 3.996 +/- 0.174 | 4.000 | 0.66 | 152,381 | 1.0 |
| `mm1`: M/M/1 queue | salabim | 3.996 +/- 0.174 | 4.000 | 17.14 | 5,834 | 26.1 |
| `mm1`: M/M/1 queue | Ciw | 4.315 +/- 0.342 | 4.000 | 1.08 | 92,754 | 1.6 |
| `mm1`: M/M/1 queue | Naga | 3.981 +/- 0.221 | 4.000 | 2.89 | 34,595 | 4.4 |
| `mm1`: M/M/1 queue | Naga, logs and sampler off | 3.981 +/- 0.221 | 4.000 | 1.94 | 51,613 | 3.0 |
| `mm3`: M/M/3 queue | SimPy | 6.101 +/- 0.357 | 6.011 | 0.67 | 148,837 | 1.0 |
| `mm3`: M/M/3 queue | salabim | 6.101 +/- 0.357 | 6.011 | 17.19 | 5,818 | 25.6 |
| `mm3`: M/M/3 queue | Ciw | 6.049 +/- 0.380 | 6.011 | 1.12 | 88,889 | 1.7 |
| `mm3`: M/M/3 queue | Naga | 5.943 +/- 0.327 | 6.011 | 2.91 | 34,409 | 4.3 |
| `mm3`: M/M/3 queue | Naga, logs and sampler off | 5.943 +/- 0.327 | 6.011 | 2.02 | 49,612 | 3.0 |
| `tandem`: Line of 3 stations | SimPy | 7.418 +/- 0.457 | 7.000 | 1.48 | 67,368 | 1.0 |
| `tandem`: Line of 3 stations | salabim | 6.930 +/- 0.386 | 7.000 | 21.81 | 4,585 | 14.7 |
| `tandem`: Line of 3 stations | Ciw | 6.988 +/- 0.399 | 7.000 | 3.17 | 31,527 | 2.1 |
| `tandem`: Line of 3 stations | Naga | 7.031 +/- 0.233 | 7.000 | 6.17 | 16,203 | 4.2 |
| `tandem`: Line of 3 stations | Naga, logs and sampler off | 7.031 +/- 0.233 | 7.000 | 4.33 | 23,105 | 2.9 |
| `jackson`: Job shop, 3 stations with rework | SimPy | 8.457 +/- 0.353 | 8.376 | 1.41 | 71,111 | 1.0 |
| `jackson`: Job shop, 3 stations with rework | salabim | 8.495 +/- 0.257 | 8.376 | 22.11 | 4,523 | 15.7 |
| `jackson`: Job shop, 3 stations with rework | Ciw | 8.474 +/- 0.342 | 8.376 | 3.02 | 33,161 | 2.1 |
| `jackson`: Job shop, 3 stations with rework | Naga | 8.348 +/- 0.208 | 8.376 | 6.28 | 15,920 | 4.5 |
| `jackson`: Job shop, 3 stations with rework | Naga, logs and sampler off | 8.348 +/- 0.208 | 8.376 | 4.55 | 21,993 | 3.2 |
