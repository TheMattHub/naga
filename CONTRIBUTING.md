# Contributing

Thanks for looking. Bug reports and small, focused pull requests are welcome.

## Before you open a pull request

- Run the tests: `pip install -e ".[plan,ml,sql,plot]" -r requirements-dev.txt`, then
  `pytest -q -n auto` (the fast set) and, for anything touching statistics or timing,
  `pytest -q -m "slow or not slow"`.
- Every change to the engine, its statistics or the model format comes with a test.
- A bug fix comes with a test that fails without it.
- Results must stay reproducible: the same model and seed give the same frames. If your
  change alters results on purpose, say so in the pull request and say why.
- English, ASCII output, `logging` rather than `print`, no `eval`/`exec`.

## Reporting a bug

A model file (or the smallest one that shows it), the command or the Python call, what you
expected and what you got. `naga validate model.json` output helps.

## Scope

This repository is the engine: the `nagasim` package and its command line. Requests about a
graphical editor do not belong here.
