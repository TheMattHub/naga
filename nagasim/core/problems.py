"""Structured validation problems: the machine-readable half of the model
contract (ROADMAP 3.1 item 1).

A leaf module beside `block_spec.py`/`distribution_spec.py` (no imports from
`schema`/`schedules`), so both `core/schedules.py` and `core/schema/` can
build Problems without a circular import; `core/schema/` re-exports `Problem`,
`PROBLEM_CODES` and `problem`, so `schema.Problem` is the public name.

Every check in the codebase produces a Problem with a STABLE `code`. The code
is the contract an agent or an editor may branch on; the `message` is the
human string the string-returning validators have always emitted and must not
change. `path` is a JSON-pointer-like location in the layout
(`/blocks/3/params/dist_params/rate`), `block` names the block when there is
one, and `hint` is a concrete next action ("add a DISPOSE block or link this
block to one").

Severity follows the prefix: `E_` is an error, `W_` a warning (advisory; the
string form of a warning has always started with "warning: " and still does).
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Problem", "PROBLEM_CODES", "problem"]


@dataclass(frozen=True)
class Problem:
    code: str                 # a key of PROBLEM_CODES
    severity: str             # "error" | "warning"
    message: str              # exactly what the string validators emit
    path: str = ""            # JSON-pointer-like, e.g. "/blocks/3/params/dist_params/rate"
    block: str | None = None  # block name when the problem belongs to one
    hint: str | None = None   # concrete next action

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message,
                "path": self.path, "block": self.block, "hint": self.hint}


# One line per code. Adding a check means adding its code here first; the codes
# are permanent, like the DEVLOG bug ids.
PROBLEM_CODES: dict[str, str] = {
    # -- blocks and parameters ---------------------------------------------
    "E_UNKNOWN_BLOCK_TYPE": "The block's `type` is not in BLOCK_REGISTRY.",
    "E_PARAM_UNKNOWN": "A param key this block type does not declare.",
    "W_AVAILABLE_IS_CAPACITY": "A balk_if or DECIDE condition reads AVAILABLE(), the capacity in force, not the free units (IDLE).",
    "W_UNKNOWN_KEY": "A key the model file format does not have (write_model): the document drops it.",
    "E_PARAM_TYPE": "A param has the wrong Python type for its ParamSpec.",
    "E_PARAM_MISSING": "A param the block type declares as required is absent.",
    "E_DUPLICATE_BLOCK": "Two blocks share a name.",
    # -- distributions ------------------------------------------------------
    "E_UNKNOWN_DISTRIBUTION": "A distribution name that is in no registry entry.",
    "E_DISTRIBUTION_MISSING": "A duration spec (link delay, failure uptime/downtime) names no distribution.",
    "E_DIST_PARAM_UNKNOWN": "A dist_params key the distribution does not take (it would be ignored).",
    "W_UNBOUNDED_DURATION": "A duration uses a distribution that can sample below 0; samples truncate to 0.",
    "W_EMPIRICAL_CONTINUOUS": "EmpiricalContinuous given equal-length values/probs samples discrete points.",
    "E_DETERMINISTIC_NO_MEAN": "simulation.deterministic is set and a "
                              "duration spec's distribution has no finite mean for its params "
                              "(e.g. Cauchy, always; Exponential with rate <= 0).",
    # -- expressions and variables -----------------------------------------
    "E_EXPRESSION": "An expression does not compile against the safe grammar.",
    "E_ASSIGN_TARGET": "An ASSIGN target is not a writable `entity.<attr>` or variable name.",
    "E_UNDECLARED_VARIABLE": "An expression or ASSIGN target names a variable the model does not declare.",
    "E_VARIABLE_NAME": "A variable name is not a Python identifier.",
    "E_VARIABLE_RESERVED": "A variable name shadows a reserved name or a built-in function.",
    "E_VARIABLE_INITIAL": "A variable's initial value is not a number, string or boolean.",
    "E_DUPLICATE_VARIABLE": "Two variables share a name.",
    "E_VARIABLE_UNKNOWN": "A VAR(\"name\") call names a variable the model does not declare "
                          ".",
    # -- lookup tables (PHASE4 A1) ------------------------------------------
    "E_TABLE_NAME": "A table has an empty or duplicate name.",
    "E_TABLE_SHAPE": "A table's keys/columns/values do not form a rectangle, or repeat a key.",
    "E_TABLE_VALUE": "A table value (or its default) is not a number.",
    "E_TABLE_MODE": "A lookup mode is unknown, or step/linear was asked for on non-numeric keys.",
    "E_TABLE_UNKNOWN": "A TABLE(\"name\", ...) call names a table the model does not declare.",
    "E_TABLE_KEYS": "A TABLE(...) call passes the wrong number of keys for that table.",
    # -- time-series inputs (PHASE4 D2) --------------------------------------
    "E_SERIES_NAME": "A time series has an empty or duplicate name.",
    "E_SERIES_SHAPE": "A time series' values/times/step do not describe a series.",
    "E_SERIES_TIMES": "A time series' sample times do not strictly increase.",
    "E_SERIES_MODE": "A time series' interpolation or beyond-the-end mode is unknown.",
    "E_SERIES_UNKNOWN": "A TS(\"name\") call names a time series the model does not declare.",
    "E_SERIES_NO_CALENDAR": "A time series uses real timestamps but the model has no calendar.",
    "W_SERIES_PERIOD_UNUSED": "A time series has a period but does not repeat, so the period is never read.",
    "W_SERIES_BEFORE_START": "A dated time series starts before, or ends after, the run window.",
    # -- levels and flows (PHASE4 D1) ----------------------------------------
    "E_LEVEL_NAME": "A level has an empty or duplicate name.",
    "E_LEVEL_BOUNDS": "A level's capacity/min/initial or a power limit is not a number, or the "
                      "three bounds are inconsistent.",
    "E_LEVEL_THRESHOLD": "A level threshold has no numeric value, or an unknown direction.",
    "E_LEVEL_UNKNOWN": "A LEVEL(\"name\") call, a TRANSFER block or an agent's transfer action "
                       "names a level the model does not declare.",
    "E_FLOW_NAME": "A flow has an empty or duplicate name.",
    "E_FLOW_LEVEL": "A flow's \"from\" or \"to\" names no declared level.",
    "E_FLOW_EFFICIENCY": "A flow's efficiency is outside (0, 1].",
    "E_FLOW_UNKNOWN": "A FLOW(\"name\") call names a flow the model does not declare.",
    "E_FLOW_TAG": "A flow's bus \"tag\" is not a string, or its \"priority\" is not a number "
                  "(flows sharing a tag are summed by FLOWSUM).",
    "E_FLOW_PEAK_PRICE": "A flow's \"peak_price\" is not a number (a demand charge "
                         "is a tariff per unit of Peak Rate, billed once at the end of the run).",
    "E_TRANSFER_MODE": "A TRANSFER block's or an agent transfer action's mode is neither "
                       "\"remove\" nor \"add\".",
    # -- system dynamics (PHASE5 SD-1, D89-D91) ------------------------------
    "E_SD_SETTINGS": "simulation.sd is not an object, its \"dt\" is not a number > 0, or its "
                     "method or time_unit is unknown.",
    "E_SD_RK4_RANDOM": "A dynamic flow rate draws random numbers under RK4, which evaluates it "
                       "four times per step.",
    "W_SD_RK4_BUS": "A dynamic flow rate reads FLOW()/FLOWSUM() under RK4, whose stages ignore "
                    "saturation.",
    "W_FLOW_CLOCK_NO_GRID": "A flow rate reads TNOW in a model with no simulation.sd step grid, so "
                            "the clock is only read at events.",
    # -- conveyors (PHASE6 depth C1) ------------------------------------------
    "E_CONVEYOR_SPEC": "A conveyor has no transit_time or an unknown leak_kind.",
    "E_CONVEYOR_GRID": "A conveyor stock needs simulation.sd (it moves one slat per DT).",
    "E_CONVEYOR_METHOD": "A conveyor stock under method rk4; conveyors need euler.",
    "E_CONVEYOR_OUTFLOW": "A conveyor has no or several primary (non-leak) outflows, or its "
                          "primary outflow has a rate.",
    "E_CONVEYOR_LEAK": "A leak on a flow that is not a conveyor's outflow, with no fraction, or "
                       "with a zone outside 0 <= start < end <= 1.",
    "W_CONVEYOR_TRANSIT": "A literal transit_time is not a multiple of dt; the belt has "
                          "round(T/dt) slats.",
    "E_CONVEYOR_WRITE": "Something would write a conveyor stock directly (a TRANSFER block or "
                        "agent transfer action, a BUFFER's mirror level, initial_state.levels, "
                        "or a subscripted conveyor), bypassing its slat model.",
    "W_CONVEYOR_ZONE": "A literal leak zone covers no slat (round(start*d) == round(end*d)), so "
                       "that leak never takes anything.",
    "W_CONVEYOR_LEVEL_KEY": "A conveyor level sets capacity/min/max_inflow/max_outflow, which a "
                            "conveyor ignores (its own conveyor.capacity/in_limit apply).",
    "E_FLOW_BIFLOW": "A flow's \"biflow\" is not a boolean, or a bi-directional flow joins a "
                     "level with a bound (min, capacity or power limit).",
    # -- auxiliaries, delays, initial expressions (PHASE5 SD-2, D92) ----------
    "E_AUX_NAME": "An auxiliary has an empty or duplicate name.",
    "E_AUX_UNKNOWN": "A literal AUX(\"name\") call names no declared auxiliary.",
    "E_AUX_CYCLE": "Auxiliaries read each other in a cycle.",
    "E_DELAY_NAME": "A delay has an empty or duplicate name.",
    "E_DELAY_UNKNOWN": "A literal DELAY(\"name\") call names no declared delay.",
    "E_DELAY_SPEC": "A delay's kind is unknown, its input or delay_time is missing, or its "
                    "delay_time is a number <= 0.",
    "E_SD_DELAY_ORDER": "A material/information delay's order (DELAYN's n) is not a literal "
                        "integer >= 1.",
    "E_SD_REQUIRES_DT": "A material or information delay (SMOOTH, DELAY1/3/N) needs "
                        "simulation.sd; a pipeline (DELAYP) does not.",
    "E_SD_FUNCTION_CONTEXT": "An inline SMOOTH/DELAY call sits where no delay can be generated "
                             "(a block, a price); only a flow rate, an auxiliary or a delay "
                             "input may hold one.",
    "E_LEVEL_INITIAL_CYCLE": "Level initial expressions read each other in a cycle.",
    # -- units (PHASE5 SD-3, core/units.py) -----------------------------------
    "W_UNIT_MISMATCH": "A level/flow/auxiliary's declared or inferred unit does not match "
                       "another unit it should agree with (expected vs found).",
    "W_UNIT_TIME": "A unit matches dimensionally but uses a different time scale than the "
                   "model's base time unit (e.g. people/hour feeding a level in days).",
    "W_UNIT_UNPARSED": "A declared `unit` string could not be parsed.",
    "E_UNIT_MISMATCH": "`simulation.sd.units == \"strict\"`: same condition as "
                       "W_UNIT_MISMATCH, reported as an error instead.",
    "E_UNIT_TIME": "`simulation.sd.units == \"strict\"`: same condition as "
                   "W_UNIT_TIME, reported as an error instead.",
    "W_UNIT_MISSING": "`simulation.sd.units == \"strict\"`: a level or flow declares "
                      "no unit at all.",
    # -- subscripts (PHASE5 SD-3) ----------------------------------------------
    "E_SUBSCRIPT_NAME": "A subscript's name is not an identifier or is a duplicate, or its "
                        "elements are empty, repeated, or hold [ ] , * or a subscript name.",
    "E_SUBSCRIPT_UNKNOWN": "A record's \"dims\" is not a list of declared subscript names.",
    "E_SUBSCRIPT_DIM": "A \"[dim]\" index names a dimension its record does not have, a "
                       "\"[*]\" sits outside SUM/MEAN, \"dims\" repeats one, or a flow joins "
                       "a subscripted level without its dims.",
    "E_SUBSCRIPT_AGGREGATE": "A one-argument SUM(x)/MEAN(x) has no \"[*]\" to unroll over, or "
                             "sits outside a level, flow, auxiliary or delay.",
    # -- subscript ranges, mappings, per-element equations (PHASE6 S3) ----------
    "E_SUBSCRIPT_RANGE": "A subscript's \"parent\" is not another declared subscript holding "
                         "all of its elements.",
    "E_SUBSCRIPT_MAP": "A subscript's \"map_to\" names no other declared subscript, or its "
                       "\"elements\" are not one target element per element (without them the "
                       "two sizes must match).",
    "E_SUBSCRIPT_SIZE": "A subscripted record's element count (the product of its dims' sizes) "
                        "is over the cap (200,000 records); it is not flattened.",
    "E_SUBSCRIPT_ELEMENT": "A record's per-element \"elements\" overrides are not an object, "
                           "or name an element combination its \"dims\" do not have.",
    "E_AGGREGATE_AGENTS": "A two- or three-argument SUM/MEAN (the agent aggregate over a "
                          "population's attribute, optionally in one state) does not name its "
                          "population, attribute and state as string literals.",
    # -- agents (PHASE5 AB-1, core/agents/) ------------------------------------
    "E_AGENT_TYPE_NAME": "An agent type has an empty or duplicate name.",
    "E_AGENT_STATE": "An agent type has no states or a duplicate state, or an initial state, "
                     "a transition end or a population's initial state is not declared.",
    "E_AGENT_TRANSITION": "A transition does not have exactly one trigger among timeout, rate, "
                          "condition, message and tick, its message is not a name, its tick is "
                          "not a condition string, or its scan is not a number > 0.",
    "E_AGENT_TICK": "A tick is not {dt, activation?, phase?}, its dt is not a number "
                    "> 0, its activation is not synchronous/random/sequential, its phase is not "
                    "in [0, dt), or a population whose type has on_tick actions or \"tick\" "
                    "transitions has no tick dt (on the type or the population), or a let "
                    " is not {name: expression} with new identifier names.",
    "E_AGENT_RATE_FORM": "A rate is not shared (no self), own (self, parameters, constants) or "
                         "a product of the two (a factor mixes self with shared state), or it "
                         "varies continuously with time (a bare TNOW, RAMP, a linear TS).",
    "E_AGENT_ATTRIBUTE": "An attribute or parameter name is invalid or duplicate, an attribute is "
                         "named \"heading\" (a read-only built-in on a continuous space), "
                         "a parameter is not a number, or an expression reads an undeclared "
                         "self.<attribute> -- self.heading included, on a type with no "
                         "continuous space.",
    "E_AGENT_ACTION": "An action is not exactly one of set {target, expr}, send {send, to, "
                      "payload?}, enter {enter, e_type?}, transfer {transfer, amount, mode?}, "
                      "signal {signal}, move_to {move_to}, spawn {spawn, count?, state?, "
                      "init?}, remove {remove}, step {step: [dx, dy]}, move_random "
                      "{move_random: \"any\"|\"free\"}, forward {forward: <distance>}, turn "
                      "{turn: <degrees>}, face {face: <target>}, random_walk "
                      "{random_walk: {step, turn_sd?}} or python {python: <function>}; a set "
                      "target is neither "
                      "self.<declared attribute> nor a declared variable, an enter names no "
                      "block an entity can enter, move_to needs a space the type does not "
                      "declare, a grid move_to/step is not in whole cells, or forward/turn/"
                      "face/random_walk is used on a type without a continuous "
                      "space.",
    "E_AGENT_EXPRESSION": "An agent type's expression (attribute init, timeout, rate, condition, "
                          "guard, action, speed, a send's where) does not compile, or reads "
                          "`payload` outside a message transition's guard and actions.",
    "E_AGENT_PYTHON": "An agent type's python is not a string of top-level def "
                      "statements that parses, uses a refused construct (import, global, class, "
                      "yield, with, a _-prefixed name, an attribute outside the agent/api views, "
                      "math and plain list/dict/str methods), or a {python: <function>} action "
                      "names no top-level def taking exactly (agent, api). Checked without "
                      "running anything; running it needs the caller's consent "
                      "(NAGASIM_ALLOW_AGENT_PYTHON=1).",
    "E_AGENT_SPACE":"An agent type's space.kind is not \"continuous\"/\"network\"/\"grid\", a "
                     "continuous space has no valid bounds or its edge is not "
                     "\"none\"/\"clamp\"/\"wrap\"/\"bounce\", a network space's model "
                     "declares no network nodes, or a grid's size/cell/torus/neighbourhood "
                     "is invalid.",
    "E_AGENT_PLACEMENT": "A population's placement.kind is not \"random\"/\"table\", or a table "
                         "placement's points/nodes list is missing, empty or malformed (on a "
                         "grid: cells [cx, cy] inside it, and no bounds).",
    "E_AGENT_GRID": "PATCH/NEIGHBOURS/NEIGHBOUR_SUM/NEIGHBOUR_MEAN/CELL_COUNT, a step or "
                    "move_random action, or a patch.<name> set is used in an agent type "
                    "without a grid space.",
    "E_AGENT_PATCH": "A grid patch is not {name, init?, diffuse?, evaporate?, grow?} with a "
                     "unique identifier name, diffuse/evaporate is not in [0, 1], grow is not "
                     "{rate >= 0, max?}, its init is not a "
                     "number or an agent-free expression of cx, cy, patch_tick is not > 0 or "
                     "missing while a patch diffuses, evaporates or grows, or PATCH(\"name\") / "
                     "patch.<name> names an undeclared patch.",
    "E_AGENT_NETWORK": "A population's network is not {kind: none|random|small_world|"
                       "scale_free|table, directed?, link_on_spawn?} with its kind's own keys: "
                       "random exactly one of p in [0, 1] or mean_degree in [0, count - 1]; "
                       "small_world an even k >= 2 below count and beta in [0, 1]; scale_free "
                       "an integer m >= 1 below count; table a model table with numeric "
                       "\"from\"/\"to\" columns; small_world/scale_free undirected.",
    "E_POPULATION_NAME": "A population has an empty or duplicate name.",
    "E_POPULATION_TYPE": "A population names an undeclared agent type.",
    "E_POPULATION_COUNT": "A population's count is not an integer >= 0, its initial states "
                          "place more agents than it has, or its max_count is not an integer "
                          ">= count.",
    "E_POPULATION_FULL": "At run time: a spawn found its population at max_count; "
                         "the run stops with an ExpressionError whose message starts with this "
                         "code.",
    "E_POPULATION_UNKNOWN": "COUNT/SUM/MEAN/AGENT, a send's `to`, a spawn or a remove target "
                            "names an undeclared population.",
    "E_SEND_TARGET": "A send action's or SEND block's `to` (or a remove action's target) is "
                     "malformed: not \"self\" (agents only), an index, an agent expression or {population, where?, random?}; "
                     "or a SEND block in a model without populations.",
    "W_AGENT_MESSAGE_UNUSED": "A message transition waits for a name that no send action or "
                              "SEND block sends.",
    "W_AGENT_SCAN": "A rate or condition reads model state that moves outside the agents "
                    "(LEVEL, AUX, NQ, a bare TNOW, ...) and has no \"scan\": it is re-evaluated "
                    "only at agent events.",
    "W_AGENT_STATE_COMPOSITE": "An expression compares self.state with a composite state's name: "
                               "self.state is the leaf, so the comparison is never true.",
    # -- XMILE/Vensim import (PHASE5 SD-5, PHASE6 S5: nagasim/sdimport.py, mdlimport.py) ----
    "E_SDIMPORT_XML": "An XMILE file was refused before parsing: a DOCTYPE/ENTITY declaration, "
                      "over the 20 MB cap, or not well-formed XML.",
    "E_SDIMPORT_MDL": "A Vensim .mdl file was refused before parsing: not decodable text, over "
                      "the 20 MB cap, or a malformed equation section.",
    "E_SDIMPORT_UNSUPPORTED": "An XMILE or Vensim construct sdimport/mdlimport does not "
                              "translate (a module, a macro, a conveyor/queue stock, a "
                              "XMILE dimension mapping, a Vensim '<->' equivalence, an unknown function "
                              "or name, a GET XLS/DIRECT/DATA external reference, a variable "
                              "name with a space); the owning variable is left out of the "
                              "imported layout.",
    "W_SDIMPORT_APPROX": "sdimport/mdlimport translated a construct with a documented "
                         "approximation (a bare DELAY, a non-euler/rk4 method, a "
                         "non-day/hour/minute/second time unit, a clock not starting at 0, a "
                         "graphical function/lookup with no input equation).",
    # -- lump-sum and fixed costs (PHASE4 D5) --------------------------------
    # -- delayed conversion and blended levels (Bio-1 G1/G2/G3) -------------
    "E_CONVERSION_NAME": "A conversion has an empty or duplicate name, or is not an object.",
    "E_CONVERSION_INTERVAL": "A conversion's \"cohort_interval\" is not a positive number.",
    "E_CONVERSION_SUBSTRATE": "A conversion declares no substrate, or one with an empty or "
                              "duplicate name.",
    "E_CONVERSION_CURVE": "A substrate's yield or release curve is malformed: a bad \"yield\", "
                          "no curve and no positive \"half_life\", non-increasing knots, or a "
                          "curve that does not end at 1.0.",
    "E_CONVERSION_UNKNOWN": "A CONV(\"name\") call names a conversion the model does not declare.",
    "E_LEVEL_COMPONENT": "A level's component has no name, is declared twice, has an "
                         "initial_fraction outside [0, 1], or the level's \"blend_tolerance\" is "
                         "not positive.",
    "E_FLOW_COMPONENT": "A flow's \"components\" is not an object of component -> share.",
    "E_FLOW_MIN_RATE": "A flow's \"min_rate\" is not a number >= 0.",
    "E_FLOW_THRESHOLD": "A flow rate threshold has no numeric value, an unknown direction, or a "
                        "non-numeric \"cost\".",
    "E_FIXED_COST_NAME": "A fixed cost has an empty or duplicate name, or is not an object.",
    "E_FIXED_COST_AMOUNT": "A fixed cost's \"amount\" is neither a number nor a valid expression.",
    "E_ENERGY_PRICE": "\"simulation.energy_price\" is neither a number nor a valid expression.",
    # -- routings (PHASE4 A2) ------------------------------------------------
    "E_SEQUENCE_NAME": "A sequence has an empty or duplicate name.",
    "E_SEQUENCE_EMPTY": "A sequence has no steps.",
    "E_SEQUENCE_STEP": "A sequence step is malformed: no station, or a non-scalar \"values\" entry.",
    "E_SEQUENCE_STATION_UNKNOWN": "A sequence step names a station with no STATION block.",
    "E_DUPLICATE_STATION": "Two STATION blocks answer to the same station name.",
    "E_SEQUENCE_UNKNOWN": "A CREATE or ASSIGN names a sequence the model does not declare.",
    "E_ROUTE_NO_SEQUENCES": "A ROUTE block exists but the model declares no stations or sequences.",
    "W_STATION_UNUSED": "A STATION block is named by no sequence step.",
    # -- orders and due dates (PHASE4 A3) ------------------------------------
    "E_ORDER_ID": "An order has an empty or duplicate id.",
    "E_ORDER_FIELD": "An order field is malformed: a non-number, a negative quantity, or a "
                     "non-scalar free attribute.",
    "E_ORDER_ENTITY_TYPE": "An order names an entity type the model does not declare.",
    "E_ORDER_SOURCE": "An order's `source` is not a CREATE block in \"Orders\" arrival mode.",
    "W_ORDER_DUE_BEFORE_RELEASE": "An order's due date is before its release time.",
    "W_ORDERS_NOT_CONSUMED": "Orders are declared but no CREATE block is in \"Orders\" mode.",
    "W_CREATE_NO_ORDERS": "A CREATE block is in \"Orders\" mode but no order names it.",
    "W_PLAN_STATION_UNKNOWN": "An order's `plan` names a station with no STATION block "
                              ".",
    # -- queue behaviours (PHASE4 A4) ----------------------------------------
    "E_QUEUE_NO_LEAVE_LINK": "A block with balk_if/renege_after has no second outgoing link for "
                             "the entities that leave without service.",
    "W_QUEUE_LEAVE_LINK_UNUSED": "A SEIZE/PROCESS has a second outgoing link but neither balk_if "
                                 "nor renege_after, so nothing ever takes it.",
    "E_PREEMPT_SCOPE": "preempt is set where the block does not own the whole service (its action "
                       "is not \"Seize Delay Release\"), or another block holds the same resource "
                       "across blocks.",
    "E_PREEMPT_RESOURCE_SET": "preempt names a resource set; preemption works on one resource.",
    "E_ON_PREEMPT": "on_preempt is neither \"resume\" nor \"restart\".",
    "W_PREEMPT_NO_PRIORITY": "preempt is set but every block seizing that resource uses the same "
                             "priority, so nothing can ever preempt anything.",
    # -- links and flow graph ----------------------------------------------
    "E_LINK_UNKNOWN_BLOCK": "A link names a block that does not exist.",
    "E_NO_CREATE": "The model has no CREATE block, so nothing ever arrives.",
    "E_NO_DISPOSE": "The model has no DISPOSE block, so nothing ever leaves.",
    "E_CREATE_NO_OUTPUT": "A CREATE block has no outgoing link.",
    "E_CREATE_ZERO_INTERARRIVAL": "A CREATE block has a constant interarrival time of zero and unlimited arrivals, so simulation time could never advance.",
    "E_DISPOSE_HAS_OUTPUT": "A DISPOSE block has an outgoing link.",
    "E_TOO_MANY_OUTPUTS": "A single-output block has more than one outgoing link.",
    "E_TERMINAL_NOT_DISPOSE": "A reachable block with no outgoing link is not a DISPOSE.",
    "E_DECIDE_BRANCHES": "A DECIDE has more probs/conditions than outgoing links.",
    "E_DECIDE_PROBS": "A DECIDE's chance probabilities do not fit its branches.",
    # -- controllable decisions (ROADMAP 3.7 A) ------------------------------
    "E_DECIDE_POLICY_NAME": "A Policy-mode DECIDE has an empty policy name.",
    "E_DECIDE_FALLBACK": "A Policy-mode DECIDE's fallback is not first/random/shortest.",
    "E_DECIDE_QUEUE_EXPRS": "A Policy-mode DECIDE with fallback \"shortest\" needs one "
                            "queue_exprs entry per outgoing link.",
    "W_UNREACHABLE": "A block cannot be reached from any CREATE.",
    # -- resources, sets, entities ------------------------------------------
    "E_DUPLICATE_RESOURCE": "Two resources (or a resource and a set) share a name.",
    "E_RESOURCE_UNKNOWN": "A SEIZE/PROCESS/RELEASE block's resource_name names no declared "
                          "resource or resource set (#284: was a fail-soft runtime warning only).",
    "E_RESOURCE_DISCIPLINE": "A resource's queue discipline is not a known one.",
    "E_RANKING_REQUIRED": "A ranked discipline needs a ranking_expr.",
    "W_RERANK_UNRANKED": "A resource's \"rerank\" is set but its discipline is not "
                         "LowestValueFirst/HighestValueFirst, so it has no effect.",
    "E_RESOURCE_SCHEDULE": "A resource's capacity_schedule is not a declared capacity schedule.",
    "E_RESOURCE_CAPACITY": "A resource's capacity is below what its capacity schedule asks for.",
    "E_RESOURCE_COST": "A resource cost field is not a number >= 0.",
    "E_RESOURCE_POWER": "A resource's \"power\" is not an object, or one of its values is not a "
                        "number >= 0.",
    "E_RESOURCE_POWER_STATE": "A resource's \"power\" names a state that is not "
                              "busy/idle/blocked/down/off_shift.",
    "E_SET_RULE": "A resource set's selection rule is not a known one.",
    "E_SET_EMPTY": "A resource set has no members.",
    "E_SET_MEMBER": "A resource set names a member that is not a declared resource.",
    "E_ENTITY_COST": "An entity type's holding_cost is not a number >= 0.",
    # -- schedules and failures ---------------------------------------------
    "E_SCHEDULE_NAME": "A schedule has an empty name.",
    "E_DUPLICATE_SCHEDULE": "Two schedules share a name.",
    "E_SCHEDULE_KIND": "A schedule's kind is neither arrival nor capacity.",
    "E_SCHEDULE_POINTS": "A schedule has no points.",
    "E_SCHEDULE_POINT": "A schedule point is not a valid [duration, value] pair.",
    # -- real calendar dates (PHASE4 T10) ------------------------------------
    "E_CALENDAR_START": "simulation.calendar has no start instant, or it is not ISO 8601.",
    "E_CALENDAR_TIMEZONE": "simulation.calendar names a timezone that is not an IANA name.",
    "E_CALENDAR_DATE": "A field written as a real date is not an ISO 8601 date or timestamp.",
    "E_SCHEDULE_NO_CALENDAR": "A weekday schedule needs a model calendar and the model has none.",
    "E_SCHEDULE_WEEKLY": "A weekday grid has an unknown day, a malformed band, or overlapping bands.",
    "E_SCHEDULE_EXCEPTION": "A schedule exception has no start date, or a malformed date range.",
    "W_SCHEDULE_EXCEPTION_UNUSED": "A schedule exception falls entirely outside the run window.",
    "E_FAILURE_NAME": "A failure has an empty name.",
    "E_DUPLICATE_FAILURE": "Two failures share a name.",
    "E_FAILURE_RESOURCES": "A failure names no resources.",
    "E_FAILURE_RESOURCE_UNKNOWN": "A failure names a resource that does not exist.",
    "E_ARRIVAL_MODE": "A CREATE's arrival_mode is not a known one.",
    "E_ARRIVAL_SCHEDULE": "A CREATE in Schedule mode needs a declared arrival schedule.",
    # -- statistics ----------------------------------------------------------
    "E_RECORD_STAT_NAME": "A RECORD block has an empty stat_name.",
    "E_RECORD_KIND": "A RECORD block's kind is not a known user-statistic kind.",
    "E_RECORD_KIND_CONFLICT": "Two RECORD blocks give the same statistic different kinds.",
    # -- units, scenarios, cosmetics -----------------------------------------
    "E_TIMESERIES_COUNTS": "simulation.timeseries_counts is not a list of at most 1000 different "
                           "block names, or names a block the model does not have.",
    "E_TIME_UNIT": "A time_unit is neither \"base\" nor one of the known units.",
    "E_UNIT": "A unit is not one of its quantity's units (length, speed, mass, energy, power).",
    "E_SCENARIO_NAME": "A scenario has an empty name.",
    "E_DUPLICATE_SCENARIO": "Two scenarios share a name.",
    "E_SCENARIO_OVERRIDE": "A scenario override is missing its path or its value.",
    # -- reality checks (PHASE6 S8) -------------------------------------------
    "E_REALITY_CHECK_NAME": "A reality check has an empty name.",
    "E_DUPLICATE_REALITY_CHECK": "Two reality checks share a name.",
    "E_REALITY_CHECK_INPUT": "A reality check's \"inputs\" is not an object of override path: "
                             "value, or one of its paths is empty.",
    "E_REALITY_CHECK_WHEN": "A reality check's \"when\" is neither \"always\", \"final\" nor "
                            "\"after <t>\".",
    # -- loop dominance (depth bucket L1, nagasim/loopdominance.py) -----------
    "E_LOOP_GRID": "Loop dominance needs the SD step grid (simulation.sd) and the model has none.",
    "W_LOOP_HYBRID": "Loop dominance on a model with DES blocks or agent populations: the "
                     "variables they write are read at their initial values (SD layer only).",
    # -- assertions (ROADMAP 3.7 B) -------------------------------------------
    "E_ASSERTION_NAME": "An assertion has an empty or duplicate name.",
    "E_DUPLICATE_ASSERTION": "Two assertions share a name.",
    "E_ASSERTION_KPI": "An assertion's \"kpi\" field is empty.",
    "E_ASSERTION_OP": "An assertion's \"op\" field is unknown or missing.",
    "E_ASSERTION_REPLICATIONS": "An assertion's \"replications\" is not an integer >= 1.",
    "E_ASSERTION_CONFIDENCE": "An assertion's \"confidence\" is not a number between 0 and 1.",
    # -- dashboard (PHASE6 G1, schema v26) ------------------------------------
    "E_DASHBOARD_NAME": "A dashboard widget has an empty or duplicate name.",
    "E_DASHBOARD_KIND": "A dashboard widget's kind is not kpi/gauge/line/bar.",
    "E_DASHBOARD_EXPR": "A dashboard widget's expression is empty, does not compile, names "
                        "an unknown variable or resource, draws a random number, or is a "
                        "constant that is not a number.",
    "E_DASHBOARD_SERIES": "A dashboard widget's series do not fit its kind: kpi/gauge take "
                          "\"expr\" (not \"series\"), line takes 1-4 series and bar 1-8, with "
                          "unique non-empty labels.",
    "E_DASHBOARD_GAUGE": "A gauge widget's \"max\" is missing or <= \"min\", or its "
                        "\"thresholds\" are unsorted or outside [min, max].",
    "E_DASHBOARD_CONTROL": "A dashboard control is unnamed or duplicated, of an unknown kind, "
                           "or names an unknown target; a slider's min >= max, step <= 0 or a "
                           "capacity range outside 0..the declared capacity; a decision's "
                           "policy has no Policy-mode DECIDE or a different option count.",
    # -- Present mode (D242, D251, schema v29) ---------------------------------
    "E_DASHBOARD_DEFINITION": "The dashboard section is malformed or over a ceiling: an "
                              "unknown key, too many entries, a range that is not finite, "
                              "ordered and within 10 000 steps, text that is too long or not "
                              "printable, an item its page does not take or whose \"ref\" "
                              "names nothing.",
    "E_DASHBOARD_INPUT": "A dashboard input's path addresses nothing an override may change "
                         "in this model, or its kind does not fit the value found there.",
    # -- the browser viewer (D325, D327, D333) ---------------------------------
    "E_VIEWER_MODEL": "A model cannot be shared as a browser viewer as it is: it holds agent "
                      "Python, a LOGIC block, something a plugin registered, a data source "
                      "that was not frozen into the copy, or it is too large.",
    "W_VIEWER_LONG_RUN": "A run at the most replications a viewer allows is estimated to take "
                         "over five minutes in a browser.",
    "W_VIEWER_EMPTY": "The model's dashboard definition lists no input or no result, so a "
                      "viewer of it has nothing to change or nothing to show.",
    "E_ANNOTATION_ID": "An annotation has an empty id.",
    "E_DUPLICATE_ANNOTATION": "Two annotations share an id.",
    "E_ANNOTATION_KIND": "An annotation's kind is neither text nor frame.",
    "E_ANNOTATION_SHAPE": "An annotation lacks the geometry its kind needs.",
    "E_BACKGROUND": "The background image section is malformed.",
    "W_BACKGROUND_MISSING": "The background image's path does not exist on disk (F3: this used "
                            "to be a plain string ModelDocument.problems() appended itself, not "
                            "a schema check; same text, now a coded Problem).",
    # -- submodels -----------------------------------------------------------
    "E_SUBMODEL": "A submodel section problem reported while flattening.",
    "E_SUBMODEL_SCOPE": "SUB_ENTRY/SUB_EXIT used outside a submodel definition.",
    "E_SUBMODEL_CYCLE": "A submodel definition instantiates itself, directly or transitively.",
    "E_EXTENDS_CYCLE": "A definition (or library asset) extends itself, directly or transitively "
                       ".",
    "E_EXTENDS_UNKNOWN": "A definition's `extends` names no definition in the model (or an "
                         "asset's, no asset of the same library).",
    "E_SUBMODEL_PORT_NAME": "A submodel port has no name.",
    "E_DUPLICATE_SUBMODEL_PORT": "Two ports on the same side share a name.",
    "E_SUBMODEL_DEAD_END": "A definition with no SUB_EXIT has a block that is not a DISPOSE dead end.",
    "E_SUBMODEL_SCOPED_NAME": "A `$name` in a definition has no top-level template to clone.",
    "E_UNKNOWN_SUBMODEL": "A SUBMODEL instance names a definition that does not exist.",
    "E_SUBMODEL_ARGUMENTS": "A SUBMODEL instance's arguments are not an object.",
    "E_SUBMODEL_ARGUMENT_UNKNOWN": "An instance passes an argument the definition does not declare.",
    "E_SUBMODEL_ARGUMENT_MISSING": "An instance omits a declared parameter that has no default.",
    "E_SUBMODEL_PORT_REQUIRED": "A link to/from an instance must name a port.",
    "E_SUBMODEL_PORT_UNKNOWN": "A link names a port the definition does not have.",
    # -- structural arguments (PHASE4 B6, ARCHITECTURE D59) -------------------
    "E_SUBMODEL_ARG_UNKNOWN": "A {\"$arg\": ...} names a parameter the definition does not declare.",
    "E_SUBMODEL_ARG_TYPE": "A {\"$arg\": ...} value does not fit the type its target declares.",
    "E_SUBMODEL_ARG_REFERENCE": "A `refers_to` argument names a record the model does not declare.",
    # -- data bindings (ROADMAP 3.4a) -----------------------------------------
    "E_DATA_SOURCE_NAME": "A data source has an empty or duplicate name.",
    "E_DATA_SOURCE_KIND": "A data source's kind is not one the engine reads.",
    "E_DATA_SOURCE_OPTION": "A live source carries an option its reader does not know, or a "
                            "value the option cannot take.",
    "E_DATA_SOURCE_LOCATION": "A data source has no location, or none its kind can use.",
    "E_DATA_CREDENTIALS": "A source URL embeds a password; read it from ${NAGASIM_DATA_<NAME>} instead.",
    "E_DATA_BINDING_NAME": "A binding has an empty or duplicate name.",
    "E_DATA_BINDING_SOURCE": "A binding names a data source the model does not declare.",
    "E_DATA_BINDING_TRANSFORM": "A binding's transform is not a known one.",
    "E_DATA_BINDING_TARGET": "A binding's target is not an override path this model has.",
    "E_DATA_FILTER": "A binding's filter is not a simple {column op value} mapping.",
    "E_DATA_READ": "A data source could not be read (missing file, bad query, unreachable URL).",
    "E_DATA_EMPTY": "A binding's column held no usable value after filtering.",
    "E_DATA_TYPE": "A binding's resulting value does not match the type the target holds.",
    "E_DATA_ENV": "A ${NAGASIM_DATA_<NAME>} reference names an environment variable that is "
                  "not set.",
    "E_DATA_ENV_NAME": "A ${NAME} reference names a variable a model file may not read: only "
                       "names starting with NAGASIM_DATA_ are expanded.",
    # -- outbound access and path confinement (nagasim/netguard.py) -----------
    "E_DATA_NETWORK_REFUSED": "A network source was refused: no human granted network access.",
    "E_DATA_URL_SCHEME": "A source URL is not http(s), or is a file masquerading as a database URL.",
    "E_DATA_URL_PARAMETER": "A database URL carries a query parameter a model file may not set.",
    "E_DATA_QUERY": "A source's query is not one SELECT statement.",
    "E_DATA_URL_HOST": "A source URL has no usable hostname, or one outside the allowed hosts.",
    "E_DATA_URL_PRIVATE": "A source URL resolves to a loopback, link-local or private address.",
    "E_DATA_URL_REDIRECT": "A fetch redirected too many times, downgraded, or landed somewhere refused.",
    "E_DATA_TOO_LARGE": "A fetched response is over the size cap.",
    "E_DATA_PATH_OUTSIDE_ROOT": "A source path escapes the root the caller confined it to.",
    "E_DATA_CONNECTION": "A live source names no connection, or one its caller did not define "
                         "for it.",
    "E_CONN_FORMAT": "The caller's connections file is malformed, or asks for something refused.",
    "E_DATA_PLAINTEXT_CREDENTIAL": "A connection would send its credential over a channel that is "
                                   "not encrypted, to another machine.",
    "E_DATA_TLS": "A connection's TLS handshake failed: the certificate is not valid for the host.",
    "W_DATA_ALLOW_NETWORK_IGNORED": "A data source carries \"allow_network\"; consent never comes "
                                    "from the model file and the key is stripped on load.",
    # -- live data (nagasim/live.py, PHASE7 7.8) ------------------------------
    "W_LIVE_LOST": "A live source's buffer overflowed: events were dropped before they were read.",
    "W_LIVE_GAP": "A live source's stream broke: an unknown number of events is missing.",
    "W_LIVE_SKEW": "A live source's clock and this machine's differ by more than 30 seconds.",
    "W_LIVE_LIMIT": "A live source's server did not accept the message limit, so the size of "
                    "what it sends is not bounded.",
    "W_LIVE_DROPPED": "A live source sent something over a bound or of a type that is not data; "
                      "it was dropped and counted.",
    # -- warm start (ROADMAP 3.4b) --------------------------------------------
    "E_INITIAL_BLOCK": "An initial-state entry is not an object, names a block the model does not have, or "
                       "misstates its place, its holder or its attributes.",
    "E_INITIAL_BLOCK_TYPE": "An initial-state entry names a block type that cannot hold entities.",
    "E_INITIAL_COUNT": "An initial-state entry's count is negative or not an integer.",
    "E_INITIAL_IN_SERVICE": "in_service exceeds the entry's count or the resource's capacity.",
    "E_INITIAL_REMAINING": "remaining is not a list of non-negative numbers, or is longer than in_service; "
                           "or ends, arrives or since is not an absolute time the entry can carry.",
    "E_INITIAL_ARRIVALS": "initial_state.arrivals does not give a CREATE block a count and a next time.",
    "E_INITIAL_FAILURE": "initial_state.resources.<name>.failures names a failure the resource does "
                         "not have, or gives it no next or down_until time.",
    "E_INITIAL_SET": "initial_state names a resource set, a cursor or a member the model does not have.",
    "E_INITIAL_BUSY": "A resource is reported busy with a number of units the in-service entities do not hold.",
    "E_INITIAL_QUEUE_CAPACITY": "More entities wait at a block, or sit in a BUFFER, than it has places.",
    "E_INITIAL_FLEET": "initial_state.fleets or an entity's trip does not fit the model: a fleet, a "
                       "vehicle record, a node, a charger or a stage it does not have.",
    "E_INITIAL_BELT": "A part on a belt in initial_state does not fit the model: not one entity at an "
                      "accumulating CONVEYOR, a stage it does not have, a time on the wrong side "
                      "of the clock, or more than one part at the belt's end.",
    "E_INITIAL_BATCH": "A formed batch in initial_state does not fit the model: an entry that "
                       "meets a SEPARATE has no members, or its members are not records of one "
                       "batch the model can form.",
    "E_INITIAL_AGE": "An initial-state entry's age is negative or not a number.",
    "E_INITIAL_RESOURCE": "initial_state.resources, or an entity's holds, names a resource the "
                          "model does not declare, or a hold it cannot be.",
    "E_INITIAL_VARIABLE": "initial_state.variables names a variable the model does not declare.",
    "E_INITIAL_CLOCK": "initial_state.clock is not a number >= 0 and before the end of the run.",
    "E_INITIAL_SCHEDULE_OFFSET": "initial_state.schedule_offset is not a number >= 0.",
    # -- a state as a document (PHASE7 7.10, D231-D238) -------------------------
    "E_STATE_FORMAT": "The file is not a state document of format 1, or holds a value that is not plain data.",
    "E_STATE_HASH": "A state document does not match its own hash: it was edited after it was written.",
    "E_STATE_MODEL": "A state document was taken for another model, or another version of this one.",
    "E_STATE_SIZE": "A state document is over the size bound (16 MiB, 100 000 entities).",
    "E_STATE_STALE": "An observed state is older than the age the caller allows, or has no time stamp.",
    "E_STATE_UNSUPPORTED": "The run holds something a state cannot carry (the message names the kind); "
                           "no partial state is written.",
    "W_STATE_NOT_COVERED": "An observed state says nothing about an element kind the model has; "
                           "it starts as the model declares it.",
    # -- a plant as a timeline (PHASE7 7.11, D252-D259) --------------------------
    "E_PLANTMAP_FORMAT": "The file is not a plant mapping of format 1, or a part of it is malformed.",
    "E_PLANTMAP_SIZE": "A plant mapping is over a bound (1 MiB, 2000 signals, 10 000 entries a table, "
                       "1024 bytes a key).",
    "E_PLANTMAP_NAME": "A plant mapping names a block, resource or level the model does not have.",
    "E_PLANTMAP_DOUBLE": "Two signals of a plant mapping tell one thing (a resource's units, a "
                         "block's content): the engine does not choose between two reports.",
    "E_PLANT_LOG": "A recorded plant log cannot be read, or is over its bound (256 MiB, 1 000 000 lines).",
    "W_PLANT_DROPPED": "Plant observations were dropped: a value the mapping cannot read, a time out of "
                       "range, a stale event, or something over a bound. Counted by reason.",
    "W_PLANT_TIME": "Plant observations had no source stamp, one ahead of its receipt, or came late; "
                    "each was given a time by the stated rule.",
    "W_PLANT_SYNTHETIC": "The observed timeline holds events for items or units the plant did not name "
                         "(a queue length, a unit count).",
    "W_PLANT_CONTRADICTION": "The plant said two things that do not agree; the stated rule was applied.",
    "W_PLANT_SERVICE_INFERRED": "A plant reports units of a resource busy and names no job: the longest-waiting "
                                "entities of the one block that seizes it are taken as in service.",
    "W_PLANT_GAP": "The plant's log has a hole (events lost, or the stream broke): what was missed is "
                   "not in the observed timeline.",
    # -- the twin cycle (PHASE7 7.12, D265-D276) ------------------------------
    "E_TWIN_STATE": "This model cannot be started from the plant's state yet (the message names the "
                    "kind): the twin goes on monitoring and makes no forecast.",
    "E_TWIN_FORECAST": "A twin's forecast failed; the last good one is kept and the next is tried at "
                       "its own time.",
    "W_TWIN_FORECAST_BUSY": "A forecast was due while the previous one was still running: none was started.",
    "W_TWIN_SOURCE": "A source of the twin is down, has not answered yet, could not be read, has "
                     "been silent for longer than --max-age, or says nothing the mapping reads: "
                     "the view keeps its last picture and no forecast is started.",
    "E_TWIN_OUTPUT": "A twin's result file could not be written; the next cycle writes again.",
    "E_TWIN_RESULT": "No twin result of format 1 can be read from that directory.",
    "W_TWIN_CLOCK": "This machine's clock jumped against its own count of time between two cycles.",
    "W_TWIN_CUT": "A twin's result was over a bound: the part named was left out (or, for a "
                  "source with more events waiting than one cycle reads, read at the next cycle).",
    "W_TWIN_SCORE": "The rolling score found in a twin's directory was of another model or "
                    "horizon, or could not be read: it starts again from now (said once).",
    "W_TWIN_MODEL_CHANGED": "The model file changed on disk while a twin runs on it: the twin keeps "
                            "the model it loaded until it is started again.",
    "E_CANDIDATES": "A candidates file is not one of format 1, is over its bound, holds a key or "
                    "an action it may not, or names what the model does not declare.",
    "E_CANDIDATE_VERB": "A candidate's verb is listed in the vocabulary and not built in this "
                        "version (the message says why), or is no verb at all.",
    "E_RECOMMEND_KPI": "The KPI the candidates are compared on is not in the forecast's table.",
    "W_TWIN_RECOMMEND": "A twin's recommendations could not be kept or continued as asked: the "
                        "message says what (a ledger of another model, a state too large to "
                        "keep, an action that no longer applies). Said once.",
    "E_OUTBOX_SINK": "The outbox given at start is no sink this version builds, or its path is "
                     "not a place a record may be written.",
    "W_OUTBOX": "The outbox could not take a record, stopped at a ceiling, or took one again "
                "after a refusal. Said once a change; records wait until they expire.",
    "W_INITIAL_STATE_WARMUP": "Both an initial state and a warm-up are set; warm-up discards the "
                              "very history the initial state supplies.",
    # -- path network, movers and conveyors (PHASE4 E1, ARCHITECTURE D68) -----
    "E_NETWORK_SHAPE": "The \"network\" section is not an object, or length_factor is not > 0.",
    "E_NETWORK_NODE": "A network node has an empty or duplicate name, or a non-numeric "
                      "coordinate.",
    "E_NETWORK_SEGMENT": "A segment names an unknown node, joins a node to itself, or has a "
                         "bad length, speed, capacity or direction.",
    "E_NETWORK_NODE_UNKNOWN": "A block's \"node\" param names a node the network does not "
                              "declare.",
    "E_NETWORK_UNREACHABLE": "Two places the model travels between are joined by no path.",
    "W_SEGMENT_DEADLOCK_RISK": "A two-way segment of capacity 1 on a network driven by more than "
                               "one vehicle: two meeting head-on can deadlock.",
    "E_DISTANCE_UNKNOWN": "A DISTANCE(\"a\", \"b\") call names a place with no network node.",
    "E_FLEET_NAME": "A mover fleet has an empty or duplicate name, or one clashing with a "
                    "resource.",
    "E_FLEET_FIELD": "A fleet's count, speed, capacity or dispatching rule is missing or "
                     "malformed.",
    "E_FLEET_NODE": "A fleet's home node is not a declared network node.",
    "E_FLEET_POWER": "A fleet's \"power\" is not an object, or one of its values is not a "
                     "number >= 0.",
    "E_FLEET_POWER_STATE": "A fleet's \"power\" names a state that is not "
                           "busy/idle/blocked/down/off_shift.",
    "E_FLEET_UNKNOWN": "A TRANSPORT block names a fleet the model does not declare.",
    "E_FLEET_LOADS": "A fleet's \"loads\" is not an integer >= 1, or is above 1 on a worker "
                     "fleet.",
    "E_FLEET_UNITS":"A fleet's \"units\" is not a list of named units with skill lists, has "
                     "duplicate names, or disagrees with its count.",
    "E_FLEET_MOTION": "A fleet's \"motion\" is not \"network\"/\"gantry\", a gantry's "
                      "speed_x/speed_y/hoist_time is missing or invalid, or a gantry fleet is "
                      "also kind \"worker\".",
    "E_WORKER_FLEET": "A SEIZE/PROCESS worker_fleet names no fleet of kind \"worker\".",
    "E_WORKER_SKILL_UNKNOWN": "A worker_skill that no unit of the worker fleet has.",
    "E_WORKER_NO_NODE": "A SEIZE/PROCESS with worker_fleet sits on no network node.",
    "E_WORKER_WHEN_BLOCKED": "worker_when_blocked is neither \"leaves\" nor \"stays\".",
    "E_WORKER_PREEMPT": ("preempt is set on a block that summons a worker, or that seizes a "
                         "resource a worker block also seizes."),
    # -- motion (automation sub-project 1, D156-D158) --------------------------
    "E_MOTION_PROFILE_UNKNOWN": "A MOTION block names a motion profile the model does not declare.",
    "E_MOTION_SEGMENT": "A MOTION segment has an unknown kind or field, no distance/time, or a "
                        "negative value; or the cycle has no segments.",
    "E_MOTION_PAYLOAD": "A move's payload_kg is above its profile's payload_max.",
    "E_MOTION_ZONE": "A MOTION block's hrc mode is unknown, or a separation zone names no network "
                     "node or has radius_m <= 0.",
    "E_MOTION_PROFILE": "A motion profile has an empty/duplicate name, an unknown kind, speed or "
                        "accel <= 0, a bad payload_max/payload_derate/collaborative_speed, or "
                        "lacks the collaborative_speed an HRC block needs.",
    # -- batteries and charging (automation sub-project 2, D164-D166) -----------
    "E_BATTERY": "A fleet's battery has capacity_kwh <= 0, a fraction outside 0-1, low >= target, "
                 "an unknown strategy, or lacks a field its strategy needs.",
    "E_CHARGER": "A charger has an empty, duplicate or already used name, an unknown node or "
                 "kind, points < 1, "
                 "power_kw <= 0, a knee, taper_floor or efficiency outside 0-1, or is a swap "
                 "station without swap_time.",
    "E_CHARGER_UNKNOWN": "A fleet's battery names a charger the model does not declare.",
    "E_BATTERY_SCHEDULE": "A fleet's battery names a schedule the model does not declare.",
    "W_NO_CHARGER": "A battery fleet has no reachable charger of the kind its strategy needs.",
    # -- mobile cobots, machine tending (automation sub-project 3, D168-D170) ---
    "E_FLEET_ARM": "A fleet's arm names an unknown motion profile, sits on a worker or gantry "
                   "fleet or one with loads > 1, or exchange is set without an arm.",
    "E_TENDING": "A STATION's tending group has a malformed segment, an unknown field, time "
                 "unit, hrc mode, zone or cobot mode, an hrc mode the arm's profile has no "
                 "collaborative_speed for, or the station has no node.",
    "W_TENDING_STAY": "A station keeps its cobot (\"stay\") but no TRANSPORT of an arm fleet "
                      "carries the part on.",
    "W_TENDING_DEADLOCK": "One cobot both feeds and empties a tending station with no exchange "
                          "and no finite space before the machine: it can wait loaded for a "
                          "machine that waits to be emptied.",
    "W_TENDING_UNUSED": "A station declares tending but no fleet has an arm.",
    # -- investment case (automation sub-project 4, D172-D173) ------------------
    "E_INVESTMENT": "The investment section is not an object, names an unknown baseline "
                    "scenario or throughput KPI, or has horizon_years or "
                    "operating_hours_per_year <= 0, discount_rate < 0 or a margin_per_part "
                    "that is not a number.",
    "W_INVESTMENT_NO_CAPEX": "The model has an investment section and no fixed cost flagged "
                             "capex, in the model or in a scenario.",
    "W_INVESTMENT_ONE_SCENARIO": "The model has an investment section and no scenario to "
                                 "compare with its baseline.",
    "E_TRAVEL_MODE": "A ROUTE/TRANSPORT travel mode is unknown, or needs a network the model "
                     "does not declare.",
    "E_CONVEYOR_PARAM": "A CONVEYOR's length, speed or spacing is not a number > 0, or its "
                        "endpoints name unknown nodes.",
    # -- blocking after service (PHASE4 T5, ARCHITECTURE D78) ----------------
    "E_BUFFER_CAPACITY": "A BUFFER's capacity, or a SEIZE/PROCESS queue_capacity, is negative.",
    "E_BUFFER_LEVEL": "A BUFFER's \"level\" names a level the model does not declare.",
    "E_PREEMPT_BLOCKING": "preempt names a resource a station holds while blocked after service; "
                          "a finished part cannot be interrupted.",
    "W_BLOCKING_LOOP": "A closed loop of finite buffers can deadlock: every station in it could "
                       "end up holding a part with nowhere to put it.",
    # -- PHASE5 O4 engine ceilings ---------------------------------------------
    "E_HOLD_SPACE_TARGET": "hold_until_space is set but the block it reserves in (space_of, or the "
                           "first link's target) is not a BUFFER with capacity > 0 or a "
                           "SEIZE/PROCESS with queue_capacity > 0.",
    "E_MATCH_ATTRIBUTE": "A MATCH match_on is not an attribute name (an identifier, optionally "
                         "written entity.<name>).",
    # -- asset libraries (PHASE4 B1-B3, product/libraries/) -----------------
    "E_LIBRARY_FORMAT": "A library file is not an object, or its format_version is unknown.",
    "E_LIBRARY_HEADER": "A library header field is missing or malformed (id, name, semver version).",
    "E_LIBRARY_VERSION": "A library needs a newer schema version than this build supports.",
    "E_ASSET_ID": "An asset has an empty or duplicate id.",
    "E_ASSET_DEFINITION": "An asset's definition is invalid by the ordinary model validator.",
    "E_ASSET_PARAMETER": "A parameter's type, default, range or choices are inconsistent.",
    "E_ASSET_BLOCK_TYPE": "An asset uses a block type this build does not register (needs a plugin).",
    "E_ASSET_REQUIRES": "An asset's required model-level records name an unknown section, or are "
                        "malformed.",
    "E_ASSET_CODE": "A library carries executable content (custom_logic, a plugin reference, Python).",
    "E_ASSET_GEOMETRY": "An asset's \"geometry\" fragment is malformed, or a block inside it binds "
                        "to a node the fragment does not declare.",
    "E_ASSET_GRAPHIC": "A graphic names an unknown state, or its SVG is unsafe or over the size cap.",
    "E_ASSET_TEST": "An asset test entry is malformed.",
    "E_ASSET_MIGRATION": "An asset's \"migrations\" entry is malformed: a bad semver range, an "
                         "unknown step, or a step naming a parameter this version lacks.",
    "E_ASSET_TEST_FAILED": "An asset test's KPI is outside its tolerance.",
    # -- parameter catalogues (PHASE4 B4, ARCHITECTURE D58) -------------------
    "E_LIBRARY_CATALOGUE": "A catalogue's shape is wrong: no name, no columns, a duplicate name, "
                           "or a row that is not an object.",
    "E_CATALOGUE_COLUMN": "A catalogue row is missing a declared column, or a preset maps a column "
                          "the catalogue does not declare.",
    "E_CATALOGUE_VALUE": "A catalogue cell's type does not fit the parameter the preset maps it to.",
    "W_LIBRARY_ITEM_CLASH": "A required model-level record already exists under that name with "
                            "different content; the model's own record is used.",
    "W_LIBRARY_UPGRADE": "A definition's library has a newer version installed.",
    # -- MES digital twin (PHASE4 4F, product/mes/, ARCHITECTURE D71) -------
    "E_MES_ADAPTER": "An adapter spec is not one this build knows (sqlite:<path> / csv:<folder>).",
    "E_MES_READ": "An MES source could not be read: missing file, missing table, bad columns.",
    "E_MES_TIME": "A timestamp is not ISO 8601.",
    "E_MESMAP_FORMAT": "A *.mesmap.json is not an object, or needs a newer format version.",
    "E_MESMAP_TIME": "A mapping has no time epoch, or an unknown model time unit.",
    "E_MESMAP_WORK_CENTRE": "A work centre entry is malformed or duplicated, or names a station, "
                            "resource or failure the model does not declare.",
    "E_MESMAP_PRODUCT": "A product entry is malformed or duplicated, or names an entity type or "
                        "sequence the model does not declare.",
    "E_MESMAP_FEED": "An unknown feed, or a feed the model cannot take (no Orders-mode CREATE, no "
                     "such process-time table, an override path that does not apply).",
    "E_MESMAP_UNMAPPED": "The MES reports a work centre, product or operation the mapping covers "
                         "with nothing.",
    "W_MESMAP_UNUSED": "A mapped work centre or product the MES does not report.",
    "W_MES_THIN_DATA": "Too few observations in the window to refit a parameter; the model's own "
                       "value stands.",
    "W_MES_NAIVE_TIME": "An MES timestamp carries no UTC offset and was read as UTC.",
    "W_MES_TABLE_MISSING": "A REST MES source answered 404 for a table, which was read as empty.",
    # -- file level -----------------------------------------------------------
    "E_SCHEMA_VERSION": "The file's schema_version is newer than this build supports.",
}


def problem(code: str, message: str, path: str = "", *,
            block: str | None = None, hint: str | None = None) -> Problem:
    """Build a Problem, deriving severity from the code prefix."""
    return Problem(code=code, severity="warning" if code.startswith("W_") else "error",
                   message=message, path=path, block=block, hint=hint)
