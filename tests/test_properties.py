"""Property-based tests (ROADMAP 2.6): a strategy generates small, always-valid
models (`_model_strategy`) and a handful of invariants are checked against
every one hypothesis can find.

Model shape: CREATE -> a chain of 2-7 middle blocks (DELAY, PROCESS, ASSIGN,
RECORD, DECIDE-chance) -> an optional BATCH (with or without a matching
SEPARATE, so a batch can be permanently consumed) -> an optional rework loop
(DECIDE-Condition on a bounded `entity.rework_count`, so it is a real cycle in
the graph but deterministically terminates after at most 2 passes) -> DISPOSE.
1-2 resources back the PROCESS blocks. Arrivals are always finite
(`max_arrivals` 2-12), so every generated model is a DAG plus at most one
bounded back-edge and always finishes: `step_until` is called with no time
limit (drain to completion) and is expected to actually drain.

`max_examples=40` and `deadline=None` keep each property's search bounded
but real; the whole file is well under the ~20s budget on this machine.
"""
from __future__ import annotations

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from nagasim.api import run_model
from nagasim.core import schema
from nagasim.core.document import ModelDocument
from nagasim.core.events import EventBus
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until
from nagasim.core.outputs import calc_kpis
from nagasim.experiments import apply_overrides, list_override_paths

_SETTINGS = settings(max_examples=40, deadline=None,
                     suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])

_DIST_CHOICES = st.sampled_from([
    ("Constant", {"value": 1.0}),
    ("Constant", {"value": 2.0}),
    ("Exponential", {"rate": 1.0}),
    ("Exponential", {"rate": 0.5}),
])


def _dist_block(kind, name, prev, extra=None):
    dist_type, dist_params = extra
    params = {"distribution": dist_type, "dist_params": dist_params, "time_unit": "base"}
    return {"name": name, "description": name, "type": kind, "position": [1, 0], "params": params}


@st.composite
def _model_strategy(draw):
    """A small, always-valid, always-terminating model (see module docstring)."""
    counter = {"n": 0}

    def name(prefix):
        counter["n"] += 1
        return f"{prefix}_{counter['n']}"

    resources = [f"R{i}" for i in range(draw(st.integers(min_value=1, max_value=2)))]
    blocks: list[dict] = []
    links: list[dict] = []

    create_name = name("create")
    dist = draw(_DIST_CHOICES)
    max_arrivals = draw(st.integers(min_value=2, max_value=12))
    blocks.append({
        "name": create_name, "description": create_name, "type": "CREATE", "position": [0, 0],
        "params": {"e_type": "DEFAULT", "arrival_mode": "Interarrival", "schedule_name": "",
                   "distribution": dist[0], "dist_params": dist[1], "time_unit": "base",
                   "entities_per_arrival": 1, "max_arrivals": max_arrivals, "start_time": 0},
    })
    prev = create_name

    n_middle = draw(st.integers(min_value=2, max_value=7))
    kinds = draw(st.lists(st.sampled_from(["DELAY", "PROCESS", "ASSIGN", "RECORD", "DECIDE"]),
                         min_size=n_middle, max_size=n_middle))
    for kind in kinds:
        if kind == "DELAY":
            n = name("delay")
            blocks.append(_dist_block("DELAY", n, prev, draw(_DIST_CHOICES)))
            links.append({"source": prev, "target": n})
            prev = n
        elif kind == "PROCESS":
            n = name("process")
            dist_type, dist_params = draw(_DIST_CHOICES)
            res = draw(st.sampled_from(resources))
            blocks.append({"name": n, "description": n, "type": "PROCESS", "position": [1, 0],
                           "params": {"action": "Seize Delay Release", "distribution": dist_type,
                                      "dist_params": dist_params, "time_unit": "base",
                                      "priority": 0, "quantity": 1, "resource_name": res}})
            links.append({"source": prev, "target": n})
            prev = n
        elif kind == "ASSIGN":
            n = name("assign")
            blocks.append({"name": n, "description": n, "type": "ASSIGN", "position": [1, 0],
                           "params": {"assignments": [{"target": "entity.tag", "expr": "1"}]}})
            links.append({"source": prev, "target": n})
            prev = n
        elif kind == "RECORD":
            n = name("record")
            blocks.append({"name": n, "description": n, "type": "RECORD", "position": [1, 0],
                           "params": {"stat_name": "seen", "kind": "count", "expr": "1"}})
            links.append({"source": prev, "target": n})
            prev = n
        else:  # DECIDE (Chance): both branches reconverge on the same node
            n = name("decide")
            after = name("after_decide")
            blocks.append({"name": n, "description": n, "type": "DECIDE", "position": [1, 0],
                           "params": {"mode": "Chance", "probs": [0.5], "conditions": []}})
            blocks.append({"name": after, "description": after, "type": "ASSIGN", "position": [1, 0],
                           "params": {"assignments": [{"target": "entity.tag", "expr": "1"}]}})
            links.append({"source": prev, "target": n})
            links.append({"source": n, "target": after})
            links.append({"source": n, "target": after})
            prev = after

    include_batch = draw(st.booleans())
    if include_batch:
        batch_name = name("batch")
        blocks.append({"name": batch_name, "description": batch_name, "type": "BATCH", "position": [1, 0],
                       "params": {"batch_size": 2, "batched_entity_type": "GROUP"}})
        links.append({"source": prev, "target": batch_name})
        prev = batch_name
        if draw(st.booleans()):  # matching SEPARATE -- members return to WIP
            sep_name = name("separate")
            blocks.append({"name": sep_name, "description": sep_name, "type": "SEPARATE",
                           "position": [1, 0],
                           "params": {"mode": "Retain original entity values", "attrs": []}})
            links.append({"source": batch_name, "target": sep_name})
            prev = sep_name
        # else: the batch is left permanently formed -- members become "consumed"

    include_rework = draw(st.booleans())
    if include_rework:
        init_n, target_n, decide_n, inc_n = (name("assign_rework_init"), name("delay_rework_target"),
                                              name("decide_rework"), name("assign_rework_inc"))
        blocks.append({"name": init_n, "description": init_n, "type": "ASSIGN", "position": [1, 0],
                       "params": {"assignments": [{"target": "entity.rework_count", "expr": "0"}]}})
        links.append({"source": prev, "target": init_n})
        blocks.append({"name": target_n, "description": target_n, "type": "DELAY", "position": [1, 0],
                       "params": {"distribution": "Constant", "dist_params": {"value": 1.0},
                                  "time_unit": "base"}})
        links.append({"source": init_n, "target": target_n})
        blocks.append({"name": decide_n, "description": decide_n, "type": "DECIDE", "position": [1, 0],
                       "params": {"mode": "Condition", "conditions": ["entity.rework_count < 2"],
                                  "probs": []}})
        links.append({"source": target_n, "target": decide_n})
        blocks.append({"name": inc_n, "description": inc_n, "type": "ASSIGN", "position": [1, 0],
                       "params": {"assignments": [{"target": "entity.rework_count",
                                                    "expr": "entity.rework_count + 1"}]}})
        links.append({"source": decide_n, "target": inc_n})   # condition true -> rework (index 0)
        links.append({"source": inc_n, "target": target_n})   # bounded back-edge
        prev = decide_n   # ELSE branch (index 1, added below) -> dispose

    dispose_name = name("dispose")
    blocks.append({"name": dispose_name, "description": dispose_name, "type": "DISPOSE",
                   "position": [1, 0],
                   "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                              "time_unit": "base"}})
    links.append({"source": prev, "target": dispose_name})

    return {
        "schema_version": schema.CURRENT_VERSION,
        "blocks": blocks, "links": links,
        "resources": [{"name": r, "capacity": 1, "discipline": "FIFO"} for r in resources],
        "resource_sets": [], "entities": [], "variables": [],
        "schedules": [], "failures": [], "scenarios": [],
        "simulation": {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes"},
    }


# ------------------------------------------------------------- (a)(b)(c)(d) --
def _run_with_trace(layout, seed=0):
    """Run *layout* to completion (drain), recording every (t, event) the bus
    emits. Returns (env, events)."""
    events = []
    env = initialize_sim_env(seed=seed)
    env.ctx.bus.subscribe(lambda name, **payload: events.append((name, payload)))
    model = prepare_model(env, layout)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 0)   # no limit: every generated model is built to drain
    return env, events


@given(layout=_model_strategy())
@_SETTINGS
def test_generated_model_validates_conserves_and_never_goes_negative(layout):
    # (a) the generated layout validates with no (non-warning) errors.
    problems = [p for p in schema.validate_model(layout) if not p.startswith("warning:")]
    assert problems == [], problems

    env, events = _run_with_trace(layout)
    all_names = {b["name"] for b in layout["blocks"]}

    # (c) time never decreases in the trace, and every entity_moved target exists.
    times = [payload["t"] for _, payload in events]
    assert times == sorted(times)
    for name, payload in events:
        if name == "entity_moved" and payload.get("target") is not None:
            assert payload["target"] in all_names

    # (d) no collector goes negative, and busy never exceeds capacity, at
    # every resource log snapshot.
    for res in env.ctx.resource_manager.resources.values():
        for entry in res.log:
            assert entry.in_use >= 0
            assert entry.queue_length >= 0
            assert entry.in_use <= res.capacity
    for store in env.ctx.store_manager.all():
        for entry in store.log:
            assert entry.items >= 0

    # (b) conservation of entities: every entity CREATE ever spawned (not a
    # BATCH's own synthetic entity, DEVLOG #29 -- `batch_elems` marks those)
    # ends up completed, still live, or permanently consumed by a batch.
    members = [e for e in env.ctx.all_entities if not e.batch_elems]
    completed = [e for e in members if e.deletion_time is not None]
    live = [e for e in members if e in env.ctx.live_entities]
    consumed = [e for e in members if e.deletion_time is None and e not in env.ctx.live_entities]
    assert len(completed) + len(live) + len(consumed) == len(members)
    assert not (set(map(id, completed)) & set(map(id, live)))
    assert all(getattr(e, "in_batch", None) is not None for e in consumed)
    assert all(getattr(e, "in_batch", None) is None for e in live)


# ------------------------------------------------------------------- (e) -----
@given(layout=_model_strategy(), seed=st.integers(min_value=0, max_value=1000))
@_SETTINGS
def test_same_seed_and_a_noop_override_reproduce_and_another_seed_keeps_the_schema(layout, seed):
    """(e) + (g) on one pair of runs: the second run is the SAME seed on the
    layout with a no-op override applied, so equal frames prove both that a
    seed fully determines a run and that a no-op override changes nothing
    (a failure of either would show; they cannot cancel)."""
    paths = list_override_paths(layout)
    noop = [{"path": paths[0][0], "value": paths[0][1]}] if paths else []
    again = apply_overrides(layout, noop) if noop else layout
    r1 = run_model(layout, seed=seed)
    r2 = run_model(again, seed=seed)
    pd.testing.assert_frame_equal(r1["system"], r2["system"])

    r3 = run_model(layout, seed=seed + 1)
    assert list(r1["system"]["Metric"]) == list(r3["system"]["Metric"])
    assert set(r1.keys()) == set(r3.keys())


# ------------------------------------------------------------------- (f) -----
@given(layout=_model_strategy())
@_SETTINGS
def test_layout_document_roundtrip_is_a_fixed_point_and_undo_redo_exact(layout):
    doc1 = ModelDocument(layout)
    canonical = doc1.to_layout()
    doc2 = ModelDocument(canonical)
    assert doc2.to_layout() == canonical   # round-trip from a canonical layout is a fixed point

    before = doc2.to_layout()
    a_block = next(iter(doc2.blocks))
    doc2.set_description(a_block, doc2.blocks[a_block].description + "!")
    assert doc2.to_layout() != before
    doc2.undo()
    assert doc2.to_layout() == before
    doc2.redo()
    assert doc2.to_layout() != before
    doc2.undo()
    assert doc2.to_layout() == before
