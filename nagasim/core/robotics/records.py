"""The `robots` model section: one record per robot type, its checks, and the
run's robots.

A record is reusable hardware: a structure with its dimensions (or a joint
chain), each actuated axis's range, speed and acceleration, and the tool frame.
A MOTION block names it by `robot`. Units are fixed (degrees, metres, seconds)
because datasheets give them so, and a per-field unit on a nested axis table
would add nothing.
"""
from __future__ import annotations

import dataclasses

from nagasim.core.problems import problem
from nagasim.core.robotics.chain import chain_robot
from nagasim.core.robotics.templates import STRUCTURES, template_robot
from nagasim.core.timeunits import conversion_factor
from nagasim.core.utils import is_finite_number

__all__ = ["FIELDS", "robot_from_record", "validate_robots"]

FIELDS = frozenset({"name", "description", "structure", "params", "joints", "axes", "tool", "home",
                    "reach_m", "payload_max", "payload_unit", "payload_derate", "tcp_speed",
                    "speed_unit", "tcp_accel", "approximate", "source"})


def _positive(record: dict, key: str, quantity: str = "", unit_key: str = "",
              si: str = "") -> float | None:
    """The field as a positive float, in *si* when it has a unit key."""
    value = record.get(key)
    if value is None:
        return None
    if not is_finite_number(value) or value <= 0:
        raise ValueError(f"{key} must be a number above 0")
    unit = record.get(unit_key) or si if unit_key else ""
    if not unit or unit in ("base", si):
        return float(value)
    try:
        return float(value) * conversion_factor(quantity, unit, si)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{unit_key}: unknown unit '{unit}'") from exc


def robot_from_record(record: dict):
    """The Robot of a `robots` record. Raises ValueError with the sentence the
    E_ROBOT problem shows."""
    if not isinstance(record, dict):
        raise ValueError("a robot must be an object")
    unknown = sorted(set(record) - FIELDS)
    if unknown:
        raise ValueError(f"unknown field '{unknown[0]}'")
    structure = record.get("structure")
    if structure != "chain" and structure not in STRUCTURES:
        raise ValueError(f"unknown structure '{structure}'; known: {', '.join((*STRUCTURES, 'chain'))}")
    axes = record.get("axes")
    if not isinstance(axes, list) or not all(isinstance(a, dict) for a in axes):
        raise ValueError("axes must be a list of {name, range, speed, accel}")
    tool = record.get("tool")
    if tool is not None:
        if not isinstance(tool, dict) or set(tool) - {"xyz", "rpy_deg"} or not all(
                isinstance(tool.get(k, [0, 0, 0]), list) and len(tool.get(k, [0, 0, 0])) == 3
                and all(is_finite_number(v) for v in tool.get(k, [0, 0, 0])) for k in ("xyz", "rpy_deg")):
            raise ValueError("tool must be {xyz: [x, y, z] m, rpy_deg: [roll, pitch, yaw] deg}")
    home = record.get("home")
    if home is not None and not (isinstance(home, list) and all(is_finite_number(v) for v in home)):
        raise ValueError("home must be a list of numbers, one per axis")
    payload_max = _positive(record, "payload_max", "mass", "payload_unit", "kg")
    derate = _positive(record, "payload_derate")
    if derate is not None and derate > 1:
        raise ValueError("payload_derate must be in (0, 1]")
    tcp_speed = _positive(record, "tcp_speed", "speed", "speed_unit", "m/s")
    tcp_accel = _positive(record, "tcp_accel")
    reach_m = _positive(record, "reach_m")
    name = str(record.get("name", ""))
    if structure == "chain":
        joints = record.get("joints")
        if not isinstance(joints, list) or not joints or not all(isinstance(j, dict) for j in joints):
            raise ValueError("a chain robot needs its joints: a list of {name, type, origin, axis}")
        robot = chain_robot(joints, axes, name=name, tool=tool, home=home)
    else:
        if "joints" in record:
            raise ValueError(f"joints are for structure 'chain' only; {structure} builds its own "
                             "from params")
        params = record.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object of dimensions in metres")
        robot = template_robot(structure, params, axes, name=name, tool=tool, home=home)
    return dataclasses.replace(robot, reach_m=reach_m, payload_max=payload_max,
                               payload_derate=1.0 if derate is None else derate,
                               tcp_speed=tcp_speed, tcp_accel=tcp_accel)


def validate_robots(layout: dict, problems: list) -> None:
    """The `robots` section's hook (core/sections.py): E_ROBOT per bad record."""
    seen: set[str] = set()
    for index, record in enumerate(layout.get("robots") or []):
        at = f"/robots/{index}"
        name = str(record.get("name", "")).strip() if isinstance(record, dict) else ""
        if not name or name in seen:
            problems.append(problem("E_ROBOT", f"robot {index}: empty or duplicate name '{name}'",
                                    f"{at}/name", hint="give every robot its own name",
                                    fields={"case": "name", "index": index, "name": name}))
            continue
        seen.add(name)
        try:
            robot_from_record(record)
        except (ValueError, TypeError, KeyError) as exc:
            problems.append(problem("E_ROBOT", f"robot '{name}': {exc}", at,
                                    hint="see the robot record's schema: structure, params, axes, tool",
                                    fields={"case": "said"}))
