"""The exporter contract (ROADMAP 2.4 C) and its `needs_trace` flag (PHASE7 7.7, D201):
an exporter drawn from the run's recorded trace says so, the command line records one
for it, and every other exporter is called exactly as before."""
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core.trace import TraceRecorder
from nagasim.exporters import EXPORTER_REGISTRY, Exporter

MODEL = str(Path(__file__).resolve().parents[1] / "nagasim" / "models" / "single_server.json")
RUN = ["--replications", "2", "--run-length", "50"]


@pytest.fixture
def seam(monkeypatch):
    """Two exporters registered for one test, and what each was called with. `tape` is
    drawn from the trace. `old` is a plugin written before the flag: its writer takes
    no keyword it does not know. `runs` is every `trace=` given to `api.run_model`."""
    seen = {"runs": []}

    def write_tape(results, path, *, layout=None, title=None, trace=None):
        seen["tape"] = trace
        if trace is None:
            raise ValueError("tape is drawn from a recorded trace")
        Path(path).write_text(str(len(trace.events)), encoding="utf-8")

    def write_old(results, path, *, layout=None, title=None):
        seen["old"] = title
        Path(path).write_text("old", encoding="utf-8")

    run_model = api.run_model

    def spy(*args, **kwargs):
        seen["runs"].append(kwargs.get("trace"))
        return run_model(*args, **kwargs)

    monkeypatch.setitem(EXPORTER_REGISTRY, "tape",
                        Exporter("tape", ".txt", write_tape, needs_trace=True))
    monkeypatch.setitem(EXPORTER_REGISTRY, "old", Exporter("old", ".txt", write_old))
    monkeypatch.setattr(api, "run_model", spy)
    return seen


def test_an_exporter_needs_no_trace_unless_it_says_so():
    assert Exporter("mine", ".txt", print).needs_trace is False     # a plugin's three fields
    assert not any(EXPORTER_REGISTRY[name].needs_trace for name in ("xlsx", "csv", "html", "json"))


def test_the_command_line_records_a_trace_only_for_an_exporter_that_needs_one(seam, tmp_path, capsys):
    old, tape = tmp_path / "old.txt", tmp_path / "tape.txt"
    assert api.main([MODEL, *RUN, "--export", f"old:{old}"]) == 0
    assert seam["runs"] == [None] and "tape" not in seam        # no recorder, no cost
    assert old.read_text(encoding="utf-8") == "old"

    assert api.main([MODEL, *RUN, "--export", f"tape:{tape}", "--export", f"old:{old}"]) == 0
    recorder = seam["runs"][-1]
    assert isinstance(recorder, TraceRecorder) and recorder.events
    assert seam["tape"] is recorder
    assert tape.read_text(encoding="utf-8") == str(len(recorder.events))
    assert seam["old"] == "single_server"       # called as before: a TypeError otherwise
    assert "tape written to" in capsys.readouterr().out


def test_the_recorded_trace_is_the_first_replication_whatever_the_workers(seam, tmp_path):
    """Review focus: with a process pool the recorder cannot come back from a child,
    so the first replication stays in this process; the file is the same either way."""
    one, two = tmp_path / "one.txt", tmp_path / "two.txt"
    assert api.main([MODEL, "--replications", "3", "--run-length", "50",
                     "--export", f"tape:{one}"]) == 0
    assert api.main([MODEL, "--replications", "3", "--run-length", "50", "--workers", "2",
                     "--export", f"tape:{two}"]) == 0
    assert one.read_text(encoding="utf-8") == two.read_text(encoding="utf-8") != "0"


def test_a_writer_that_needs_a_trace_says_so_without_one(seam, tmp_path):
    with pytest.raises(ValueError, match="drawn from a recorded trace"):
        api._run_export(f"tape:{tmp_path / 'none.txt'}", {}, MODEL, layout={})
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_format_is_named_and_records_nothing(seam, tmp_path, capsys):
    assert api.main([MODEL, *RUN, "--export", f"nope:{tmp_path / 'x'}"]) == 0
    assert seam["runs"] == [None]
    assert "'nope:" in capsys.readouterr().out and list(tmp_path.iterdir()) == []
