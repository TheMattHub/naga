"""The recipe library: the engine site's how-to/recipes/ pages, each a modelling question with a tiny
runnable script and its generated output. The scripts and outputs are checked by tests/test_engine_docs.py
(every snippets/*.py has its output file, regenerated and compared); this file checks the pages around them."""
import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
goals_tool = importlib.import_module("tools.gen_goals")

ENGINE = goals_tool.SITES["engine"]["docs"]
FOLDER = ENGINE / "how-to" / "recipes"
PAGES = sorted(p for p in FOLDER.glob("*.md") if p.name != "index.md")
SNIPPETS = ENGINE / "snippets"
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_library_holds_about_thirty_five_recipes_each_with_a_script_and_its_output():
    assert len(PAGES) >= 30
    for page in PAGES:
        assert (SNIPPETS / f"recipe-{page.stem}.py").is_file(), page.name
        assert (SNIPPETS / f"recipe-{page.stem}-output.txt").is_file(), page.name
    scripts = {p.name for p in SNIPPETS.glob("recipe-*.py")}
    assert scripts == {f"recipe-{p.stem}.py" for p in PAGES}, "a script without a page, or a page without a script"


def test_every_recipe_is_in_the_nav_and_the_index():
    nav = goals_tool.nav_pages("engine")
    index = _text(FOLDER / "index.md")
    assert "how-to/recipes/index.md" in nav
    for page in PAGES:
        assert f"how-to/recipes/{page.name}" in nav, f"{page.name} is not in the nav"
        assert f"]({page.name})" in index, f"{page.name} is not in the index"
    assert set(re.findall(r"\]\(([a-z0-9-]+\.md)\)", index)) - {"index.md"} == {p.name for p in PAGES}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_a_recipe_asks_a_question_and_follows_the_template(page):
    text = _text(page)
    title = text.splitlines()[0]
    assert re.match(r"# (How do I .+\?|What happens .+\?)$", title), title
    assert re.findall(r"^## (.+)$", text, re.M) == ["How it works", "Elements used"], page.name
    script = f'--8<-- "docs/snippets/recipe-{page.stem}.py"'
    output = f'--8<-- "docs/snippets/recipe-{page.stem}-output.txt"'
    assert text.index(script) < text.index(output), page.name
    steps = re.findall(r"^\d\. .+$", text, re.M)
    assert len(steps) == 3, f"{page.name}: the explanation is three lines"
    assert all(len(step.split()) <= 40 for step in steps), page.name
    assert re.search(r"^- (Distributions|Functions|Blocks|Model-file sections|Python): ", text, re.M), page.name


def _anchors(path: Path) -> set[str]:
    heads = re.findall(r"^#+ (.+)$", _text(path), re.M)
    return {re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-") for h in heads}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_the_elements_used_links_resolve(page):
    section = _text(page).split("## Elements used")[1]
    links = LINK.findall(section.split("Back to all")[0])
    assert links, page.name
    for link in links:
        target, _, anchor = link.partition("#")
        path = (page.parent / target).resolve()
        assert path.is_file(), f"{page.name}: {link}"
        if anchor:
            assert anchor in _anchors(path), f"{page.name}: {link}"


def _lines(stem: str) -> list[str]:
    return _text(SNIPPETS / f"recipe-{stem}-output.txt").splitlines()


def _numbers(line: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+\.?\d*", line)]


def test_what_the_recipes_say_about_their_output_is_true():
    """The prose reads the printed output; a regeneration cannot make it false unnoticed."""
    assert _lines("timetable-arrivals")[0] == "5 trucks unloaded"
    assert _lines("order-copies")[1] == "jobs per order: 3.0"
    assert _lines("belt-transit")[0].startswith("belt at 1.0 m/s: transit 30.0 s")
    assert _lines("belt-transit")[1].startswith("belt at 2.0 m/s: transit 15.0 s")
    # a site limit below the sum of the chargers stretches a charge
    wide, tight = (float(re.search(r"([\d.]+) minutes", line).group(1)) for line in _lines("site-power"))
    assert tight > 2 * wide
    # a buffer of one loses parts, five finishes as many as fifty
    rows = [_numbers(line) for line in _lines("buffer-wip")[1:]]
    assert rows[0][1] < rows[1][1] == rows[2][1] and rows[0][2] > rows[1][2] > rows[2][2] == 0
    # riders arrive faster than drivers, so some riders are still waiting; every driver is matched
    rows = {line.split()[0]: line.split() for line in _lines("pair-flows")[1:]}
    assert int(rows["RIDER"][1]) > int(rows["RIDER"][2]) and rows["DRIVER"][1] == rows["DRIVER"][2]
    # the Cauchy waits swing across runs, the tight normal does not
    cauchy, normal = ([float(x) for x in line.split()[-5:]] for line in _lines("wild-outliers")[1:])
    assert max(cauchy) - min(cauchy) > 20 * (max(normal) - min(normal))
    # present value is below the cash banked and within 1% of the exact figure
    last = _lines("discounted-cash")[-2]
    banked, present = _numbers(last)[1:3]
    exact = _numbers(_lines("discounted-cash")[-1])[-1]
    assert present < banked and abs(present - exact) / exact < 0.01
    # near-agents: the three sensors carry 10, 12 and 14, so sums over 5 m run from 22 to 26
    crowd = next(line for line in _lines("near-agents") if line.split()[0] == "crowd")
    assert _numbers(crowd)[-2:] == [22.0, 26.0]
    # a retest loop at p = 0.5 is busier than 3 successes at p = 0.8
    geometric, negative = (_numbers(line)[-1] for line in _lines("retest-attempts"))
    assert geometric > 2 * negative
    # smoothing cuts the spread of a noisy reading
    reading, smooth, smooth3 = (float(line.split()[-1]) for line in _lines("smooth-noise")[1:])
    assert smooth < reading / 3 and smooth3 < reading / 3
