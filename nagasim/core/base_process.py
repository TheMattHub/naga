from abc import ABC, abstractmethod
from nagasim.core import blocking
from nagasim.core.expressions import ExpressionScope
from nagasim.core.timeunits import base_factor
import numpy as np
import hashlib
import logging

log = logging.getLogger(__name__)

__all__ = ["BaseProcess", "Block", "deterministic_hash"]


def deterministic_hash(name: str) -> int: # the builtin hash() is randomized per process (security), so it can't be used here
    """ The decoded hash method ensures:
    - Consistency across Python sessions and platforms
    - Low risk of collisions
    - Full determinism across replications, even after restarting the editor
    """
    return int(hashlib.md5(name.encode()).hexdigest(), 16) % 10_000
        # name --> different seed process
        # % 10_000 --> avoids overflows while maintaining sufficient entropy


class BaseProcess(ABC):

    """
    Abstract base for every "base process", i.e. every object that must launch an env.process().
    self_process() must be implemented by subclasses and defines the process to be launched.
    self_run() launches self_process() in the environment passed as a parameter.
    """

    # Blocking after service (PHASE4 T5, ARCHITECTURE D78). Class attributes, so
    # a model with no finite buffer anywhere pays one boolean test per forward
    # and not one byte per block.
    space = None                 # a blocking.Space when this block has places
    space_passthrough = False    # the "still in the machine" token rides through
    space_aware = False          # this model has finite space somewhere
    can_block = False            # this block can hold a resource until accepted
    block_after_service = False  # ... and `analyse` found it must
    # Blocking BEFORE service (PHASE5 O4): reserve a downstream place before
    # the job starts; `analyse` resolves `reserve_space` from `space_of`.
    hold_until_space = False
    space_of = ""
    reserve_space = None

    def __init__(self, env, block, next_process=None):

        self.env = env
        # The simulation context (ARCHITECTURE D6); every block reads state
        # here. A plain attribute, not a property over env.ctx: it is read some
        # twenty times per entity and the context never changes during a run.
        self.ctx = env.ctx
        self._block = block
        self.next_process = next_process if next_process is not None else [] # needed to manage the graph's next_process lists
        # Per-branch transfer delay (ROADMAP 1.5), aligned by index with
        # next_process; GraphModel.link() appends to both together. An index
        # past the end of this list (the normal case: no delay, or a
        # next_process set directly by a test, bypassing GraphModel.link())
        # means "no delay" -- forward() below treats it exactly like None.
        self.link_delays = []
        self._forward_cache = {}    # output index -> (target, its name, counter key); see forward()

        # assign a unique fallback name: class name + instance counter
        name_counter = env.ctx.name_counter

        cls_name = self.__class__.__name__.lower()

        # Respect existing name,
        if not block.name: # auto-name blocks created without one (e.g. in tests)
            count = name_counter.get(cls_name, 0) + 1
            name_counter[cls_name] = count
            self.name = f"{cls_name}_{count}"
            self.block.name = self.name
        else: # the normal case: the editor/JSON layout already names the block
            self.name = block.name

            # Optional: sync back to the context counter if block.name = "decide_7"
            if "_" in self.name:
                prefix, suffix = self.name.rsplit("_", 1)
                try:
                    idx = int(suffix)
                    name_counter[prefix] = max(name_counter.get(prefix, 0), idx + 1)
                except ValueError:
                    pass

        """
        Reproducible, independent stream per (replication, block): the two
        entropy sources go into a SeedSequence as a list, so seed s + hash h
        can no longer collide with seed s+1 + hash h-1.
        """
        self.rng = np.random.default_rng(np.random.SeedSequence([env.ctx.seed, deterministic_hash(self.name)]))
        log.debug("RNG initialized for %s from (seed=%s, hash=%s)", self.name, env.ctx.seed, deterministic_hash(self.name))

        self._active_entity = []

        self._resource = None

        self.negative_samples = 0  # count of durations truncated to 0 by sample_duration (DEVLOG negative-duration guard)

        # Submodel parameters bound to this block (ROADMAP 2.3 part B): empty
        # for every block a plain model declares; GraphModel copies the block's
        # "locals" section here for a block that came out of a flattened
        # submodel instance.
        self.locals = {}

        env.ctx.process_list.append(self)

    def expression_scope(self, entity=None, extra=None):
        """What a model expression sampled here may read (ROADMAP 1.1).

        *extra* (PHASE4 A2) adds names visible to ONE expression only, on top of
        the block's submodel locals: ROUTE's transfer time sees `from_station`
        and `to_station`, a PROCESS setup sees `previous_key` and `key`. They
        are merged over `locals`, so a submodel parameter never shadows them.
        """
        locals_ = {**self.locals, **extra} if extra else self.locals
        return ExpressionScope(entity=entity, ctx=self.ctx, rng=self.rng, locals=locals_)

    def sample_duration(self, dist, entity=None, extra=None) -> float:
        """Sample *dist* for use as a SimPy timeout, never a negative one.

        Every duration-bearing block (Create inter-arrival, Delay, Seize service
        delay, Release delay, Dispose delay, Signal delay) goes through this one
        helper instead of calling dist.sample(self.rng) directly, so a
        distribution that can produce negative values (Normal, Cauchy,
        JohnsonSU, ...) never reaches env.timeout() with one: SimPy raises on a
        negative timeout. The first truncation for a block logs a warning; every
        one is counted in self.negative_samples. *entity* is what an
        expression-valued parameter sees as `entity`.
        """
        # ROADMAP 2.5: skip building an ExpressionScope for the common case (a
        # plain numeric distribution needs no entity/ctx/variable lookup) --
        # confirmed hot by profiling tools/bench.py's mm1_100k benchmark.
        scope = self.expression_scope(entity, extra) if getattr(dist, "needs_scope", True) else None
        # PHASE6 W4 P1, D119: every duration-bearing distribution goes through
        # this one helper, so this is the single choke point that substitutes
        # a distribution's mean for a random draw in deterministic mode.
        value = dist.sample(self.rng, scope=scope, deterministic=self.ctx.deterministic)
        unit = getattr(dist, "time_unit", "base")
        if unit != "base" and unit != self.ctx.time_unit:
            value *= base_factor("time", unit, self.ctx.units)
        if value < 0:
            if self.negative_samples == 0:
                log.warning(f"{self.name}: sampled negative duration ({value:.4g}); truncating to 0.")
            self.negative_samples += 1
            return 0.0
        return value

    def emit(self, name, **payload):
        """Publish an engine event; the animator is just a subscriber (ARCHITECTURE 10.2).

        Fast path: a headless run (the common case for a benchmark, a batch of
        replications, or a scenario sweep) has no subscriber at all, so there is
        nothing to build a payload dict for. The list is read directly: the
        `has_subscribers` property costs a call on every event of every entity.
        """
        bus = self.ctx.bus
        if not bus._subscribers:
            return
        bus.emit(name, block=self, t=self.env.now, **payload)

    def forward(self, entity, index=0):
        """Emit `entity_moved` then route *entity* to `next_process[index]` (DEVLOG #16).

        Every block used to repeat `self.emit(...)` + `self.next_process[i].self_run(...)`
        with no guard, so a terminal block raised a bare IndexError. This is the one
        place that pairs the two and fails loud with the block's name.

        A link transfer delay (ROADMAP 1.5, `link_delays[index]`) samples with
        THIS block's rng, emits `entity_moved` with the sampled `duration`, and
        moves the entity into `next_proc` only after that duration elapses
        (a tiny transfer process); with no delay, behaviour is exactly as
        before -- the entity enters `next_proc` in the same instant.
        """
        try:
            next_proc = self.next_process[index]
        except IndexError:
            raise RuntimeError(f"Block '{self.name}' has no outgoing link #{index}")
        # The target's name and the counter key, kept per output: building the
        # tuple for every move showed in profiles. Checked against the target
        # itself, so a `next_process` rewired later is simply looked up again.
        cached = self._forward_cache.get(index)
        if cached is None or cached[0] is not next_proc:
            # getattr, not next_proc.name: some tests terminate a chain with a bare
            # stub (order-recording only, no BaseProcess) that has no .name.
            target_name = getattr(next_proc, "name", None)
            cached = self._forward_cache[index] = (next_proc, target_name, (self.name, target_name))
        target_name = cached[1]
        if target_name is not None:
            # Per-link entity counter (PHASE6 W3 M2, ARCHITECTURE D113): a
            # plain dict increment, always on -- feeds "material_flow_summary"
            # and `svgcharts.sankey`. Reset at warm-up (sim_runner.reset_statistics).
            counts = self.ctx.link_flow_counts
            key = cached[2]
            counts[key] = counts.get(key, 0) + 1
        delay = self.link_delays[index] if index < len(self.link_delays) else None
        if delay is None:
            if self.ctx.bus._subscribers:         # as `emit`, before the keywords are built
                self.emit("entity_moved", entity=entity, next_index=index, target=target_name)
            if self.space_aware:                  # PHASE4 T5 (ARCHITECTURE D78)
                blocking.on_forward(entity, next_proc)
            next_proc.self_run(entity)
            return
        duration = self.sample_duration(delay, entity)
        self.emit("entity_moved", entity=entity, next_index=index, target=target_name, duration=duration)
        if self.space_aware:
            # A link with a transfer delay takes the part off the machine: it is
            # in transit, not waiting for room (ARCHITECTURE D78).
            blocking.accept(entity)
        self.env.process(self._transfer(next_proc, entity, duration))

    def send_to(self, entity, next_proc, duration=0.0, index=0, path=(), travel=None):
        """Move *entity* to *next_proc* with no drawn link (PHASE4 A2, ROUTE).

        `forward` is the link version -- it looks the target up in
        `next_process` and samples that link's own delay. This one is handed
        both, so a route jump is a normal `entity_moved` event (playback and
        the animator only ever read block NAMES, never the link list) followed
        by the same transfer process a delayed link uses.

        *path* (PHASE4 E1) is the network node list the entity follows, and
        *travel* the part of *duration* spent driving along it (the rest is
        handling at the far end). With them the animator interpolates the real
        polyline from the trace alone; without them nothing changes.
        """
        self.emit("entity_moved", entity=entity, next_index=index,
                  target=getattr(next_proc, "name", None), duration=duration,
                  path=tuple(path), travel=duration if travel is None else travel)
        if self.space_aware:
            # A route jump is a move to another station, not a wait for room.
            blocking.accept(entity)
        if duration > 0:
            self.env.process(self._transfer(next_proc, entity, duration))
        else:
            next_proc.self_run(entity)

    def _transfer(self, next_proc, entity, duration):
        """Link transfer process (ROADMAP 1.5): the entity is in transit for
        `duration`, still in `ctx.live_entities` (only Dispose removes it), then
        enters `next_proc`."""
        yield self.env.timeout(duration)
        next_proc.self_run(entity)

    @abstractmethod
    def self_process(self):
        pass

    @abstractmethod
    def self_run(self):
        """Launches the process defined in self_process().
        Designed to be used in a chain of processes: each process calls the
        next process's self_run() (self.next_process) in cascade."""
        pass

    @abstractmethod
    def get_params_dict(self):
        """Return a dict of params to be saved (and reloaded) in a simulation
        layout file. Data-structure serialization: reads only, never writes;
        used by the UI."""
        pass

    def set_params_from_dict(self, param_dict): # used to load process params when importing a layout
        for k, v in param_dict.items():
            if hasattr(self, k):
                setattr(self, k, v)

    @property
    def block(self):
        return self._block
    
    
    @property
    def resource(self):
        return self._resource
    
    @resource.setter
    def resource(self, value):
        self._resource = value
    
    @property
    def active_entity(self):
        """Return the entity (or entities) currently being processed here."""
        return self._active_entity

    @active_entity.setter
    def active_entity(self, value):
        # The two fields are read directly, once: this runs for every entity
        # and each property is a call. No resource means no capacity limit.
        resource = self._resource
        active = self._active_entity
        if resource is not None and len(active) >= resource.capacity:
            raise ValueError(f"Capacity of {resource} exceeded!")
        active.append(value)

    def remove_active_entity(self, entity):
        try:
            self._active_entity.remove(entity)
        except ValueError:
             ValueError(f"Entity {entity} not present in list!")
    
    


class Block:

    def __init__(self, position=(0,0), shape=None, description=None, color=None, type="", name=None):
        self._position = position    
        self._shape = shape
        self._description = description
        self._color = color
        self._process = None
        self._name = name

        # The type is a BLOCK_REGISTRY key (D35); whoever builds a Block from a
        # layout (GraphModel.load_from_json, document.py) checks it there, so a
        # plugin block's name works like a built-in one. Stored upper-cased.
        self._block_type = str(type).upper()
        if not self._block_type:
            raise ValueError("Error: block type must not be empty (a BLOCK_REGISTRY key).")


    @property
    def position(self):
        """Return the block's position."""
        return self._position

    @position.setter
    def position(self, value):
        self._position = value

    #@property
    #def output_size(self):
    #    """Return the output size."""
    #    return self._output_size

    #@property
    #def output_list(self):
    #    """Return the output list."""
    #    return self._output_list

    @property
    def shape(self):
        """Return the block's shape."""
        return self._shape

    @property
    def description(self):
        """Return the block's description."""
        return self._description

    @description.setter
    def description(self, value):
        self._description = value

    @property
    def color(self):
        """Return the block's color."""
        return self._color

    @property
    def block_type(self):
        """Return the block's type name (a plain string; BLOCK_REGISTRY is
        the source of truth for which names are valid, ROADMAP 2.4 B)."""
        return self._block_type
    
    @property
    def name(self):
        return self._name
    
    @name.setter
    def name(self, value):
        self._name = value

    def __repr__(self):
        return f"<Block name='{self._name}', type={self.block_type}, position={self._position}>"

