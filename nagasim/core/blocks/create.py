import dataclasses
import logging
import math

from nagasim.core.agents import AgentRef
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, DIST_PARAMS, ParamSpec
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.orders import ORDERS_MODE
from nagasim.core.quantities import param, to_base
from nagasim.core.sim_classes import spawn_entity
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Create", "SPEC", "ARRIVAL_MODES"]

# "Interarrival": sample the gap from `distribution` (the classic behaviour).
# "Schedule": a non-homogeneous Poisson process driven by an arrival schedule
# whose values are rates per time unit (ROADMAP 1.4).
# "Orders": one release per order this block owns, at the order's own release
# time, carrying its due date and routing (PHASE4 A3, ARCHITECTURE D53).
ARRIVAL_MODES = ("Interarrival", "Schedule", ORDERS_MODE)

# Rejected thinning candidates between two hand-backs to the event loop: large
# enough that the extra events do not matter, small enough that a closed
# stretch reaches the end of the run in bounded work per event.
_THINNING_YIELD = 256


class Create(BaseProcess):
    """Entity source. Either samples the inter-arrival gap from `distribution`
    (`arrival_mode = "Interarrival"`, the classic behaviour) or drives a
    non-homogeneous Poisson process from an arrival `Schedule` by thinning
    (`arrival_mode = "Schedule"`). Every model needs at least one
    CREATE block."""

    def __init__(self, env, block, time_between_arrivals, max_arrivals, entities_per_arrival=1, next_process=None, attributes=None, start_time=0, population=""):
        super().__init__(env, block, next_process)
        self.max_arrivals = max_arrivals
        # A population every entity this block creates also gets an agent in
        # (H2, D130): the same `entity.agent` an agent's own `enter` action
        # sets (AgentSystem.spawn), so SEND/COUNT/... treat either source the
        # same way. Empty = off (unchanged behaviour).
        self.population = population
        self.time_between_arrivals = (
            time_between_arrivals if isinstance(time_between_arrivals, StochasticDistribution)
            else StochasticDistribution("Constant", {"value": time_between_arrivals})
        )
        self.entities_per_arrival = entities_per_arrival
        self.attributes = attributes.copy() if attributes else {}
        self.start_time = start_time
        self.arrival_mode = "Interarrival"
        self.schedule_name = ""
        # Routing (PHASE4 A2): the name of a declared sequence, stamped on every
        # entity this block creates along with `step = 0`. Empty = no routing.
        self.sequence = ""

        self.counter_arrivals = 0
        self.created_entities = 0
        self.next_at = None     # D235: the time of the next arrival, once one is drawn
        # #467: when it was drawn, which orders two arrivals due at one instant (SimPy
        # serves equal times in the order they were scheduled); a state carries it.
        self.scheduled_at = None
        # #472: that arrival's event; its place in SimPy's queue breaks a tie on
        # scheduled_at (two drawn in one instant), and `rank` carries it
        self.pending, self.rank = None, None
        self._walk = None       # D455: deterministic thinning's place on the schedule

    def self_process(self):
        if self.arrival_mode == ORDERS_MODE:
            yield from self._release_orders()
            return

                # wait until first creation time
        if self.env.now < self.start_time:
            self.next_at = self.start_time
            if self.scheduled_at is None:       # not put back by a state
                self.scheduled_at = self.env.now
            # Absolute: a state hands back the very instant an arrival was due (D232).
            self.pending = timeout_at(self.env, self.start_time)
            yield self.pending

        while self.counter_arrivals < self.max_arrivals or self.max_arrivals == 0:

            if self.arrival_mode == "Schedule":
                if not (yield from self._wait_for_thinned_arrival()):
                    break
            # Wait before each batch, but skip if it's the first round and we're at start_time
            elif self.created_entities > 0 or self.env.now > self.start_time:
                if self.time_between_arrivals.dist_type == "FixedSchedule":
                    next_time = self.time_between_arrivals.sample(self.rng, current=self.env.now,
                                                                     scope=self.expression_scope())
                    if next_time is None:
                        break  # no more arrivals
                    self.next_at, self.scheduled_at = next_time, self.env.now
                    self.pending = self.env.timeout(next_time - self.env.now)
                    yield self.pending
                else:
                    iat = self.sample_duration(self.time_between_arrivals)  # inter-arrival time
                    self.next_at, self.scheduled_at = self.env.now + iat, self.env.now
                    self.pending = self.env.timeout(iat)
                    yield self.pending

            self._spawn_batch()
            self.counter_arrivals += 1 # An arrival can be a batch of entities

    def _wait_for_thinned_arrival(self):
        """Advance to the next arrival of the non-homogeneous Poisson process.

        Thinning (Lewis-Shedler): candidates come from an exponential with the
        schedule's maximum rate and are kept with probability
        rate(t) / max_rate, which needs exactly one extra uniform per candidate
        and no integration of the rate function.

        Candidate times are summed here and only the accepted one is waited
        for, so a rejected candidate costs two draws and no event. That is
        sound because nothing changes an arrival schedule during a run and the
        block's random stream is its own: the draws, their order and each
        float sum are what one wait per candidate gave. A zero-rate stretch
        still cannot spin: every `_THINNING_YIELD` rejections the clock is
        moved to the candidate reached, which lets the run end and other
        processes go.

        Returns False when the block can never produce another arrival, so the
        caller stops.
        """
        schedule = self.ctx.schedules.get(self.schedule_name)
        if schedule is None:
            raise RuntimeError(f"Block '{self.name}': unknown arrival schedule "
                               f"'{self.schedule_name}'")
        max_rate = schedule.max_value
        if max_rate <= 0:
            return False
        if self.ctx.deterministic:
            return (yield from self._fluid_arrival(schedule))
        t = self.env.now
        rejected = 0
        while True:
            t += float(self.rng.exponential(1.0 / max_rate))
            if self.rng.random() * max_rate <= schedule.value_at(t):
                yield timeout_at(self.env, t)   # absolute: now + (t - now) can miss t (#341)
                return True
            rejected += 1
            if rejected % _THINNING_YIELD == 0:
                yield timeout_at(self.env, t)

    def _fluid_arrival(self, schedule):
        """Deterministic mode (D119, D455): arrival k comes when the schedule's
        integrated rate, from `start_time`, reaches k. No draw, so a run started
        from a state's arrival count walks the same segments to the same times.
        `_walk` keeps (segment start, area there) between arrivals.

        The walk hands the clock back as the stochastic path does (#475): past the run's
        horizon (the run ends before that event is served) and every `_THINNING_YIELD`
        segments, so a tiny rate cannot spin it. Its locals survive the yield, and a state
        taken then carries the count only, from which a started run walks the same segments.
        """
        target = self.counter_arrivals + 1
        t, area = self._walk or (float(self.start_time), 0.0)
        horizon, segments = self.ctx.sim_horizon or math.inf, 0
        while True:
            end = schedule.next_change_after(t)
            # The rate over (t, end), read inside it: a folded bound one ulp off t (#476)
            # gives t the value of the segment before.
            rate = schedule.value_at(t if math.isinf(end) else t + (end - t) / 2)
            if rate > 0 and area + rate * (end - t) >= target:
                break
            if math.isinf(end):
                return False                    # the rate is 0 for ever
            area += rate * (end - t)
            t = end
            segments += 1
            if t > horizon or segments % _THINNING_YIELD == 0:
                yield timeout_at(self.env, t)
                horizon = self.ctx.sim_horizon or math.inf
        self._walk = (t, area)
        self.next_at, self.scheduled_at = t + (target - area) / rate, self.env.now
        yield timeout_at(self.env, self.next_at)
        return True

    def _release_orders(self):
        """Arrival mode "Orders" (PHASE4 A3): one release per order this block
        owns, at the order's own release time, in release order. The block's
        `distribution`, `max_arrivals` and `entities_per_arrival` are ignored
        -- the order book is the arrival process. `start_time` still holds: an
        order released before it waits for it, so the block's own switch-on
        time stays meaningful.
        """
        mine = sorted((o for o in self.ctx.orders.values() if o.block == self.name),
                      key=lambda o: (o.release, o.id))
        # D485: a state with order counters says WHICH were released (a plant releases out
        # of order); an older one only how many (`initial_state.arrivals`); 0 otherwise.
        released = self.ctx.released_orders
        todo = mine[self.counter_arrivals:] if released is None \
            else [o for o in mine if o.id not in released]
        for order in (o for o in todo if not o.held):
            at = max(float(order.release), float(self.start_time))
            if at > self.env.now:
                self.next_at = at       # D235: a state is not taken on a release
                yield self.env.timeout(at - self.env.now)
            for _ in range(order.quantity):
                self._spawn_batch(order=order)
            order.created += order.quantity
            self.counter_arrivals += 1

    def _spawn_batch(self, order=None):
        for _ in range(1 if order is not None else self.entities_per_arrival):
            entity = spawn_entity(self.ctx, attrs=self.attributes)
            sequence = self.sequence
            if order is not None:
                sequence = order.sequence or sequence
                entity.order_id = order.id
                if order.entity_type:
                    entity.e_type = order.entity_type
                if order.due is not None:
                    entity.due_date = float(order.due)
                if order.priority is not None:
                    entity.priority = order.priority
                for key, value in order.attributes.items():
                    setattr(entity, key, value)
                if order.plan:
                    entity.plan = dict(order.plan)     # PHASE6 W4 P4, D122
            if sequence:
                entity.sequence = sequence
                entity.step = 0
            if self.population:
                self._spawn_agent(entity)
            self.created_entities += 1
            self.emit("entity_created", entity=entity)
            if self.ctx.debug:
                log.debug("Entity %s has been created at time %s.", entity.id, self.env.now)
            self.forward(entity, 0)

    def self_run(self):
        return self.env.process(self.self_process())

    def _spawn_agent(self, entity) -> None:
        """`population` (D130): a C5 birth, then `entity.agent` as `enter` sets
        it; `from_block` because this runs outside AgentSystem._run (#224)."""
        system = self.ctx.agent_system
        if system is None:
            raise ValueError(f"CREATE '{self.name}': population '{self.population}' "
                             f"is not declared")
        pop = system.population(self.population)
        i = system.from_block(system.add_agent, pop)
        entity.agent = AgentRef(pop.name, i, int(pop.gen[i]))

    def get_params_dict(self):
        return {
            "e_type": self.attributes.get("e_type", "DEFAULT"),
            "arrival_mode": self.arrival_mode,
            "schedule_name": self.schedule_name,
            "distribution": getattr(self, "distribution", self.time_between_arrivals.dist_type),
            "dist_params": getattr(self, "dist_params", self.time_between_arrivals.params),
            "time_unit": self.time_between_arrivals.time_unit,
            "entities_per_arrival": self.entities_per_arrival,
            "max_arrivals": self.max_arrivals,
            "start_time": self.start_time,
            "start_time_unit": self.ctx.units.time,   # the attribute is in the model unit
            "sequence": self.sequence,
            "population": self.population,
        }

    def set_params_from_dict(self, param_dict): #Not really needed but ok
        self.distribution = param_dict.get("distribution", "Constant")
        self.distribution_params = param_dict.get("dist_params", {"value": 1})
        time_unit = param_dict.get("time_unit", "base")
        self.time_between_arrivals = StochasticDistribution(
            self.distribution, self.distribution_params, time_unit=time_unit)

        self.arrival_mode = param_dict.get("arrival_mode", "Interarrival")
        self.schedule_name = param_dict.get("schedule_name", "")
        self.entities_per_arrival = param_dict.get("entities_per_arrival", 1)
        self.max_arrivals = param_dict.get("max_arrivals", 0)
        self.start_time = to_base(param(SPEC, "start_time"), param_dict, self.ctx.units,
                                  SPEC.params)
        self.sequence = param_dict.get("sequence", "")
        self.population = param_dict.get("population", "")

        e_type = param_dict.get("e_type", "A")
        self.attributes = {"e_type": e_type}


SPEC = BlockSpec(
    type_name="CREATE", cls=Create,
    params=(
        ParamSpec("e_type", str, "DEFAULT", description="Entity type assigned to every entity this block creates.", quantity="none"),
        # Declared before the distribution so the properties form can let one
        # editor own the pair (ui/properties.py swaps them by mode).
        ParamSpec("arrival_mode", str, "Interarrival",
                  description="\"Interarrival\": sample the gap from `distribution`. \"Schedule\": a "
                              "non-homogeneous Poisson process driven by `schedule_name`. "
                              "\"Orders\": release the model's declared orders, each at its own "
                              "release time with its due date, quantity and routing.", quantity="none"),
        ParamSpec("schedule_name", str, "",
                  description="Name of an arrival Schedule to drive from, when arrival_mode is \"Schedule\".", quantity="none"),
        # Not the shared {"value": 0.0}: a zero gap with unlimited arrivals never
        # lets time advance (DEVLOG #88).
        *(dataclasses.replace(p, default={"value": 1.0})
          if p.name == "dist_params" else p for p in DIST_PARAMS),
        ParamSpec("entities_per_arrival", int, 1, description="Number of entities created per arrival event.", quantity="count", min=1),
        ParamSpec("max_arrivals", int, 0, description="Number of arrival events before this block stops; 0 = unlimited.", quantity="count", min=0),
        ParamSpec("start_time", float, 0, description="Simulation time of the first possible arrival.", quantity="time", min=0),
        ParamSpec("start_time_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `start_time`: \"base\" (the model's) or one of its quantity's units."),
        ParamSpec("sequence", str, "",
                  description="Name of a declared routing sequence: every entity "
                              "created here starts it at step 0. Empty = no routing. It is a "
                              "literal name, not an expression -- use an ASSIGN to "
                              "entity.sequence when the routing depends on the entity.", quantity="none"),
        ParamSpec("population", str, "",
                  description="Name of a declared population. When set, every entity created "
                              "here also gets a new agent there (its statechart starts), and "
                              "entity.agent points to it. Empty = off. A full population raises "
                              "the same error a spawn action would.", quantity="none"),
    ),
    factory=lambda env, block: Create(env, block, time_between_arrivals=1, max_arrivals=0),
    family="source", shape="pill",
    duration=True,
)
