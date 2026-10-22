"""State in, fifth round (D437 to D440): a formed batch, a level transfer waiting for room. The
exit check is the one of `test_state.py`: a run started from a state taken at t is the
uninterrupted run with a warm-up of t, and the state taken again at the clock is the same
document."""
import numpy as np
import pytest

from conftest import chain, create, dispose, layout, problem_codes, process
from test_state import _block, _bundled, _carried, _kpis, _match_line, _refusal

from nagasim import api, state


# ---------------------------------------------------------------- a formed batch (D437, D438)
def _oven(time_limit=60, bake=3.5, split=True, distribution="Constant",
          mode="Retain original entity values"):
    """A part every 1 from 0 takes one of 6 tokens; 3 parts make a LOAD, which bakes 3.5 on
    the one oven and splits; each part is machined 0.4 and gives its token back. Without
    split there are no tokens and the load leaves whole. Nothing random."""
    c, b, d = create("c"), _block("b", "BATCH", batch_size=3, batched_entity_type="LOAD"), \
        dispose("d")
    p = process("p", resource="Oven", value=bake, distribution=distribution)
    blocks = [c, b, p, d]
    if split:
        blocks = [c, _block("t", "SEIZE", resource_name="Tokens", quantity=1, priority=0), b, p,
                  _block("s", "SEPARATE", mode=mode), process("w", resource="Machine", value=0.4),
                  _block("r", "RELEASE", resource_name="Tokens"), d]
    return layout(blocks, chain(*blocks), resources=[
        {"name": "Tokens", "capacity": 6}, {"name": "Oven", "capacity": 1},
        {"name": "Machine", "capacity": 1}],
        simulation={"time_limit": time_limit, "warmup": 0, "replications": 1})


def test_a_load_in_the_oven_by_hand():
    """Loads form at 2 (parts 0-2) and 5 (parts 3-5); the first bakes 2-5.5, the second waits
    for the oven and bakes 5.5-9. At 6.2 the second is in the oven with its three parts
    inside, each holding the token it took on arriving. Started from there, it splits at 9
    and its parts leave the machine one after another: 9.4, 9.8, 10.2."""
    model = _oven()
    doc = state.take(model, 6.2)
    load = next(e for e in doc["state"]["entities"] if e.get("members"))
    assert (load["block"], load["e_type"], load["ends"], load["since"]) == ("p", "LOAD", [9.0], 5.5)
    assert load["age"] == pytest.approx(1.2)
    assert [m["age"] for m in load["members"]] == pytest.approx([3.2, 2.2, 1.2])
    assert [m["holds"] for m in load["members"]] == [
        {"Tokens": {"since": t}} for t in (3.0, 4.0, 5.0)]
    started, problems = state.start(model, doc)
    assert problems == []
    log = api.run_model(started)["entity_log"]
    parts = log[(log["Type"] == "DEFAULT") & log["Created At"].between(2.5, 5.5)]
    exits = sorted(parts["Exited At"])
    assert exits == pytest.approx([9.4, 9.8, 10.2])


@pytest.mark.parametrize("model, times", [
    (_oven(), (2.5, 6.2, 13.7, 20.1, 33.3, 47.95)), (_oven(split=False), (6.2, 13.7, 33.3)),
    (_oven(mode="Take all representative values"), (6.2, 20.1, 47.95))],
    ids=["split", "whole", "take all"])
def test_a_formed_batch_is_carried_with_its_members(model, times):
    for at in times:
        taken = _carried(model, at)
        assert any(e.get("members") for e in taken["entities"]), at


def _pairs():
    """A MATCH that batches an A and a B into a PAIR, which a station of 3.5 serves and a
    SEPARATE splits again."""
    model = _match_line()
    m = next(b for b in model["blocks"] if b["name"] == "m")
    m["params"].update(batch_option=True, batched_entity_type="PAIR")
    p, s = process("p", value=3.5), _block("s", "SEPARATE")
    model["blocks"] += [p, s]
    model["links"] = [link for link in model["links"] if link["source"] != "m"] \
        + chain(m, p, s) + [{"source": "s", "target": "d"}]
    model["resources"] = [{"name": "Server", "capacity": 1}]
    return model


@pytest.mark.parametrize("at", [7.3, 15.7, 31.1])
def test_a_pair_a_match_formed_is_carried(at):
    taken = _carried(_pairs(), at)
    assert any(len(e.get("members", ())) == 2 for e in taken["entities"])


@pytest.mark.slow
def test_a_formed_batch_is_reproduced_in_distribution():
    """As `test_state`: the state of seed s started with another seed against the run of seed
    s, 30 seeds, each mean difference inside its 99% interval."""
    model = _oven(time_limit=400, bake=1 / 2.8, distribution="Exponential")
    kpis = ("entity/DEFAULT/Avg Time", "resource/Oven/Idle %", "system/Total Entities Completed")
    differences = {kpi: [] for kpi in kpis}
    for seed in range(30):
        doc = state.take(model, 150.3, seed=seed)
        warm, problems = state.start(model, doc)
        assert problems == []
        reference = _kpis(api.run_model(model, warmup=150.3, replications=1, seed=seed))
        started = _kpis(api.run_model(warm, replications=1, seed=1000 + seed))
        for kpi in kpis:
            differences[kpi].append(started[kpi] - reference[kpi])
    for kpi, values in differences.items():
        mean, error = np.mean(values), np.std(values, ddof=1) / np.sqrt(len(values))
        assert abs(mean) <= 2.756 * error and error > 0, kpi


@pytest.mark.parametrize("name, at", [("warehouse_library", 36.3), ("warehouse_library", 52.07),
                                      ("warehouse_library", 300.3), ("mfg_line_library", 56.5),
                                      ("mfg_line_library", 700.3)])
def test_the_bundled_trucks_truckloads_and_oven_loads_are_carried(name, at):
    taken = _carried(_bundled(name, deterministic=True), at)
    assert any(e.get("members") for e in taken["entities"])


def test_a_batch_inside_a_batch_is_refused_by_name():
    c, b1, b2 = create("c"), _block("b1", "BATCH", batch_size=2), _block("b2", "BATCH",
                                                                          batch_size=2)
    p, d = process("p", value=9.0), dispose("d")
    model = layout([c, b1, b2, p, d], chain(c, b1, b2, p, d),
                   resources=[{"name": "Server", "capacity": 1}],
                   simulation={"time_limit": 60, "warmup": 0, "replications": 1})
    assert "a batch inside a batch" in _refusal(model, 5.5)


def _load(**member):
    """The load in the oven at 6.2 (`test_a_load_in_the_oven_by_hand`); *member* changes its
    third member, a None value removing that key."""
    third = {"age": 1.2, "holds": {"Tokens": {"since": 5.0}}, **member}
    return {"block": "p", "count": 1, "in_service": 1, "ends": [9.0], "since": 5.5, "age": 1.2,
            "e_type": "LOAD", "members": [{"age": 3.2, "holds": {"Tokens": {"since": 3.0}}},
                                          {"age": 2.2, "holds": {"Tokens": {"since": 4.0}}},
                                          {k: v for k, v in third.items() if v is not None}]}


def _members_at(block, count=1):
    return {"block": block, "count": 1, "e_type": "LOAD", "age": 1.0,
            "members": [{"age": 2.0, "holds": {"Tokens": {}}}] * count}


@pytest.mark.parametrize("entry, code", [
    (_load(), None),
    (_load(holds=None), "E_INITIAL_RESOURCE"),      # #455: the RELEASE after the split
    (_load(holds={"Tokens": {"since": 4.9}}), "E_INITIAL_RESOURCE"),    # #460: before it was
    (_load(attributes={"add_cost": 1}), "E_INITIAL_BATCH"),            # #457
    (_members_at("t"), "E_INITIAL_BATCH"),          # #456: it would be batched again
    (_members_at("b"), "E_INITIAL_BATCH"),
    (_members_at("w"), "E_INITIAL_BATCH"),          # after the SEPARATE
    (_members_at("p"), None),
    ({**_load(), "count": 2}, "E_INITIAL_BATCH"),
    ({**_load(), "members": []}, "E_INITIAL_BATCH"),
    ({**_load(), "members": "three"}, "E_INITIAL_BATCH"),
    ({**_load(), "members": [{}] * 4}, "E_INITIAL_BATCH"),          # more than a BATCH forms
    (_load(age=1.0), "E_INITIAL_BATCH"),                             # younger than its batch
    (_load(age=float("nan")), "E_INITIAL_BATCH"),
    (_load(where="p"), "E_INITIAL_BATCH"),
    (_load(e_type=5), "E_INITIAL_BATCH"),
    (_load(attributes={"id": 1}), "E_INITIAL_BATCH"),
    (_load(attributes={"cost": "x"}), "E_INITIAL_BATCH"),
    (_load(holds={"Nobody": {}}), "E_INITIAL_RESOURCE"),
    (_load(holds={"Machine": {}}), "E_INITIAL_RESOURCE"),           # nothing gives it back
    (_load(holds={"Tokens": {"units": 5}}), "E_INITIAL_IN_SERVICE"),  # 7 of 6 tokens
    ({**_load(), "members": None, "age": 0}, "E_INITIAL_BATCH"),    # #451: a lone entity
], ids=lambda v: str(v)[:40] if isinstance(v, str) else None)
def test_a_bad_formed_batch_is_refused(entry, code):
    model = _oven()
    entry = {k: v for k, v in entry.items() if v is not None}
    model["initial_state"] = {"clock": 6.2, "entities": [entry]}
    found = problem_codes(model)
    assert (code in found) if code else not found


def test_members_need_a_batch_the_model_forms():
    c, p, d = create("c"), process("p"), dispose("d")
    model = layout([c, p, d], chain(c, p, d), resources=[{"name": "Server", "capacity": 1}])
    model["initial_state"] = {"entities": [{"block": "p", "count": 1, "members": [{}]}]}
    assert "E_INITIAL_BATCH" in problem_codes(model)


@pytest.mark.parametrize("split", [True, False], ids=["split", "whole"])
def test_a_formed_batch_under_a_plant_is_refused_by_name(split):
    """D440: a plant sees the load at the oven, and no signal says which parts are in it;
    also where the load leaves whole and no SEPARATE follows (its type is the BATCH's)."""
    seen = {"at": 6.2, "entities": [{"block": "p", "e_type": "LOAD", "holds": ["Oven"],
                                     "since": 5.5}]}
    doc, _problems = state.from_plant(_oven(split=split), seen)
    started, problems = state.start(_oven(split=split), doc)
    assert started is None and "E_INITIAL_BATCH" in {p.code for p in problems}


# ---------------------------------------------------------------- a waiting transfer (D439)
def _transfer(name, mode, amount):
    return _block(name, "TRANSFER", level="Stock", amount=str(amount), mode=mode, wait=True)


def _draws():
    """A draw of 4 every 1.5 from 1 from a stock of 5 that a flow of 1 refills; each waits
    for the one before it."""
    c = create("c", dist_params={"value": 1.5}, start_time=1, max_arrivals=4)
    draw, d = _transfer("draw", "remove", 4), dispose("d")
    return layout([c, draw, d], chain(c, draw, d),
                  levels=[{"name": "Stock", "capacity": 100, "initial": 5}],
                  flows=[{"name": "replenish", "to": "Stock", "rate": "1"}],
                  simulation={"time_limit": 20, "warmup": 0, "replications": 1})


def test_two_draws_waiting_for_their_level_by_hand():
    """The first draw takes 4 at 1 (stock 6, then 2); the second waits from 2.5 until the
    stock is 4 at 3 (then 0); the third waits from 4 and the fourth from 5.5, behind it. At 6
    the stock is 3 and two draws wait in that order; started from there, the third leaves
    when the stock is 4, at 7, and the fourth four later, at 11."""
    model = _draws()
    doc = state.take(model, 6.0)
    assert doc["state"]["levels"] == {"Stock": pytest.approx(3.0)}
    assert [(e["block"], e["transfer"], e["since"]) for e in doc["state"]["entities"]] == [
        ("draw", {"amount": 4.0}, 4.0), ("draw", {"amount": 4.0}, 5.5)]
    started, problems = state.start(model, doc)
    assert problems == []
    log = api.run_model(started)["entity_log"]
    assert sorted(log["Exited At"].dropna()) == pytest.approx([7.0, 11.0])


def _stock():
    """Puts of 3 every 1 from 0.5 and takes of 5 every 2 from 0.25 on a stock of 7 (from 2):
    a put waits for room, a take for stock, both in the level's one queue. No flow: a lump
    alone moves the level. Nothing random."""
    c, put, d = create("c", start_time=0.5), _transfer("put", "add", 3), dispose("d")
    c2, take, d2 = create("c2", dist_params={"value": 2.0}, start_time=0.25),         _transfer("take", "remove", 5), dispose("d2")
    return layout([c, put, d, c2, take, d2], chain(c, put, d) + chain(c2, take, d2),
                  levels=[{"name": "Stock", "capacity": 7, "initial": 2}],
                  simulation={"time_limit": 40, "warmup": 0, "replications": 1})


@pytest.mark.parametrize("at, waiting", [(12.1, 1), (19.9, 2), (31.3, 3), (37.7, 5)])
def test_puts_waiting_for_room_are_carried_in_their_queue_order(at, waiting):
    taken = _carried(_stock(), at)
    assert [e["block"] for e in taken["entities"] if "transfer" in e] == ["put"] * waiting


@pytest.mark.parametrize("at", [4.5, 6.0, 9.2])
def test_draws_waiting_for_their_level_are_carried_exactly(at):
    """A flow refills the stock: the minimum a draw's lump leaves counts (#454)."""
    taken = _carried(_draws(), at)
    assert any("transfer" in e for e in taken["entities"])


@pytest.mark.parametrize("at", [396.3, 612.3, 924.3])
def test_the_bundled_warehouse_putaways_waiting_for_room_are_taken_again_the_same(at):
    """Stochastic: exact in distribution only (D233); the state taken again is the same."""
    model = _bundled("warehouse_library")
    doc = state.take(model, at)
    started, problems = state.start(model, doc)
    assert problems == [] and state.take(started, at)["hash"] == doc["hash"]
    assert any("transfer" in e for e in doc["state"]["entities"])


@pytest.mark.parametrize("entry, code", [
    ({"transfer": {"amount": 4.0}, "since": 3.0}, None),
    ({"transfer": {"amount": 0}}, "E_INITIAL_BLOCK"),
    ({"transfer": {"amount": -4.0}}, "E_INITIAL_BLOCK"),
    ({"transfer": {"amount": float("inf")}}, "E_INITIAL_BLOCK"),
    ({"transfer": {"amount": 100.5}}, "E_INITIAL_BLOCK"),          # #458: above the capacity
    ({"transfer": {"amount": "4"}}, "E_INITIAL_BLOCK"),
    ({"transfer": {"amount": 4.0, "mode": "add"}}, "E_INITIAL_BLOCK"),
    ({"transfer": 4.0}, "E_INITIAL_BLOCK"),
    ({"transfer": {"amount": 4.0}, "count": 2}, "E_INITIAL_BLOCK"),
    ({"transfer": {"amount": 4.0}, "arrives": 5.0}, "E_INITIAL_BLOCK"),
    ({"transfer": {"amount": 4.0}, "since": 5.0}, "E_INITIAL_REMAINING"),
    ({"transfer": {"amount": 4.0}, "block": "d"}, "E_INITIAL_BLOCK_TYPE"),
    ({}, "E_INITIAL_BLOCK_TYPE"),               # D440: a plant sees it there, with no amount
])
def test_a_bad_waiting_transfer_is_refused(entry, code):
    model = _draws()
    model["initial_state"] = {"clock": 4.5, "entities": [{"block": "draw", "count": 1, **entry}]}
    found = problem_codes(model)
    assert (code in found) if code else not found


def test_a_transfer_that_does_not_wait_carries_no_amount():
    model = _draws()
    model["blocks"][1]["params"]["wait"] = False
    model["initial_state"] = {"clock": 4.5, "entities": [
        {"block": "draw", "count": 1, "transfer": {"amount": 4.0}}]}
    assert "E_INITIAL_BLOCK" in problem_codes(model)
