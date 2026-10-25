"""ModelDocument tests (UI_DESIGN.md M4a). Pure Python, no Qt."""
import json
from pathlib import Path

import pytest

from nagasim import api
from nagasim.core.document import ModelDocument
from nagasim.core.quantities import fill_units
from nagasim.core.schema import CURRENT_VERSION

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"
CURATED_EXAMPLES = ["single_server.json", "decide_routing.json", "hold_signal_flow.json"]


def test_empty_document_has_default_settings_and_no_blocks():
    doc = ModelDocument()
    assert doc.blocks == {}
    assert doc.links == []
    assert doc.resources == []
    assert doc.resource_sets == []
    assert doc.entities == []
    assert doc.schedules == []
    assert doc.failures == []
    assert doc.scenarios == []
    assert doc.annotations == []
    assert doc.submodels == {}
    assert doc.simulation == {"time_limit": None, "warmup": 0, "replications": 1, "time_unit": "minutes",
                              "length_unit": "meters", "currency": "EUR", "energy_price": 0}
    assert doc.tables == []
    assert doc.sequences == []
    assert doc.dirty is False
    assert doc.path is None
    assert doc.to_layout()["schema_version"] == CURRENT_VERSION


# -- add / remove / rename -----------------------------------------------

def test_add_block_auto_names_and_defaults_params_from_registry():
    doc = ModelDocument()
    name = doc.add_block("CREATE", (10, 20))
    assert name == "create_1"
    block = doc.blocks[name]
    assert block.type == "CREATE"
    assert block.description == name
    assert block.position == (10.0, 20.0)
    assert block.params["max_arrivals"] == 0
    assert block.params["e_type"] == "DEFAULT"
    assert doc.dirty is True


def test_add_block_rejects_unknown_type():
    doc = ModelDocument()
    with pytest.raises(ValueError):
        doc.add_block("NOT_A_TYPE", (0, 0))


def test_add_block_auto_naming_reuses_smallest_free_number():
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0))  # create_1
    doc.add_block("CREATE", (0, 0))  # create_2
    doc.remove_blocks(["create_1"])
    reused = doc.add_block("CREATE", (0, 0))
    assert reused == "create_1"


def test_remove_blocks_also_removes_their_links():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    doc.remove_blocks([a])
    assert a not in doc.blocks
    assert doc.links == []


def test_remove_items_mixed_delete_is_one_undo_step():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DELAY", (1, 0))
    c = doc.add_block("DISPOSE", (2, 0))
    doc.add_link(a, b)
    doc.add_link(b, c)
    before = doc.to_layout()
    depth = len(doc._undo_stack)

    doc.remove_items(blocks=[b], links=[(a, b)])  # (a, b) is dropped anyway with b

    assert b not in doc.blocks
    assert doc.links == []
    assert len(doc._undo_stack) == depth + 1  # one snapshot for the whole mixed delete

    doc.undo()
    assert doc.to_layout() == before


def test_remove_blocks_no_names_present_is_a_noop():
    doc = ModelDocument()
    before_undo_depth = len(doc._undo_stack)
    doc.remove_blocks(["does_not_exist"])
    assert len(doc._undo_stack) == before_undo_depth
    assert doc.dirty is False


def test_rename_block_updates_links_and_rejects_bad_names():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)

    doc.rename_block(a, "start")
    assert "start" in doc.blocks
    assert a not in doc.blocks
    assert doc.links == [("start", b)]

    with pytest.raises(ValueError):
        doc.rename_block("start", "")
    with pytest.raises(ValueError):
        doc.rename_block("start", b)  # already taken
    with pytest.raises(ValueError):
        doc.rename_block("missing", "x")


# -- links: rules, order, reorder -----------------------------------------

def test_add_link_rules():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))

    with pytest.raises(ValueError):
        doc.add_link(a, a)  # self-link
    with pytest.raises(ValueError):
        doc.add_link(a, "missing")  # unknown block

    doc.add_link(a, b)
    with pytest.raises(ValueError):
        doc.add_link(a, b)  # duplicate


def test_reorder_link_changes_out_links_and_to_layout_order():
    doc = ModelDocument()
    src = doc.add_block("DECIDE", (0, 0))
    t1 = doc.add_block("DISPOSE", (1, 0))
    t2 = doc.add_block("DISPOSE", (2, 0))
    t3 = doc.add_block("DISPOSE", (3, 0))
    doc.add_link(src, t1)
    doc.add_link(src, t2)
    doc.add_link(src, t3)

    doc.reorder_link(src, 0, 2)  # move t1 to the end: t2, t3, t1

    assert doc.out_links(src) == [t2, t3, t1]
    layout_links = [(link["source"], link["target"]) for link in doc.to_layout()["links"]]
    assert [t for s, t in layout_links if s == src] == [t2, t3, t1]


def test_remove_link_is_noop_when_absent():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    depth = len(doc._undo_stack)
    doc.remove_link(a, b)  # never added
    assert len(doc._undo_stack) == depth


# -- undo / redo ------------------------------------------------------------

def test_undo_redo_restore_exact_layouts_across_mixed_operations():
    doc = ModelDocument()
    layout0 = doc.to_layout()

    a = doc.add_block("CREATE", (0, 0))
    layout1 = doc.to_layout()

    b = doc.add_block("DISPOSE", (1, 0))
    layout2 = doc.to_layout()

    doc.add_link(a, b)
    layout3 = doc.to_layout()

    doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 7})
    layout4 = doc.to_layout()

    assert doc.can_undo
    doc.undo()
    assert doc.to_layout() == layout3
    doc.undo()
    assert doc.to_layout() == layout2
    doc.undo()
    assert doc.to_layout() == layout1
    doc.undo()
    assert doc.to_layout() == layout0
    assert not doc.can_undo

    assert doc.can_redo
    doc.redo()
    assert doc.to_layout() == layout1
    doc.redo()
    doc.redo()
    doc.redo()
    assert doc.to_layout() == layout4
    assert not doc.can_redo


def test_noop_mutation_pushes_no_undo_snapshot():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (5, 5))
    depth = len(doc._undo_stack)

    doc.move_blocks({a: (5, 5)})  # same position: no-op
    doc.set_params(a, dict(doc.blocks[a].params))  # identical params: no-op
    doc.set_description(a, doc.blocks[a].description)  # identical text: no-op
    doc.rename_block(a, a)  # identical name: no-op

    assert len(doc._undo_stack) == depth


# -- dirty flag / save / load ------------------------------------------------

def test_dirty_flag_lifecycle_with_save_and_load_roundtrip(tmp_path):
    doc = ModelDocument()
    assert doc.dirty is False
    doc.add_block("CREATE", (0, 0))
    assert doc.dirty is True

    path = tmp_path / "model.json"
    doc.save(path)
    assert doc.dirty is False
    assert doc.path == path

    reloaded = ModelDocument.load(path)
    assert reloaded.dirty is False
    assert reloaded.path == path
    # save writes the units a session's new block left "base" (v28, D159)
    assert reloaded.to_layout() == fill_units(doc.to_layout())


def test_loading_v3_shaped_layout_shifts_positions_once_and_is_stable(tmp_path):
    v3 = {
        "schema_version": 3,
        "blocks": [
            {"name": "create_1", "description": "create_1", "type": "CREATE", "position": [10, 20],
             "params": {"e_type": "DEFAULT", "distribution": "Constant", "dist_params": {"value": 1.0},
                        "entities_per_arrival": 1, "max_arrivals": 0, "start_time": 0}},
            {"name": "dispose_1", "description": "dispose_1", "type": "DISPOSE", "position": [30, 40],
             "params": {}},
        ],
        "links": [["create_1", "dispose_1"]],
        "resources": [], "entities": [], "simulation": {"time_limit": None, "warmup": 0, "replications": 1},
    }
    path = tmp_path / "v3_model.json"
    path.write_text(json.dumps(v3))

    doc = ModelDocument.load(path)
    assert doc.blocks["create_1"].position == (80.0, 60.0)  # (pos + (40, 20)) * 1.5, snapped to 20

    doc.save(path)
    layout_after_first_save = json.loads(path.read_text())

    reloaded = ModelDocument.load(path)
    assert reloaded.blocks["create_1"].position == (80.0, 60.0)  # unchanged: no drift
    reloaded.save(path)
    assert json.loads(path.read_text()) == layout_after_first_save


# -- transaction() : grouped undo ----------------------------------------

def test_transaction_groups_mutations_into_one_undo_step():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    before = doc.to_layout()
    depth = len(doc._undo_stack)

    with doc.transaction():
        doc.add_link(a, b)
        doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 3})

    assert doc.links == [(a, b)]
    assert doc.blocks[a].params["max_arrivals"] == 3
    assert len(doc._undo_stack) == depth + 1

    doc.undo()
    assert doc.to_layout() == before


def test_nested_transaction_is_still_one_undo_step():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    before = doc.to_layout()
    depth = len(doc._undo_stack)

    with doc.transaction():
        with doc.transaction():
            doc.set_description(a, "inner")
        doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 2})

    assert doc.blocks[a].description == "inner"
    assert doc.blocks[a].params["max_arrivals"] == 2
    assert len(doc._undo_stack) == depth + 1

    doc.undo()
    assert doc.to_layout() == before


def test_transaction_exception_restores_pre_transaction_state_and_reraises():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    before = doc.to_layout()
    depth = len(doc._undo_stack)

    with pytest.raises(ValueError):
        with doc.transaction():
            doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 9})
            doc.rename_block(a, "")  # raises: empty name

    assert doc.to_layout() == before  # partial mutation rolled back
    assert len(doc._undo_stack) == depth  # a failed transaction leaves no undo entry


def test_empty_transaction_pushes_no_undo_snapshot():
    doc = ModelDocument()
    depth = len(doc._undo_stack)
    with doc.transaction():
        pass
    assert len(doc._undo_stack) == depth


def test_transaction_delivers_notifications_only_at_exit():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    seen = []
    doc.subscribe(lambda kind, **info: seen.append(kind))

    with doc.transaction():
        doc.add_link(a, b)
        doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 1})
        assert seen == []  # nothing delivered while the transaction is open

    assert seen == ["links", "params"]


# -- labelled undo/redo (U3c) --------------------------------------------

def test_representative_edits_record_default_labels():
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))               # -> "Add block"
    b = doc.add_block("DISPOSE", (1, 0))
    doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 3})
    doc.rename_block(b, "leave")
    doc.remove_blocks(["leave"])
    doc.set_resources([{"name": "R", "capacity": 1, "type": "Resource"}])  # generic set_records
    doc.upsert_resource({"name": "R2", "capacity": 1, "type": "Resource"})  # generic upsert_record

    assert doc.undo_labels() == [
        "Edit resource",           # upsert_resource
        "Edit resources",          # set_resources (bare set_records default)
        "Delete leave",            # remove_blocks
        "Rename leave",            # rename_block
        "Edit create_1",           # set_params
        "Add block",               # add_block dispose_1
        "Add block",               # add_block create_1
    ]


def test_transaction_label_is_used_over_the_inner_default():
    doc = ModelDocument()
    with doc.transaction(label="Paste"):
        doc.add_block("CREATE", (0, 0))
        doc.add_block("DISPOSE", (1, 0))
    assert doc.undo_labels() == ["Paste"]


def test_no_label_defaults_to_edit():
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0))            # -> "Add block"
    with doc.transaction():                    # no label passed -> "Edit"
        doc.add_block("DISPOSE", (1, 0))
    assert doc.undo_labels()[0] == "Edit"
    assert doc.undo_labels()[1] == "Add block"

    doc._push_undo()                           # a direct low-level call, no label
    assert doc.undo_labels()[0] == "Edit"


def test_multi_step_undo_redo_move_exactly_that_many_steps():
    # 5 steps, each with a distinct label so the exact step landed on is
    # unambiguous: add_block, add_block, add_link, rename_block, set_params.
    doc = ModelDocument()
    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    doc.rename_block(b, "leave")
    doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 1})
    final = doc.to_layout()

    label = doc.undo(3)   # undoes set_params, rename_block, add_link (in that order)
    assert label == "Add link create_1 -> dispose_1"   # the LAST of the 3 steps undone
    assert len(doc.blocks) == 2
    assert doc.links == []
    assert "leave" not in doc.blocks
    assert len(doc.redo_labels()) == 3

    label = doc.redo(2)   # redoes add_link, rename_block
    assert label == "Rename leave"
    assert doc.links == [(a, "leave")]
    assert doc.to_layout() != final   # one redo step short of the fully-redone state

    # undo(N) with N larger than the stack only moves what is there, no error
    remaining = len(doc.undo_labels())
    doc.undo(remaining + 10)
    assert not doc.can_undo


def test_undo_redo_cap_matches_the_undo_stack_cap():
    from nagasim.core.document import _MAX_UNDO_DEPTH

    doc = ModelDocument()
    for i in range(_MAX_UNDO_DEPTH + 20):
        doc.add_block("CREATE", (i, 0))
    assert len(doc.undo_labels()) == _MAX_UNDO_DEPTH


def test_newest_undo_step_is_the_same_object_until_another_step_comes_or_it_is_undone():
    from nagasim.core.document import _MAX_UNDO_DEPTH

    doc = ModelDocument()
    assert doc.newest_undo_step() is None
    doc.add_block("CREATE", (0, 0), "a")
    step = doc.newest_undo_step()
    assert step == {**step, "blocks": []}           # the layout from before the edit
    assert doc.newest_undo_step() is step

    doc.add_block("CREATE", (10, 0), "b")           # an edit: another step on top
    assert doc.newest_undo_step() is not step
    doc.undo()                                      # ... undone: the step is the newest again
    assert doc.newest_undo_step() is step

    doc.undo()                                      # the step itself undone
    assert doc.newest_undo_step() is None
    doc.redo()
    assert doc.newest_undo_step() is not step       # a redo pushes a new snapshot

    for i in range(_MAX_UNDO_DEPTH + 5):            # at the cap an edit still changes the newest
        before = doc.newest_undo_step()
        doc.add_block("CREATE", (i, 50))
        assert doc.newest_undo_step() is not before


# -- listeners ----------------------------------------------------------

def test_subscribers_receive_kinds():
    doc = ModelDocument()
    seen = []
    doc.subscribe(lambda kind, **info: seen.append(kind))

    a = doc.add_block("CREATE", (0, 0))
    b = doc.add_block("DISPOSE", (1, 0))
    doc.add_link(a, b)
    doc.set_params(a, {**doc.blocks[a].params, "max_arrivals": 3})
    doc.set_resources([{"name": "R", "capacity": 1, "type": "Resource"}])
    doc.set_simulation(warmup=5)
    doc.undo()

    assert seen == ["blocks", "blocks", "links", "params", "tables", "simulation", "reset"]


# -- annotations (ROADMAP 2.3 part A #4) --------------------------------

def test_add_annotation_auto_ids_and_round_trips_through_to_layout():
    doc = ModelDocument()
    text_id = doc.add_annotation("text", position=[10, 20], text="note", font_size=12, color="#000000")
    frame_id = doc.add_annotation("frame", rect=[0, 0, 100, 50], title="Area", color="#ff0000",
                                  opacity=0.5)
    assert text_id == "text_1"
    assert frame_id == "frame_1"
    assert doc.dirty is True
    layout = doc.to_layout()
    assert layout["annotations"] == [
        {"id": "text_1", "kind": "text", "position": [10, 20], "text": "note",
         "font_size": 12, "color": "#000000"},
        {"id": "frame_1", "kind": "frame", "rect": [0, 0, 100, 50], "title": "Area",
         "color": "#ff0000", "opacity": 0.5},
    ]


def test_add_annotation_rejects_unknown_kind():
    doc = ModelDocument()
    with pytest.raises(ValueError):
        doc.add_annotation("bogus")


def test_add_annotation_reuses_smallest_free_number():
    doc = ModelDocument()
    doc.add_annotation("text", position=[0, 0], text="a")
    second = doc.add_annotation("text", position=[0, 0], text="b")
    doc.remove_annotations([second])
    reused = doc.add_annotation("text", position=[0, 0], text="c")
    assert reused == second


def test_update_annotation_changes_fields_and_is_one_undo_step():
    doc = ModelDocument()
    ann_id = doc.add_annotation("text", position=[0, 0], text="a", font_size=10, color="#000000")
    depth = len(doc._undo_stack)
    doc.update_annotation(ann_id, text="b", position=[5, 5])
    assert len(doc._undo_stack) == depth + 1
    entry = doc.annotations[0]
    assert entry["text"] == "b" and entry["position"] == [5, 5]
    assert entry["font_size"] == 10  # untouched fields survive a partial update

    doc.undo()
    assert doc.annotations[0]["text"] == "a"


def test_update_annotation_unknown_id_raises():
    doc = ModelDocument()
    with pytest.raises(ValueError):
        doc.update_annotation("does_not_exist", text="x")


def test_update_annotation_noop_when_nothing_changes():
    doc = ModelDocument()
    ann_id = doc.add_annotation("text", position=[0, 0], text="a")
    depth = len(doc._undo_stack)
    doc.update_annotation(ann_id, text="a")
    assert len(doc._undo_stack) == depth
    assert doc.dirty is True  # already dirty from add_annotation; noop doesn't re-dirty a clean doc


def test_remove_annotations_drops_matching_ids_and_ignores_unknown():
    doc = ModelDocument()
    keep = doc.add_annotation("text", position=[0, 0], text="keep")
    drop = doc.add_annotation("frame", rect=[0, 0, 10, 10])
    doc.remove_annotations([drop, "does_not_exist"])
    assert [a["id"] for a in doc.annotations] == [keep]


def test_annotations_ignored_by_validation_and_engine():
    # PHASE6 U5 #185: an otherwise-empty model raises no problems at all (the
    # ProblemsPanel shows "Empty model" instead) -- an annotation changes
    # that no more than it changes anything else here, so the doc stays
    # exactly as problem-free with one as without.
    doc = ModelDocument()
    assert doc.problems() == []
    doc.add_annotation("text", position=[0, 0], text="note")
    assert doc.problems() == []


def test_v10_model_file_migrates_and_gains_empty_annotations():
    doc = ModelDocument.load(MODELS_DIR / "single_server.json")
    assert doc.annotations == []
    # v12 -> v13 (ROADMAP 3.4): the twin sections arrive empty, so an older
    # model file behaves exactly as it did.
    assert (doc.data_sources, doc.data_bindings, doc.initial_state) == ([], [], {})
    assert doc.tables == []          # v13 -> v14 (PHASE4 A1), same story
    assert doc.sequences == []       # v14 -> v15 (PHASE4 A2), same story
    # v16 -> v17 (PHASE4 D1/D2): the continuous sections, same story again.
    assert (doc.timeseries_inputs, doc.levels, doc.flows) == ([], [], [])
    assert doc.to_layout()["schema_version"] == CURRENT_VERSION


def test_unsubscribe_stops_notifications():
    doc = ModelDocument()
    seen = []

    def listener(kind, **info):
        seen.append(kind)

    doc.subscribe(listener)
    doc.add_block("CREATE", (0, 0))
    doc.unsubscribe(listener)
    doc.add_block("CREATE", (0, 0))

    assert seen == ["blocks"]


# -- problems() ---------------------------------------------------------

def test_problems_flags_model_with_no_dispose():
    doc = ModelDocument()
    doc.add_block("CREATE", (0, 0))
    problems = doc.problems()
    assert any("no DISPOSE" in p.message for p in problems)


def test_problems_empty_for_curated_examples():
    for filename in CURATED_EXAMPLES:
        doc = ModelDocument.load(MODELS_DIR / filename)
        non_warnings = [p for p in doc.problems() if p.severity != "warning"]
        assert non_warnings == [], filename


# -- API-built document runs through nagasim.api -----------------------------

def test_document_built_via_api_runs_through_run_model():
    doc = ModelDocument()
    create = doc.add_block("CREATE", (0, 0))
    delay = doc.add_block("DELAY", (100, 0))
    dispose = doc.add_block("DISPOSE", (200, 0))
    doc.add_link(create, delay)
    doc.add_link(delay, dispose)
    doc.set_params(create, {**doc.blocks[create].params, "max_arrivals": 5})

    results = api.run_model(doc.to_layout(), replications=1, run_length=50)

    assert set(results["system"]["Run"]) == {0}


def test_sequence_helpers_upsert_rename_remove_and_undo_as_one_step():
    doc = ModelDocument()
    doc.upsert_sequence({"name": "Widget", "steps": [{"station": "Mill"}]})
    doc.upsert_sequence({"name": "Widget", "steps": [{"station": "Mill"}, {"station": "Drill"}]})
    assert len(doc.sequences) == 1 and len(doc.sequences[0]["steps"]) == 2

    doc.rename_sequence("Widget", "Gadget")
    assert doc.sequences[0]["name"] == "Gadget"
    doc.undo()
    assert doc.sequences[0]["name"] == "Widget"

    doc.remove_sequence("Widget")
    assert doc.sequences == []


# -- parallel links between one block pair (PHASE4 T4, ARCHITECTURE D77) ------

def _decide_to_one_dispose(mode="Chance", **params):
    """CREATE -> DECIDE -> the SAME DISPOSE twice: two branches, one target."""
    doc = ModelDocument()
    create = doc.add_block("CREATE", (0, 0))
    decide = doc.add_block("DECIDE", (100, 0))
    out = doc.add_block("DISPOSE", (200, 0))
    doc.add_link(create, decide)
    first = doc.add_link(decide, out)
    second = doc.add_link(decide, out)
    doc.set_params(create, {**doc.blocks[create].params, "max_arrivals": 40,
                            "distribution": "Constant", "dist_params": {"value": 1.0}})
    doc.set_params(decide, {**doc.blocks[decide].params, "mode": mode, **params})
    return doc, (create, decide, out), (first, second)


def test_two_links_between_one_pair_are_separate_branches_and_the_model_is_valid():
    doc, (_create, decide, out), (first, second) = _decide_to_one_dispose(
        probs=[0.25], conditions=[])
    assert (first, second) == (0, 1)
    assert doc.branch_index(decide, out, index=0) == 0
    assert doc.branch_index(decide, out, index=1) == 1
    assert doc.link_count(decide, out) == 2
    assert doc.problems() == []

    # Each link carries its own transfer delay, and removing one leaves the other.
    doc.set_link_delay(decide, out, {"distribution": "Constant", "dist_params": {"value": 2.0}},
                       index=1)
    assert doc.link_delay(decide, out, index=0) is None
    assert doc.link_delay(decide, out, index=1)["dist_params"] == {"value": 2.0}
    doc.remove_link(decide, out, index=0)
    assert doc.link_count(decide, out) == 1
    assert doc.link_delay(decide, out)["dist_params"] == {"value": 2.0}
    doc.undo()
    assert doc.link_count(decide, out) == 2 and doc.link_delay(decide, out, index=0) is None

    # A single-output block still refuses the same connection twice.
    plain = ModelDocument()
    a, b = plain.add_block("DELAY", (0, 0)), plain.add_block("DISPOSE", (100, 0))
    plain.add_link(a, b)
    with pytest.raises(ValueError, match="already exists"):
        plain.add_link(a, b)


def test_a_decide_routes_by_chance_over_two_links_to_the_same_target():
    """Branch order is what binds probs[n] to a link, so branch 1 must take the
    remaining 75% even though both branches end at the same DISPOSE."""
    from nagasim.core.trace import TraceRecorder

    doc, (_create, decide, out), _ = _decide_to_one_dispose(probs=[0.25], conditions=[])
    trace = TraceRecorder()
    api.run_model(doc.to_layout(), replications=1, run_length=60, seed=0, trace=trace)

    branches = [e.branch for e in trace.events
                if e.kind == "entity_moved" and e.block == decide and e.target == out]
    assert len(branches) == 40 and set(branches) == {0, 1}
    assert 4 <= branches.count(0) <= 18        # 25% of 40, sampling slack


def test_round_trip_keeps_parallel_links_in_order_and_an_old_file_is_stable(tmp_path):
    doc, (_create, decide, out), _ = _decide_to_one_dispose(
        mode="Condition", conditions=["entity.priority == 0"], probs=[])
    doc.set_link_delay(decide, out, {"distribution": "Constant", "dist_params": {"value": 3.0}},
                       index=1)
    path = tmp_path / "parallel.json"
    doc.save(path)

    reloaded = ModelDocument.load(path)
    assert reloaded.links == doc.links
    assert reloaded.link_delay(decide, out, index=0) is None
    assert reloaded.link_delay(decide, out, index=1)["dist_params"] == {"value": 3.0}
    # A file without link ids -- every file, since D77 adds no field -- re-saves
    # byte-identical, and so does an existing curated model.
    first = path.read_text()
    reloaded.save(path)
    assert path.read_text() == first
    old = ModelDocument.load(MODELS_DIR / "decide_routing.json")
    out_path = tmp_path / "old.json"
    old.save(out_path)
    once = out_path.read_text()
    ModelDocument.load(out_path).save(out_path)
    assert out_path.read_text() == once


def test_copy_paste_keeps_both_parallel_links():
    from nagasim.core import clipboard

    doc, (create, decide, out), _ = _decide_to_one_dispose(probs=[0.25], conditions=[])
    fragment = clipboard.copy_selection(doc, [create, decide, out])
    assert len(fragment["links"]) == 3
    new_names = clipboard.paste_fragment(doc, fragment)
    assert doc.link_count(new_names[1], new_names[2]) == 2


# -- counter offsets (Phase 13 B1, D755) ------------------------------------

def _counter_doc():
    doc = ModelDocument()
    return doc, doc.add_block("DECIDE", (0, 0))


def test_set_counter_offsets_is_one_undo_step_and_none_clears_a_key():
    doc, name = _counter_doc()
    assert doc.blocks[name].counter_offsets is None
    depth = len(doc._undo_stack)
    doc.set_counter_offsets({name: {"out": (4, -6), "exit:1": (10, 2)}})
    assert len(doc._undo_stack) == depth + 1
    assert doc.blocks[name].counter_offsets == {"out": (4.0, -6.0), "exit:1": (10.0, 2.0)}
    doc.set_counter_offsets({name: {"out": None}})
    assert doc.blocks[name].counter_offsets == {"exit:1": (10.0, 2.0)}
    doc.set_counter_offsets({name: {"exit:1": None}})
    assert doc.blocks[name].counter_offsets is None       # empty collapses to absent
    doc.undo()
    assert doc.blocks[name].counter_offsets == {"exit:1": (10.0, 2.0)}
    doc.undo()
    doc.undo()
    assert doc.blocks[name].counter_offsets is None
    depth = len(doc._undo_stack)
    doc.set_counter_offsets({name: {"out": None}, "ghost": {"out": (1, 1)}})
    assert len(doc._undo_stack) == depth                  # a no-op pushes nothing


def test_counter_offsets_round_trip_and_a_model_without_them_saves_unchanged(tmp_path):
    doc, name = _counter_doc()
    plain = tmp_path / "plain.json"
    doc.save(plain)
    assert "counter_offsets" not in plain.read_text()
    doc.set_counter_offsets({name: {"entry:0": (-8, 5)}})
    path = tmp_path / "counters.json"
    doc.save(path)
    assert json.loads(path.read_text())["blocks"][0]["counter_offsets"] == {"entry:0": [-8.0, 5.0]}
    reloaded = ModelDocument.load(path)
    assert reloaded.blocks[name].counter_offsets == {"entry:0": (-8.0, 5.0)}
    reloaded.set_counter_offsets({name: {"entry:0": None}})
    reloaded.save(path)
    assert path.read_text() == plain.read_text()


def test_copy_paste_carries_counter_offsets():
    from nagasim.core import clipboard

    doc, name = _counter_doc()
    doc.set_counter_offsets({name: {"out": (3, 4)}})
    new_names = clipboard.paste_fragment(doc, clipboard.copy_selection(doc, [name]))
    assert doc.blocks[new_names[0]].counter_offsets == {"out": (3.0, 4.0)}


def test_counter_offsets_through_a_submodel_definition_view():
    doc = ModelDocument()
    inner = doc.add_block("DELAY", (0, 0))
    doc.create_submodel_from_selection([inner], "Cell")
    view = doc.open_definition("Cell")
    (member,) = view.blocks
    view.set_counter_offsets({member: {"out": (1, 2)}})
    assert view.blocks[member].counter_offsets == {"out": (1.0, 2.0)}
    assert inner not in doc.blocks                        # the instance was left alone
