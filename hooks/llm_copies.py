"""mkdocs hook: the site as plain text for AI readers.

At build time it writes a Markdown copy of every page next to its HTML (`learn/index.md`
beside `learn/index.html`), links it from the page (`rel="alternate"`), and writes
`llms-full.txt`, the whole site as one file in menu order. Nothing is committed under
`docs/`: the copies are made from the sources, with the `--8<--` includes expanded by the
same preprocessor the build uses, so a copy shows what the page shows.
"""
import re
from pathlib import Path

import markdown
import yaml
from pymdownx.snippets import SnippetExtension

_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")
_pages: dict = {}        # src_uri -> (page, expanded Markdown), in the order pages are read
_nav = None
# An API page is one mkdocstrings directive (`::: dotted.name` and its indented options); its
# copy holds the signature and the docstring instead, read statically with griffe, the library
# mkdocstrings reads them with. A directive that lists `members` gets each one's, in order.
_DIRECTIVE = re.compile(r"^::: (\S+)\n(?:[ \t]+.*\n|\n)*", re.M)
_packages: dict = {}     # package name -> loaded griffe module


def _signature(obj) -> str:
    """`name(parameters)` for a function, or for a class its `__init__` without self."""
    func = obj.all_members.get("__init__") if obj.is_class else obj
    parts = []
    for p in getattr(func, "parameters", []):
        if p.name in ("self", "cls"):
            continue
        text = p.name + (f": {p.annotation}" if p.annotation is not None else "")
        parts.append(text + (f" = {p.default}" if p.default is not None else ""))
    return f"{obj.name}({', '.join(parts)})"


def _api_text(match, root: Path) -> str:
    import griffe
    dotted = match.group(1)
    package, path = dotted.split(".", 1)
    if package not in _packages:
        _packages[package] = griffe.load(package, search_paths=[str(root)])
    obj = _packages[package][path]
    text = _one(dotted, obj)
    options = yaml.safe_load(match.group(0).split("\n", 1)[1]) or {}
    for name in (options.get("options") or {}).get("members") or []:
        text += "\n" + _one(f"{dotted}.{name}", obj.all_members[name], "### ")
    return text


def _one(dotted: str, obj, heading: str = "") -> str:
    doc = obj.docstring.value if obj.docstring else ""
    return f"{heading}`{dotted}`\n\n```python\n{_signature(obj)}\n```\n\n{doc}\n"


def _expand(text: str, root: Path) -> str:
    """*text* with its snippet includes replaced by the files, API directives by their
    docstrings, and one-line HTML comments (the generated-page notices) dropped."""
    md = markdown.Markdown(extensions=[SnippetExtension(check_paths=True)])
    lines = md.preprocessors["snippet"].run(text.split(chr(10)))
    text = chr(10).join(line for line in lines if not _COMMENT.match(line))
    return _DIRECTIVE.sub(lambda m: _api_text(m, root), text).strip() + chr(10)


def on_pre_build(config):
    _pages.clear()


def on_nav(nav, config, files):
    global _nav
    _nav = nav
    return nav


def on_page_markdown(markdown_text, page, config, files):
    _pages[page.file.src_uri] = (page, _expand(markdown_text, Path(config.config_file_path).parent))
    return markdown_text


def on_post_page(output, page, config):
    href = Path(page.file.src_uri).name
    tag = f'<link rel="alternate" type="text/markdown" href="{href}">'
    return output.replace("</head>", tag + "</head>", 1)


def on_post_build(config):
    site = Path(config["site_dir"])
    for src_uri, (_page, text) in _pages.items():
        target = site / src_uri
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline=chr(10))
    base = config["site_url"].rstrip("/") + "/"
    parts = [f"# {config['site_name']}: full documentation", "",
             f"> {config['site_description']}. Every page of the site as one text file, in the "
             "order of its menu. The list of pages with a sentence each is llms.txt.", ""]
    for nav_page in _nav.pages:
        entry = _pages.get(nav_page.file.src_uri)
        if entry:
            parts += ["---", "", f"Page: {nav_page.title}", f"URL: {base}{nav_page.file.src_uri[:-3]}.html",
                      "", entry[1]]
    (site / "llms-full.txt").write_text(chr(10).join(parts), encoding="utf-8", newline=chr(10))
