"""An order signal and the order verbs (D481 to D489).

The state carries the plant's order book by id; a plant mapping reads it from one row per
order; `hold`, `release`, `expedite` and `resequence` compile to an override of that state and
are compared on the forecast. Spec: docs/superpowers/specs/2026-10-16-order-signal-design.md.
"""
import copy
import csv
import json
from pathlib import Path

import pytest

from nagasim import plant, recommend, state
from nagasim.api import run_model
from nagasim.examples import model_path


def bundled(name):
    return json.loads(open(model_path(name), encoding="utf-8").read())


def order(oid, release, *, due=50, quantity=1, kind="WIDGET", **more):
    return {"id": oid, "release": release, "due": due, "quantity": quantity, "entity_type": kind,
            "sequence": "Fast" if kind == "WIDGET" else "Slow", **more}


def summary(model):
    return run_model(model, replications=1, seed=0)["order_summary"].set_index("Order")


# ---------------------------------------------------------------- the mock MES (D481 to D483)
MES = Path(model_path("mes_twin_shop")).parent / "data" / "mes_twin_shop"
CENTRES = {"Cutting": "cutting", "Drilling": "drilling", "Assembly": "assembly"}
# The spec's example: the mock MES of `mes_twin_shop` mapped with no special case.
MES_MAP = {"format": 1, "time": {"epoch": "2026-09-01T06:00:00Z", "unit": "minutes"}, "signals": [
    {"source": "work_orders", "key": "{id}", "as": "order",
     "fields": {"type": "product", "quantity": "quantity", "done": "quantity_done", "due": "due",
                "priority": "priority", "release": "release", "status": "status"},
     "types": {"WIDGET": "WIDGET", "BRACKET": "BRACKET"},
     "sequences": {"WIDGET": "Widget", "BRACKET": "Bracket"},
     "statuses": {"planned": "waiting", "released": "released", "in progress": "released",
                  "complete": "done", "held": "held"}},
    {"source": "wip", "key": "{id}", "field": "work_centre", "as": "at",
     "blocks": {mes: f"{asset}/work" for mes, asset in CENTRES.items()}, "order_field": "work_order"},
    *({"source": "equipment", "key": mes, "field": "state", "as": "state",
       "resource": f"{asset}/Mfg Routed machine",
       "states": {"running": "busy", "idle": "idle", "down": "down"}} for mes, asset in CENTRES.items())]}


def _cell(text):
    """A CSV cell as a REST answer would carry it: a number where it reads as one."""
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return text


def mes_log():
    """The mock MES's tables as a REST source delivers them: one row a key, stamped by the
    row's own time column (`time` option) or the snapshot's."""
    def rows(name):
        with open(MES / name, encoding="utf-8", newline="") as handle:
            return [{k: _cell(v) for k, v in row.items()} for row in csv.DictReader(handle)]
    taken = rows("snapshot.csv")[0]["taken_at"]
    return ([{"source": "work_orders", "key": r["id"], "value": r, "source_time": taken}
             for r in rows("work_orders.csv")]
            + [{"source": "wip", "key": r["work_order"], "value": r, "source_time": r["arrived"]}
               for r in rows("wip.csv")]
            + [{"source": "equipment", "key": r["id"], "value": r, "source_time": r["since"]}
               for r in rows("equipment.csv")])


def mes_seen(model, log=None):
    mapper = plant.Mapper(MES_MAP, model)
    mapper.feed(plant.in_order(mapper, mes_log() if log is None else log))
    return mapper, dict(mapper.snapshot(), at=mapper.now)


def test_the_mock_mes_maps_onto_the_order_signal_and_starts_the_model():
    """from_plant on mes_twin_shop's mock data: the book is the open orders, the two in
    progress released with their jobs at Assembly, the one running there in service."""
    model = bundled("mes_twin_shop")
    assert plant.check(MES_MAP, model) == []
    mapper, seen = mes_seen(model)
    # The one unit busy at Assembly names no job: synthetic, and inferred below.
    assert [p.code for p in mapper.problems()] == ["W_PLANT_SYNTHETIC"]
    assert seen["at"] == 2880.0      # 2026-09-03T06:00Z
    doc, problems = state.from_plant(model, seen)
    assert [p.code for p in problems if p.severity == "error"] == []
    s = doc["state"]
    book = {o["id"]: o for o in s["order_book"]}
    assert len(book) == 30 and not any(o.get("held") for o in book.values())   # 56 done: left out
    assert book["WO-1058"] == {"id": "WO-1058", "quantity": 1, "entity_type": "WIDGET",
                               "sequence": "Widget", "due": 3050.0, "priority": 0,
                               "release": 2900.0}
    assert s["orders"] == {"WO-1056": {"created": 1, "completed": 0},
                           "WO-1057": {"created": 1, "completed": 0}}
    jobs = {e["attributes"]["order_id"]: e for e in s["entities"]}
    assert jobs["WO-1056"]["in_service"] == 1 and jobs["WO-1056"]["block"] == "assembly/work"
    assert jobs["WO-1057"]["attributes"] == {"order_id": "WO-1057", "due_date": 3000.0,
                                             "priority": 0, "station": "Assembly",
                                             "sequence": "Bracket", "step": 2,
                                             "operation": "assemble", "process_time": 35.0}
    assert jobs["WO-1057"]["e_type"] == "BRACKET"
    assert "arrivals" not in {p.message.split("about ")[-1].split(":")[0] for p in problems}
    started, refused = state.start(model, doc, until=2880.0 + 480)
    assert started is not None, refused
    orders = summary(started)
    # Released in the forecast at their own times, one by one; nothing released twice.
    assert orders.loc["WO-1058", "Created"] == 1 and orders.loc["WO-1056", "Created"] == 1
    assert orders.loc["WO-1056", "Completed"] == 1 and orders.loc["WO-1085", "Created"] == 0


def test_an_order_row_is_read_whole_or_not_at_all():
    """D482: hostile or broken order rows are dropped and counted; a plain id is the only
    plant text kept; a row gone, or a status meaning gone, forgets the order."""
    model = bundled("mes_twin_shop")
    good = {"id": "WO-1", "product": "WIDGET", "quantity": 2, "quantity_done": 0,
            "due": "2026-09-01T10:00:00Z", "priority": 1, "release": "2026-09-01T07:00:00Z",
            "status": "planned"}

    def told(*rows):
        log = [{"source": "work_orders", "key": key, "value": row,
                "source_time": "2026-09-01T06:00:00Z"} for key, row in rows]
        mapper, seen = mes_seen(model, log)
        return mapper.report()["counts"], {o["id"]: o for o in seen["orders"]}
    counts, book = told(("WO-1", good))
    assert book["WO-1"]["release"] == 60.0 and book["WO-1"]["due"] == 240.0
    for bad in ({"quantity": 0}, {"quantity": 2.5}, {"quantity_done": -1}, {"due": "soon"},
                {"release": "1969-01-01T00:00:00Z"}, {"priority": "high"}, {"product": "SPROCKET"},
                {"status": "lost"}, {"status": None}):
        counts, book = told(("WO-1", {**good, **bad}))
        assert book == {} and counts.get("value") == 1, bad
    for ident in ("<b>x</b>", "../etc", "x" * 65, " WO-1"):
        counts, book = told((ident, good))
        assert book == {} and counts.get("value") == 1, ident
    counts, book = told(("WO-1", good), ("WO-1", {**good, "status": "cancelled"}))
    assert book["WO-1"]["status"] == "waiting" and counts.get("value") == 1   # not mapped
    gone = {**MES_MAP, "signals": [{**MES_MAP["signals"][0], "statuses": {
        **MES_MAP["signals"][0]["statuses"], "cancelled": "gone"}}]}
    mapper = plant.Mapper(gone, model)
    mapper.feed([{"source": "work_orders", "key": "WO-1", "value": row,
                  "source_time": f"2026-09-01T06:0{i}:00Z"}
                 for i, row in enumerate((good, {**good, "status": "cancelled"}))])
    assert mapper.snapshot()["orders"] == []


def test_a_mapping_reads_one_order_book_with_its_status_and_the_models_routings():
    model = bundled("mes_twin_shop")
    signal = MES_MAP["signals"][0]

    def codes(*signals):
        return {p.code for p in plant.check({**MES_MAP, "signals": list(signals)}, model)}
    assert codes(signal, signal) == {"E_PLANTMAP_DOUBLE"}
    assert codes({**signal, "sequences": {"WIDGET": "Nowhere"}}) == {"E_PLANTMAP_NAME"}
    for bad in ({"fields": {"type": "product"}}, {"fields": {"colour": "c", "status": "s"}},
                {"statuses": {"planned": "maybe"}}, {"key": "orders"},
                {"statuses": None}, {"fields": None}):
        assert codes({**signal, **bad}) == {"E_PLANTMAP_FORMAT"}, bad
    assert codes({**MES_MAP["signals"][1], "order_field": 3}) == {"E_PLANTMAP_FORMAT"}


# ---------------------------------------------------------------- the book (D484, D485)
@pytest.mark.parametrize("name, at", [("mes_twin_shop", 100.5), ("mes_twin_shop", 300.5),
                                      ("mes_twin_shop", 450.5), ("order_due_dates", 3.5),
                                      ("order_due_dates", 7.5)])
def test_a_state_with_orders_is_taken_again_the_same_with_and_without_a_book(name, at):
    """The round trip, exact: taken, started, taken again at the clock, the same hash; and
    the model's own orders carried as the state's book change nothing."""
    model = bundled(name)
    doc = state.take(model, at)
    started, _ = state.start(model, doc)
    assert state.take(started, at)["hash"] == doc["hash"]
    booked = state.document(model, {**doc["state"], "order_book": model["orders"]}, source="run",
                            at=at, seed=0)
    started_booked, problems = state.start(model, booked)
    assert started_booked is not None, problems
    assert state.take(started_booked, at)["hash"] == booked["hash"]
    plain, with_book = summary(started), summary(started_booked)
    assert plain.equals(with_book)


def test_a_book_replaces_the_models_orders_and_releases_by_identity():
    """D484, D485: the book's orders are the run's; one with a released counter is not
    released again even when an earlier one is still waiting, and a held one never is."""
    model = bundled("order_due_dates")
    book = [order("A-1", 0, due=40), order("A-2", 4, due=40), order("A-3", 1, due=40),
            order("A-4", 0, due=40, held=True)]
    model["initial_state"] = {"clock": 2.0, "order_book": book,
                              "orders": {"A-2": {"created": 1, "completed": 1, "completed_at": 1.5}}}
    orders = summary(model)
    assert sorted(orders.index) == ["A-1", "A-2", "A-3", "A-4"]
    assert orders.loc["A-2", "Created"] == 1 and orders.loc["A-2", "Completion Time"] == 1.5
    assert orders.loc["A-4", "Created"] == 0                # held: never released
    # A-1 (release 0, past) and A-3 (release 1, past) go at once, A-1 first: cut 2-4, 4-6.
    assert (orders.loc["A-1", "Completion Time"], orders.loc["A-3", "Completion Time"]) == (5.0, 7.0)


def test_a_book_is_judged_as_the_orders_section_at_its_own_path():
    model = bundled("order_due_dates")
    model["initial_state"] = {"order_book": [order("A-1", 0, sequence="Nowhere"),
                                             order("A-1", 0, held="yes")]}
    started, problems = state.start(model, state.document(model, model.pop("initial_state")))
    assert started is None
    found = {(p.code, p.path) for p in problems}
    assert ("E_SEQUENCE_UNKNOWN", "/initial_state/order_book/0/sequence") in found
    assert ("E_ORDER_ID", "/initial_state/order_book/1/id") in found
    assert ("E_INITIAL_ORDERS", "/initial_state/order_book") in found


# ---------------------------------------------------------------- the verbs (D486 to D488)
def ranked_shop():
    """The order shop with its Cutter ranked on priority, and two orders released later."""
    model = bundled("order_due_dates")
    model["resources"][0].update(discipline="LowestValueFirst", ranking_expr="entity.priority")
    for entry in model["orders"]:
        entry["priority"] = 1
    model["orders"] += [order("WO-4", 5, due=30, priority=1), order("WO-5", 6, due=30, priority=1)]
    return model


def started_at(model, at):
    started, problems = state.start(model, state.take(model, at))
    assert started is not None, problems
    return started


def act(verb, target, value):
    return {"verb": verb, "target": target, "value": value}


def done(model, oid):
    orders = summary(model)
    return orders.loc[oid, "Created"], orders.loc[oid, "Completion Time"]


def test_each_order_verb_changes_the_forecast_as_it_is_compiled():
    """At 0.5 WO-1 is cut, WO-2 (two jobs) and WO-3 wait, WO-4 and WO-5 come at 5 and 6."""
    started = started_at(ranked_shop(), 0.5)

    def after(*action):
        return recommend.applied(started, act(*action))
    base = summary(started)
    assert list(base["Created"]) == [1, 2, 1, 1, 1]
    # hold: never released
    assert done(after("hold", "WO-4", True), "WO-4")[0] == 0
    # release: now, not at 6, so it is cut before WO-4 (released at 5)
    assert done(after("release", "WO-5", True), "WO-5")[1] < base.loc["WO-5", "Completion Time"]
    # resequence: WO-5 takes WO-4's time (5), WO-4 takes WO-5's (6)
    swapped = after("resequence", "WO-5", "WO-4")
    assert {o["id"]: o["release"] for o in swapped["initial_state"]["order_book"]}["WO-4"] == 6.0
    assert done(swapped, "WO-5")[1] < done(swapped, "WO-4")[1]
    assert base.loc["WO-4", "Completion Time"] < base.loc["WO-5", "Completion Time"]
    # expedite: WO-3's job waiting at Cut is ranked first when WO-1 is done (cut 2-5, pack 5-7)
    expedited = after("expedite", "WO-3", 0)
    assert done(expedited, "WO-3") == (1, 7.0) and base.loc["WO-3", "Completion Time"] == 11.0
    # Compiled from the state's own values: the run's state, the model's book in model time.
    book = after("hold", "WO-4", True)["initial_state"]["order_book"]
    assert [o["id"] for o in book] == [o["id"] for o in ranked_shop()["orders"]]
    assert [o.get("held", False) for o in book] == [False, False, False, True, False]


def test_an_order_verb_is_compared_on_the_forecast_like_any_candidate():
    started = started_at(ranked_shop(), 0.5)
    actions = [act("expedite", "WO-3", 0), act("hold", "WO-3", True), act("hold", "WO-4", True)]
    rows, reason = recommend.effects(started, actions, kpi="order/WO-3/Completion Time",
                                     better="lower", horizon=40.0, replications=2, seed=0)
    by = {(r["action"]["verb"], r["action"]["target"]): r for r in rows}
    assert reason is None and by["expedite", "WO-3"]["verdict"] == "better"
    assert by["expedite", "WO-3"]["effect"] == -4.0
    assert by["hold", "WO-3"]["verdict"] == "failed"         # released: refused by the state
    assert by["hold", "WO-4"]["verdict"] == "no_effect"


def test_an_order_verb_the_model_or_the_state_cannot_answer_is_refused_by_name(tmp_path):
    """D487. At load: by the model; at a comparison: by the state."""
    def refused(action, model):
        with pytest.raises(recommend.Refused) as caught:
            recommend.override_of(action, model) if "initial_state" in model \
                else recommend.check_action(action, model)
        return caught.value.message

    shop = bundled("mes_twin_shop")         # FIFO everywhere: nothing reads a priority
    assert "nothing in the model reads entity.priority" in refused(act("expedite", "WO-1", 0), shop)
    assert recommend.check_action(act("hold", "WO-9999", True), shop)    # the order: the state's
    single = bundled("single_server")
    assert "releases no orders" in refused(act("hold", "WO-1", True), single)
    for bad in (act("hold", "WO-1", False), act("release", "WO-1", 1),
                act("expedite", "WO-1", "high"), act("expedite", "WO-1", 1e10),
                act("resequence", "WO-1", 3), act("hold", "WO\n1", True)):
        with pytest.raises(recommend.Refused):
            recommend.check_action(bad, ranked_shop())
    started = started_at(ranked_shop(), 0.5)
    assert "no order 'WO-9'" in refused(act("hold", "WO-9", True), started)
    for action in (act("hold", "WO-1", True), act("release", "WO-2", True),
                   act("resequence", "WO-4", "WO-3"), act("resequence", "WO-3", "WO-4")):
        assert "released" in refused(action, started), action
    later = started_at(ranked_shop(), 3.5)          # WO-1 is complete by 3
    assert "complete" in refused(act("expedite", "WO-1", 0), later)
    tied = ranked_shop()
    tied["orders"][4]["release"] = 5
    assert "at one time" in refused(act("resequence", "WO-5", "WO-4"), started_at(tied, 0.5))
    held = recommend.applied(started, act("hold", "WO-4", True))
    assert "held" in refused(act("resequence", "WO-4", "WO-5"), held)


def test_an_order_verb_migrates_and_flattens_the_model_once(monkeypatch):
    """A candidate's cost with a big book: one `_flat` per override, not three."""
    started = started_at(ranked_shop(), 0.5)
    calls = []
    flat = recommend._flat
    monkeypatch.setattr(recommend, "_flat", lambda model: calls.append(1) or flat(model))
    recommend.override_of(act("expedite", "WO-3", 0), started)
    assert len(calls) == 1


def test_an_effect_the_state_already_shows_is_no_change():
    """D488: a taken hold, expedite or resequence stands harmlessly once the plant did it."""
    started = started_at(ranked_shop(), 0.5)
    for action in (act("hold", "WO-4", True), act("expedite", "WO-3", 0),
                   act("resequence", "WO-4", "WO-5")):
        once = recommend.applied(started, action)
        assert recommend.applied(once, action)["initial_state"] == once["initial_state"]


# ---------------------------------------------------------------- the twin, end to end
def test_a_twin_on_the_mock_mes_recommends_order_verbs_and_they_reproduce(tmp_path):
    """The headline: mes_twin_shop's twin reads the mock MES, starts its forecast from the
    plant's book, compares order verbs on it, records the one that wins, and the record
    reproduces from its stored state and seed."""
    from nagasim import monitor, outbox, twin

    model = bundled("mes_twin_shop")
    wanted = [act("release", "WO-1060", True), act("hold", "WO-1058", True),
              act("resequence", "WO-1061", "WO-1059"), act("hold", "WO-1056", True)]
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"format": 1, "kind": "naga-candidates",
                                "kpi": "order/WO-1060/Completion Time", "better": "lower",
                                "candidates": wanted}), encoding="utf-8")
    expedite = dict(json.loads(path.read_text()), candidates=[act("expedite", "WO-1060", 0)])
    (tmp_path / "expedite.json").write_text(json.dumps(expedite), encoding="utf-8")
    with pytest.raises(recommend.Refused, match="entity.priority"):
        recommend.load(tmp_path / "expedite.json", model)

    class Clock:
        wall, mono = 1_788_000_000.0, 0.0

    clock = Clock()
    feed = twin.LogFeed(mes_log())
    source = twin.PlantSource(model, MES_MAP, feed, name="shop", clock=feed.clock)
    advisor = recommend.Advisor(
        recommend.load(path, model), model=model, horizon=480.0, name="shop", mapping=MES_MAP,
        out_dir=tmp_path / "out", sink=outbox.open_sink(f"file:{tmp_path / 'q.jsonl'}"),
        wall=lambda: clock.wall, monotonic=lambda: clock.mono)
    cycle = twin.Cycle(source, clock=lambda: clock.wall, monotonic=lambda: clock.mono,
                       out_dir=tmp_path / "out", horizon=480.0, replications=4, seed=3,
                       forecast_every=1e9, advisor=advisor)
    doc = cycle.step()
    assert doc["forecast"]["status"] == "ok", doc["problems"]
    items = {monitor.action_text(i["action"]): i for i in doc["recommend"]["items"]}
    won = items["release WO-1060"]
    assert (won["verdict"], won["rank"], won["status"]) == ("better", 1, "delivered")
    assert won["effect"] < 0 and won["high"] < 0
    assert items["hold WO-1056"]["verdict"] == "failed"         # released: refused
    assert items["resequence WO-1061 before WO-1059"]["verdict"] in ("no_effect", "better", "worse")
    (record,) = [r for r in map(json.loads, (tmp_path / "q.jsonl").read_text().splitlines())
                 if r["action"] == wanted[0]]
    assert recommend.check(record)
    lines, differences = recommend.reproduce(tmp_path / "out", model)
    assert differences == 0 and sum("reproduced" in line for line in lines) >= 1, lines
