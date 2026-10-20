"""SEND (PHASE5 AB-2): the zero-time block that lets an entity reach an agent.

`to` is what an agent's `send` action takes (`agents.compile_target`): the
default "entity.agent" answers the agent whose `enter` action made the entity;
AGENT("Pop", i) names one agent; {"population", "where"?, "random"?} a whole
population, the members a condition selects, or one of them at random (drawn
from this block's stream). The message is delivered before the entity moves
on, FIFO, at the same instant (D95).
"""
from nagasim.core.agents import compile_target
from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec
from nagasim.core.blocks._common import evaluate_expression

__all__ = ["Send", "SPEC"]


class Send(BaseProcess):
    """Sends a message, with an optional payload, to agents of an agent-based model so they can react, for example a customer calling a driver."""

    def __init__(self, env, block, next_process=None, message="", to="entity.agent",
                 payload=""):
        super().__init__(env, block, next_process)
        self.message, self.to, self.payload = message, to, payload
        self._resolve = compile_target(to)

    def self_process(self, entity):
        system = self.ctx.agent_system
        if system is None:
            raise RuntimeError(f"Block '{self.name}': the model declares no populations")
        payload = evaluate_expression(self, self.payload, entity) if self.payload != "" else None
        system.send(self.message, self._resolve(system, self.expression_scope(entity)), payload)
        self.forward(entity, 0)

    def self_run(self, entity):
        self.self_process(entity)

    def get_params_dict(self):
        return {"message": self.message, "to": self.to, "payload": self.payload}

    def set_params_from_dict(self, param_dict):
        self.message = param_dict.get("message", "")
        self.to = param_dict.get("to", "entity.agent")
        self.payload = param_dict.get("payload", "")
        self._resolve = compile_target(self.to)


SPEC = BlockSpec(
    type_name="SEND", cls=Send,
    params=(
        ParamSpec("message", str, "", required=True,
                  description="Name of the message; an agent's \"message\" transition matches it.", quantity="none"),
        ParamSpec("to", object, "entity.agent",
                  description="Recipients: an expression giving an agent (entity.agent, "
                              "AGENT(\"Pop\", i)) or {\"population\": \"Pop\"[, \"where\": "
                              "\"<condition on self>\"][, \"random\": true]}.", quantity="none"),
        ParamSpec("payload", str, "",
                  description="Optional expression evaluated for the entity; the receiving "
                              "transition reads it as `payload`.", quantity="none"),
    ),
    factory=lambda env, block: Send(env, block),
    family="control",
)
