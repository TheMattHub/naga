"""Independent oracle for W7 Q2 "Three-way merge and git drivers" (PHASE6-6D-W7-PLAN, D125).

Hand-derived expected `modeldiff.merge` output and `naga merge` CLI
behaviour, written BEFORE Q2 exists so the failures below are the spec, not
a bug report. All access to the not-yet-built API goes through the small
adapter functions right below the imports -- once Q2 lands, the coordinator
reconciles real names/shapes here in one place instead of at every call site.

Reading choices this oracle bakes in (state per the task brief and the plan's
Q2 rules -- flagged where they are a guess for the coordinator to reconcile):
- `modeldiff.merge(base, ours, theirs) -> (merged, conflicts)`. A conflict
  has `.path`, `.ours`, `.theirs` (the two candidate values; `None` stands
  for "deleted on this side").
- Changed differently on both sides -> one conflict, merged keeps OURS (the
  plan states this explicitly). GUESS: a delete-vs-modify conflict follows
  the same "merged keeps ours" convention (cases 5 and 7 below).
- Moved differently on both sides never conflicts -- ours' position wins.
- Case 7's dangling link (ours deletes a block, theirs adds a link to it) is
  a conflict on the LINK record, not the deleted block -- the merge itself
  creates the invalid reference, so it is a merge-time validation problem,
  not either side's edit taken alone.
"""
import copy

import pytest

from conftest import chain, create, delay, dispose, layout, process


# -- adapters: the one place that knows modeldiff's real call shape --------

def _merge(base, ours, theirs):
    from nagasim import modeldiff
    return modeldiff.merge(base, ours, theirs)


def _conflict_path(c):
    return getattr(c, "path", None)


def _conflict_ours(c):
    return getattr(c, "ours", None)


def _conflict_theirs(c):
    return getattr(c, "theirs", None)


def _run_cli(args):
    from nagasim.cli import main
    return main(args)


def _merge_cli_wired():
    """True once nagasim.cli.main actually dispatches `naga merge` itself.

    Guard, not a feature probe: calling main(["merge", ...]) before that
    dispatch exists falls through to the last branch of main(), which
    prints the help text instead of merging. Checks the COMMANDS registry
    (ARCH-REVIEW-PLAN B4) rather than sniffing main()'s source text as before.
    """
    from nagasim.cli import COMMANDS
    return "merge" in COMMANDS


def _require_cli():
    if not _merge_cli_wired():
        pytest.fail("naga merge is not wired into nagasim.cli.main yet (W7 Q2 pending)")


# -- semantic layout comparison: modeldiff.diff if it exists, else a local -
# -- identity-based normaliser (list order is not semantic, per Q1) --------

def _normalize(doc):
    doc = copy.deepcopy(doc)
    if "blocks" in doc:
        doc["blocks"] = {b["name"]: b for b in doc["blocks"]}
    if "links" in doc:
        doc["links"] = {(l["source"], l["target"]): l for l in doc["links"]}
    for key in ("resources", "resource_sets", "entities", "variables",
                "schedules", "failures"):
        if key in doc:
            doc[key] = {r["name"]: r for r in doc[key]}
    return doc


def _assert_layouts_equal(expected, merged):
    try:
        from nagasim import modeldiff
        has_diff = hasattr(modeldiff, "diff")
    except ImportError:
        has_diff = False
    if has_diff:
        from nagasim import modeldiff
        assert modeldiff.diff(expected, merged) == []
    else:
        assert _normalize(expected) == _normalize(merged)


# -- shared model fixtures (wire format: docs/ARCHITECTURE.md section 7) ---

def _resource(name, capacity=1):
    return {"name": name, "capacity": capacity, "discipline": "FIFO",
            "cost_busy": 0.0, "cost_idle": 0.0, "cost_per_use": 0.0,
            "cost_time_unit": "hours"}


def _write(tmp_path, name, doc):
    import json
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


# -- case 1: only ours changes a PROCESS param ---------------------------

def test_ours_only_changes_process_param():
    c, d = create(), dispose()
    p_base = process(value=1.0)
    base = layout([c, p_base, d], chain(c, p_base, d), resources=[_resource("Server")])

    p_ours = process(value=2.0)
    ours = layout([c, p_ours, d], chain(c, p_ours, d), resources=[_resource("Server")])

    theirs = copy.deepcopy(base)

    merged, conflicts = _merge(base, ours, theirs)
    assert conflicts == []
    _assert_layouts_equal(ours, merged)


# -- case 2: ours changes block A, theirs changes block B ----------------

def test_ours_and_theirs_change_different_blocks():
    c, p, dl, d = create(), process(value=1.0), delay(value=3.0), dispose()
    base = layout([c, p, dl, d], chain(c, p, dl, d), resources=[_resource("Server")])

    p_ours = process(value=2.0)
    ours = layout([c, p_ours, dl, d], chain(c, p_ours, dl, d), resources=[_resource("Server")])

    dl_theirs = delay(value=9.0)
    theirs = layout([c, p, dl_theirs, d], chain(c, p, dl_theirs, d), resources=[_resource("Server")])

    merged, conflicts = _merge(base, ours, theirs)
    assert conflicts == []

    expected = layout([c, p_ours, dl_theirs, d], chain(c, p_ours, dl_theirs, d),
                       resources=[_resource("Server")])
    _assert_layouts_equal(expected, merged)


# -- case 3: both change the same param identically -----------------------

def test_both_change_same_param_identically():
    c, d = create(), dispose()
    p_base = process(value=1.0)
    base = layout([c, p_base, d], chain(c, p_base, d), resources=[_resource("Server")])

    p_new = process(value=5.0)
    ours = layout([c, p_new, d], chain(c, p_new, d), resources=[_resource("Server")])
    theirs = layout([c, p_new, d], chain(c, p_new, d), resources=[_resource("Server")])

    merged, conflicts = _merge(base, ours, theirs)
    assert conflicts == []
    _assert_layouts_equal(ours, merged)


# -- case 4: both change the same param differently -> conflict, ours wins

def test_both_change_same_param_differently_conflict():
    c, d = create(), dispose()
    p_base = process(value=1.0)
    base = layout([c, p_base, d], chain(c, p_base, d), resources=[_resource("Server")])

    p_ours = process(value=2.0)
    ours = layout([c, p_ours, d], chain(c, p_ours, d), resources=[_resource("Server")])

    p_theirs = process(value=3.0)
    theirs = layout([c, p_theirs, d], chain(c, p_theirs, d), resources=[_resource("Server")])

    merged, conflicts = _merge(base, ours, theirs)
    assert len(conflicts) == 1
    only = conflicts[0]
    assert _conflict_path(only) == "blocks.process_1.params.dist_params.value"
    assert _conflict_ours(only) == 2.0
    assert _conflict_theirs(only) == 3.0

    _assert_layouts_equal(ours, merged)  # merged keeps OURS on conflict


# -- case 5: ours deletes a block, theirs modifies it -> conflict ---------

def test_ours_deletes_theirs_modifies_conflict():
    c, d = create(), dispose()
    dl_base = delay(value=3.0)
    base = layout([c, dl_base, d], chain(c, dl_base, d))

    # ours deletes the delay block entirely, rewiring the link straight through
    ours = layout([c, d], chain(c, d))

    dl_theirs = delay(value=9.0)
    theirs = layout([c, dl_theirs, d], chain(c, dl_theirs, d))

    merged, conflicts = _merge(base, ours, theirs)
    assert len(conflicts) == 1
    only = conflicts[0]
    assert _conflict_path(only) == "blocks.delay_1"
    assert _conflict_ours(only) is None            # deleted on ours' side
    assert _conflict_theirs(only) == {**dl_theirs, "params": {**dl_theirs["params"],
                                                          "time_unit": "minutes"}}   # v28

    # GUESS: delete-vs-modify resolves like changed/changed -- ours wins.
    _assert_layouts_equal(ours, merged)


# -- case 6: both move the same block differently -> no conflict, ours wins

def test_both_move_same_block_differently_ours_wins():
    c, d = create(), dispose()
    dl_base = delay()  # position [1, 0]
    base = layout([c, dl_base, d], chain(c, dl_base, d))

    dl_ours = {**dl_base, "position": [5, 5]}
    ours = layout([c, dl_ours, d], chain(c, dl_ours, d))

    dl_theirs = {**dl_base, "position": [9, 9]}
    theirs = layout([c, dl_theirs, d], chain(c, dl_theirs, d))

    merged, conflicts = _merge(base, ours, theirs)
    assert conflicts == []
    _assert_layouts_equal(ours, merged)


# -- case 7: ours deletes a block, theirs adds a link to it ---------------

def test_ours_deletes_theirs_adds_link_conflict():
    c, p, d = create(), process(), dispose()
    base = layout([c, p, d], chain(c, p, d))

    # ours deletes the process block, rewiring create -> dispose directly.
    ours = layout([c, d], chain(c, d))

    # theirs, unaware of the deletion, adds a new block downstream of the
    # process block and links it in.
    dl = delay()  # name "delay_1"
    theirs_links = chain(c, p, d) + [{"source": p["name"], "target": dl["name"]}]
    theirs = layout([c, p, d, dl], theirs_links)

    merged, conflicts = _merge(base, ours, theirs)
    assert len(conflicts) == 1
    only = conflicts[0]
    # GUESS: the record in trouble is the dangling link, not the deleted
    # block -- the merge itself creates the invalid reference.
    assert _conflict_path(only) == "links.process_1->delay_1"
    assert _conflict_ours(only) is None
    assert _conflict_theirs(only) == {"source": "process_1", "target": "delay_1"}

    # GUESS: ours' deletion wins (process_1 and the dangling link are gone);
    # theirs' unrelated new block still merges in, now disconnected.
    merged_blocks = {b["name"] for b in merged["blocks"]}
    merged_links = {(l["source"], l["target"]) for l in merged["links"]}
    assert merged_blocks == {"create_1", "dispose_1", "delay_1"}
    assert "process_1" not in merged_blocks
    assert ("create_1", "dispose_1") in merged_links
    assert ("process_1", "delay_1") not in merged_links


# -- case 8: CLI git driver -----------------------------------------------

def test_cli_merge_driver(tmp_path, capsys):
    _require_cli()

    # exit 0, OUT written: case 2's non-conflicting triple
    c, p, dl, d = create(), process(value=1.0), delay(value=3.0), dispose()
    base = layout([c, p, dl, d], chain(c, p, dl, d), resources=[_resource("Server")])
    p_ours = process(value=2.0)
    ours = layout([c, p_ours, dl, d], chain(c, p_ours, dl, d), resources=[_resource("Server")])
    dl_theirs = delay(value=9.0)
    theirs = layout([c, p, dl_theirs, d], chain(c, p, dl_theirs, d), resources=[_resource("Server")])

    base_path = _write(tmp_path, "base.json", base)
    ours_path = _write(tmp_path, "ours.json", ours)
    theirs_path = _write(tmp_path, "theirs.json", theirs)
    out_path = tmp_path / "out.json"

    code = _run_cli(["merge", base_path, ours_path, theirs_path, "-o", str(out_path)])
    capsys.readouterr()
    assert code == 0
    assert out_path.exists()

    # exit 1, conflict report on stderr: case 4's conflicting triple
    p_base2 = process(value=1.0)
    base2 = layout([c, p_base2, d], chain(c, p_base2, d), resources=[_resource("Server")])
    p_ours2 = process(value=2.0)
    ours2 = layout([c, p_ours2, d], chain(c, p_ours2, d), resources=[_resource("Server")])
    p_theirs2 = process(value=3.0)
    theirs2 = layout([c, p_theirs2, d], chain(c, p_theirs2, d), resources=[_resource("Server")])

    base2_path = _write(tmp_path, "base2.json", base2)
    ours2_path = _write(tmp_path, "ours2.json", ours2)
    theirs2_path = _write(tmp_path, "theirs2.json", theirs2)
    out2_path = tmp_path / "out2.json"

    code2 = _run_cli(["merge", base2_path, ours2_path, theirs2_path, "-o", str(out2_path)])
    captured = capsys.readouterr()
    assert code2 == 1
    assert "conflict" in captured.err.lower()
