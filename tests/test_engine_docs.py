"""The engine's own documentation site (`docs/`, `mkdocs.yml`): its generated pages are
the generator's, and nothing on it has rotted. `tools/gen_engine_docs.py` writes the
generated pages; this file regenerates them and compares."""
import importlib
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
gen = importlib.import_module("tools.gen_engine_docs")

DOCS = gen.DOCS
REGENERATE = "out of date: run `python tools/gen_engine_docs.py` and commit"
DOCS_PYTHON = (3, 12)      # the version both docs workflows build the site with


def _same(written: Path, committed: Path) -> bool:
    return committed.is_file() and (written.read_text(encoding="utf-8")
                                    == committed.read_text(encoding="utf-8"))


def _files(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


@pytest.mark.parametrize("generate", [pytest.param(g, marks=pytest.mark.slow) if g is gen.gen_expressions else g
                                      for g in gen.REFERENCE], ids=lambda g: g.__name__)
def test_a_reference_page_is_the_generators(generate, tmp_path):
    generate(tmp_path)
    if generate is gen.gen_cli and sys.version_info[:2] != DOCS_PYTHON:
        # The pages are argparse's own `--help` text, and argparse words an option
        # differently from Python 3.13 on ("-o, --out FILE"). They are compared on the
        # version the site is built with; the commands they list are checked on all.
        heads = lambda path: [line for line in path.read_text(encoding="utf-8").splitlines()
                              if line.startswith("## ")]
        stale = [name for name in _files(tmp_path)
                 if heads(tmp_path / name) != heads(DOCS / "reference" / name)]
    else:
        stale = [name for name in _files(tmp_path) if not _same(tmp_path / name, DOCS / "reference" / name)]
    assert not stale, f"{stale} {REGENERATE}"


def test_the_reference_folder_holds_only_generated_pages(tmp_path):
    for generate in gen.REFERENCE:
        if generate is not gen.gen_kpis:        # the one that runs a model; its name is known
            generate(tmp_path)
    pages = lambda names: [name for name in names if name.endswith(".md")]     # a chart is a file beside its page
    assert pages(_files(DOCS / "reference")) == sorted([*pages(_files(tmp_path)), "kpis.md"])


def test_cli_reference_is_split_by_group():
    """One page per group, every command on exactly one of them, none longer than a screenful
    of sections would suggest (the one page it replaced was 900 lines)."""
    from nagasim.cli import COMMANDS

    folder = DOCS / "reference" / "cli"
    names = [name for *_head, group in gen.CLI_GROUPS for name in group]
    assert sorted(names) == sorted(COMMANDS)
    assert sorted(p.name for p in folder.glob("*.md")) == sorted(
        ["index.md", *(f"{slug}.md" for slug, *_rest in gen.CLI_GROUPS)])
    for slug, *_rest, group in gen.CLI_GROUPS:
        text = (folder / f"{slug}.md").read_text(encoding="utf-8")
        assert [line for line in text.splitlines() if line.startswith("## ")] == [
            f"## naga {name}" for name in group]
    assert not (DOCS / "reference" / "cli.md").exists()


# -- every command and snippet on the site is run (D345) ----------------------
#
# A ```console fence holds commands, one a line (`naga ...`, `python ...`; a `pip` line is
# not run); a ```python fence is executed, the fences of one page sharing a namespace; a
# fence with title="name" is written as that file first. Each page runs in its own scratch
# folder, which holds two stand-ins the pages name, `model.json` and `line.json`, and nothing
# else: a bundled model is named, not found by path. A fence that cannot run here says why on
# the line before it: <!-- no-run: reason -->. Output is not compared. The README is a page.

MODELS = ROOT / "nagasim" / "models"
STAND_INS = {"model.json": "single_server.json", "line.json": "single_server.json"}
# `elements/` holds the fragments that generated pages inline, not pages: their links read from the page.
PAGES = sorted(p for p in DOCS.rglob("*.md") if p.relative_to(DOCS).parts[0] != "elements")
RUN = [*PAGES, gen.SITE / "README.md"]
ELEMENT_FOLDERS = ("blocks", "distributions", "functions")


def _element_page(page: Path) -> bool:
    """A block, distribution or function page: its examples ran when the page was generated,
    and the generated-pages test fails if one does not, so the fence runner leaves them out."""
    return page.parent.parent == DOCS / "reference" and page.parent.name in ELEMENT_FOLDERS


FENCED = [p for p in RUN if not _element_page(p)]
COMMAND = re.compile(r"^\s*(?:\$ )?(naga|python|pip) ")
EXCUSE = re.compile(r"^<!-- no-run: \S.* -->$")


def fences(text: str):
    """(language, title, body, excused, first line number) of every fenced block."""
    lines, found, start, previous = text.split("\n"), [], None, ""
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            if start is None:
                start, info, excused = number, line.strip()[3:], bool(EXCUSE.match(previous))
            else:
                title = re.search(r'title="([^"]+)"', info)
                found.append((info.split(" ")[0], title and title.group(1),
                              "\n".join(lines[start:number - 1]), excused, start))
                start = None
        if line.strip():
            previous = line.strip()
    return found


def _commands(body: str):
    joined = body.replace("\\\n", " ")              # a command continued on the next line
    return [args for args in (shlex.split(line, comments=True) for line in joined.split("\n")) if args]


INCLUDE = re.compile(r'^--8<-- "([^"]+)"\s*$', re.M)


def _expanded(body: str) -> str:
    """A fence's body with its `--8<-- "path"` includes replaced by the file, as the site build does."""
    return INCLUDE.sub(lambda m: (gen.SITE / m.group(1)).read_text(encoding="utf-8").rstrip(chr(10)), body)


def _runnable(page: Path) -> bool:
    return any(language in ("console", "python") and not excused
               for language, _t, _b, excused, _n in fences(page.read_text(encoding="utf-8")))


@pytest.mark.parametrize("page", [p for p in FENCED if _runnable(p)],
                         ids=lambda p: p.relative_to(gen.SITE).as_posix())
@pytest.mark.docs
@pytest.mark.slow
def test_the_commands_and_snippets_of_a_page_run(page, tmp_path, monkeypatch, capsys):
    from nagasim import cli

    for name, model in STAND_INS.items():
        shutil.copy2(MODELS / model, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    namespace = {"__name__": "__main__"}
    for language, title, body, excused, number in fences(page.read_text(encoding="utf-8")):
        where = f"{page.name}:{number}"
        if title:
            (tmp_path / title).write_text(body + "\n", encoding="utf-8")
        elif excused:
            continue
        elif language == "python":
            code = _expanded(body)
            for library in ("salabim", "mesa", "ortools"):         # docs-only pins; the page runs where they are installed
                if f"import {library}" in code:
                    pytest.importorskip(library)
            exec(compile(code, where, "exec"), namespace)
        elif language == "console":
            for args in _commands(body):
                if args[0] == "$":
                    args = args[1:]
                if args[0] == "naga":
                    try:
                        code = cli.main(args[1:])
                    except SystemExit as stop:
                        code = stop.code
                    assert not code, f"{where}: {' '.join(args)} -> exit {code}\n{capsys.readouterr()}"
                elif args[0] == "python":
                    done = subprocess.run([sys.executable, *args[1:]], capture_output=True, text=True,
                                          timeout=300, env={**os.environ, "PYTHONPATH": str(ROOT)})
                    assert done.returncode == 0, f"{where}: {' '.join(args)}\n{done.stdout}{done.stderr}"
                else:
                    assert args[0] == "pip", f"{where}: a console fence holds commands, not '{args[0]}'"


def test_no_command_hides_outside_a_console_fence():
    """A command in an indented block or in another kind of fence would never be run."""
    hidden = []
    for page in FENCED:
        if page.parent.name == "reference":
            continue                                    # generated: --help text, usage lines
        text = page.read_text(encoding="utf-8")
        inside = set()
        for language, _title, body, _excused, number in fences(text):
            if language == "console":
                inside.update(range(number, number + body.count("\n") + 2))
        hidden += [f"{page.name}:{number}: {line.strip()[:60]}"
                   for number, line in enumerate(text.split("\n"), 1)
                   if COMMAND.match(line) and number not in inside]
    assert not hidden, "\n".join(hidden)


# -- the gallery (D344): a number on a page is the generator's, never typed ----

CATALOGUE = gen.bundled_models()


@pytest.mark.parametrize("category", list(CATALOGUE))
@pytest.mark.docs
@pytest.mark.slow
def test_a_gallery_pages_numbers_are_the_generators(category, tmp_path):
    """Regenerated by running the page's models (seed 0); compared whole, so a typed or
    stale number, description or diagram fails."""
    gen.gen_gallery(tmp_path, only=category)
    (page,) = tmp_path.iterdir()
    assert _same(page, DOCS / "gallery" / page.name), f"gallery/{page.name} {REGENERATE}"


def test_the_gallery_shows_every_bundled_model_and_nothing_else(tmp_path):
    (tmp_path / "index.md").write_text("", encoding="utf-8")
    gen._write(tmp_path, "index.md", [gen.gallery_index(CATALOGUE)])
    assert _same(tmp_path / "index.md", DOCS / "gallery" / "index.md"), f"gallery/index.md {REGENERATE}"
    pages = {f"{gen._slug(category)}.md" for category in CATALOGUE} | {"index.md"}
    assert {p.name for p in (DOCS / "gallery").glob("*.md")} == pages
    shown = "".join((DOCS / "gallery" / name).read_text(encoding="utf-8") for name in pages)
    missing = [p.name for p in sorted(MODELS.glob("*.json")) if f"naga run {p.stem}" not in shown]
    assert not missing, missing


# -- the worked examples: their numbers and chart data are the generator's ------

@pytest.mark.docs
@pytest.mark.slow
def test_the_worked_examples_and_their_chart_data_are_the_generators(tmp_path):
    """Regenerated by running their models (seed 0) and compared whole, with the CSV files
    the charts are drawn from. The SVG bytes are not compared (fonts and matplotlib differ
    by machine): every chart a page shows is drawn, and nothing else is."""
    gen.gen_worked_examples(tmp_path)
    folder = DOCS / "worked-examples"
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    stale = [name for name in written if not _same(tmp_path / name, folder / name)]
    assert not stale, f"{stale} {REGENERATE}"
    committed = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                       if p.suffix in (".md", ".csv"))
    assert committed == written
    shown = {name for page in folder.glob("*.md")
             for name in re.findall(r"\]\((img/[^)]+)\)", page.read_text(encoding="utf-8"))}
    drawn = {f"img/{p.name}" for p in (folder / "img").iterdir()}
    assert shown == drawn == {f"img/{stem}.svg" for stem in gen.CHARTS}


def test_the_published_model_schema_is_the_generators_at_its_id(tmp_path):
    """D470: the schema a model's `"$schema"` names is on the site, at its `$id`."""
    from nagasim.modelschema import SCHEMA_ID

    gen.gen_model_schema(tmp_path)
    (page,) = tmp_path.iterdir()
    assert _same(page, DOCS / "schema" / page.name), f"schema/{page.name} {REGENERATE}"
    config, _nav = gen.site_nav()
    assert SCHEMA_ID == config["site_url"].rstrip("/") + "/schema/" + page.name
    named = {m for p in DOCS.rglob("*.md")
             for m in re.findall(r"/schema/(model-v\d+\.json)", p.read_text(encoding="utf-8"))}
    assert named == {page.name}, "a page names the current schema version's file"


# -- snippets: code and output shown on a pages are the generator's -------------

def test_every_snippet_has_an_output_file_and_no_other_does():
    folder = DOCS / "snippets"
    assert sorted(p.name for p in folder.glob("*-output.txt")) == sorted(
        gen.output_name(p) for p in folder.glob("*.py"))


@pytest.mark.parametrize("source", sorted((DOCS / "snippets").glob("*.py")), ids=lambda p: p.name)
@pytest.mark.docs
@pytest.mark.slow
def test_the_snippet_outputs_are_the_generators(source, tmp_path):
    """One test a snippet, so the long ones run side by side under xdist. Only a snippet that
    imports salabim or mesa is skipped where that library is absent (both are pinned in
    requirements-docs.txt, not in requirements-dev.txt)."""
    for library in ("salabim", "mesa", "ortools"):
        if f"import {library}" in source.read_text(encoding="utf-8"):
            pytest.importorskip(library)
    gen.gen_snippets_one(source, tmp_path)
    name = gen.output_name(source)
    assert _same(tmp_path / name, DOCS / "snippets" / name), f"{source.name} {REGENERATE}"


def _numbers(name: str) -> list[float]:
    text = (DOCS / "snippets" / name).read_text(encoding="utf-8")
    return [float(x) for x in re.findall(r"-?\d+\.\d+", text)]


def test_what_the_prose_says_about_generated_output_is_true():
    """Pages state no typed number; the sentences that compare a printed result with
    something are checked here, so a regeneration cannot make them false unnoticed."""
    # index.md: "The simulated interval brackets the exact value".
    simulated, half_width, exact = _numbers("landing-output.txt")
    assert abs(simulated - exact) <= half_width
    # learn/5: faster service lowers the mean wait and the paired test calls it significant.
    table = (DOCS / "snippets" / "learn-5-output.txt").read_text(encoding="utf-8").splitlines()
    baseline, faster = table[1].split(), table[2].split()
    assert float(faster[-3]) < float(baseline[-3]) and float(faster[-2]) < 0
    assert faster[-1] == "True"
    # coming-from/mesa.md: both shares ever infected are close to the exact one.
    mesa = _numbers("from-mesa-mesa-output.txt")[0]
    naga, exact = _numbers("from-mesa-naga-output.txt")
    assert abs(mesa - exact) <= 0.05 and abs(naga - exact) <= 0.05


def test_the_landing_page_opens_with_install_example_and_output():
    text = (DOCS / "index.md").read_text(encoding="utf-8")
    first_card = text.index('class="grid cards"')
    at = [text.index(marker) for marker in ("pip install nagasim",
                                            '--8<-- "docs/snippets/landing-example.py"',
                                            '--8<-- "docs/snippets/landing-output.txt"')]
    assert at == sorted(at) and at[-1] < first_card
    assert (DOCS / "snippets" / "landing-output.txt").read_text(encoding="utf-8").strip()


def test_the_learning_path_builds_one_model_step_by_step():
    """Six pages in nav order, each showing its script and its output; the script of step n
    keeps the model lines of step n-1 (at most two lines change), so one model grows."""
    _config, nav = gen.site_nav()
    steps = [path for _section, _title, path in nav
             if path.startswith("learn/") and path != "learn/index.md"]
    assert [Path(p).name[0] for p in steps] == list("123456")
    previous = None
    for number, page in enumerate(steps, 1):
        text = (DOCS / page).read_text(encoding="utf-8")
        assert text.index(f'--8<-- "docs/snippets/learn-{number}.py"') < text.index(
            f'--8<-- "docs/snippets/learn-{number}-output.txt"')
        script = (DOCS / "snippets" / f"learn-{number}.py").read_text(encoding="utf-8").splitlines()
        # The model is everything before the first line that runs it.
        model = [line for line in script[:next(i for i, line in enumerate(script)
                                              if line.startswith(("results", "experiment")))] if line.strip()]
        if previous:
            assert len([line for line in previous if line not in model]) <= 2, f"step {number}"
        previous = model
        assert (DOCS / "snippets" / f"learn-{number}-output.txt").read_text(encoding="utf-8").strip()


def test_nav_is_tutorials_howto_reference_background_validation():
    config, _nav = gen.site_nav()
    assert [next(iter(item)) if isinstance(item, dict) else item for item in config["nav"]] == [
        "Home", "Learn", "How-to", "Reference", "Background", "Validation", "Release notes"]


def test_old_reference_models_url_redirects():
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("tag:yaml.org,2002:python/", lambda loader, suffix, node: None)
    config = yaml.load((gen.SITE / "mkdocs.yml").read_text(encoding="utf-8"), Loader=Loader)
    maps = {}
    for plugin in config["plugins"]:
        if isinstance(plugin, dict) and "redirects" in plugin:
            maps = plugin["redirects"]["redirect_maps"]
    assert maps["reference-models.md"] == "validation/reference-models.md"
    assert (DOCS / maps["reference-models.md"]).is_file()


def test_citing_page_shows_the_citation_file():
    text = (DOCS / "validation" / "citing.md").read_text(encoding="utf-8")
    assert '--8<-- "CITATION.cff"' in text
    assert (gen.ROOT / "packages" / "engine" / "CITATION.cff").is_file() or (gen.ROOT / "CITATION.cff").is_file()


# -- the build's AI-reader files: Markdown copies and llms-full.txt -------------

# Written in pieces: the export scans its own sources for these names.
SITE_LEAK = re.compile("|".join(["Matteo" "Brunetti", "Arena" "Py", "Strata" "gem"]), re.I)


@pytest.fixture(scope="module")
def built_site(tmp_path_factory):
    """The site built (strict) from a fresh export when this repository can make one, from
    the engine repository itself otherwise (that is what the export is)."""
    pytest.importorskip("mkdocs")
    pytest.importorskip("material")
    root = tmp_path_factory.mktemp("site")
    if (ROOT / "tools" / "export_engine.py").is_file():
        export_engine = importlib.import_module("tools.export_engine")
        assert export_engine.export(root / "naga") == []
        source = root / "naga"
    else:
        source = ROOT
    done = subprocess.run([sys.executable, "-m", "mkdocs", "build", "--strict", "-d", str(root / "out")],
                          cwd=str(source), capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    return root / "out", root


@pytest.mark.slow
@pytest.mark.docs
def test_the_built_site_has_markdown_copies_and_llms_full(built_site):
    site, root = built_site
    _config, nav = gen.site_nav()
    pages = sorted(site.rglob("*.html"))
    pages = [p for p in pages if p.name != "404.html" and "assets" not in p.parts
             and "http-equiv=\"refresh\"" not in p.read_text(encoding="utf-8")]  # not a redirect stub
    assert len(pages) >= len(nav)
    for page in pages:
        copy = page.with_suffix(".md")
        assert copy.is_file(), f"no Markdown copy of {page.relative_to(site)}"
        assert f'rel="alternate" type="text/markdown" href="{copy.name}"' in page.read_text(encoding="utf-8")
        assert "--8<--" not in copy.read_text(encoding="utf-8"), f"{copy.name}: an include was not expanded"
    full = (site / "llms-full.txt").read_text(encoding="utf-8")
    for _section, title, path in nav:
        heading = title or (DOCS / path).read_text(encoding="utf-8").split(chr(10), 1)[0]
        assert heading.lstrip("# ") in full or path[:-3] + ".html" in full, heading
    for name in [*(p.with_suffix(".md") for p in pages), site / "llms-full.txt"]:
        text = name.read_text(encoding="utf-8")
        assert not SITE_LEAK.search(text) and str(root) not in text, name.name
        assert "\n::: " not in text, f"{name.name}: an API directive was not expanded"
    # The API pages hold the signature and the docstring, and llms-full.txt is in menu order.
    run_model = (site / "reference" / "api" / "run_model.md").read_text(encoding="utf-8")
    assert "run_model(" in run_model and "Parameters" in run_model
    # A class page lists its methods: the Markdown copy holds each one's signature and docstring.
    model = (site / "reference" / "api" / "Model.md").read_text(encoding="utf-8")
    assert "### `nagasim.build.Model.run`" in model and "to_dict(self)" not in model.split("Model.to_dict")[0]
    assert "### `nagasim.build.Model.link`" in model and "add_resource" in model
    results = (site / "reference" / "api" / "Results.md").read_text(encoding="utf-8")
    assert "### `nagasim.results.Results.kpi`" in results and "kpi(key: str)" in results
    urls = re.findall(r"^URL: (\S+)$", full, re.M)
    assert len(urls) == len(nav)
    assert all(url.endswith("/" + path[:-3] + ".html") for url, (_s, _t, path) in zip(urls, nav))


def test_coming_from_pages_run_both_sides():
    """Each page shows the other library and Naga on the same model, both executed: a fence
    importing the library, a fence importing nagasim, and nothing excused from running."""
    for name, library in (("simpy", "simpy"), ("salabim", "salabim"), ("mesa", "mesa")):
        text = (DOCS / "coming-from" / f"{name}.md").read_text(encoding="utf-8")
        bodies = [_expanded(body) for _l, _t, body, excused, _n in fences(text) if not excused]
        assert any(f"import {library}" in body for body in bodies), name
        assert any("nagasim" in body for body in bodies), name
        assert not [n for language, _t, _b, excused, n in fences(text) if excused and language == "python"], name


# -- hand-written guidance fragments (docs/elements/, site_docs/elements/) ----

ELEMENTS = DOCS / "elements"
SD_AND_AGENT_GROUPS = ("system-dynamics", "agents", "agent-space")


def test_every_block_has_a_guidance_fragment():
    """Typical uses and behaviour for each of the registry's block types, read from the code."""
    for block_type in gen.BLOCK_REGISTRY:
        text = (ELEMENTS / "blocks" / f"{block_type}.md").read_text(encoding="utf-8")
        assert len(text) > 400 and "## Behaviour and rules" in text, block_type
    assert {p.stem for p in (ELEMENTS / "blocks").glob("*.md")} == set(gen.BLOCK_REGISTRY), "a fragment for no block"


def test_every_section_belongs_to_a_family_with_a_fragment():
    from nagasim.core import sections

    grouped = [name for names in gen.SECTION_FAMILIES.values() for name in names]
    assert sorted(grouped) == sorted(s.name for s in sections.SECTIONS), "each section is in exactly one family"
    assert {p.stem for p in (ELEMENTS / "sections").glob("*.md")} == set(gen.SECTION_FAMILIES)
    for family in gen.SECTION_FAMILIES:
        text = (ELEMENTS / "sections" / f"{family}.md").read_text(encoding="utf-8")
        assert len(text) > 400 and "## Behaviour and rules" in text, family


def test_function_groups_and_the_index_have_guidance():
    for page in (*SD_AND_AGENT_GROUPS, "index"):
        assert len((ELEMENTS / "functions" / f"{page}.md").read_text(encoding="utf-8")) > 300, page
    assert "case sensitive" in (ELEMENTS / "functions" / "index.md").read_text(encoding="utf-8")
    assert {g[0] for g in gen.FUNCTION_GROUPS} >= set(SD_AND_AGENT_GROUPS)


def test_forty_problem_codes_say_how_to_fix_them():
    from nagasim.core.problems import PROBLEM_CODES

    fixes = gen.problem_fixes()
    assert len(fixes) >= 40, len(fixes)
    assert set(fixes) <= set(PROBLEM_CODES), sorted(set(fixes) - set(PROBLEM_CODES))
    assert all(len(text) > 30 for text in fixes.values())
    page = (DOCS / "reference" / "problems" / "blocks-and-flow.md").read_text(encoding="utf-8")
    assert "## How to fix" in page and "`E_NO_CREATE`:" in page


# -- housekeeping: no plan codes, no hand-written count that can drift --------

PLAN_CODE = gen.PLAN_CODE
TENS = {"ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90, "hundred": 100}


def _without_comments(text: str) -> str:
    """*text* with HTML comments blanked, keeping the line count: an ID kept for the
    maintainers lives in a comment (`<!-- D123 -->`), which a reader never sees."""
    return re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)


def test_no_internal_plan_codes_in_pages():
    """The pages are for readers of the engine and of the product: a code from the project's
    own plans ("6D1 U3", "D345", a DEVLOG number) means nothing to them. Both sites are scanned."""
    product = sorted((ROOT / "site_docs").rglob("*.md"))
    found = [f"{page.relative_to(ROOT)}:{number}: {line.strip()[:70]}"
             for page in [*RUN, *product]
             for number, line in enumerate(
                 _without_comments(page.read_text(encoding="utf-8")).splitlines(), 1)
             if PLAN_CODE.search(line)]
    assert not found, chr(10).join(found)


def test_hand_written_counts_match():
    """A count typed into prose ("71 tools", "about sixty models") is the generator's value, or the
    page does not state one: the numbers change with every release."""
    tools = len(gen.engine_tools())
    models = sum(len(group) for group in CATALOGUE.values())
    wrong = []
    for page in RUN:
        text = page.read_text(encoding="utf-8")
        wrong += [f"{page.name}: {n} tools, the engine has {tools}"
                  for n in re.findall(r"\b(\d+) (?:agent )?tools\b", text) if int(n) != tools]
        wrong += [f"{page.name}: {m.group(0)}, the package has {models}"
                  for m in re.finditer(r"\babout (\w+) (?:bundled )?models\b", text)
                  if m.group(1) in TENS and abs(TENS[m.group(1)] - models) >= 5]
    assert not wrong, chr(10).join(wrong)


# -- the README and pages say what the code does ------------------------------

README = gen.SITE / "README.md"


def test_readme_says_what_writes():
    """The twin reads and proposes; `naga plc-write` is the one writer. While that command exists,
    no page says the engine never writes, and the README names the command and the Modbus reader."""
    from nagasim.cli import COMMANDS

    assert "plc-write" in COMMANDS
    for page in (README, DOCS / "index.md"):
        text = page.read_text(encoding="utf-8")
        assert "never writes to one" not in text, page.name
    text = README.read_text(encoding="utf-8")
    assert "naga plc-write" in text and "Modbus" in text


def test_site_links_match_the_url_style():
    """mkdocs builds with `use_directory_urls: false`: a page is `name.html`, the index is `/`."""
    assert "use_directory_urls: false" in (gen.SITE / "mkdocs.yml").read_text(encoding="utf-8")
    bad = [link for link in re.findall(r"thematthub\.github\.io/naga/([^\s)>\"]*)",
                                       README.read_text(encoding="utf-8"))
           if link and not link.split("#")[0].endswith((".html", ".json", ".txt"))]
    assert not bad, bad


def test_simpy_page_knows_pace_and_sessions():
    import inspect

    from nagasim import api

    simpy_page = (DOCS / "coming-from" / "simpy.md").read_text(encoding="utf-8")
    if "pace" in inspect.signature(api.run_model).parameters:
        assert "pace=" in simpy_page
    assert "SimSession" in simpy_page
    python_api = (DOCS / "python-api.md").read_text(encoding="utf-8")
    assert "SimSession" in python_api and "SimEnv" in python_api


def test_state_limits_are_generated():
    """The digital-twin page's list of what a twin cannot start from is rendered from state.py's
    `_unsupported(...)` kinds, so it cannot drift from what the code refuses."""
    kinds = gen.state_limit_kinds()
    assert "a worker fleet" in kinds and "a swap station" in kinds and len(kinds) >= 10
    text = gen.LIMITS_PAGE.read_text(encoding="utf-8")
    start, end = gen.STATE_LIMITS_BEGIN, gen.STATE_LIMITS_END
    assert start in text and end in text
    assert text.split(start)[1].split(end)[0].strip() == gen.state_limits_text().strip(), (
        f"digital-twin limits {REGENERATE}")


def test_plugin_example_is_exported():
    # The exporter exists only in the repository the engine is developed in; the exported tests skip.
    export_engine = pytest.importorskip("tools.export_engine")

    assert "examples/plugins/inspection_plugin/README.md" in export_engine.EXTRA_FILES
    assert (ROOT / "examples" / "plugins" / "inspection_plugin" / "README.md").is_file()


# -- llms.txt (D346): a truthful map of the site ------------------------------

def test_llms_txt_is_the_generators_and_lists_every_page(tmp_path):
    gen.gen_llms(tmp_path)
    assert _same(tmp_path / "llms.txt", DOCS / "llms.txt"), f"llms.txt {REGENERATE}"
    config, nav = gen.site_nav()
    in_nav = [path for _section, _title, path in nav]
    on_disk = sorted(p.relative_to(DOCS).as_posix() for p in PAGES)
    assert sorted(in_nav) == on_disk, "every page is in mkdocs.yml's nav, once, and nothing else is"
    linked = re.findall(r"\]\((\S+?)\)", (DOCS / "llms.txt").read_text(encoding="utf-8"))
    base = config["site_url"].rstrip("/") + "/"
    assert [link.removeprefix(base).removesuffix(".html") + ".md" for link in linked] == in_nav


def test_391_the_readme_reads_a_result_frame_that_exists():
    """DEVLOG #391: the README's Python snippet printed `results["system_summary"]`, a key
    no run returns, so the first thing a reader copied raised KeyError."""
    from nagasim.api import run_model

    readme = (gen.SITE / "README.md").read_text(encoding="utf-8")
    results = run_model(str(MODELS / "single_server.json"), replications=1, seed=0, run_length=50)
    named = re.findall(r'results\["(\w+)"\]', readme)
    assert named and all(key in results for key in named), named


# -- the API reference is the docstrings (mkdocstrings) ------------------------

def _api_object(dotted):
    module, *names = dotted.split(".")
    obj = importlib.import_module(module)
    for name in names:
        try:
            obj = getattr(obj, name)
        except AttributeError:      # a submodule not imported by its parent yet
            obj = importlib.import_module(f"{obj.__name__}.{name}")
    return obj


def _public_methods(cls):
    """The public callables a class defines or inherits from classes other than the built-ins,
    read from the class dicts (not `dir`), so a method made at run time is not among them."""
    return [n for base in cls.__mro__ if base.__module__ != "builtins"
            for n, v in vars(base).items() if not n.startswith("_") and callable(v)]


# Every entry point, and every public method of the classes among them.
API_DOCUMENTED = [*gen.API, *(f"{d}.{m}" for d in gen.API_MEMBERS
                              for m in _public_methods(_api_object(d)))]


def test_the_class_pages_list_every_public_method():
    for dotted, members in gen.API_MEMBERS.items():
        assert sorted(members) == sorted(_public_methods(_api_object(dotted))), dotted


@pytest.mark.parametrize("dotted", API_DOCUMENTED)
def test_every_api_entry_point_has_a_numpydoc_docstring_with_example(dotted):
    import inspect

    doc = inspect.getdoc(_api_object(dotted)) or ""
    obj = _api_object(dotted)
    wanted = ["Examples", "See Also"]
    if inspect.isclass(obj):        # a class that builds itself from nothing (Results) has none
        if "__init__" in vars(obj):
            wanted.append("Parameters")
    elif [p for p in inspect.signature(obj).parameters if p != "self"]:
        wanted.append("Parameters")
    if not inspect.isclass(obj):
        wanted.append("Returns")
    missing = [s for s in wanted if not re.search(rf"^{s}\n-{{{len(s)}}}$", doc, re.M)]
    assert not missing, f"{dotted}: no {missing} section"


@pytest.mark.parametrize("dotted", API_DOCUMENTED)
def test_the_api_docstring_examples_run(dotted):
    import doctest

    if dotted.endswith(".plot"):    # matplotlib is an optional extra (nagasim[plot])
        pytest.importorskip("matplotlib")
    flags = doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE
    tests = [t for t in doctest.DocTestFinder(recurse=False).find(_api_object(dotted), dotted, globs={})
             if t.examples]
    assert tests, f"{dotted}: no >>> example"
    runner = doctest.DocTestRunner(optionflags=flags)
    for test in tests:
        runner.run(test)
    assert runner.summarize(verbose=False).failed == 0, f"{dotted}: an example no longer prints what its docstring says"


def test_every_list_builder_is_on_the_model_page_and_works(tmp_path):
    """The `add_*` methods exist only at run time, so the page is generated from their registry
    and this runs the page's own example for each."""
    from nagasim import build as nb

    gen.gen_api(tmp_path)
    page = (tmp_path / "api" / "Model.md").read_text(encoding="utf-8")
    for method, section in nb._RECORDS.items():
        assert f"| `{method}` | `{section.name}` | `{section.key}` |" in page
        m = nb.Model()
        assert getattr(m, method)(**{section.key: "x"}) == "x", method


def test_the_api_pages_are_one_directive_each_and_in_the_nav(tmp_path):
    gen.gen_api(tmp_path)
    pages = {p.stem: p.read_text(encoding="utf-8") for p in (tmp_path / "api").glob("*.md")}
    assert set(pages) == {d.rsplit(".", 1)[1] for d in gen.API} | {"index"}
    for dotted in gen.API:
        assert f"::: {dotted}\n" in pages[dotted.rsplit(".", 1)[1]]
    _config, nav = gen.site_nav()
    in_nav = {path for _section, _title, path in nav}
    assert {f"reference/api/{name}.md" for name in pages} <= in_nav


def test_old_api_url_redirects():
    config, _nav = gen.site_nav()
    redirects = next(p["redirects"] for p in config["plugins"] if isinstance(p, dict) and "redirects" in p)
    assert redirects["redirect_maps"]["reference/api.md"] == "reference/api/index.md"
    assert not (DOCS / "reference" / "api.md").exists()


# -- one page for each block, distribution and group of functions --------------

def test_every_block_distribution_and_function_has_its_page():
    reference = DOCS / "reference"
    for folder, names in (("blocks", gen.BLOCK_REGISTRY), ("distributions", gen.DISTRIBUTION_REGISTRY)):
        pages = {p.stem for p in (reference / folder).glob("*.md")} - {"index"}
        assert pages == {gen._page_name(n) for n in names}, f"{folder}: a page for each entry and no other"
        index = (reference / folder / "index.md").read_text(encoding="utf-8")
        assert all(f"]({gen._page_name(n)}.md)" in index for n in names), f"{folder}/index.md lists every entry"
    charts = {p.stem for p in (reference / "distributions").glob("*.svg")}
    assert charts == {gen._page_name(n) for n in gen.DISTRIBUTION_REGISTRY if n != "FixedSchedule"}
    groups = [p for p in (reference / "functions").glob("*.md") if p.stem != "index"]
    assert {p.stem for p in groups} == {page for page, *_rest in gen.FUNCTION_GROUPS}
    text = "\n".join(p.read_text(encoding="utf-8") for p in groups)
    assert set(re.findall(r"^## (\w+)$", text, re.M)) == set(gen.FUNCTIONS), "a section for each function"


def test_a_block_page_shows_its_example_and_the_output_it_gave():
    for page in (DOCS / "reference" / "blocks").glob("*.md"):
        if page.stem == "index":
            continue
        found = {language: body for language, _title, body, _excused, _n
                 in fences(page.read_text(encoding="utf-8"))}
        assert "from nagasim import build" in found["python"], page.name
        assert found["text"].strip(), f"{page.name}: the example printed nothing"


def test_old_element_urls_redirect():
    config, _nav = gen.site_nav()
    redirects = next(p["redirects"] for p in config["plugins"] if isinstance(p, dict) and "redirects" in p)
    for old, new in (("blocks", "blocks"), ("distributions", "distributions"), ("expressions", "functions")):
        assert redirects["redirect_maps"][f"reference/{old}.md"] == f"reference/{new}/index.md"
        assert (DOCS / "reference" / new / "index.md").is_file()
        assert not (DOCS / "reference" / f"{old}.md").exists()


# -- one page for each section of the model file, and the problem codes ------------

def test_every_section_has_its_page_and_its_example():
    from nagasim.core import sections

    reference = DOCS / "reference" / "model-file"
    pages = {p.stem for p in reference.glob("*.md")} - {"index"}
    assert pages == {s.name for s in sections.SECTIONS}, "a page for each section and no other"
    index = (reference / "index.md").read_text(encoding="utf-8")
    assert all(f"]({s.name}.md)" in index for s in sections.SECTIONS), "index.md lists every section"
    assert set(gen.SECTION_EXAMPLES) == {s.name for s in sections.SECTIONS}, "an example for each section"


@pytest.mark.parametrize("name", [s.name for s in gen.sections.SECTIONS])
def test_a_section_example_validates_and_its_builder_call_runs(name):
    """The fragment, with the sections it names, passes the JSON Schema and the model checks;
    the builder line on the page runs."""
    import copy

    jsonschema = pytest.importorskip("jsonschema")
    from nagasim import build as nb
    from nagasim.core import schema as schema_mod
    from nagasim.modelschema import build_json_schema

    fragment, needs = gen.section_example(name)
    layout = schema_mod.migrate(copy.deepcopy({**gen.SECTION_BASE, **needs, name: fragment}))
    errors = [e.message for e in jsonschema.Draft202012Validator(build_json_schema()).iter_errors(layout)]
    assert not errors, errors[:3]
    fatal = [(p.code, p.message) for p in schema_mod.validate_model_structured(layout) if p.severity == "error"]
    assert not fatal, fatal[:3]
    code = gen.builder_code(next(s for s in gen.sections.SECTIONS if s.name == name), fragment)
    if code is None:
        return      # a sentence names the call: `a >> b`, `m.definition(name)`
    namespace = {}
    exec(code, namespace)    # noqa: S102 - the page's own snippet, made by the generator
    assert namespace["m"].to_dict().get(name), f"{name}: the builder call wrote nothing"


def test_a_section_page_lists_the_schemas_fields():
    from nagasim.modelschema import build_json_schema

    schema = build_json_schema()
    text = (DOCS / "reference" / "model-file" / "resources.md").read_text(encoding="utf-8")
    fields = schema["$defs"]["resource"]["properties"]
    assert all(f"| `{field}` |" in text for field in fields)
    assert not [f for f, spec in fields.items() if not spec.get("description")], "every field is described"


def test_every_problem_code_is_listed_once_in_an_area_page():
    from nagasim.core.problems import PROBLEM_CODES

    folder = DOCS / "reference" / "problems"
    listed = []
    for stem, *_rest in gen.PROBLEM_AREAS:
        # The table rows only: the "How to fix" list names a code again.
        listed += re.findall(r"^\| <span id=\"\w+\"></span>`([EW]_[A-Z0-9_]+)`", (folder / f"{stem}.md").read_text(encoding="utf-8"), re.M)
    assert sorted(listed) == sorted(PROBLEM_CODES), "every code on exactly one page"
    assert {p.stem for p in folder.glob("*.md")} == {"index", *(stem for stem, *_rest in gen.PROBLEM_AREAS)}
    for code in PROBLEM_CODES:
        assert gen.problem_area(code) in {stem for stem, *_rest in gen.PROBLEM_AREAS}


def test_generated_tables_write_no_html_tags():
    """A `<name>` in a table cell would read as a tag and vanish from the page."""
    tag = re.compile(r"<(?!/?span\b|!--)[A-Za-z]")
    for folder in ("problems", "model-file"):
        for page in (DOCS / "reference" / folder).glob("*.md"):
            outside = "".join(part for i, part in enumerate(page.read_text(encoding="utf-8").split("`")) if i % 2 == 0)
            assert not tag.search(outside), f"{page.name}: an unescaped <tag> outside code"


def test_stability_says_the_problem_list_may_change_before_one_point_zero():
    text = (DOCS / "stability.md").read_text(encoding="utf-8")
    assert "problem codes and their wording may change before 1.0" in text
