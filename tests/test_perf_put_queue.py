"""Speed review C (2026-11-23): the put queue is a deque, so a grant takes its head in O(1).

The differential tests drive the new queue and a frozen copy of the old one (a list
`SortedQueue` with bisection, granted by SimPy's own `_trigger_put` and its `pop(idx)`)
with the same random scripts: priorities, same-instant ties, preemption, capacity above
one, reneging, multi-unit groups, blockers, warm-start re-keys and the rerank discipline.
Every step's queue, holders, grants and preemptions must be the same. The perf guard counts
calls on an overloaded queue: a list's `pop(0)` is one C call either way, so the guard
checks that no list pop or bisection runs per grant, and the calls per grant.
"""
import bisect
import cProfile
import gc
import random
import sys
from bisect import insort_right
from collections import deque
from operator import attrgetter

import pytest
import simpy
from simpy.resources.base import BaseResource
from simpy.resources.resource import SortedQueue

from nagasim.core.resource_manager import (_BLOCKER_PRIORITY, _Group, _GroupRequest, _InsortQueue,
                                           _RerankResource, _Resource)

NAN = float("nan")


# -- the frozen old code (4e87db29) ---------------------------------------------------------
class _OldInsortQueue(SortedQueue):
    def append(self, item) -> None:
        if self.maxlen is not None and len(self) >= self.maxlen:
            raise RuntimeError("Cannot append event. Queue is full.")
        insort_right(self, item, key=attrgetter("key"))


class _OldResource(_Resource):
    PutQueue = _OldInsortQueue
    _trigger_put = BaseResource._trigger_put        # SimPy's loop, `pop(idx)` on the list


class _OldRerank(_RerankResource, _OldResource):    # rerank's sort, then the old loop
    pass


class _OldOpen:
    """The old `ResourceSet._open` operations on a plain list."""

    def __init__(self):
        self.waits = []

    def enqueue(self, t):
        bisect.insort(self.waits, t)

    def close(self, t):
        index = bisect.bisect_left(self.waits, t)
        if index < len(self.waits) and self.waits[index] == t:
            del self.waits[index]


# -- the scripts ----------------------------------------------------------------------------
def _script(rng, rerank=False, nan=False):
    """*nan*: some ranks (and rerank slopes) are NaN, a ranking expression's missing value."""
    capacity = rng.choice((1, 1, 2, 3))
    users = []
    for i in range(rng.randrange(10, 40)):
        users.append({
            "at": rng.choice((0.0, 0.0, 0.5, 1.0, 2.0, 3.5)),
            "priority": rng.choice((0, 1, 1, 2, 5)),
            "preempt": rng.random() < 0.3,
            "units": rng.choice((1, 1, 1, 1, 2)) if capacity > 1 else 1,
            "service": rng.choice((0.5, 1.0, 1.0, 2.5)),
            "patience": rng.choice((None, None, None, 0.5, 2.0)),
            "rekey": rng.random() < 0.15,       # a warm start re-keys it to an earlier time
            "slope": rng.choice((-1.0, 0.0, 2.0, *((NAN,) if nan else ()))) if rerank else 0.0,
            "rank": rng.choice((0.0, 0.0, 1.0, 2.0, NAN)) if nan else 0,
        })
    blockers = [(rng.choice((0.0, 1.0, 2.0)), rng.choice((0.5, 1.5))) for _ in range(rng.randrange(3))]
    return capacity, users, blockers


def _run(resource_cls, script):
    capacity, users, blockers = script
    env = simpy.Environment()
    res = resource_cls(env, capacity=capacity)
    if issubclass(resource_cls, _RerankResource):
        res.rank_of = lambda scope: scope[0] * env.now + scope[1]
    trace = []

    def tag(requests):
        return requests[0].tag

    def user(i, spec):
        yield env.timeout(spec["at"])
        units = spec["units"]
        priority = (spec["priority"], spec["rank"])
        if units == 1:
            requests = [res.request(priority=priority, preempt=spec["preempt"])]
        else:
            group = _Group(units)
            requests = [_GroupRequest(res, priority, spec["preempt"], group) for _ in range(units)]
        for request in requests:
            request.tag = i
            if spec["slope"]:
                request.rerank_scope = (spec["slope"], spec["priority"])
        if spec["rekey"]:       # as seizing._as_of: an earlier time, back in its place
            for request in requests:
                request.time = max(spec["at"] - 1.0, 0.0)
                request.key = (request.priority, request.time, not request.preempt)
                if request in res.put_queue:
                    res.put_queue.remove(request)
                    res.put_queue.append(request)
        wait = env.all_of(requests)
        try:
            if spec["patience"] is None:
                yield wait
            else:
                yield wait | env.timeout(spec["patience"])
            if not wait.triggered:
                trace.append((env.now, "renege", i))
                for request in requests:
                    if request.triggered:
                        res.release(request)
                    else:
                        request.cancel()
                return
            trace.append((env.now, "grant", i))
            yield env.timeout(spec["service"])
            trace.append((env.now, "done", i))
        except simpy.Interrupt:
            trace.append((env.now, "preempted", i))
        for request in requests:
            res.release(request)

    def blocker(at, hold):
        yield env.timeout(at)
        request = res.request(priority=_BLOCKER_PRIORITY, preempt=False)
        request.tag = "B"
        yield request
        yield env.timeout(hold)
        res.release(request)

    for i, spec in enumerate(users):
        env.process(user(i, spec))
    for at, hold in blockers:
        env.process(blocker(at, hold))
    while env.peek() < float("inf"):
        env.step()
        trace.append((env.now, [r.tag for r in res.put_queue], sorted(map(str, (u.tag for u in res.users)))))
    return trace


def test_grant_order_is_the_old_queue_s_on_random_scripts():
    rng = random.Random(7)
    for _ in range(150):
        script = _script(rng)
        assert _run(_Resource, script) == _run(_OldResource, script)


def test_rerank_grant_order_is_the_old_queue_s_on_random_scripts():
    rng = random.Random(11)
    for _ in range(60):
        script = _script(rng, rerank=True)
        assert _run(_RerankResource, script) == _run(_OldRerank, script)


def test_nan_ranks_give_the_old_queue_s_order():
    """A NaN rank leaves the queue unsorted, where the fast paths would not give bisection's
    place (review finding 3): from then on every request bisects, as on the old list."""
    rng = random.Random(13)
    for _ in range(60):
        script = _script(rng, nan=True)
        assert _run(_Resource, script) == _run(_OldResource, script)
    for _ in range(30):
        script = _script(rng, rerank=True, nan=True)
        assert _run(_RerankResource, script) == _run(_OldRerank, script)


def test_a_nan_ranked_resource_gives_the_old_entity_log(monkeypatch):
    """End to end: a LowestValueFirst resource whose ranking expression is NaN for some
    entities gives the entity log of a queue that always bisects (the old list queue)."""
    from conftest import chain, create, dispose, layout, process
    from nagasim import api
    from nagasim.core import resource_manager

    def entity_log():
        c = create("c", distribution="Exponential", dist_params={"rate": 1.0})
        a = {"name": "a", "description": "a", "type": "ASSIGN", "position": [1, 0],
             "params": {"assignments": [{"target": "entity.r", "expr":
                                         "((1e308 * 10) * 0) if (TNOW % 3 < 1) else (TNOW % 7)"}]}}
        p = process("p", value=0.9, distribution="Exponential")
        d = dispose("d")
        doc = layout([c, a, p, d], chain(c, a, p, d),
                     resources=[{"name": "Server", "capacity": 1,
                                 "discipline": "LowestValueFirst", "ranking_expr": "entity.r"}],
                     simulation={"time_limit": 400, "warmup": 0, "replications": 1})
        return api.run_model(doc, seed=3)["entity_log"]
    new = entity_log()
    monkeypatch.setattr(resource_manager._InsortQueue, "append",
                        lambda self, item: insort_right(self, item, key=attrgetter("key")))
    old = entity_log()
    assert len(new) > 400 and new.equals(old)


def test_the_set_s_open_waits_match_the_old_list(env):
    manager = env.ctx.resource_manager
    manager.register("A")
    rset = manager.register_set("Team", ["A"])
    rng = random.Random(3)
    old = _OldOpen()
    for _step in range(2_000):
        step = rng.choice((0.0, 0.0, 0.5, 1.0))
        if step:
            env.run(until=env.now + step)
        if rng.random() < 0.55 or not old.waits:
            since = env.now - rng.choice((0.0, 0.0, 0.0, 0.5, 3.0))    # a warm start's `since`
            old.enqueue(rset.enqueue(since=since))
        else:
            t = rng.choice(old.waits + [env.now + 9.0])                # a wait, or none
            old.close(t)
            rset.left(t)
        assert list(rset._open) == old.waits


def test_the_queue_is_a_deque_with_list_reads():
    env = simpy.Environment()
    res = _Resource(env, capacity=1)
    requests = [res.request(priority=(p, 0), preempt=False) for p in (2, 1, 2, 0, 1)]
    queue = res.put_queue
    assert isinstance(queue, _InsortQueue) and isinstance(queue, deque)
    assert [r.key[0][0] for r in queue] == [0, 1, 1, 2]       # the first was granted
    assert requests[3] in queue and queue.index(requests[3]) == 0 and len(queue) == 4
    queue.sort(key=lambda r: -r.key[0][0])                    # stable, as list.sort
    assert [r.key[0][0] for r in queue] == [2, 1, 1, 0]
    assert queue[1] is requests[1] and queue[2] is requests[4]


# -- the perf guard -------------------------------------------------------------------------
WAITERS = 20_000
# Calls per grant while an overloaded FIFO queue drains (release, grant, the process's next
# step), in this test. Measured 2026-11-23: 64.0 on the old list queue (its `pop(0)` per
# grant), 63.0 on the deque. One more call per grant fails it.
CALLS_PER_GRANT = 63.5


@pytest.mark.perf
# cProfile's counts differ between Python versions; the public engine CI runs 3.11-3.14
@pytest.mark.skipif(sys.version_info[:2] != (3, 12), reason="call budgets measured on 3.12")
def test_an_overloaded_queue_grants_without_list_pops_or_bisection():
    env = simpy.Environment()
    res = _Resource(env, capacity=1)
    order = []

    def user(i):
        with res.request(priority=(0, 0), preempt=False) as request:
            yield request
            order.append(i)
            yield env.timeout(1.0)

    for i in range(WAITERS):
        env.process(user(i))
    env.run(until=0.5)                      # all asked; one holds, the rest wait
    assert len(res.put_queue) == WAITERS - 1 and isinstance(res.put_queue, deque)
    collecting = gc.isenabled()       # as tools/bench_calls.py: no collection inside the count
    gc.collect()
    gc.disable()
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        env.run()
    finally:
        profiler.disable()
        if collecting:
            gc.enable()
    assert order == list(range(WAITERS))
    names = {}
    for entry in profiler.getstats():
        name = entry.code if isinstance(entry.code, str) else entry.code.co_name
        names[name] = names.get(name, 0) + entry.callcount
    calls = sum(names.values())
    # One list pop per release is SimPy's get queue (one entry); the old put queue added a
    # second, `pop(0)` of the long list, per grant.
    assert names.get("<method 'pop' of 'list' objects>", 0) <= WAITERS
    assert not any(("insort" in n or "bisect" in n or "'insert' of" in n) for n in names), \
        sorted(n for n in names if "of '" in n)
    assert calls / (WAITERS - 1) <= CALLS_PER_GRANT, f"{calls / (WAITERS - 1):.2f} calls per grant"
