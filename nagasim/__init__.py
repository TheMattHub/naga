__version__ = "0.21.0"

# The three names most scripts want, loaded on first use: a plain import of the
# package (or `naga --help`) must not pull in the engine (D915).
_LAZY = {"run_model": "nagasim.api", "Model": "nagasim.build", "Results": "nagasim.results"}
# The modules the first pages import from, listed by `dir(nagasim)` (tab completion) and imported
# on first use, so `nagasim.api.<TAB>` works after a bare `import nagasim` (#1897).
_SUBMODULES = ("api", "experiments", "optimize", "appearance")


def __getattr__(attr: str):
    import importlib
    if attr in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{attr}")
    module = _LAZY.get(attr)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")
    return getattr(importlib.import_module(module), attr)


def __dir__():
    return sorted(set(globals()) | set(_LAZY) | set(_SUBMODULES))
