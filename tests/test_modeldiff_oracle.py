"""Independent oracle for W7 Q1 "Semantic diff" (PHASE6-6D-W7-PLAN, D124).

Hand-derived expected `modeldiff.diff` output and `naga diff` CLI
behaviour, written BEFORE Q1 exists so the failures below are the spec, not
a bug report. All access to the not-yet-built API goes through the small
adapter functions right below the imports -- once Q1 lands, the coordinator
reconciles real names/shapes here in one place instead of at every call site.

Reading choices this oracle bakes in (state per the task brief):
- `modeldiff.diff(a, b)` takes two LAYOUT DICTS (already migrated or not --
  the function migrates both itself, case 7). The CLI loads JSON from its
  own file arguments and calls the same function; it does not take dicts.
- A `moved`-only diff (canvas `position`) is layout noise, hidden by
  default: `naga diff` without `--all` reports "no changes" (exit 0)
  when the only differences are `moved`. `--all` turns that back into a
  reported difference (exit 1). See CLI_HIDES_MOVED_BY_DEFAULT below.
- An added/removed record's path is the whole record (`resources.Spare`),
  not a field under it -- field-level paths only apply to `changed`.
"""
import copy
import json

import pytest

from conftest import chain, create, delay, dispose, layout, process
from conftest import resource_record as _resource

# Our chosen reading of the CLI's default visibility rule (see module
# docstring) -- named here so it reads as a decision, not a magic bool.
CLI_HIDES_MOVED_BY_DEFAULT = True


# -- adapters: the one place that knows modeldiff's real call shape --------

def _diff(a, b):
    from nagasim import modeldiff
    return modeldiff.diff(a, b)


def _kind(change):
    return getattr(change, "kind", None)


def _path(change):
    return getattr(change, "path", None)


def _old(change):
    return getattr(change, "old", None)


def _new(change):
    return getattr(change, "new", None)


def _run_cli(args):
    from nagasim.cli import main
    return main(args)


def _cli_diff_wired():
    """True once nagasim.cli.main actually dispatches `naga diff` itself.

    Guard, not a feature probe: calling main(["diff", ...]) before that
    dispatch exists falls through to the last branch of main(), which
    prints the help text instead of diffing. Checks the COMMANDS registry
    (ARCH-REVIEW-PLAN B4) rather than sniffing main()'s source text as before.
    """
    from nagasim.cli import COMMANDS
    return "diff" in COMMANDS


def _require_cli():
    if not _cli_diff_wired():
        pytest.fail("naga diff is not wired into nagasim.cli.main yet (W7 Q1 pending)")


# -- shared model fixtures (wire format: docs/ARCHITECTURE.md section 7) ---

def _base_layout():
    c, p, d = create(), process(), dispose()
    return layout([c, p, d], chain(c, p, d), resources=[_resource("Server")])


def _write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


# -- case 1: identical layouts -----------------------------------------

def test_identical_layouts_no_changes(tmp_path, capsys):
    doc = _base_layout()
    assert _diff(doc, copy.deepcopy(doc)) == []

    path_a = _write(tmp_path, "a.json", doc)
    path_b = _write(tmp_path, "b.json", doc)
    _require_cli()
    code = _run_cli(["diff", path_a, path_b])
    capsys.readouterr()
    assert code == 0


# -- case 2: same blocks, different list order --------------------------

def test_reordered_block_list_no_changes():
    c, p, d = create(), process(), dispose()
    links = chain(c, p, d)
    a = layout([c, p, d], links, resources=[_resource("Server")])
    b = layout([d, p, c], links, resources=[_resource("Server")])  # reversed list
    assert _diff(a, b) == []


# -- case 3: one PROCESS param changed -----------------------------------

def test_one_process_param_changed():
    c, d = create(), dispose()
    p_old, p_new = process(value=1.0), process(value=2.0)
    a = layout([c, p_old, d], chain(c, p_old, d), resources=[_resource("Server")])
    b = layout([c, p_new, d], chain(c, p_new, d), resources=[_resource("Server")])

    changes = _diff(a, b)
    assert len(changes) == 1
    only = changes[0]
    assert _kind(only) == "changed"
    assert _path(only) == "blocks.process_1.params.dist_params.value"
    assert _old(only) == 1.0
    assert _new(only) == 2.0


# -- case 4: added link + removed resource -------------------------------

def test_added_link_and_removed_resource():
    c, p, d = create(), process(), dispose()
    base_links = chain(c, p, d)
    a = layout([c, p, d], base_links, resources=[_resource("Server"), _resource("Spare")])
    b = layout([c, p, d], base_links + [{"source": c["name"], "target": d["name"]}],
               resources=[_resource("Server")])

    changes = _diff(a, b)
    assert len(changes) == 2
    seen = {(_kind(ch), _path(ch)) for ch in changes}
    assert seen == {
        ("added", "links.create_1->dispose_1"),
        ("removed", "resources.Spare"),
    }


# -- case 5: a block moved on the canvas only ----------------------------

def test_block_moved_only():
    c, d = create(), dispose()
    delay_a = delay()  # position [1, 0], see conftest.delay()
    links = chain(c, delay_a, d)
    a = layout([c, delay_a, d], links)
    delay_b = {**delay_a, "position": [5, 5]}
    b = layout([c, delay_b, d], links)

    changes = _diff(a, b)
    assert len(changes) == 1
    only = changes[0]
    assert _kind(only) == "moved"
    assert _path(only) == "blocks.delay_1.position"
    assert _old(only) == [1, 0]
    assert _new(only) == [5, 5]


def test_block_moved_only_cli_hides_by_default(tmp_path):
    c, d = create(), dispose()
    delay_a = delay()
    links = chain(c, delay_a, d)
    a = layout([c, delay_a, d], links)
    delay_b = {**delay_a, "position": [5, 5]}
    b = layout([c, delay_b, d], links)

    path_a = _write(tmp_path, "a.json", a)
    path_b = _write(tmp_path, "b.json", b)
    _require_cli()

    default_code = _run_cli(["diff", path_a, path_b])
    assert default_code == (0 if CLI_HIDES_MOVED_BY_DEFAULT else 1)

    all_code = _run_cli(["diff", path_a, path_b, "--all"])
    assert all_code == 1  # --all surfaces the moved change as a difference


# -- case 6: --textconv is stable under reordering, changes with content --

def test_textconv_stable_across_order_differs_after_change(tmp_path, capsys):
    _require_cli()
    c, p, d = create(), process(), dispose()
    links = chain(c, p, d)
    order_a = layout([c, p, d], links, resources=[_resource("Server")])
    order_b = layout([d, p, c], links, resources=[_resource("Server")])

    p_changed = process(value=2.0)
    changed = layout([c, p_changed, d], chain(c, p_changed, d),
                      resources=[_resource("Server")])

    path_a = _write(tmp_path, "order_a.json", order_a)
    path_b = _write(tmp_path, "order_b.json", order_b)
    path_changed = _write(tmp_path, "changed.json", changed)

    _run_cli(["diff", "--textconv", path_a])
    text_a = capsys.readouterr().out
    _run_cli(["diff", "--textconv", path_b])
    text_b = capsys.readouterr().out
    _run_cli(["diff", "--textconv", path_changed])
    text_changed = capsys.readouterr().out

    assert text_a == text_b
    assert text_a != text_changed


# -- case 7: an older schema_version vs. the same model migrated ---------

def test_old_schema_version_vs_migrated_no_changes():
    from nagasim.core import schema

    c, p, d = create(), process(), dispose()
    old = layout([c, p, d], chain(c, p, d), resources=[_resource("Server")])
    assert old["schema_version"] == 9  # conftest.layout()'s starting version

    migrated = schema.migrate(old)
    assert migrated["schema_version"] == schema.CURRENT_VERSION

    assert _diff(old, migrated) == []
