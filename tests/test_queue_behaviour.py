"""Queue behaviours: balking, reneging, preemption (PHASE4 A4).

Two levels, as in test_tables.py / test_sequences.py / test_orders.py: exact
hand-computed timelines for the mechanics (the tie rule, resume vs restart,
costing, no leaked requests, validation codes) and closed-form oracles for the
stochastic behaviour (M/M/1/K for balking, M/M/1+M for reneging); preemptive
priority is an exact identity (the high class never sees the low one).
"""
import math

import pytest

from nagasim import experiments
from nagasim.api import run_model
from nagasim.core.resource_manager import ResourceSet
from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.trace import TraceRecorder
from nagasim.core.stats import ci95

from conftest import (chain, create, delay, dispose, layout, metric_value, problem_codes, process,
                      resource_row, run_layout)


def _queue_row(results, block="serve"):
    frame = results["queue_summary"]
    return frame[frame["Block"] == block].iloc[0]


# ---------------------------------------------------------------- exact mechanics
def test_balking_and_reneging_are_exact_and_leave_by_the_second_output():
    """Arrivals every minute, service 5 minutes, capacity 1.

    With `balk_if` "queue_length >= 1" only the first two arrivals get in (one
    in service, one waiting) and every later one balks while the queue is full.

    With `renege_after` 3 every waiting entity gives up after exactly 3
    minutes -- except the one that arrived at t=2, whose patience runs out at
    t=5, the very instant the first entity releases the desk: the tie rule
    gives it the service (D54), so 4 of the 6 arrivals give up, not 5.
    """
    c = create("arrive", distribution="Constant", dist_params={"value": 1.0}, max_arrivals=6)
    out, left = dispose("out"), dispose("left")

    balk = layout([c, process("serve", resource="Desk", value=5.0, balk_if="queue_length >= 1"),
                   out, left],
                  chain(c, {"name": "serve"}, out) + [{"source": "serve", "target": "left"}],
                  resources=[{"name": "Desk", "capacity": 1}])
    results = run_model(balk, replications=1, run_length=100, warmup=0)
    row = _queue_row(results)
    # t=0 seizes, t=1 queues, t=2..5 balk (4 of them): 6 arrivals, 2 served.
    assert row["Balked"] == 4 and row["Reneged"] == 0
    assert math.isnan(row["Avg Wait Before Renege"])
    # Everything that balked really left by the second output, not by the first.
    assert results["entity_summary"].set_index("Entity Type").loc["DEFAULT", "Total Completed"] == 6

    renege = layout([c, process("serve", resource="Desk", value=5.0, renege_after="3"),
                     out, left],
                    chain(c, {"name": "serve"}, out) + [{"source": "serve", "target": "left"}],
                    resources=[{"name": "Desk", "capacity": 1}])
    results = run_model(renege, replications=1, run_length=100, warmup=0)
    row = _queue_row(results)
    assert row["Reneged"] == 4 and row["Balked"] == 0
    assert row["Avg Wait Before Renege"] == 3.0 and row["Max Wait Before Renege"] == 3.0
    # One KPI key per count, so a scenario comparison can name them.
    flat = experiments.flatten_kpis(results).set_index("KPI")["Value"]
    assert flat["queue/serve/Reneged"] == 4.0


def test_both_outputs_of_a_queueing_block_may_end_at_the_same_dispose():
    """PHASE4 T4 (D77): the reneging exit and the normal one often go to the
    same place. Two parallel links express that directly -- no merge hop -- and
    branch 1 is still the one that leaves without service."""
    c = create("arrive", distribution="Constant", dist_params={"value": 1.0}, max_arrivals=6)
    out = dispose("out")
    model = layout([c, process("serve", resource="Desk", value=5.0, renege_after="3"), out],
                   chain(c, {"name": "serve"}, out) + [{"source": "serve", "target": "out"}],
                   resources=[{"name": "Desk", "capacity": 1}])
    trace = TraceRecorder()
    results = run_model(model, replications=1, run_length=100, warmup=0, trace=trace)

    assert _queue_row(results)["Reneged"] == 4
    branches = [e.branch for e in trace.events
                if e.kind == "entity_moved" and e.block == "serve"]
    assert branches.count(1) == 4 and branches.count(0) == 2


@pytest.mark.parametrize("join, patience", [(1.0, 4.0), (4.0, 1.0)])
def test_a_grant_in_the_same_instant_beats_the_patience(join, patience):
    """The tie rule (ARCHITECTURE D54): the second entity's patience runs out
    at t=5, the exact instant the first entity releases the desk. The grant
    wins, in BOTH orders SimPy can process the two events in -- the release
    event is older than the patience timer in the first case and younger in
    the second.
    """
    first = create("first", distribution="Constant", dist_params={"value": 100.0}, max_arrivals=1)
    second = create("second", distribution="Constant", dist_params={"value": 100.0},
                    max_arrivals=1, start_time=int(join))
    second["position"] = [0, 1]
    hold = process("hold", resource="Desk", value=5.0)
    serve = process("serve", resource="Desk", value=1.0, renege_after=str(patience))
    out, left = dispose("out"), dispose("left")
    model = layout([first, second, hold, serve, out, left],
                   [{"source": "first", "target": "hold"}, {"source": "second", "target": "serve"},
                    {"source": "hold", "target": "out"}, {"source": "serve", "target": "out"},
                    {"source": "serve", "target": "left"}],
                   resources=[{"name": "Desk", "capacity": 1}])
    results = run_model(model, replications=1, run_length=50, warmup=0)
    assert _queue_row(results)["Reneged"] == 0
    # Granted at 5, served for 1: it really was served, not silently dropped.
    log = results["entity_log"]
    assert sorted(log["Exited At"]) == [5.0, 6.0]


@pytest.mark.parametrize("mode, exit_time, busy_cost", [("resume", 14.0, 14.0), ("restart", 17.0, 17.0)])
def test_preemption_resume_and_restart_are_exact(mode, exit_time, busy_cost):
    """A 10-minute job starts at t=0; a priority job arrives at t=3, preempts
    it and runs 4 minutes.

        resume   low 0-3, high 3-7, low 7-14 (7 minutes left)
        restart  low 0-3, high 3-7, low 7-17 (the whole 10 again)

    Busy time and therefore busy cost are exactly the time units held, and
    `cost_per_use` is charged once per JOB: two seizes, not three.
    """
    low_in = create("low_in", distribution="Constant", dist_params={"value": 100.0},
                    max_arrivals=1, e_type="Low")
    high_in = create("high_in", distribution="Constant", dist_params={"value": 100.0},
                     max_arrivals=1, start_time=3, e_type="High")
    high_in["position"] = [0, 1]
    low = process("low", resource="M", value=10.0, priority=5, on_preempt=mode)
    high = process("high", resource="M", value=4.0, priority=1, preempt=True)
    high["position"] = [1, 1]
    out = dispose("out")
    model = layout([low_in, high_in, low, high, out],
                   [{"source": "low_in", "target": "low"}, {"source": "high_in", "target": "high"},
                    {"source": "low", "target": "out"}, {"source": "high", "target": "out"}],
                   resources=[{"name": "M", "capacity": 1, "cost_busy": 60.0, "cost_per_use": 1.0,
                               "cost_time_unit": "hours"}],
                   simulation={"time_limit": 100, "warmup": 0, "replications": 1,
                               "time_unit": "minutes"})
    trace = TraceRecorder()
    results = run_model(model, replications=1, run_length=100, warmup=0, trace=trace)

    # Animation and playback need nothing new (PHASE4 A4 item 7): a preempted
    # entity simply gives the resource back and seizes it again later, and a
    # balking/reneging one leaves by an ordinary entity_moved.
    low_id = min(e.entity_id for e in trace.events if e.e_type == "LOW")
    assert [(e.t, e.kind) for e in trace.events
            if e.entity_id == low_id and e.kind in ("entity_seized", "entity_released")] == [
        (0, "entity_seized"), (3.0, "entity_released"),
        (7.0, "entity_seized"), (exit_time, "entity_released")]

    log = results["entity_log"].set_index("Type")
    assert log.loc["LOW", "Exited At"] == exit_time
    assert log.loc["HIGH", "Exited At"] == 7.0
    resource = results["resource_summary"].set_index("Resource").loc["M"]
    assert resource["Total Seizes"] == 2
    assert resource["Usage Cost"] == 2.0
    assert resource["Busy Cost"] == pytest.approx(busy_cost)
    # The interruption is counted on the block whose job was interrupted.
    assert _queue_row(results, "low")["Preempted"] == 1
    assert _queue_row(results, "high")["Preempted"] == 0


@pytest.mark.parametrize("mode, changeovers, setup_time, cost, b_out", [
    ("resume", 1, 5.0, 100.0, 18.0),
    ("restart", 2, 7.0, 200.0, 20.0),
])
def test_an_interrupted_changeover_obeys_on_preempt(mode, changeovers, setup_time, cost, b_out):
    """DEVLOG #105 and ARCHITECTURE D55, with PHASE4 T3's changeover cost and
    PHASE4 T7's resume semantics for the setup phase.

    One machine. Block `work` has a 5-minute, 100-money changeover and starts
    set up for A. A arrives at 0 and runs 0-5 with no changeover. B arrives at
    6 and begins its changeover, 6-11. At t=8 a rush job preempts it two
    minutes in and runs 8-10, so B gets the unit back at 10 with 2 of its 5
    minutes of setup already spent.

        resume   3 minutes of setup left: 10-13, then 5 of service -> out at 18.
                 ONE changeover, 5 minutes of setup, 100 of cost.
        restart  the whole changeover again: 10-15, then service -> out at 20.
                 TWO changeovers, 2 + 5 = 7 minutes and 200 of cost -- the
                 plant really did start the changeover twice.
    """
    a_in = create("a_in", distribution="Constant", dist_params={"value": 100.0},
                  max_arrivals=1, e_type="A")
    b_in = create("b_in", distribution="Constant", dist_params={"value": 100.0},
                  max_arrivals=1, start_time=6, e_type="B")
    b_in["position"] = [0, 1]
    rush_in = create("rush_in", distribution="Constant", dist_params={"value": 100.0},
                     max_arrivals=1, start_time=8, e_type="RUSH")
    rush_in["position"] = [0, 2]
    work = process("work", resource="M", value=5.0, priority=5, on_preempt=mode,
                   setup="5.0", setup_cost="100.0", initial_key="A")
    rush = process("rush", resource="M", value=2.0, priority=1, preempt=True)
    rush["position"] = [1, 2]
    model = layout([a_in, b_in, rush_in, work, rush, dispose("out")],
                   [{"source": "a_in", "target": "work"}, {"source": "b_in", "target": "work"},
                    {"source": "rush_in", "target": "rush"}, {"source": "work", "target": "out"},
                    {"source": "rush", "target": "out"}],
                   resources=[{"name": "M", "capacity": 1}],
                   simulation={"time_limit": 100, "warmup": 0, "replications": 1,
                               "time_unit": "minutes"})
    results = run_model(model, replications=1, run_length=100, warmup=0)

    row = results["queue_summary"].set_index("Block").loc["work"]
    assert row["Changeovers"] == changeovers
    assert row["Setup Time"] == pytest.approx(setup_time)
    assert float(results["cost_summary"].set_index("Item").loc["Changeover work", "Amount"]) \
        == pytest.approx(cost)
    exits = sorted(results["entity_log"]["Exited At"])
    assert exits == pytest.approx([5.0, 10.0, b_out])       # A, the rush job, then B


def test_blockers_never_preempt_and_a_reneging_run_leaks_nothing(env, make_model):
    """A failure takes the resource down while a preempting model runs.

    The failure's blockers outrank every entity but never interrupt one (the
    WAIT rule, ARCHITECTURE D55), so the job in service at the failure keeps
    its unit; and once the run has drained, no request is left anywhere -- no
    queue entry, no uncounted set-member half, no uncharged resume.
    """
    c = create("arrive", distribution="Constant", dist_params={"value": 2.0}, max_arrivals=8)
    urgent = create("urgent", distribution="Constant", dist_params={"value": 7.0},
                    max_arrivals=3, start_time=3, e_type="Urgent")
    urgent["position"] = [0, 1]
    serve = process("serve", resource="M", value=3.0, priority=5, renege_after="4")
    rush = process("rush", resource="M", value=1.0, priority=1, preempt=True)
    rush["position"] = [1, 1]
    out, left = dispose("out"), dispose("left")
    path = make_model(
        [c, urgent, serve, rush, out, left],
        [["arrive", "serve"], ["urgent", "rush"], ["serve", "out"], ["rush", "out"],
         ["serve", "left"]],
        resources=[{"name": "M", "capacity": 1}],
        failures=[{"name": "breakdown", "resources": ["M"],
                   "uptime": {"distribution": "Constant", "dist_params": {"value": 5.0}},
                   "downtime": {"distribution": "Constant", "dist_params": {"value": 2.0}}}])
    run_layout(env, path, run_len=60)

    resource = env.ctx.resource_manager.get("M")
    assert resource.waiting == 0 and resource.in_service == 0
    assert resource._enqueued == {} and resource._uncounted == set()
    assert resource._uncharged == set()
    # Only blockers may still hold units at the end (the failure's, if it is
    # down when the run stops); no entity request survives the run.
    assert resource.count == len(resource._blockers)
    assert not any(hasattr(e, "req_params") for e in env.ctx.live_entities)
    served = [p for p in env.ctx.process_list if p.name == "serve"][0]
    assert served.reneged_count > 0, "the model must actually exercise reneging"


def test_reneging_works_with_a_resource_set_a_schedule_and_the_warm_up(make_model, env):
    """One entity reneges off a two-member SET while both members are off
    shift, so its request was pending on every member; the set's own queue
    collector comes back to zero, and a warm-up reset zeroes the counts it
    collected before the cut-off.
    """
    c = create("arrive", distribution="Constant", dist_params={"value": 1.0}, max_arrivals=8)
    serve = process("serve", resource="Team", value=4.0, renege_after="1")
    out, left = dispose("out"), dispose("left")
    path = make_model([c, serve, out, left],
                      [["arrive", "serve"], ["serve", "out"], ["serve", "left"]],
                      resources=[{"name": "A", "capacity": 1, "capacity_schedule": "shift"},
                                 {"name": "B", "capacity": 1}],
                      schedules=[{"name": "shift", "kind": "capacity",
                                  "points": [[3.0, 0], [100.0, 1]]}])
    layout_with_set = path
    import json
    doc = json.loads(open(path).read())
    doc["resource_sets"] = [{"name": "Team", "members": ["A", "B"], "rule": "PreferredOrder"}]
    open(path, "w").write(json.dumps(doc))

    run_layout(env, layout_with_set, run_len=40)
    team = env.ctx.resource_manager.resolve("Team")
    assert isinstance(team, ResourceSet)
    assert team.queue_len.level == 0 and team._open == []
    for member in team.members:
        assert member._enqueued == {} and member._uncounted == set()

    served = [p for p in env.ctx.process_list if p.name == "serve"][0]
    assert served.reneged_count > 0
    served.reset_queue_stats()      # what sim_runner does at the warm-up cut-off
    assert served.reneged_count == 0 and served.renege_wait.count == 0


# ---------------------------------------------------------------- validation
def _codes(model):
    return problem_codes(model, severity=None)


def test_queue_behaviour_validation_codes():
    """Every new code, on one model each: they are the contract an editor and
    an agent branch on."""
    assert "E_QUEUE_NO_LEAVE_LINK" in _codes(_one_output("balk_if", "queue_length >= 2"))
    assert "E_QUEUE_NO_LEAVE_LINK" in _codes(_one_output("renege_after", "5"))
    # A malformed balk_if is an ordinary expression problem, and `queue_length`
    # is a legal name there and nowhere else.
    assert "E_EXPRESSION" in _codes(_two_outputs(balk_if="queue_length >= "))
    assert "E_EXPRESSION" not in _codes(_two_outputs(balk_if="queue_length >= 2"))
    assert "E_EXPRESSION" in _codes(_two_outputs(renege_after="queue_length"))
    assert "E_TIME_UNIT" in _codes(_two_outputs(renege_after="5", renege_time_unit="fortnights"))

    # The second output with neither option set is a warning, not an error:
    # the link is simply dead.
    assert _codes(_two_outputs()) == {"W_QUEUE_LEAVE_LINK_UNUSED"}

    # Preemption's scope (ARCHITECTURE D55).
    assert "E_PREEMPT_SCOPE" in _codes(_preempting(action="Seize Delay"))
    assert "E_ON_PREEMPT" in _codes(_preempting(on_preempt="continue"))
    assert "E_PREEMPT_RESOURCE_SET" in _codes(_preempting(resource="Team"))
    assert "W_PREEMPT_NO_PRIORITY" in _codes(_preempting())
    assert "E_PREEMPT_SCOPE" in _codes(_preempting(with_seize_block=True))
    # ... and a well-formed preempting model -- one whose resource really is
    # contested by two priorities -- is clean.
    assert _codes(_preempting(rival=True)) == set()


def _one_output(key, value):
    c, p, out = create("arrive"), process("serve", **{key: value}), dispose("out")
    return layout([c, p, out], chain(c, p, out),
                  resources=[{"name": "Server", "capacity": 1}])


def _two_outputs(**params):
    c, p = create("arrive"), process("serve", **params)
    out, left = dispose("out"), dispose("left")
    return layout([c, p, out, left],
                  chain(c, p, out) + [{"source": "serve", "target": "left"}],
                  resources=[{"name": "Server", "capacity": 1}])


def _preempting(action="Seize Delay Release", on_preempt="resume", resource="Server",
                priority=0, with_seize_block=False, rival=False):
    c = create("arrive")
    p = process("serve", resource=resource, action=action, preempt=True, on_preempt=on_preempt,
                priority=priority)
    out = dispose("out")
    blocks, links = [c, p, out], chain(c, p, out)
    if action != "Seize Delay Release":     # a Seize-Delay needs its RELEASE later
        rel = {"name": "rel", "description": "rel", "type": "RELEASE", "position": [2, 0],
               "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                          "resource_name": resource}}
        blocks, links = [c, p, rel, out], chain(c, p, rel, out)
    if rival:       # a second, lower-priority job stream on the same resource
        other = create("other")
        other["position"] = [0, 1]
        bulk = process("bulk", resource=resource, priority=9)
        bulk["position"] = [1, 1]
        blocks += [other, bulk]
        links += chain(other, bulk) + [{"source": "bulk", "target": "out"}]
    if with_seize_block:
        other = create("other")
        other["position"] = [0, 1]
        seize = {"name": "hold", "description": "hold", "type": "SEIZE", "position": [1, 1],
                 "params": {"distribution": "Constant", "dist_params": {"value": 1.0},
                            "resource_name": resource}}
        rel = {"name": "rel2", "description": "rel2", "type": "RELEASE", "position": [2, 1],
               "params": {"distribution": "Constant", "dist_params": {"value": 0.0},
                          "resource_name": resource}}
        blocks += [other, seize, rel]
        links += chain(other, seize, rel) + [{"source": "rel2", "target": "out"}]
    return layout(blocks, links,
                  resources=[{"name": "Server", "capacity": 1}, {"name": "Other", "capacity": 1}],
                  resource_sets=[{"name": "Team", "members": ["Server", "Other"]}])


# ---------------------------------------------------------------- oracles
def _in_ci(samples, theory, label, confidence=0.99):
    st = ci95(samples, confidence=confidence)
    assert st["lb"] <= theory <= st["ub"], (
        f"{label}: theory {theory:.4f} outside CI [{st['lb']:.4f}, {st['ub']:.4f}] "
        f"(mean {st['mean']:.4f})")


def test_balking_reproduces_the_mm1k_blocking_probability(make_model):
    """M/M/1/K is exactly an M/M/1 that balks at `queue_length >= K-1`.

    With lambda = 0.8, mu = 1 and K = 3 in the system, the stationary
    distribution is p_n = (1-r) r^n / (1 - r^(K+1)), so the fraction of
    arrivals turned away is p_K and the mean queue is sum (n-1) p_n. Short
    runs are enough here: both are fractions of the whole arrival stream, not
    a tail, so 6 replications of 3000 time units put about 14000 arrivals
    behind each estimate.
    """
    lam, mu, K = 0.8, 1.0, 3
    r = lam / mu
    p = [(1 - r) * r ** n / (1 - r ** (K + 1)) for n in range(K + 1)]
    blocking, mean_queue = p[K], sum((n - 1) * p[n] for n in range(1, K + 1))

    c = create("arrive", distribution="Exponential", dist_params={"rate": lam})
    serve = process("serve", resource="Server", distribution="Exponential", value=mu,
                    balk_if=f"queue_length >= {K - 1}")
    out, left = dispose("out"), dispose("left")
    path = make_model([c, serve, out, left],
                      [["arrive", "serve"], ["serve", "out"], ["serve", "left"]],
                      resources=[{"name": "Server", "capacity": 1}])

    balked, queues = [], []
    for seed in range(6):
        kpi = run_single_simulation(path, seed=seed, run_len=3000, warmup=300)
        row = kpi["queue_summary"].iloc[0]
        created = kpi["entity_summary"]["Total Created"].sum()
        balked.append(row["Balked"] / created)
        queues.append(kpi["resource_summary"].iloc[0]["Queue Length Avg"])
    _in_ci(balked, blocking, "M/M/1/K blocking probability")
    _in_ci(queues, mean_queue, "M/M/1/K mean queue length")


def test_reneging_reproduces_the_mm1_plus_m_birth_death(make_model):
    """M/M/1+M with exponential patience: a birth-death chain whose death rate
    in state n is mu + (n-1) theta, because every waiting customer may give up.

    The stationary distribution is computed here rather than quoted, and both
    the fraction who give up (their total reneging rate over lambda) and the
    mean queue are checked against it.

    Seeds 6 -> 4, run_len 3000 -> 2000, warmup 300 -> 250 (suite reduction,
    M3D-5): at this size the 99% CIs are [0.358, 0.390] (theory 0.3662, so
    0.008/0.024 clear of each edge) and [0.786, 1.011] (theory 0.8789, so
    0.093/0.132 clear) -- comfortable on both.
    """
    lam, mu, theta = 1.2, 1.0, 0.5
    states = 300
    p = [1.0]
    for n in range(1, states):
        p.append(p[-1] * lam / (mu + (n - 1) * theta))
    total = sum(p)
    p = [value / total for value in p]
    reneged = sum((n - 1) * theta * p[n] for n in range(1, states)) / lam
    mean_queue = sum((n - 1) * p[n] for n in range(1, states))

    c = create("arrive", distribution="Exponential", dist_params={"rate": lam})
    serve = process("serve", resource="Server", distribution="Exponential", value=mu,
                    renege_after=f"EXPO({1 / theta})")
    out, left = dispose("out"), dispose("left")
    path = make_model([c, serve, out, left],
                      [["arrive", "serve"], ["serve", "out"], ["serve", "left"]],
                      resources=[{"name": "Server", "capacity": 1}])

    fractions, queues = [], []
    for seed in range(4):
        kpi = run_single_simulation(path, seed=seed, run_len=2000, warmup=250)
        row = kpi["queue_summary"].iloc[0]
        created = kpi["entity_summary"]["Total Created"].sum()
        fractions.append(row["Reneged"] / created)
        queues.append(kpi["resource_summary"].iloc[0]["Queue Length Avg"])
    _in_ci(fractions, reneged, "M/M/1+M fraction reneged")
    _in_ci(queues, mean_queue, "M/M/1+M mean queue length")


def test_preemptive_resume_leaves_the_high_class_untouched_by_the_low_one(make_model):
    """Under preemptive-resume priority the high class is never delayed by the
    low one at all, so its numbers are IDENTICAL with and without the low class
    in the model (exact, no statistics: per-block RNG streams mean the high
    stream draws the same numbers in both, so any interference would show as a
    difference). The solo model is then a plain M/M/1, whose theory
    `test_oracles.test_mm1_sanity_with_a_loose_tolerance` already checks, so
    the long statistical run that used to follow was dropped (suite pass 4).
    """
    mu, lam_high, lam_low = 1.0, 0.4, 0.4

    high_in = create("high_in", distribution="Exponential", dist_params={"rate": lam_high},
                     e_type="High")
    low_in = create("low_in", distribution="Exponential", dist_params={"rate": lam_low},
                    e_type="Low")
    low_in["position"] = [0, 1]
    high = process("high", resource="Server", distribution="Exponential", value=mu,
                   priority=0, preempt=True)
    low = process("low", resource="Server", distribution="Exponential", value=mu,
                  priority=1, on_preempt="resume")
    low["position"] = [1, 1]
    out = dispose("out")
    server = [{"name": "Server", "capacity": 1}]
    mixed = make_model([high_in, low_in, high, low, out],
                       [["high_in", "high"], ["low_in", "low"], ["high", "out"], ["low", "out"]],
                       resources=server, filename="mixed.json")
    solo = make_model([high_in, high, out], [["high_in", "high"], ["high", "out"]],
                      resources=server, filename="solo.json")

    # Four short runs: serial is cheaper than waking the session process pool.
    for seed in range(2):
        with_low = run_single_simulation(mixed, seed=seed, run_len=3000, warmup=300)
        without = run_single_simulation(solo, seed=seed, run_len=3000, warmup=300)
        row = with_low["entity_summary"].set_index("Entity Type").loc["HIGH"]
        assert (row == without["entity_summary"].set_index("Entity Type").loc["HIGH"]).all()
        # The low class really was interrupted -- otherwise the identity is vacuous.
        assert _queue_row(with_low, "low")["Preempted"] > 0


# ===================== blocking after service (PHASE4 T5, D78) ==============
def buffer(name="buf", capacity=1, **extra):
    params = {"capacity": capacity}
    params.update(extra)
    return {"name": name, "description": name, "type": "BUFFER", "position": [2, 0],
            "params": params}


def _two_machine_line(middle, *, n=10, s1=1.0, s2=3.0, m1_extra=None, m2_extra=None):
    """A saturated two-machine line: *n* parts released at t=0, M1 then M2,
    with whatever blocks *middle* puts between them."""
    src = create("src", distribution="Constant", dist_params={"value": 0.0}, max_arrivals=n)
    m1 = process("m1", resource="M1", value=s1, **(m1_extra or {}))
    m2 = process("m2", resource="M2", value=s2, **(m2_extra or {}))
    m2["position"] = [3, 0]
    out = dispose("out")
    blocks = [src, m1, *middle, m2, out]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "M1", "capacity": 1}, {"name": "M2", "capacity": 1}])


def _buffer_row(results, block):
    frame = results["buffer_summary"]
    return frame[frame["Block"] == block].iloc[0]


def test_a_finite_buffer_blocks_the_first_machine_on_an_exact_timeline():
    """Ten parts released at t=0, M1 takes 1 minute, M2 takes 3, one place
    between them. By hand:

    * part 1 done at 1, buffer free, M2 starts it at once -- M1 never waits;
    * part 2 done at 2 into the one place; part 3 done at 3 with the place
      still taken, so M1 is BLOCKED 3 -> 4, when M2 starts part 2;
    * from there M1 runs at M2's pace: work 1, blocked 2, for every part.

    So M1 is blocked for 1 + 7 x 2 = 15 minutes, holds its unit for 10 + 15 =
    25 of the 31 minutes the run lasts, and the last part leaves at 3 x 10 + 1.
    """
    results = run_model(_two_machine_line([buffer("buf", 1)]), replications=1,
                        run_length=0, warmup=0)
    assert metric_value(results, "Simulation Time") == 31.0
    row = _buffer_row(results, "m1")
    assert row["Blocked %"] == round(100 * 15 / 31, 2)
    assert row["Starved %"] == round(100 * 6 / 31, 2)          # idle 25 -> 31
    assert resource_row(results, "M1")["Usage % (snapshot)"] == round(100 * 25 / 31, 2)
    # The buffer held a part for 26 of the 31 minutes, and never more than one.
    buf = _buffer_row(results, "buf")
    assert (buf["Capacity"], buf["Max Content"], buf["Entered"]) == (1, 1.0, 10)
    assert buf["Avg Content"] == round(26 / 31, 4)
    assert buf["Time Full %"] == round(100 * 26 / 31, 2)


def test_a_zero_place_buffer_holds_the_part_until_the_next_station_starts_it():
    """The classic zero-buffer line: M1 hands over only when M2 starts.

    M1 finishes part 1 at t=1 and M2 takes it at once; part 2 is done at 2 and
    waits IN M1 until M2 is free at 4. From then M1 is blocked 2 of every 3
    minutes and the last of the 10 parts leaves at 3 x 10 + 1 = 31 -- the same
    throughput one place gives, since M2 is the bottleneck either way.
    """
    results = run_model(_two_machine_line([buffer("buf", 0)]), replications=1,
                        run_length=0, warmup=0)
    assert metric_value(results, "Simulation Time") == 31.0
    # Blocked 2 -> 4, 5 -> 7, ... 26 -> 28: nine waits of 2 minutes.
    assert _buffer_row(results, "m1")["Blocked %"] == round(100 * 18 / 31, 2)
    assert _buffer_row(results, "buf")["Capacity"] == 0


def test_transparent_blocks_keep_the_hold_and_a_delay_ends_it():
    """Which blocks are "transparent" to the hold is the D78 contract.

    An ASSIGN between the stations changes nothing: it takes no time and holds
    nothing, so the part is still in M1. A DELAY does take time -- the part is
    in transit, off the machine -- so M1 is free the moment it hands over.
    """
    assign = {"name": "stamp", "description": "stamp", "type": "ASSIGN", "position": [2, 1],
              "params": {"assignments": [{"target": "entity.seen", "expr": "1"}]}}
    through = run_model(_two_machine_line([buffer("buf", 1), assign]),
                        replications=1, run_length=0, warmup=0)
    assert _buffer_row(through, "m1")["Blocked %"] == round(100 * 15 / 31, 2)

    moved = run_model(_two_machine_line([delay("move", 2.0), buffer("buf", 1)]),
                      replications=1, run_length=0, warmup=0)
    assert moved["buffer_summary"][moved["buffer_summary"]["Block"] == "m1"].empty


def test_a_station_with_a_finite_input_queue_blocks_upstream_and_can_balk_instead():
    """`queue_capacity` is the same space owned by the station (D78), and it is
    what `balk_if` reads as `queue_length` -- the LOSS system that is the
    alternative to blocking, on the same line."""
    results = run_model(_two_machine_line([], m2_extra={"queue_capacity": 1}),
                        replications=1, run_length=0, warmup=0)
    assert metric_value(results, "Simulation Time") == 31.0
    assert _buffer_row(results, "m1")["Blocked %"] == round(100 * 15 / 31, 2)
    assert _buffer_row(results, "m2")["Capacity"] == 1

    # The same line, but a full queue turns parts away instead of stopping M1.
    loss = _two_machine_line([], m2_extra={"queue_capacity": 1, "balk_if": "queue_length >= 1"})
    loss["blocks"].append(dispose("lost"))
    loss["links"].append({"source": "m2", "target": "lost"})
    results = run_model(loss, replications=1, run_length=0, warmup=0)
    assert _queue_row(results, "m2")["Balked"] > 0
    assert _buffer_row(results, "m1")["Blocked %"] == 0.0     # nothing ever blocks


def test_a_reneging_part_frees_the_place_it_was_waiting_in():
    """A part that gives up leaves its place at once, so the machine behind it
    is unblocked by the renege and not by a service completion."""
    line = _two_machine_line([], n=4, s1=1.0, s2=10.0,
                             m2_extra={"queue_capacity": 1, "renege_after": "2"})
    line["blocks"].append(dispose("lost"))
    line["links"].append({"source": "m2", "target": "lost"})
    results = run_model(line, replications=1, run_length=0, warmup=0)
    assert _queue_row(results, "m2")["Reneged"] == 3
    assert _buffer_row(results, "m2")["Max Content"] == 1.0
    assert _buffer_row(results, "m1")["Blocked %"] > 0.0


def test_a_downstream_failure_blocks_upstream_for_the_downtime_less_the_slack():
    """The exact interaction with a breakdown (D78 and ROADMAP 1.4's WAIT rule).

    Both machines take 2 minutes, two places between them, and M2 fails at
    t=10 for 6 minutes. M1 keeps working while the places fill and only then
    blocks, so it loses the downtime minus the slack the buffer bought.
    """
    line = _two_machine_line([buffer("buf", 2)], n=40, s1=2.0, s2=2.0)
    line["failures"] = [{"name": "jam", "resources": ["M2"],
                         "uptime": {"distribution": "Constant", "dist_params": {"value": 10.0}},
                         "downtime": {"distribution": "Constant", "dist_params": {"value": 6.0}}}]
    results = run_model(line, replications=1, run_length=16, warmup=0)
    assert _buffer_row(results, "buf")["Max Content"] == 2.0
    blocked = _buffer_row(results, "m1")["Blocked %"] * 16 / 100
    assert blocked == pytest.approx(2.0, abs=0.01)


def test_a_loop_of_finite_buffers_warns_and_then_fails_the_run():
    """A rework loop back into a full buffer can really deadlock (D78). The
    static check names the loop; the run refuses to end quietly."""
    src = create("src", distribution="Constant", dist_params={"value": 0.0}, max_arrivals=6)
    m1 = process("m1", resource="M1", value=1.0)
    m2 = process("m2", resource="M2", value=1.0)
    m2["position"] = [4, 0]
    again = {"name": "again", "description": "again", "type": "DECIDE", "position": [5, 0],
             "params": {"mode": "Chance", "probs": [1.0]}}
    b1, b2 = buffer("b1", 1), buffer("b2", 1)
    b2["position"] = [6, 0]
    out = dispose("out")
    blocks = [src, m1, b1, m2, again, b2, out]
    links = (chain(src, m1, b1, m2, again) + [{"source": "again", "target": "b2"},
                                              {"source": "b2", "target": "m1"}])
    model = layout(blocks, links,
                   resources=[{"name": "M1", "capacity": 1}, {"name": "M2", "capacity": 1}])
    loops = [p for p in validate_model_structured(model) if p.code == "W_BLOCKING_LOOP"]
    assert loops and "b1" in loops[0].message and "b2" in loops[0].message

    with pytest.raises(RuntimeError, match="deadlock"):
        run_model(model, replications=1, run_length=100, warmup=0)


def test_a_capacity_and_a_preemption_that_cannot_work_are_refused():
    """Two permanent codes: a negative capacity is not a count, and a part that
    is blocked is finished, so nothing may preempt it (D55 and D78)."""
    bad = _two_machine_line([buffer("buf", -1)])
    assert "E_BUFFER_CAPACITY" in problem_codes(bad, severity=None)

    line = _two_machine_line([buffer("buf", 1)], m1_extra={"preempt": True})
    assert "E_PREEMPT_BLOCKING" in problem_codes(line, severity=None)


def test_playback_shows_the_blocked_station_as_blocked_not_busy():
    """The `blocked` graphic state (docs/ASSET_GRAPHICS.md) needs the trace to
    carry it: a machine holding a finished part is not merely busy."""
    from nagasim.core.playback import Timeline

    trace = TraceRecorder()
    run_model(_two_machine_line([buffer("buf", 1)]), replications=1, run_length=0, warmup=0,
              trace=trace)
    states = [e.state for e in trace.events if e.kind == "resource_state"]
    assert "blocked" in states
    # M1 is blocked from t=3 to t=4 (the exact timeline above), so a frame
    # inside it must report "blocked" and not "busy".
    timeline = Timeline(trace.events)
    assert timeline.state_at(3.5)["resources"]["M1"]["state"] == "blocked"
    assert timeline.state_at(0.5)["resources"]["M1"]["state"] == "busy"


# ============ blocking BEFORE service (PHASE5 O4, ARCHITECTURE 8 #11a) ============
def _bbs_line(places, *, hold, n=10, s1=1.0, s2=3.0, distribution="Constant"):
    """M1 -> a BUFFER of *places* -> M2, saturated; *hold* makes M1 reserve its
    downstream place before it starts a job (blocking BEFORE service)."""
    src = create("src", distribution="Constant", dist_params={"value": 0.0}, max_arrivals=n)
    m1 = process("m1", resource="M1", value=s1, distribution=distribution, hold_until_space=hold)
    m2 = process("m2", resource="M2", value=s2, distribution=distribution)
    m2["position"] = [3, 0]
    out = dispose("out")
    blocks = [src, m1, buffer("buf", places), m2, out]
    return layout(blocks, chain(*blocks),
                  resources=[{"name": "M1", "capacity": 1}, {"name": "M2", "capacity": 1}])


def test_blocking_before_service_on_an_exact_timeline():
    """The line of the blocking-AFTER test above (M1 1 min, M2 3 min, one
    place, ten parts at t=0), but M1 starts a job only once it holds the place.

    * part 1: place reserved at 0, done at 1, M2 starts it at once (place free);
    * part 2: reserved at 1, done at 2, waits in the place until M2 frees at 4;
    * part k >= 3: M1 seizes at 3k-7 and is BLOCKED 2 minutes before it may
      start (the place is part k-1's until M2 starts it), works 1.

    Blocked 8 x 2 = 16 (one more than blocking after service: part 3 waits
    from 2, not from 3), M1 held 0 -> 26, the place taken 0 -> 28 without a
    break, and the last part still leaves at 31: M2 is the bottleneck.
    """
    results = run_model(_bbs_line(1, hold=True), replications=1, run_length=0, warmup=0)
    assert metric_value(results, "Simulation Time") == 31.0
    row = _buffer_row(results, "m1")
    assert row["Blocked %"] == round(100 * 16 / 31, 2)
    assert row["Starved %"] == round(100 * 5 / 31, 2)
    assert resource_row(results, "M1")["Usage % (snapshot)"] == round(100 * 26 / 31, 2)
    buf = _buffer_row(results, "buf")
    assert (buf["Max Content"], buf["Entered"]) == (1.0, 10)
    assert buf["Avg Content"] == round(28 / 31, 4)


def test_blocking_before_service_with_one_place_is_after_service_with_none():
    """Two exponential machines (rate 1 each), saturated. Before-service with B
    places IS after-service with B-1 (M1 may start part n exactly when M2
    starts part n-1 in both), so with the same streams the two runs leave at
    the SAME instants. The after-service, zero-place, equal-rate line is the
    Markov chain `test_oracles.test_blocked_tandem_line_matches_its_markov_chain`
    checks (throughput 2/3), so no separate statistical run is needed here
    (suite pass 4 dropped it).
    """
    before = run_model(_bbs_line(1, hold=True, n=300, s1=1.0, s2=1.0, distribution="Exponential"),
                       replications=1, run_length=0, warmup=0, seed=5)
    after = run_model(_bbs_line(0, hold=False, n=300, s1=1.0, s2=1.0, distribution="Exponential"),
                      replications=1, run_length=0, warmup=0, seed=5)
    assert list(before["entity_log"]["Exited At"]) == list(after["entity_log"]["Exited At"])


def test_hold_until_space_needs_a_downstream_space():
    """E_HOLD_SPACE_TARGET: nothing to reserve in, statically and at run time."""
    no_space = _bbs_line(0, hold=True)
    assert "E_HOLD_SPACE_TARGET" in _codes(no_space)
    with pytest.raises(Exception, match="hold_until_space"):
        run_model(no_space, replications=1, run_length=0, warmup=0)
    # A station's own queue_capacity is a space too, named with space_of.
    line = _two_machine_line([], m1_extra={"hold_until_space": True, "space_of": "m2"},
                             m2_extra={"queue_capacity": 1})
    assert "E_HOLD_SPACE_TARGET" not in _codes(line)
    assert metric_value(run_model(line, replications=1, run_length=0, warmup=0),
                        "Simulation Time") == 31.0
    assert _codes(_bbs_line(1, hold=True)) == set()
    # A station that reserves before service is a blocker even where it would
    # not block after service (a DELAY in between): no preemption on it.
    guarded = _two_machine_line([delay("move", 2.0)],
                                m1_extra={"hold_until_space": True, "space_of": "m2",
                                          "preempt": True},
                                m2_extra={"queue_capacity": 1})
    assert "E_PREEMPT_BLOCKING" in _codes(guarded)


# ============ preemption restart resampling (PHASE5 O4, D55) ===================
def _restart_model(resample, distribution="Exponential", value=0.1):
    """The exact preemption case above: a low job at t=0, a priority job at t=3
    that runs 4 minutes, low restarts under `on_preempt="restart"`."""
    low_in = create("low_in", distribution="Constant", dist_params={"value": 100.0},
                    max_arrivals=1, e_type="Low")
    high_in = create("high_in", distribution="Constant", dist_params={"value": 100.0},
                     max_arrivals=1, start_time=3, e_type="High")
    high_in["position"] = [0, 1]
    low = process("low", resource="M", value=value, distribution=distribution, priority=5,
                  on_preempt="restart", resample=resample)
    high = process("high", resource="M", value=4.0, priority=1, preempt=True)
    high["position"] = [1, 1]
    out = dispose("out")
    return layout([low_in, high_in, low, high, out],
                  [{"source": "low_in", "target": "low"}, {"source": "high_in", "target": "high"},
                   {"source": "low", "target": "out"}, {"source": "high", "target": "out"}],
                  resources=[{"name": "M", "capacity": 1}])


def test_restart_resampling_draws_the_next_sample_of_the_blocks_own_stream():
    """With `resample` the restarted job takes the block's NEXT draw, so the
    oracle is the block's stream itself (CRN: the same SeedSequence the engine
    builds): exit = 7 + draw 2, against 7 + draw 1 without it. A constant
    service is identical either way."""
    import numpy as np
    from nagasim.core.base_process import deterministic_hash
    from nagasim.core.distributions import StochasticDistribution

    expo = StochasticDistribution("Exponential", {"rate": 0.1})
    for seed in range(20):       # the first seed whose first job outlasts t=3
        rng = np.random.default_rng(np.random.SeedSequence([seed, deterministic_hash("low")]))
        first, second = expo.sample(rng), expo.sample(rng)
        if first > 3.0:
            break
    exits = {}
    for resample in (False, True):
        log = run_model(_restart_model(resample), replications=1, run_length=0, warmup=0,
                        seed=seed)["entity_log"].set_index("Type")
        exits[resample] = log.loc["LOW", "Exited At"]
    assert exits[False] == pytest.approx(7.0 + first)
    assert exits[True] == pytest.approx(7.0 + second)

    same = [run_model(_restart_model(resample, "Constant", 10.0), replications=1, run_length=0,
                      warmup=0)["entity_log"] for resample in (False, True)]
    assert same[0].equals(same[1])
