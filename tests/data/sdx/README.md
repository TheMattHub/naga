# SDXorg test-models (PHASE6 S11)

A small subset of https://github.com/SDXorg/test-models, source commit
`21aab02739dc5187bc9564e4d3de14e575905d2f` (2025-03-14), vendored for
`tests/test_sdx_conformance.py`. Repository licence: MIT (`LICENSE` in this
directory, copied verbatim). No per-model `README.md` in the upstream `tests/`
tree claims a different licence or author restriction (checked the `AUTHORS`
file and grepped every vendored model's own `README.md` for "copyright" /
"licence" -- none found), so the repository-wide MIT grant covers every file
below.

Each subdirectory is one upstream `tests/<name>/` model, trimmed to the model
file(s) and its canonical `output.csv`/`output.tab` (screenshots and other
formats dropped). Total size: about 75 KB.

| Directory | Upstream test | Category |
|---|---|---|
| `comparisons/` | `tests/comparisons` | builtins (comparison operators) |
| `eval_order/` | `tests/eval_order` | builtins (arithmetic, evaluation order) |
| `lookups_no_indirect/` | `tests/lookups` (the `_no-indirect` variant) | lookups + stocks/flows basics (a real stock/flow pair, a graphical function) |
| `trig/` | `tests/trig` | builtins (trig) |
| `smooth/` | `tests/smooth` | delays (SMOOTH family, default initial) |
| `min_max_1arg/` | `tests/min_max_1arg` | subscripts (array literal) |
| `abs/` | `tests/abs` (`.mdl` only) | stocks/flows basics (unbounded Vensim stock) |
| `rounding/` | `tests/rounding` (`.mdl` only) | builtins (MODULO, INTEGER) |
| `lookups_without_range/` | `tests/lookups_without_range` | lookups (standalone lookup variable) |
| `na/` | `tests/na` | builtins (`:NA:`) |
| `pi/` | `tests/pi` | builtins (PI) |
| `input_functions/` | `tests/input_functions` | builtins (STEP, PULSE, PULSE TRAIN, RAMP) |
| `non_negative_stocks/` | `tests/non_negative_stocks` | stocks/flows basics (bounded stock) -- xfail, see the test file |

Two candidates were tried and dropped rather than vendored, both upstream
corpus quirks confirmed at the pinned commit, not something to work around by
editing the vendored data:

- `tests/non_negative_flows` (and its `_behavior` sibling): malformed XML --
  an unclosed `<flow>` element -- so `xml.etree` refuses it before any of our
  logic runs.
- `tests/zeroled_decimals`: every one of its flow-like variables is tagged
  `<aux>`, not `<flow>` (only `<flow>`-tagged elements make it into a stock's
  own `inflow`/`outflow`, XMILE's own rule); every stock in that file would
  import as static.

Only `non_negative_stocks` is marked `xfail(strict=True)`: two flows each
drain two sibling stocks, which Naga's flow model (exactly two ends) cannot
represent. See `tests/test_sdx_conformance.py` for the reason string.
