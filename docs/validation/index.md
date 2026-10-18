# Validation

Naga is checked against answers that are known without simulating, and anyone can repeat the checks. This section holds the evidence: models with an exact answer, the comparison with other libraries on speed and accuracy, and how to cite the engine.

- [Reference models](reference-models.md): queueing, epidemic, diffusion and tank models whose answer follows from a formula, beside what the engine gives and the interval around it.
- [Benchmarks](benchmarks.md): the same models run in SimPy, salabim, Ciw and Naga, with the time each takes and its distance from theory.
- [Citing](citing.md): the citation file, for a paper or a report.

Every number on these pages is written by a program from a run, never typed. A test regenerates the reference models and compares; the benchmark table is a measured file that carries the date, machine and versions it was measured with. A model without a formula cannot be validated this way, so for your own models use [model assertions](../model-assertions.md), which turn what you know about the system into checks that run with it.
