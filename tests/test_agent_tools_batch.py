"""Plan F3 Task 2: the agent tool `apply_edits`, written before the tool (D1451-D1454).

One call applies many model edits as ONE undo step and validates the model twice (before and
after) instead of twice per edit, which is what made a 2,000-piece build quadratic. These tests
fix what that must mean: the answer, the refusals before any edit, atomicity, the deferral's
reset, the nested case in the assistant's turn, and the proof that it changes nothing else --
single calls answer as before, and a batch ends in the same model as the same calls one at a time.

The interfaces are imported inside the tests, not at the top, so that until the tool exists each
test fails by itself instead of the whole file failing to collect.
"""
import copy
import dataclasses
import inspect
import json
import re
import shutil
import sys
from contextlib import contextmanager

import pytest

from nagasim import agent_tools as T
from nagasim import examples
from nagasim.agent_tools import core, discover
from nagasim.core.document import EditLocked, ModelDocument

# The edit tools a batch may carry (the plan's Rulings), spelled out here a second time on purpose:
# a change to the list in `batch.py` must be a change to this test too.
BATCHABLE_NAMES = frozenset((
    "add_block update_block remove_blocks connect disconnect reorder_branches set_resources "
    "set_calendar set_entity_types set_simulation_settings set_sd_settings set_model_info "
    "auto_layout place_piece place_feeder place_transfer upsert_record remove_record upsert_node "
    "remove_node upsert_segment remove_segment upsert_scenario").split())


@pytest.fixture
def ws(tmp_path):
    """An isolated workspace per test; the process-wide workspace and the MCP profile are put back
    afterwards, so a failing test cannot leak its models or its tool list into the next one."""
    before_ws, before_exposed = T.workspace(), discover.exposed
    yield T.use_workspace(T.Workspace(tmp_path))
    T.use_workspace(before_ws)
    discover.exposed = before_exposed


def batch_module():
    from nagasim.agent_tools import batch
    return batch


def ok(result):
    assert "error" not in result, result.get("error")
    return result


def new(ws, name="m"):
    ok(T.call("new_model", name=name))
    return ws.document(name)


def apply(model, edits):
    # Without this, a refusal test would pass on "no tool named apply_edits", which is also an error.
    assert "apply_edits" in core.TOOLS, "apply_edits is not registered"
    return T.call("apply_edits", model=model, edits=edits)


def add(name, kind="DISPOSE"):
    return {"tool": "add_block", "args": {"type": kind, "name": name}}


def belt(name, **where):
    return {"tool": "place_piece",
            "args": {"name": name, "type": "belt", "segments": [{"straight": 4.0}], **where}}


def problem_dicts(doc):
    return [p.as_dict() for p in core._problems(doc)]


def counts(doc):
    problems = core._problems(doc)
    return (sum(1 for p in problems if p.severity == "error"),
            sum(1 for p in problems if p.severity == "warning"))


# -- behaviour ----------------------------------------------------------------

def test_batch_builds_and_answers_once(ws):
    doc = new(ws)
    empty = copy.deepcopy(doc.to_layout())
    edits = [add("In", "CREATE"),
             belt("P1", x=0.0, y=0.0, rotation=0.0),
             belt("P2", snap_to="P1.out"),
             belt("P3", snap_to="P2.out"),
             {"tool": "connect", "args": {"source": "In", "target": "P1"}}]
    answer = ok(apply("m", edits))
    assert answer["ok"] is True
    assert answer["applied"] == 5
    assert answer["added"] == ["In", "P1", "P2", "P3"]
    assert (answer["errors"], answer["warnings"]) == counts(doc)
    assert doc.undo_labels()[0] == "Apply 5 edits"
    doc.undo()
    assert not doc.blocks and doc.to_layout() == empty


def test_failing_edit_applies_nothing(ws):
    doc = new(ws)
    ok(T.call("add_block", model="m", type="DISPOSE", name="Keep"))
    layout, labels = copy.deepcopy(doc.to_layout()), doc.undo_labels()
    # The index is the item's position in the list sent, from 0, as an agent counts its own array.
    answer = apply("m", [add("In", "CREATE"),
                         belt("P1", x=0.0, y=0.0, rotation=0.0),
                         belt("P2", snap_to="P1.nope")])
    error = answer["error"]
    assert error["code"] == "E_UNKNOWN_PORT"
    assert error["message"].startswith("edit 2 (place_piece): ")
    assert "has no out port 'nope'" in error["message"]
    assert error["hint"].startswith("nothing was applied; ")
    assert "out ports: out" in error["hint"]
    assert doc.to_layout() == layout
    assert doc.undo_labels() == labels


def _bad_edit_lists():
    other = {"tool": "add_block", "args": {"model": "other", "type": "DISPOSE", "name": "X"}}
    first = add("First")
    return [
        ("empty", [], None),
        ("over the cap", [add("Z")] * 1001, None),
        ("no tool", [first, {"args": {"type": "DISPOSE"}}], None),
        ("args not an object", [first, {"tool": "add_block", "args": [1, 2]}], None),
        ("other model", [first, other], None),
        ("nested", [first, {"tool": "apply_edits", "args": {"edits": [add("Y")]}}], None),
        ("save_model", [first, {"tool": "save_model", "args": {}}], None),
        ("write_model", [first, {"tool": "write_model", "args": {}}], None),
        ("undo", [first, {"tool": "undo", "args": {}}], None),
        ("set_item_code", [first, {"tool": "set_item_code", "args": {}}], None),
        ("csv", [first, {"tool": "upsert_table_from_csv", "args": {}}], None),
        ("set_layout", [first, {"tool": "set_layout", "args": {}}], None),
        ("unknown", [first, {"tool": "no_such_tool", "args": {}}], "E_UNKNOWN_TOOL"),
        # `connect` is a batchable tool the session does not publish (the human's --tools choice).
        ("not published", [first, {"tool": "connect", "args": {"source": "a", "target": "b"}}],
         "E_UNKNOWN_TOOL"),
    ]


BAD_EDITS = _bad_edit_lists()


@pytest.mark.parametrize("label, edits, code", BAD_EDITS, ids=[case[0] for case in BAD_EDITS])
def test_refused_before_any_edit(ws, label, edits, code):
    doc = new(ws)
    new(ws, "other")
    layout, labels = copy.deepcopy(doc.to_layout()), doc.undo_labels()
    with core.offering({"apply_edits", "add_block"} if label == "not published" else None):
        answer = apply("m", edits)
    assert "error" in answer, answer
    if code:
        assert answer["error"]["code"] == code
    if label == "unknown":
        assert answer["error"]["code"] == core.unknown_tool_error("no_such_tool").code
    assert doc.to_layout() == layout
    assert doc.undo_labels() == labels
    assert ws.document("other").to_layout()["blocks"] == []


def test_args_model_same_as_batch(ws):
    doc = new(ws)
    answer = ok(apply("m", [{"tool": "add_block",
                             "args": {"model": "m", "type": "DISPOSE", "name": "Same"}}]))
    assert answer["applied"] == 1 and answer["added"] == ["Same"]
    assert "Same" in doc.blocks


def test_alias_resolved_before_list(ws, monkeypatch):
    doc = new(ws)
    # No shipped alias points at an edit tool, so two are made up: one for a listed tool (must
    # run), one for a tool the list refuses (must not slip through under the old name).
    monkeypatch.setitem(core.ALIASES, "put_end", ("add_block", {"type": "DISPOSE"}))
    monkeypatch.setitem(core.ALIASES, "old_save", ("save_model", {}))
    answer = ok(apply("m", [{"tool": "put_end", "args": {"name": "Via alias"}}]))
    assert answer["added"] == ["Via alias"] and doc.blocks["Via alias"].type == "DISPOSE"
    layout = copy.deepcopy(doc.to_layout())
    for name in ("old_save", "list_sequences"):     # the second is a shipped alias of a read tool
        assert "error" in apply("m", [add("Never"), {"tool": name, "args": {}}])
        assert doc.to_layout() == layout


def test_json_string_args(ws):
    doc = new(ws)
    answer = ok(apply("m", [{"tool": "add_block",
                             "args": json.dumps({"type": "DISPOSE", "name": "From text"})}]))
    assert answer["added"] == ["From text"]
    whole = json.dumps([add("Whole one"), add("Whole two")])    # some MCP clients send it this way
    answer = ok(T.call("apply_edits", model="m", edits=whole))
    assert answer["applied"] == 2 and {"Whole one", "Whole two"} <= set(doc.blocks)


def test_raising_step_rolls_back(ws, monkeypatch):
    doc = new(ws)
    layout, labels = copy.deepcopy(doc.to_layout()), doc.undo_labels()
    doc.edit_lock = "locked by a test"
    with pytest.raises(EditLocked):     # a RuntimeError the tool wrapper does not catch
        apply("m", [add("A"), add("B")])
    doc.edit_lock = None
    assert doc.to_layout() == layout
    assert core._DEFERRED.get() is False
    # The same when the step that raises comes after edits that went through.
    spec = core.TOOLS["connect"]

    def boom(**_):
        raise RuntimeError("boom")
    monkeypatch.setitem(core.TOOLS, "connect", dataclasses.replace(spec, fn=boom))
    with pytest.raises(RuntimeError, match="boom"):
        apply("m", [add("A"), add("B"), {"tool": "connect", "args": {"source": "A", "target": "B"}}])
    assert doc.to_layout() == layout
    assert doc.undo_labels() == labels
    assert core._DEFERRED.get() is False


def test_deferral_reset_after_failure(ws):
    new(ws, "control")
    expected = ok(T.call("add_block", model="control", type="PROCESS", name="X"))
    doc = new(ws)
    assert "error" in apply("m", [add("A"), belt("P", snap_to="A.nope")])
    assert core._DEFERRED.get() is False
    answer = ok(T.call("add_block", model="m", type="PROCESS", name="X"))
    assert answer["introduced"] and answer["introduced"] == expected["introduced"]
    assert (answer["errors"], answer["warnings"]) == counts(doc)


def test_batch_inside_open_turn(ws):
    doc = new(ws)
    with doc.transaction("Turn"):
        ok(T.call("add_block", model="m", type="DISPOSE", name="Earlier"))
        answer = apply("m", [belt("P1", x=0.0, y=0.0, rotation=0.0),
                             belt("P2", snap_to="P1.nope")])
        assert answer["error"]["code"] == "E_UNKNOWN_PORT"
        assert list(doc.blocks) == ["Earlier"]
    assert list(doc.blocks) == ["Earlier"]
    assert doc.undo_labels() == ["Turn"]
    doc.undo()
    assert not doc.blocks


def test_batchable_is_model_edits_only():
    batch = batch_module()
    assert batch.MAX_EDITS == 1000
    assert batch.BATCHABLE == BATCHABLE_NAMES
    # The tools the plan keeps out, by reason: a batch inside a batch, file and document handling,
    # undo, trusted code (D1344), the file-reading importers, and `set_layout`, whose answer
    # carries `dropped_links` a batch answer would hide.
    out = {"apply_edits", "new_model", "open_model", "save_model", "write_model", "undo", "redo",
           "set_item_code", "set_layout", "import_sd_model", "import_event_log",
           "upsert_table_from_csv", "upsert_timeseries_input_from_csv", "upsert_orders_from_csv"}
    assert not out & set(batch.BATCHABLE)
    # What the listed tools answer beside the EDIT keys today (read off their output schemas on
    # 2026-11-25). The brief guessed {block, layout_position, layout_rotation, name}; the real set
    # is these: each is the echo of the edit's own result, none a side effect on anything but the
    # document. A tool that adds another key must be looked at, not silently batched.
    # look_changed and look_skipped (update_block, place_piece, D1476) say which pieces a look
    # write touched; a batch carries them per edit in `looks`, and get_ports reads the same back.
    echo = {"block", "calendar", "entities", "index", "layout_position", "layout_rotation", "link",
            "look_changed", "look_skipped", "moved", "node", "order", "record", "removed",
            "resources", "scenario", "sd", "section", "segment", "simulation"}
    for name in sorted(batch.BATCHABLE):
        spec = core.TOOLS[name]
        assert not spec.read_only, name
        assert "_open(" in inspect.getsource(spec.raw), name
        assert set(spec.output_schema["properties"]) <= set(core.EDIT) | echo, name


def test_batchable_adds_no_io_surface():
    """Security rule (CLAUDE.md): a batch adds no file access and no permission argument. No
    batchable tool writes a path (`assistant.WRITES`) or takes a path/file/CSV argument."""
    from nagasim import assistant
    batch = batch_module()
    assert not set(batch.BATCHABLE) & set(assistant.WRITES)
    file_args = set(assistant.WRITES.values()) | {"path", "file", "csv"}
    for name in sorted(batch.BATCHABLE):
        for prop in core.TOOLS[name].input_schema["properties"]:
            assert prop not in file_args, (name, prop)
            assert not prop.endswith(("_path", "_file", "_csv")), (name, prop)
    # The docstring lists what a batch takes; the list and the code must not drift apart.
    # Whole words both ways: "connect" must not be found inside "disconnect", and a tool a batch
    # refuses must not be named as if it took part (a one-word name only as `name` or name().
    description = core.TOOLS["apply_edits"].description
    assert all(re.search(rf"\b{n}\b", description) for n in batch.BATCHABLE)
    named = {n for n in core.TOOLS
             if re.search(rf"\b{n}\b" if "_" in n else rf"`{n}`|\b{n}\(", description)}
    assert not named - set(batch.BATCHABLE), sorted(named - set(batch.BATCHABLE))
    assert f"{batch.MAX_EDITS:,}" in description and "1,000" in description   # the limit's text


def test_one_validation_per_batch(ws, monkeypatch):
    from nagasim.core import document

    validations = []
    layouts = []
    real_validate = document.schema.validate_model_structured
    real_layout = ModelDocument.to_layout

    def counting_validate(*args, **kwargs):
        validations.append(1)
        return real_validate(*args, **kwargs)

    def counting_layout(self):
        layouts.append(1)
        return real_layout(self)

    new(ws, "ten"), new(ws, "sixty")
    monkeypatch.setattr(document.schema, "validate_model_structured", counting_validate)
    monkeypatch.setattr(ModelDocument, "to_layout", counting_layout)
    seen = {}
    for model, n in (("ten", 10), ("sixty", 60)):
        validations.clear(), layouts.clear()
        ok(apply(model, [add(f"D{i}") for i in range(n)]))
        seen[n] = (len(validations), len(layouts))
    assert seen[10][0] == seen[60][0] == 2      # before and after, not per edit
    assert seen[10][1] == seen[60][1]           # no per-edit snapshot or layout copy either


# -- proof: nothing else changes ------------------------------------------------

@contextmanager
def recording():
    """Collect (tool, kwargs, answer) of every `T.call` made by a builder while it builds one call
    at a time. A plain function, shared with the scale-network batch test of Task 3. The kwargs are
    copied, so a builder reusing a dict later cannot change what was recorded."""
    log = []
    real = T.call

    def recorded(tool_name, /, **kwargs):
        answer = real(tool_name, **kwargs)
        log.append((tool_name, copy.deepcopy(kwargs), copy.deepcopy(answer)))
        return answer

    T.call = recorded
    try:
        yield log
    finally:
        T.call = real


def tool_module(name):
    """A repo `tools/` module; the engine export does not ship the builders, so these tests skip there."""
    return pytest.importorskip(f"tools.{name}")


def build_conveyor():
    build_conveyor_line = tool_module("build_conveyor_line")
    build_conveyor_line.build()


def build_bridge():
    build_bridge_line = tool_module("build_bridge_line")
    build_bridge_line.build_in_workspace()


def build_scale():
    gen_scale_network = tool_module("gen_scale_network")
    gen_scale_network.build(30)


BUILDERS = [pytest.param(build_conveyor, id="conveyor_line"),
            pytest.param(build_bridge, id="bridge_line"),
            pytest.param(build_scale, id="scale_network_30_batched")]


def oracle_pair():
    """The two helpers exactly as they were before this branch (`git show ddda4e34:nagasim/
    agent_tools/core.py`), validating on every call. Names they use are read through `core`, so the
    test's workspace and the problem limit are the live ones. Each counts its calls."""
    calls = {"open": 0, "edit_result": 0}

    def _open(model: str):
        calls["open"] += 1
        doc = core._WORKSPACE.document(model)
        return doc, core._problems(doc)

    def _edit_result(doc, before, extra=None):
        calls["edit_result"] += 1
        after = core._problems(doc)
        before_keys = {p.message for p in before}
        after_keys = {p.message for p in after}
        out = {
            "ok": True,
            "introduced": core._brief_problems(
                [p for p in after if p.message not in before_keys][:core.MAX_PROBLEMS]),
            "fixed": [p.message for p in before if p.message not in after_keys][:core.MAX_PROBLEMS],
            "errors": sum(1 for p in after if p.severity == "error"),
            "warnings": sum(1 for p in after if p.severity == "warning"),
        }
        out.update(extra or {})
        return out

    return _open, _edit_result, calls


@pytest.mark.parametrize("builder", BUILDERS)
def test_single_calls_unchanged(tmp_path, monkeypatch, builder):
    before_ws = T.workspace()
    try:
        T.use_workspace(T.Workspace(tmp_path / "now"))
        (tmp_path / "now").mkdir()
        with recording() as now:
            builder()

        (tmp_path / "then").mkdir()
        T.use_workspace(T.Workspace(tmp_path / "then"))
        _open, _edit_result, calls = oracle_pair()
        # model.py and records.py bind both names with `from .core import`, so patching `core`
        # alone would reach nothing: replace every agent_tools module's own binding.
        originals = {"_open": core._open, "_edit_result": core._edit_result}
        patched = []
        for name, module in list(sys.modules.items()):
            if module is None or not name.startswith("nagasim.agent_tools"):
                continue
            for attr, oracle in (("_open", _open), ("_edit_result", _edit_result)):
                if getattr(module, attr, None) is originals[attr]:
                    monkeypatch.setattr(module, attr, oracle)
                    patched.append((name, attr))
        # core defines them and model and records import them by name: all three, both helpers.
        assert {"nagasim.agent_tools.core", "nagasim.agent_tools.model",
                "nagasim.agent_tools.records"} <= {name for name, _ in patched}
        with recording() as then:
            builder()
    finally:
        T.use_workspace(before_ws)
    # A patch that reached nothing would pass vacuously: the oracle must have run.
    assert calls["open"] > 0 and calls["edit_result"] > 0
    assert len(now) == len(then)
    assert [(tool, answer) for tool, _, answer in now] == [(tool, answer) for tool, _, answer in then]


def batchable_calls(log):
    return [(tool, kwargs, answer) for tool, kwargs, answer in log
            if tool in batch_module().BATCHABLE and "error" not in answer]


# The scale network now builds through the batch itself; its own test is below.
@pytest.mark.parametrize("builder", BUILDERS[:2])
def test_batch_matches_single_calls(tmp_path, builder):
    batch = batch_module()
    before_ws = T.workspace()
    try:
        (tmp_path / "single").mkdir()
        single = T.use_workspace(T.Workspace(tmp_path / "single"))
        with recording() as log:
            builder()
        made = next(kw["name"] for tool, kw, _ in log if tool == "new_model")
        steps = batchable_calls(log)
        assert steps, "the builder made no batchable call"

        (tmp_path / "batched").mkdir()
        batched = T.use_workspace(T.Workspace(tmp_path / "batched"))
        ok(T.call("new_model", name=made))
        edits = [{"tool": tool, "args": {k: v for k, v in kwargs.items() if k != "model"}}
                 for tool, kwargs, _ in steps]
        answer = None
        for start in range(0, len(edits), batch.MAX_EDITS):
            answer = ok(T.call("apply_edits", model=made, edits=edits[start:start + batch.MAX_EDITS]))
        one_by_one, together = single.document(made), batched.document(made)
    finally:
        T.use_workspace(before_ws)
    assert together.to_layout() == one_by_one.to_layout()
    assert problem_dicts(together) == problem_dicts(one_by_one)
    last = steps[-1][2]
    assert (answer["errors"], answer["warnings"]) == (last["errors"], last["warnings"])


def test_scale_network_batched_equals_single(tmp_path):
    """The generator's batched build ends in the layout and the check_layout answer of the same edits
    made one call at a time (replayed from what it sent: the pre-rewrite builder is not kept), and
    it makes at most ceil(edits / MAX_EDITS) + 1 tool calls."""
    gen_scale_network = tool_module("gen_scale_network")
    batch = batch_module()
    before_ws = T.workspace()
    try:
        (tmp_path / "batched").mkdir()
        batched = T.use_workspace(T.Workspace(tmp_path / "batched"))
        with recording() as log:
            gen_scale_network.build(30)
        sent = [e for tool, kw, _ in log if tool == "apply_edits" for e in kw["edits"]]
        assert sent and all(e["tool"] in batch.BATCHABLE for e in sent)
        together_check = ok(T.call("check_layout", model=gen_scale_network.NAME))

        (tmp_path / "single").mkdir()
        single = T.use_workspace(T.Workspace(tmp_path / "single"))
        ok(T.call("new_model", name=gen_scale_network.NAME))
        for e in sent:
            ok(T.call(e["tool"], model=gen_scale_network.NAME, **e["args"]))
        single_check = ok(T.call("check_layout", model=gen_scale_network.NAME))
        one_by_one, together = single.document(gen_scale_network.NAME), batched.document(gen_scale_network.NAME)
    finally:
        T.use_workspace(before_ws)
    assert together.to_layout() == one_by_one.to_layout()
    assert together_check == single_check
    assert len(log) <= -(-len(sent) // batch.MAX_EDITS) + 1


def test_a_batch_across_a_chunk_boundary_builds_the_same_layout(tmp_path, monkeypatch):
    """M14: the generator sends its edits in chunks of MAX_EDITS; with the limit patched to 10 (in
    the module and in the generator, which imported it by name) the build crosses many boundaries
    and ends in the layout and the check_layout answer of the one-chunk build."""
    gen_scale_network = tool_module("gen_scale_network")
    batch = batch_module()
    before_ws = T.workspace()
    try:
        (tmp_path / "whole").mkdir()
        whole = T.use_workspace(T.Workspace(tmp_path / "whole"))
        gen_scale_network.build(30)
        whole_check = ok(T.call("check_layout", model=gen_scale_network.NAME))
        whole_layout = whole.document(gen_scale_network.NAME).to_layout()

        monkeypatch.setattr(batch, "MAX_EDITS", 10)
        monkeypatch.setattr(gen_scale_network, "MAX_EDITS", 10)
        (tmp_path / "chunked").mkdir()
        chunked = T.use_workspace(T.Workspace(tmp_path / "chunked"))
        with recording() as log:
            gen_scale_network.build(30)
        calls = [kw for tool, kw, _ in log if tool == "apply_edits"]
        assert len(calls) > 1 and all(len(kw["edits"]) <= 10 for kw in calls)
        chunked_check = ok(T.call("check_layout", model=gen_scale_network.NAME))
        chunked_layout = chunked.document(gen_scale_network.NAME).to_layout()
    finally:
        T.use_workspace(before_ws)
    assert chunked_layout == whole_layout
    assert chunked_check == whole_check


def test_a_null_args_or_model_is_unset(ws):
    """M6: a client that sends null for what it leaves out is read as the adapter reads it."""
    doc = new(ws)
    answer = ok(apply("m", [{"tool": "add_block", "args": {"type": "DISPOSE", "name": "A", "model": None}},
                            {"tool": "auto_layout", "args": None}]))
    assert answer["applied"] == 2 and "A" in doc.blocks
    assert apply("m", [{"tool": "add_block", "args": {"type": "DISPOSE", "model": "other"}}])["error"]


def first_bundled_with_submodels():
    for name in examples.names():
        layout = json.loads(examples.path(name).read_text(encoding="utf-8"))
        if layout.get("submodels"):
            return name
    raise AssertionError("no bundled model has submodels")


BUNDLED = ["conveyor_line", "job_shop_routed", "sd_supply_game", "first_with_submodels"]


@pytest.mark.parametrize("bundled", BUNDLED)
def test_batch_matches_single_calls_on_bundled(ws, tmp_path, bundled):
    name = first_bundled_with_submodels() if bundled == "first_with_submodels" else bundled
    shutil.copy(examples.path(name), tmp_path / f"{name}.json")
    for handle in ("single", "batched"):
        ok(T.call("open_model", path=f"{name}.json", name=handle))
    one_by_one, together = ws.document("single"), ws.document("batched")
    first = next(iter(one_by_one.blocks))
    source = next(b for b, blk in one_by_one.blocks.items() if blk.type != "DISPOSE")
    edits = [{"tool": "update_block", "args": {"name": first, "description": "F3"}},
             {"tool": "add_block", "args": {"type": "DISPOSE", "name": "F3 end"}},
             {"tool": "connect", "args": {"source": source, "target": "F3 end"}},
             {"tool": "add_block", "args": {"type": "DISPOSE", "name": "F3 spare"}},
             {"tool": "remove_blocks", "args": {"names": ["F3 spare"]}}]
    start = core._problems(one_by_one)
    singles = [T.call(e["tool"], model="single", **e["args"]) for e in edits]
    # Both paths must succeed, or the comparison would be between an error and a success.
    assert all("error" not in s for s in singles), singles
    answer = ok(apply("batched", edits))

    assert together.to_layout() == one_by_one.to_layout()
    assert problem_dicts(together) == problem_dicts(one_by_one)
    expected = core._edit_result(one_by_one, start)
    for key in ("introduced", "fixed", "errors", "warnings"):
        assert answer[key] == expected[key], key
    assert answer["applied"] == 5 and answer["added"] == ["F3 end", "F3 spare"]
