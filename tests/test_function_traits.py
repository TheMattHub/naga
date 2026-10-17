"""ARCH-REVIEW B2: every hand-kept function-name set is derived from
`expressions.TRAITS`. These literals are the sets as they stood before the
derivation; a trait edit that changes one must change it here too, on purpose."""
import math

from nagasim.core import agents, expressions, levels, sd, units
from nagasim.core.blocks import hold

# NEAR_HEADING joined the family with #254 (it lacked "dynamic" and "population").
_NEAR = ("NEAR", "NEAREST", "NEAR_SUM", "NEAR_MEAN", "NEAR_MIN", "NEAR_MAX", "NEAR_HEADING")
_MATH = ("min", "max", "abs", "round", "int", "float", "sqrt", "exp", "log", "log10", "floor", "ceil")


def test_derived_sets_match_the_snapshot():
    assert levels._SAMPLER_CALLS == {"RAND", "EXPO", "UNIF", "NORM", "TRIA", "DISC"}
    assert levels._BUS_CALLS == {"FLOW", "FLOWSUM"}
    assert levels._DYNAMIC_CALLS == {"LEVEL", "FRACTION", "RAMP", "DELAY", "TS", "COUNT", "SUM",
                                     "MEAN", *_NEAR, "KNN_SUM", "KNN_MEAN"}
    assert agents._SAMPLER_CALLS == levels._SAMPLER_CALLS
    assert agents._HELD_CALLS == {"LEVEL", "FLOW", "FLOWSUM", "CONV", "FRACTION", "AUX", "DELAY",
                                  "NQ", "NR", "MR", "AVAILABLE", "IDLE", "NS", "POWER"}
    assert agents._GRID_CALLS == {"PATCH", "NEIGHBOURS", "NEIGHBOUR_SUM", "NEIGHBOUR_MEAN", "CELL_COUNT"}
    assert agents._LINK_CALLS == {"LINKED", "LINK_SUM", "LINK_MEAN", "IS_LINKED"}
    assert agents._PER_AGENT == (levels._SAMPLER_CALLS | agents._GRID_CALLS | agents._LINK_CALLS
                                 | {*_NEAR, "KNN_SUM", "KNN_MEAN"})
    assert agents._PURE == {*_MATH, "IF", "SIN", "COS", "TAN", "ATAN2", "SQRT", "PI", "DEG", "RAD"}
    assert agents._VEC_CALLS == {"SIN": math.sin, "COS": math.cos, "TAN": math.tan,
                                 "ATAN2": math.atan2, "SQRT": math.sqrt, "sqrt": math.sqrt,
                                 "DEG": math.degrees, "RAD": math.radians, "abs": abs}
    assert hold._STEADY_CALLS == {*_MATH, "TABLE", "DISTANCE"}
    assert hold._WATCHED_CALLS == {"VAR": "var", "NQ": "res", "NR": "res", "MR": "res",
                                   "AVAILABLE": "res", "IDLE": "res", "NS": "store", "LEVEL": "level"}
    assert units._LITERAL_LOOKUPS == {"LEVEL": "level_unit", "AUX": "aux_unit", "DELAY": "delay_unit"}
    assert units._DELAY_MACROS == ("SMOOTH", "SMOOTH3", "DELAY1", "DELAY3", "DELAYN", "DELAYP")
    assert sd.MACROS == {"SMOOTH": ("information", 1), "SMOOTH3": ("information", 3),
                         "DELAY1": ("material", 1), "DELAY3": ("material", 3),
                         "DELAYN": ("material", None), "DELAYP": ("pipeline", 1)}
    # Ordered: `_compile` fills `refs` in this order.
    assert expressions._NAMED_LOOKUPS == (
        "TS", "LEVEL", "FLOW", "CONV", "VAR", "AUX", "DELAY", "COUNT", "SUM", "MEAN", "AGENT",
        *_NEAR, "KNN_SUM", "KNN_MEAN", "NEIGHBOURS", "NEIGHBOUR_SUM",
        "NEIGHBOUR_MEAN", "CELL_COUNT", "LINKED", "POWER")
    assert expressions._STATE_LOOKUPS == ("NQ", "NR", "MR", "AVAILABLE", "IDLE", "NS")
    # schema.validate_layout_structured's population tuple, as it was spelled out.
    assert expressions.functions_with("population") == (
        "COUNT", "SUM", "MEAN", "AGENT", *_NEAR, "KNN_SUM", "KNN_MEAN", "NEIGHBOURS",
        "NEIGHBOUR_SUM", "NEIGHBOUR_MEAN", "CELL_COUNT", "LINKED")


def test_traits_tag_real_functions_only():
    assert set(expressions.TRAITS) <= expressions.FUNCTION_NAMES
