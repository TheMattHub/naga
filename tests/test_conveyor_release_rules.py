"""CONVEYOR release rules beyond the spec cases (D1357): mixed sizes, two slugs in a row, the
hold per part, units, checks, states.

Hand derivations. Typed belt 3.0 m, speed 0.5, cell 0.01, unless said.
- Mixed sizes, belt `gap` 0.09: A 0.41 takes 50 cells (0.50 m), B 0.70 takes 79 (0.79 m), so
  every item length differs from its occupied length. A boards at 0 (next entry 0 + 0.50 / 0.5
  = 1.0), B arrives and boards at 1.0 (next entry 1.0 + 0.79 / 0.5 = 2.58), A arrives at 2.0
  and boards at 2.58. Free at the end: 6.0, 7.0, 8.58.
  Metering gap 0.40, from the tail (item length): A leaves 6.0; B is there at 7.0 and leaves
  6.0 + (0.41 + 0.40) / 0.5 = 7.62 (from the occupied length it would be 7.80); A rides to the
  end by 7.62 + 0.79 / 0.5 = 9.20 and leaves 7.62 + (0.70 + 0.40) / 0.5 = 9.82 (10.00 from the
  occupied length). Holds 0, 0.62, 0.62.
  Interval 0.60: B leaves 6.0 + max(0.60, 0.50) / 0.5 = 7.2; A is at the end by
  7.2 + 1.58 = 8.78 and 7.2 + max(0.60, 0.79) / 0.5 = 8.78: it leaves 8.78, not 7.2 + 1.2 = 8.4.
- Slug count 2, timeout 100, items 0.41 fed every 1.82 s (0, 1.82, 3.64, 5.46): the lead
  stands at 6.0, the second queues at 1.82 + 2.59 / 0.5 = 7.0: both go at 7.0, exits 7.0 and
  7.0 + 0.82 = 7.82 (its free arrival). The third is at the end at 9.64 and leads the next
  slug; the fourth queues at 5.46 + 2.59 / 0.5 = 10.64: exits 10.64 and 11.46.
- Metering gap 0.40 with four 0.41 items fed every 0.82 s (the spec case): each item after the
  first stands 1.62 - 0.82 = 0.80 at the end.
"""
import math

import pytest
from conftest import create, dispose, run_env
from test_conveyor_divert_spec import belt as untyped_belt
from test_conveyor_divert_spec import model, plain_line
from test_conveyor_types import exits_of, sized, typed

from nagasim.core.schema import validate_model_structured

TOL = 1e-9


def _close(got, want):
    assert len(got) == len(want), got
    assert all(math.isclose(g, w, abs_tol=TOL) for g, w in zip(got, want)), (got, want)


def _belt_doc(release, arrivals, feed_gap, **belt):
    conveyor = typed(length=3.0, speed=0.5, **belt)
    conveyor["params"]["release"] = release
    doc = plain_line(conveyor, arrivals=arrivals, gap=feed_gap)
    doc["entities"] = [sized("DEFAULT", 0.41)]
    return doc


def _mixed(release):
    doc = _belt_doc(release, 2, 2.0, gap=0.09)
    doc["blocks"][0] = create(name="c", e_type="A", max_arrivals=2, dist_params={"value": 2.0},
                              start_time=0)
    doc["blocks"].append(create(name="b", e_type="B", max_arrivals=1, start_time=1.0))
    doc["links"][0]["source"] = "c"
    doc["links"].append({"source": "b", "target": "belt"})
    doc["entities"] = [sized("A", 0.41), sized("B", 0.70)]
    env, model = run_env(doc, 100)
    return sorted((e.deletion_time, e.e_type) for e in env.ctx.all_entities), model.blocks["belt"]


def test_metering_gap_reads_the_tail_of_each_size_not_its_occupied_length():
    exits, belt = _mixed({"metering": {"gap": 0.40}})
    assert [t for _t, t in exits] == ["A", "B", "A"]
    _close([t for t, _ in exits], [6.0, 7.62, 9.82])
    _close(list(belt.release_waits.values), [0.0, 0.62, 0.62])


def test_metering_interval_never_nearer_than_the_occupied_length_of_the_part_before():
    exits, _ = _mixed({"metering": {"interval": 0.60}})
    _close([t for t, _ in exits], [6.0, 7.2, 8.78])


def test_two_slugs_in_a_row():
    env, _ = run_env(_belt_doc({"slug": {"count": 2, "timeout": 100.0}}, 4, 1.82), 100)
    _close(exits_of(env), [7.0, 7.82, 10.64, 11.46])


def test_slug_of_one_never_holds():
    """count 1: the lead goes as it stands, the belt of release {}."""
    env, _ = run_env(_belt_doc({"slug": {"count": 1, "timeout": 100.0}}, 4, 0.82), 100)
    _close(exits_of(env), [6.0, 6.82, 7.64, 8.46])


def test_the_hold_per_part_is_the_metering_loss():
    env, model = run_env(_belt_doc({"metering": {"gap": 0.40}}, 4, 0.82), 100)
    _close(exits_of(env), [6.0, 7.62, 9.24, 10.86])
    _close(list(model.blocks["belt"].release_waits.values), [0.0, 0.8, 0.8, 0.8])


def test_lengths_follow_the_length_unit_and_the_timeout_its_time_unit():
    """length 300 cm with a gap of 40 cm is the spec's 3.0 m belt with a 0.40 m gap; a timeout
    of 300 seconds is 5 of the test model's base unit, minutes (the spec's slug timeout case)."""
    doc = _belt_doc({"metering": {"gap": 40}}, 4, 0.82)
    doc["blocks"][1]["params"].update(length=300.0, length_unit="centimeters")
    env, _ = run_env(doc, 100)
    _close(exits_of(env), [6.0, 7.62, 9.24, 10.86])
    slug = {"slug": {"count": 3, "timeout": 300, "time_unit": "seconds"}}
    env, _ = run_env(_belt_doc(slug, 2, 1.82), 100)
    _close(exits_of(env), [11.0, 11.82])


def test_release_is_written_back_only_when_set():
    _env, model = run_env(_belt_doc({}, 1, 1.0), 100)
    assert "release" not in model.blocks["belt"].get_params_dict()
    rule = {"slug": {"count": 3, "timeout": 5.0}}
    _env, model = run_env(_belt_doc(rule, 1, 1.0), 100)
    assert model.blocks["belt"].get_params_dict()["release"] == rule


def _cases(doc):
    return [p.fields["case"] for p in validate_model_structured(doc)
            if p.code == "E_CONVEYOR_RELEASE"]


@pytest.mark.parametrize("release", [
    {"metering": {"gap": 0.4, "interval": 0.6}},
    {"metering": {}},
    {"metering": {"gap": -0.1}},
    {"singulation": {"interval": 0.6}},
    {"slug": {"count": 3}},
    {"slug": {"count": 0, "timeout": 5.0}},
    {"slug": {"count": 2, "timeout": 5.0, "time_unit": "fortnights"}},
    {"pacing": {"gap": 0.4}},
    "metering",
])
def test_a_bad_release_is_refused(release):
    assert _cases(_belt_doc(release, 1, 1.0)) == ["shape"]


def test_good_release_rules_are_no_problem():
    for release in ({}, {"metering": {"gap": 0}}, {"metering": {"interval": 0.6}},
                    {"singulation": {"gap": 0.4}},
                    {"slug": {"count": 2, "timeout": 1, "time_unit": "minutes"}}):
        assert _cases(_belt_doc(release, 1, 1.0)) == [], release


def test_a_release_rule_needs_an_accumulating_belt_without_zones():
    doc = _belt_doc({"metering": {"gap": 0.4}}, 1, 1.0, accumulating=False)
    assert _cases(doc) == ["belt"]
    doc = _belt_doc({"metering": {"gap": 0.4}}, 1, 1.0)
    doc["blocks"][1]["params"]["zones"] = {"count": 2}
    assert _cases(doc) == ["zones"]


def test_a_release_belt_is_refused_in_a_state_by_name():
    from nagasim import state
    doc = _belt_doc({"metering": {"gap": 0.4}}, 1, 1.0)
    params = doc["blocks"][1]["params"]
    del params["type"], params["gap"]
    params["spacing"] = 0.41
    doc["entities"] = []
    doc["simulation"]["time_limit"] = 30
    assert state.refused_kind(doc) == "a conveyor with a release rule"
    doc["initial_state"] = {"clock": 2.0, "entities": [
        {"block": "belt", "count": 1, "belt": {"stage": "ride", "until": 3.0, "entered": 1.0}}]}
    found = [p for p in validate_model_structured(doc) if p.code == "E_INITIAL_BELT"]
    assert [p.fields["case"] for p in found] == ["refuse"]


# Fix round 1 (R25): a slug counts every queued part, behind a point too. Hand derivations,
# 3.0 m at 0.5, items 0.41 fed every 1.82 s (entries 0, 1.82, 3.64), slug 3, timeout 100.
# - Typed belt, front eye at 2.5: the section after the eye holds 50 cells, two items. Item 1
#   stands at 6.0. Item 2's place 2.59 is past the eye: it rides 2.5 -> 3.0 from 6.82 and is
#   queued at 6.82 + 0.09 / 0.5 = 7.0. Item 3's place 2.18 is before the eye: queued on its
#   first ride at 3.64 + 2.18 / 0.5 = 8.0. Released at 8.0: 8.0, 8.82 (catch-up), and item 3
#   reaches the eye at 8.64 = 8.0 + the section lag 0.32 / 0.5, the end at 9.64.
#   (Counting only parts past the last point gave 106.0, 106.82, 207.64.)
# - Untyped belt (spacing 0.41), a stop at 2.5 that dwells 1.0: item 1 dwells 5-6, stands at
#   the end at 7.0. Item 2 reaches the stop at 6.82, dwells to 7.82 (its place 2.59 is past
#   the stop), queued at 7.82 + 0.18 = 8.0. Item 3's place 2.18 is behind the stop: queued at
#   3.64 + 4.36 = 8.0 without the dwell. Released at 8.0 (lead held 1.0): 8.0;
#   item 2 rides from the stop at 7.82 to the end at 8.82 = 8.0 + 0.82; item 3 reaches the
#   stop at 8.64 (section lag (0.82 - 0.5) / 0.5 after item 1 left at 8.0), dwells to 9.64,
#   ends at 10.64.

def _slug_behind(point):
    if point == "eye":
        conveyor = typed(length=3.0, speed=0.5, eyes=[{"at": 2.5, "edge": "front"}])
        entities = [sized("DEFAULT", 0.41)]
    else:
        conveyor = untyped_belt(length=3.0, speed=0.5, spacing=0.41,
                                stops=[{"at": 2.5, "time": 1.0}])
        entities = []
    conveyor["params"]["release"] = {"slug": {"count": 3, "timeout": 100.0}}
    doc = plain_line(conveyor, arrivals=3, gap=1.82)
    doc["entities"] = entities
    env, model = run_env(doc, 300)
    return exits_of(env), model.blocks["belt"]


def test_a_slug_counts_a_part_queued_behind_an_eye():
    exits, belt = _slug_behind("eye")
    _close(exits, [8.0, 8.82, 9.64])
    _close(list(belt.release_waits.values), [2.0, 0.0, 0.0])


def test_a_part_queued_behind_a_stop_counts_and_dwells_after_the_release():
    exits, belt = _slug_behind("stop")
    _close(exits, [8.0, 8.82, 10.64])
    _close(list(belt.release_waits.values), [1.0, 0.0, 0.0])


# Fix round 2: the slug group keeps belt order with merges and diverts. Untyped belt 3.0 m at
# 0.5, spacing 0.41, slug count 2, timeout 100; a place in the queue behind the lead: 2.59.
# Sections between points hold ceil(1.0 / 0.41) = 3 parts. A main part claims a merge from
# 0.41 before it.
# - One merge at 1.0: A boards 0, claims 1.18 -> 2.0, ends 6.0 (lead). S joins at 3.0 (A left
#   the point at 2.0, + 0.82 = 2.82), rides 1.0 -> 3.0 from 3.0: queued 3.0 + 1.59 / 0.5 =
#   6.18. Exits A 6.18, S 6.18 + 0.82 = 7.0.
# - A divert at 1.0 takes S: A1 boards 0, ends 6.0 (lead); S boards 1.82 and is diverted at
#   3.82 (it leaves the group); A2 boards 3.64, passes the divert at 5.64, queued 5.64 +
#   1.59 / 0.5 = 8.82. Exits A 8.82, 9.64; S 3.82. (Left in the group, S would never queue
#   and the slug would time out at 106.)
# - Two merges, S1 at 1.0, S2 at 2.0; A boards 0.
#   S2 at 4.9, S1 at 5.0 and 6.0: A passes merge 1 at 2.0, merge 2 at 4.0, ends 6.0 (lead).
#   S2 joins merge 2 at 4.9 (4.0 + 0.82 = 4.82): queued 4.9 + 0.59 / 0.5 = 6.08; release:
#   A 6.08, S2 6.9. S1a joins merge 1 at 5.0, behind S2 though S2 joined at the other merge;
#   claims merge 2 from 6.18, rides 2.0 -> 3.0 from 7.0, ends 9.0 (lead). S1b joins at 6.0,
#   rides from merge 2 at 8.0: queued 8.0 + 1.18 = 9.18. Exits S1 9.18, 10.0.
#   S2 at 0.0, S1 at 0.2 and 1.2: S2 joins merge 2 at 0, ends 2.0 (lead). S1a joins merge 1 at
#   0.2, AHEAD of A (still at 0.1), rides from merge 2 at 2.2: queued 2.2 + 1.18 = 3.38;
#   release: S2 3.38, S1a 4.2. A claims merge 1 1.18 -> 2.0, merge 2 3.18 -> 4.0, ends 6.0
#   (lead); S1b joins merge 1 at 2.82 behind A, rides from merge 2 at 4.82: queued 6.0.
#   Exits A 6.0, S1b 6.82.

def _slug_line(merges=(), diverts=None, sides=(), main=1, main_gap=1.0):
    conveyor = untyped_belt(length=3.0, speed=0.5, spacing=0.41, merges=list(merges),
                            diverts=diverts)
    conveyor["params"]["release"] = {"slug": {"count": 2, "timeout": 100.0}}
    blocks = [create(name="a", e_type="A", max_arrivals=main, dist_params={"value": main_gap},
                     start_time=0), conveyor, dispose()]
    links = [{"source": "a", "target": "belt"}, {"source": "belt", "target": "dispose_1"}]
    if diverts:
        blocks.append(dispose(name="dside"))
        links.append({"source": "belt", "target": "dside"})
    for name, e_type, starts in sides:
        gap = starts[1] - starts[0] if len(starts) > 1 else 1.0
        blocks.append(create(name=name, e_type=e_type, max_arrivals=len(starts),
                             dist_params={"value": gap}, start_time=starts[0]))
        links.append({"source": name, "target": "belt"})
    env, _ = run_env(model(blocks, links), 300)
    return {e_type: sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == e_type)
            for e_type in {e.e_type for e in env.ctx.all_entities}}


def test_a_slug_with_one_merge():
    exits = _slug_line(merges=[{"at": 1.0, "from": "s"}], sides=[("s", "S", [3.0])])
    _close(exits["A"], [6.18])
    _close(exits["S"], [7.0])


def test_a_part_diverted_off_a_slug_belt_leaves_the_slug():
    exits = _slug_line(diverts=[{"at": 1.0, "condition": "entity.e_type == 'S'"}],
                       sides=[("s", "S", [1.82])], main=2, main_gap=3.64)
    _close(exits["A"], [8.82, 9.64])
    _close(exits["S"], [3.82])


@pytest.mark.parametrize("s1, s2, want", [
    ([5.0, 6.0], [4.9], {"A": [6.08], "S2": [6.9], "S1": [9.18, 10.0]}),
    ([0.2, 1.2], [0.0], {"S2": [3.38], "S1": [4.2, 6.82], "A": [6.0]}),
])
def test_a_slug_with_two_merges_keeps_belt_order(s1, s2, want):
    exits = _slug_line(merges=[{"at": 1.0, "from": "s1"}, {"at": 2.0, "from": "s2"}],
                       sides=[("s1", "S1", s1), ("s2", "S2", s2)])
    for e_type, times in want.items():
        _close(exits[e_type], times)
