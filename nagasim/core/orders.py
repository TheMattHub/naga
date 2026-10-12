"""Orders and due dates: the "orders" model section (PHASE4 A3).

An order is a released job: "make 2 WIDGETs on routing Shaft, out of the door
by 480". It is what an MES actually hands a twin, and it is the only way a
model can answer "will order X be late". The JSON shape is the release list a
planner would export:

    {"id": "WO-1001", "release": 0, "due": 480, "quantity": 2,
     "entity_type": "WIDGET", "sequence": "Shaft", "priority": 1,
     "source": "release_orders", "attributes": {"customer": "ACME"}}

`release` and `due` are in the model's BASE time unit, like every other
duration-free number in a layout. A CREATE block in arrival mode "Orders"
(ARCHITECTURE D53) emits each order it owns at the order's `release` time,
spawning `quantity` entities that each carry `order_id`, `due_date`,
`sequence`/`step`, `priority` and the order's free `attributes`.

WHICH CREATE OWNS AN ORDER -- one rule, resolved here at build time so it is
the same for the engine and for validation: the block named by `source`, or,
with no `source`, the FIRST Orders-mode CREATE in layout order. An order
carries its own `entity_type`, so one CREATE already handles a mixed product
mix; `source` exists only for a model whose orders enter at different points.

Due dates do NOT need orders: any entity may be given one by an ASSIGN
(`entity.due_date = now + 480`), which is how a make-to-stock or service model
gets tardiness KPIs. `core/stats.py` accounts both cases at DISPOSE; this
module only owns the section, the CREATE-side release and the per-order
counters the KPI frames read.

Stdlib only, a leaf module beside `tables.py`/`sequences.py`, with its own
structured validation so `core/schema/` just calls it.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from nagasim.core import calendar as cal
from nagasim.core.problems import problem
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled
from nagasim.core.sequences import station_blocks
from nagasim.core.timeunits import units_of
from nagasim.core.utils import is_number as _is_number

__all__ = ["ORDERS_MODE", "Order", "OrderError", "build_orders", "orders_mode_creates",
           "orders_csv", "orders_from_csv", "validate_orders_structured"]

# CREATE's third arrival mode (`core/blocks/create.py`'s ARRIVAL_MODES).
ORDERS_MODE = "Orders"

# The CSV columns `orders_csv` writes and `orders_from_csv` reads; any further
# column of an imported file becomes a free attribute.
CSV_COLUMNS = ("id", "release", "due", "quantity", "entity_type", "sequence", "priority",
               "source", "time_unit")


class OrderError(ValueError):
    """A malformed CSV import. Structural faults in the section itself are
    Problems (`validate_orders_structured`), never exceptions."""


@dataclass
class Order:
    """One built order plus the counters the run fills in.

    `block` is the CREATE that owns it (see the module docstring); `created`,
    `completed` and `last_completion` are written by the run -- CREATE on
    release, `stats.account_due_date` as each of the order's entities is
    disposed -- and read once, by `outputs.summarize_orders`.
    """

    id: str
    release: float = 0.0
    due: float | None = None
    quantity: int = 1
    entity_type: str = ""
    sequence: str = ""
    priority: float | None = None
    attributes: dict = field(default_factory=dict)
    # {station: planned start} from a proposal (PHASE6 W4 P4, D122), carried
    # as `entity.plan` and read by the "Plan" queue discipline.
    plan: dict = field(default_factory=dict)
    block: str = ""
    created: int = 0
    completed: int = 0
    last_completion: float | None = None

    @property
    def complete(self) -> bool:
        """Every entity this order released has been disposed -- and at least
        one was, so an order whose release time never arrived is incomplete
        rather than vacuously complete."""
        return self.created > 0 and self.completed >= self.created


def orders_mode_creates(layout: dict) -> list[str]:
    """Every CREATE block in "Orders" arrival mode, in layout order."""
    return [b.get("name", "") for b in layout.get("blocks", [])
            if str(b.get("type", "")).upper() == "CREATE"
            and (b.get("params") or {}).get("arrival_mode") == ORDERS_MODE]


def build_orders(layout: dict, calendar=None) -> dict[str, Order]:
    """{id: Order} for the layout's "orders" section, built once per
    replication, each already assigned to the CREATE that will release it.
    Shape faults are validation's job; this only normalises.

    `release` and `due` may be ISO timestamps when the model has a calendar
    (PHASE4 T10); they are converted here, so the engine only ever sees the
    base-unit numbers it always did.
    """
    creates = orders_mode_creates(layout)
    default = creates[0] if creates else ""
    out: dict[str, Order] = {}
    units, fields = units_of(layout.get("simulation")), SECTION_FIELDS["orders"]
    for entry in layout.get("orders", []) or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get("source") or "")
        f = field_factor(param(fields, "release"), entry, units, fields)
        # A dated value is already model time; only a number is in the order's unit.
        release, due = (
            scaled(cal.to_model_time(entry.get(key), calendar, what), 1.0 if isinstance(
                entry.get(key), str) else f)
            for key, what in (("release", "order release"), ("due", "order due date")))
        order = Order(
            id=str(entry.get("id", "")),
            release=_float(release, 0.0),
            due=None if due is None else _float(due, 0.0),
            quantity=max(int(_float(entry.get("quantity"), 1.0)), 0),
            entity_type=str(entry.get("entity_type") or ""),
            sequence=str(entry.get("sequence") or ""),
            priority=None if entry.get("priority") is None else _float(entry.get("priority"), 0.0),
            attributes=dict(entry.get("attributes") or {}),
            plan=dict(entry.get("plan") or {}),
            block=source if source in creates else ("" if source else default),
        )
        out[order.id] = order
    return out


def _float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------- validation
def validate_orders_structured(layout: dict, sequences: dict | None = None,
                               calendar=None) -> list:
    """Check the layout's "orders" section against its sequences, entity types
    and Orders-mode CREATE blocks. *sequences* is the {name: step count} map
    `validate_sequences_structured` already returned, so the two agree on what
    a declared routing is."""
    problems: list = []
    entries = layout.get("orders", []) or []
    creates = orders_mode_creates(layout)
    sequences = sequences or {}
    # Entity types are matched case-insensitively: `Entity.e_type` upper-cases
    # whatever it is given (DEVLOG #87 is the same trap on a table key), so
    # "widget" and "WIDGET" are one type by the time the engine sees them.
    declared_types = {str(e.get("name", "")).casefold()
                      for e in layout.get("entities", []) or []}
    known_types = ", ".join(sorted(str(e.get("name", ""))
                                   for e in layout.get("entities", []) or [])) or "(none)"
    seen: set[str] = set()
    owned: dict[str, int] = {name: 0 for name in creates}
    stations = station_blocks(layout)

    for index, entry in enumerate(entries):
        at = f"/orders/{index}"
        if not isinstance(entry, dict):
            problems.append(problem("E_ORDER_FIELD", f"order {index} is not an object", at,
                                    hint="an order is {\"id\": \"WO-1\", \"release\": 0, "
                                         "\"due\": 480}"))
            continue
        oid = entry.get("id", "")
        if not isinstance(oid, str) or not oid.strip():
            problems.append(problem("E_ORDER_ID", "order: id must not be empty", f"{at}/id",
                                    hint="give every order a unique id, e.g. \"WO-1001\""))
            continue
        if oid in seen:
            problems.append(problem("E_ORDER_ID", f"duplicate order id: {oid}", f"{at}/id",
                                    hint="order ids identify an order in the KPI frames: "
                                         "rename one"))
        seen.add(oid)

        problems.extend(_field_problems(oid, entry, at, calendar))
        problems.extend(_plan_problems(oid, entry.get("plan"), at, stations))
        release = _dated(entry.get("release", 0), calendar)
        due = _dated(entry.get("due"), calendar)
        if _is_number(due) and _is_number(release) and due < release:
            problems.append(problem(
                "W_ORDER_DUE_BEFORE_RELEASE",
                f"order '{oid}': due {due:g} is before release {release:g}",
                f"{at}/due",
                hint="a due date is an absolute time in the model's base unit, not a lead time"))

        sequence = entry.get("sequence")
        if sequence and sequence not in sequences:
            problems.append(problem(
                "E_SEQUENCE_UNKNOWN", f"order '{oid}': unknown sequence '{sequence}'",
                f"{at}/sequence",
                hint=f"declare it under \"sequences\"; declared: "
                     f"{', '.join(sorted(sequences)) or '(none)'}"))

        e_type = entry.get("entity_type")
        if e_type and declared_types and str(e_type).casefold() not in declared_types:
            problems.append(problem(
                "E_ORDER_ENTITY_TYPE", f"order '{oid}': unknown entity type '{e_type}'",
                f"{at}/entity_type",
                hint=f"declare it under \"entities\"; declared: {known_types}"))

        source = entry.get("source")
        if source:
            if source in owned:
                owned[source] += 1
            else:
                problems.append(problem(
                    "E_ORDER_SOURCE",
                    f"order '{oid}': '{source}' is not a CREATE block in \"Orders\" mode",
                    f"{at}/source",
                    hint=f"name one of: {', '.join(creates) or '(no CREATE is in Orders mode)'}, "
                         f"or drop \"source\" to use the first one"))
        elif creates:
            owned[creates[0]] += 1

    if entries and not creates:
        problems.append(problem(
            "W_ORDERS_NOT_CONSUMED",
            f"the model declares {len(entries)} order(s) but no CREATE block releases "
            f"them", "/orders",
            hint="set a CREATE block's arrival_mode to \"Orders\""))
    for name in creates:
        if not owned.get(name):
            index = next((i for i, b in enumerate(layout.get("blocks", []))
                          if b.get("name") == name), 0)
            problems.append(problem(
                "W_CREATE_NO_ORDERS",
                f"{name}: arrival mode \"Orders\" but no order names it",
                f"/blocks/{index}/params/arrival_mode", block=name,
                hint="add orders under \"orders\", or switch this CREATE back to Interarrival"))
    return problems


def _plan_problems(oid: str, plan, at: str, stations: dict) -> list:
    """`plan` (PHASE6 W4 P4, D122): {station: planned start}. A key naming no
    station only warns -- the discipline simply never meets it."""
    if plan is None:
        return []
    if not isinstance(plan, dict):
        return [problem("E_ORDER_FIELD", f"order '{oid}': \"plan\" must be an object",
                        f"{at}/plan", hint="write {\"Cutting\": 0, \"Drilling\": 4.5}")]
    problems = []
    for station, start in plan.items():
        if not _is_number(start):
            problems.append(problem("E_ORDER_FIELD",
                                    f"order '{oid}': planned start at '{station}' must be a number",
                                    f"{at}/plan/{station}", hint="a time in the model's base unit"))
        elif station not in stations:
            problems.append(problem(
                "W_PLAN_STATION_UNKNOWN",
                f"order '{oid}': plan names unknown station '{station}'",
                f"{at}/plan/{station}",
                hint=f"stations: {', '.join(sorted(stations)) or '(none)'}"))
    return problems


def _dated(value, calendar):
    """An order time as a number: an ISO stamp converted through the calendar,
    anything else unchanged (so the checks below still see what was written)."""
    if not isinstance(value, str):
        return value
    try:
        return cal.to_model_time(value, calendar, "order time")
    except cal.CalendarError:
        return value


def _field_problems(oid: str, entry: dict, at: str, calendar=None) -> list:
    """The plain shape checks: the numbers are numbers and the free attributes
    are a flat mapping of scalars, exactly as a sequence step's `values` is."""
    problems = []
    for key, minimum in (("release", 0.0), ("due", None), ("quantity", 1.0), ("priority", None)):
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, str) and key in ("release", "due"):
            # PHASE4 T10: an absolute time may be written as a real date.
            try:
                cal.to_model_time(value, calendar, f"order '{oid}' {key}")
            except cal.CalendarError as exc:
                problems.append(problem("E_CALENDAR_DATE", f"order '{oid}': {exc}", f"{at}/{key}",
                                        hint="write it as 2026-01-05T06:00:00, or use a number "
                                             "in the model's base time unit"))
            continue
        if not _is_number(value):
            problems.append(problem(
                "E_ORDER_FIELD", f"order '{oid}': {key} must be a number", f"{at}/{key}",
                hint=f"\"{key}\" is a number in the model's base time unit"
                     if key in ("release", "due") else f"\"{key}\" is a number"))
        elif minimum is not None and value < minimum:
            problems.append(problem(
                "E_ORDER_FIELD", f"order '{oid}': {key} must be at least {minimum:g}",
                f"{at}/{key}", hint=f"use {minimum:g} or more"))
    for key in ("entity_type", "sequence", "source"):
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(problem("E_ORDER_FIELD", f"order '{oid}': {key} must be a string",
                                    f"{at}/{key}", hint=f"\"{key}\" names a declared {key}"))
    attributes = entry.get("attributes")
    if attributes is None:
        return problems
    if not isinstance(attributes, dict):
        return problems + [problem(
            "E_ORDER_FIELD", f"order '{oid}': \"attributes\" must be an object",
            f"{at}/attributes", hint="write {\"customer\": \"ACME\", \"lot\": 3}")]
    for key, value in attributes.items():
        if not str(key).isidentifier() or str(key).startswith("_"):
            problems.append(problem(
                "E_ORDER_FIELD", f"order '{oid}': '{key}' is not a valid attribute name",
                f"{at}/attributes/{key}",
                hint="an entity attribute is a Python identifier not starting with _"))
        elif not isinstance(value, (int, float, str, bool)):
            problems.append(problem(
                "E_ORDER_FIELD",
                f"order '{oid}': value of '{key}' must be a number, string or boolean",
                f"{at}/attributes/{key}",
                hint="an attribute is assigned straight onto the entity: keep it scalar"))
    return problems


# ---------------------------------------------------------------- CSV
def orders_csv(section) -> str:
    """The whole section as CSV text: one header row of `CSV_COLUMNS` plus
    every free attribute name any order uses, then one row per order.
    `orders_from_csv` reads back exactly this."""
    entries = [e for e in (section or []) if isinstance(e, dict)]
    extras: list[str] = []
    for entry in entries:
        for key in (entry.get("attributes") or {}):
            if key not in extras:
                extras.append(str(key))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([*CSV_COLUMNS, *extras])
    for entry in entries:
        attributes = entry.get("attributes") or {}
        writer.writerow([_cell(entry.get(c)) for c in CSV_COLUMNS]
                        + [_cell(attributes.get(k)) for k in extras])
    return out.getvalue()


def _cell(value):
    return "" if value is None else value


def orders_from_csv(text: str) -> list[dict]:
    """A whole "orders" section from CSV text (see `orders_csv`). `id` is the
    one required column; every column outside `CSV_COLUMNS` becomes a free
    attribute. Raises OrderError on a file this cannot read."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise OrderError("the CSV has no data rows")
    if not any((r or {}).get("id") for r in rows):
        raise OrderError("the CSV has no \"id\" column")
    entries = []
    for row in rows:
        oid = str(row.get("id") or "").strip()
        if not oid:
            continue
        entry: dict = {"id": oid}
        for column in ("release", "due", "quantity", "priority"):
            number = _number_cell(row.get(column))
            if number is not None:
                entry[column] = int(number) if column == "quantity" else number
        for column in ("entity_type", "sequence", "source", "time_unit"):
            text_value = str(row.get(column) or "").strip()
            if text_value:
                entry[column] = text_value
        attributes = {k: _scalar_cell(v) for k, v in row.items()
                      if k and k not in CSV_COLUMNS and str(v or "").strip()}
        if attributes:
            entry["attributes"] = attributes
        entries.append(entry)
    if not entries:
        raise OrderError("the CSV has no row with an id")
    return entries


def _number_cell(cell):
    try:
        return float(str(cell).strip())
    except (TypeError, ValueError):
        return None


def _scalar_cell(cell):
    """A free-attribute cell: a number when it reads as one, else the text."""
    number = _number_cell(cell)
    if number is None:
        return str(cell).strip()
    return int(number) if number == int(number) and "." not in str(cell) else number
