"""TRANSPORT: move an entity with a vehicle from a mover fleet (PHASE4 E1).

ROUTE with a forklift. The entity is going to the same place ROUTE would send
it -- the next station of its `sequence`, or the `destination` this block names
-- but instead of a transfer time out of a distribution, a vehicle has to come
and get it:

    request a vehicle  ->  it drives EMPTY to the entity's node
    ->  load  ->  it drives LOADED to the destination  ->  unload  ->  free

One block for the whole cycle, deliberately: Arena's Request/Transport/Free
triple only earns its keep when a model holds a vehicle across several legs
(ARCHITECTURE D68). The entity is
delivered when the unload finishes; the vehicle is busy for the whole cycle and,
with the fleet's `park: "home"`, for the drive home as well.

A fleet with `loads: k` (PHASE6 W3-M4, D116) turns the cycle into a TOUR when
other TRANSPORT requests are waiting as a vehicle is granted: up to k-1 of them
ride along, the vehicle visits every pick-up and then every drop in
nearest-neighbour order, and each entity goes on at its own drop.

A fleet with an `arm` is a mobile cobot (automation sub-project 3, D168-D170):
at a STATION that declares `tending` the flat load and unload become the pick
and place cycles of `core/tending.py`, the machine keeps a finished part until
it is picked and the cobot holds a part until the machine accepts it.

Every duration is exact geometry: `distance / speed`, with the distance the
shortest path on the network and the speed the fleet's loaded or empty one,
capped by any segment speed limit. Nothing is stepped or integrated.
"""
import logging

from nagasim.core import blocking, tending
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.utils import timeout_at

log = logging.getLogger(__name__)

__all__ = ["Transport", "SPEC"]


class Transport(BaseProcess):
    """Seize a vehicle, fetch the entity, carry it, unload, release."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.fleet_name = ""
        self.destination = ""
        self.priority = 0
        self.trip_count = 0

    @property
    def space_passthrough(self) -> bool:
        """An arm fleet's TRANSPORT takes a part off its machine when the
        cobot has picked it, not when the part arrives here (D169). Only in a
        model with a tending station: elsewhere an arm fleet is a plain one."""
        fleet = self.ctx.fleets.get(self.fleet_name)
        return (fleet is not None and bool(fleet.arm)
                and any(s.tending for s in self.ctx.stations.values()))

    # -- where the entity is and where it is going --------------------------
    def _origin(self, entity) -> str:
        place = getattr(entity, "station", "") or ""
        node = self.ctx.places.get(place)
        if node is None:
            raise RuntimeError(
                f"Transport block '{self.name}': entity {entity.id} is at '{place}', which has no "
                f"network node. Route it through a STATION with a `node`, or set `destination`.")
        return node

    def _target(self, entity):
        """(target block, destination node, station name) for this trip.

        With `destination` empty the block reads the entity's routing exactly as
        ROUTE does, so a TRANSPORT can simply replace a ROUTE in a routed model;
        with a destination it names a STATION directly, for a model with no
        sequences at all.
        """
        station = self.destination
        if not station:
            name = getattr(entity, "sequence", "")
            if not name:
                raise RuntimeError(
                    f"Transport block '{self.name}': entity {entity.id} has no sequence and the "
                    f"block names no destination. Set one, or give the entity a routing.")
            steps = self.ctx.sequences.get(name)
            if steps is None:
                raise RuntimeError(f"Transport block '{self.name}': entity {entity.id} names "
                                   f"unknown sequence '{name}'.")
            index = int(getattr(entity, "step", 0))
            if index >= len(steps):
                return None, "", ""          # routing finished: out along the drawn link
            step = steps[index]
            station = step["station"]
            for key, value in step["values"].items():
                setattr(entity, key, value)
            entity.step = index + 1
        target = self.ctx.stations.get(station)
        if target is None:
            raise RuntimeError(f"Transport block '{self.name}': '{station}' is not a STATION "
                               f"block in this model.")
        node = self.ctx.places.get(station)
        if node is None:
            raise RuntimeError(f"Transport block '{self.name}': station '{station}' has no "
                               f"network node; set its `node` param.")
        return target, node, station

    # -- the trip ------------------------------------------------------------
    def self_process(self, entity):
        fleet = self.ctx.fleets.get(self.fleet_name)
        if fleet is None:
            raise RuntimeError(f"Transport block '{self.name}': unknown fleet "
                               f"'{self.fleet_name}'. Declare it under \"fleets\".")
        target, destination, _station = self._target(entity)
        if target is None:
            self.forward(entity, 0)
            return
        origin = self._origin(entity)
        job = (self, entity, origin, destination, target)

        # Tending (D169): an arm fleet picks the part out of a station that
        # declares `tending`, which keeps it in its machine until the pick
        # ends. Anywhere else the part leaves its machine here, as ever (a
        # no-op unless the token rode through `space_passthrough`).
        here = self.ctx.stations.get(getattr(entity, "station", "") or "")
        picks = tending.applies(fleet, here)
        if not picks:
            blocking.accept(entity)
        arrived = self.env.now
        held = tending.cobot_of(self, entity, fleet)
        if held is not None:
            request, mover = held         # the cobot that stayed with the part: no dispatch
            mover.trips += 1
            fleet.loads_carried += 1
        else:
            if picks and tending.call_waiting_cobot(self.ctx, fleet, here, job):
                return        # a cobot waiting loaded at this machine exchanges it (D170)
            if picks:
                tending.check_stall(self.env, fleet)   # no cobot may ever come
            got = yield from self._dispatch(fleet, job)
            if got is None:
                return
            request, mover = got
        mover.task = ("pickup", job, None)          # D383: what a state reads
        yield from fleet.drive(mover, origin, loaded=False, block=self)
        if picks:
            yield from tending.pick(self, entity, fleet, mover, here, arrived)
        else:
            mover.task = ("load", job, None)
            yield from fleet.handle(mover, fleet.duration(self, entity, fleet.load_time))
        yield from self._deliveries(fleet, request, mover, job)

    def _dispatch(self, fleet, job, since=None):
        """Generator: wait for a vehicle for *job*; (request, vehicle), or None when
        another vehicle's tour or exchange carries it, or this one's tour is done."""
        _block, entity, origin, _destination, _target = job
        request, mover = yield from fleet.acquire(self, entity, origin, self.priority, job=job,
                                                  since=since)
        if mover is None:
            return None       # attached to another vehicle's tour or exchange, which carries it
        jobs = fleet.attach(mover)
        if jobs:
            yield from self._tour(fleet, mover, [job] + jobs)
            fleet.free(request, mover, block=self, entity=entity)
            fleet.park_home(mover, block=self)
            return None
        return request, mover

    def _deliveries(self, fleet, request, mover, job):
        # A loop, not a recursion: each delivery may exchange its part for the
        # next job, with no end to the chain.
        while job is not None:
            job = yield from job[0]._deliver(fleet, request, mover, *job[1:])

    def resume(self, entity, trip: dict, record: dict | None, since=None, gate=None):
        """Generator: a trip a state found waiting for a vehicle or under way (D385),
        entered at its stage. A stage with its time (`drive.started`, `until`) ends when
        it was due; one without (a plant says where a vehicle is, not since when) starts
        now from the vehicle's node. At a tending station the load is the cobot's pick
        (D446) and the unload its place, whose part the machine already holds: *gate* is
        what the machine waits on (D447)."""
        fleet, env = self.ctx.fleets[self.fleet_name], self.env
        origin, destination = trip["origin"], trip["destination"]
        target = self.ctx.stations[trip["target"]]
        job = (self, entity, origin, destination, target)
        here = self.ctx.stations.get(getattr(entity, "station", "") or "")
        picks, begun = tending.applies(fleet, here), env.now  # its wait before was counted there
        if trip.get("unit") is None:
            got = yield from self._dispatch(fleet, job, since)
            if got is None:
                return
            request, mover = got
            stage, record = "pickup", {}
        else:
            mover = fleet.movers[trip["unit"]]
            request = fleet.claim(mover)
            yield request
            self.ctx.settling -= 1
            stage, record = trip.get("stage", "carry"), record or {}
        started = (record.get("drive") or {}).get("started")
        if stage in ("pickup", "load"):
            until = None
            if stage == "pickup":
                mover.task = ("pickup", job, None)
                yield from fleet.drive(mover, origin, loaded=False, block=self, started=started)
            else:
                until = record.get("until")        # a load under way ends when it was due
            if picks:
                mover.task = ("load", job, trip.get("cycle"))
                yield from tending.pick(self, entity, fleet, mover, here, begun, until)
            else:
                mover.task = ("load", job, None)
                yield from fleet.handle(mover, fleet.duration(self, entity, fleet.load_time)
                                        if until is None else 0.0, until)
            yield from self._deliveries(fleet, request, mover, job)
            return
        if stage == "carry" and started is None:
            # from where the vehicle stands: the block's own loaded half
            yield from self._deliveries(fleet, request, mover,
                                        (self, entity, mover.node, destination, target))
            return
        if stage == "carry":
            unload = float(trip.get("unload") or 0.0)
            mover.task = ("carry", job, unload)
            path, travel = fleet.leg(mover.node, destination, fleet.speed)
            self.emit("entity_moved", entity=entity, next_index=0, target=target.name,
                      duration=max(started + travel + unload - env.now, 0.0), path=tuple(path),
                      travel=max(started + travel - env.now, 0.0))
            yield from fleet.drive(mover, destination, loaded=True, block=self, started=started)
            job = yield from self._arrive(fleet, request, mover, entity, target, unload)
            yield from self._deliveries(fleet, request, mover, job)
            return
        if gate is not None:        # a cobot placing the part in its machine (D447)
            mover.task = ("unload", job, trip.get("cycle"))
            job = yield from self._arrive(fleet, request, mover, entity, target, 0.0,
                                          (gate, record["until"]))
            yield from self._deliveries(fleet, request, mover, job)
            return
        mover.task = ("unload", job, None)
        self.emit("entity_moved", entity=entity, next_index=0, target=target.name,
                  duration=max((record.get("until") or env.now) - env.now, 0.0),
                  path=(destination,), travel=0.0)
        yield from fleet.handle(mover, fleet.duration(self, entity, fleet.unload_time)
                                if record.get("until") is None else 0.0, record.get("until"))
        self.trip_count += 1
        self._drop(fleet, request, mover, entity, target)

    def _drop(self, fleet, request, mover, entity, target) -> None:
        """The entity is delivered the moment it is unloaded, and the vehicle is
        free the moment it is empty: under `park: "home"` the drive home is
        its own interruptible process, so a request arriving mid-return
        diverts it from wherever it has got to (PHASE4 T7, ARCHITECTURE D68)."""
        target.self_run(entity)
        fleet.free(request, mover, block=self, entity=entity)
        fleet.park_home(mover, block=self)

    def _deliver(self, fleet, request, mover, entity, origin, destination, target):
        """Generator: the loaded half of one trip -- drive, unload, hand the
        entity to its station, free the vehicle. Returns the job of the finished
        part a cobot exchanged for *entity* (D170), which it carries on with
        instead of being freed; None otherwise."""
        places = tending.applies(fleet, target)
        unload = 0.0 if places else fleet.duration(self, entity, fleet.unload_time)
        if origin != destination:
            path, travel = fleet.leg(origin, destination, fleet.speed)
        else:
            path, travel = (destination,), 0.0
        self.emit("entity_moved", entity=entity, next_index=0, target=target.name,
                  duration=travel + unload, path=tuple(path), travel=travel)

        mover.task = ("carry", (self, entity, origin, destination, target), unload)
        yield from fleet.drive(mover, destination, loaded=True, block=self)
        return (yield from self._arrive(fleet, request, mover, entity, target, unload))

    def _arrive(self, fleet, request, mover, entity, target, unload, placing=None):
        """Generator: the loaded vehicle is at *target*: it unloads, or a cobot places the
        part, then hands it over and is freed. *placing*: (gate, until) of a state's place
        in progress (D447). Returns `_deliver`'s exchanged job."""
        places = tending.applies(fleet, target)
        swapped = None
        if placing is not None:
            yield from tending.placing(self, fleet, mover, target, *placing)
        elif places:
            # The cobot waits loaded until the machine accepts the part, then
            # places it; the part entered the station inside `place`.
            swapped = yield from tending.place(self, entity, fleet, mover, target)
        else:
            mover.task = ("unload", mover.task[1], None)
            yield from fleet.handle(mover, unload)
        self.trip_count += 1
        if self.ctx.debug:
            log.debug("Entity %s transported to '%s' by %s at %s.", entity.id, target.name,
                      fleet.name, self.env.now)
        if swapped is not None:
            return swapped
        if (places and target.tending.get("cobot") == "stay"
                and tending.stay(self, entity, fleet, request, mover, target)):
            return None       # the slot stays held; the next TRANSPORT or the exit frees it
        if not places:
            self._drop(fleet, request, mover, entity, target)
            return None
        fleet.free(request, mover, block=self, entity=entity)
        fleet.park_home(mover, block=self)
        return None

    def _tour(self, fleet, mover, jobs):
        """Generator: one vehicle carries every job in *jobs* (W3-M4, D116).

        Stops: the pick-ups in nearest-neighbour order from the vehicle's
        position, then the drops in nearest-neighbour order from the last
        pick-up, by network length as `nearest` measures (a gantry's own
        travel time, D118). Load and unload times are sampled up front, so the
        timetable -- and with it each entity's `entity_moved` ride over the
        legs it shares -- is known at dispatch.
        """
        stops = []                            # (node, job, handling seconds, is_drop)
        # A gantry diverted mid-hop starts from `mover.pos`, not its node (#194).
        here, pos = mover.node, mover.pos
        for is_drop in (False, True):
            left = list(jobs)
            while left:
                # ponytail: nearest-neighbour, not an optimal tour; a TSP solver if k grows
                job = min(left, key=lambda j: fleet.leg_distance(here, j[3 if is_drop else 2], pos))
                left.remove(job)
                here, pos = job[3 if is_drop else 2], None
                expr = fleet.unload_time if is_drop else fleet.load_time
                stops.append((here, job, fleet.duration(job[0], job[1], expr), is_drop))
        # Timetable: per stop the path driven to it, the arrival and the end of handling.
        legs, t, here, pos, speed = [], self.env.now, mover.node, mover.pos, fleet.speed_empty
        for node, _job, handling, _drop in stops:
            if node != here or pos is not None:
                path, leg_time = fleet.leg(here, node, speed, pos)
                t += leg_time
            else:
                path = [node]
            legs.append((path, t, t + handling))
            t, here, pos, speed = t + handling, node, None, fleet.speed
        drop_at = {id(stop[1]): n for n, stop in enumerate(stops) if stop[3]}

        for n, (node, job, handling, is_drop) in enumerate(stops):
            block, entity, _origin, _destination, target = job
            yield from fleet.drive(mover, node, loaded=n > 0, block=self)
            yield from fleet.handle(mover, handling)
            if not is_drop:
                last = drop_at[id(job)]
                ride = [node]
                for path, _arrive, _end in legs[n + 1:last + 1]:
                    ride.extend(path[1:])
                start = legs[n][2]
                block.emit("entity_moved", entity=entity, next_index=0, target=target.name,
                           duration=legs[last][2] - start, path=tuple(ride),
                           travel=legs[last][1] - start)
                continue
            block.trip_count += 1
            target.self_run(entity)

    def self_run(self, entity):
        self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {"fleet": self.fleet_name, "destination": self.destination,
                "priority": self.priority}

    def set_params_from_dict(self, param_dict):
        self.fleet_name = str(param_dict.get("fleet", "") or "").strip()
        self.destination = str(param_dict.get("destination", "") or "").strip()
        self.priority = int(param_dict.get("priority", 0))


SPEC = BlockSpec(
    type_name="TRANSPORT", cls=Transport,
    params=(
        ParamSpec("fleet", str, "", required=True,
                  description="Name of the mover fleet whose vehicles do the carrying.", quantity="none"),
        ParamSpec("destination", str, "",
                  description="STATION the entity is carried to. Empty: the next station of the "
                              "entity's routing, exactly as ROUTE would choose it.", quantity="none"),
        ParamSpec("priority", int, 0,
                  description="Request priority for a vehicle; lower is served first, and "
                              "outranks the fleet's dispatching rule.", quantity="none"),
    ),
    factory=lambda env, block: Transport(env, block),
    family="move",
)
