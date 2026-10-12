Measured 2026-10-13 with Python 3.12.4 on Intel64 Family 6 Model 186 Stepping 2, GenuineIntel, 20 logical CPUs, Windows 11. SimPy 4.1.1, salabim 26.0.8, Ciw 3.2.7, Naga 0.6.0. Each model: 10 replications of 10,000 time units, the first 1,000 discarded, about 100,000 customers in all. CPU seconds, the minimum of 3 runs.

| Model | Library | Mean time in system (95% interval) | Theory | CPU seconds | Customers per second | Times SimPy's |
|---|---|---|---|---|---|---|
| `mm1`: M/M/1 queue | SimPy | 3.996 +/- 0.174 | 4.000 | 0.58 | 172,973 | 1.0 |
| `mm1`: M/M/1 queue | salabim | 3.996 +/- 0.174 | 4.000 | 15.53 | 6,439 | 26.9 |
| `mm1`: M/M/1 queue | Ciw | 4.315 +/- 0.342 | 4.000 | 1.03 | 96,970 | 1.8 |
| `mm1`: M/M/1 queue | Naga | 3.981 +/- 0.221 | 4.000 | 3.22 | 31,068 | 5.6 |
| `mm1`: M/M/1 queue | Naga, logs and sampler off | 3.981 +/- 0.221 | 4.000 | 2.47 | 40,506 | 4.3 |
| `mm3`: M/M/3 queue | SimPy | 6.101 +/- 0.357 | 6.011 | 0.62 | 160,000 | 1.0 |
| `mm3`: M/M/3 queue | salabim | 6.101 +/- 0.357 | 6.011 | 16.08 | 6,220 | 25.7 |
| `mm3`: M/M/3 queue | Ciw | 6.049 +/- 0.380 | 6.011 | 1.08 | 92,754 | 1.7 |
| `mm3`: M/M/3 queue | Naga | 5.943 +/- 0.327 | 6.011 | 3.25 | 30,769 | 5.2 |
| `mm3`: M/M/3 queue | Naga, logs and sampler off | 5.943 +/- 0.327 | 6.011 | 2.47 | 40,506 | 4.0 |
| `tandem`: Line of 3 stations | SimPy | 7.418 +/- 0.457 | 7.000 | 1.42 | 70,330 | 1.0 |
| `tandem`: Line of 3 stations | salabim | 6.930 +/- 0.386 | 7.000 | 20.33 | 4,919 | 14.3 |
| `tandem`: Line of 3 stations | Ciw | 6.988 +/- 0.399 | 7.000 | 2.97 | 33,684 | 2.1 |
| `tandem`: Line of 3 stations | Naga | 7.031 +/- 0.233 | 7.000 | 7.34 | 13,617 | 5.2 |
| `tandem`: Line of 3 stations | Naga, logs and sampler off | 7.031 +/- 0.233 | 7.000 | 5.58 | 17,927 | 3.9 |
| `jackson`: Job shop, 3 stations with rework | SimPy | 8.457 +/- 0.353 | 8.376 | 1.36 | 73,563 | 1.0 |
| `jackson`: Job shop, 3 stations with rework | salabim | 8.495 +/- 0.257 | 8.376 | 20.62 | 4,848 | 15.2 |
| `jackson`: Job shop, 3 stations with rework | Ciw | 8.474 +/- 0.342 | 8.376 | 2.86 | 34,973 | 2.1 |
| `jackson`: Job shop, 3 stations with rework | Naga | 8.348 +/- 0.208 | 8.376 | 7.33 | 13,646 | 5.4 |
| `jackson`: Job shop, 3 stations with rework | Naga, logs and sampler off | 8.348 +/- 0.208 | 8.376 | 5.64 | 17,729 | 4.1 |
