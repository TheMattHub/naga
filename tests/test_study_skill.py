"""The simulation-study skill ships as package data and stays true to the tool registry."""
from __future__ import annotations

import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path

from nagasim import agent_tools, study

REPO = Path(__file__).resolve().parents[1]
FILES = ("SKILL.md", "references/interview.md", "references/method.md")
# Words in backticks that look like tool names but are study fields or verdicts, not tools.
NON_TOOLS = {"plan_approved", "not_verified", "known_answer", "one_go", "next_step"}


def test_skill_front_matter():
    text = study.skill_text()
    head = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    fields = dict(line.split(": ", 1) for line in head.splitlines())
    assert fields["name"] == study.skill_dir().name == "simulation-study"
    assert re.fullmatch(r"[a-z0-9-]{1,64}", fields["name"])
    assert 0 < len(fields["description"]) <= 1024


def test_skill_names_only_real_tools():
    """Rule: every backticked identifier shaped like `word_word` (lowercase, one or more
    underscores, optionally followed by "(") in the three files must be a registered
    agent tool or be listed in NON_TOOLS."""
    seen = set()
    for name in FILES:
        seen |= set(re.findall(r"`([a-z]+(?:_[a-z]+)+)\(?[^`]*`", study.skill_text(name)))
    unknown = {w for w in seen if w not in agent_tools.TOOLS and w not in NON_TOOLS}
    assert not unknown, f"not tools: {sorted(unknown)}"
    assert "study_status" in seen


def test_skill_is_package_data():
    # Both files in this repository; only the root one in the exported engine repository.
    found = [f for f in (REPO / "pyproject.toml", REPO / "packages" / "engine" / "pyproject.toml") if f.is_file()]
    assert found
    for pyproject in found:
        globs = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["setuptools"][
            "package-data"]["nagasim"]
        for name in FILES:
            rel = f"skills/simulation-study/{name}"
            assert any(fnmatch(rel, g) for g in globs), f"{pyproject} misses {rel}"


def test_skill_is_ascii_and_short():
    assert len(study.skill_text().splitlines()) < 500
    for name in FILES:
        study.skill_text(name).encode("ascii")


def test_skill_cli_prints_path(capsys):
    from nagasim import cli
    assert cli.main(["skill"]) == 0
    out = capsys.readouterr().out
    assert (Path(out.splitlines()[0]) / "SKILL.md").is_file() and "--copy" in out


def test_skill_cli_copies_and_refuses_overwrite(tmp_path, capsys):
    from nagasim import cli
    assert cli.main(["skill", "--copy", str(tmp_path)]) == 0
    assert (tmp_path / "simulation-study" / "references" / "method.md").is_file()
    (tmp_path / "simulation-study" / "SKILL.md").write_text("edited", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["skill", "--copy", str(tmp_path)]) == 1
    assert "--force" in capsys.readouterr().err
    assert (tmp_path / "simulation-study" / "SKILL.md").read_text(encoding="utf-8") == "edited"
    assert cli.main(["skill", "--copy", str(tmp_path), "--force"]) == 0
    assert (tmp_path / "simulation-study" / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_prompts_return_the_skill(tmp_path):
    import asyncio
    from nagasim import mcp_server
    server = mcp_server.build_server(tmp_path)
    run = str(asyncio.run(server.get_prompt("run_simulation_study", {"question": "how many docks?"})))
    assert "study_status" in run and "how many docks?" in run and "name: simulation-study" not in run
    interview = str(asyncio.run(server.get_prompt("study_interview", {})))
    assert study.skill_text("references/interview.md").splitlines()[0] in interview


def test_agents_md_names_the_guide(tmp_path):
    study.create(tmp_path / "s", "q")
    text = (tmp_path / "s" / "AGENTS.md").read_text(encoding="utf-8")
    assert "run_simulation_study" in text and "naga skill" in text
