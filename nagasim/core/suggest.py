"""One close-match helper for every "unknown name" message (D910): a misspelt
key, parameter or field names the right one instead of only listing them all."""
from __future__ import annotations

import difflib
from collections.abc import Iterable

__all__ = ["close", "did_you_mean"]


def close(word: str, options: Iterable[str], n: int = 3, cutoff: float = 0.6) -> list[str]:
    """The options closest to *word*, best first; empty when none is close."""
    return difflib.get_close_matches(str(word), [str(o) for o in options], n=n, cutoff=cutoff)


def did_you_mean(word: str, options: Iterable[str]) -> str:
    """`"did you mean 'x'?"` for the closest option, or `""` when none is close."""
    near = close(word, options, n=1)
    return f"did you mean '{near[0]}'?" if near else ""
