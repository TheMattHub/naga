import logging
import math

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import compiled_closure, entity_scope, evaluate_expression, validated_probs
from nagasim.core.control import DecisionRequest, PausingController

log = logging.getLogger(__name__)

__all__ = ["Decide", "SPEC", "FALLBACK_MODES"]

FALLBACK_MODES = ("first", "random", "shortest")


class Decide(BaseProcess):
    """Routing decision: sends each part, order or customer down one of several links by percentage chance, by a condition (rework, type, size) or by a policy.

    "Chance" mode draws on the block's own RNG against
    cumulative `probs`; "Condition" mode evaluates each of `conditions` in
    order and takes the first true one. Both modes take the last outgoing
    link as an ELSE branch when there is one more link than probs/conditions;
    otherwise an uncovered draw or condition fails loud rather than silently
    dropping the entity.

    "Policy" mode hands the choice to `ctx.controllers[policy]`:
    the number of actions is simply the number of outgoing links. A plain
    (synchronous) controller is called inline, in zero simulated time, exactly
    like Chance/Condition above. A `PausingController` (used by `nagasim.rl.
    SimEnv` to drive the policy from outside the run) is different: this block
    yields on a simpy event instead, so the whole simulation genuinely pauses
    until something resolves it. With no controller attached for `policy`, the
    block falls back to `fallback` ("first" / "random" / "shortest") so a
    Policy-mode model still runs and validates headless.
    """

    def __init__(self, env, block, next_process=None, conditions=None, probs=None, mode="Chance",
                policy="", observation=None, fallback="first", queue_exprs=None):
        super().__init__(env, block, next_process)
        self.conditions = list(conditions) if conditions else []
        self.probs = validated_probs(list(probs) if probs else [], block.name)
        self.mode = mode
        self.policy = policy or ""
        self.observation = list(observation) if observation else []
        self.fallback = fallback or "first"
        self.queue_exprs = list(queue_exprs) if queue_exprs else []
        # Deterministic Chance mode (PHASE6 W4 P1, D119): largest-remainder
        # allocation state, built lazily on the first entity (next_process is
        # not known yet at __init__ time -- GraphModel.link wires it after).
        self._plan = None
        self._chance_probs = None
        self._chance_assigned = None
        self._chance_seen = 0

    def _condition_plan(self):
        """(text, closure) per condition, compiled once. Keyed on the texts, not
        on the list object, so a replaced or edited `conditions` can never leave
        a stale plan; a condition that does not compile has closure None and
        raises its original error per entity via `evaluate_expression`."""
        key = tuple(self.conditions)
        plan = self._plan
        if plan is None or plan[0] != key:
            plan = self._plan = (key, [(c, compiled_closure(c)) for c in key])
        return plan[1]

    def self_process(self, entity):
        if self.mode == "Condition":
            scope = entity_scope(self, entity)
            for i, (condition, fn) in enumerate(self._condition_plan()):
                # A broken expression fails loud: validation compiles every
                # condition at load time, so reaching here means a real error.
                if (fn(scope) if fn is not None else evaluate_expression(self, condition, entity)):
                    if self.ctx.debug:
                        log.debug("Condition %s met: %s", i, condition)
                    self.forward(entity, i)
                    return

            if self.ctx.debug:
                log.debug("No match for entity %s. Routing to ELSE.", getattr(entity, 'e_type', '?'))
            if self.next_process and len(self.next_process) > len(self.conditions): #We should never get here when using probs, so no need to check
                self.forward(entity, len(self.next_process) - 1)
            else:
                log.warning("No fallback defined.")

        elif self.mode == "Chance":
            if self.ctx.deterministic:
                self.forward(entity, self._deterministic_chance_index())
                return

            r = self.rng.random()
            cumulative = 0.0
            for i, p in enumerate(self.probs):  # p is a float probability
                cumulative += float(p)
                if r < cumulative:
                    if self.ctx.debug:
                        log.debug("Routed via chance to output %s (r=%.2f, cutoff=%.2f)", i, r, cumulative)
                    self.forward(entity, i)
                    return

            # ELSE case - no warning needed
            if not (self.next_process and len(self.next_process) > len(self.probs)):
                # No ELSE link: dropping the entity here would silently lose work
                raise RuntimeError(
                    f"Decide block '{self.name}' drew {r:.4f} but its probabilities sum to "
                    f"{sum(float(p) for p in self.probs):.4f} and it has no ELSE link. "
                    f"Add an ELSE link or make the probabilities sum to 1."
                )
            self.forward(entity, len(self.next_process) - 1)

        elif self.mode == "Policy":
            # PausingController is handled by _process_policy_paused (self_run
            # routes there instead of here); reaching this branch means either
            # no controller or a synchronous one.
            self.forward(entity, self._policy_action(entity))

    def self_run(self, entity):
        if self.mode == "Policy" and isinstance(self.ctx.controllers.get(self.policy), PausingController):
            return self.env.process(self._process_policy_paused(entity))
        self.self_process(entity)

    def _observation_tuple(self, entity) -> tuple:
        return tuple(float(evaluate_expression(self, expr, entity)) for expr in self.observation)

    def _policy_action(self, entity) -> int:
        n_actions = len(self.next_process)
        controller = self.ctx.controllers.get(self.policy)
        if controller is not None:
            request = DecisionRequest(policy=self.policy, block=self.name, t=self.env.now,
                                      entity_id=entity.id, e_type=getattr(entity, "e_type", None),
                                      observation=self._observation_tuple(entity), n_actions=n_actions)
            action = int(controller.decide(request))
        else:
            action = self._fallback_action(entity, n_actions)
        if not (0 <= action < n_actions):
            raise RuntimeError(f"Decide block '{self.name}': policy '{self.policy}' returned "
                               f"action {action}, outside [0, {n_actions})")
        return action

    def _deterministic_chance_index(self) -> int:
        """Largest-remainder allocation for Chance mode in deterministic run
        mode (PHASE6 W4 P1, D119): after n entities through this block, each
        branch's count is round(n * p) by the largest-remainder method (the
        rounding that keeps every count's deviation from n * p below 1 while
        the counts sum to exactly n), computed fresh for every entity so the
        allocation is exact at every n, not just in the limit. A fractional
        tie goes to the LOWER branch index -- an arbitrary but documented and
        stable choice; the plan text does not pin one (`tests/
        test_w4_p1_oracle.py`'s tied 0.5/0.5 case checks only that both
        branches land within 1 of their target, not which one gets the extra
        unit).

        `probs` may cover fewer branches than `next_process` (an implicit
        ELSE link takes the remainder, same as the stochastic branch above);
        that ELSE probability is folded in here as one more branch.
        """
        if self._chance_probs is None:
            probs = [float(p) for p in self.probs]
            if self.next_process and len(self.next_process) > len(probs):
                probs.append(max(0.0, 1.0 - sum(probs)))
            self._chance_probs = probs
            self._chance_assigned = [0] * len(probs)
        self._chance_seen += 1
        n = self._chance_seen
        raw = [n * p for p in self._chance_probs]
        floors = [math.floor(x) for x in raw]
        remainder = n - sum(floors)
        # Largest fractional part first; a tie keeps the lower branch index
        # first (Python's sort is stable, and the key ties on -fraction).
        order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
        target = list(floors)
        for i in order[:remainder]:
            target[i] += 1
        for i, (assigned, wanted) in enumerate(zip(self._chance_assigned, target)):
            if assigned < wanted:
                self._chance_assigned[i] += 1
                return i
        return len(self._chance_assigned) - 1   # defensive: rounding leaves every branch full

    def _fallback_action(self, entity, n_actions: int) -> int:
        if self.fallback == "first":
            return 0
        if self.fallback == "random":
            return int(self.rng.integers(0, n_actions))
        if self.fallback == "shortest":
            values = [evaluate_expression(self, expr, entity) for expr in self.queue_exprs]
            if not values:
                raise RuntimeError(f"Decide block '{self.name}': fallback 'shortest' needs "
                                   f"queue_exprs")
            return int(min(range(len(values)), key=lambda i: values[i]))
        raise RuntimeError(f"Decide block '{self.name}': unknown fallback '{self.fallback}' "
                           f"(expected one of {FALLBACK_MODES})")

    def _process_policy_paused(self, entity):
        """Policy mode with a PausingController (nagasim.rl.SimEnv): really
        suspend this process on a simpy event until SimEnv.step() resolves it."""
        controller = self.ctx.controllers[self.policy]
        n_actions = len(self.next_process)
        request = DecisionRequest(policy=self.policy, block=self.name, t=self.env.now,
                                  entity_id=entity.id, e_type=getattr(entity, "e_type", None),
                                  observation=self._observation_tuple(entity), n_actions=n_actions)
        action = int((yield controller.request(self.env, request)))
        if not (0 <= action < n_actions):
            raise RuntimeError(f"Decide block '{self.name}': policy '{self.policy}' returned "
                               f"action {action}, outside [0, {n_actions})")
        self.forward(entity, action)

    def get_params_dict(self):
        return {
            "conditions": self.conditions,
            "probs": self.probs,
            "mode": self.mode,
            "policy": self.policy,
            "observation": self.observation,
            "fallback": self.fallback,
            "queue_exprs": self.queue_exprs,
        }

    def set_params_from_dict(self, param_dict):
        self.conditions = param_dict.get("conditions", [])
        self.probs = validated_probs(param_dict.get("probs", []), self.name)
        self.mode = param_dict.get("mode", "Chance")
        self.policy = param_dict.get("policy", "")
        self.observation = param_dict.get("observation", [])
        self.fallback = param_dict.get("fallback", "first")
        self.queue_exprs = param_dict.get("queue_exprs", [])


SPEC = BlockSpec(
    type_name="DECIDE", cls=Decide,
    params=(
        ParamSpec("mode", str, "Chance",
                  description="\"Chance\": branch by probability. \"Condition\": branch by "
                              "expression. \"Policy\": branch by a controller attached to `policy`.", quantity="none"),
        ParamSpec("probs", list, [], description="Cumulative branch probabilities, used when mode is \"Chance\".", quantity="ratio", kinds=("table",)),
        ParamSpec("conditions", list, [], description="Ordered list of boolean expressions, used when mode is \"Condition\".", quantity="none"),
        ParamSpec("policy", str, "", description="Decision-point name; used when mode is \"Policy\". Several DECIDE blocks may share one.", quantity="none"),
        ParamSpec("observation", list, [], description="Expressions (evaluated with the arriving entity) forming the observation handed to the controller, when mode is \"Policy\".", quantity="none"),
        ParamSpec("fallback", str, "first", description="\"first\"/\"random\"/\"shortest\": used when mode is \"Policy\" and no controller is attached for `policy`.", quantity="none"),
        ParamSpec("queue_exprs", list, [], description="One expression per outgoing link; used by fallback \"shortest\" to pick the argmin.", quantity="none"),
    ),
    factory=lambda env, block: Decide(env, block, conditions=["True"]),
    multi_output=True,
    family="control", shape="diamond",
    transparent=True,
)
