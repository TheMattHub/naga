"""Exposed eye levels read incrementally (A2-R6, final review I2).

`eye<k>.blocked` comes from each part's nose (D1390). The levels were read again for every part
against every eye at each stage change; now a part is read again only when a stage of it or of a
part ahead changes or its next edge falls due, and an eye its back has passed is not read again.
Three checks: a front eye and a back eye at one sensor (the reviewer's gate: the level is 1
exactly while a part lies between its two edges), the old full re-read as a reference, and the
cost on a long packed belt.
"""
import copy
import math
import random

import pytest

from conftest import create, dispose, process
from test_conveyor_divert_spec import belt, model
from nagasim import api
from nagasim.core.blocks import conveyor as conveyor_module
from nagasim.core.blocks._belt_points import write_exposed
from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import setup_run, step_until
from nagasim.core.trace import TraceRecorder


def _build(rng, more=False):
    """A random belt: accumulating or not, a stop with or without a contended resource, a
    divert with a push time, a part length, a front and a back eye at one sensor; *more* also
    draws a merge fed from a side source (a part lands mid-order) or a release rule."""
    length = rng.choice([5.0, 6.0, 8.0])
    acc = rng.random() < 0.75
    conveyor = belt(name="B1", length=length, speed=rng.choice([0.5, 1.0]),
                    spacing=rng.choice([0.5, 0.8]), accumulating=acc, exit_resource="R")
    p = conveyor["params"]
    sensor = round(rng.uniform(0.6, length - 0.3), 2)
    p["eyes"] = [{"at": sensor}, {"at": sensor, "edge": "back"}]
    if rng.random() < 0.5:
        p["part_length"] = rng.choice([0.3, 0.45])
    shape = rng.choice(["stop_res", "push", "stop_res+push", "plain", "stop"])
    blocks, links, resources = [], [], ["R"]
    if "stop" in shape:
        at = round(rng.uniform(0.8, length - 0.8), 1)
        if abs(at - sensor) < 0.05:
            at = round(at + 0.1, 1)
        res = "Q" if "res" in shape else ""
        p["stops"] = [{"at": at, "time": rng.choice([0.4, 1.5, 3.0]), "resource": res}]
        if res:
            resources.append("Q")
            blocks += [create(name="q_src", e_type="QJOB", distribution="Exponential",
                              dist_params={"rate": 0.3}, max_arrivals=12, start_time=0),
                       process(name="q_job", resource="Q", value=rng.choice([1.0, 2.5])),
                       dispose(name="q_out")]
            links += [{"source": "q_src", "target": "q_job"},
                      {"source": "q_job", "target": "q_out"}]
    side = []
    if "push" in shape and acc:
        at = round(rng.uniform(0.8, length - 0.8), 1)
        while any(abs(at - s["at"]) < 0.2 for s in p["stops"]) or abs(at - sensor) < 0.05:
            at = round(at + 0.3, 1)
        if at < length - 0.2:
            p["diverts"] = [{"at": at, "condition": "entity.id % 3 == 0",
                             "time": rng.choice([0.5, 1.2]), "when_full": "wait"}]
            side = [dispose(name="side")]
    if more and acc and rng.random() < 0.6:
        if rng.random() < 0.5 and not p["stops"]:
            at = round(rng.uniform(0.8, length - 0.8), 1)
            p["merges"] = [{"at": at, "from": "side_src", "priority": "belt"}]
            blocks.append(create(name="side_src", max_arrivals=8, dist_params={"value": 2.3},
                                 start_time=0.7))
            links.append({"source": "side_src", "target": "B1"})
        else:
            p["release"] = rng.choice([{"metering": {"interval": 0.9}},
                                       {"slug": {"count": 2, "timeout": 3.0}}])
    constant = rng.random() < 0.5
    blocks += [create(max_arrivals=14, distribution="Constant" if constant else "Exponential",
                      dist_params=({"value": rng.choice([0.6, 1.0, 1.7])} if constant
                                   else {"rate": rng.choice([0.7, 1.4])}), start_time=0),
               conveyor, process(name="pick", resource="R", value=rng.choice([0.3, 0.9, 1.6]),
                                 action="Delay Release"), dispose(), *side]
    links += [{"source": "create_1", "target": "B1"}, {"source": "B1", "target": "pick"},
              {"source": "pick", "target": "dispose_1"}]
    if side:
        links.append({"source": "B1", "target": "side"})
    return model(blocks, links, resources, time_unit="seconds")


def _valid(doc):
    return not [p for p in validate_model_structured(copy.deepcopy(doc)) if p.severity == "error"]


def _exposed(doc):
    on = copy.deepcopy(doc)
    next(b for b in on["blocks"] if b["name"] == "B1")["params"]["expose"] = True
    return on


def _levels(writes):
    """(time, level) at each change, the last write of an instant counting."""
    merged = []
    for t, v in writes:
        if merged and abs(merged[-1][0] - t) < 1e-9:
            merged[-1] = (merged[-1][0], v)
        else:
            merged.append((t, v))
    out, last = [], 0
    for t, v in merged:
        if v != last:
            out.append((t, v))
            last = v
    return out


def _frames_key(results):
    import hashlib
    digest = hashlib.sha256()
    for name in sorted(results):
        digest.update(name.encode())
        digest.update(results[name].to_csv(float_format="%.12g").encode())
    return digest.hexdigest()


def _gate(doc, identity=False):
    """The level of eye 0 against the front/back eye events: None when a part left before its
    back passed the sensor (a divert there), since then the events cannot say. No instant writes
    two values; *identity*: every frame is the same with expose on and off."""
    if identity:
        assert _frames_key(api.run_model(copy.deepcopy(doc), replications=1, seed=3)) ==             _frames_key(api.run_model(_exposed(doc), replications=1, seed=3))
    rec = TraceRecorder()
    env, _ = setup_run(_exposed(doc), seed=3, run_len=5000, warmup=0.0, subscribers=(rec,))
    writes = []
    env.ctx.variable_hooks.setdefault("B1.eye0.blocked", []).append(
        lambda n: writes.append((env.now, env.ctx.variables[n])))
    step_until(env, 5000)
    assert not any(abs(a[0] - b[0]) < 1e-9 and a[1] != b[1] for a, b in zip(writes, writes[1:]))
    front, back = {}, {}
    for e in rec.events:
        if e.kind == "eye" and e.block == "B1":
            (front if int(e.value) == 0 else back).setdefault(e.entity_id, e.t)
    if set(front) - set(back):
        return None
    marks = sorted([(t, 1) for t in front.values()] + [(back[i], -1) for i in front],
                   key=lambda m: (m[0], -m[1]))
    want, level, cover, i = [], 0, 0, 0
    while i < len(marks):
        t = marks[i][0]
        while i < len(marks) and abs(marks[i][0] - t) < 1e-9:
            cover += marks[i][1]
            i += 1
        if (1 if cover > 0 else 0) != level:
            level = 1 - level
            want.append((t, level))
    got = _levels(writes)
    assert len(got) == len(want), (got, want)
    for (tg, vg), (tw, vw) in zip(got, want):
        assert vg == vw and tg == pytest.approx(tw, abs=1e-6), (got, want)
    return len(want)


def _gate_cases(count, identity=False):
    rng = random.Random(4242)
    checked = 0
    for _ in range(count):
        doc = _build(rng)
        if not _valid(doc):
            continue
        if _gate(doc, identity) is not None:
            checked += 1
    return checked


def test_gate_front_and_back_eye_first_belts():
    assert _gate_cases(12) >= 8


@pytest.mark.slow
def test_gate_front_and_back_eye_all_belts():
    """The reviewer's 85 belts: 81 comparable, all exact (the other 4 divert a part across the
    sensor before its back passes it), and expose on moves no frame on any of the 85."""
    assert _gate_cases(90, identity=True) >= 80


def _full_reread(self, since=None, wake=False):
    """The reading before A2-R6: every part against every eye, at every call (reference)."""
    now, tol = self.env.now, 1e-9
    covering = {eye.index: set() for eye in self._eyes}
    due = math.inf
    for rec in self._order:
        length = self.item_length(rec[8])
        for eye in self._eyes:
            front = self._nose_reach(rec, eye.sensor)
            if front > now + tol:
                due = min(due, front)
                continue
            back = self._nose_reach(rec, eye.sensor + length)
            if back > now + tol:
                covering[eye.index].add(rec[8].id)
                due = min(due, back)
    for index, parts in covering.items():
        self._covering[index] = parts
        write_exposed(self, f"eye{index}.blocked", 1 if parts else 0)
    if due < math.inf:
        self._eye_due = [t for t in self._eye_due if t > now + tol]
        if not any(t <= due + tol for t in self._eye_due):
            self._eye_due.append(due)
            conveyor_module.timeout_at(self.env, due).callbacks.append(
                lambda _event: self._eye_levels())


def _writes(doc, monkeypatch=None):
    on = _exposed(doc)
    eyes = len(next(b for b in on["blocks"] if b["name"] == "B1")["params"]["eyes"])
    env, _ = setup_run(on, seed=3, run_len=3000, warmup=0.0)
    log = []
    for k in range(eyes):
        env.ctx.variable_hooks.setdefault(f"B1.eye{k}.blocked", []).append(
            lambda n: log.append((env.now, n, env.ctx.variables[n])))
    step_until(env, 3000)
    return log


@pytest.mark.slow
def test_incremental_levels_write_what_the_full_reread_wrote(monkeypatch):
    """Up to eight eyes on random belts, merges and release rules among them: the same writes
    in the same order, each within 1e-9 s (an edge time computed at another instant can differ
    in the last bits)."""
    rng, compared = random.Random(9), 0
    for _ in range(80):
        doc = _build(rng, more=True)
        p = next(b for b in doc["blocks"] if b["name"] == "B1")["params"]
        extra = sorted({round(rng.uniform(0.3, p["length"] - 0.2), 2)
                        for _ in range(rng.randint(1, 6))})
        p["eyes"] += [{"at": a} for a in extra]
        if not _valid(doc):
            continue
        new = _writes(doc)
        with monkeypatch.context() as m:
            m.setattr(conveyor_module.Conveyor, "_eye_levels", _full_reread)
            old = _writes(doc)
        assert len(new) == len(old)
        for a, b in zip(new, old):
            assert a[1:] == b[1:] and a[0] == pytest.approx(b[0], abs=1e-9), (a, b)
        compared += 1
    assert compared >= 60


def _packed(parts, eyes, expose):
    pts = [round(100 * (k + 1) / (eyes + 1), 3) for k in range(eyes)]
    conveyor = belt(length=100, speed=1.0, spacing=0.5, exit_resource="R")
    conveyor["params"]["eyes"] = [{"at": a} for a in pts]
    conveyor["params"]["expose"] = expose
    blocks = [create(max_arrivals=parts, dist_params={"value": 0.5}, start_time=0), conveyor,
              process(name="pick", resource="R", value=0.8, action="Delay Release"), dispose()]
    links = [{"source": "create_1", "target": "belt"}, {"source": "belt", "target": "pick"},
             {"source": "pick", "target": "dispose_1"}]
    return model(blocks, links, ["R"])


@pytest.mark.slow
@pytest.mark.perf
def test_expose_on_a_packed_belt_with_many_eyes_runs_in_seconds():
    """120 parts every 0.5 s onto a 100 m belt with 20 exposed eyes, the end letting one go every
    0.8 s: 111.6 s with the full re-read, about 3.4 s now (0.2 s with expose off). The
    wall-clock guard (run alone) fails if the full re-read comes back."""
    import time
    doc = _packed(120, 20, True)
    started = time.process_time()
    results = api.run_model(doc, replications=1)
    assert time.process_time() - started < 12.0
    assert results["entity_log"]["Exited At"].notna().sum() == 120
