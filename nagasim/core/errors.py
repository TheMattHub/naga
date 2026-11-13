"""Errors a model causes while it runs, as opposed to a fault in the engine."""

__all__ = ["ModelRunError"]


class ModelRunError(RuntimeError):
    """The model sent the run somewhere it cannot go on from (an entity no branch takes).
    A RuntimeError, as before; the command line prints it as one line, like a validation
    error (#1721), where an engine fault keeps its traceback.

    `block` and `code` (E_RUN_BLOCK) name the block that stopped the run (#1766), as they do
    on a ValueError, TypeError or ArithmeticError a block raised (`sim_runner.step_until`);
    exceptions carry their attributes back from a worker process."""
    block: str | None = None
    code: str | None = None
