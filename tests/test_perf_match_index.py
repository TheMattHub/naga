"""MATCH tie order (performance report 2026-10-07, M1).

The rule the per-type index must keep: the oldest waiting entity of each type,
a tie in entry time going to the lower store index and then the earlier
position; types ordered by how long their oldest has waited, a tie going to
the type met first when the stores are read in order. `ScanMatch` is the scan
the index replaced, kept as the oracle.
"""
import random

import pytest

from nagasim.api import run_model
from nagasim.core.base_process import Block
from nagasim.core.blocks import Match
from nagasim.core.sim_classes import Entity
from nagasim.core.sim_runner import initialize_sim_env

from conftest import chain, create, dispose, layout


class ScanMatch(Match):
    """Match as it was before the index: every arrival scans every stored item."""

    def _store_index(self, entity):
        if entity.e_type in self.type_store_map:
            return self.type_store_map[entity.e_type]
        for i, store in enumerate(self.stores):
            if not store.items:
                return i
        for i, store in enumerate(self.stores):
            if any(e_type == entity.e_type for e_type, _ in store.items):
                return i
        return min(range(len(self.stores)), key=lambda i: len(self.stores[i].items))

    def _oldest_per_type(self, value=None):
        oldest = {}
        for store in self.stores:
            for e_type, ent in store.items:
                if self.match_on and self._key(ent) != value:
                    continue
                entered = store._entry_times.get(ent.id, self.env.now)
                if e_type not in oldest or entered < oldest[e_type][0]:
                    oldest[e_type] = (entered, store, ent)
        return [(e_type, store, ent)
                for e_type, (_, store, ent) in sorted(oldest.items(), key=lambda kv: kv[1][0])]

    def self_process(self, entity):
        idx = self._store_index(entity)
        yield self.stores[idx].put((entity.e_type, entity))
        if entity.id in self.stores[idx]._entry_times:      # the log rule, as in Match
            self.stores[idx]._log_state(event_type="store")
        waiting = self._oldest_per_type(self._key(entity) if self.match_on else None)
        if len(waiting) < self.number_to_match:
            return
        for _, store, ent in waiting[:self.number_to_match]:
            for item in store.take(1, lambda item, e=ent: item[1] is e):
                self.forward(item[1], 0)


class _Recorder:
    def __init__(self, env):
        self.env, self.seen = env, []

    def self_run(self, entity):
        self.seen.append((self.env.now, entity.tag))


def _play(cls, script, number_to_match, type_store_map=None, match_on=""):
    """Feed *script*, a list of (time, e_type, key), entities at one time
    arriving in the same instant in list order. Returns what left, as
    [(time, tag)] with tag = position in the script, what is still waiting per
    store, and each store's statistics."""
    env = initialize_sim_env(seed=0)
    match = cls(env, Block(position=(0, 0), type="MATCH", name="m"), number_to_match)
    match.type_store_map = dict(type_store_map or {})
    match.match_on = match_on
    match.stores = [env.ctx.store_manager.register(f"m_match_store_{i}")
                    for i in range(number_to_match)]
    recorder = _Recorder(env)
    match.next_process = [recorder]

    def driver():
        for tag, (at, e_type, key) in enumerate(script):
            if at > env.now:
                yield env.timeout(at - env.now)
            entity = Entity()
            entity.e_type, entity.key, entity.tag = e_type, key, tag
            match.self_run(entity)

    env.process(driver())
    env.run(until=max(at for at, _, _ in script) + 1)
    waiting = [[ent.tag for _, ent in store.items] for store in match.stores]
    stats = [(s.residence.count, s.residence.mean, s.log_events, len(s._entry_times))
             for s in match.stores]
    return recorder.seen, waiting, stats


def _left(script, number_to_match, **kw):
    seen, waiting, _ = _play(Match, script, number_to_match, **kw)
    return [tag for _, tag in seen], waiting


def test_a_tie_across_stores_goes_to_the_type_met_first_in_store_order():
    """A, B, C in one instant, two stores: A -> 0, B -> 1 (the empty one),
    C -> 0 (least loaded). All three have waited equally long; read in store
    order the types are A, C, B, so A and C leave and B waits."""
    assert _left([(0, "A", 0), (0, "B", 0), (0, "C", 0)], 2) == ([0, 2], [[], [1]])


def test_a_tie_within_a_type_goes_to_the_lower_store():
    """A, B, A, C in one instant, three stores: the second A takes the empty
    store 2, C joins store 0. The two As tie, and store 0's leaves."""
    assert (_left([(0, "A", 0), (0, "B", 0), (0, "A", 0), (0, "C", 0)], 3)
            == ([0, 3, 1], [[], [], [2]]))


def test_the_older_entity_wins_from_a_higher_store():
    """After the first match store 2 still holds the A of t=1; the A of t=3
    lands in the emptied store 0. The older one leaves although its store
    comes later."""
    script = [(0, "A", 0), (0, "B", 0), (1, "A", 0), (2, "C", 0),      # match at 2: tags 0, 1, 3
              (3, "A", 0), (4, "B", 0), (5, "C", 0)]                  # match at 5: tags 2, 5, 6
    assert _left(script, 3) == ([0, 1, 3, 2, 5, 6], [[4], [], []])


def test_a_tie_within_one_store_goes_to_the_earlier_position():
    """Two As in one instant in the same store: the first put leaves first."""
    assert (_left([(0, "A", 0), (0, "A", 0), (1, "B", 0), (2, "B", 0)], 2,
                  type_store_map={"A": 0, "B": 1}) == ([0, 2, 1, 3], [[], []]))


def test_a_match_takes_from_the_middle_of_a_store():
    """Store 0 holds A, A, C, A when B arrives: the first A and the C leave,
    the C from the middle; the next match still takes the oldest A left."""
    script = [(0, "A", 0), (1, "A", 0), (2, "C", 0), (3, "A", 0), (4, "B", 0),
              (5, "C", 0), (6, "B", 0)]
    assert (_left(script, 3, type_store_map={"A": 0, "C": 0, "B": 1})
            == ([0, 2, 4, 1, 5, 6], [[3], [], []]))


def test_match_on_takes_the_equal_value_from_the_middle_fifo_within_a_value():
    """As with key 1, 2, 2, 3 wait; B(2) takes the first 2 (the middle of the
    store), B(3) the last, B(2) the other 2, and B(9) waits."""
    script = [(0, "A", 1), (0, "A", 2), (0, "A", 2), (1, "A", 3),
              (2, "B", 2), (3, "B", 3), (4, "B", 2), (5, "B", 9)]
    assert (_left(script, 2, type_store_map={"A": 0, "B": 1}, match_on="key")
            == ([1, 4, 3, 5, 2, 6], [[0], [7]]))


def test_match_on_ties_follow_the_same_rule():
    """One instant, key 1 everywhere except the first A. A(2) -> store 0,
    B -> store 1, A(1) -> store 0 (it holds A), C -> store 1 (least loaded):
    among the key-1 entities the types read in store order are A, B, C."""
    script = [(0, "A", 2), (0, "B", 1), (0, "A", 1), (0, "C", 1)]
    assert _left(script, 2, match_on="key") == ([2, 1], [[0], [3]])


@pytest.mark.parametrize("match_on", ["", "key"])
def test_the_index_matches_the_scan_on_random_arrivals(match_on):
    """Coarse times (many ties, several arrivals per instant), several types,
    with and without a type_store_map: what leaves, when, in which order, what
    stays in each store and every store statistic equal the scan's."""
    rnd = random.Random(1007 if match_on else 1008)
    for case in range(300):
        number_to_match = rnd.choice([2, 2, 3, 4])
        types = "ABCDE"[:rnd.randint(number_to_match, 5)]
        mapping = ({t: rnd.randrange(number_to_match) for t in types if rnd.random() < 0.7}
                   if rnd.random() < 0.5 else {})
        weights = [rnd.choice([1, 1, 3, 6]) for _ in types]     # unbalanced: queues build up
        script = sorted(((rnd.randint(0, 12), rnd.choices(types, weights)[0], rnd.randint(1, 3))
                         for _ in range(rnd.randint(1, 60))), key=lambda arrival: arrival[0])
        args = (script, number_to_match, mapping, match_on)
        assert _play(Match, *args) == _play(ScanMatch, *args), (case, args)


def test_entities_injected_by_an_initial_state_are_matched_like_arrivals():
    """A warm start hands its entities to MATCH through the block's own entry,
    so they wait and leave FIFO like any other: the two As of the snapshot
    leave with the first two Bs."""
    b_in = create("b_in", e_type="B", start_time=5, max_arrivals=3)
    match = {"name": "pair", "description": "pair", "type": "MATCH", "position": [1, 0],
             "params": {"number_to_match": 2, "batch_option": False}}
    out = dispose("out")
    model = layout([b_in, match, out], chain(b_in, match, out),
                   initial_state={"entities": [{"block": "pair", "count": 2, "e_type": "A"}]})
    log = run_model(model, replications=1, run_length=50, warmup=0)["entity_log"]
    assert sorted(log[log["Type"] == "A"]["Exited At"]) == [5.0, 6.0]
    assert sorted(log[log["Type"] == "B"]["Exited At"].dropna()) == [5.0, 6.0]


def test_an_arrival_already_matched_away_writes_no_store_row():
    """A and B arrive in one instant and match. B resumes after A has taken it
    out of its store, so it is not waiting there and logs no "store" row (the
    same resume that armed BATCH's stale timer, DEVLOG #343). The second A,
    left waiting, does."""
    env = initialize_sim_env(seed=0)
    match = Match(env, Block(position=(0, 0), type="MATCH", name="m"), 2)
    match.stores = [env.ctx.store_manager.register(f"m_match_store_{i}") for i in range(2)]
    match.next_process = [_Recorder(env)]
    for tag, e_type in enumerate("ABA"):
        entity = Entity()
        entity.e_type, entity.key, entity.tag = e_type, 0, tag
        match.self_run(entity)
    env.run()
    rows = [[(snap.items, snap.event_type) for snap in store.log] for store in match.stores]
    # store 0: A in, A in, first A waits, matched out, second A waits; store 1: B in, matched out
    assert rows == [[(1, "put"), (2, "put"), (2, "store"), (1, "take"), (1, "store")],
                    [(1, "put"), (0, "take")]]
