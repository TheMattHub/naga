"""MOTION: a PROCESS whose service is a cycle of motions (automation
sub-project 1, D156-D158).

The cycle is a list of segments -- `move` (distance_m, optional payload_kg),
`pick`, `place`, `tool_change`, `dwell`, `process` (each a `time`) -- run in
order; the service time is their sum. Everything else is PROCESS's code: this
class overrides only `Process._serve`. Timed segments are drawn at the cycle's
start in segment order on the block's own stream (common random numbers,
exactly as PROCESS); a move is timed when it STARTS, because the separation
mode (D158) looks at where the workers are at that instant, and a move in
progress is never re-timed. A state carries a cycle mid-way as its `cycle`
(D444): the segment in progress, when it ends, and the times.
"""
import simpy

from nagasim.core import motion as timing
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks.process import SPEC as _PROCESS_SPEC, Process
from nagasim.core.distributions import StochasticDistribution
from nagasim.core.seizing import acquire, preempt_release
from nagasim.core.timeunits import base_factor
from nagasim.core.utils import timeout_at

__all__ = ["Motion", "SPEC"]

# PROCESS params MOTION does not take: the service is the cycle, and MOTION's
# own `time_unit` (default seconds) replaces PROCESS's (default base).
_NOT_MOTION = frozenset({"distribution", "dist_params", "time_unit"})


class Motion(Process):
    """Robot or cobot station whose service is a cycle of timed moves, picks, places and tool changes, so cycle time comes from motion data, not a distribution.

    A station whose service is a cycle of motions -- moves timed from a
    motion profile (trapezoidal, payload-derated, slowed for human-robot
    collaboration) plus picks, places, tool changes, dwells and process steps.
    Seizing, setups, failures, preemption, workers and blocking are PROCESS's."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.profile = ""
        self.hrc = "none"
        self.zone = {}
        self.segments = []
        self.time_unit = "seconds"
        self._times = []            # per segment: a Constant distribution, None for a move
        self._profile = None
        self.reset_motion_stats()

    def reset_motion_stats(self) -> None:
        """Counters behind "motion_summary"; also the warm-up reset (sim_runner)."""
        self.motion_stats = {}      # segment kind -> [count, total time in base units]
        self.cycles = 0
        self.cycle_time_total = 0.0
        self.moves = 0
        self.collaborative_moves = 0

    def _serve(self, entity):
        yield from self._run(entity, self._draw_times(entity), 0, [])

    def resume(self, entity, cycle: dict):
        """Generator: the cycle a state found mid-way (D444), from its segment in progress,
        which ends at `until`; the segments before it are in the record as they ran, the
        timed ones after it were drawn at the cycle's start, a move is timed when it starts.
        A plant's cycle (D464) is the segment and when it began: the times are drawn now,
        the moves begun timed now, and the segment ends at `since` + its time (now if past)."""
        if "since" in cycle:
            stage, times = cycle["stage"], self._draw_times(entity)
            record = []
            for i, segment in enumerate(self.segments[:stage + 1]):
                slow = segment.get("kind") == "move" and timing.collaborative_now(
                    self.ctx, self.hrc, self.zone)
                record.append((segment.get("kind"), self._move_time(segment, slow)
                               if segment.get("kind") == "move" else times[i], slow))
            until = max(float(cycle["since"]) + record[stage][1], self.env.now)
            yield from self._run(entity, times, stage, record, until)
            return
        stage, times, slow = cycle["stage"], cycle["times"], set(cycle.get("slow") or ())
        record = [(s.get("kind"), float(times[i]), i in slow)
                  for i, s in enumerate(self.segments[:stage + 1])]
        drawn = [None if s.get("kind") == "move" else times[i] for i, s in enumerate(self.segments)]
        yield from self._run(entity, drawn, stage, record, float(cycle["until"]))

    def _run(self, entity, times, done, record, until=None):
        """The cycle from segment *done* on, inside PROCESS's preemption loop. *until*: the
        segment in progress ends then (a state's, D444), with no changeover before it."""
        left = None if until is None else until - self.env.now      # of the current segment
        while True:
            started = None
            try:
                if (self.setup or self.setup_cost) and self.seizes and until is None:
                    yield from self._changeover(entity)
                while done < len(self.segments):
                    segment = self.segments[done]
                    if left is None:
                        collaborative = False
                        if segment.get("kind") == "move":
                            collaborative = timing.collaborative_now(self.ctx, self.hrc,
                                                                     self.zone)
                            left = self._move_time(segment, collaborative)
                        else:
                            left = times[done]
                        record.append((segment.get("kind"), left, collaborative))
                        self.emit("cycle_phase", entity=entity, state=segment.get("kind"),
                                  value=float(done), duration=left)
                    started = self.env.now
                    # D383: what a state reads; a state's segment lands on its own end
                    entity._cycle = (done, started + left if until is None else until,
                                     times, record)
                    yield self.env.timeout(left) if until is None else timeout_at(self.env,
                                                                                  until)
                    done, left, started, until = done + 1, None, None, None
                break
            except simpy.Interrupt:
                entity._cycle = until = None
                # PROCESS's rule per segment: "resume" finishes the segment in
                # progress (not re-timed, not re-traced); "restart" runs the whole
                # cycle again -- the same timed draws, or fresh ones with
                # `resample` -- and a restarted cycle's moves are timed anew.
                if started is not None:
                    if self.on_preempt != "restart":
                        left = max(left - (self.env.now - started), 0.0)
                    else:
                        done, left, record = 0, None, []
                        if self.resample:
                            times = self._draw_times(entity)
                preempt_release(self, entity, self.resource_name)
                yield from acquire(self, entity, self.resource_name, self.quantity,
                                   self.priority, preempt=self.preempt, resumed=True)
        entity._cycle = None
        self._commit(record)

    def _draw_times(self, entity) -> list:
        return [None if dist is None else self.sample_duration(dist, entity)
                for dist in self._times]

    def _profile_record(self) -> dict:
        if self._profile is None:
            profile = self.ctx.motion_profiles.get(self.profile)
            if profile is None:
                raise RuntimeError(f"MOTION block '{self.name}': unknown motion profile "
                                   f"'{self.profile}' (E_MOTION_PROFILE_UNKNOWN).")
            self._profile = profile
        return self._profile

    def _move_time(self, segment: dict, collaborative: bool) -> float:
        seconds = timing.move_seconds(self._profile_record(), segment, collaborative)
        return seconds * base_factor("time", "seconds", self.ctx.units)

    def _commit(self, record: list) -> None:
        """Count one COMPLETED cycle; a cycle restarted after a preemption was
        never finished, so only its last pass is here."""
        total = 0.0
        for kind, duration, collaborative in record:
            entry = self.motion_stats.setdefault(kind, [0, 0.0])
            entry[0] += 1
            entry[1] += duration
            total += duration
            if kind == "move":
                self.moves += 1
                self.collaborative_moves += int(collaborative)
        self.cycles += 1
        self.cycle_time_total += total

    def get_params_dict(self):
        params = {k: v for k, v in super().get_params_dict().items() if k not in _NOT_MOTION}
        return {**params, "profile": self.profile, "hrc": self.hrc, "zone": dict(self.zone),
                "segments": [dict(s) for s in self.segments], "time_unit": self.time_unit}

    def set_params_from_dict(self, param_dict):
        super().set_params_from_dict({k: v for k, v in param_dict.items()
                                      if k not in _NOT_MOTION})
        self.profile = str(param_dict.get("profile", "") or "")
        self.hrc = str(param_dict.get("hrc", "none") or "none")
        self.zone = dict(param_dict.get("zone") or {})
        self.segments = [dict(s) for s in param_dict.get("segments") or []
                         if isinstance(s, dict)]
        self.time_unit = str(param_dict.get("time_unit", "seconds") or "seconds")
        self._times = [None if s.get("kind") == "move"
                       else StochasticDistribution("Constant", {"value": s.get("time") or 0},
                                                   time_unit=self.time_unit)
                       for s in self.segments]
        self._profile = None


_INHERITED = tuple(p for p in _PROCESS_SPEC.params if p.name not in _NOT_MOTION)
_MOTION_PARAMS = (
    ParamSpec("profile", str, "",
              description="Name of a record in \"motion_profiles\": speed, acceleration, payload "
                          "derating and collaborative speed of whoever does the motions.", quantity="none"),
    ParamSpec("hrc", str, "none",
              description="Human-robot collaboration: \"none\", \"collaborative\" (every "
                          "move at the profile's collaborative_speed) or \"separation\" (a move "
                          "starts at collaborative_speed when a worker-fleet unit is within "
                          "the zone's radius of its node, by path or straight distance).", quantity="none"),
    ParamSpec("zone", dict, {},
              description="With hrc \"separation\": {\"name\": <a \"zones\" record>} -- the "
                          "record's node and radius win -- or, with no name, {\"node\": <network "
                          "node>, \"radius_m\": <metres>, \"measure\"?: \"path\"|\"straight\"}.",
              quantity="none"),
    ParamSpec("segments", list, [],
              description="The cycle, in order: {\"kind\": \"move\", \"distance_m\", "
                          "\"payload_kg\"?} or {\"kind\": pick|place|tool_change|dwell|process, "
                          "\"time\": <number or expression>}. The service time is their sum.", quantity="time", kinds=("table",), unit_key="time_unit"),
    ParamSpec("time_unit", str, "seconds",
              description="Unit of every segment `time`: \"base\" or seconds/minutes/hours/"
                          "days. Moves are always computed in seconds.", quantity="none", kinds=("choice",)),
)

SPEC = BlockSpec(
    type_name="MOTION", cls=Motion,
    params=(_INHERITED[0], *_MOTION_PARAMS, *_INHERITED[1:]),     # "action" first, as PROCESS
    factory=lambda env, block: Motion(env, block),
    family="work", shape="motion",
    duration=True, holding=True, service=True, queueing=True, uses_resource=True,
)
