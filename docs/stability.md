# Stability and deprecation

Before version 1.0 the Python API may change between minor versions, and every such change has a line in the [release notes](changelog.md). This page says what you can rely on in the meantime and what you cannot.

- **Model files always migrate forward.** A file written for an older schema version is upgraded when it is loaded, and the JSON Schema of the current version is published at its own address (`schema/model-v<N>.json`).
- **Agent tool names and problem codes are permanent.** A code keeps its meaning, and a code that is retired (0.18.0 retired `W_SET_PARTIAL_HOLD` when the case it warned about became impossible) is never reused, so an agent or a script that matches a code never reads a different problem under it.
- **The list of problem codes and their wording may change before 1.0.** New codes are added and a message may be reworded, so match on the code and not on the text; the [list](reference/problems/index.md) is generated.
- **The Python API may change before 1.0.** Pin the version in a script you cannot touch again (`nagasim==<the version you tested>`).
- **Behaviour changes are announced.** A change to what a model computes is in the highlights of [What's new](whats-new.md) and in the release notes, never silent.

Once 1.0 is released, the Python API will follow the usual rule that a minor release adds and a major release may remove, with a deprecation warning one minor version ahead.

## Stable at 1.0

When 1.0 is released, this is the surface that keeps its names, arguments and meaning under the rule above. No date is set for it. Everything else in the package, such as modules under `nagasim.core`, may still change in a minor release.

- **`run_model`**: its arguments and the `Results` it returns.
- **`Model` and `build`**: the Python builder in `nagasim.build` and the model file it writes.
- **`Results` methods**: `frame`, `kpis`, `kpi`, `checks`, `plot` and `to_excel`.
- **`run_scenarios` and `what_if`**: the scenario comparison and the what-if question, with their results.
- **Agent tool names**: a tool keeps its name, and so does a script that calls it.
- **Problem codes**: a code keeps its meaning.
- **Model file migration**: a file written for an older schema version still loads.
