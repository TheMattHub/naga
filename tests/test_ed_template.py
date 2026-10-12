"""ed_patient_agents.json (H3): a patient is both a DES entity and a Patient
agent (CREATE population, D129/D130). The doctor's queue discipline
(HighestValueFirst, ranking_expr "entity.agent.acuity") ranks the queue by
the agent's live attribute, so a patient who has already deteriorated when it
reaches the doctor is served ahead of one still Stable. `Doctor` also sets
"rerank": true (H5, D131), so a patient's rank is re-checked against its
CURRENT acuity right before every grant, not fixed at the instant it joined.
"""
from pathlib import Path

from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_runner import initialize_sim_env, prepare_model, step_until

MODEL_PATH = Path(__file__).resolve().parents[1] / "nagasim" / "models" / "ed_patient_agents.json"


def test_ed_patient_agents_validates_clean():
    import json

    layout = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    problems = validate_model_structured(layout)
    assert not [p for p in problems if p.severity == "error"]


def test_ed_patient_agents_treats_patients_and_prioritises_acuity():
    """A deterministic-seed run checking the "rerank" invariant directly: at
    every "entity_seized" grant of `Doctor`, the patient just granted must
    have a CURRENT acuity at least as high as every patient still waiting in
    `Doctor`'s queue right after it (the acuities `rerank` just re-sorted on).
    A rank-once queue could not promise this -- a patient who deteriorated
    after joining would stay behind one that joined higher but never worsened.
    """
    violations = []

    def on_event(name, block=None, t=None, entity=None, **payload):
        if name != "entity_seized" or payload.get("resource") != "Doctor":
            return
        pop, i = env.ctx.agent_system.deref(entity.agent)
        granted_acuity = pop.attrs["acuity"][i]
        doctor = env.ctx.resource_manager.get("Doctor")
        for req in doctor.queue:
            scope = getattr(req, "rerank_scope", None)
            if scope is None:      # a blocker, or a request this resource never scoped
                continue
            wpop, wi = env.ctx.agent_system.deref(scope.entity.agent)
            if wpop.attrs["acuity"][wi] > granted_acuity:
                violations.append((entity.id, granted_acuity, scope.entity.id, wpop.attrs["acuity"][wi]))

    env = initialize_sim_env(seed=0)
    env.ctx.bus.subscribe(on_event)
    model = prepare_model(env, str(MODEL_PATH))
    env.ctx.agent_system.start(200.0)
    for start in model.start_blocks():
        start.self_run()
    step_until(env, 200.0)

    # Treats patients: some completed the whole flow (Discharged agents removed
    # at DISPOSE, so births > still-live).
    patients = env.ctx.agent_system.by_name["Patients"]
    assert patients.births > patients.live > 0
    assert violations == []

    # H2 (D130): every live agent still has its entity in the system and vice
    # versa -- remove_agent kept the two counts in lockstep.
    assert len(env.ctx.live_entities) == patients.live
