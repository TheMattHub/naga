# Benchmarks

This page compares Naga with raw SimPy, salabim and Ciw on queueing models whose mean time in system is known exactly. A library is timed only if the exact value lies inside the confidence interval of its mean, so a fast but wrong implementation gets no row.

--8<-- "benchmarks/results.md"

The table is written by `benchmarks/compare.py` from its measurements and carries the date, the machine and the library versions it was measured with. To repeat it, install `benchmarks/requirements.txt` and run `python benchmarks/compare.py`. [Coming from SimPy](../coming-from/simpy.md) explains how to read the columns and why Naga is slower than a bare SimPy loop: it does more per event, and the statistics, logs and experiments are built on that.
