"""Carriers (Phase 15 M2, D819-D824): SEPARATE `carrier: "keep"` releases a truck's
members one at a time and lets the truck live on; BATCH `carrier_type` loads members
onto an arriving truck. Every expected value is worked out by hand in
docs/superpowers/specs/2026-10-26-phase15-limits-design.md ("M2/M3 carriers")."""
import pytest

from nagasim import state
from nagasim.core.sim_runner import run_single_simulation
from nagasim.core.trace import TraceRecorder

from conftest import block, chain, create, dispose, layout, problem_codes, run_env


def _assign(name, **targets):
    return block(name, "ASSIGN", {"assignments": [{"target": f"entity.{k}", "expr": str(v)}
                                                  for k, v in targets.items()]})


def _batch(name="batch_1", size=4, e_type="TRUCK", **params):
    return block(name, "BATCH", {"batch_size": size, "batched_entity_type": e_type, **params})


def _separate(name="separate_1", carrier="keep", **params):
    return block(name, "SEPARATE", {"mode": "Retain original entity values", "attrs": [],
                                    "carrier": carrier, **params})


def _parts(name="create_1", n=4, at=0.0, every=1000.0, arrivals=1, e_type="PART"):
    return create(name, e_type=e_type, entities_per_arrival=n, max_arrivals=arrivals,
                  start_time=at, dist_params={"value": every})


def _unload_model(member_time="2", entities=None, warmup=0.0, **separate):
    """Four parts at 0 -> BATCH 4 (TRUCK) -> SEPARATE keep; members and the
    carrier each to their own DISPOSE."""
    blocks = [_parts(), _batch(), _separate(member_time=member_time, **separate),
              dispose("members"), dispose("carriers")]
    links = chain(*blocks[:4]) + [{"source": "separate_1", "target": "carriers"}]
    return layout(blocks, links, entities=entities or [])


def _gone(env, e_type):
    """[(id, deletion_time)] of the entities of *e_type* that left, by leaving time."""
    rows = [(e.id, e.deletion_time) for e in env.ctx.all_entities
            if e.e_type == e_type and e.deletion_time is not None]
    return sorted(rows, key=lambda r: (r[1], r[0]))


def _entity(kpi, e_type, column):
    df = kpi["entity_summary"]
    return float(df.loc[df["Entity Type"] == e_type, column].iloc[0])


# ---------------------------------------------------------------- SEPARATE keep
def test_members_leave_one_at_a_time_and_the_carrier_after_them():
    env, _ = run_env(_unload_model(), 100)
    assert [t for _i, t in _gone(env, "PART")] == [2.0, 4.0, 6.0, 8.0]
    assert [t for _i, t in _gone(env, "TRUCK")] == [8.0]
    kpi = run_single_simulation(_unload_model(), seed=0, run_len=100, warmup=0)
    assert _entity(kpi, "PART", "Avg Time") == pytest.approx(5.0)
    assert _entity(kpi, "TRUCK", "Avg Time") == pytest.approx(8.0)
    assert _entity(kpi, "PART", "Total Completed") == 4
    assert _entity(kpi, "TRUCK", "Total Completed") == 1


def test_no_member_time_releases_all_in_one_instant_members_first():
    recorder = TraceRecorder()
    env, _ = run_env(_unload_model(member_time=""), 100, subscribers=[recorder])
    assert [t for _i, t in _gone(env, "PART")] == [0.0] * 4
    assert [t for _i, t in _gone(env, "TRUCK")] == [0.0]
    kinds = [e.e_type for e in recorder.events
             if e.kind == "entity_moved" and e.block == "separate_1"]
    assert kinds == ["PART"] * 4 + ["TRUCK"]


def test_lifo_releases_the_last_loaded_first():
    env, _ = run_env(_unload_model(release_order="lifo"), 100)
    left = _gone(env, "PART")
    ids = [i for i, _t in left]
    assert [t for _i, t in left] == [2.0, 4.0, 6.0, 8.0]
    assert ids == sorted(ids, reverse=True)
    assert [t for _i, t in _gone(env, "TRUCK")] == [8.0]


def test_an_entity_with_no_members_leaves_by_the_carrier_link_at_once():
    blocks = [create("trucks", e_type="TRUCK", max_arrivals=1), _separate(member_time="2"),
              dispose("members"), dispose("carriers")]
    links = chain(*blocks[:3]) + [{"source": "separate_1", "target": "carriers"}]
    env, _ = run_env(layout(blocks, links), 10)
    assert _gone(env, "TRUCK")[0][1] == 0.0
    assert _gone(env, "PART") == []


def test_dispose_mode_still_refuses_an_entity_that_is_not_a_batch():
    blocks = [create("trucks", e_type="TRUCK", max_arrivals=1), _separate(carrier="dispose"),
              dispose("members")]
    with pytest.raises(Exception, match="not a batch"):
        run_env(layout(blocks, chain(*blocks)), 10)


def _cost_model(carrier):
    blocks = [_parts(), _assign("price", cost=10), _batch(), _assign("own", cost="entity.cost + 5"),
              _separate(carrier=carrier), dispose("members")]
    links = chain(*blocks)
    if carrier == "keep":
        blocks.append(dispose("carriers"))
        links.append({"source": "separate_1", "target": "carriers"})
    return layout(blocks, links)


def test_keep_mode_gives_each_member_back_its_own_cost():
    env, _ = run_env(_cost_model("keep"), 10)
    parts = [e for e in env.ctx.all_entities if e.e_type == "PART"]
    trucks = [e for e in env.ctx.all_entities if e.e_type == "TRUCK"]
    assert [e.cost for e in parts] == [10.0] * 4
    assert trucks[0].cost == pytest.approx(5.0)


def test_dispose_mode_still_splits_the_cost_evenly():
    env, _ = run_env(_cost_model("dispose"), 10)
    parts = [e for e in env.ctx.all_entities if e.e_type == "PART"]
    assert [e.cost for e in parts] == [pytest.approx(11.25)] * 4


def test_two_carriers_release_in_parallel():
    blocks = [_parts(n=2, arrivals=2, every=1.0), _batch(size=2), _separate(member_time="3"),
              dispose("members"), dispose("carriers")]
    links = chain(*blocks[:4]) + [{"source": "separate_1", "target": "carriers"}]
    env, _ = run_env(layout(blocks, links), 100)
    assert [t for _i, t in _gone(env, "PART")] == [3.0, 4.0, 6.0, 7.0]
    assert [t for _i, t in _gone(env, "TRUCK")] == [6.0, 7.0]


def test_member_time_is_evaluated_in_the_members_scope():
    blocks = [_parts("p1", n=1, at=0.0), _assign("w1", w=1), _parts("p2", n=1, at=1.0),
              _assign("w3", w=3), _batch(size=2), _separate(member_time="entity.w"),
              dispose("members"), dispose("carriers")]
    links = [{"source": "p1", "target": "w1"}, {"source": "w1", "target": "batch_1"},
             {"source": "p2", "target": "w3"}, {"source": "w3", "target": "batch_1"},
             {"source": "batch_1", "target": "separate_1"},
             {"source": "separate_1", "target": "members"},
             {"source": "separate_1", "target": "carriers"}]
    env, _ = run_env(layout(blocks, links), 100)
    by_w = {e.w: e.deletion_time for e in env.ctx.all_entities if e.e_type == "PART"}
    assert by_w == {1: 2.0, 3: 5.0}
    assert _gone(env, "TRUCK")[0][1] == 5.0


def test_warm_up_counts_members_that_leave_after_it():
    kpi = run_single_simulation(_unload_model(), seed=0, run_len=100, warmup=5)
    assert _entity(kpi, "PART", "Total Completed") == 2
    assert _entity(kpi, "TRUCK", "Total Completed") == 1


def test_members_accrue_their_own_holding_cost_aboard():
    entities = [{"name": "PART", "holding_cost": 1.0}, {"name": "TRUCK", "holding_cost": 0.5}]
    env, _ = run_env(_unload_model(entities=entities), 100)
    parts = sorted((e for e in env.ctx.all_entities if e.e_type == "PART"),
                   key=lambda e: e.deletion_time)
    assert [e._holding_cost for e in parts] == [2.0, 4.0, 6.0, 8.0]
    truck = next(e for e in env.ctx.all_entities if e.e_type == "TRUCK")
    assert truck._holding_cost == pytest.approx(4.0)
    kpi = run_single_simulation(_unload_model(entities=entities), seed=0, run_len=100, warmup=0)
    assert _entity(kpi, "PART", "Avg Cost") == pytest.approx(5.0)
    df = kpi["system"]
    assert float(df.loc[df["Metric"] == "Total Entity Holding Cost", "Value"].iloc[0]) == \
        pytest.approx(24.0)


# ---------------------------------------------------------------- BATCH carrier
def _load_model(carriers, pallets, member_time="0", size=4, max_wait="", capacity="",
                then_unload=False):
    """Carriers (type TRUCK) and pallets into one carrier BATCH. *carriers* and
    *pallets* are CREATE blocks (or lists of them, with their links)."""
    batch = _batch(size=size, carrier_type="TRUCK", member_time=member_time, max_wait=max_wait,
                   carrier_capacity=capacity)
    blocks, links = [batch], []
    for source in carriers + pallets:
        if isinstance(source, tuple):          # (create, assign): a stamped source
            blocks += list(source)
            links += [{"source": source[0]["name"], "target": source[1]["name"]},
                      {"source": source[1]["name"], "target": "batch_1"}]
        else:
            blocks.append(source)
            links.append({"source": source["name"], "target": "batch_1"})
    if then_unload:
        blocks += [_separate(member_time=""), dispose("members"), dispose("carriers")]
        links += [{"source": "batch_1", "target": "separate_1"},
                  {"source": "separate_1", "target": "members"},
                  {"source": "separate_1", "target": "carriers"}]
    else:
        blocks.append(dispose("carriers"))
        links.append({"source": "batch_1", "target": "carriers"})
    return layout(blocks, links)


def _truck(name="trucks", at=0.0, n=1, every=1000.0):
    return create(name, e_type="TRUCK", max_arrivals=n, start_time=at, dist_params={"value": every})


def _pallets(at, n, every=1.0, name="pallets"):
    return create(name, e_type="PALLET", max_arrivals=n, start_time=at, dist_params={"value": every})


def _store(env):
    return env.ctx.store_manager.get("batch_1_batch")


def test_a_carrier_loads_arrivals_until_full():
    model = _load_model([_truck()], [_pallets(1.0, 5)], member_time="0.5", then_unload=True)
    env, _ = run_env(model, 100)
    assert _gone(env, "TRUCK")[0][1] == 4.5
    assert [t for _i, t in _gone(env, "PALLET")] == [4.5] * 4
    assert len(_store(env).items) == 1
    kpi = run_single_simulation(model, seed=0, run_len=100, warmup=0)
    assert _entity(kpi, "PALLET", "Avg Time") == pytest.approx(2.0)


def test_members_waiting_before_the_carrier_leave_the_store_as_their_load_starts():
    model = _load_model([_truck(at=10.0)], [_pallets(0.0, 4)], member_time="1", then_unload=True)
    env, _ = run_env(model, 100)
    assert _gone(env, "TRUCK")[0][1] == 14.0
    assert _store(env).residence.mean == pytest.approx(10.0)
    kpi = run_single_simulation(model, seed=0, run_len=100, warmup=0)
    assert _entity(kpi, "PALLET", "Avg Time") == pytest.approx(12.5)


def test_cut_off_lets_the_load_in_progress_finish_and_starts_no_more():
    pallets = [_pallets(2.0, 1, name="p1"), _pallets(9.5, 1, name="p2"),
               _pallets(10.0, 1, name="p3"), _pallets(12.0, 1, name="p4")]
    model = _load_model([_truck()], pallets, member_time="1", max_wait="10", then_unload=True)
    env, _ = run_env(model, 100)
    assert _gone(env, "TRUCK")[0][1] == 10.5
    assert len(_gone(env, "PALLET")) == 2
    assert len(_store(env).items) == 2


def test_a_member_arriving_at_the_cut_off_instant_is_loaded():
    model = _load_model([_truck()], [_pallets(10.0, 1)], max_wait="10", then_unload=True)
    env, _ = run_env(model, 100)
    assert _gone(env, "TRUCK")[0][1] == 10.0
    assert [t for _i, t in _gone(env, "PALLET")] == [10.0]


def test_an_empty_carrier_leaves_at_its_cut_off():
    model = _load_model([_truck()], [], max_wait="5", then_unload=True)
    model["blocks"].append(_pallets(1000.0, 1))           # a model needs no pallets, but a
    model["links"].append({"source": "pallets", "target": "batch_1"})   # source keeps it legal
    env, _ = run_env(model, 100)
    assert _gone(env, "TRUCK")[0][1] == 5.0
    assert _gone(env, "PALLET") == []


@pytest.mark.parametrize("member_time, leaves", [("1.5", [5.5, 6.5]), ("0", [3.0, 5.0])])
def test_the_oldest_idle_carrier_takes_the_next_member(member_time, leaves):
    model = _load_model([_truck(n=2, every=1.0)], [_pallets(2.0, 4)], member_time=member_time,
                        size=2)
    env, _ = run_env(model, 100)
    trucks = sorted((e for e in env.ctx.all_entities if e.e_type == "TRUCK"), key=lambda e: e.id)
    assert [e.deletion_time for e in trucks] == leaves


def test_a_carrier_that_arrives_part_loaded_takes_only_the_rest():
    blocks = [_parts("first3", n=3, e_type="PALLET"), _batch("pre", size=3, e_type="TRUCK"),
              _batch(size=4, carrier_type="TRUCK"), _pallets(5.0, 3), dispose("carriers")]
    links = [{"source": "first3", "target": "pre"}, {"source": "pre", "target": "batch_1"},
             {"source": "pallets", "target": "batch_1"}, {"source": "batch_1", "target": "carriers"}]
    env, _ = run_env(layout(blocks, links), 100)
    truck = next(e for e in env.ctx.all_entities if e.e_type == "TRUCK")
    assert truck.deletion_time == 5.0 and len(truck.batch_elems) == 4
    assert len(_store(env).items) == 2


def test_each_carrier_has_its_own_capacity():
    carriers = [(_truck("c1"), _assign("cap2", capacity=2)),
                (_truck("c2", at=2.5), _assign("cap3", capacity=3))]
    model = _load_model(carriers, [_pallets(1.0, 5)], capacity="entity.capacity")
    env, _ = run_env(model, 100)
    trucks = sorted((e for e in env.ctx.all_entities if e.e_type == "TRUCK"), key=lambda e: e.id)
    assert [(e.deletion_time, len(e.batch_elems)) for e in trucks] == [(2.0, 2), (5.0, 3)]


def test_a_capacity_of_zero_leaves_at_once_and_below_zero_fails_by_code():
    model = _load_model([(_truck(at=3.0), _assign("cap0", capacity=0))], [_pallets(1.0, 2)],
                        capacity="entity.capacity")
    env, _ = run_env(model, 100)
    assert _gone(env, "TRUCK")[0][1] == 3.0
    model = _load_model([(_truck(), _assign("capneg", capacity=-1))], [_pallets(1.0, 2)],
                        capacity="entity.capacity")
    with pytest.raises(Exception, match="E_BATCH_CARRIER_CAPACITY"):
        run_env(model, 100)


def test_a_plain_batch_is_unchanged():
    blocks = [_parts(n=4), _batch(size=2), dispose()]
    env, _ = run_env(layout(blocks, chain(*blocks)), 10)
    assert len(_gone(env, "TRUCK")) == 2


# ---------------------------------------------------------------- validation
def _codes(**separate):
    return problem_codes(_unload_model(**separate))


def test_validation_codes():
    assert "E_SEPARATE_CARRIER" in _codes(carrier="truck")
    assert "E_SEPARATE_RELEASE_ORDER" in _codes(release_order="random")
    assert not _codes() & {"E_SEPARATE_CARRIER", "E_SEPARATE_CARRIER_LINK",
                           "E_SEPARATE_RELEASE_ORDER", "E_SEPARATE_MEMBER_TIME"}
    one_link = _unload_model()
    one_link["links"] = [link for link in one_link["links"] if link["target"] != "carriers"]
    one_link["blocks"] = [b for b in one_link["blocks"] if b["name"] != "carriers"]
    assert "E_SEPARATE_CARRIER_LINK" in problem_codes(one_link)
    two_links = _unload_model(carrier="dispose", member_time="")
    assert "E_TOO_MANY_OUTPUTS" in problem_codes(two_links)
    timed = _unload_model(carrier="dispose", member_time="2")
    assert "E_SEPARATE_MEMBER_TIME" in problem_codes(timed)
    blocks = [_parts(), _batch(member_time="1"), dispose()]
    assert "E_BATCH_MEMBER_TIME" in problem_codes(layout(blocks, chain(*blocks)))
    blocks = [_parts(), _batch(carrier_capacity="3"), dispose()]
    assert "E_BATCH_MEMBER_TIME" in problem_codes(layout(blocks, chain(*blocks)))


def test_a_plant_log_names_the_carrier_a_member_was_loaded_onto():
    """D461: no batch is created after a carrier load, so the absorption names the carrier."""
    from nagasim import plant
    model = _load_model([_truck()], [_pallets(1.0, 3)], size=2, then_unload=True)
    recorder = TraceRecorder()
    run_env(model, 20, subscribers=[recorder])
    lines = plant.log_of(recorder.events, plant.scaffold(model))
    truck = next(e.entity_id for e in recorder.events if e.e_type == "TRUCK")
    merged = [line["value"]["batch"] for line in lines if line["value"].get("at") == "+"]
    assert merged == [str(truck)] * 2


# ---------------------------------------------------------------- state in and out
def test_a_state_with_a_truck_mid_unload_or_mid_load_is_refused_by_name():
    model = _unload_model()
    model["simulation"] = {"time_limit": 100, "warmup": 0, "replications": 1}
    with pytest.raises(state.StateError) as caught:
        state.take(model, 3.0)
    assert "a carrier loading or unloading" in caught.value.message
    model = _load_model([_truck()], [_pallets(1.0, 5)], member_time="0.5")
    model["simulation"] = {"time_limit": 100, "warmup": 0, "replications": 1}
    with pytest.raises(state.StateError) as caught:
        state.take(model, 1.25)
    assert "a carrier loading or unloading" in caught.value.message


def test_an_initial_carrier_at_a_carrier_batch_is_refused_and_a_kept_one_needs_no_members():
    model = _load_model([_truck()], [_pallets(1.0, 5)])
    model["initial_state"] = {"entities": [{"block": "batch_1", "count": 1, "e_type": "TRUCK"}]}
    assert "E_INITIAL_CARRIER" in problem_codes(model)
    model["initial_state"] = {"entities": [{"block": "batch_1", "count": 2, "e_type": "PALLET"}]}
    assert "E_INITIAL_CARRIER" not in problem_codes(model)
    blocks = [create("trucks", e_type="TRUCK", max_arrivals=1), block("wait", "DELAY", {
        "distribution": "Constant", "dist_params": {"value": 1.0}}), _separate(),
        dispose("members"), dispose("carriers")]
    links = chain(*blocks[:4]) + [{"source": "separate_1", "target": "carriers"}]
    model = layout(blocks, links)
    model["initial_state"] = {"entities": [{"block": "wait", "count": 1, "e_type": "TRUCK"}]}
    assert "E_INITIAL_BATCH" not in problem_codes(model)
    model["blocks"][2]["params"]["carrier"] = "dispose"
    model["links"] = chain(*blocks[:4])
    assert "E_INITIAL_BATCH" in problem_codes(model)


def test_a_carrier_unloading_at_a_keep_separate_is_an_entity_of_the_block():
    """Review R3: the truck sits at the SEPARATE for n x member_time, so the block lists it
    as present (WIP, the debugger's 'entities here') until it leaves on link 1."""
    from nagasim.core.sim_runner import setup_run, step_until
    from conftest import create, dispose, layout
    blocks = [create("c", max_arrivals=1, entities_per_arrival=2, dist_params={"value": 0.0}),
              {"name": "b", "description": "b", "type": "BATCH", "position": [1, 0],
               "params": {"batch_size": 2, "batched_entity_type": "LOAD"}},
              {"name": "sep", "description": "sep", "type": "SEPARATE", "position": [2, 0],
               "params": {"carrier": "keep", "member_time": "2"}},
              dispose("dm"), dispose("dc")]
    links = [{"source": "c", "target": "b"}, {"source": "b", "target": "sep"},
             {"source": "sep", "target": "dm"}, {"source": "sep", "target": "dc"}]
    env, model = setup_run(layout(blocks, links), seed=0, run_len=10, warmup=0.0)
    sep = model.blocks["sep"]
    step_until(env, 1)
    assert len(sep.active_entity) == 1                   # the carrier, mid-unload
    step_until(env, 5)
    assert len(sep.active_entity) == 0                   # gone on link 1 at t=4
