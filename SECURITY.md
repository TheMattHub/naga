# Security

A model file is data: loading and running one never executes code from it, expressions are
parsed by the engine's own grammar (no `eval`), and file or network access asked for by a model
(data bindings, imports) goes through `nagasim/netguard.py`, where the permission is the
caller's, never the file's.

If you find a way round any of that, or any other vulnerability, please do not open a public
issue. Use GitHub's private vulnerability reporting on this repository ("Security" tab, "Report a
vulnerability"). You will get an answer within a week.
