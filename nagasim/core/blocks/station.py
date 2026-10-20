"""STATION: a named entry point a ROUTE block can send an entity to (PHASE4 A2).

A station is just a block, and its NAME is the station's name (ARCHITECTURE
D51): no second namespace, no `$` rule of its own -- a STATION inside a
submodel instance is flattened to `cell_1/Mill` exactly like every other block,
which is the name its sequence step must use.

The optional `station` param is the one exception (PHASE4 B6, ARCHITECTURE
D59): it gives the block a user-facing ALIAS, so a station that lives inside an
asset can be called "Mill 1" instead of `mill_1/station`, with the alias
normally coming from a structural argument (`{"$arg": "station_name"}`). The
block answers to BOTH names, `entity.station` is stamped with the alias, and
two blocks claiming one name is `E_DUPLICATE_STATION`. Empty (the default) is
exactly D51's behaviour.

At run time it does two things: it stamps `entity.station` (so a PROCESS at
this station can read `TABLE("ProcessTime", entity.e_type, entity.station)`,
and so the next ROUTE knows where the entity is travelling from) and forwards
along its own outgoing link. Entities reach it by route, by an ordinary drawn
link, or both.
"""
import logging

from nagasim.core.base_process import BaseProcess
from nagasim.core.block_spec import BlockSpec, ParamSpec

log = logging.getLogger(__name__)

__all__ = ["Station", "SPEC"]


class Station(BaseProcess):
    """Named stop on a job-shop or multi-step routing where a ROUTE block delivers parts, orders or jobs and sets their current location.

    Named entry point of a routing. Entities arrive here from a
    ROUTE block whose sequence step names this block, or along an ordinary
    link; either way `entity.station` becomes this block's name and the entity
    continues along the station's single outgoing link."""

    def __init__(self, env, block, next_process=None):
        super().__init__(env, block, next_process)
        self.alias = ""
        self.node = ""          # PHASE4 E1: where this station is on the network
        self.tending = {}       # D169: how an arm fleet's cobot loads and unloads it
        # Registered by name so ROUTE resolves a step's station in one lookup
        # (the same self-registration Seize does with ctx.seize_blocks).
        env.ctx.stations[self.name] = self

    @property
    def station_name(self) -> str:
        return self.alias or self.name

    def self_process(self, entity):
        entity.station = self.station_name
        if self.ctx.debug:
            log.debug("Entity %s arrived at station '%s' at time %s.", entity.id, self.station_name, self.env.now)
        self.forward(entity, 0)

    def self_run(self, entity):
        # No yield anywhere: a station is an instantaneous marker, so it costs
        # no SimPy process (same shape as ASSIGN).
        self.self_process(entity)

    def get_params_dict(self):
        return {"station": self.alias, "node": self.node, "tending": dict(self.tending)}

    def set_params_from_dict(self, param_dict):
        self.node = str(param_dict.get("node", "") or "").strip()
        self.tending = dict(param_dict.get("tending") or {})
        self.alias = str(param_dict.get("station", "") or "").strip()
        if self.alias:
            # Both names resolve here: a routing written against the flattened
            # block name keeps working after an alias is added.
            self.ctx.stations[self.alias] = self


SPEC = BlockSpec(
    type_name="STATION", cls=Station,
    params=(
        ParamSpec("station", str, "",
                  description="Optional station name this block answers to, shown as "
                              "`entity.station` and usable in a sequence step. Empty: the block's "
                              "own name is the station name.", quantity="none"),
        ParamSpec("node", str, "",
                  description="Optional path-network node this station sits on. With "
                              "one, travel to and from this station is the shortest path along "
                              "the network and `DISTANCE(\"...\", \"...\")` can name it.", quantity="none"),
        ParamSpec("tending", dict, {},
                  description="Machine tending by a fleet with an `arm`: {\"place\": "
                              "[segments], \"pick\": [segments], \"time_unit\": \"seconds\", "
                              "\"hrc\": \"none\", \"zone\": {}, \"cobot\": \"leave\"|\"stay\"}. "
                              "Segments, hrc and zone are a MOTION block's, timed with the "
                              "fleet's arm profile; a missing cycle uses the fleet's flat "
                              "unload/load time. The machine keeps a finished part until the "
                              "cobot has picked it, and the cobot holds a part until the "
                              "machine accepts it; \"stay\" keeps the cobot at the station "
                              "until the part moves on. Empty: an ordinary station.",
                  quantity="none"),
    ),
    factory=lambda env, block: Station(env, block),
    family="move", shape="pill",
    transparent=True,
)
