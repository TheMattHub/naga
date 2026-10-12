"""The resource wait queue inserts in order instead of re-sorting (performance
report Q1): same grant order as stock SimPy, linear cost in the queue length."""
import random
import time

import simpy

from simpy.resources.resource import SortedQueue

from nagasim.core.resource_manager import (LoggedSimpyResource, _InsortQueue, _RerankResource,
                                           _Resource)


def _grant_order(resource_cls, arrivals):
    """Grant order of requests on a capacity-1 resource. *arrivals* is a list of
    (time, priority); request i is the i-th entry."""
    env = simpy.Environment()
    resource = resource_cls(env, capacity=1)
    order = []

    def user(i, at, priority):
        yield env.timeout(at)
        with resource.request(priority=priority, preempt=False) as request:
            yield request
            order.append(i)
            yield env.timeout(1.0)

    for i, (at, priority) in enumerate(arrivals):
        env.process(user(i, at, priority))
    env.run()
    return order


def test_grant_order_matches_stock_simpy_with_ties():
    rng = random.Random(0)
    for _ in range(50):
        # few distinct priorities and times, so equal keys are common
        arrivals = [(rng.choice((0.0, 0.0, 0.5, 2.0)), rng.choice((0, 1, 1, 2))) for _ in range(40)]
        assert _grant_order(_Resource, arrivals) == _grant_order(simpy.PreemptiveResource, arrivals)


def test_equal_requests_at_one_instant_are_served_first_come():
    arrivals = [(0.0, 1)] * 6
    assert _grant_order(_Resource, arrivals) == [0, 1, 2, 3, 4, 5]


def test_every_plain_resource_uses_the_insert_queue():
    env = simpy.Environment()
    resource = LoggedSimpyResource(env, capacity=1, name="R")
    assert isinstance(resource._core, _Resource)
    assert isinstance(resource._core.put_queue, _InsortQueue)


def test_a_long_queue_is_not_resorted_on_every_request():
    """20,000 waiters. Re-sorting on each request is about 2e8 key calls (tens
    of seconds); inserting is well under a second. The budget is ~10x local time
    for CI runners."""
    env = simpy.Environment()
    resource = _Resource(env, capacity=1)
    rng = random.Random(1)
    started = time.perf_counter()
    for _ in range(20_000):
        resource.request(priority=rng.randrange(1000), preempt=False)
    elapsed = time.perf_counter() - started
    keys = [request.key for request in resource.put_queue]
    assert keys == sorted(keys)
    assert elapsed < 10.0


def _rerank_order(resource_cls, arrivals):
    """Grant order on a capacity-1 reranking resource whose ranks drift with
    time. *arrivals* is a list of (time, slope, base); the rank is slope*now+base."""
    env = simpy.Environment()
    resource = resource_cls(env, capacity=1)
    resource.rank_of = lambda scope: scope[0] * env.now + scope[1]
    order = []

    def user(i, at, slope, base):
        yield env.timeout(at)
        request = resource.request(priority=(0, base), preempt=False)
        request.rerank_scope = (slope, base)
        yield request
        order.append(i)
        yield env.timeout(1.0)
        resource.release(request)

    for i, (at, slope, base) in enumerate(arrivals):
        env.process(user(i, at, slope, base))
    env.run()
    return order


def test_a_reranking_resource_grants_as_it_did_on_the_stock_queue():
    """`_RerankResource` rewrites the waiting keys and re-sorts by them before
    each grant, so the list is in key order again when the next request is
    inserted. (`movers._DispatchResource` sorts by another key and keeps the
    stock queue for that reason.)"""
    class _StockRerank(_RerankResource):
        PutQueue = SortedQueue

    rng = random.Random(2)
    for _ in range(50):
        arrivals = [(rng.choice((0.0, 0.5, 1.5, 3.0)), rng.choice((-1.0, 0.0, 2.0)),
                     rng.choice((0.0, 1.0, 1.0, 5.0))) for _ in range(30)]
        assert _rerank_order(_RerankResource, arrivals) == _rerank_order(_StockRerank, arrivals)
