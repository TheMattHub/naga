Use LOGIC when a routing rule is too complex for [DECIDE](decide.md): a rule that needs loops, several attributes, a lookup or state kept between entities. You write the rule as a small Python class, and the block uses the number it returns as the index of the outgoing link.

## Behaviour and rules

The class needs an `execute(self, entity, ctx)` method that returns the link index. A method `execute(self, entity)` without `ctx` is also accepted. The model file only names the class in `logic_class`. The class is looked up first in the module the caller passes with `run_model(logic=...)` or `naga run --logic`, then in the default module `custom_logic`.

A model file cannot name a module or a path, so a file alone cannot make the engine run Python. The caller chooses the module. A class that neither module has stops the run with an error.

A new instance of the class is created for every entity. Anything the rule must remember between entities therefore belongs in model variables or on the context, not in the instance. The returned index must be a valid link of the block, or the run stops with an error. The block takes no time.
