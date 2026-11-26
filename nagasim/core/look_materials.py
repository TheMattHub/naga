"""The engine's material table for conveyor looks (plan I, D1482-D1484).

Every value of `frame_color`, `belt_kind`, `roller_finish` and `guide_style` maps to a
`Material(color, metallic, roughness)` that any renderer can read; the values are those of the
reference drawing. Colours are sRGB `#RRGGBB`: a glTF base colour needs `linear_rgb(srgb(c))`.
A coloured frame (a name other than steel, or `#RRGGBB`) is paint: metallic 0.15, steel's
roughness, so the hex of steel does not draw as steel. Steel and zinc stay metal. Imports
nothing from `conveyor_look`.
"""
from __future__ import annotations

import re
from typing import NamedTuple

__all__ = ["Material", "MATERIALS", "SLOTS", "POST_CAP", "is_hex", "srgb", "linear_rgb",
           "material", "guide_material"]


class Material(NamedTuple):
    """A surface: an sRGB colour `#RRGGBB` and its metallic and roughness factors, each 0 to 1,
    as a glTF metallic-roughness material takes them (its base colour is
    `linear_rgb(srgb(color))`)."""

    color: str          # "#RRGGBB"
    metallic: float
    roughness: float


_PAINT = 0.15           # painted steel is paint, not metal (ruling, plan I ledger)
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")


def _frame(color: str, metallic: float = _PAINT) -> Material:
    return Material(color, metallic, 0.42)


_BELT = {"black_rubber": "#1C1E21", "green_pvc": "#267340", "white_food": "#EBEBE6",
         "plastic_modular": "#BFC4CC"}
_ROLLER = {"zinc": "#A7AEB4", "grey": "#737880", "blue_sleeve": "#2659B2"}

MATERIALS: dict[str, dict[str, Material]] = {
    "frame_color": {"steel": _frame("#5E646B", 0.85), "grey": _frame("#8C9199"),
                    "blue": _frame("#1F4C9E"), "green": _frame("#337340"),
                    "yellow": _frame("#F2BF1A"), "orange": _frame("#EB731A")},
    "belt_kind": {k: Material(c, 0.0, 0.88) for k, c in _BELT.items()},
    "roller_finish": {k: Material(c, 0.75, 0.5) for k, c in _ROLLER.items()},
    "guide_style": {"round": Material("#A7AEB4", 0.75, 0.5),
                    "flat": Material("#A7AEB4", 0.75, 0.5),
                    "panel": _frame("#5E646B", 0.85)},    # a panel is drawn in the frame's material
}

# The kit palette material each field recolours: a colour override replaces only the base colour.
SLOTS = {"frame_color": "steel", "belt_kind": "rubber", "roller_finish": "galv"}

POST_CAP = Material("#24272B", 0.1, 0.6)


def is_hex(value) -> bool:
    """Whether *value* is a colour `#RRGGBB`: six hex digits, either case, nothing around them.

    Parameters
    ----------
    value : object
        Anything; a non-string is never a colour.

    Returns
    -------
    bool
        True for `"#5e646b"` or `"#5E646B"`; False for `"#abc"`, `"5E646B"` or `"#5E646B "`.

    See Also
    --------
    nagasim.appearance.material : Turns a hex frame colour into paint.

    Examples
    --------
    >>> from nagasim.appearance import is_hex
    >>> is_hex("#1F4C9E"), is_hex("blue")
    (True, False)
    """
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def srgb(color: str) -> tuple[float, float, float]:
    """The sRGB channels of a colour `#RRGGBB`, each 0 to 1.

    Every colour of a look is sRGB. A glTF base colour, and most renderers' shader inputs, are
    linear: pass the result to `linear_rgb`, as `linear_rgb(srgb(c))`.

    Parameters
    ----------
    color : str
        A colour `#RRGGBB`.

    Returns
    -------
    tuple of float
        (red, green, blue), still sRGB-encoded.

    Raises
    ------
    ValueError
        When *color* is not a colour `#RRGGBB`.

    See Also
    --------
    nagasim.appearance.linear_rgb : The linear components a glTF base colour takes.
    nagasim.appearance.material : The material whose colour this reads.

    Examples
    --------
    >>> from nagasim.appearance import srgb
    >>> srgb("#FF8000")
    (1.0, 0.5019607843137255, 0.0)
    """
    if not is_hex(color):
        raise ValueError(f"{color!r} is not a colour #RRGGBB")
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))


def linear_rgb(rgb) -> tuple[float, float, float]:
    """sRGB channels to linear ones, as a glTF base colour takes them.

    The colours of a look are sRGB `#RRGGBB`; a glTF `baseColorFactor` is
    `linear_rgb(srgb(c))`. Written by hand, so a renderer's embedded Python needs no numpy.

    Parameters
    ----------
    rgb : sequence of float
        sRGB channels, each 0 to 1, as `srgb` gives them.

    Returns
    -------
    tuple of float
        The same channels, linear.

    See Also
    --------
    nagasim.appearance.srgb : The sRGB components of a `#RRGGBB` colour.

    Examples
    --------
    >>> from nagasim.appearance import linear_rgb, srgb
    >>> [round(c, 4) for c in linear_rgb(srgb("#808080"))]
    [0.2159, 0.2159, 0.2159]
    """
    def one(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return tuple(one(float(c)) for c in rgb)


def material(field: str, value: str) -> Material:
    """The material that one look value paints, as the reference drawing draws it.

    What each field paints:

    - `frame_color`: the rails, end plates, legs, guide posts and panel guides. A colour
      `#RRGGBB` is always paint (metallic 0.15), even `#5E646B`, the colour of `steel`: only the
      name `steel` draws the frame as bare metal.
    - `belt_kind`: the belt bed, pulley and end plates (a belt only).
    - `roller_finish`: the roller tubes.
    - `guide_style`: the round and flat guide rails. A panel guide is drawn in the frame's
      material, so call `guide_material()` for a guide; `MATERIALS["guide_style"]["panel"]` is
      only a placeholder.

    `POST_CAP` is the cap on top of each guide post, whatever the look. Colours are sRGB
    `#RRGGBB`; a glTF base colour is `linear_rgb(srgb(m.color))`.

    Parameters
    ----------
    field : str
        `frame_color`, `belt_kind`, `roller_finish` or `guide_style`.
    value : str
        One of that field's names (`MATERIALS[field]`), or for `frame_color` a colour `#RRGGBB`.

    Returns
    -------
    Material
        Its colour, metallic and roughness.

    Raises
    ------
    ValueError
        When *field* is not one of the four, or *value* is not one of its values; the message
        names the values it takes.

    See Also
    --------
    nagasim.appearance.guide_material : The material of a side guide, panel included.
    nagasim.appearance.look_of : The look values of one piece.

    Examples
    --------
    >>> from nagasim.appearance import material
    >>> material("frame_color", "blue")
    Material(color='#1F4C9E', metallic=0.15, roughness=0.42)
    >>> material("frame_color", "#5E646B").metallic
    0.15
    """
    if field not in MATERIALS:
        raise ValueError(f"field must be one of {', '.join(MATERIALS)}, not {field!r}")
    table = MATERIALS[field]
    if field == "frame_color" and is_hex(value):
        return _frame(value)
    if isinstance(value, str) and value in table:
        return table[value]
    tail = ", or a colour #RRGGBB" if field == "frame_color" else ""
    raise ValueError(f"{field} must be one of {', '.join(table)}{tail}")


def guide_material(style: str, frame_color: str) -> Material:
    """The material of a piece's side guides; use it rather than the raw `MATERIALS` entry.

    A round or flat guide rail is bare zinc; a panel guide is cut from the frame, so it takes the
    frame's material (`material("frame_color", frame_color)`). Colours are sRGB `#RRGGBB`; a glTF
    base colour is `linear_rgb(srgb(m.color))`.

    Parameters
    ----------
    style : str
        The piece's `guide_style`: `round`, `flat` or `panel`.
    frame_color : str
        The piece's `frame_color`, a name or a colour `#RRGGBB`.

    Returns
    -------
    Material
        The guide's colour, metallic and roughness.

    Raises
    ------
    ValueError
        When *style* is not a guide style, or a panel's *frame_color* is not a frame colour.

    See Also
    --------
    nagasim.appearance.material : The material of any other look value.
    nagasim.appearance.look_of : The piece's `guide_style` and `frame_color`.

    Examples
    --------
    >>> from nagasim.appearance import guide_material, material
    >>> guide_material("panel", "orange") == material("frame_color", "orange")
    True
    """
    if style == "panel":
        return material("frame_color", frame_color)
    if style not in MATERIALS["guide_style"]:
        raise ValueError(f"guide_style must be one of {', '.join(MATERIALS['guide_style'])}")
    return MATERIALS["guide_style"][style]
