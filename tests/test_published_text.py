"""Text a user or an agent reads carries no id of the development plan, and a bundled
model's own text names no feature of an editor.

The rule: a registry description, a help string, a tool description, a problem's text, a
bundled model's `info` -- anything a generator publishes or a command prints -- says why in
words. An ordinary code comment may keep its decision id."""
import argparse
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODELS = ROOT / "nagasim" / "models"

# The development plan's own references: phases, roadmap items, decisions, the bug register,
# the architecture document, work packages and "depth buckets".
PLAN_TAG = re.compile(r"PHASE ?\d|ROADMAP|ARCHITECTURE|DEVLOG|\bD\d{2,3}\b|\bW\d+ ?[MQ]\d|depth bucket"
                      r"|\bBio-\d|\(#\d+[,)]|ARCH-REVIEW|\b6D\d?\b")
# What only an editor has. "dock" is not here: a warehouse has docks.
EDITOR_WORD = re.compile(r"Studies|3D view|dashboard|slider|toolbar|canvas|click|open it|palette"
                         r"|\beditor\b|Model >", re.I)


def test_no_bundled_models_own_text_names_a_plan_tag_or_an_editor_feature():
    found = []
    for path in sorted(MODELS.glob("*.json")):
        info = json.loads(path.read_text(encoding="utf-8")).get("info") or {}
        for key in ("title", "description", "notes", "category"):
            for match in list(PLAN_TAG.finditer(str(info.get(key, "")))) \
                    + list(EDITOR_WORD.finditer(str(info.get(key, "")))):
                found.append(f"{path.name} info.{key}: '{match.group(0)}'")
    assert not found, "\n".join(found)


def test_every_bundled_model_has_a_title_and_a_category():
    bare = [path.name for path in sorted(MODELS.glob("*.json"))
            if not all((json.loads(path.read_text(encoding="utf-8")).get("info") or {}).get(key)
                       for key in ("title", "category"))]
    assert not bare, bare


# -- every string the engine publishes ------------------------------------------

def _parsers(parser, prog):
    yield prog, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _parsers(sub, f"{prog} {name}")


def _help_texts(monkeypatch):
    """(command, its --help) for every command and sub-command, from the parsers themselves."""
    from nagasim import cli

    made = []

    def grab(self, args=None, namespace=None):
        made.append(self)
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
    for name in cli.COMMANDS:
        with pytest.raises(SystemExit):
            cli.main([name, "--help"])
    monkeypatch.undo()
    texts = {}
    for top in made:
        for prog, parser in _parsers(top, top.prog):
            texts[prog] = parser.format_help()
    return texts


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _strings(value)


def test_no_published_string_carries_a_plan_tag(monkeypatch):
    """What `naga <command> --help` prints, what `naga mcp` sends an agent, what the
    registries, the schema and the problem codes describe themselves with, and every page
    of the documentation site."""
    from nagasim import agent_tools, cli, modelschema
    from nagasim.core import problems, sections
    from nagasim.core.block_registry import BLOCK_REGISTRY
    from nagasim.core.distributions import DISTRIBUTION_REGISTRY
    gen = importlib.import_module("tools.gen_engine_docs")

    published = {}
    for name, spec in BLOCK_REGISTRY.items():
        published[f"block {name}"] = (spec.cls.__doc__ or "") + " ".join(p.description or "" for p in spec.params)
    for name, spec in DISTRIBUTION_REGISTRY.items():
        published[f"distribution {name}"] = spec.use_case + " ".join(p.hint or "" for p in spec.params)
    for section in sections.SECTIONS:
        published[f"section {section.name}"] = section.doc or ""
    for tool in agent_tools.TOOLS.values():
        if tool.origin.startswith("nagasim."):
            published[f"tool {tool.name}"] = tool.description + " ".join(_strings(tool.input_schema))
    for name, (_module, _attr, summary) in cli.COMMANDS.items():
        published[f"command {name}"] = summary
    for prog, text in _help_texts(monkeypatch).items():
        published[f"help of {prog}"] = text
    assert len([key for key in published if key.startswith("help of")]) > len(cli.COMMANDS)
    for code, text in problems.PROBLEM_CODES.items():
        published[f"problem {code}"] = str(text)
    published["naga schema"] = " ".join(_strings(modelschema.build_json_schema()))
    published["naga ci-template"] = (ROOT / "nagasim" / "templates" / "naga-models.yml").read_text(encoding="utf-8")
    for path in sorted(gen.DOCS.rglob("*.md")) + [gen.SITE / "README.md", gen.SITE / "CHANGELOG.md"]:
        published[path.relative_to(gen.SITE).as_posix()] = path.read_text(encoding="utf-8")

    found = [f"{where}: '{match.group(0)}' in ...{' '.join(text.split())[max(0, match.start() - 40):match.end() + 20]}"
             for where, text in published.items()
             for match in PLAN_TAG.finditer(" ".join(text.split()))]
    assert not found, "\n".join(found[:40])
