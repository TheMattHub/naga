"""ROUTE: send an entity to the next station of its sequence (PHASE4 A2).

The entity carries `sequence` (a name in the model's "sequences" section) and
`step` (the index of the next operation, 0 on the first visit). ROUTE reads the
step, assigns the step's `values` onto the entity, advances `step`, and sends
the entity to the STATION block that step names -- a jump with no drawn link,
emitted as a normal `entity_moved` so the trace and the animation show the
travel.

Two edge cases, both deliberate:

* the sequence is FINISHED (`step` past the last one): the entity continues
  along ROUTE's own outgoing link, so a DISPOSE or any further flow follows.
  That is why a ROUTE block needs one link like any other block.
* the entity has NO sequence: a run-time model error naming block and entity.
  Falling through to the outgoing link would be indistinguishable from "the
  routing finished", which is the one thing a routed model must not confuse --
  an entity that never got its `sequence` assigned is a modelling mistake, not
  a flow.

The optional transfer time is an ordinary duration spec, so it can be a
distribution or an expression; `from_station` and `to_station` are in scope for
it and nothing else, which is what a from-to distance table needs:
`TABLE("Distance", from_station, to_station) / 50`.
"""
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, DIST_PARAMS, ParamSpec
from nagasim.core.quantities import field_factor, param, scaled
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.expressions import ExpressionError, compile_expression

log = logging.getLogger(__name__)

__all__ = ["Route", "SPEC", "TRAVEL_MODES"]

# How ROUTE turns "this entity is going there" into a duration (PHASE4 E1).
# "time" is the original behaviour -- an ordinary duration spec. "network" is
# free-path travel: the shortest path between the two stations' nodes, divided
# by the entity's own `speed` expression, so a picker walking at 1.2 m/s and a
# pallet truck at 3 m/s use the same geometry.
TRAVEL_MODES = ("time", "network")


class Route(BaseProcess):
    """Send the entity to the next station of its `sequence`, applying that
    step's attribute values and advancing its `step` index; once the sequence
    is finished, forward along the block's own outgoing link instead."""

    def __init__(self, env, block, next_process=None, delay=0):
        super().__init__(env, block, next_process)
        self.delay = (
            delay if isinstance(delay, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": delay})
        )
        self.travel = "time"        # PHASE4 E1
        self.speed = ""
        self._speed_expr = None

    def _network_travel(self, entity, station):
        """(duration, path) for the walk from the entity's station to *station*.

        Both places resolve through `ctx.places`, so a station name, its alias
        or a bare node name all work; an entity that is nowhere yet (no
        `station`) starts from the destination, i.e. arrives instantly.
        """
        network = self.ctx.network
        places = self.ctx.places
        origin = places.get(getattr(entity, "station", "") or "")
        target = places.get(station)
        if target is None or not network.has(target):
            raise RuntimeError(
                f"Route block '{self.name}': station '{station}' has no network node; set its "
                f"STATION block's `node` param, or use travel mode \"time\".")
        if origin is None or origin == target:
            return 0.0, ()
        if self._speed_expr is None:
            self._speed_expr = compile_expression(self.speed or "1")
        try:
            speed = float(self._speed_expr.evaluate(self.expression_scope(entity)))
        except (ExpressionError, TypeError, ValueError) as exc:
            raise ExpressionError(f"Route block '{self.name}': speed: {exc}") from None
        if speed <= 0:
            raise RuntimeError(f"Route block '{self.name}': travel speed must be > 0, got {speed}.")
        # Quickest route at this walker's speed where a segment caps one (T7).
        path = network.path(origin, target, speed)
        if not path:
            raise RuntimeError(f"Route block '{self.name}': no path from '{origin}' to '{target}'.")
        return network.travel_time(path, speed), tuple(path)

    def self_process(self, entity):
        name = getattr(entity, "sequence", "")
        if not name:
            raise RuntimeError(
                f"Route block '{self.name}': entity {entity.id} has no sequence. Set one on the "
                f"CREATE block ('sequence'), or with an ASSIGN to entity.sequence.")
        steps = self.ctx.sequences.get(name)
        if steps is None:
            raise RuntimeError(f"Route block '{self.name}': entity {entity.id} names unknown "
                               f"sequence '{name}'.")

        index = int(getattr(entity, "step", 0))
        if index >= len(steps):
            self.forward(entity, 0)     # routing finished: on along the drawn link
            return

        step = steps[index]
        station = step["station"]
        target = self.ctx.stations.get(station)
        if target is None:
            raise RuntimeError(f"Route block '{self.name}': sequence '{name}' step {index} names "
                               f"station '{station}', which is not a STATION block in this model.")

        for key, value in step["values"].items():
            setattr(entity, key, value)
        entity.step = index + 1

        if self.travel == "network":
            duration, path = self._network_travel(entity, station)
        else:
            duration = self.sample_duration(self.delay, entity, extra={
                "from_station": getattr(entity, "station", ""), "to_station": station})
            path = ()
        if self.ctx.debug:
            log.debug("Entity %s routed to '%s' (step %s of '%s') at time %s.",
                      entity.id, station, index, name, self.env.now)
        self.send_to(entity, target, duration, path=path)

    def self_run(self, entity):
        # No yield: a transfer time becomes its own tiny process inside
        # send_to, exactly as a link delay does in forward().
        self.self_process(entity)

    def get_params_dict(self):
        return {
            "distribution": self.delay.dist_type,
            "dist_params": self.delay.params,
            "time_unit": self.delay.time_unit,
            "travel": self.travel,
            "speed": self.speed,
        }

    def set_params_from_dict(self, param_dict):
        self.delay = StochasticDistribution(
            param_dict.get("distribution", "Constant"),
            param_dict.get("dist_params", {"value": 0.0}),
            time_unit=param_dict.get("time_unit", "base"))
        self.travel = param_dict.get("travel", "time") or "time"
        self.speed = scaled(str(param_dict.get("speed", "") or ""),
                            field_factor(param(SPEC, "speed"), param_dict, self.ctx.units,
                                         SPEC.params))
        self._speed_expr = None


SPEC = BlockSpec(
    type_name="ROUTE", cls=Route,
    params=DIST_PARAMS + (
        ParamSpec("travel", str, "time",
                  description="How the transfer time is found: \"time\" uses the distribution "
                              "above, \"network\" divides the shortest path between the two "
                              "stations' nodes by `speed`.", quantity="none"),
        ParamSpec("speed", str, "",
                  description="Travel speed for `travel = \"network\"`, an expression in "
                              "distance per time unit (e.g. 1.2, or `entity.walk_speed`). "
                              "Empty means 1.", quantity="speed", kinds=("number", "expression")),
        ParamSpec("speed_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `speed`: \"base\" (the model's) or one of its quantity's units."),
    ),
    factory=lambda env, block: Route(env, block),
    family="control", shape="diamond",
    duration=True,
)
