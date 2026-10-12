"""PHASE6 K3: the "Machine agent with repair" template
(`nagasim/models/agents_machine_repair.json`) is the worked example for
composite/history states (K1, ARCHITECTURE 5.25, D136-D139). Its own
validation, run and statistics are already covered by every bundled example's
generic checks in `tests/test_api.py` -- this file adds only the one
template-specific claim: deep history on the repair transition really
resumes the recorded leaf."""
import json
from pathlib import Path

from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

MODEL = Path(__file__).resolve().parents[1] / "nagasim" / "models" / "agents_machine_repair.json"


def test_deep_history_resumes_busy_not_working_initial():
    """The wear condition ("Working" -> "Broken" `condition: "self.wear >= 20"`)
    can only turn true from within "Busy": `wear` is incremented ONLY by
    Busy's own entry action, so the instant it reaches 20 the agent is
    necessarily in Busy, not Idle. The random-breakdown rate is zeroed here so
    it cannot also send the machine to Broken from Idle, which would make
    the resumed leaf ambiguous. With that isolated, `history: "deep"` on the
    repair transition must resume Busy every single time -- never Working's
    `initial` ("Idle"), which is what a repair with no history (or "shallow"
    on a plain composite) would give instead."""
    layout = json.loads(MODEL.read_text())
    layout["populations"][0]["count"] = 1
    for transition in layout["agent_types"][0]["transitions"]:
        if transition.get("from") == "Working" and "rate" in transition:
            transition["rate"] = "0"

    run_len = 4000.0
    env = initialize_sim_env(seed=1)
    env.ctx.sim_horizon = run_len
    prepare_model(env, layout)
    env.ctx.agent_system.start(run_len)
    system = env.ctx.agent_system
    pop = system.by_name["Machines"]

    def leaf():
        return pop.type.states[int(pop.state[0])]

    resumes_seen = 0
    was_broken = False
    t = 0.0
    while t < run_len:
        t += 0.5
        step_until(env, t)
        broken_now = leaf() == "Broken"
        if was_broken and not broken_now:
            assert leaf() == "Busy"
            resumes_seen += 1
        was_broken = broken_now

    assert resumes_seen >= 1, "no breakdown/repair cycle observed in the horizon"
