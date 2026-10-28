"""The example model pages (site_docs/tutorials/, the pages the examples index lists) carry no
typed result number: a run's figures come from `tools/gen_docs.py gen_examples`, which writes a
results include and an "Elements used" include per page under site_docs/snippets/examples/."""
import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
gen_docs = importlib.import_module("tools.gen_docs")

PAGES = gen_docs.example_pages()
SNIPPETS = gen_docs.EXAMPLES_OUT
INCLUDE = '--8<-- "site_docs/snippets/examples/{page}-{kind}.md"'


def _text(page: str) -> str:
    return (gen_docs.TUTORIALS / f"{page}.md").read_text(encoding="utf-8")


def test_the_examples_index_lists_the_45_example_pages():
    assert len(PAGES) == 45
    assert all((gen_docs.TUTORIALS / f"{page}.md").is_file() for page in PAGES)


@pytest.mark.parametrize("page", sorted(PAGES))
def test_a_page_ends_with_elements_used_and_includes_its_results(page):
    text = _text(page).replace("\r\n", "\n").rstrip()
    last = text.rsplit("\n## ", 1)[-1]
    assert last.startswith("Elements used\n"), f"{page}: the last section must be 'Elements used'"
    assert INCLUDE.format(page=page, kind="elements") in last
    assert (INCLUDE.format(page=page, kind="results") in text) == (page not in gen_docs.NO_RESULTS)


def test_the_elements_includes_match_the_generator(tmp_path):
    """Cheap half of the drift guard: the elements lists need no run."""
    for page, stems in PAGES.items():
        committed = (SNIPPETS / f"{page}-elements.md").read_text(encoding="utf-8")
        assert committed.endswith("\n".join(gen_docs._elements_lines(stems)) + "\n"), (
            f"{page}-elements.md is out of date: run PY tools/gen_docs.py")


@pytest.mark.slow
def test_the_results_includes_match_a_fresh_seeded_run(tmp_path):
    """Runs every example model as the generator does and diffs the includes against the committed ones."""
    stale = []
    original = gen_docs.EXAMPLES_OUT
    gen_docs.EXAMPLES_OUT = tmp_path
    try:
        gen_docs.gen_examples()
    finally:
        gen_docs.EXAMPLES_OUT = original
    for fresh in sorted(tmp_path.glob("*.md")):
        if (SNIPPETS / fresh.name).read_text(encoding="utf-8") != fresh.read_text(encoding="utf-8"):
            stale.append(fresh.name)
    assert not stale, f"out of date, run PY tools/gen_docs.py and commit: {stale}"


# ---- no typed result numbers --------------------------------------------------------------------

# A section that works a number out on paper is checked by its own test (the oracle and reference-model
# tests named in the page), so its figures are derivations, not results.
BY_HAND = re.compile(r"check|by hand|theory", re.I)
NUMBER = re.compile(r"(?<![\w.])\d[\d , ]*(?:\.\d+)?(?:\s?%)?")
# Figures a page may state without a run: dates in the model's calendar and the like. Keep it short.
ALLOWED: dict[str, set[str]] = {
    # The model file holds the per-scenario capital totals, not the 0.8 M EUR per MW they come from.
    "berth-shore-power": {"0.8"},
    # The Loops That Matter output is the page's subject; tests/test_loopdominance.py guards it.
    "loop-dominance": {"0.80", "0.798936", "0.73", "0.730074", "0.67", "0.665685", "0.28", "23.0", "23.1",
                       "0.5006", "0.5019"},
}


def _normal(token: str) -> str:
    return re.sub(r"[ , ]", "", token.replace("%", "")).strip()


def _result_like(token: str) -> bool:
    n = _normal(token)
    if not n or (len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2100):
        return False
    return "." in n or "%" in token or (n.isdigit() and int(n) >= 1000)


def _prose_numbers(text: str):
    """(line, token) of every result-looking number in the page outside code, comments, headings
    and by-hand sections. A token counts when it has a decimal part, a percent sign, or is 1000 or
    more; inline code and link targets are removed first."""
    in_code, exempt = False, False
    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if line.startswith("#"):
            exempt = bool(BY_HAND.search(line)) and line.startswith("##")
            continue
        if in_code or exempt or line.startswith("<!--") or line.startswith("--8<--"):
            continue
        plain = re.sub(r"`[^`]*`|\]\([^)]*\)", " ", line)
        for match in NUMBER.finditer(plain):
            token = match.group(0).strip(" ,")
            if _result_like(token):
                yield number, token


def _model_numbers(stems: list[str]) -> set[str]:
    """Every number written in the page's model files, normalised: the page may restate an input."""
    found: set[str] = set()
    for stem in stems:
        raw = (gen_docs.engine.MODELS / f"{stem}.json").read_text(encoding="utf-8")
        for token in re.findall(r"-?\d+(?:\.\d+)?", raw):
            found.add(token.lstrip("-"))
            found.add(str(float(token)).rstrip("0").rstrip(".") if "." in token else token)
            found.add(str(round(float(token) * 100, 6)).rstrip("0").rstrip("."))   # 0.6 is "60 %"
    return found


@pytest.mark.parametrize("page", sorted(PAGES))
def test_a_page_types_no_result_number(page):
    """Heuristic, tuned to be honest rather than clever. Outside by-hand sections (headings with
    'check' or 'theory' in them, whose figures a test checks) a number with a decimal part, a percent
    sign or a value of 1000 or more must be an input written in the page's model files, or listed in
    ALLOWED with the reason. Anything else is a result and belongs in the generated include."""
    inputs = _model_numbers(PAGES[page] or [])
    stray = [(n, t) for n, t in _prose_numbers(_text(page))
             if _normal(t) not in inputs and _normal(t) not in ALLOWED.get(page, set())]
    assert not stray, f"{page}: typed numbers outside the generated include: {stray[:12]}"
