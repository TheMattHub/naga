"""Plan L task 1 (D1520, D1521): run and load answers carry the log. Engine-only, no PySide6.

Public API assumed (written from the requirements, before the implementation):
    from nagasim.logcapture import capture
    with capture() as cap:
        ...
    cap.entries()    -> list of dicts {level, source, message, count}, at most 20
    cap.truncated    -> bool, True when more than 20 distinct records were folded away
`level` is "warning" or "error"; `source` is the logger name without the leading `nagasim.`;
repeats of one (level, source, message) fold into `count`; INFO and below are not captured;
`warnings.warn` inside the block is captured as a warning. Agent answers carry the same
entries as `log` (absent when empty) plus `log_truncated` when cut at 20.
"""
import copy
import json
import logging
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import chain, create, dispose, layout, process  # noqa: E402
from test_conveyor_checks import _stale_route  # noqa: E402

from nagasim import agent_tools as T
from nagasim import api
from nagasim.agent_tools import runs

NEGATIVE = "sampled a negative duration"      # nagasim/core/base_process.py, logger core.base_process


def _capture():
    from nagasim.logcapture import capture
    return capture()


def _log(name, level, message):
    logging.getLogger(f"nagasim.{name}").log(level, message)


# ------------------------------------------------------------------ the context manager

def test_repeats_fold_into_count_and_source_drops_the_package_prefix():
    with _capture() as cap:
        for _ in range(3):
            _log("zz_cap.alpha", logging.WARNING, "the same words")
        _log("zz_cap.alpha", logging.WARNING, "other words")
    found = {e["message"]: e for e in cap.entries()}
    assert set(found) == {"the same words", "other words"}      # two kinds, not four lines
    assert found["the same words"] == {"level": "warning", "source": "zz_cap.alpha",
                                       "message": "the same words", "count": 3}
    assert found["other words"]["count"] == 1
    assert cap.truncated is False


def test_warning_and_error_are_captured_info_and_debug_are_not():
    with _capture() as cap:
        _log("zz_cap.levels", logging.DEBUG, "d-text")
        _log("zz_cap.levels", logging.INFO, "i-text")
        _log("zz_cap.levels", logging.WARNING, "w-text")
        _log("zz_cap.levels", logging.ERROR, "e-text")
    assert {(e["level"], e["message"]) for e in cap.entries()} == {("warning", "w-text"),
                                                                    ("error", "e-text")}


def test_the_same_words_at_two_levels_are_two_entries():
    with _capture() as cap:
        _log("zz_cap.levels", logging.WARNING, "same")
        _log("zz_cap.levels", logging.ERROR, "same")
    assert sorted((e["level"], e["count"]) for e in cap.entries()) == [("error", 1), ("warning", 1)]


def test_python_warnings_inside_the_block_are_captured_as_warnings():
    with _capture() as cap:
        warnings.warn("zz_cap careful with this", UserWarning)
    found = [e for e in cap.entries() if "zz_cap careful with this" in e["message"]]
    assert len(found) == 1 and found[0]["level"] == "warning" and found[0]["count"] == 1


def test_more_than_twenty_kinds_keep_twenty_newest_and_say_truncated():
    with _capture() as cap:
        for i in range(25):
            _log("zz_cap.many", logging.WARNING, f"kind {i:02d}")
    messages = [e["message"] for e in cap.entries()]
    assert len(messages) == 20 and cap.truncated is True
    assert "kind 24" in messages and "kind 05" in messages     # newest kinds kept
    assert "kind 00" not in messages and "kind 04" not in messages


def test_exactly_twenty_kinds_are_not_truncated():
    with _capture() as cap:
        for i in range(20):
            _log("zz_cap.many", logging.WARNING, f"kind {i:02d}")
    assert len(cap.entries()) == 20 and cap.truncated is False


def test_nothing_logged_gives_an_empty_list_and_not_truncated():
    with _capture() as cap:
        pass
    assert cap.entries() == [] and cap.truncated is False


def test_records_outside_the_block_are_not_captured_and_the_handler_is_removed():
    before = list(logging.getLogger("nagasim").handlers) + list(logging.getLogger().handlers)
    with _capture() as cap:
        _log("zz_cap.in", logging.WARNING, "inside")
    _log("zz_cap.out", logging.WARNING, "outside")
    assert [e["message"] for e in cap.entries()] == ["inside"]
    assert list(logging.getLogger("nagasim").handlers) + list(logging.getLogger().handlers) == before


# ------------------------------------------------------------------ threads, nesting, restore

def _nagasim_handlers():
    return list(logging.getLogger("nagasim").handlers)


def test_a_record_or_warning_on_another_thread_is_not_captured():
    """A capture is per thread: the editor's GUI-thread records stay out of a worker tool's answer."""
    import threading
    with _capture() as cap:
        other = threading.Thread(target=lambda: (_log("zz_cap.thread", logging.WARNING, "other thread"),
                                                 warnings.warn("zz_cap other thread warns")))
        other.start()
        other.join(5)
        _log("zz_cap.thread", logging.WARNING, "this thread")
    assert [e["message"] for e in cap.entries()] == ["this thread"]


def test_nested_captures_both_get_the_inner_records_and_the_handler_goes_with_the_outer():
    before = _nagasim_handlers()
    with _capture() as outer:
        _log("zz_cap.nest", logging.WARNING, "outer only")
        with _capture() as inner:
            _log("zz_cap.nest", logging.WARNING, "both")
        assert len(_nagasim_handlers()) == len(before) + 1       # still there for the outer
        _log("zz_cap.nest", logging.WARNING, "outer again")
    assert [e["message"] for e in inner.entries()] == ["both"]
    assert [e["message"] for e in outer.entries()] == ["outer only", "both", "outer again"]
    assert _nagasim_handlers() == before


def test_an_exception_in_the_block_restores_the_handler_and_the_hook_and_nothing_leaks():
    before, hook = _nagasim_handlers(), warnings.showwarning
    with pytest.raises(RuntimeError, match="zz_cap boom"):
        with _capture():
            raise RuntimeError("zz_cap boom")
    assert _nagasim_handlers() == before and warnings.showwarning is hook
    for _ in range(200):
        with _capture():
            _log("zz_cap.many_calls", logging.WARNING, "again")
    from nagasim import logcapture
    assert _nagasim_handlers() == before and warnings.showwarning is hook
    assert logcapture._active == {}


def test_a_catch_warnings_closed_on_another_thread_never_makes_the_hook_call_itself():
    """Checkpoint C1: thread B saves the hook inside its `catch_warnings()` while A's capture is
    open and puts it back after A closed. A later capture must still pass warnings on (no
    RecursionError) and leave the hook as it was before all of this."""
    import threading
    hook = warnings.showwarning
    opened, entered, closed = threading.Event(), threading.Event(), threading.Event()

    def other():
        opened.wait(5)
        with warnings.catch_warnings():
            entered.set()
            closed.wait(5)

    b = threading.Thread(target=other)
    b.start()
    with _capture():
        opened.set()
        assert entered.wait(5)
    closed.set()
    b.join(5)
    assert not b.is_alive()
    with _capture() as cap:
        warnings.warn("zz_cap after the race", UserWarning)
    assert [e["message"] for e in cap.entries()] == ["zz_cap after the race"]
    assert warnings.showwarning is hook


def test_a_hook_that_wraps_the_capture_hook_does_not_recurse():
    """A hook installed while a capture is open that calls back the one it replaced, then saved by
    the next capture as the hook to pass on to: _show must not call back into it forever."""
    with _capture():
        inner = warnings.showwarning
        warnings.showwarning = lambda *a, **k: inner(*a, **k)
    try:
        with _capture() as cap:
            warnings.warn("zz_cap wrapped hook", UserWarning)
        assert [e["message"] for e in cap.entries()] == ["zz_cap wrapped hook"]
    finally:
        from nagasim import logcapture
        warnings.showwarning = logcapture._ORIGINAL


# ------------------------------------------------------------------ run_model, inline and pool

def _negative_model(time_limit=200):
    """A Normal service time that goes below 0: the engine logs the negative-duration warning."""
    blocks = [create(max_arrivals=0, dist_params={"value": 1.0}),
              process(distribution="Normal"), dispose()]
    blocks[1]["params"]["dist_params"] = {"mean": 0.5, "stddev": 0.5}
    return layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                  simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})


def _quiet_model():
    blocks = [create(max_arrivals=5, dist_params={"value": 1.0}), process(value=0.5), dispose()]
    return layout(blocks, chain(*blocks), resources=[{"name": "Server", "capacity": 1}],
                  simulation={"time_limit": 50, "warmup": 0, "replications": 1})


@pytest.fixture
def ws(tmp_path):
    return T.use_workspace(T.Workspace(tmp_path))


def _open(tmp_path, doc, name="m"):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    answer = T.call("open_model", path=str(path), name=name)
    assert "error" not in answer, answer
    return answer


def _negative_entries(answer):
    return [e for e in answer.get("log", []) if NEGATIVE in e["message"]]


def test_an_inline_run_answer_carries_the_run_time_warning_once(ws, tmp_path):
    _open(tmp_path, _negative_model())
    answer = T.call("run_model", model="m", replications=1)
    assert "error" not in answer, answer
    found = _negative_entries(answer)
    assert len(found) == 1, answer.get("log")
    assert found[0]["level"] == "warning" and found[0]["source"] == "core.base_process"
    assert found[0]["count"] == 1
    assert set(found[0]) == {"level", "source", "message", "count"}
    assert "log_truncated" not in answer


def test_an_inline_four_replication_answer_carries_the_warning_once(ws, tmp_path):
    """The run tools run inline (workers=1); the pool path is held at the engine below
    (`test_a_process_pool_run_captures_the_worker_records_once_as_inline`)."""
    _open(tmp_path, _negative_model())
    answer = T.call("run_model", model="m", replications=4)
    assert "error" not in answer, answer
    found = _negative_entries(answer)
    assert len(found) == 1, answer.get("log")
    assert found[0]["count"] >= 1
    assert found[0]["source"] == "core.base_process" and found[0]["level"] == "warning"


def test_a_refused_run_answer_carries_what_the_run_logged(ws, tmp_path, monkeypatch):
    """A run that logs and then fails is refused (E_RUN_FAILED); the refusal still carries `log`."""
    _open(tmp_path, _quiet_model())

    def failing(*args, **kwargs):
        _log("zz_cap.fail", logging.ERROR, "zz_cap the block broke")
        raise RuntimeError("zz_cap run failed")

    monkeypatch.setattr(api, "run_model", failing)
    monkeypatch.setattr(runs.api, "run_model", failing, raising=False)
    answer = T.call("run_model", model="m", replications=1)
    assert answer["error"]["code"] == "E_RUN_FAILED", answer
    assert answer["log"] == [{"level": "error", "source": "zz_cap.fail",
                              "message": "zz_cap the block broke", "count": 1}]


def test_a_run_with_nothing_logged_has_no_log_key(ws, tmp_path):
    _open(tmp_path, _quiet_model())
    answer = T.call("run_model", model="m", replications=1)
    assert "error" not in answer, answer
    assert "log" not in answer and "log_truncated" not in answer


def test_an_answer_carries_at_most_twenty_entries_and_log_truncated(ws, tmp_path, monkeypatch):
    """Twenty-five kinds logged during the run: the model's own run function is wrapped so each
    run logs them (the tool, not the test, owns the capture)."""
    _open(tmp_path, _quiet_model())
    original = api.run_model

    def noisy(*args, **kwargs):
        for i in range(25):
            _log("zz_cap.many", logging.WARNING, f"kind {i:02d}")
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "run_model", noisy)
    monkeypatch.setattr(runs.api, "run_model", noisy, raising=False)
    answer = T.call("run_model", model="m", replications=1)
    assert "error" not in answer, answer
    assert len(answer["log"]) == 20 and answer["log_truncated"] is True


def test_kpis_are_identical_with_and_without_capture():
    from nagasim.logcapture import capture
    doc = _negative_model(time_limit=100)
    plain = api.run_model(copy.deepcopy(doc), replications=2, seed=3)
    with capture():
        captured = api.run_model(copy.deepcopy(doc), replications=2, seed=3)
    import pandas as pd
    assert set(plain) == set(captured)
    for name in plain:
        pd.testing.assert_frame_equal(plain[name], captured[name])


# ------------------------------------------------------------------ run_scenarios

@pytest.fixture
def unchecked(monkeypatch):
    """As tests/test_run_problems.py: what the validator refuses is let through."""
    from nagasim.core import graph_model
    monkeypatch.setattr(graph_model, "fatal_problems", lambda structured: [])
    monkeypatch.setattr(runs, "_runnable_layout", lambda doc, model, then="": doc.to_layout())


def _with_scenario(doc):
    doc["scenarios"] = [{"name": "Other", "overrides": [{"path": "simulation.warmup", "value": 0}]}]
    return doc


def test_run_scenarios_returns_each_scenarios_problems(ws, tmp_path, unchecked):
    """D1521: `problems` is keyed by scenario name (as `laws` is), each a list of the run
    problems of that scenario, the same dicts run_model's `problems` holds."""
    doc = _stale_route(False)
    doc["simulation"].update(time_limit=50, replications=1)
    _open(tmp_path, _with_scenario(doc))
    answer = T.call("run_scenarios", model="m", replications=1)
    assert "error" not in answer, answer
    assert set(answer["problems"]) == {"Baseline", "Other"}
    for name, found in answer["problems"].items():
        assert [(p["code"], p["block"]) for p in found] == [("W_ROUTE_FORBIDDEN", "T")], name
        assert found[0]["hint"]


def test_run_scenarios_returns_the_log_and_no_log_when_quiet(ws, tmp_path):
    _open(tmp_path, _with_scenario(_negative_model()), name="noisy")
    answer = T.call("run_scenarios", model="noisy", replications=1)
    assert "error" not in answer, answer
    found = _negative_entries(answer)
    assert len(found) == 1 and found[0]["count"] >= 1, answer.get("log")
    _open(tmp_path, _with_scenario(_quiet_model()), name="quiet")
    quiet = T.call("run_scenarios", model="quiet", replications=1)
    assert "error" not in quiet, quiet
    assert "log" not in quiet and "log_truncated" not in quiet


def test_a_quiet_scenario_run_has_no_problems_entries_with_content(ws, tmp_path):
    """No digest grows when nothing happened (D1389): a quiet run has no non-empty problems."""
    _open(tmp_path, _with_scenario(_quiet_model()))
    answer = T.call("run_scenarios", model="m", replications=1)
    assert "error" not in answer, answer
    assert not any(answer.get("problems", {}).values())


# ------------------------------------------------------------------ open_model

def test_open_model_of_a_model_that_logs_a_load_warning_returns_log(ws, tmp_path, monkeypatch):
    """No model file in this engine makes `open_model` itself log (unknown keys come back as
    `problems`, plugin loading happens at first run), so the load is made to log: the
    document loader is wrapped to log one warning, as a plugin or repair warning would. The
    capture must span the whole tool call, whatever logs inside it."""
    from nagasim.core.document import ModelDocument
    original = ModelDocument.load.__func__

    def noisy(cls, path, **kwargs):
        logging.getLogger("nagasim.core.document").warning("zz_cap repaired something on load")
        return original(cls, path, **kwargs)

    monkeypatch.setattr(ModelDocument, "load", classmethod(noisy))
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_quiet_model()), encoding="utf-8")
    answer = T.call("open_model", path=str(path), name="m")
    assert "error" not in answer, answer
    assert answer["log"] == [{"level": "warning", "source": "core.document",
                              "message": "zz_cap repaired something on load", "count": 1}]


def test_open_model_of_a_quiet_model_has_no_log_key(ws, tmp_path):
    answer = _open(tmp_path, _quiet_model())
    assert "log" not in answer and "log_truncated" not in answer


@pytest.mark.slow
def test_a_process_pool_run_captures_the_worker_records_once_as_inline():
    """The tool runs inline (workers=1), so the pool path is held here, at the engine: records a
    worker logged come back to the parent and land in the capture as often as inline ones."""
    from nagasim.logcapture import capture
    found = {}
    for workers in (1, 2):
        with capture() as cap:
            api.run_model(copy.deepcopy(_negative_model()), replications=4, workers=workers)
        found[workers] = cap.entries()
    assert found[2] == found[1]
    assert [e["count"] for e in found[2] if NEGATIVE in e["message"]] == [1]
