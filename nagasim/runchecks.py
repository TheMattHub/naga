"""RunChecks: the law checks of one run, as `Results.checks()` returns them (D906).

A thin, frozen view over the list `nagasim.laws.checks` produces: the count of
passed checks, the partial ones (the warnings), one line per warning and the
`laws.summary` shape the agent digest carries. Shown, never enforced (D907):
nothing here changes a result or raises into a run.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

__all__ = ["RunChecks"]


@dataclass(frozen=True)
class RunChecks:
    """The law checks of one run.

    Attributes
    ----------
    found : tuple of dict
        Every check `nagasim.laws.checks` made, each with ``name``, ``verdict``
        and ``why``.
    note : str
        Why there are no checks, when there are none ("no layout", or "checks
        failed: ..."); empty otherwise.
    """

    found: tuple[dict, ...]
    note: str = ""

    @property
    def passed(self) -> int:
        """How many checks passed."""
        return sum(c["verdict"] == "passed" for c in self.found)

    @property
    def partial(self) -> tuple[dict, ...]:
        """The checks that did not pass: the warnings."""
        return tuple(c for c in self.found if c["verdict"] != "passed")

    def lines(self) -> list[str]:
        """One ``"<name>: <why>"`` line per warning."""
        return [f"{c['name']}: {c['why']}" for c in self.partial]

    def as_dict(self) -> dict:
        """`nagasim.laws.summary` shape (``passed``, ``partial``) plus ``note``."""
        return {"passed": self.passed, "partial": list(self.partial), "note": self.note}

    def _repr_html_(self) -> str:
        """Jupyter display: the warnings as a list, or a one-line all-clear."""
        lines = self.lines()
        if not lines:
            return f"<p>{self.passed} checks passed{'; ' + html.escape(self.note) if self.note else ''}</p>"
        items = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
        return f"<ul>{items}</ul>"
