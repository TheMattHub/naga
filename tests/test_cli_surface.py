"""The first minutes at the command line (review S1-05, S1-10): version, near-miss names, a short scenarios table."""
from __future__ import annotations

import pytest

import nagasim
from nagasim import cli


def run(capsys, *argv):
    code = cli.main([*argv])
    return code, capsys.readouterr()


def test_version_and_did_you_mean(capsys):
    code, out = run(capsys, "--version")
    assert code == 0 and nagasim.__version__ in out.out
    code, out = run(capsys, "rnu", "single_server")
    assert code == 2 and "'rnu'" in out.err and "'run'" in out.err
    code, out = run(capsys, "run", "single_servr")
    assert code == 2 and "single_server" in out.err


def test_scenarios_default_fits_100_columns(capsys):
    argv = ["automation_cell", "--replications", "3", "--run-length", "2000"]
    code, out = run(capsys, "scenarios", *argv)
    lines = out.out.splitlines()
    assert code == 0 and len(lines) <= 40 and max(map(len, lines)) <= 100
    assert lines[0].split() == ["KPI", "Scenario", "Mean", "Diff", "+/-", "Significant"]
    assert not any(line.lstrip().startswith("cost/") for line in lines)
    code, out = run(capsys, "scenarios", *argv, "--kpi", "cost/Robot cell/Amount")
    assert [line.split()[0] for line in out.out.splitlines()[1:]] == ["cost/Robot"] * 2
    code, out = run(capsys, "scenarios", *argv, "--all")
    assert len(out.out.splitlines()) > 100
