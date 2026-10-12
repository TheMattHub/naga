"""PHASE6 G5: the playable templates (ed_game.json, sd_supply_game.json,
sd_training_pipeline.json) validate clean and play headlessly through
`SimSession`: advance, answer every pending decision with a fixed rule, move a
slider once, finish with a non-empty dashboard frame and game_log, and replay
the game_log to identical results (D133/D134, ARCHITECTURE 5.40-5.41)."""
from pathlib import Path

import json

from nagasim.core.schema import validate_model_structured
from nagasim.core.session import SimSession

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
ED_GAME = MODELS_DIR / "ed_game.json"
SD_GAME = MODELS_DIR / "sd_supply_game.json"
TRAINING_PIPELINE = MODELS_DIR / "sd_training_pipeline.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _play(layout: dict, *, decision_option, control_name: str, control_value) -> tuple[SimSession, dict]:
    """Advance to the horizon, answering every pending decision with
    `decision_option` (an int or a callable of the policy name), moving
    *control_name* once. Returns (session, results)."""
    session = SimSession(layout, seed=0)
    moved = False
    while True:
        state = session.advance()
        if state == "decision":
            for pending in session.pending():
                option = decision_option(pending["policy"]) if callable(decision_option) else decision_option
                session.decide(pending["policy"], option)
                if not moved:
                    session.apply_control(control_name, control_value)
                    moved = True
        elif state in ("horizon", "drained"):
            break
        else:
            raise AssertionError(f"unexpected advance() state: {state!r}")
    if not moved:      # a model with no decisions before the horizon: still exercise the control
        session.apply_control(control_name, control_value)
    results = session.finish()
    return session, results


def _assert_playable(layout: dict, results: dict) -> None:
    assert not results["dashboard"].empty
    assert not results["game_log"].empty


def test_ed_game_validates_clean():
    problems = validate_model_structured(_load(ED_GAME))
    assert not [p for p in problems if p.severity == "error"]


def test_sd_supply_game_validates_clean():
    problems = validate_model_structured(_load(SD_GAME))
    assert not [p for p in problems if p.severity == "error"]


def test_training_pipeline_validates_clean():
    problems = validate_model_structured(_load(TRAINING_PIPELINE))
    assert not [p for p in problems if p.severity == "error"]


def test_ed_game_plays_and_replays_exactly():
    layout = _load(ED_GAME)
    session, played = _play(layout, decision_option=0, control_name="doctor_capacity",
                            control_value=2)
    _assert_playable(layout, played)

    replayed = SimSession.replay(layout, 0, session.game_log)
    for frame in ("system", "resource_summary", "dashboard", "game_log"):
        assert played[frame].equals(replayed[frame]), frame


def test_sd_supply_game_plays_and_replays_exactly():
    layout = _load(SD_GAME)
    session, played = _play(layout, decision_option=1, control_name="orders_slider",
                            control_value=20)
    _assert_playable(layout, played)

    replayed = SimSession.replay(layout, 0, session.game_log)
    for frame in ("level_summary", "flow_summary", "dashboard", "game_log"):
        assert played[frame].equals(replayed[frame]), frame


def test_training_pipeline_plays_and_replays_exactly():
    """No DECIDE in this template (PHASE6 C3) -- only the staff_goal slider,
    the "a model with no decisions before the horizon" branch `_play` already
    covers for `_assert_playable`'s dashboard/game_log expectations."""
    layout = _load(TRAINING_PIPELINE)
    session, played = _play(layout, decision_option=0, control_name="staff_goal_slider",
                            control_value=150)
    _assert_playable(layout, played)

    replayed = SimSession.replay(layout, 0, session.game_log)
    for frame in ("level_summary", "flow_summary", "dashboard", "game_log"):
        assert played[frame].equals(replayed[frame]), frame
