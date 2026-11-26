"""How a model looks: the calls another tool draws a model with, as the reference drawing does.

The calls read a `ModelDocument` (`ModelDocument.load(path)`) or one block's params, and give
each conveyor piece's place on the floor, its look fields and materials, and each entity type's
form and size. Dimensions are metres and colours are sRGB `#RRGGBB` (`linear_rgb(srgb(c))`
gives a glTF base colour). Draw a side guide with `guide_material()`, since a panel guide takes
the frame's material and `MATERIALS["guide_style"]["panel"]` is only a placeholder.

This module is provisional: it may change between minor versions until a second tool uses
it. Its names are those of the core modules, so a script that imports from here keeps working
when they move; the four `piece_*` calls are its own, read-only wrappers of the floor geometry.

    from nagasim import appearance

    doc = appearance.ModelDocument.load("nagasim/models/conveyor_line.json")
    forms = appearance.forms_of(doc)              # {TYPE: ItemForm(form, size, prop)}
    for name, block in doc.blocks.items():
        look = appearance.look_of(block.params, appearance.length_factor(doc, block.params))
        start = appearance.piece_start(doc, name)    # None: not a piece on the floor
        if look and start:
            frame = appearance.material("frame_color", look["frame_color"])
            middle = appearance.piece_pose(doc, name, appearance.piece_length_m(doc, name) / 2)
"""
from __future__ import annotations

from nagasim.core import conveyor_path
from nagasim.core.conveyor_look import (FRAME_HEIGHT_M, RAIL_DEPTH_M, RAISED_PALLET_M, default,
                                        exit_openings, fields_of, guide_runs, look_of)
from nagasim.core.conveyor_path import Pose, Segment
from nagasim.core.document import ModelDocument
from nagasim.core.entity_forms import FORMS, ItemForm, default_carton_size, form_of, forms_of
from nagasim.core.floor import floor_geometry, length_factor
from nagasim.core.look_materials import (MATERIALS, POST_CAP, Material, guide_material, is_hex,
                                         linear_rgb, material, srgb)

__all__ = ["Material", "MATERIALS", "POST_CAP", "material", "guide_material", "srgb",
           "linear_rgb", "is_hex", "fields_of", "default", "look_of", "length_factor",
           "guide_runs", "exit_openings", "RAISED_PALLET_M", "RAIL_DEPTH_M", "FRAME_HEIGHT_M",
           "FORMS", "ItemForm", "form_of", "forms_of", "default_carton_size", "ModelDocument",
           "Pose", "Segment", "piece_start", "piece_segments", "piece_pose", "piece_length_m"]


def piece_start(document: ModelDocument, name: str) -> Pose | None:
    """Where a conveyor piece starts on the floor: its position and heading.

    The pose is the block's `layout_position` and `layout_rotation`, in plan
    coordinates: x east and y south in metres, and a heading in degrees clockwise on screen,
    0 pointing east and 90 south. A piece is drawn from here along its `path`.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.
    name : str
        The CONVEYOR block's name.

    Returns
    -------
    Pose or None
        `Pose(x, y, heading)`; None for a missing block, a block that is not a CONVEYOR, a
        piece with no `path` (the reference drawing shows it as a flat tile) or a path the
        engine refuses. An unplaced piece starts at (0, 0).

    See Also
    --------
    nagasim.appearance.piece_segments : The pieces of path laid from this pose.
    nagasim.appearance.piece_pose : The pose at a distance along the path.

    Examples
    --------
    >>> from nagasim.appearance import ModelDocument, piece_start
    >>> doc = ModelDocument({"blocks": [{"name": "Belt", "type": "CONVEYOR",
    ...     "layout_position": [2, 3], "layout_rotation": 90,
    ...     "params": {"type": "roller", "path": [{"straight": 4}]}}]})
    >>> piece_start(doc, "Belt")
    Pose(x=2.0, y=3.0, heading=90.0)
    >>> piece_start(doc, "Missing") is None
    True
    """
    geometry = floor_geometry(document, name)
    return None if geometry is None else geometry[0]


def piece_segments(document: ModelDocument, name: str) -> list[Segment] | None:
    """A conveyor piece's path as straight and arc segments, in metres.

    The segments are laid end to end from `piece_start`. A straight has its
    `length`; an arc has its `radius`, the `angle` it sweeps in degrees and its `turn`, `left`
    (counter-clockwise on screen, the heading falls) or `right` (clockwise, the heading rises),
    and its `length` is the arc's. The lengths and radii are in metres whatever the belt's
    length unit.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.
    name : str
        The CONVEYOR block's name.

    Returns
    -------
    list of Segment or None
        `Segment(kind, length, radius, angle, turn)` per piece of path, `kind` "straight" or
        "arc"; None in the cases `piece_start` returns None.

    See Also
    --------
    nagasim.appearance.piece_start : Where the first segment starts.
    nagasim.appearance.piece_length_m : The segments' total length.

    Examples
    --------
    >>> from nagasim.appearance import ModelDocument, piece_segments
    >>> doc = ModelDocument({"simulation": {"length_unit": "millimeters"}, "blocks": [
    ...     {"name": "Belt", "type": "CONVEYOR", "params": {"type": "belt",
    ...      "path": [{"straight": 2000}, {"arc": {"radius": 500, "angle": 90, "turn": "left"}}]}}]})
    >>> [(s.kind, round(s.length, 3), s.radius, s.turn) for s in piece_segments(doc, "Belt")]
    [('straight', 2.0, 0.0, ''), ('arc', 0.785, 0.5, 'left')]
    """
    geometry = floor_geometry(document, name)
    return None if geometry is None else list(geometry[1])


def piece_pose(document: ModelDocument, name: str, distance: float) -> Pose | None:
    """The pose on a conveyor piece's centreline at a distance along its path.

    *distance* is in metres from the piece's start and is clamped to the piece,
    so 0 gives `piece_start` and `piece_length_m` gives its end. The heading is the direction of
    travel there, in degrees clockwise on screen; it is not folded into [0, 360), so a left arc
    from a heading of 0 ends at -90. The reference drawing places each leg, roller, drive and
    guide post at such a pose.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.
    name : str
        The CONVEYOR block's name.
    distance : float
        Metres along the path from the piece's start.

    Returns
    -------
    Pose or None
        `Pose(x, y, heading)` in plan coordinates; None in the cases `piece_start` returns None.

    See Also
    --------
    nagasim.appearance.piece_start : The pose at distance 0.
    nagasim.appearance.piece_length_m : The distance of the piece's end.

    Examples
    --------
    >>> from nagasim.appearance import ModelDocument, piece_pose
    >>> doc = ModelDocument({"blocks": [{"name": "Belt", "type": "CONVEYOR",
    ...     "params": {"type": "roller",
    ...                "path": [{"straight": 4}, {"arc": {"radius": 1, "angle": 90, "turn": "right"}}]}}]})
    >>> piece_pose(doc, "Belt", 2)
    Pose(x=2.0, y=0.0, heading=0.0)
    >>> end = piece_pose(doc, "Belt", 100)
    >>> round(end.x, 3), round(end.y, 3), round(end.heading, 3)
    (5.0, 1.0, 90.0)
    """
    geometry = floor_geometry(document, name)
    if geometry is None:
        return None
    start, segments, _diverts = geometry
    return conveyor_path.pose_at(start, segments, distance)


def piece_length_m(document: ModelDocument, name: str) -> float | None:
    """A conveyor piece's length along its path, in metres.

    It is the sum of `piece_segments`' lengths, whatever the belt's length unit;
    `guide_runs(length, openings, side)` and the placement rules of the Appearance page take
    it.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.
    name : str
        The CONVEYOR block's name.

    Returns
    -------
    float or None
        The length in metres; None in the cases `piece_start` returns None.

    See Also
    --------
    nagasim.appearance.piece_segments : The segments it sums.
    nagasim.appearance.guide_runs : The guide stretches over this length.

    Examples
    --------
    >>> from nagasim.appearance import ModelDocument, piece_length_m
    >>> doc = ModelDocument({"simulation": {"length_unit": "millimeters"}, "blocks": [
    ...     {"name": "Belt", "type": "CONVEYOR",
    ...      "params": {"type": "belt", "path": [{"straight": 2500}]}}]})
    >>> piece_length_m(doc, "Belt")
    2.5
    """
    geometry = floor_geometry(document, name)
    return None if geometry is None else conveyor_path.path_length(geometry[1])
