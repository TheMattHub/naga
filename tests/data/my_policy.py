"""A caller's own decision policy for tests/test_caller_policies.py (D1120): the Policy-mode
DECIDE `route` sends every entity down link 1 (the PROCESS branch), and each call is counted."""
from nagasim.core.control import Controller

CALLS = []


class ToProcess(Controller):
    def decide(self, request):
        CALLS.append(request.block)
        return 1


CONTROLLERS = {"route": ToProcess()}
OTHER = {"route": ToProcess()}
NOT_A_DICT = 3
