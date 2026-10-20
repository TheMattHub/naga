import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.quantities import param, to_base
from nagasim.core.blocks._common import evaluate_expression
from nagasim.core.expressions import compile_expression, functions_with

log = logging.getLogger(__name__)

__all__ = ["Hold", "SPEC"]

# Event-driven Condition mode (PHASE5 O4). Calls whose value cannot change
# while nothing happens in the model:
_STEADY_CALLS = frozenset(functions_with("steady"))
# Named lookups whose every change the engine announces (`ctx.touch`), and the
# key kind it announces them under. LEVEL is announced at each re-evaluation
# but also drifts in between, so it keeps the scan as well. VAR and LEVEL stay
# spelled out: which lookups `ctx.touch` announces is an engine fact, not a
# function trait.
_WATCHED_CALLS = {**dict.fromkeys(functions_with("resource_state"), "res"),
                  **dict.fromkeys(functions_with("store_state"), "store"),
                  "VAR": "var", "LEVEL": "level"}
# Bare names that never change while an entity waits.
_STEADY_NAMES = frozenset({"entity", "true", "false", "HORIZON"})


class Hold(BaseProcess):
    """Holds entities waiting in a queue until a named signal fires, a condition becomes true (a buffer has room, a machine is idle), or for good.

    Infinite mode never releases: the entity stays held in the store for the
    rest of the run. That is the documented semantics, not a leak.
    """

    def __init__(self, env, block, store, next_process=None, mode="Signal",
                 conditions=None, signal_name=None, scan_interval=1.0):
        super().__init__(env, block, next_process)
        self.store = store
        self.mode = mode
        self.conditions = list(conditions) if conditions else [] # all must be true (mode == "Condition")
        self.signal_name = signal_name
        self.scan_interval = scan_interval # re-check interval for Condition mode
        self.waiting_entities = []
        self._wake = None       # (keys, polled), worked out on the first wait

        # Safety checks
        if self.mode == "Signal" and not self.signal_name:
            raise ValueError("Mode 'Signal' requires a signal_name.")
        elif self.mode == "Condition" and not self.conditions:
            raise ValueError("Mode 'Condition' requires at least one condition.")

        # Signal list, also created by the Signal block
        if self.mode == "Signal" and self.signal_name:
            if self.signal_name not in env.ctx.signals:
                env.ctx.signals[self.signal_name] = self.env.event()

        log.debug("Hold initialized with mode: %s, signal: %r", self.mode, self.signal_name)

    def self_process(self, entity):
        if self.ctx.debug:
            log.debug("Entity %s enters Hold at time %s.", entity.id, self.env.now)

        self.active_entity = entity
        self.waiting_entities.append(entity)
        yield self.store.put(entity)  # DEVLOG #30: route through the store like every other store-using block

        if self.mode == "Infinite":  # wait indefinitely -- entity stays held, by design
            return

        elif self.mode == "Signal": # wait for a signal
            if self.signal_name not in self.ctx.signals:
                self.ctx.signals[self.signal_name] = self.env.event()

            current_event = self.ctx.signals[self.signal_name]
            if self.ctx.debug:
                log.debug("Entity %s awaits signal '%s' at time %s.", entity.id, self.signal_name, self.env.now)
            yield current_event

        elif self.mode == "Condition": # wait until every condition expression is true
            # Event-driven (PHASE5 O4): re-evaluated the instant anything the
            # conditions read is written; `scan_interval` only for what has no
            # event (TNOW, TS(), RAND(), a level drifting between events).
            while not all(evaluate_expression(self, c, entity) for c in self.conditions):
                yield self._next_change()

        # Release logic
        yield self.store.get(lambda e: e is entity)
        self.waiting_entities.remove(entity)
        self.remove_active_entity(entity)

        if self.ctx.debug:
            log.debug("Entity %s released from Hold at time %s.", entity.id, self.env.now)

        self.forward(entity, 0)

    def _wake_keys(self):
        """(keys, polled): the `ctx.touch` keys the conditions read, and
        whether they also read something no event announces."""
        keys, polled = set(), False
        for text in self.conditions:
            expr = compile_expression(text)
            for name in expr.names:
                if name in ("TNOW", "now"):
                    polled = True
                elif name not in _STEADY_NAMES and name not in self.locals:
                    keys.add(("var", name))
            for func in expr.calls - _STEADY_CALLS:
                kind = _WATCHED_CALLS.get(func)
                if kind is None or func in expr.computed_refs:
                    polled = True       # a clock, a sample, a series, a computed name
                    continue
                for name in expr.refs.get(func, ()):
                    if kind == "store":
                        # NS() accepts a store's own name or its block's (_store).
                        keys.update(("store", n) for n in (name, f"{name}_hold", f"{name}_batch"))
                    else:
                        keys.add((kind, name))
                polled = polled or kind == "level"
        return frozenset(keys), polled

    def _next_change(self):
        """The event that fires when the conditions may have changed truth."""
        if self._wake is None:
            self._wake = self._wake_keys()
        keys, polled = self._wake
        watched = self.ctx.watched
        events = []
        for key in keys:
            if key not in watched:
                watched[key] = self.env.event()
            events.append(watched[key])
        if polled:
            events.append(self.env.timeout(self.scan_interval))
        # Nothing to wait for (the conditions read only the entity and
        # constants): false now is false for ever -- held, like "Infinite".
        return self.env.any_of(events) if events else self.env.event()

    def self_run(self, entity):
        return self.env.process(self.self_process(entity))

    def get_params_dict(self):
        return {
            "mode": self.mode,
            "signal_name": self.signal_name,
            "conditions": self.conditions,
            "scan_interval": self.scan_interval,
        }

    def set_params_from_dict(self, param_dict):
        self.mode = param_dict.get("mode", "Signal")
        self.signal_name = param_dict.get("signal_name", None)
        self.conditions = param_dict.get("conditions", [])
        self.scan_interval = to_base(param(SPEC, "scan_interval"), param_dict, self.ctx.units,
                                     SPEC.params)
        self._wake = None


SPEC = BlockSpec(
    type_name="HOLD", cls=Hold,
    params=(
        ParamSpec("mode", str, "Signal", description="\"Signal\": wait for a named signal. \"Condition\": wait until every condition is true, re-checked whenever something it reads changes. \"Infinite\": never release.", quantity="none"),
        ParamSpec("signal_name", str, "", description="Name of the signal to wait for, when mode is \"Signal\".", quantity="none"),
        ParamSpec("conditions", list, [], description="Boolean expressions that must all be true to release, when mode is \"Condition\".", quantity="none"),
        ParamSpec("scan_interval", float, 1.0, description="Fallback re-check interval, when mode is \"Condition\": used only when a condition reads something no event announces (TNOW, TS(), RAND(), a level between events). Variables, NQ/NR/MR/IDLE, NS and threshold writes wake it at once.", quantity="time", min=0),
        ParamSpec("scan_interval_unit", str, "base", quantity="none", kinds=("choice",),
                  description="Unit of `scan_interval`: \"base\" (the model's) or one of its quantity's units."),
    ),
    factory=lambda env, block: Hold(env, block, store=None, mode="Signal", conditions=None, signal_name="Default"),
    family="control",
    holding=True,
)
