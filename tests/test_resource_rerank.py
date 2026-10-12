"""Resource "rerank" (H5, D131): a ranked discipline (LowestValueFirst/
HighestValueFirst) computes each request's rank ONCE when it joins the queue
by default; "rerank": true instead re-evaluates it, in the same scope, just
before every grant (`LoggedSimpyResource._trigger_put` via `_RerankResource`).
"""
import json

from nagasim.core.base_process import Block
from nagasim.core.blocks import Release, Seize
from nagasim.core.resource_manager import ResourceManager
from nagasim.core.schema import validate_model_structured
from nagasim.core.sim_classes import Entity


class _Recorder:
    """Terminal stub recording the order entities reach it (test_resources.py's
    pattern)."""

    def __init__(self):
        self.order = []
        self.block = Block(position=(9, 9), type="DISPOSE", name="rec")
        self.next_process = []

    def self_run(self, entity):
        self.order.append(entity.tag)


def _lane(env, index, recorder, delay=0):
    seize = Seize(env, Block(position=(index, 0), type="SEIZE", name=f"seize_{index}"))
    release = Release(env, Block(position=(index, 1), type="RELEASE", name=f"release_{index}"))
    seize.set_params_from_dict({"resource_name": "R", "priority": 0, "quantity": 1,
                                "distribution": "Constant", "dist_params": {"value": delay}})
    release.set_params_from_dict({"resource_name": "R",
                                 "distribution": "Constant", "dist_params": {"value": 0}})
    seize.next_process = [release]
    release.next_process = [recorder]
    return seize


def _feed(env, seize, entity, at):
    def _run():
        yield env.timeout(at)
        seize.self_run(entity)
    env.process(_run())


def _run_scenario(env, rerank):
    """Occupy the single server until t=10; queue three entities at t=1,2,3
    with equal rank (value=1, so FIFO ties break the join order), then at t=5
    raise the LAST one's value above the other two's -- a change that only a
    reranking resource acts on."""
    env.ctx.resource_manager.register("R", capacity=1, discipline="HighestValueFirst",
                                      ranking_expr="entity.value", rerank=rerank)
    recorder = _Recorder()

    occupier = Entity()
    occupier.tag, occupier.value = "occupier", 0
    _feed(env, _lane(env, 0, recorder, delay=10), occupier, 0)

    entities = []
    for index in range(3):
        entity = Entity()
        entity.tag, entity.value = index, 1
        entities.append(entity)
        _feed(env, _lane(env, index + 1, recorder), entity, index + 1)

    def _deteriorate():
        yield env.timeout(5)
        entities[2].value = 100      # the third to join now outranks the other two

    env.process(_deteriorate())
    env.run()
    return [tag for tag in recorder.order if tag != "occupier"]


def test_rerank_false_keeps_the_rank_computed_at_join(env):
    assert _run_scenario(env, rerank=False) == [0, 1, 2]


def test_rerank_true_reacts_to_the_later_change():
    from nagasim.core.sim_runner import initialize_sim_env
    assert _run_scenario(initialize_sim_env(seed=0), rerank=True) == [2, 0, 1]


def test_rerank_round_trips_through_register_and_export(env):
    manager = ResourceManager(env)
    manager.register("Doctor", capacity=1, discipline="HighestValueFirst",
                     ranking_expr="entity.value", rerank=True)
    manager.register("Nurse", capacity=1, discipline="FIFO")

    exported = {r["name"]: r for r in manager.export_to_json()}
    assert exported["Doctor"]["rerank"] is True
    assert "rerank" not in exported["Nurse"]      # default False stays implicit

    reloaded = ResourceManager(env)
    reloaded.load_from_json(list(exported.values()))
    assert reloaded.get("Doctor").rerank is True
    assert reloaded.get("Nurse").rerank is False


def test_rerank_has_no_effect_declared_on_an_unranked_discipline(env):
    """A FIFO resource ignores "rerank" (its rank is always 0) rather than
    silently reordering by request time; schema.py flags the combination."""
    resource = env.ctx.resource_manager.register("R", capacity=1, discipline="FIFO", rerank=True)
    assert resource.rerank is False

    layout = {"resources": [{"name": "R", "capacity": 1, "discipline": "FIFO", "rerank": True}],
              "blocks": [], "links": []}
    problems = validate_model_structured(layout)
    assert any(p.code == "W_RERANK_UNRANKED" for p in problems)
