"""A caller's own LOGIC module for tests/test_logic_module.py (D914)."""


class RouteBig:
    """Every entity takes outgoing link 1 (the default SampleLogic takes link 0)."""

    def execute(self, entity, ctx):
        return 1
