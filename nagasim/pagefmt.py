"""How every hand-over page writes a result (D1236): one interval wording, one KPI label, one
name for the reference case. Used by the deck, the control room, the report, the player, the
viewer, the monitor and the client pack's README, so a director reads the same number the
same way on each.

    interval_text(0.276, 0.24, 0.312, "%", share=True)  "27.6% (95% interval 24.0 to 31.2)"
    kpi_label("system/Avg WIP", dashboard)              the author's label, else "Avg WIP"
    reference_name("it")                                the reference case in that language

The words come from the page string tables (`pagetext`), which the pages' scripts read too,
so Python and the browser say the same. Standard library only: the viewer ships this module
to the browser with the rest of the engine.
"""
from __future__ import annotations

import math

from nagasim import pagetext

__all__ = ["REFERENCE", "number", "interval_text", "interval_numbers", "kpi_label", "kpi_parts", "reference_name",
           "is_bookkeeping"]

# The engine's name for the reference case (`experiments.BASELINE`); Open 2 of the journeys tier
# rules it is the one word every page uses for it ("Baseline", never "base model" or "today").
REFERENCE = "Baseline"

# The KPI families of `experiments.flatten_kpis` keys: the prefix is bookkeeping, not a name.
_FAMILIES = ("system", "user", "resource", "store", "entity")


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sig(value: float, digits: int) -> str:
    """*digits* significant digits, plain notation (never 2.3e-05), trailing zeros dropped."""
    if value == 0:
        return "0"
    places = max(0, digits - 1 - math.floor(math.log10(abs(value))))
    text = f"{round(value, places):.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def number(value, lang: str | None = None, digits: int = 4) -> str:
    """A number for a reader: *digits* significant digits, the language's decimal sign; "-" for
    a missing or non-finite one (a page never shows "nan" or "-inf")."""
    if not _finite(value):
        return "-"
    return pagetext.number(_sig(float(value), digits), pagetext.table(lang))


def _glue(text: str, unit: str) -> str:
    if not unit:
        return text
    return text + unit if unit == "%" else f"{text} {unit}"


def interval_text(mean, lo=None, hi=None, unit: str = "", lang: str | None = None, *,
                  share: bool = False, signed: bool = False) -> str:
    """A mean with its 95% interval: "27.6% (95% interval 24.0 to 31.2)".

    The bounds and the mean share one number of decimals, chosen so the half-width shows two
    significant digits: the precision the run supports, no more. Without a usable interval
    (one replication, a NaN or an infinite bound) the mean alone, at four significant digits.
    *share*: the numbers are fractions shown as percentages (the unit becomes "%"). *signed*:
    a difference, every number with its sign ("+0.20 (95% interval +0.18 to +0.22)").
    *unit* follows the mean only. Words from `pagetext` (key "fmt.interval") in *lang*."""
    words = pagetext.table(lang)
    if not _finite(mean):
        return "-"
    scale = 100.0 if share else 1.0
    unit = "%" if share else (unit or "")
    mean_text, lo_text, hi_text = interval_numbers(float(mean) * scale, None if lo is None else lo * scale,
                                                   None if hi is None else hi * scale, signed=signed)
    if lo_text:
        return pagetext.say(words, "fmt.interval", mean=_glue(pagetext.number(mean_text, words), unit),
                            lo=pagetext.number(lo_text, words), hi=pagetext.number(hi_text, words))
    return _glue(pagetext.number(mean_text, words), unit)


def interval_numbers(mean, lo=None, hi=None, *, signed: bool = False, group: bool = False) -> tuple[str, str, str]:
    """(mean, low, high) as text, one number of decimals for all three: the half-width shows two
    significant digits, the precision the run supports (D1236). The one rule the hand-over pages
    (`interval_text`) and the editor's Summary table share. Without a usable interval (one
    replication, a NaN or an infinite bound) the mean alone at four significant digits and
    ("", "") for the bounds; a zero-width interval (every replication equal) all three at four
    significant digits, so a caller sees low == high. Python's decimal point; *group*: thousands separators ("1,528.5"); *signed*: a
    "+" on a positive number."""
    if not _finite(mean):
        return "-", "", ""
    mean = float(mean)
    sure = _finite(lo) and _finite(hi)
    half = abs(hi - lo) / 2 if sure else 0.0
    places = 0 if half == 0 else min(max(0, 1 - math.floor(math.log10(half))), 9)

    def fmt(value: float) -> str:
        # A zero interval (every replication equal) has no precision to follow: 4 digits.
        text = _sig(value, 4) if half == 0 else f"{value:.{places}f}"
        if float(text) == 0:
            text = text.lstrip("-")         # never "-0.0"
        elif signed and value > 0:
            text = "+" + text
        if group:
            head, dot, tail = text.partition(".")
            sign = head[:1] if head[:1] in "+-" else ""
            text = sign + f"{int(head[len(sign):]):,}" + dot + tail
        return text
    return fmt(mean), fmt(float(lo)) if sure else "", fmt(float(hi)) if sure else ""


def _entries(dashboard) -> list:
    """The results of a dashboard definition (a model's `dashboard` section or a resolved one)."""
    if not isinstance(dashboard, dict):
        return []
    return [entry for entry in dashboard.get("results") or () if isinstance(entry, dict)]


def kpi_parts(key: str, dashboard=None) -> tuple[str, str]:
    """(label, unit) of a KPI key: the dashboard's label and unit for it, else the plain name
    without its family prefix and no unit. "system/Avg WIP" -> ("Avg WIP", ""); a resource's
    "resource/cell_1/Machine/Utilization" -> ("Utilization - cell_1/Machine", "")."""
    key = str(key)
    for entry in _entries(dashboard):
        if entry.get("kpi") == key and entry.get("label"):
            return str(entry["label"]), str(entry.get("unit") or "")
    family, sep, rest = key.partition("/")
    if not sep or family not in _FAMILIES:
        return key, ""
    if family in ("system", "user"):
        return rest, ""
    owner, sep, column = rest.rpartition("/")
    return (f"{column.replace('_', ' ')} - {owner}" if sep else rest.replace("_", " ")), ""


def kpi_label(key: str, dashboard=None) -> str:
    """The name a reader sees for a KPI: the dashboard's label, else the plain name without its
    family prefix (never "system/..."), with its unit in brackets when the dashboard gives one."""
    label, unit = kpi_parts(key, dashboard)
    return f"{label} ({unit})" if unit else label


def reference_name(lang: str | None = None) -> str:
    """The reference case's name in *lang* (`REFERENCE` in English)."""
    return pagetext.table(lang)["name.baseline"]


def is_bookkeeping(key: str, values=None) -> bool:
    """True for a row a client page leaves out: the engine's one display rule,
    `core.outputs.hidden_row` (D1251), under the pages' name."""
    from nagasim.core.outputs import hidden_row     # late: the rule's module loads pandas
    return hidden_row(key, values)
