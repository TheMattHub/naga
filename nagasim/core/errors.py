"""Errors a model causes while it runs, as opposed to a fault in the engine."""


class ModelRunError(RuntimeError):
    """The model sent the run somewhere it cannot go on from (an entity no branch takes).
    A RuntimeError, as before; the command line prints it as one line, like a validation
    error (#1721), where an engine fault keeps its traceback."""
