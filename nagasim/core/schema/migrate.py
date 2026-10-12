"""Schema versioning and migration (ARCHITECTURE section 7.5, DEVLOG #33, #78).

Every layout saved by the editor carries "schema_version": CURRENT_VERSION.
Older files (no version key) are migrated on load, per block, since a real-world
model file can mix generations (a block copy-pasted from an older file). A
version newer than CURRENT_VERSION is refused -- this build cannot know what it
means. The version-by-version history is the table in ARCHITECTURE section 7.5
(also rendered into site_docs/reference/schema.md by tools/gen_docs.py).
"""
from __future__ import annotations

import copy

from nagasim.core import sections
from nagasim.settings.config import LEGACY_BLOCK_W, LEGACY_BLOCK_H


CURRENT_VERSION = 29


# Keys a model file may carry but must never be OBEYED (ARCHITECTURE 7.4: a
# model file is untrusted content, and permission is the caller's to give).
CONSENT_KEYS = ("allow_network",)


class ModelValidationError(ValueError):
    """Raised when a layout fails validation; carries every problem found."""

    def __init__(self, problems):
        # A bare string is accepted so the exception survives a pickle round
        # trip: pickle rebuilds it from args == (joined message,), and treating
        # that string as an iterable of problems would explode it per character
        # (ROADMAP 1.6 runs replications in a process pool).
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) or "invalid model")


def _migrate_block_params(block: dict) -> dict:
    """Return a copy of *block* with its params converted from gen-1/gen-2 shape
    to gen-3. Detection is per-block, not per-file (ARCHITECTURE section 7.5)."""
    params = dict(block.get("params", {}))

    # gen-1 CREATE: time_between_arrivals / max_entities / attributes.e_type
    if "time_between_arrivals" in params:
        value = params.pop("time_between_arrivals")
        params["distribution"] = "Constant"
        params["dist_params"] = {"value": value}
    if "max_entities" in params:
        params["max_arrivals"] = params.pop("max_entities")
    if "attributes" in params:
        attrs = params.pop("attributes") or {}
        if "e_type" in attrs:
            params["e_type"] = attrs["e_type"]

    # gen-1 scalar delay / deletion_delay -> Constant dist_params
    for scalar_key in ("delay", "deletion_delay"):
        if scalar_key in params and not isinstance(params[scalar_key], dict):
            value = params.pop(scalar_key)
            params["distribution"] = params.get("distribution", "Constant")
            params["dist_params"] = {"value": value}

    # gen-2 distribution_params -> dist_params; drop the vestigial empty dict
    if "distribution_params" in params:
        legacy = params.pop("distribution_params")
        if legacy and "dist_params" not in params:
            params["dist_params"] = legacy

    # stray SEIZE/RELEASE 'capacity' key never belonged in block params -- but
    # BUFFER's capacity IS its one parameter (PHASE4 T5), so it stays.
    if block.get("type", "").upper() != "BUFFER":
        params.pop("capacity", None)

    # v4 -> v5: ASSIGN's free-form {attr: value} dict becomes an ordered list of
    # {target, expr}. repr() turns the stored value back into a literal the
    # expression grammar accepts (strings get their quotes back).
    if block.get("type", "").upper() == "ASSIGN" and "assignments" not in params:
        params = {"assignments": [{"target": f"entity.{key}", "expr": repr(value)}
                                  for key, value in params.items()]}

    new_block = dict(block)
    new_block["params"] = params
    return new_block


def _migrate_link(link) -> dict:
    """v7 `[src, tgt]` -> v8 `{"source": src, "target": tgt}` (ROADMAP 1.5);
    an object already in v8 shape passes through unchanged."""
    if isinstance(link, dict):
        return dict(link)
    src, tgt = link
    return {"source": src, "target": tgt}


def _migrate_resource(resource: dict) -> dict:
    """v5 -> v6: drop `type`, gain `discipline`. Priority is now always honoured
    (every resource is a PriorityResource), so a PriorityResource and a plain
    Resource both become a FIFO queue with the same behaviour as before."""
    migrated = {k: v for k, v in resource.items() if k != "type"}
    migrated.setdefault("discipline", "FIFO")
    return migrated


_QT_GRID = 20
# Qt blocks are 120x60 scene units; legacy (pre-Qt) layouts were laid out for
# 80x40 pixel blocks, so a plain top-left -> centre shift leaves Qt blocks
# overlapping. Scaling by 1.5 (120/80 == 60/40) spreads them back out.
_QT_SCALE = 1.5


def _shift_positions_to_centre(layout: dict) -> None:
    """Mutate *layout* in place: convert a stored top-left corner (v3 and
    earlier) into a Qt centre position, scaled up for the larger block size
    and snapped to the 20-unit grid."""
    dx, dy = LEGACY_BLOCK_W / 2, LEGACY_BLOCK_H / 2
    for block in layout.get("blocks", []):
        pos = block.get("position")
        if pos is not None:
            cx = round((pos[0] + dx) * _QT_SCALE / _QT_GRID) * _QT_GRID
            cy = round((pos[1] + dy) * _QT_SCALE / _QT_GRID) * _QT_GRID
            block["position"] = [float(cx), float(cy)]


def _migrate_hold_mode(layout: dict) -> None:
    """Mutate *layout* in place: HOLD's legacy UI mode string, pre-dating the
    "Condition" rename (DEVLOG #6). Applied unconditionally, like the section
    defaults below -- a HOLD block copy-pasted from an old file can carry this
    string inside an otherwise CURRENT-version layout, and the rename is
    idempotent (nothing to do once a block already says "Condition")."""
    for block in layout.get("blocks", []):
        if block.get("type", "").upper() != "HOLD":
            continue
        params = block.get("params")
        if isinstance(params, dict) and params.get("mode") == "Scan for Conditions":
            params["mode"] = "Condition"


def _strip_consent(layout: dict) -> dict:
    """Drop every consent key from the data sources, in place.

    A model file that says "this source may reach the network" is a file asking
    for a permission only its reader can give; opening one must not grant it.
    Stripping here means no code path downstream can accidentally honour it,
    and the key is never written back out.
    """
    for source in layout.get("data_sources") or []:
        if isinstance(source, dict):
            for key in CONSENT_KEYS:
                source.pop(key, None)
    return layout


def migrate(layout: dict) -> dict:
    """Return a NEW gen-4 layout dict; never mutates *layout*.

    Raises ModelValidationError if schema_version is newer than CURRENT_VERSION.
    """
    version = layout.get("schema_version")
    if version is not None and version > CURRENT_VERSION:
        raise ModelValidationError([
            f"schema_version {version} is newer than the version this build supports "
            f"({CURRENT_VERSION})."
        ])
    migrated = copy.deepcopy(layout)
    if version != CURRENT_VERSION:
        migrated["blocks"] = [_migrate_block_params(b) for b in migrated.get("blocks", [])]
        migrated["resources"] = [_migrate_resource(r) for r in migrated.get("resources", [])]
        migrated["links"] = [_migrate_link(l) for l in migrated.get("links", [])]   # v7 -> v8
    _migrate_hold_mode(migrated)
    # The section defaults below run for a CURRENT-version file too (DEVLOG
    # #215): they are idempotent, and a file the editor saved without a newer
    # default (`simulation.energy_price`) must read the same as its older
    # commit, or `naga diff`/`merge` report a change nobody made.
    for section in sections.SECTIONS:
        if section.default is not sections.MISSING:
            migrated.setdefault(section.name, copy.deepcopy(section.default))
    # A malformed `simulation` (not an object) is validation's to report.
    simulation = migrated["simulation"] if isinstance(migrated["simulation"], dict) else {}
    simulation.setdefault("time_unit", "minutes")               # v7 -> v8
    simulation.setdefault("length_unit", "meters")              # v27 -> v28 (units spec, D160)
    simulation.setdefault("currency", "EUR")                    # v9 -> v10 (ROADMAP 2.2)
    simulation.setdefault("energy_price", 0)                    # v23 -> v24 (PHASE6 W3 M1)
    if version is None or version < 4:
        _shift_positions_to_centre(migrated)  # v(<4) stored top-left; v4 stores centre
    from nagasim.core.quantities import fill_units              # kept off the module import
    fill_units(migrated)                                        # v27 -> v28 (D159), idempotent
    migrated["schema_version"] = CURRENT_VERSION
    return _strip_consent(migrated)
