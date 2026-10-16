"""The engine's own documentation site (`docs/`, `mkdocs.yml`): its generated pages are
the generator's, and nothing on it has rotted. `tools/gen_engine_docs.py` writes the
generated pages; this file regenerates them and compares."""
import importlib
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


@pytest.mark.parametrize("generate", gen.REFERENCE, ids=lambda g: g.__name__)
def test_a_reference_page_is_the_generators(generate, tmp_path):
    if generate is gen.gen_cli and sys.version_info[:2] != DOCS_PYTHON:
        # The page is argparse's own `--help` text, and argparse words an option
        # differently from Python 3.13 on ("-o, --out FILE"). It is compared on the
        # version the site is built with; the commands it lists are checked on all.
        generate(tmp_path)
        page = (tmp_path / "cli.md").read_text(encoding="utf-8")
        committed = (DOCS / "reference" / "cli.md").read_text(encoding="utf-8")
        heads = lambda text: [line for line in text.splitlines() if line.startswith("## ")]
        assert heads(page) == heads(committed), f"['cli.md'] {REGENERATE}"
        return
    generate(tmp_path)
    stale = [p.name for p in tmp_path.iterdir() if not _same(p, DOCS / "reference" / p.name)]
    assert not stale, f"{stale} {REGENERATE}"


def test_the_reference_folder_holds_only_generated_pages(tmp_path):
    for generate in gen.REFERENCE:
        if generate is not gen.gen_kpis:        # the one that runs a model; its name is known
            generate(tmp_path)
    assert ({p.name for p in (DOCS / "reference").glob("*.md")}
            == {p.name for p in tmp_path.iterdir()} | {"kpis.md"})


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
PAGES = sorted(DOCS.rglob("*.md"))
RUN = [*PAGES, gen.SITE / "README.md"]
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


def _runnable(page: Path) -> bool:
    return any(language in ("console", "python") and not excused
               for language, _t, _b, excused, _n in fences(page.read_text(encoding="utf-8")))


@pytest.mark.parametrize("page", [p for p in RUN if _runnable(p)],
                         ids=lambda p: p.relative_to(gen.SITE).as_posix())
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
            exec(compile(body, where, "exec"), namespace)
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
    for page in RUN:
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
