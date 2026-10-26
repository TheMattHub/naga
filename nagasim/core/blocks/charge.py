"""CHARGE: entities take energy at up to their own power, sharing one site cap
(Phase 15 M4, D825-D829).

Every entity at the block is a session: the energy it still needs, its own kW
limit, a deadline, and the rate it gets now. A shared cap moves every
session's finish time whenever one arrives, fills up or reaches its deadline,
which no DELAY can express, so the block owns the schedule: at each of those
instants it books `rate x dt` on every session, ends the full and the due ones,
re-allocates the cap and sets ONE timer to the next such instant. Rates are
constant in between, so the block's total power is piecewise constant and a
flow reading it through `power_variable` (the level engine re-evaluates on a
variable write) integrates exactly -- the D60 contract.

Limits, deliberately: no taper (a tapering car under a shared cap frees power
continuously, which has no piecewise-constant schedule, D828) and a constant
cap (a cap following the bus would need the level engine to wake the block).
"""
import logging
import math

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.quantities import field_factor, param
from nagasim.core.timeunits import conversion_factor
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Charge", "SPEC", "SHARING_RULES"]

SHARING_RULES = ("equal", "first_come")
# kWh left below which a session counts as full: absorbs the rounding of
# `rate x dt` at a finish instant computed as `left / rate`.
_EPS = 1e-9


class _Session:
    __slots__ = ("entity", "left", "max_kw", "deadline", "rate", "done", "full_at")

    def __init__(self, entity, need, max_kw, deadline, done):
        self.entity, self.left, self.max_kw, self.deadline = entity, need, max_kw, deadline
        self.rate, self.done, self.full_at = 0.0, done, None


class Charge(BaseProcess):
    """Charges an entity (an electric vehicle, a battery pack) with the energy it needs at up to its own power, sharing a site power cap with every other entity at this block; it leaves when full or when its time is up.

    The block's total grid kW (what the entities take over `efficiency`) is
    kept in `power_variable`, so a flow with `rate: VAR("name")` puts it on the
    electricity bus; `site_limit` caps that grid kW. On leaving the entity
    carries `charged_kwh`, `charge_full` (1 or 0) and `charge_time` (entry to
    full, or to leaving when not full, in `time_limit_unit`)."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.energy = "0"
        self.max_power = "0"
        self.time_limit = ""
        self.time_limit_from = ""
        self.leave_when_full = True
        self.site_limit = 0.0
        self.efficiency = 1.0
        self.sharing = "equal"
        self.power_variable = ""
        self._limit_unit = "base"
        self._limit_factor = 1.0                     # model time units per time_limit_unit
        self._hours = conversion_factor("time", env.ctx.units.time, "hours")   # hours per model unit
        self.sessions = []                           # charging now, in plug order
        self.total_kw = 0.0                          # grid side
        self._last = env.now
        self._timer = None

    # -- one entity -----------------------------------------------------------

    def self_process(self, entity):
        now = self.env.now
        self.active_entity = entity
        need = max(float(evaluate_expression(self, self.energy, entity)), 0.0)
        max_kw = max(float(evaluate_expression(self, self.max_power, entity)), 0.0)
        deadline = math.inf
        if str(self.time_limit).strip():
            limit = float(evaluate_expression(self, self.time_limit, entity)) * self._limit_factor
            start = (float(evaluate_expression(self, self.time_limit_from, entity))
                     if str(self.time_limit_from).strip() else now)
            deadline = start + limit
        charged, full_at = 0.0, None
        if need <= _EPS:
            full_at = now
        elif max_kw <= 0 and deadline == math.inf:
            # #1131: no power and no limit is a session that can never end; leave uncharged.
            log.warning("Charge '%s': entity %s has no charging power and no time limit; "
                        "it leaves at once with nothing charged.", self.name, entity.id)
        elif deadline > now:
            session = _Session(entity, need, max_kw, deadline, self.env.event())
            self._advance()
            self.sessions.append(session)
            self._replan()
            yield session.done
            full_at = session.full_at
            charged = need if full_at is not None else need - session.left
            if full_at is not None and not self.leave_when_full and deadline > self.env.now:
                yield timeout_at(self.env, deadline)
        entity.charged_kwh = charged
        entity.charge_full = 1 if full_at is not None else 0
        end = full_at if full_at is not None else self.env.now
        entity.charge_time = (end - now) / self._limit_factor
        if self.ctx.debug:
            log.debug("Entity %s leaves Charge '%s' at %s with %s kWh.", entity.id, self.name,
                      self.env.now, charged)
        self.remove_active_entity(entity)
        self.forward(entity, 0)

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    # -- the site -------------------------------------------------------------

    def _advance(self) -> None:
        """Book what every session drew since the last change."""
        now = self.env.now
        hours = (now - self._last) * self._hours
        if hours > 0:
            for s in self.sessions:
                s.left -= s.rate * hours
        self._last = now

    def _replan(self) -> None:
        """End the full and the due sessions, share the cap, set the next timer."""
        now = self.env.now
        keep = []
        for s in self.sessions:
            if s.left <= _EPS:                 # full first: full at its deadline is full
                s.full_at = now
                s.done.succeed()
            elif now >= s.deadline:
                s.done.succeed()
            else:
                keep.append(s)
        self.sessions = keep
        self._allocate()
        nxt = math.inf
        for s in keep:
            if s.rate > 0:
                nxt = min(nxt, now + s.left / (s.rate * self._hours))
            nxt = min(nxt, s.deadline)
        if nxt < math.inf:
            timer = self._timer = timeout_at(self.env, nxt)
            timer.callbacks.append(lambda _event, t=timer: self._tick(t))
        else:
            self._timer = None

    def _tick(self, timer) -> None:
        if timer is self._timer:               # a later change replaced this timer
            self._advance()
            self._replan()

    def _allocate(self) -> None:
        """Rates per D826. No cap: each its own max. `equal` is max-min fair
        (water-filling): smallest max first, each takes min(max, cap left /
        sessions left), so a slow car's unused share goes to the others.
        `first_come`: plug order, each takes min(max, cap left)."""
        sessions = self.sessions
        if self.site_limit <= 0:
            for s in sessions:
                s.rate = s.max_kw
        else:
            left = self.site_limit * self.efficiency     # the cap is on the grid side
            if self.sharing == "first_come":
                for s in sessions:
                    s.rate = min(s.max_kw, left)
                    left -= s.rate
            else:
                count = len(sessions)
                for s in sorted(sessions, key=lambda s: s.max_kw):   # stable: plug order on ties
                    s.rate = min(s.max_kw, left / count)
                    left -= s.rate
                    count -= 1
        total = math.fsum(s.rate for s in sessions) / self.efficiency
        if total != self.total_kw:
            self.total_kw = total
            self._publish(total)

    def _publish(self, total: float) -> None:
        """Write the total kW, waking what reads it the way an ASSIGN does."""
        name = self.power_variable
        if not name:
            return
        ctx = self.ctx
        ctx.variables[name] = total
        if ctx.watched or ctx.traced_variables:
            ctx.wrote_variable(name)
        if ctx.level_system is not None:
            ctx.level_system.trigger()
        if ctx.agent_system is not None:
            ctx.agent_system.trigger()

    # -- params ---------------------------------------------------------------

    def get_params_dict(self):
        return {"energy": self.energy, "max_power": self.max_power,
                "time_limit": self.time_limit, "time_limit_unit": self._limit_unit,
                "time_limit_from": self.time_limit_from,
                "leave_when_full": self.leave_when_full, "site_limit": self.site_limit,
                "efficiency": self.efficiency, "sharing": self.sharing,
                "power_variable": self.power_variable}

    def set_params_from_dict(self, param_dict):
        self.energy = str(param_dict.get("energy", "0"))
        self.max_power = str(param_dict.get("max_power", "0"))
        self.time_limit = str(param_dict.get("time_limit", "") or "")
        self._limit_unit = param_dict.get("time_limit_unit", "base")
        self._limit_factor = field_factor(param(SPEC, "time_limit"), param_dict, self.ctx.units,
                                          SPEC.params)
        self.time_limit_from = str(param_dict.get("time_limit_from", "") or "")
        self.leave_when_full = bool(param_dict.get("leave_when_full", True))
        self.site_limit = float(param_dict.get("site_limit", 0.0) or 0.0)
        efficiency = float(param_dict.get("efficiency", 1.0))
        self.efficiency = efficiency if 0 < efficiency <= 1 else 1.0   # validation reports it
        self.sharing = param_dict.get("sharing", "equal")
        self.power_variable = str(param_dict.get("power_variable", "") or "")


SPEC = BlockSpec(
    type_name="CHARGE", cls=Charge,
    params=(
        ParamSpec("energy", str, "0", required=True,
                  description="Energy the entity needs, kWh: an expression, e.g. entity.need_kwh.",
                  quantity="energy", kinds=("number", "expression"), unit_key=""),
        ParamSpec("max_power", str, "0", required=True,
                  description="Most power the entity takes, kW: an expression, e.g. "
                              "min(11, entity.max_kw).",
                  quantity="power", kinds=("number", "expression"), unit_key=""),
        ParamSpec("time_limit", str, "",
                  description="Longest time the entity stays (an expression); it leaves then, "
                              "full or not. Empty = until full.",
                  quantity="time", kinds=("number", "expression")),
        ParamSpec("time_limit_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `time_limit` and of the `charge_time` the entity "
                              "carries: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("time_limit_from", str, "",
                  description="Instant, in model time, the time limit counts from: an "
                              "expression such as entity.arrived_at. Empty = when the entity "
                              "enters. A deadline already past sends it on at once, charged 0.",
                  quantity="none", kinds=("expression",)),
        ParamSpec("leave_when_full", bool, True,
                  description="Leave as soon as the need is met. Off: stay (drawing nothing) "
                              "until the time limit, as a car stays plugged in.",
                  quantity="none"),
        ParamSpec("site_limit", float, 0.0, min=0,
                  description="Most power all entities at this block draw together from the "
                              "grid, kW; 0 = no cap.",
                  quantity="power", unit_key=""),
        ParamSpec("efficiency", float, 1.0, min=0, max=1, min_exclusive=True,
                  description="Energy the entity stores per unit drawn from the grid (charger "
                              "losses); 1 = none. The site limit and power_variable are grid side.",
                  quantity="ratio"),
        ParamSpec("sharing", str, "equal", quantity="none",
                  description="How the cap is shared: \"equal\" (an equal share each; what a "
                              "slow entity cannot use goes to the others) or \"first_come\" "
                              "(in order of arrival, each as much as it can take)."),
        ParamSpec("power_variable", str, "",
                  description="Model variable kept equal to the block's total grid kW, for a flow "
                              "rate VAR(\"name\") on the electricity bus. Empty = none.",
                  quantity="none"),
    ),
    factory=lambda env, block: Charge(env, block),
    family="work",
)
