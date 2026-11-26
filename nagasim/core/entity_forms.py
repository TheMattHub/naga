"""Every entity type's form and drawn size in 3D (D1458, D1462, D1487).

The form is inferred from the fields an Entities row already has (name, `icon`, `size`) and from
the BATCH `carrier_type`s, because no row field names a 3D graphic and adding one would change the
engine's schema. `forms_of` gives one `ItemForm` per listed type, so another renderer can draw a
model's items as the editor does. A `prop` is named here with its size (D1488); which mesh draws
it, and what to draw when that mesh is missing, is the renderer's business. Pure: no Qt.
"""
from __future__ import annotations

import re
from typing import Mapping, NamedTuple

__all__ = [
    "FORMS", "DEFAULT_CARTON_M", "MIN_CARTON_M", "EPAL_M", "PERSON_M", "VEHICLE_M", "CONTAINER_M",
    "PROP_M", "PERSON_WORDS", "VEHICLE_WORDS", "CONTAINER_WORDS", "ISO_FLOW_WORDS", "INFO_WORDS",
    "PROP_WORDS", "ItemForm", "full_sizes", "form_of", "default_carton_size", "forms_of",
]

# `prop` (controller ruling 2026-11-26): a shipped mesh at its own size.
FORMS = ("carton", "container", "pallet", "person", "vehicle", "disc", "prop")

DEFAULT_CARTON_M = (0.40, 0.30, 0.25)
# The default carton shrinks no shorter than this: one belt with a 2 cm spacing made every unsized
# type of the model a 2 cm box, in its queue rows too (final review M8).
MIN_CARTON_M = 0.10
EPAL_M = (1.2, 0.8, 0.144)
PERSON_M = (0.30, 0.50, 1.70)          # front to back, shoulder width, height
VEHICLE_M = (2.4, 1.08, 1.8)           # the mover box (scene3d.MOVER_LENGTH_M, 0.45 wide, 1.8 tall)
CONTAINER_M = {20: (6.058, 2.438, 2.591), 40: (12.192, 2.438, 2.591), 45: (13.716, 2.438, 2.591)}
# Copied from the shipped meshes' `size_m` and `height_m` (D1488); a product test pins them to the
# manifest, so a re-export that changes a mesh's size fails there instead of leaving a stale copy.
PROP_M = {"stretcher": (2.1, 0.95, 1.15), "wheelchair": (1.14, 0.674, 0.94)}

PERSON_WORDS = frozenset(
    "PERSON PEOPLE PATIENT PASSENGER PAX CUSTOMER CLIENT VISITOR GUEST WORKER OPERATOR STAFF "
    "NURSE DOCTOR PEDESTRIAN SHOPPER STUDENT".split())
VEHICLE_WORDS = frozenset(
    "TRUCK LORRY VAN CAR BUS TRAILER TRACTOR VEHICLE EV FORKLIFT AGV AMR TUG ROBOT DRONE".split())
CONTAINER_WORDS = frozenset("CONTAINER TEU FEU".split())
ISO_FLOW_WORDS = frozenset("EXPORT IMPORT TRANSSHIP TRANSSHIPMENT REEFER EMPTY ISO".split())
INFO_WORDS = frozenset(
    "ORDER CALL DOCUMENT REVIEW REQUEST MESSAGE SIGNAL TICKET TASK EMAIL ESCALATION FLIGHT "
    "AIRCRAFT SHIP VESSEL".split())
# Name word -> prop name; checked before PERSON_WORDS, so a PATIENT_STRETCHER is the stretcher,
# not a standing person.
PROP_WORDS = {"STRETCHER": "stretcher", "WHEELCHAIR": "wheelchair"}

_ICON_FORMS = {"person": "person", "truck": "vehicle", "box": "carton", "square": "carton",
               "document": "disc", "gear": "disc", "bolt": "disc", "drop": "disc"}
_WORD_SPLIT = re.compile(r"[^A-Z0-9]+")


class ItemForm(NamedTuple):
    """One type's form, its drawn size in metres (length, width, height; None for a disc) and,
    for an undeclared `prop`, the prop's name (a `PROP_M` key). A declared size wins over a prop,
    so a sized prop has no name and draws as the plain box at its size."""

    form: str
    size: tuple[float, float, float] | None
    prop: str | None = None


def _words(name) -> list[str]:
    return [w for w in _WORD_SPLIT.split(str(name or "").upper()) if w]


def full_sizes(entities, length_unit: str | None) -> dict:
    """`{TYPE NAME: (length, width, height)}` in metres for the rows with a full size (D1445): a
    partial size counts as none. The sizes are in the model's length unit, which the engine
    converts to metres only for the belt, so the drawing must do it too."""
    from nagasim.core.timeunits import conversion_factor

    factor = conversion_factor("length", length_unit or "meters", "meters")
    out = {}
    for row in entities or ():
        size = (row.get("size") or {}) if isinstance(row, dict) else {}
        try:
            dims = tuple(float(size[k]) for k in ("length", "width", "height"))
        except (KeyError, TypeError, ValueError):
            continue
        if all(d > 0 for d in dims) and row.get("name"):
            out[str(row["name"]).upper()] = tuple(d * factor for d in dims)
    return out


def _prop_word(words) -> str | None:
    return next((w for w in words if w in PROP_WORDS), None)


def form_of(row: Mapping, pallet_carriers: frozenset[str]) -> str:
    """The form one entity type draws as, from its name and icon; one of `FORMS`.

    In order: a pallet carrier or a name with PALLET is a pallet; then the row's `icon`; then a
    word of the name (a prop such as STRETCHER, a person, a vehicle, a container, an information
    item drawn as a disc); else a carton. The size rule (a sized disc is a carton) belongs to
    `forms_of`, so call `forms_of` for a whole model.

    Parameters
    ----------
    row : mapping
        An Entities row, or `{"name": TYPE}` for a type with no row.
    pallet_carriers : frozenset of str
        The upper-cased `carrier_type`s of BATCH blocks with a `pattern`.

    Returns
    -------
    str
        `carton`, `container`, `pallet`, `person`, `vehicle`, `disc` or `prop`.

    See Also
    --------
    nagasim.appearance.forms_of : The form and size of every entity type of a model.

    Examples
    --------
    >>> from nagasim.appearance import form_of
    >>> form_of({"name": "PATIENT"}, frozenset())
    'person'
    """
    # The rules are D1462's 1-7 plus the `prop` ruling.
    name = str(row.get("name") or "").upper()
    if name in pallet_carriers or "PALLET" in name:
        return "pallet"
    icon = _ICON_FORMS.get(str(row.get("icon") or "").strip().lower())
    if icon is not None:
        return icon
    words = _words(name)
    if _prop_word(words) is not None:
        return "prop"
    for vocabulary, form in ((PERSON_WORDS, "person"), (VEHICLE_WORDS, "vehicle")):
        if any(w in vocabulary for w in words):
            return form
    # A bare 20/40/45 is too common in part names (PART_40): a size counts only beside a box-flow word.
    if any(w in CONTAINER_WORDS for w in words) or (
            any(w in ("20", "40", "45") for w in words) and any(w in ISO_FLOW_WORDS for w in words)):
        return "container"
    if any(w in INFO_WORDS for w in words):
        return "disc"
    return "carton"


def _container_size(words) -> tuple[float, float, float]:
    if "FEU" in words:          # a forty-foot equivalent unit (final review M1)
        return CONTAINER_M[40]
    for feet in (40, 45):
        if str(feet) in words:
            return CONTAINER_M[feet]
    return CONTAINER_M[20]


def _blocks(document):
    blocks = getattr(document, "blocks", None) or {}
    return blocks.values() if isinstance(blocks, Mapping) else blocks


def default_carton_size(document) -> tuple[float, float, float]:
    """The size of a carton whose type declares none, in metres (length, width, height).

    0.40 x 0.30 x 0.25 m, scaled uniformly by `min(1, max(shortest, 0.10) / 0.40)` in a model
    with a conveyor, so a carton fits the shortest belt. *shortest* is the least part length a
    belt gives an unsized part: its `part_length`, else its type's zone length, else its
    `spacing`, in metres. A belt of an unknown type or unit is skipped.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.

    Returns
    -------
    tuple of float
        (length, width, height) in metres.

    See Also
    --------
    nagasim.appearance.forms_of : Uses this size for a carton that declares none.

    Examples
    --------
    >>> from nagasim.appearance import default_carton_size
    >>> from nagasim.appearance import ModelDocument
    >>> default_carton_size(ModelDocument())
    (0.4, 0.3, 0.25)
    """
    # The shrink is D1458's.
    from nagasim.core.blocks.conveyor import SPEC
    from nagasim.core.conveyor_types import TYPES
    from nagasim.core.quantities import field_factor, param
    from nagasim.core.timeunits import units_of

    units = units_of(getattr(document, "simulation", None))
    shortest = None
    for block in _blocks(document):
        if str(getattr(block, "type", "") or "") != "CONVEYOR":
            continue
        params = getattr(block, "params", None) or {}
        kind = str(params.get("type") or "")
        try:
            def measured(name, default):
                raw = float(params.get(name, default) or default)
                return raw * field_factor(param(SPEC, name), params, units, SPEC.params)

            length = measured("part_length", 0.0)
            if length <= 0:
                length = TYPES[kind].zone_length if kind else measured("spacing", 1.0)
        except (KeyError, TypeError, ValueError):
            continue
        if length > 0 and (shortest is None or length < shortest):
            shortest = length
    if shortest is None:
        return DEFAULT_CARTON_M
    scale = min(1.0, max(shortest, MIN_CARTON_M) / DEFAULT_CARTON_M[0])
    return tuple(d * scale for d in DEFAULT_CARTON_M)


def forms_of(document) -> dict[str, ItemForm]:
    """The form and drawn size of every entity type in a model.

    One `ItemForm` per Entities name and BATCH `carrier_type`, upper-cased. A declared `size`
    wins; else the form's own size (a EUR pallet, a person, a vehicle, an ISO container by its
    20, 40 or 45 feet); a type with no row is the `default_carton_size` carton. A `prop` (such
    as a stretcher) gets its `PROP_M` size and name: which mesh draws it, and what to draw
    without one, is the renderer's choice. A disc has no size.

    Parameters
    ----------
    document : ModelDocument
        The model, `ModelDocument.load(path)`.

    Returns
    -------
    dict of str to ItemForm
        `{TYPE: ItemForm(form, size, prop)}`, sizes in metres (length, width, height).

    See Also
    --------
    nagasim.appearance.default_carton_size : The size of an unsized carton.
    nagasim.appearance.form_of : The form rules for one row.

    Examples
    --------
    >>> from nagasim.appearance import forms_of
    >>> from nagasim.appearance import ModelDocument
    >>> forms_of(ModelDocument({"entities": [{"name": "Patient"}]}))["PATIENT"]
    ItemForm(form='person', size=(0.3, 0.5, 1.7), prop=None)
    """
    simulation = getattr(document, "simulation", None) or {}
    sized = full_sizes(getattr(document, "entities", None), simulation.get("length_unit"))
    pallet_carriers, carriers = set(), []
    for block in _blocks(document):
        if str(getattr(block, "type", "") or "") != "BATCH":
            continue
        params = getattr(block, "params", None) or {}
        carrier = str(params.get("carrier_type") or "").strip().upper()
        if carrier:
            carriers.append(carrier)
            if params.get("pattern"):
                pallet_carriers.add(carrier)
    pallet_carriers = frozenset(pallet_carriers)
    rows = {}
    for row in getattr(document, "entities", None) or ():
        if isinstance(row, dict) and row.get("name"):
            rows.setdefault(str(row["name"]).upper(), row)
    for carrier in carriers:
        rows.setdefault(carrier, {"name": carrier})
    fallback = default_carton_size(document)
    out = {}
    for key, row in rows.items():
        form = form_of(row, pallet_carriers)
        size = sized.get(key)
        if size is not None:
            # A declared size wins over a prop's shipped mesh, which draws at its own size: the
            # plain box at the declared size keeps the drawing and the row slot the same size
            # (checkpoint 2 review M1).
            out[key] = ItemForm("carton" if form == "disc" else form, size)
            continue
        words = _words(key)
        if form == "prop":
            prop = PROP_WORDS[_prop_word(words)]
            out[key] = ItemForm("prop", PROP_M[prop], prop)
            continue
        size = {"carton": fallback, "pallet": EPAL_M, "person": PERSON_M,
                "vehicle": VEHICLE_M, "container": _container_size(words),
                "disc": None}[form]
        out[key] = ItemForm(form, size)
    return out
