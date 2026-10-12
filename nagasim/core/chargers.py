"""Chargers and battery-swap stations for electric fleets (automation
sub-project 2, D164-D166). The model section is `chargers`:

    {"name": "dock_A", "node": "N12", "kind": "charge", "points": 2, "power_kw": 3.0,
     "knee": 0.8, "taper_floor": 0.1, "efficiency": 1.0}
    {"name": "swap_1", "node": "N30", "kind": "swap", "points": 1, "power_kw": 10.0,
     "swap_time": "TRIA(2,3,5)", "spare_packs": 4}

A charger sits at a network node and has `points` places. The fleet
(`core/movers.py`) decides who charges where and when; this module owns the
section's validation -- a fleet's `battery` block included, because that needs
the charger names -- and the charger's own state and statistics.
"""
from __future__ import annotations

import simpy

from nagasim.core.battery import charge_hours, power_at, soc_after
from nagasim.core.expressions import ExpressionError, compile_expression
from nagasim.core.network import build_network
from nagasim.core.problems import problem
from nagasim.core.quantities import SECTION_FIELDS, field_factor, param, scaled
from nagasim.core.stats import Tally, TimeWeighted
from nagasim.core.timeunits import conversion_factor, units_of

__all__ = ["CHARGER_KINDS", "BATTERY_STRATEGIES", "Charger", "build_chargers",
           "validate_chargers"]

CHARGER_KINDS = ("charge", "swap")
BATTERY_STRATEGIES = ("threshold", "opportunity", "swap", "scheduled")


class Charger:
    """One `chargers` record at run time: its points, the sessions drawing
    power from it right now, and its statistics.

    A session is the list `[capacity kWh, SoC at the start, start time, SoC
    cap]`: a vehicle on a point, or a removed pack recharging at a swap
    station (which takes no point). SoC at any instant is closed form
    (`core/battery.py`), so nothing is stored while one runs; the warm-up
    reset restarts a running session at the reset instant.
    """

    def __init__(self, env, spec: dict):
        units = env.ctx.units
        fields = SECTION_FIELDS["chargers"]
        self.env = env
        self.name = str(spec["name"])
        self.node = str(spec.get("node", ""))
        self.kind = spec.get("kind", "charge")
        self.capacity = max(int(spec.get("points", 1)), 1)
        self.points = simpy.Resource(env, capacity=self.capacity)
        self.power_kw = float(spec["power_kw"])
        self.knee = float(spec.get("knee", 0.8))
        self.floor = float(spec.get("taper_floor", 0.1))
        self.efficiency = float(spec.get("efficiency", 1.0))
        self._hours = conversion_factor("time", units.time, "hours")   # per model time unit
        self._swap_time = compile_expression(str(spec.get("swap_time") or "0"))
        self._swap_factor = field_factor(param(fields, "swap_time"), spec, units, fields)
        packs = spec.get("spare_packs")
        # Charged packs in stock; None is an unlimited stock.
        self.packs = None if packs is None else simpy.Container(env, init=int(packs))
        self.running: list[tuple] = []
        self.queue_len = TimeWeighted(env.now)     # vehicles waiting for a point or a pack
        self.busy = TimeWeighted(env.now)          # points in use
        self.wait = Tally()
        self.stay = Tally()                        # time on a point, one value per session
        self.grid_kwh = 0.0
        self.peak_kw = 0.0

    @property
    def sessions(self) -> int:
        return self.stay.count

    def duration(self, capacity: float, s0: float, s1: float) -> float:
        """Model time to charge a *capacity* kWh battery from SoC *s0* to *s1*."""
        return charge_hours(capacity, s0, s1, self.power_kw, self.knee, self.floor) / self._hours

    def swap_time(self, scope) -> float:
        value = float(self._swap_time.evaluate(scope))
        return max(scaled(value, self._swap_factor), 0.0)

    def soc(self, session: tuple, t: float) -> float:
        capacity, s0, started, cap = session
        return soc_after(capacity, s0, (t - started) * self._hours, self.power_kw, self.knee,
                         self.floor, cap)

    def _power(self, t: float) -> float:
        return sum(power_at(self.soc(s, t), self.power_kw, self.knee, self.floor)
                   for s in self.running)

    def plug(self, capacity: float, s0: float, cap: float = 1.0) -> list:
        """Power starts flowing into a battery. The total can only peak at a
        start, since each session's power never rises."""
        session = [capacity, s0, self.env.now, cap]
        self.running.append(session)
        self.peak_kw = max(self.peak_kw, self._power(self.env.now))
        return session

    def unplug(self, session: list) -> float:
        """The session ends now, complete or cut short; returns its SoC and
        books its grid energy."""
        s1 = self.soc(session, self.env.now)
        self.running.remove(session)
        self.grid_kwh += session[0] * (s1 - session[1]) / self.efficiency
        return s1

    def energy(self, t: float) -> float:
        """Grid kWh up to *t*: the sessions ended plus what the running ones
        have drawn so far."""
        return self.grid_kwh + sum(s[0] * (self.soc(s, t) - s[1]) / self.efficiency
                                   for s in self.running)

    def occupy(self, arrived: float) -> None:
        """A vehicle that arrived at *arrived* is now served on its point."""
        self.wait.add(self.env.now - arrived)
        self.busy.add(1, self.env.now)

    def leave(self, point, since: float) -> None:
        self.busy.add(-1, self.env.now)
        self.stay.add(self.env.now - since)
        self.points.release(point)

    def recharge(self, capacity: float, s0: float):
        """Generator: a removed pack recharges to full, then is back in stock."""
        session = self.plug(capacity, s0)
        yield self.env.timeout(self.duration(capacity, s0, 1.0))
        self.unplug(session)
        if self.packs is not None:
            yield self.packs.put(1)

    def reset_stats(self, t: float) -> None:
        """Warm-up: zero the totals; what is plugged in keeps charging, as a
        session that starts now (what it drew before was warm-up)."""
        self.queue_len.reset(t)
        self.busy.reset(t)
        self.wait.reset()
        self.stay.reset()
        for session in self.running:
            session[1], session[2] = self.soc(session, t), t
        self.grid_kwh = 0.0
        self.peak_kw = self._power(t)


def build_chargers(layout: dict, env) -> dict[str, Charger]:
    """{name: Charger} for the layout's "chargers" section, once per run."""
    return {str(spec["name"]): Charger(env, spec) for spec in layout.get("chargers") or []
            if isinstance(spec, dict) and spec.get("name")}


# -- validation --------------------------------------------------------------

def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _fraction(value) -> bool:
    return _number(value) is not None and 0 <= value <= 1


def validate_chargers(layout: dict, problems: list) -> None:
    """The `chargers` section's hook (core/sections.py); it also checks every
    fleet's `battery`, which names chargers and a schedule."""
    network = layout.get("network") if isinstance(layout.get("network"), dict) else {}
    nodes = {str(n.get("name", "")).strip() for n in network.get("nodes") or []
             if isinstance(n, dict)}
    # A charger is a row of energy_summary next to the resources and fleets,
    # so it may not share a name with one.
    taken = {str(r.get("name", "")) for key in ("resources", "resource_sets", "fleets")
             for r in layout.get(key) or [] if isinstance(r, dict)}
    chargers: dict[str, dict] = {}
    for index, record in enumerate(layout.get("chargers") or []):
        at = f"/chargers/{index}"
        name = str(record.get("name", "")).strip() if isinstance(record, dict) else ""
        if not name or name in chargers or name in taken:
            problems.append(problem("E_CHARGER", f"charger {index}: empty or duplicate name "
                                    f"'{name}'", f"{at}/name",
                                    hint="give every charger a name no other charger, resource "
                                         "or fleet has"))
            continue
        chargers[name] = record

        def bad(field, text, hint, name=name, at=at):
            problems.append(problem("E_CHARGER", f"charger '{name}': {text}", f"{at}/{field}",
                                    hint=hint))

        if str(record.get("node", "")).strip() not in nodes:
            bad("node", f"unknown node '{record.get('node', '')}'",
                f"name a declared network node: {', '.join(sorted(nodes)) or '(none)'}")
        kind = record.get("kind", "charge")
        if kind not in CHARGER_KINDS:
            bad("kind", f"unknown kind '{kind}'", f"one of: {', '.join(CHARGER_KINDS)}")
        points = record.get("points", 1)
        if not isinstance(points, int) or isinstance(points, bool) or points < 1:
            bad("points", "points must be an integer >= 1", "how many vehicles it serves at once")
        if not (_number(record.get("power_kw")) or 0) > 0:
            bad("power_kw", "power_kw must be a number > 0", "the charging power in kW")
        if not _fraction(record.get("knee", 0.8)):
            bad("knee", "knee must be between 0 and 1", "the SoC above which power tapers")
        # A floor of 0 would never reach 100 %: the charge time is infinite.
        for field in ("taper_floor", "efficiency"):
            value = record.get(field, 1.0)
            if not (_fraction(value) and value > 0):
                bad(field, f"{field} must be above 0 and at most 1",
                    "the share of power_kw left at 100 %" if field == "taper_floor"
                    else "battery kWh per grid kWh")
        packs = record.get("spare_packs")
        # 0 packs would hold the first vehicle for ever: none is ever put back.
        if packs is not None and (not isinstance(packs, int) or isinstance(packs, bool)
                                  or packs < 1):
            bad("spare_packs", "spare_packs must be an integer >= 1",
                "clear it for unlimited packs")
        if kind == "swap":
            swap_time = record.get("swap_time")
            if swap_time in (None, ""):
                bad("swap_time", "a swap station needs swap_time", "e.g. \"TRIA(2,3,5)\"")
            else:
                try:
                    compile_expression(str(swap_time))
                except ExpressionError as exc:
                    bad("swap_time", f"swap_time: {exc}", "a number or an expression")
    _validate_batteries(layout, chargers, nodes, problems)


def _validate_batteries(layout: dict, chargers: dict, nodes: set, problems: list) -> None:
    schedules = {s.get("name") for s in layout.get("schedules") or [] if isinstance(s, dict)}
    network = None
    for index, fleet in enumerate(layout.get("fleets") or []):
        battery = fleet.get("battery") if isinstance(fleet, dict) else None
        if battery is None:
            continue
        at = f"/fleets/{index}/battery"
        name = fleet.get("name", index)

        def bad(code, field, text, hint, name=name, at=at):
            problems.append(problem(code, f"fleet '{name}': battery: {text}",
                                    f"{at}/{field}".rstrip("/"), hint=hint))

        if not isinstance(battery, dict):
            bad("E_BATTERY", "", "must be an object", "{\"capacity_kwh\": 2.0, \"low\": 0.25}")
            continue
        if fleet.get("kind") == "worker":
            bad("E_BATTERY", "", "a worker fleet has no battery",
                "drop battery, or use kind \"vehicle\"")
        if not (_number(battery.get("capacity_kwh")) or 0) > 0:
            bad("E_BATTERY", "capacity_kwh", "capacity_kwh must be a number > 0",
                "the usable energy of one vehicle's battery")
        low, target = battery.get("low"), battery.get("target", 1.0)
        fractions = {"initial": battery.get("initial", 1.0), "low": low, "target": target}
        if battery.get("opportunity_below") is not None:
            fractions["opportunity_below"] = battery["opportunity_below"]
        for field, value in fractions.items():
            if not _fraction(value):
                bad("E_BATTERY", field, f"{field} must be a number between 0 and 1",
                    "a state of charge, 0 to 1")
        if _fraction(low) and _fraction(target) and low >= target:
            bad("E_BATTERY", "low", f"low {low:g} must be below target {target:g}",
                "the vehicle charges from low up to target")
        strategy = battery.get("strategy", "threshold")
        if strategy not in BATTERY_STRATEGIES:
            bad("E_BATTERY", "strategy", f"unknown strategy '{strategy}'",
                f"one of: {', '.join(BATTERY_STRATEGIES)}")
            continue
        if strategy == "opportunity" and battery.get("opportunity_below") is None:
            bad("E_BATTERY", "opportunity_below",
                "strategy \"opportunity\" needs opportunity_below",
                "idle vehicles below it charge when a point is free")
        if strategy == "scheduled":
            schedule = battery.get("schedule")
            if not schedule:
                bad("E_BATTERY", "schedule", "strategy \"scheduled\" needs a schedule",
                    "name a record of \"schedules\"; charging happens while its value is > 0")
            elif schedule not in schedules:
                bad("E_BATTERY_SCHEDULE", "schedule", f"unknown schedule '{schedule}'",
                    f"declared: {', '.join(sorted(map(str, schedules))) or '(none)'}")
        allowed = battery.get("chargers")
        if allowed is not None and not (isinstance(allowed, list)
                                        and all(isinstance(c, str) for c in allowed)):
            bad("E_BATTERY", "chargers", "chargers must be a list of charger names",
                "drop it to allow every charger of the matching kind")
            continue
        for charger in allowed or ():
            if charger not in chargers:
                bad("E_CHARGER_UNKNOWN", "chargers", f"unknown charger '{charger}'",
                    f"declared: {', '.join(sorted(chargers)) or '(none)'}")
        # W_NO_CHARGER: no charger of the kind the strategy needs can be driven
        # to from the fleet's home (a gantry flies there: any known node counts).
        if network is None:
            network = build_network(layout.get("network") if nodes else None,
                                    units_of(layout.get("simulation")))
        kind = "swap" if strategy == "swap" else "charge"
        home = str(fleet.get("home") or "").strip() or next(iter(network.nodes), "")
        usable = {c: record for c, record in chargers.items()
                  if record.get("kind", "charge") == kind and (not allowed or c in allowed)}
        reachable = [c for c, record in usable.items()
                     if home in network.nodes and record.get("node") in network.nodes
                     and (fleet.get("motion") == "gantry"
                          or network.distance(home, record["node"]) < float("inf"))]
        if not reachable:
            # A library asset's own charger sits at a node the user still has
            # to join to the aisles: say which.
            hint = ("join " + ", ".join(f"node '{r.get('node')}' ({c})"
                                        for c, r in sorted(usable.items()))
                    + " to the network the fleet drives on" if usable
                    else "add one under \"chargers\" on a node the fleet can drive to")
            bad("W_NO_CHARGER", "chargers", f"no reachable charger of kind \"{kind}\"",
                hint + "; until then the vehicles never charge")
