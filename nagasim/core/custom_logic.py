"""User-defined Logic classes. Plain classes, not BaseProcess subclasses (DEVLOG #2):
the LOGIC block imports this module and instantiates the named class with no
arguments, then calls execute(entity, ctx) -- or execute(entity) if that is all
the class accepts. execute() returns the index of the outgoing link to follow.
The Logic block editor appends new stubs here; do not delete this file."""


class SampleLogic:
    def execute(self, entity, ctx):
        # Return the index of the outgoing link to follow.
        return 0
