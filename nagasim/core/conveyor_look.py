"""Conveyor look settings: the fields a piece's `look` may hold, their per-type defaults and
value lists, their validation and schema, and the stored form (plan H, D1470-D1475).

Data and pure functions only; no engine block code reads `look` or `look_set`, so timing never
depends on them. Dimensions are metres, like the datasheet values in `conveyor_types.TYPES`;
opening positions are in the belt's length unit, like `path`. Only the names of the choice
values belong here. Their materials are in `look_materials`.

    fields_of(kind)           the fields of a type, in the panel's order
    default / choices         a field's default and its value list for a type
    value_problem             why one value is refused
    look_of(params, factor)   every field resolved, plus the openings (in metres)
    tidy(params)              the stored form: defaults dropped unless set by hand
    inherit(source_params)    what a piece snapped to another takes
    guide_runs                a guide's runs between the openings
    exit_openings             the openings a divert or side merge cuts, never stored
    look_problems             `E_CONVEYOR_LOOK` for the validator
    chain_of / chain_value    the line a look spreads along, and a reset's value
    apply_look / set_openings the writes, each one undo step
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field as _field
from typing import TYPE_CHECKING, Mapping, Sequence

from nagasim.core import conveyor_path
from nagasim.core.conveyor_types import TYPES
from nagasim.core.look_materials import is_hex
from nagasim.core.problems import Problem, problem

if TYPE_CHECKING:                   # the document imports this module, never the reverse
    from nagasim.core.document import ModelDocument

__all__ = ["ROLLER_TYPES", "FRAME_COLORS", "BELT_KINDS", "ROLLER_FINISHES", "GUIDE_STYLES",
           "DRIVE_SIDES", "OPENING_SIDES", "ROLLER_DIAMETER_M", "PULLEY_DIAMETER_M",
           "LEG_SPACING_M", "RAISED_PALLET_M", "CHAIN_STRAND_SPACING_M", "MAX_STRANDS",
           "RAIL_DEPTH_M", "STRAND_WIDTH_M", "FRAME_HEIGHT_M", "fields_of", "default",
           "choices", "value_problem", "look_of", "width_m", "height_m", "piece_length", "tidy", "inherit",
           "guide_runs", "exit_openings", "look_problems", "look_schema", "look_set_schema", "chain_of",
           "chain_value", "LookResult", "apply_look", "set_openings"]

ROLLER_TYPES = ("roller", "mdr", "pallet")
FRAME_COLORS = ("steel", "grey", "blue", "green", "yellow", "orange")
BELT_KINDS = ("black_rubber", "green_pvc", "white_food", "plastic_modular")
ROLLER_FINISHES = ("zinc", "grey", "blue_sleeve")
GUIDE_STYLES = ("round", "flat", "panel")
DRIVE_SIDES = ("under", "left", "right", "none")
OPENING_SIDES = ("left", "right", "both")
ROLLER_DIAMETER_M = {"roller": 0.05, "mdr": 0.05, "pallet": 0.089}
PULLEY_DIAMETER_M = 0.034          # the belt end's pulley: wrap radius 0.017
LEG_SPACING_M = 1.5
# A pallet piece above this height stands on legs by default; at or under it, on the floor
# (D1486).
RAISED_PALLET_M = 0.2
CHAIN_STRAND_SPACING_M = 0.822     # 1.012 - 2 x (0.05 + 0.045), the strands drawn today
MAX_STRANDS = 6
# The kit's frame, which the validator's fit rules read too (the engine never imports the kit):
# a rail's depth inboard of its outer face, one chain strand's width, and the rails' and ends'
# height (a type lower than it, as the 0.095 m pallet, has its own height instead).
RAIL_DEPTH_M = 0.05
STRAND_WIDTH_M = 0.05
FRAME_HEIGHT_M = 0.1

# field -> (kind of value, the types that have it), in the panel's order
_ALL = tuple(TYPES)
_FIELDS = {
    "width": ("metres", _ALL), "height": ("metres", _ALL),
    "frame_color": ("choice", _ALL), "leg_spacing": ("metres0", _ALL),
    "drive_side": ("choice", ("belt", "roller", "chain", "pallet")),
    "guide_left": ("metres0", _ALL), "guide_right": ("metres0", _ALL),
    "guide_style": ("choice", _ALL),
    "roller_pitch": ("metres", ROLLER_TYPES), "roller_diameter": ("metres", ROLLER_TYPES),
    "roller_finish": ("choice", ROLLER_TYPES),
    "belt_kind": ("choice", ("belt",)), "pulley_diameter": ("metres", ("belt",)),
    "chain_strands": ("integer", ("chain",)), "strand_spacing": ("metres", ("chain",)),
}
# field -> (lowest, highest) in metres; leg_spacing and the guides also take 0 (none). The
# pulley's lowest is the default's 34 mm, which a 50 mm floor would refuse (D1471).
# A height is no lower than the type's frame (`value_problem`); the bound is the lowest any
# type takes, so the schema's minimum is what the validator accepts somewhere (0.095, a pallet).
_LOWEST_HEIGHT_M = min(min(t.height for t in TYPES.values()), FRAME_HEIGHT_M)
_BOUNDS = {"width": (0.1, 3.0), "height": (_LOWEST_HEIGHT_M, 3.0), "leg_spacing": (0.5, 10.0),
           "guide_left": (0.0, 1.5), "guide_right": (0.0, 1.5), "roller_pitch": (0.03, 1.0),
           "roller_diameter": (0.02, 0.3), "pulley_diameter": (0.03, 0.5),
           "strand_spacing": (0.05, 1.5)}
_ZERO_OK = ("leg_spacing", "guide_left", "guide_right")
_CHOICES = {"frame_color": FRAME_COLORS, "drive_side": DRIVE_SIDES, "guide_style": GUIDE_STYLES,
            "roller_finish": ROLLER_FINISHES, "belt_kind": BELT_KINDS}
_NAMES = tuple(_FIELDS)
_OPENING_KEYS = {"side", "start", "end"}


def fields_of(kind: str) -> tuple[str, ...]:
    """The look fields a conveyor type takes, in the order an editor lists them.

    Parameters
    ----------
    kind : str
        A conveyor type: the piece's `type` param (`belt`, `roller`, `mdr`, `chain`, `pallet`).

    Returns
    -------
    tuple of str
        The field names; () for an untyped piece or an unknown type.

    See Also
    --------
    nagasim.appearance.look_of : The fields resolved for one piece.
    nagasim.appearance.default : A field's value when a piece does not set it.

    Examples
    --------
    >>> from nagasim.appearance import fields_of
    >>> "belt_kind" in fields_of("belt"), "belt_kind" in fields_of("roller")
    (True, False)
    """
    if kind not in TYPES:
        return ()
    return tuple(f for f, (_, types) in _FIELDS.items() if kind in types)


def default(kind: str, field: str, look: Mapping | None = None):
    """The value of a look field that a piece does not set.

    Lengths are metres. Only a pallet's `leg_spacing` depends on the rest of the look: legs every
    1.5 m when its `height` is above `RAISED_PALLET_M`, else 0 (no legs). Where the legs and
    the drive go is in `look_of`.

    Parameters
    ----------
    kind : str
        A conveyor type that has *field* (see `fields_of`).
    field : str
        A look field name.
    look : mapping, optional
        The piece's stored look (its `look` param), read for a pallet's `leg_spacing` only.

    Returns
    -------
    float, int or str
        The default value.

    Raises
    ------
    KeyError
        When *kind* or *field* is unknown.

    See Also
    --------
    nagasim.appearance.look_of : Every field of a piece, and where legs and drive go.
    nagasim.appearance.fields_of : The fields a type takes.

    Examples
    --------
    >>> from nagasim.appearance import default
    >>> default("pallet", "leg_spacing"), default("pallet", "leg_spacing", {"height": 0.5})
    (0, 1.5)
    """
    if field == "width":
        return TYPES[kind].width
    if field == "height":
        return TYPES[kind].height
    if field == "frame_color":
        return "steel"
    if field == "leg_spacing":
        if kind != "pallet":
            return LEG_SPACING_M
        # The stored height counts only when valid, as look_of reads it (D1486).
        height = look.get("height") if isinstance(look, Mapping) else None
        if height is None or value_problem(kind, "height", height) is not None:
            height = TYPES[kind].height
        return LEG_SPACING_M if height > RAISED_PALLET_M else 0
    if field == "drive_side":
        return "right" if kind == "pallet" else "under"
    if field in ("guide_left", "guide_right"):
        return 0
    if field == "guide_style":
        return "round"
    if field == "roller_pitch":
        return TYPES[kind].roller_pitch
    if field == "roller_diameter":
        return ROLLER_DIAMETER_M[kind]
    if field == "roller_finish":
        return "zinc"
    if field == "belt_kind":
        return "black_rubber"
    if field == "pulley_diameter":
        return PULLEY_DIAMETER_M
    if field == "chain_strands":
        return 2
    if field == "strand_spacing":
        return CHAIN_STRAND_SPACING_M
    raise KeyError(field)


def choices(kind: str, field: str) -> tuple[str, ...]:
    """The value list of a choice field for *kind*; () for a number field."""
    values = _CHOICES.get(field, ())
    if field == "drive_side" and kind == "pallet":
        return tuple(v for v in values if v != "under")     # a pallet drive is never under
    return values


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def value_problem(kind: str, field: str, value) -> str | None:
    """Why *value* is refused for *field* on a *kind* piece (one sentence, no block name), or None."""
    if field not in fields_of(kind):
        return f"{field} is not a setting of a {kind or 'untyped'} conveyor"
    shape = _FIELDS[field][0]
    if shape == "choice":
        allowed = choices(kind, field)
        if value in allowed and isinstance(value, str):
            return None
        if field == "frame_color":
            if is_hex(value):
                return None
            return f"{field} must be one of {', '.join(allowed)}, or a colour #RRGGBB"
        return f"{field} must be one of {', '.join(allowed)}"
    if shape == "integer":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_STRANDS:
            return f"{field} must be a whole number from 1 to {MAX_STRANDS}"
        return None
    if not _number(value):
        return f"{field} must be a number"
    low, high = _BOUNDS[field]
    if field == "height":       # no lower than its frame, or the rails sink under the floor (D1471)
        low = max(low, min(TYPES[kind].height, FRAME_HEIGHT_M))
    if field in _ZERO_OK and value == 0:
        return None
    if not low <= value <= high:
        return f"{field} must be {'0 or ' if field in _ZERO_OK else ''}from {low:g} to {high:g} m"
    return None


def _stored(params: Mapping) -> dict:
    raw = params.get("look") if isinstance(params, Mapping) else None
    return raw if isinstance(raw, dict) else {}


def _opening_ok(item) -> bool:
    return (isinstance(item, dict) and set(item) == _OPENING_KEYS
            and item["side"] in OPENING_SIDES and isinstance(item["side"], str)
            and _number(item["start"]) and _number(item["end"])
            and 0 <= item["start"] < item["end"])


def _openings_ok(raw) -> bool:
    return isinstance(raw, list) and all(_opening_ok(o) for o in raw)


def look_of(params: Mapping, factor: float = 1.0) -> dict:
    """Everything needed to draw one conveyor piece: each look field of its type, resolved.

    A field the piece stores with a valid value keeps it; any other takes its `default`. Lengths
    are metres and colours are names or sRGB `#RRGGBB`; `material()` and `guide_material()` turn
    them into materials. `"openings"` lists the gaps in the side guides that the file stores, in
    metres; add `exit_openings` for the gaps diverts and merges cut.

    Legs and drive, as the reference drawing places them: legs stand at 0.05 m from the start
    and then every `leg_spacing` metres along the path, none nearer than 0.05 m to the end; one
    more stands 0.05 m before the end when that is more than 0.3 m past the last leg. Each leg
    reaches from the floor to the underside of the rails (a `leg_spacing` of 0 means none). A
    pallet piece at or under `RAISED_PALLET_M` (0.2 m) high stands on the floor and has no legs
    by default; above it, legs every 1.5 m. The drive (`drive_side` `under`) hangs under the
    bed, at mid-length on a roller or pallet piece and 0.15 m from the end on a belt or chain;
    `left` or `right` hangs it at the same place and height beside the frame, 0.01 m outboard.
    Its top is 0.05 m under the conveying surface, and it never goes below the floor. A
    pallet's drive is always beside the frame (an `under` drive goes to the right), and on a
    low pallet piece it stands on the floor.

    Parameters
    ----------
    params : mapping
        A CONVEYOR block's params (`document.blocks[name].params`).
    factor : float, default 1.0
        Metres per unit of the piece's length unit, `length_factor(document, params)`; it scales
        the openings, which the file stores in that unit, like the path.

    Returns
    -------
    dict
        `{field: value, ..., "openings": [{"side", "start", "end"}, ...]}`; {} for a block
        that is not a typed conveyor piece. Never raises.

    See Also
    --------
    nagasim.appearance.length_factor : The *factor* for a piece.
    nagasim.appearance.material : The material a look value paints.
    nagasim.appearance.exit_openings : The guide gaps the exits cut.

    Examples
    --------
    >>> from nagasim.appearance import look_of
    >>> look = look_of({"type": "roller", "look": {"frame_color": "blue"}})
    >>> look["frame_color"], look["roller_finish"], look["openings"]
    ('blue', 'zinc', [])
    """
    try:
        kind = params.get("type") if isinstance(params, Mapping) else None
        if kind not in TYPES:
            return {}
        stored = _stored(params)
        out = {}
        for field in fields_of(kind):
            value = stored.get(field)
            out[field] = value if field in stored and value_problem(kind, field, value) is None \
                else default(kind, field, stored)
        raw = stored.get("openings")
        out["openings"] = [{"side": o["side"], "start": o["start"] * factor,
                            "end": o["end"] * factor}
                           for o in raw] if _openings_ok(raw) else []
        return out
    except Exception:       # a look read for drawing never stops the drawing
        return {}


def _one_field(params: Mapping, field: str):
    """`look_of(params).get(field)` without resolving the rest: the 3D view and the 2D player
    read a width per belt per frame, where a whole look costs about 50 times one field."""
    try:
        kind = params.get("type") if isinstance(params, Mapping) else None
        if kind not in TYPES or field not in fields_of(kind):
            return None
        stored = _stored(params)
        value = stored.get(field)
        if field in stored and value_problem(kind, field, value) is None:
            return value
        return default(kind, field, stored)
    except Exception:       # a look read for drawing never stops the drawing
        return None


def width_m(params: Mapping) -> float | None:
    """The look width, or None for an untyped piece."""
    return _one_field(params, "width")


def height_m(params: Mapping) -> float | None:
    """The look height (top of the conveying surface), or None for an untyped piece."""
    return _one_field(params, "height")


def piece_length(params: Mapping) -> float | None:
    """A piece's length in its own unit: its path's, else its `length`; None when neither."""
    try:
        if params.get("path"):         # [] is no path, as floor_geometry reads it
            return conveyor_path.path_length(conveyor_path.parse_path(params["path"]))
    except (ValueError, TypeError):
        return None
    length = params.get("length")
    return float(length) if _number(length) and length > 0 else None


def _merged_openings(openings: list, length: float | None) -> list:
    """Openings clamped to [0, length], empty ones dropped, overlapping ones with the same
    side merged, sorted by start."""
    kept = []
    for item in openings:
        start, end = item["start"], item["end"]
        if length is not None:
            end = min(end, length)
        if start < end:
            kept.append({"side": item["side"], "start": start, "end": end})
    out: list = []
    for side in OPENING_SIDES:
        for item in sorted((o for o in kept if o["side"] == side), key=lambda o: o["start"]):
            if out and out[-1]["side"] == side and item["start"] <= out[-1]["end"]:
                out[-1]["end"] = max(out[-1]["end"], item["end"])
            else:
                out.append(dict(item))
    order = {s: i for i, s in enumerate(OPENING_SIDES)}
    return sorted(out, key=lambda o: (o["start"], order[o["side"]], o["end"]))


def tidy(params: dict, before: Mapping | None = None) -> dict:
    """The stored form of a CONVEYOR's params (D1475): look fields equal to their default are
    dropped unless set by hand (in `look_set`), fields the type lacks go with their `look_set`
    entries, openings are clamped to the path, merged and sorted. When *before* (the stored
    params) has another type, the kept fields the new type refuses, alone or by a cross rule,
    go too unless set by hand, so a type change never makes a look error nobody wrote. Never
    adds a key; returns *params* itself when nothing changes. Any other look that is not valid
    is left for the validator."""
    kind = params.get("type")
    look, look_set = params.get("look"), params.get("look_set")
    if not kind and (look or look_set):
        out = dict(params)                              # untyped: no fields, so no look (D1475)
        if look is not None:
            out["look"] = {}
        if look_set is not None:
            out["look_set"] = []
        return out
    if kind not in TYPES or (look is None and look_set is None):
        return params
    if look is not None and not isinstance(look, dict):
        return params
    if look_set is not None and (not isinstance(look_set, list)
                                 or not all(isinstance(n, str) for n in look_set)):
        return params
    own = fields_of(kind)
    new_look, new_set = look, look_set
    if look is not None:
        new_look = {}
        for key, value in look.items():
            if key in _FIELDS and key not in own:
                continue                                # the type lacks it
            # The default reads the incoming look, never new_look (still being built): a height
            # after leg_spacing in key order counts as much as one before it (D1486).
            if key in own and key not in (look_set or ()) and not isinstance(value, bool) \
                    and value == default(kind, key, look) \
                    and value_problem(kind, key, value) is None:
                continue                                # today's value, nobody set it
            new_look[key] = value
        raw = look.get("openings")
        if "openings" in look and _openings_ok(raw):
            merged = _merged_openings(raw, piece_length(params))
            if merged:
                new_look["openings"] = merged
            else:
                new_look.pop("openings", None)
        if isinstance(before, Mapping) and before.get("type") != kind:
            hand = set(look_set or ())
            new_look = {k: v for k, v in new_look.items()
                        if k not in own or k in hand or value_problem(kind, k, v) is None}
            for found in _cross_problems("", "", kind, new_look):
                for key in _CROSS_FIELDS[found.fields["case"]]:
                    if key not in hand:
                        new_look.pop(key, None)
    if look_set is not None:
        new_set = [n for n in look_set if not (n in _FIELDS and n not in own)]
    if new_look == look and new_set == look_set:
        return params
    out = dict(params)
    if look is not None:
        out["look"] = new_look
    if look_set is not None:
        out["look_set"] = new_set
    return out


def inherit(source_params: Mapping) -> dict:
    """What a piece snapped to a CONVEYOR takes: its look without the openings, nothing set by hand."""
    return {"look": {k: v for k, v in _stored(source_params).items() if k != "openings"},
            "look_set": []}


def guide_runs(length: float, openings: Sequence[Mapping], side: str) -> list[tuple[float, float]]:
    """The stretches of one side guide that stand: [0, *length*] minus that side's openings.

    Parameters
    ----------
    length : float
        The piece's length, in the openings' unit (metres for `look_of`'s openings).
    openings : sequence of mapping
        `{"side", "start", "end"}` gaps, as `look_of` and `exit_openings` give them; one whose
        side is *side* or `both` cuts the guide.
    side : str
        `left` or `right`, seen along the travel.

    Returns
    -------
    list of (float, float)
        (start, end) runs in order; a run of 1e-9 or less is dropped.

    See Also
    --------
    nagasim.appearance.look_of : The stored openings, in metres.
    nagasim.appearance.exit_openings : The openings the exits cut.
    nagasim.appearance.piece_length_m : The piece's length in metres.

    Examples
    --------
    >>> from nagasim.appearance import guide_runs
    >>> guide_runs(2.0, [{"side": "left", "start": 0.5, "end": 0.9}], "left")
    [(0.0, 0.5), (0.9, 2.0)]
    """
    cuts = sorted((max(0.0, o["start"]), min(length, o["end"])) for o in openings
                  if o.get("side") in (side, "both"))
    runs, cursor = [], 0.0
    for start, end in cuts:
        if start > cursor:
            runs.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        runs.append((cursor, length))
    return [(a, b) for a, b in runs if b - a > 1e-9]


def _exit_cut(at: float, side: str, width: float, across: float) -> dict:
    # An exit *width* wide leaving at an angle whose sine is *across* crosses the side over
    # width / sine; a near-parallel one is held at 2 widths (a 30 degree exit's cut), so the
    # guide stays standing along a shallow divert or merge.
    half = width / max(abs(across), 0.5) / 2
    return {"side": side, "start": max(0.0, at - half), "end": at + half}


def exit_openings(document: "ModelDocument", name: str) -> list[dict]:
    """The gaps a piece's side exits cut in its guides, which the file never stores.

    One gap per divert, on its side, and one per merge, on the side its feeder ends (by the
    feeder's end point, else its heading). Each is centred on the exit's point and as long as
    the exit crosses the side: the piece's own width for a divert, the feeder's for a merge, over
    the sine of their angle. Draw the guides with `look_of`'s openings plus these.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.
    name : str
        The CONVEYOR block's name.

    Returns
    -------
    list of dict
        `{"side", "start", "end"}` in metres along the piece's path; [] for a missing block, a
        block that is not a conveyor or a piece not on the floor. A feeder not on the floor
        cuts nothing.

    See Also
    --------
    nagasim.appearance.look_of : The stored openings and the rest of the look.
    nagasim.appearance.guide_runs : The guide runs between the openings.

    Examples
    --------
    >>> from nagasim.appearance import exit_openings
    >>> from nagasim.appearance import ModelDocument
    >>> exit_openings(ModelDocument(), "missing")
    []
    """
    # The cuts are D1479's; both drawings add them to the stored openings.
    from nagasim.core import floor
    block = document.blocks.get(name)
    if block is None or block.type != "CONVEYOR":
        return []
    params = block.params or {}
    merges = params.get("merges") if isinstance(params.get("merges"), list) else []
    if not params.get("diverts") and not merges:
        return []
    geometry = floor.floor_geometry(document, name)
    if geometry is None:
        return []
    start, segments, diverts = geometry
    out = []
    for divert in diverts if isinstance(diverts, list) else []:
        at = divert.get("at") if isinstance(divert, dict) else None
        if _number(at):
            side = "right" if divert.get("side") == "right" else "left"
            across = math.sin(math.radians(conveyor_path.divert_angle(divert)))
            out.append(_exit_cut(float(at), side, floor.belt_width_m(params), across))
    factor = floor.length_factor(document, params)
    for merge in merges:
        at, feeder = (merge.get("at"), merge.get("from")) if isinstance(merge, dict) \
            else (None, None)
        feed = floor.floor_geometry(document, feeder) if isinstance(feeder, str) else None
        if not _number(at) or feed is None or not feed[1]:
            continue
        s = float(at) * factor
        pose = conveyor_path.pose_at(start, segments, s)
        end = conveyor_path.end_pose(feed[0], feed[1])
        h = math.radians(pose.heading)
        left = (end.x - pose.x) * math.sin(h) - (end.y - pose.y) * math.cos(h)
        # Headings rise clockwise (y south): a feeder turned right of the travel comes in
        # from the left.
        across = math.sin(math.radians(end.heading - pose.heading))
        if abs(left) > 1e-9:
            side = "left" if left > 0 else "right"
        elif abs(across) > 1e-9:
            side = "left" if across > 0 else "right"
        else:
            continue                    # in line with the travel: no side to open
        out.append(_exit_cut(s, side, floor.belt_width_m(document.blocks[feeder].params),
                             across))
    return out


# the fields each cross rule of `_cross_problems` reads
_CROSS_FIELDS = {"diameter": ("roller_diameter", "roller_pitch"),
                 "strands": ("chain_strands", "strand_spacing", "width"),
                 "roller_height": ("roller_diameter", "height"),
                 "pulley_height": ("pulley_diameter", "height")}


def _cross_problems(name: str, at: str, kind: str, stored: dict) -> list[Problem]:
    """Rules that read two fields; each uses a stored value only when it is valid itself."""
    def pick(field):
        value = stored.get(field)
        return value if field in stored and value_problem(kind, field, value) is None \
            else default(kind, field, stored)

    out = []
    if kind in ROLLER_TYPES and pick("roller_diameter") > pick("roller_pitch"):
        out.append(problem(
            "E_CONVEYOR_LOOK", f"{name}: roller_diameter must not exceed roller_pitch",
            f"{at}/look", block=name, hint="make the rollers thinner or the pitch longer",
            fields={"case": "diameter", "field": "roller_diameter"}))
    # The outer strands' outer edges stay inboard of the rails' inner faces (D1471).
    if kind == "chain" and (pick("chain_strands") - 1) * pick("strand_spacing") \
            > pick("width") - 2 * RAIL_DEPTH_M - STRAND_WIDTH_M + 1e-9:
        out.append(problem(
            "E_CONVEYOR_LOOK", f"{name}: the chain strands do not fit within the width",
            f"{at}/look", block=name,
            hint="fewer strands, a smaller strand_spacing or a larger width",
            fields={"case": "strands", "field": "strand_spacing"}))
    # The kit stands a roller or a belt's pulley at height - diameter: no lower than the floor.
    if kind in ROLLER_TYPES and pick("roller_diameter") > pick("height"):
        out.append(problem(
            "E_CONVEYOR_LOOK", f"{name}: roller_diameter must not exceed the height",
            f"{at}/look", block=name, hint="make the rollers thinner or the height larger",
            fields={"case": "roller_height", "field": "roller_diameter"}))
    if kind == "belt" and pick("pulley_diameter") > pick("height"):
        out.append(problem(
            "E_CONVEYOR_LOOK", f"{name}: pulley_diameter must not exceed the height",
            f"{at}/look", block=name, hint="make the pulleys smaller or the height larger",
            fields={"case": "pulley_height", "field": "pulley_diameter"}))
    return out


def look_problems(name: str, params: Mapping, at: str) -> list[Problem]:
    """`E_CONVEYOR_LOOK` problems of one CONVEYOR, at `<at>/look` or `<at>/look_set`."""
    look, look_set = params.get("look"), params.get("look_set")
    kind = params.get("type")
    out: list[Problem] = []
    if kind not in TYPES:
        for leaf, value in (("look", look), ("look_set", look_set)):
            if value:
                out.append(problem(
                    "E_CONVEYOR_LOOK", f"{name}: {leaf} needs a conveyor type", f"{at}/{leaf}",
                    block=name, hint="set type to belt, roller, chain, pallet or mdr first",
                    fields={"case": "untyped", "field": leaf}))
        return out
    own = fields_of(kind)
    if look is not None and not isinstance(look, dict):
        out.append(problem("E_CONVEYOR_LOOK", f"{name}: look must be an object", f"{at}/look",
                           block=name, fields={"case": "shape", "field": "look"}))
        look = None
    if look_set is not None and not isinstance(look_set, list):
        out.append(problem("E_CONVEYOR_LOOK", f"{name}: look_set must be a list",
                           f"{at}/look_set", block=name,
                           fields={"case": "shape", "field": "look_set"}))
        look_set = None
    for key, value in (look or {}).items():
        if key == "openings":
            out.extend(_opening_problems(name, at, value))
        elif key not in own:
            out.append(problem(
                "E_CONVEYOR_LOOK", f"{name}: {key} is not a look setting of a {kind} conveyor",
                f"{at}/look", block=name, hint=f"its settings: {', '.join(own)}, openings",
                fields={"case": "field", "field": str(key)}))
        else:
            why = value_problem(kind, key, value)
            if why:
                out.append(problem("E_CONVEYOR_LOOK", f"{name}: {why}", f"{at}/look", block=name,
                                   hint=f"change {key} in look",
                                   fields={"case": "value", "field": key}))
    if isinstance(look, dict):
        out.extend(_cross_problems(name, at, kind, look))
    seen = set()
    for entry in look_set or []:
        if not isinstance(entry, str) or entry not in own:
            out.append(problem(
                "E_CONVEYOR_LOOK", f"{name}: look_set names {entry!r}, which is not a look "
                f"setting of a {kind} conveyor", f"{at}/look_set", block=name,
                hint="openings are never set by hand either",
                fields={"case": "set_field", "field": str(entry)}))
        elif entry in seen:
            out.append(problem("E_CONVEYOR_LOOK", f"{name}: look_set names {entry} twice",
                               f"{at}/look_set", block=name,
                               fields={"case": "set_twice", "field": entry}))
        seen.add(entry)
    return out


def _opening_problems(name: str, at: str, raw) -> list[Problem]:
    if not isinstance(raw, list):
        return [problem("E_CONVEYOR_LOOK", f"{name}: openings must be a list", f"{at}/look",
                        block=name, fields={"case": "opening", "field": "openings"})]
    out = []
    for index, item in enumerate(raw):
        if not _opening_ok(item):
            out.append(problem(
                "E_CONVEYOR_LOOK", f"{name}: opening {index} must be {{side, start, end}} with "
                f"side left, right or both and 0 <= start < end", f"{at}/look", block=name,
                hint="positions are in the belt's length unit",
                fields={"case": "opening", "field": "openings"}))
    return out


def _property(field: str) -> dict:
    shape = _FIELDS[field][0]
    types = ", ".join(_FIELDS[field][1])
    if shape == "choice":
        every = sorted({v for k in _FIELDS[field][1] for v in choices(k, field)},
                       key=lambda v: _CHOICES[field].index(v))
        if field == "frame_color":
            return {"anyOf": [{"type": "string", "enum": every},
                              {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}],
                    "description": f"Choice or a colour #RRGGBB, for {types}."}
        return {"type": "string", "enum": every, "description": f"Choice for {types}."}
    if shape == "integer":
        return {"type": "integer", "minimum": 1, "maximum": MAX_STRANDS,
                "description": f"Count, for {types}."}
    low, high = _BOUNDS[field]
    if field in _ZERO_OK and low:
        return {"anyOf": [{"const": 0}, {"type": "number", "minimum": low, "maximum": high}],
                "description": f"Metres, 0 for none or from {low:g} to {high:g}, for {types}."}
    if field == "height":
        return {"type": "number", "minimum": low, "maximum": high,
                "description": f"Metres, from the frame's {FRAME_HEIGHT_M:g} (a lower type: its "
                               f"own height, {low:g} on a pallet) to {high:g}, for {types}."}
    if field in _ZERO_OK:       # 0 is inside the range: one plain number, no anyOf
        return {"type": "number", "minimum": 0, "maximum": high,
                "description": f"Metres, 0 to {high:g}, for {types}."}
    return {"type": "number", "minimum": low, "maximum": high,
            "description": f"Metres, from {low:g} to {high:g}, for {types}."}


def look_schema() -> dict:
    """The JSON Schema of a CONVEYOR's `look`."""
    props = {f: _property(f) for f in _NAMES}
    props["openings"] = {
        "type": "array",
        "description": "Gaps in the side guides, in the belt's length unit.",
        "items": {"type": "object",
                  "properties": {"side": {"type": "string", "enum": list(OPENING_SIDES)},
                                 "start": {"type": "number", "minimum": 0},
                                 "end": {"type": "number", "exclusiveMinimum": 0}},
                  "required": ["side", "start", "end"], "additionalProperties": False}}
    return {"type": "object", "properties": props, "additionalProperties": False,
            "description": "Look settings that differ from the type's defaults."}


def look_set_schema() -> dict:
    """The JSON Schema of a CONVEYOR's `look_set`."""
    return {"type": "array", "uniqueItems": True, "items": {"type": "string", "enum": list(_NAMES)},
            "description": "Look settings kept on this piece when a change spreads along its line."}


# -- the chain and the writes (D1473, D1474) --------------------------------------------------

def _kind_of(document: "ModelDocument", name: str) -> str | None:
    """The conveyor type of block *name*, or None when it is no typed CONVEYOR."""
    block = document.blocks.get(name)
    if block is None or block.type != "CONVEYOR":
        return None
    kind = block.params.get("type")
    return kind if kind in TYPES else None


def chain_of(document: "ModelDocument", name: str) -> list[str]:
    """The pieces of *name*'s chain, upstream first, *name* included; [name] for a lone piece.
    A step joins two CONVEYORs of one type when the first has exactly one link out and the
    second exactly one link in (parallel links count). A ring is listed once."""
    kind = _kind_of(document, name)
    if kind is None:
        return [name]
    links = document.links
    outs = Counter(src for src, _ in links)
    ins = Counter(tgt for _, tgt in links)
    after = {src: tgt for src, tgt in links if outs[src] == 1}
    before = {tgt: src for src, tgt in links if ins[tgt] == 1}
    seen = {name}

    def walk(step: dict, partner_count: Counter) -> list[str]:
        out, here = [], name
        while True:
            there = step.get(here)
            if there is None or there in seen or _kind_of(document, there) != kind \
                    or partner_count[there] != 1:
                return out
            seen.add(there)
            out.append(there)
            here = there

    upstream = walk(before, outs)
    return upstream[::-1] + [name] + walk(after, ins)


def look_set_of(params: Mapping) -> list:
    raw = params.get("look_set")
    return list(raw) if isinstance(raw, list) else []


def chain_value(document: "ModelDocument", name: str, field: str):
    """The value *field* takes when reset on *name*: that of the nearest member upstream not
    keeping it by hand, else the nearest such member downstream, else the type default."""
    kind = _kind_of(document, name)
    if kind is None or field not in fields_of(kind):
        raise ValueError(f"{field} is not a look setting of {name}")
    chain = chain_of(document, name)
    at = chain.index(name)
    for member in chain[:at][::-1] + chain[at + 1:]:
        params = document.blocks[member].params
        if field not in look_set_of(params):
            return look_of(params)[field]
    return default(kind, field, _stored(document.blocks[name].params))


@dataclass(frozen=True)
class LookResult:
    """What a look write did: the pieces whose params changed, in chain order, and for each
    skipped piece the fields it keeps by hand."""
    changed: list[str] = _field(default_factory=list)
    skipped: dict[str, list[str]] = _field(default_factory=dict)


def _with_look(params: dict, look: dict, look_set: list) -> dict:
    """*params* with *look* and *look_set*; a key that was absent is added only when its value
    is not empty, so a short-form piece gains no keys."""
    out = dict(params)
    if look or "look" in params:
        out["look"] = look
    if look_set or "look_set" in params:
        out["look_set"] = look_set
    return out


def _refusal(document: "ModelDocument", name: str, look: Mapping, scope: str,
             reset: Sequence[str]) -> str | None:
    """Why a look write is refused (the fields and values named), or None."""
    kind = _kind_of(document, name)
    if kind is None:
        return "it is not a conveyor with a type, so it has no look"
    if scope not in ("chain", "piece"):
        return f"scope must be chain or piece, not {scope!r}"
    reasons = []
    for key, value in look.items():
        if key == "openings":
            if not _openings_ok(value):
                reasons.append("openings must be a list of {side, start, end} with side left, "
                               "right or both and 0 <= start < end")
        else:
            reasons.append(value_problem(kind, key, value))
    for key in reset:
        if key == "openings":
            reasons.append("openings cannot be reset, only set")
        elif key not in fields_of(kind):
            reasons.append(f"{key} is not a setting of a {kind} conveyor")
    both = [key for key in look if key in reset]
    if both:
        reasons.append(f"{', '.join(both)} cannot be both set and reset")
    return "; ".join(r for r in reasons if r) or None


def apply_look(document: "ModelDocument", name: str, look: Mapping | None = None,
               scope: str = "chain", reset: Sequence[str] = ()) -> LookResult:
    """Set the *look* fields on *name* and, with scope "chain", on each member of its chain
    that does not keep the field by hand; put the *reset* fields of *name* back to the chain's
    value. Openings stay on *name*. One undo step; a refused field or value raises ValueError
    before anything is written."""
    look = dict(look or {})
    reset = [str(key) for key in reset]
    why = _refusal(document, name, look, scope, reset)
    if why:
        raise ValueError(f"{name}: {why}")
    chain = chain_of(document, name)
    spread = {k: v for k, v in look.items() if k != "openings"}
    values = {key: chain_value(document, name, key) for key in reset}
    before = {member: document.blocks[member].params for member in chain}
    skipped: dict[str, list[str]] = {}
    writes: dict[str, dict] = {}
    for member in chain:
        if member != name and scope == "piece":
            continue
        params = before[member]
        own, kept = dict(_stored(params)), look_set_of(params)
        if member == name:
            own.update(spread)
            own.update(values)
            if "openings" in look:
                own["openings"] = [dict(o) for o in look["openings"]]
            if scope == "piece":                 # set by hand: later chain writes skip it
                kept += [k for k in spread if k not in kept]
            # A chain write named here takes the value and keeps a hand mark: only reset
            # removes one (spec Builder check item 3, D1474).
            kept = [k for k in kept if k not in values]
        else:
            held = [k for k in spread if k in kept]
            if held:
                skipped[member] = held
            own.update({k: v for k, v in spread.items() if k not in kept})
        writes[member] = _with_look(params, own, kept)
    # A write may not give a piece a cross-field problem it did not have (D1474): refuse it whole.
    # The named piece first: a chain member failing for the same values is not the cause.
    for member, new in sorted(writes.items(), key=lambda item: item[0] != name):
        kind = _kind_of(document, member)
        had = {p.fields["case"] for p in _cross_problems(member, "", kind, _stored(before[member]))}
        for found in _cross_problems(member, "", kind, _stored(new)):
            if found.fields["case"] not in had:
                if member == name:
                    raise ValueError(f"{found.message}; change both fields in one call")
                # the clash is on a chain piece that holds one of the rule's fields by hand
                rule = _CROSS_FIELDS[found.fields["case"]]
                held = [k for k in look_set_of(before[member]) if k in rule] or [rule[0]]
                raise ValueError(f"{found.message}; {member} keeps {held[0]} by hand, so "
                                 f"change that field on {member} first")
    with document.transaction("Edit look"):
        for member, new in writes.items():
            document.set_params(member, new)
    changed = [m for m in chain if document.blocks[m].params != before[m]]
    return LookResult(changed=changed, skipped=skipped)


def set_openings(document: "ModelDocument", name: str, openings: list[dict]) -> None:
    """Write the openings of piece *name* (the 2D drag, the panel): one undo step."""
    why = _refusal(document, name, {"openings": openings}, "piece", ())
    if why:
        raise ValueError(f"{name}: {why}")
    params = document.blocks[name].params
    own = dict(_stored(params))
    own["openings"] = [dict(o) for o in openings]
    document.set_params(name, _with_look(params, own, look_set_of(params)))
