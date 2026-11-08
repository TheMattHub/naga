"""The words of the shared pages (D1056): one string table per language, chosen when a
player or a viewer is written, so a file speaks one language and holds no switcher.

    table("it")                {key: text}, every key of the English table
    say(words, key, **values)  one text with its {placeholders} filled
    number(text, words)        a number's text with the language's decimal sign
    markup(page, words)        the constant page with each {{key}} as its HTML-safe text

The tables are data (`templates/pagetext.<language>.json`), not code: the pages' scripts
stay constant and pinned by hash, and read the table from the page's data block. The
Italian text is kept there because engine source is ASCII (tests/test_hygiene.py); the
pages stay ASCII because the data block and `markup` escape every other character.
"""
from __future__ import annotations

import functools
import html
import json
import re
from pathlib import Path

__all__ = ["LANGUAGES", "DEFAULT", "table", "say", "number", "markup", "Refusal", "page_words",
           "explain"]

LANGUAGES = ("en", "it")
DEFAULT = "en"
_HERE = Path(__file__).parent / "templates"


@functools.lru_cache(maxsize=None)
def _read(language: str) -> dict:
    return json.loads((_HERE / f"pagetext.{language}.json").read_text(encoding="utf-8"))


def table(language: str | None = None) -> dict:
    """{key: text} of *language* (default English). A ValueError names the languages
    there are. A copy: the caller may not change the shared one."""
    language = language or DEFAULT
    if language not in LANGUAGES:
        raise ValueError(f"language {str(language)[:20]!r}: choose one of {', '.join(LANGUAGES)}")
    return dict(_read(language))


def say(words: dict, key: str, **values) -> str:
    """The text of *key* with its {name} placeholders filled from *values*."""
    return re.sub(r"\{([a-z_]+)\}", lambda found: str(values.get(found.group(1), found.group(0))),
                  words[key])


def number(text: str, words: dict) -> str:
    """*text*, a number written by Python, with the language's decimal sign."""
    return text.replace(".", words["decimal"]) if words["decimal"] != "." else text


def _html(text: str) -> str:
    """HTML text in ASCII: every other character as a numeric reference."""
    return html.escape(text, quote=True).encode("ascii", "xmlcharrefreplace").decode("ascii")


def markup(page: str, words: dict) -> str:
    """*page* (constant markup) with each {{key}} replaced by that key's text, escaped."""
    return re.sub(r"\{\{([a-z_.]+)\}\}", lambda found: _html(words[found.group(1)]), page)


# -- errors (D1088) --------------------------------------------------------------------
# A page tells its reader why a run did not start in the page's language, for the problem
# codes a viewer's levers and run can raise (`error.<CODE>.message` and `.fix`) and for the
# lever refusals that have no code (`lever.<reason>`). Every other message stays as the engine
# wrote it, with its code shown. English pages say what the engine says, unchanged.
class Refusal(ValueError):
    """A ValueError whose message is English and whose cause a page can word: *reason* names
    a `lever.<reason>` text filled from *values*, *structured* holds `Problem`s (codes)."""

    def __init__(self, message: str, reason: str = "", values: dict | None = None, structured=()):
        super().__init__(message)
        self.reason, self.values, self.structured = reason, dict(values or {}), list(structured)


_PYTHON_AREAS = ("error.", "lever.", "measured.", "monitor.")


def page_words(words: dict, areas: tuple = ()) -> dict:
    """The part of a table a page's script reads. The areas Python words itself (errors,
    levers, measured texts) stay out, and the monitor's only goes into the monitor's page
    (*areas* names it): a page is as small as it was."""
    left = tuple(area for area in _PYTHON_AREAS if area not in areas)
    return {key: text for key, text in words.items() if not key.startswith(left)}


def _coded(words: dict, code: str, where: str) -> str | None:
    """One problem as a sentence, or None for a code the table has no text for."""
    message, fix = words.get(f"error.{code}.message"), words.get(f"error.{code}.fix")
    if message is None or fix is None:
        return None
    where = " ".join(str(where or "").split())[:60]
    return f"{where + ': ' if where else ''}{message} {words['error.fix']} {fix} ({code})"


def explain(exc: BaseException, language: str | None = None) -> str:
    """Why a run was refused, in *language*, cut to 500 characters. English: the engine's
    own text. Another language: the problems it holds worded from the table, any the table
    lacks as the engine wrote them with their code."""
    text = str(exc)[:500]
    if (language or DEFAULT) == DEFAULT:
        return text
    words = table(language)
    reason = getattr(exc, "reason", "")
    if reason and f"lever.{reason}" in words:
        # A number with the language's sign; a whole float (a bound of 1e6) without ".0".
        shown = {k: number(str(int(v) if isinstance(v, float) and v.is_integer() else v), words)
                 if isinstance(v, (int, float)) else str(v)[:40]
                 for k, v in exc.values.items()}
        return say(words, f"lever.{reason}", **shown)[:500]
    lines = []
    for found in list(getattr(exc, "structured", None) or [])[:3]:
        lines.append(_coded(words, found.code, found.block or found.path)
                     or f"{found.message} ({found.code})")
    if not lines:
        code = re.match(r"(E_[A-Z_]+)\b", text)     # a run-time error that starts with its code
        return ((code and _coded(words, code.group(1), "")) or text)[:500]
    return " ".join(lines)[:500]
