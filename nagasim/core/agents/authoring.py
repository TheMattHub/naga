"""Agent authoring records (PHASE5 AB-1): `AgentType`, `Attribute`, `State`,
`Transition`, `Action`, `Population`, `Space`, `Tick`, `Network`, whose
`to_dict` is the JSON, and `add(layout, *records)`."""
from __future__ import annotations

from nagasim.core.agents.common import TRIGGERS
from nagasim.core.sd import _Record


def _plain(items) -> list:
    """Nested records as their JSON, so `to_dict` and `==` see plain data."""
    return [item.to_dict() if hasattr(item, "to_dict") else item for item in items]


class Attribute(_Record):
    """A per-agent column, numeric (`kind="number"`, default) or categorical
    (`kind="category"`, PHASE6 A2: a closed `categories` list, stored as the
    category's index -- exact in the same float column a numeric attribute
    uses, so no second dtype-per-attribute machinery is needed anywhere a
    column is grown, padded or table-initialised). `init` is an expression
    evaluated per agent at run start with the agent's own stream
    (`UNIF(0.5, 1.5)`, or a quoted category name), or (C3) `{"column":
    "<col>"}` reading row i of the population's own DATA TABLE (the table its
    "count" names) for agent i -- no draw, no expression."""

    _FIELDS = (("name", "name", None), ("init", "init", "0"), ("kind", "kind", "number"),
              ("categories", "categories", None))

    def __init__(self, name: str, init="0", kind: str = "number", categories=None, **fields):
        self.name = name
        self.init = init if isinstance(init, dict) else str(init)
        self.kind = kind
        self.categories = list(categories) if categories is not None else None
        self.extra = fields


class Action(_Record):
    """One action, held as its JSON: `Action.set(target, expr)`,
    `Action.send(message, to, payload)`, `Action.enter(block, e_type)`,
    `Action.transfer(level, amount, mode)`, `Action.signal(name)`,
    `Action.move_to(target)` (AB-3: a point `[x, y]`, a node name, or an
    expression giving a point/node/agent -- `AGENT("Pop", i)`)."""

    def __init__(self, **fields):
        self.extra = fields

    @classmethod
    def set(cls, target: str, expr) -> "Action":
        return cls(target=target, expr=str(expr))

    @classmethod
    def send(cls, message: str, to, payload=None) -> "Action":
        return cls(send=message, to=to, **({} if payload is None else {"payload": str(payload)}))

    @classmethod
    def enter(cls, block: str, e_type: str | None = None) -> "Action":
        return cls(enter=block, **({} if e_type is None else {"e_type": e_type}))

    @classmethod
    def transfer(cls, level: str, amount, mode: str = "remove") -> "Action":
        return cls(transfer=level, amount=str(amount), mode=mode)

    @classmethod
    def signal(cls, name: str) -> "Action":
        return cls(signal=name)

    @classmethod
    def move_to(cls, target) -> "Action":
        """*target*: `[x, y]` (a literal point) or an expression string --
        `"'dock'"`/`"self.home"` for a node, `"[x, y]"` for a computed point,
        `'AGENT("Pop", i)'` for another agent."""
        literal = (isinstance(target, (list, tuple)) and len(target) == 2
                  and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                          for v in target))
        return cls(move_to=[float(target[0]), float(target[1])] if literal else str(target))

    @classmethod
    def spawn(cls, population: str, count=None, state: str | None = None,
              init: dict | None = None) -> "Action":
        """C5: *count* (default 1) new agents of *population*, in *state* (default
        its type's initial state); *init* `{attr: expr}` is evaluated in the
        SPAWNER's scope, after the newborn's own attribute inits."""
        fields = {"spawn": population}
        if count is not None:
            fields["count"] = count if isinstance(count, int) else str(count)
        if state is not None:
            fields["state"] = state
        if init:
            fields["init"] = {k: v if isinstance(v, (int, float)) else str(v)
                              for k, v in init.items()}
        return cls(**fields)

    @classmethod
    def remove(cls, target="self") -> "Action":
        """C5: remove *target* -- "self" or any send target (an index,
        `AGENT("Pop", i)`, `{"population", "where"?, "random"?}`)."""
        return cls(remove=target)

    @classmethod
    def step(cls, dx, dy) -> "Action":
        """A4: move by [dx, dy] cells (integers or expressions), instantly."""
        return cls(step=[v if isinstance(v, int) else str(v) for v in (dx, dy)])

    @classmethod
    def move_random(cls, free: bool = False) -> "Action":
        """A4: to a uniformly drawn neighbourhood cell (*free*: an empty one).
        A patch write is a set: `Action.set("patch.food", expr)`."""
        return cls(move_random="free" if free else "any")

    @classmethod
    def link(cls, target) -> "Action":
        """A5: link self to *target* (any send target of its own population)."""
        return cls(link=target)

    @classmethod
    def unlink(cls, target) -> "Action":
        """A5: drop self's links to *target* ("links": every one)."""
        return cls(unlink=target)

    @classmethod
    def forward(cls, distance) -> "Action":
        """A6: move *distance* along self.heading -- an instant jump inside
        on_tick, a timed segment (at `speed`, "arrived" on arrival) outside
        it; needs a continuous space."""
        return cls(forward=str(distance))

    @classmethod
    def turn(cls, degrees) -> "Action":
        """A6: rotate self.heading by *degrees*, left positive, instant."""
        return cls(turn=str(degrees))

    @classmethod
    def face(cls, target) -> "Action":
        """A6: point self.heading at a point `[x, y]`, an expression giving
        one, or an agent reference (`AGENT("Pop", i)`), instant."""
        literal = (isinstance(target, (list, tuple)) and len(target) == 2
                  and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                          for v in target))
        return cls(face=[float(target[0]), float(target[1])] if literal else str(target))

    @classmethod
    def random_walk(cls, step, turn_sd=0) -> "Action":
        """A6: turn by a normal draw (mean 0, sd *turn_sd*) from self's own
        stream, then forward *step* -- NetLogo's own random walk."""
        return cls(random_walk={"step": str(step), "turn_sd": str(turn_sd)})

    @classmethod
    def python(cls, function: str) -> "Action":
        """A7: call function(agent, api) from the type's `python` code."""
        return cls(python=function)


class Network(_Record):
    """A population's initial links (PHASE6 A5, D103): `Network("random",
    p=0.1)` or `mean_degree=4`, `Network("small_world", k=4, beta=0.1)`,
    `Network("scale_free", m=2)`, `Network("table", table="Edges")` (columns
    "from", "to": agent indices) or `Network("none")`; `directed=True`
    (random/table only) and `link_on_spawn=True` pass through."""

    _FIELDS = (("kind", "kind", None),)

    def __init__(self, kind: str = "none", **fields):
        self.kind, self.extra = kind, fields


class State(_Record):
    """A statechart state with its entry and exit actions; `color`/`diagram`
    pass through for the editor, `parent`/`initial` (PHASE6 K1) nest it."""

    _FIELDS = (("name", "name", None), ("entry", "entry", []), ("exit", "exit", []))

    def __init__(self, name: str, entry=(), exit=(), **fields):
        self.name, self.entry, self.exit, self.extra = name, _plain(entry), _plain(exit), fields


class Transition:
    """`{"from", "to", "<trigger>": value, "guard"?, "actions"?, "scan"?}` with
    exactly one trigger: "timeout" (a duration), "rate" (per agent per time
    unit), "condition" (a boolean), "message" (a name, "" for any; its
    guard and actions read `payload`) or "tick" (PHASE6 A1: a boolean checked
    at each of the population's ticks, "" for every tick). `scan=<interval>` (a keyword, kept in
    `extra`) re-checks a rate or condition on the grid k * scan."""

    def __init__(self, source: str, target: str, trigger: str, value="", guard: str = "",
                 actions=(), **fields):
        self.source, self.target, self.trigger, self.value = source, target, trigger, value
        self.guard, self.actions, self.extra = guard, _plain(actions), fields

    def to_dict(self) -> dict:
        out = {"from": self.source, "to": self.target}
        if self.trigger:
            out[self.trigger] = self.value
        if self.guard:
            out["guard"] = self.guard
        if self.actions:
            out["actions"] = self.actions
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Transition":
        data = dict(data)
        trigger = next((key for key in TRIGGERS if key in data), "")
        return cls(data.pop("from", ""), data.pop("to", ""), trigger,
                   data.pop(trigger, "") if trigger else "", data.pop("guard", ""),
                   data.pop("actions", ()), **data)

    __eq__ = _Record.__eq__
    __repr__ = _Record.__repr__


class Space(_Record):
    """An agent type's `space` (AB-3): `"continuous"` (`bounds`: `[x0, y0, x1,
    y1]`, `edge`: PHASE6 A6's "none" (default)/"clamp"/"wrap"/"bounce" for
    forward/random_walk) or `"network"` (the model's own `network` section; no
    bounds), or (PHASE6 A4) `"grid"`: `Space("grid", size=[w, h], torus=True,
    neighbourhood="von_neumann", patches=[{"name": "food", "init": 1,
    "diffuse": 0.5}], patch_tick=1)` -- the other keys pass through."""

    _FIELDS = (("kind", "kind", None), ("bounds", "bounds", None))

    def __init__(self, kind: str, bounds=None, **fields):
        self.kind = kind
        self.bounds = [float(v) for v in bounds] if bounds is not None else None
        self.extra = fields


class Tick(_Record):
    """A fixed-step schedule (PHASE6 A1): ticks at `phase + k * dt` (k an
    integer, strictly after the run start), `activation` one of ACTIVATIONS."""

    _FIELDS = (("dt", "dt", None), ("activation", "activation", "synchronous"),
               ("phase", "phase", 0))

    def __init__(self, dt=None, activation: str = "synchronous", phase=0, **fields):
        self.dt, self.activation, self.phase, self.extra = dt, activation, phase, fields


class AgentType(_Record):
    """An agent type (`agent_types` section): parameters are the expression
    scope's locals (the submodel mechanism), states may be given by name.
    `space` (AB-3) is a `Space`/dict or None; `speed` is an expression (self,
    parameters, constants) a `move_to` evaluates once per call. `tick` (A1)
    is a `Tick`/dict or None; `on_tick` the actions each agent runs per tick."""

    SECTION = "agent_types"
    _FIELDS = (("name", "name", None), ("parameters", "parameters", {}),
               ("attributes", "attributes", []), ("states", "states", []),
               ("transitions", "transitions", []), ("initial", "initial_state", ""),
               ("space", "space", None), ("speed", "speed", "1"), ("tick", "tick", None),
               ("on_tick", "on_tick", []))

    def __init__(self, name: str, parameters=None, attributes=(), states=(), transitions=(),
                 initial: str = "", space=None, speed="1", tick=None, on_tick=(), **fields):
        self.name, self.parameters = name, dict(parameters or {})
        self.attributes = _plain(Attribute(a) if isinstance(a, str) else a for a in attributes)
        self.states = _plain(State(s) if isinstance(s, str) else s for s in states)
        self.transitions = _plain(transitions)
        self.initial = initial or (self.states[0]["name"] if self.states else "")
        self.space = space.to_dict() if hasattr(space, "to_dict") else space
        self.speed, self.extra = str(speed), fields
        self.tick = tick.to_dict() if hasattr(tick, "to_dict") else tick
        self.on_tick = _plain(on_tick)


class Population(_Record):
    """`{"name", "type", "count", "initial": [{"state", "count"}]}`: the first
    agents by index start in the listed states, the rest in the type's
    initial state. `count` (C3) may also be `{"table": "<name>"}` -- the
    number of rows of that model table, one agent per row, row i for agent i
    (what an attribute's `{"column": ...}` init then reads) -- or
    `{"table": "<name>", "column": "<col>"}`, the SUM of that column (a group
    table: no row-per-agent attribute init, since a row is no longer one
    agent). `placement` (AB-3, only meaningful when the type declares a
    space): `{"kind": "random"|"table", "bounds"|"points"|"nodes": ...}`,
    default `{"kind": "random"}` (uniform over the space's own bounds, or a
    uniform random network node). `max_count` (C5): the most agents alive at
    once when `spawn` adds some; None means the resolved `count`. `tick`
    (A1): a `Tick`/dict whose keys override the type's own `tick`."""

    SECTION = "populations"

    def __init__(self, name: str, type: str, count: int = 0, initial_states=None,
                 placement=None, max_count: int | None = None, tick=None, **fields):
        self.name, self.type, self.count = name, type, count
        self.initial_states = dict(initial_states or {})
        self.placement = placement.to_dict() if hasattr(placement, "to_dict") else placement
        self.max_count = max_count
        self.tick = tick.to_dict() if hasattr(tick, "to_dict") else tick
        if hasattr(fields.get("network"), "to_dict"):       # A5: a Network record
            fields["network"] = fields["network"].to_dict()
        self.extra = fields

    def to_dict(self) -> dict:
        out = {"name": self.name, "type": self.type, "count": self.count}
        if self.initial_states:
            out["initial"] = [{"state": s, "count": n} for s, n in self.initial_states.items()]
        if self.placement:
            out["placement"] = self.placement
        if self.max_count is not None:
            out["max_count"] = self.max_count
        if self.tick is not None:
            out["tick"] = self.tick
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "Population":
        data = dict(data)
        initial = {e.get("state"): e.get("count", 0) for e in data.pop("initial", None) or []}
        placement = data.pop("placement", None)
        return cls(data.pop("name", ""), data.pop("type", ""), data.pop("count", 0), initial,
                   placement, data.pop("max_count", None), data.pop("tick", None), **data)


def add(layout: dict, *records) -> dict:
    """Append *records* to their sections of *layout* (in place) and return it."""
    for record in records:
        layout.setdefault(record.SECTION, []).append(record.to_dict())
    return layout
