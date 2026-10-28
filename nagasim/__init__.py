__version__ = "0.19.0"

# The three names most scripts want, loaded on first use: a plain import of the
# package (or `naga --help`) must not pull in the engine (D915).
_LAZY = {"run_model": "nagasim.api", "Model": "nagasim.build", "Results": "nagasim.results"}


def __getattr__(attr: str):
    module = _LAZY.get(attr)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")
    import importlib
    return getattr(importlib.import_module(module), attr)


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
