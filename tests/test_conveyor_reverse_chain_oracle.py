"""A reversible chain against an independent rigid-belt model (A2-R5, final review I1).

The spec chain (A 4 m + B 4 m, 1 m/s, spacing 0.5, free exits) moves as one 8 m belt: a part
enters at A's start only while it runs forward and the last part on is 0.5 m in, and leaves by
B's end forward or A's start backward. The oracle below is that belt as a plain event loop, with
no engine code, compared on random arrivals and reversals.
"""
import random

import pytest

from conftest import create, dispose, run_env
from test_conveyor_reverse import _signal
from test_conveyor_reverse_chain import _chain_doc

L, OCC, V = 8.0, 0.5, 1.0


def _oracle(arrivals, toggles, horizon=10_000.0):
    """[(exit time, 'end' | 'back') or None] per part, in arrival order."""
    t, d, on, queue, out = 0.0, 1, [], [], {}
    nxt_arr = nxt_tog = 0
    while t < horizon:
        cand = []
        if nxt_arr < len(arrivals):
            cand.append((arrivals[nxt_arr], 0, "arr"))
        if nxt_tog < len(toggles):
            cand.append((toggles[nxt_tog], 1, "tog"))
        if on:
            lead = (L - max(x for x, _ in on)) if d > 0 else min(x for x, _ in on)
            cand.append((t + lead / V, 2, "exit"))
        if queue and d > 0:
            near = min((x for x, _ in on), default=None)
            cand.append((t + (0.0 if near is None else max(0.0, (OCC - near) / V)), 3, "enter"))
        if not cand:
            break
        when, _prio, kind = min(cand)
        for item in on:
            item[0] += d * V * (when - t)
        t = when
        if kind == "arr":
            queue.append(nxt_arr)
            nxt_arr += 1
        elif kind == "tog":
            d, nxt_tog = -d, nxt_tog + 1
        elif kind == "exit":
            item = max(on) if d > 0 else min(on)
            on.remove(item)
            out[item[1]] = (t, "end" if d > 0 else "back")
        else:
            on.append([0.0, queue.pop(0)])
    return [out.get(i) for i in range(len(arrivals))]


def _engine(arrivals, toggles):
    """The same line in the engine, one single-arrival CREATE per part and per signal."""
    doc = _chain_doc([0.0], [])
    doc["blocks"] = [b for b in doc["blocks"] if b["name"] != "parts"]
    doc["links"] = [k for k in doc["links"] if k["source"] != "parts"]
    for i, at in enumerate(arrivals):
        doc["blocks"].append(create(name=f"p{i}", e_type="PART", max_arrivals=1, start_time=at))
        doc["links"].append({"source": f"p{i}", "target": "A"})
    doc["blocks"] += [_signal(), dispose(name="sig_out")]
    doc["links"].append({"source": "fire", "target": "sig_out"})
    for i, at in enumerate(toggles):
        doc["blocks"].append(create(name=f"s{i}", e_type="SIG", max_arrivals=1, start_time=at))
        doc["links"].append({"source": f"s{i}", "target": "fire"})
    env, _ = run_env(doc, 10_000)
    parts = sorted((e for e in env.ctx.all_entities if e.e_type == "PART"),
                   key=lambda e: e.creation_time)
    return ([None if e.deletion_time is None else e.deletion_time for e in parts],
            env.ctx.variables["n_end"], env.ctx.variables["n_back"])


def _cases(seed=77, count=120):
    """The reviewer's 120 draws: 3-25 parts, 1-6 reversals, ending forward so every part leaves."""
    rng = random.Random(seed)
    for _ in range(count):
        n, t, arrivals = rng.randint(3, 25), 0.0, []
        for _ in range(n):
            t += rng.choice([0.6, 0.9, 1.3]) + rng.random() * rng.choice([0.5, 3.0])
            arrivals.append(round(t, 3))
        k = rng.choice([1, 2, 2, 3, 4, 5, 6])
        toggles = sorted(round(rng.uniform(1.0, arrivals[-1] + 10.0) + 0.0007, 4)
                         for _ in range(k))
        if len(set(toggles)) < k or set(toggles) & set(arrivals):
            continue
        if k % 2:
            toggles.append(round(toggles[-1] + rng.uniform(0.5, 20.0), 4))
        yield arrivals, toggles


def _check(arrivals, toggles):
    want = _oracle(arrivals, toggles)
    got, n_end, n_back = _engine(arrivals, toggles)
    for i, (g, w) in enumerate(zip(got, want)):
        assert (g is None) == (w is None), (i, g, w, arrivals, toggles)
        if w is not None:
            assert g == pytest.approx(w[0], abs=1e-6), (i, g, w, arrivals, toggles)
    assert n_end == sum(1 for w in want if w and w[1] == "end")
    assert n_back == sum(1 for w in want if w and w[1] == "back")


def test_crossing_back_is_not_starved_by_parts_waiting_to_board():
    """I1: reversed at 5 for good, one part on each piece, 20 more parts from 5.5 that cannot
    board. The part on B (5 m along) crosses back and leaves A's start at 10, the one on A at
    8; before A2-R5 the waiting parts held A's cells and the part on B stood at the joint."""
    doc = _chain_doc([0.0, 2.0], [5.0])
    doc["blocks"].append(create(name="more", e_type="PART", max_arrivals=20,
                                dist_params={"value": 0.01}, start_time=5.5))
    doc["links"].append({"source": "more", "target": "A"})
    env, _ = run_env(doc, 200)
    left = sorted(e.deletion_time for e in env.ctx.all_entities
                  if e.e_type == "PART" and e.deletion_time is not None)
    assert left == pytest.approx([8.0, 10.0])
    assert env.ctx.variables["n_back"] == 2
    assert env.ctx.conveyors["A"].space.content.level == 0     # waiting parts hold no cells


def test_oracle_all_random_chains():
    """All of the reviewer's random chains are exact against the rigid belt (6 of them were
    0.5 to 1.5 s off before A2-R5)."""
    for arrivals, toggles in _cases():
        _check(arrivals, toggles)
