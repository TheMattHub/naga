"""H10 (performance report 2026-10-07): the per-entity `log.debug` calls are
skipped unless debug output was wanted when the run was set up
(`SimulationContext.debug`), and a run with DEBUG on logs what it always did."""
import hashlib
import logging
from pathlib import Path

import pytest

import nagasim
from nagasim.core.sim_runner import initialize_sim_env, run_single_simulation

MODELS = Path(nagasim.__file__).resolve().parent / "models"

# model -> (run length, DEBUG records from the blocks, seizing and the resource
# manager, SHA-256 of them sorted), captured on da5c5ab, before any call was
# guarded. Sorted: the order of two events inside one instant is not what this
# pins. Together the five models reach every guarded call but DUPLICATE's and
# MATCH's.
EXPECTED = {
    "warehouse_library": (60, 452, "0e0e70f0c5358787"),
    "hold_signal_flow": (200, 228, "f78ed4d3b11cbb5c"),
    "biogas_plant_library": (400, 1431, "f6dcaada3408fdd2"),
    "mes_twin_shop": (300, 78, "c8725c488d833801"),
    # Re-pinned when the instances were renamed (Phase 13 R): a block's random stream is seeded
    # from its name, so a renamed block draws a different, statistically equal, sample.
    "clinic_library": (200, 733, "2a27116fd37383ed"),
}


class _Debug(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []
        self.templates = set()

    def emit(self, record):
        name = record.name
        if record.levelno == logging.DEBUG and (name.startswith("nagasim.core.blocks.") or name in (
                "nagasim.core.seizing", "nagasim.core.resource_manager")):
            self.lines.append(f"{name}|{record.getMessage()}")
            self.templates.add(record.msg)


@pytest.fixture
def debug_run():
    """Run a bundled model with DEBUG switched on for *logger* first."""
    handler = _Debug()
    touched = []

    def run(model, run_len, logger="nagasim"):
        target = logging.getLogger(logger)
        touched.append((target, target.level))
        target.setLevel(logging.DEBUG)
        logging.getLogger("nagasim").addHandler(handler)
        run_single_simulation(str(MODELS / f"{model}.json"), seed=0, run_len=run_len, warmup=0)
        return handler

    yield run
    logging.getLogger("nagasim").removeHandler(handler)
    for target, level in touched:
        target.setLevel(level)


def digest(lines):
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:16]


@pytest.mark.parametrize("model", sorted(EXPECTED))
def test_debug_run_logs_the_same_messages_as_before(debug_run, model):
    run_len, count, sha = EXPECTED[model]
    lines = debug_run(model, run_len).lines
    assert (len(lines), digest(lines)) == (count, sha)


def test_debug_is_decided_from_the_loggers_when_the_run_is_set_up(debug_run):
    assert initialize_sim_env().ctx.debug is False
    # One module's logger is enough, and only that module then speaks.
    handler = debug_run("single_server", 50, logger="nagasim.core.seizing")
    assert handler.lines and all(line.startswith("nagasim.core.seizing|") for line in handler.lines)
    assert initialize_sim_env().ctx.debug is True


def test_no_per_entity_debug_call_is_made_with_debug_off(monkeypatch):
    calls = []
    monkeypatch.setattr(logging.Logger, "debug", lambda self, msg, *args, **kw: calls.append(msg))
    run_single_simulation(str(MODELS / "single_server.json"), seed=0, run_len=50, warmup=0)
    assert not [msg for msg in calls if msg.startswith("Entity ")]
