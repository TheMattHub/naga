"""PHASE6 A5: agent links and network generators (D103). Every expected
value is hand derived or independent of the engine: Erdos-Renyi rebuilt from
the documented stream, Watts-Strogatz at beta = 0 as the exact ring lattice,
Barabasi-Albert's link count m * (n - m) and degree floor, LINKED/LINK_MEAN
against a brute-force scan of the link list, delivery counts, and an SIR on a
ring whose deterministic infection times are the ring distances.
"""
import math

import numpy as np
import pytest

from nagasim.core import agents as A
from conftest import agent_rng, agent_seed, problem_codes, run_env
from nagasim.core.base_process import deterministic_hash
from nagasim.core.playback import Timeline
from nagasim.core.schema import migrate
from nagasim.core.trace import TraceRecorder


def _model(*records, time_limit=1.5, tables=None):
    layout = {"blocks": [], "links": [], "simulation": {"time_limit": time_limit}}
    if tables:
        layout["tables"] = tables
    return A.add(layout, *records)


def _run(layout, seed=1, recorder=None):
    subscribers = (recorder,) if recorder is not None else ()
    env, _ = run_env(layout, layout["simulation"]["time_limit"], seed=seed,
                     subscribers=subscribers)
    return env.ctx.agent_system


def _codes(layout):
    return problem_codes(layout)


def _network_stream(seed, pop):
    return np.random.default_rng(agent_seed(seed, pop, deterministic_hash("network")))


def _people(n, network, kind=None, **extra):
    kind = kind or A.AgentType("Person", states=["S"])
    return _run(_model(kind, A.Population("People", "Person", n, network=network, **extra)))


def _edges_table(pairs):
    return {"name": "Edges", "keys": list(range(len(pairs))), "columns": ["from", "to"],
            "values": [list(p) for p in pairs]}


# ---------------------------------------------------------------- generators
@pytest.mark.parametrize("directed", [False, True])
def test_erdos_renyi_is_the_documented_stream(directed):
    n, p, seed = 9, 0.35, 4
    system = _run(_model(A.AgentType("Person", states=["S"]),
                         A.Population("People", "Person", n,
                                      network=A.Network("random", p=p, directed=directed))),
                  seed=seed)
    rng, want = _network_stream(seed, "People"), []
    for i in range(n):
        for j in range(n):
            if (j != i if directed else j > i) and rng.random() < p:
                want.append((i, j))
    assert system.links_of("People") == sorted(want)


def test_mean_degree_is_p_times_n_minus_one():
    n, degree = 7, 3
    system = _people(n, A.Network("random", mean_degree=degree))
    rng = _network_stream(1, "People")
    want = [(i, j) for i in range(n) for j in range(i + 1, n) if rng.random() < degree / (n - 1)]
    assert system.links_of("People") == want


@pytest.mark.parametrize("n, k", [(10, 2), (10, 4), (7, 6)])
def test_watts_strogatz_beta_zero_is_the_ring_lattice(n, k):
    system = _people(n, A.Network("small_world", k=k, beta=0))
    ring = {tuple(sorted((i, (i + d) % n))) for i in range(n) for d in range(1, k // 2 + 1)}
    assert system.links_of("People") == sorted(ring)


def test_watts_strogatz_rewiring_keeps_the_link_count():
    n, k = 40, 4
    links = _people(n, A.Network("small_world", k=k, beta=0.5)).links_of("People")
    ring = {(i, (i + d) % n) for i in range(n) for d in (1, 2)}
    assert len(links) == n * k // 2 == len(set(links))
    assert all(i != j for i, j in links)
    assert set(links) != {tuple(sorted(e)) for e in ring}      # some rewired


def test_barabasi_albert_link_count_and_degrees():
    n, m = 60, 3
    links = _people(n, A.Network("scale_free", m=m)).links_of("People")
    assert len(links) == m * (n - m) == len(set(links))       # no initial links, no repeats
    degree = np.zeros(n, dtype=int)
    earlier = np.zeros(n, dtype=int)            # links to an agent with a lower index
    for i, j in links:
        degree[i] += 1
        degree[j] += 1
        earlier[j] += 1
    assert all(earlier[v] == m for v in range(m, n)) and not earlier[:m].any()
    assert degree[m:].min() >= m and degree.sum() == 2 * m * (n - m)
    assert degree.max() > 2 * m                 # a hub: preferential attachment


def test_table_network_directed_and_is_linked():
    kind = A.AgentType("Person", attributes=["out", "to2", "to0"], states=["S", "T"],
                       transitions=[A.Transition("S", "T", "timeout", "0", actions=[
                           A.Action.set("self.out", 'LINKED("People")'),
                           A.Action.set("self.to2", 'IS_LINKED(AGENT("People", 2))'),
                           A.Action.set("self.to0", 'IS_LINKED(AGENT("People", 0))')])])
    layout = _model(kind, A.Population("People", "Person", 4,
                                       network=A.Network("table", table="Edges", directed=True)),
                    tables=[_edges_table([(0, 2), (1, 2), (2, 0), (3, 3), (0, 2)])])
    pop = _run(layout).by_name["People"]
    assert pop.system.links_of("People") == [(0, 2), (1, 2), (2, 0)]    # no self-loop, no repeat
    assert list(pop.attrs["out"]) == [1, 1, 1, 0]
    assert list(pop.attrs["to2"]) == [1, 1, 0, 0] and list(pop.attrs["to0"]) == [0, 0, 1, 0]


# ---------------------------------------------------------------- expressions
PROBES = {"c": 'LINKED("People")', "ci": 'LINKED("People", "I")', "s": 'LINK_SUM("v")',
          "m": 'LINK_MEAN("v", "I")'}


def test_linked_and_link_aggregates_equal_brute_force():
    n = 30
    kind = A.AgentType("Person", attributes=[A.Attribute("v", "UNIF(0, 10)")]
                       + [A.Attribute(k, "-1") for k in PROBES], states=["S", "I"],
                       tick=A.Tick(1), on_tick=[A.Action.set(f"self.{k}", text)
                                                for k, text in PROBES.items()])
    pop = _run(_model(kind, A.Population("People", "Person", n, {"I": 12},
                                         network=A.Network("random", p=0.2)))).by_name["People"]
    nbrs = {i: [] for i in range(n)}
    for i, j in pop.system.links_of("People"):
        nbrs[i].append(j)
        nbrs[j].append(i)
    v, infected = pop.attrs["v"], pop.state == 1
    for i in range(n):
        sick = [j for j in nbrs[i] if infected[j]]
        assert pop.attrs["c"][i] == len(nbrs[i]) and pop.attrs["ci"][i] == len(sick)
        assert pop.attrs["s"][i] == pytest.approx(sum(v[j] for j in nbrs[i]))
        assert pop.attrs["m"][i] == pytest.approx(np.mean([v[j] for j in sick]) if sick else 0)


def test_synchronous_tick_reads_the_tick_start_links():
    # Everyone unlinks all its links, then counts: the tick-start links count.
    kind = A.AgentType("Person", attributes=["c"], states=["S"], tick=A.Tick(1),
                       on_tick=[A.Action.unlink("links"), A.Action.set("self.c", 'LINKED("People")')])
    pop = _people(6, A.Network("small_world", k=2), kind=kind).by_name["People"]
    assert list(pop.attrs["c"]) == [2] * 6 and pop.system.links_of("People") == []


# ---------------------------------------------------------------- sends and link actions
def test_send_to_links_delivers_once_per_neighbour():
    kind = A.AgentType(
        "Person", attributes=["ping", "pong", "rnd"], states=["S", "Done"],
        transitions=[
            A.Transition("S", "Done", "timeout", "0", guard="self.id == 0", actions=[
                A.Action.send("ping", "links"),
                A.Action.send("pong", {"links": True, "where": "self.id >= 3"}),
                A.Action.send("rnd", {"links": True, "random": True})]),
            *(A.Transition("S", "S", "message", name,
                           actions=[A.Action.set(f"self.{name}", f"self.{name} + 1")])
              for name in ("ping", "pong", "rnd"))])
    layout = _model(kind, A.Population("People", "Person", 6,
                                       network=A.Network("table", table="Edges")),
                    tables=[_edges_table([(0, 1), (3, 0), (0, 5), (2, 3)])])
    pop = _run(layout, seed=3).by_name["People"]
    assert list(pop.attrs["ping"]) == [0, 1, 0, 1, 0, 1]
    assert list(pop.attrs["pong"]) == [0, 0, 0, 1, 0, 1]
    # One of agent 0's neighbours [1, 3, 5], drawn from agent 0's own stream.
    pick = [1, 3, 5][int(agent_rng(3, "People", 0).integers(3))]
    assert list(pop.attrs["rnd"]) == [int(i == pick) for i in range(6)]
    assert pop.system.dropped == 0


def test_link_and_unlink_at_run_time():
    kind = A.AgentType(
        "Person", attributes=["c1", "c2"], states=["S", "A", "B"],
        transitions=[
            A.Transition("S", "A", "timeout", "1", guard="self.id == 0", actions=[
                A.Action.link('AGENT("People", 2)'), A.Action.link(3), A.Action.link("self"),
                A.Action.set("self.c1", 'LINKED("People")')]),
            A.Transition("A", "B", "timeout", "1", actions=[
                A.Action.unlink('AGENT("People", 2)'), A.Action.unlink(1),
                A.Action.set("self.c2", 'LINKED("People")')])])
    pop = _run(_model(kind, A.Population("People", "Person", 4), time_limit=3)).by_name["People"]
    assert pop.attrs["c1"][0] == 2 and pop.attrs["c2"][0] == 1      # self never links
    assert pop.system.links_of("People") == [(0, 3)]


@pytest.mark.parametrize("time_limit, on_spawn, extra", [
    (1.5, True, []), (2.5, False, []), (2.5, True, [(0, 2)])])
def test_death_drops_incident_links_and_a_newborn_starts_unlinked(time_limit, on_spawn, extra):
    # Agent 2 dies at t=1; agent 0 spawns at t=2 into the freed slot 2.
    kind = A.AgentType("Person", states=["S", "T"], transitions=[
        A.Transition("S", "T", "timeout", "1", guard="self.id == 2",
                     actions=[A.Action.remove("self")]),
        A.Transition("S", "T", "timeout", "2", guard="self.id == 0",
                     actions=[A.Action.spawn("People")])])
    net = A.Network("small_world", k=2, link_on_spawn=on_spawn)
    system = _run(_model(kind, A.Population("People", "Person", 6, network=net),
                         time_limit=time_limit))
    ring = [(0, 1), (0, 5), (1, 2), (2, 3), (3, 4), (4, 5)]
    assert system.links_of("People") == sorted([e for e in ring if 2 not in e] + extra)


@pytest.mark.parametrize("k", [2, 4])
def test_sir_on_a_ring_spreads_along_links_only(k):
    # I -> R after 1, telling its links; an S told turns I at once. Agent i
    # is infected at ceil(ring distance / (k / 2)): the links, nothing else.
    n = 20
    kind = A.AgentType("Person", attributes=[A.Attribute("t_inf", "-1")], states=["S", "I", "R"],
                       transitions=[
                           A.Transition("S", "I", "message", "infect",
                                        actions=[A.Action.set("self.t_inf", "TNOW")]),
                           A.Transition("I", "R", "timeout", "1",
                                        actions=[A.Action.send("infect", "links")])])
    pop = _run(_model(kind, A.Population("People", "Person", n, {"I": 1},
                                         network=A.Network("small_world", k=k)),
                      time_limit=n)).by_name["People"]
    want = [-1] + [math.ceil(min(i, n - i) / (k // 2)) for i in range(1, n)]
    assert list(pop.attrs["t_inf"]) == want
    assert pop.counts == [0, 0, n]


def test_no_network_no_spread():
    kind = A.AgentType("Person", states=["S", "I", "R"], transitions=[
        A.Transition("S", "I", "message", "infect"),
        A.Transition("I", "R", "timeout", "1", actions=[A.Action.send("infect", "links")])])
    pop = _run(_model(kind, A.Population("People", "Person", 5, {"I": 1}),
                      time_limit=3)).by_name["People"]
    assert pop.counts == [4, 0, 1]


# ---------------------------------------------------------------- playback
def test_traced_links_reach_the_timeline():
    kind = A.AgentType("Person", states=["S", "T"], space=A.Space("continuous", [0, 0, 10, 10]),
                       transitions=[A.Transition("S", "T", "timeout", "1", guard="self.id == 1",
                                                 actions=[A.Action.remove("self")])])
    recorder = TraceRecorder()
    _run(_model(kind, A.Population("People", "Person", 4,
                                   network=A.Network("small_world", k=2)), time_limit=2),
         recorder=recorder)
    timeline = Timeline(recorder.events)
    ring = [("People", i, j) for i, j in [(0, 1), (0, 3), (1, 2), (2, 3)]]
    assert timeline.state_at(0.5)["agent_links"] == ring
    assert timeline.state_at(1.5)["agent_links"] == [("People", 0, 3), ("People", 2, 3)]


# ---------------------------------------------------------------- validation and API
def _with_network(network, count=10, tables=None):
    return _model(A.AgentType("Person", states=["S"]),
                  A.Population("People", "Person", count, network=network), tables=tables)


@pytest.mark.parametrize("network", [
    {"kind": "lattice"}, {"kind": "random"}, {"kind": "random", "p": 0.1, "mean_degree": 2},
    {"kind": "random", "p": 1.5}, {"kind": "random", "mean_degree": 10},
    {"kind": "small_world", "k": 3}, {"kind": "small_world", "k": 10},
    {"kind": "small_world", "k": 2, "beta": 2}, {"kind": "small_world", "k": 2, "directed": True},
    {"kind": "scale_free", "m": 0}, {"kind": "scale_free", "m": 10},
    {"kind": "table", "table": "Nope"}, {"kind": "none", "p": 0.1},
    {"kind": "random", "p": 0.1, "directed": "yes"}])
def test_bad_networks_are_refused(network):
    assert "E_AGENT_NETWORK" in _codes(_with_network(network))


def test_good_networks_validate():
    tables = [_edges_table([(0, 1)])]
    for network in ({"kind": "random", "mean_degree": 9}, {"kind": "small_world", "k": 8},
                    {"kind": "scale_free", "m": 9, "link_on_spawn": True},
                    {"kind": "table", "table": "Edges", "directed": True}, {"kind": "none"}):
        assert not _codes(_with_network(network, tables=tables))


@pytest.mark.parametrize("text, code", [
    ('LINK_SUM("nope")', "E_AGENT_ATTRIBUTE"), ('LINK_MEAN("c", "Nope")', "E_AGENT_STATE"),
    ('LINK_SUM("colour")', "E_AGENT_ATTRIBUTE"), ('LINKED("Nobody")', "E_POPULATION_UNKNOWN"),
    ('LINKED("People", "Nope")', "E_AGENT_STATE")])
def test_bad_link_reads_are_refused(text, code):
    kind = A.AgentType("Person", attributes=["c", A.Attribute("colour", '"red"', "category",
                                                             ["red", "blue"])],
                       states=["S", "T"],
                       transitions=[A.Transition("S", "T", "timeout", "0",
                                                 actions=[A.Action.set("self.c", text)])])
    assert code in _codes(_model(kind, A.Population("People", "Person", 3)))


def test_link_reads_and_targets_in_the_wrong_place():
    rate = A.AgentType("Person", states=["S", "I"],
                       transitions=[A.Transition("S", "I", "rate", '0.1 * LINKED("People", "I")')])
    assert "E_AGENT_RATE_FORM" in _codes(_model(rate, A.Population("People", "Person", 3)))
    bad = A.AgentType("Person", states=["S"], transitions=[A.Transition("S", "S", "timeout", "1",
                      actions=[A.Action.link(True), A.Action.send("x", {"links": False})])])
    codes = _codes(_model(bad, A.Population("People", "Person", 3)))
    assert {"E_AGENT_ACTION", "E_SEND_TARGET"} <= codes
    block = _model(A.AgentType("Person", states=["S"]), A.Population("People", "Person", 3))
    block["blocks"] = [{"name": "Tell", "type": "SEND",
                        "params": {"message": "x", "to": "links"}}]
    assert "E_SEND_TARGET" in _codes(block)


def test_python_api_round_trip():
    pop = A.Population("People", "Person", 5, network=A.Network("small_world", k=2, beta=0.1))
    assert pop.to_dict()["network"] == {"kind": "small_world", "k": 2, "beta": 0.1}
    assert A.Population.from_dict(pop.to_dict()) == pop
    assert A.Action.link("links").to_dict() == {"link": "links"}
    assert A.Action.unlink(3).to_dict() == {"unlink": 3}


def test_the_json_schema_accepts_networks_and_link_actions():
    jsonschema = pytest.importorskip("jsonschema")
    from nagasim.modelschema import build_json_schema
    kind = A.AgentType("Person", states=["S"], transitions=[A.Transition(
        "S", "S", "timeout", "1", actions=[
            A.Action.link({"links": True, "random": True}), A.Action.unlink("links"),
            A.Action.send("m", {"links": True, "where": "self.id > 0"})])])
    layout = migrate(_model(kind, A.Population("People", "Person", 5, network=A.Network(
        "small_world", k=2, beta=0.1, link_on_spawn=True))))
    assert not list(jsonschema.Draft202012Validator(build_json_schema()).iter_errors(layout))
    assert not _codes(layout)
