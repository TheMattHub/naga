# Stability and deprecation

Before version 1.0 the Python API may change between minor versions, and every such change has a line in the [release notes](changelog.md). This page says what you can rely on in the meantime and what you cannot.

- **Model files always migrate forward.** A file written for an older schema version is upgraded when it is loaded, and the JSON Schema of the current version is published at its own address (`schema/model-v<N>.json`).
- **Agent tool names and problem codes are permanent.** An agent or a script that matches a code keeps working across releases.
- **The Python API may change before 1.0.** Pin the version in a script you cannot touch again (`nagasim==<the version you tested>`).
- **Behaviour changes are announced.** A change to what a model computes is in the highlights of [What's new](whats-new.md) and in the release notes, never silent.

Once 1.0 is released, the Python API will follow the usual rule that a minor release adds and a major release may remove, with a deprecation warning one minor version ahead.
