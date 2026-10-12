"""Generate golden determinism snapshots (ROADMAP 2.5).

For 4 small curated models x 2 seeds: a hash of the rounded system KPI frame
plus the entity count, written to tests/golden/<model>_<seed>.json. Generated
once, BEFORE any performance optimisation; `tests/test_golden.py` re-derives
the same hash after every change and fails on any drift.

    PY tools/make_golden.py

Only ever re-run this deliberately (a real, intended KPI change -- e.g. the
scipy -> numpy sampler swap ROADMAP 2.5 explicitly allows) and say so in the
commit/report; regenerating it to silence a failing test defeats the test.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nagasim.api import run_model  # noqa: E402

MODELS = ["single_server.json", "decide_routing.json", "hold_signal_flow.json", "priority_rework.json"]
SEEDS = [0, 1]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"
MODELS_DIR = REPO_ROOT / "nagasim" / "models"


def payload_of(results: dict) -> dict:
    """(system KPI frame rounded to 6 decimals, entity count) of one replication's
    results, however it was run -- `tests/test_debugger.py` hashes a stepped one."""
    sys_df = results["system"]
    rounded = {str(row["Metric"]): (round(row["Value"], 6) if isinstance(row["Value"], (int, float))
                                    else row["Value"])
               for _, row in sys_df.iterrows()}
    return {"system": rounded, "entity_count": len(results["entity_log"])}


def golden_payload(model: str, seed: int) -> dict:
    """`payload_of` one (model, seed) -- single replication, everything else from
    the model's own "simulation" section."""
    return payload_of(run_model(str(MODELS_DIR / model), replications=1, seed=seed))


def golden_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# -- view state (PHASE7 7.7, D196) ------------------------------------------------
# What a view derives from a run, hashed per bundled model: the proof that moving the
# derivations out of the editor's player changed none of them. Every reader of it
# (the editor's `TracePlayer`, the engine functions beside `Timeline`) must give the
# hash committed in VIEW_STATE.
VIEW_STATE = GOLDEN_DIR / "view_state.json"
VIEW_RUN = {"replications": 1, "seed": 0, "warmup": 0, "run_length": 200}
VIEW_STEPS = 24         # 25 instants: duration * k / 24


def view_models() -> list[str]:
    return sorted(path.name for path in MODELS_DIR.glob("*.json"))


def has_artwork(model: str) -> bool:
    """Whether this copy of a bundled model carries asset graphics that nominate what the
    view shows (`graphics.state_source`). A placed library asset keeps them in the model
    file; the engine's own repository ships the models without them. Graphics with no
    `state_source` change nothing the view derives, so such a model is still reproduced."""
    layout = json.loads((MODELS_DIR / model).read_text(encoding="utf-8"))
    graphics = ((definition.get("info") or {}).get("graphics")
                for definition in (layout.get("submodels") or {}).values())
    return any(isinstance(item, dict) and item.get("state_source") for item in graphics)


def canonical(value):
    """*value* as plain JSON data that compares equal however it was read: a tuple
    is a list, a key is text and a number is a float rounded to 6 decimals (a
    document holds 30.0 where the file holds 30)."""
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 6)
    return value


def view_state_hash(model: str, read) -> str:
    """sha256 of what *read*(model path, trace, times) gives for one traced
    replication of *model*: the maps, the block places and the outputs at 25
    instants (the keys: `read_through_player` in `tests/test_ui_view_state.py`)."""
    from nagasim.core.playback import Timeline
    from nagasim.core.trace import TraceRecorder

    trace = TraceRecorder()
    run_model(str(MODELS_DIR / model), trace=trace, **VIEW_RUN)
    duration = Timeline(trace.events).duration
    times = [duration * k / VIEW_STEPS for k in range(VIEW_STEPS + 1)]
    payload = canonical(read(MODELS_DIR / model, trace, times))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def view_baseline(model: str) -> str | None:
    """The committed hash of *model*, or None where this copy cannot give it: the
    hash was written from a model carrying asset graphics and this copy has none."""
    baseline = json.loads(VIEW_STATE.read_text(encoding="utf-8"))
    if model in baseline["artwork"] and not has_artwork(model):
        return None
    return baseline["hashes"][model]


def write_view_state(read) -> None:
    """Write VIEW_STATE from *read*. Only ever deliberately, like the hashes above."""
    record = {"artwork": [model for model in view_models() if has_artwork(model)],
              "hashes": {model: view_state_hash(model, read) for model in view_models()}}
    VIEW_STATE.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def read_view_state(path, trace, times) -> dict:
    """The view state through the engine alone: the derivations beside `Timeline`
    on the layout as loaded from the file. Same keys as the editor's reader
    (`read_through_player` in `tests/test_ui_view_state.py`); a deliberate change
    of a derivation rewrites the baseline with `write_view_state(read_view_state)`."""
    from nagasim.core import network, playback
    from nagasim.core.schema import load_layout

    layout = load_layout(str(path))
    timeline = playback.Timeline(trace.events)
    sources = playback.asset_state_sources(layout)
    units = playback.unit_sources(layout, sources)
    nodes = {node.get("name"): node for node in (layout.get("network") or {}).get("nodes") or []}
    frames = []
    for t in times:
        state = timeline.state_at(t)
        frames.append({"asset_states": playback.asset_states(state, sources),
                       "unit_counts": playback.unit_counts(state, units),
                       "resources": playback.resource_rows(state),
                       "kpi": playback.kpi_snapshot(timeline, t)})
    return {"sources": sources, "units": units, "nodes": playback.node_blocks(layout),
            "colors": playback.state_colors(layout),
            "places": {block["name"]: network.block_place(block.get("params") or {},
                                                          block.get("layout_position"), nodes)
                       for block in layout.get("blocks") or []},
            "frames": frames}


def main() -> int:
    GOLDEN_DIR.mkdir(exist_ok=True)
    for model in MODELS:
        for seed in SEEDS:
            payload = golden_payload(model, seed)
            record = {"model": model, "seed": seed, "hash": golden_hash(payload), "payload": payload}
            out = GOLDEN_DIR / f"{Path(model).stem}_seed{seed}.json"
            out.write_text(json.dumps(record, indent=2, sort_keys=True))
            print(f"{out.relative_to(REPO_ROOT)}: {record['hash'][:12]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
