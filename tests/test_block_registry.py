"""BLOCK_REGISTRY is the single source of truth for constructible block types
(class + factory + param schema), replacing GraphModel's old type_map lambda
table (ARCHITECTURE D11)."""
import json
from pathlib import Path

import pytest

from nagasim.core.base_process import BaseProcess, Block
from nagasim.core.block_registry import BLOCK_REGISTRY, validate_params
import numpy as np
from nagasim.core.distributions import (
    ARRIVAL_DISTRIBUTIONS, DELAY_DISTRIBUTIONS, DISTRIBUTION_PARAM_HINTS,
    DISTRIBUTION_REGISTRY, DISTRIBUTION_USE_CASES, SPECIAL_PURPOSE_DISTRIBUTIONS,
    StochasticDistribution, _rebuild_derived_tables, get_available_distributions,
    get_distribution_params,
)

MODELS_DIR = Path(__file__).resolve().parents[1] / "nagasim" / "models"


def test_every_registry_entry_constructs_and_roundtrips_its_default_params(env):
    """Meta-test: a block type added to BLOCK_REGISTRY without a matching,
    working schema fails right here. One loop over all 17 types rather than 17
    parametrized tests -- each case is microseconds of work next to pytest's
    per-test overhead, and the assertion names every broken type at once."""
    problems = []
    for type_name, spec in BLOCK_REGISTRY.items():
        if not issubclass(spec.cls, BaseProcess) or spec.type_name != type_name:
            problems.append(f"{type_name}: bad registry entry ({spec.cls}, {spec.type_name})")
            continue
        block = Block(position=(0, 0), type=type_name, name=f"{type_name.lower()}_rt")
        process = spec.factory(env, block)
        if not isinstance(process, spec.cls):
            problems.append(f"{type_name}: factory returned {type(process)}")
            continue

        defaults = {p.name: p.default for p in spec.params}
        try:
            process.set_params_from_dict(defaults)
        except Exception as exc:                     # noqa: BLE001 - report, don't stop
            problems.append(f"{type_name}: defaults rejected: {exc}")
            continue

        extra = set(process.get_params_dict()) - {p.name for p in spec.params}
        if extra:
            problems.append(f"{type_name}: params not in the schema: {sorted(extra)}")

    assert not problems, "\n  ".join([""] + problems)


def test_validate_params_flags_unknown_key_and_wrong_type():
    problems = validate_params("CREATE", {
        "e_type": "DEFAULT",
        "distribution": "Constant",
        "dist_params": {"value": 1.0},
        "entities_per_arrival": 1,
        "max_arrivals": "five",     # wrong type: should be int
        "start_time": 0,
        "not_a_real_param": 1,      # unknown key
    })
    assert any(p.startswith("unknown key") for p in problems)
    assert any(p.startswith("wrong type") for p in problems)


def test_validate_params_accepts_schema_defaults():
    for type_name, spec in BLOCK_REGISTRY.items():
        defaults = {p.name: p.default for p in spec.params}
        assert validate_params(type_name, defaults) == []


def test_validate_params_never_raises_on_unknown_type():
    assert validate_params("NOT_A_TYPE", {"x": 1}) == ["unknown block type: NOT_A_TYPE"]


def test_example_models_pass_validation_with_zero_problems():
    found = []
    for model_path in sorted(MODELS_DIR.glob("*.json")):
        layout = json.loads(model_path.read_text())
        for blk in layout["blocks"]:
            problems = validate_params(blk["type"].upper(), blk.get("params", {}))
            found += [f"{model_path.name} block '{blk['name']}': {p}" for p in problems]
    assert not found, "\n  ".join([""] + found)


def test_removing_a_block_type_leaves_the_rest_of_the_registry_working(qapp):
    """ARCHITECTURE 10.3 rule 7 (removal test): BLOCK_REGISTRY-driven machinery
    (validate_params for every other type, the Qt palette) must not assume a
    fixed set of block types -- deleting one entry should not break anything
    that reads the others."""
    # product.ui is product, not engine; skip cleanly under the engine wheel.
    PaletteTree = pytest.importorskip("product.ui.palette").PaletteTree

    removed = BLOCK_REGISTRY.pop("DUPLICATE")
    try:
        # validate_params still works for every remaining type.
        for type_name, spec in BLOCK_REGISTRY.items():
            defaults = {p.name: p.default for p in spec.params}
            assert validate_params(type_name, defaults) == []
        assert validate_params("DUPLICATE", {}) == ["unknown block type: DUPLICATE"]

        # the palette is built straight from BLOCK_REGISTRY (ui/palette.py) --
        # one fewer entry means one fewer item, not a crash.
        palette = PaletteTree()
        listed = {row.text(0) for row in palette.rows("block")}
        assert listed == set(BLOCK_REGISTRY)
    finally:
        BLOCK_REGISTRY["DUPLICATE"] = removed


def test_family_and_shape_are_derived_and_match_the_pre_b3_snapshot():
    """ARCH-REVIEW B3 (D35): family and shape used to be hand-kept lists in ui/style.py/ui/palette.py, a second place to
    edit -- and forget -- when a block is added. They now live on each type's
    own BlockSpec (block_spec.BlockSpec.family/.shape) and
    ui/style.BLOCK_FAMILY/_SHAPES derive from the
    registry. This snapshots the exact literals they replaced, so a
    derivation bug (not just a spec missing a family) fails loud."""
    style = pytest.importorskip("product.ui.style")

    expected_family = {
        "CREATE": "source", "SUB_ENTRY": "source",
        "DISPOSE": "sink", "SUB_EXIT": "sink",
        "PROCESS": "work", "DELAY": "work", "MOTION": "work",
        "SEIZE": "resource", "RELEASE": "resource",
        "DECIDE": "control", "ROUTE": "control", "HOLD": "control", "SIGNAL": "control",
        "LOGIC": "control", "SEND": "control",
        "BATCH": "group", "SEPARATE": "group", "DUPLICATE": "group", "MATCH": "group",
        "RECORD": "stats",
        "STATION": "move", "TRANSFER": "move", "TRANSPORT": "move", "CONVEYOR": "move",
        "BUFFER": "move",
        "ASSIGN": "data",
        "SUBMODEL": "container",
    }
    expected_shapes = {"CREATE": "pill", "DISPOSE": "cap", "DECIDE": "diamond", "PROCESS": "double",
                        "MOTION": "motion",
                        "SUB_ENTRY": "pill", "SUB_EXIT": "cap", "SUBMODEL": "submodel",
                        "STATION": "pill", "ROUTE": "diamond"}
    expected_type_names = {
        "CREATE", "DISPOSE", "ASSIGN", "DECIDE", "SEIZE", "PROCESS", "MOTION", "DELAY", "RELEASE",
        "BATCH", "MATCH", "SEPARATE", "DUPLICATE", "HOLD", "SIGNAL", "LOGIC", "RECORD",
        "STATION", "ROUTE", "TRANSFER", "TRANSPORT", "CONVEYOR", "BUFFER", "SEND",
        "SUBMODEL", "SUB_ENTRY", "SUB_EXIT",
    }

    assert style.BLOCK_FAMILY == expected_family
    assert style._SHAPES == expected_shapes
    assert set(BLOCK_REGISTRY) == expected_type_names


def test_behaviour_flags_derive_the_pre_e2_hand_kept_type_sets():
    """ARCH-REVIEW E2: _DURATION_BLOCK_TYPES (core/schema/layout.py),
    HOLDING_TYPES/SERVICE_TYPES (core/initial_state.py), TRANSPARENT_TYPES
    (core/blocking.py), QUEUEING_BLOCK_TYPES (core/seizing.py) and
    _RESOURCE_BLOCKS (ui/tables/resources.py) used to be hand-kept per-type-name tuples,
    a second (and third, and fourth...) place to edit -- and forget -- when a
    block's behaviour changes. They now derive from each block's own
    BlockSpec.duration/.holding/.service/.transparent/.queueing/.uses_resource
    flag (block_registry.*_types()). This snapshots the exact literals they
    replaced, so a derivation bug fails loud."""
    from nagasim.core import block_registry, blocking, initial_state, seizing

    expected_duration = {"CREATE", "DELAY", "SEIZE", "PROCESS", "MOTION", "RELEASE", "DISPOSE",
                         "SIGNAL", "ROUTE"}
    expected_holding = {"PROCESS", "MOTION", "SEIZE", "DELAY", "HOLD", "BATCH", "MATCH", "BUFFER",
                        "SEPARATE", "DUPLICATE", "SIGNAL", "LOGIC"}
    expected_service = {"PROCESS", "MOTION", "SEIZE", "DELAY"}
    expected_transparent = {"ASSIGN", "RECORD", "DECIDE", "STATION", "LOGIC"}
    expected_queueing = {"SEIZE", "PROCESS", "MOTION"}
    expected_resource = {"SEIZE", "RELEASE", "PROCESS", "MOTION"}

    assert set(block_registry.duration_types()) == expected_duration
    assert set(block_registry.holding_types()) == expected_holding
    assert set(block_registry.service_types()) == expected_service
    assert set(block_registry.transparent_types()) == expected_transparent
    assert set(block_registry.queueing_types()) == expected_queueing
    assert set(block_registry.resource_block_types()) == expected_resource

    # And every consumer actually picked up the derived value.
    assert set(initial_state.HOLDING_TYPES) == expected_holding
    assert set(initial_state.SERVICE_TYPES) == expected_service
    assert set(blocking.TRANSPARENT_TYPES) == expected_transparent
    assert set(seizing.QUEUEING_BLOCK_TYPES) == expected_queueing

    tables = pytest.importorskip("product.ui.tables.resources")
    assert set(tables._RESOURCE_BLOCKS) == expected_resource


def test_every_registered_spec_has_a_family_and_shape_known_to_the_ui():
    """A plugin (or a new built-in) that forgets `family`/`shape` on its
    BlockSpec must still render, not crash -- ui/style.py falls back to a
    neutral family/shape (see BLOCK_FAMILY's derivation), but every spec is
    still expected to name a family, and whatever shape it names (or falls
    back to) must be one ui/style.block_path actually draws."""
    style = pytest.importorskip("product.ui.style")
    known_shapes = {"rect", "pill", "cap", "diamond", "double", "submodel", "motion"}

    for type_name, spec in BLOCK_REGISTRY.items():
        assert spec.family, f"{type_name}: BlockSpec has no family"
        assert spec.family in style.FAMILY_FILLS, \
            f"{type_name}: family {spec.family!r} unknown to ui/style.FAMILY_FILLS"
        assert style.shape_of(type_name) in known_shapes, \
            f"{type_name}: shape {style.shape_of(type_name)!r} unknown to ui/style.block_path"


# ======================= DISTRIBUTION_REGISTRY ============================

# -- snapshot of the old hand-written tables (core/distributions.py, pre-2.4 A) --

_OLD_ARRIVAL = [
    "Constant", "Exponential", "Lognormal", "Gamma", "Weibull",
    "Triangular", "Uniform", "Beta", "Erlang", "LogLogistic",
    "InverseGaussian", "Rayleigh", "Poisson", "EmpiricalContinuous", "EmpiricalDiscrete",
    "FixedSchedule", "CustomExpression",
]

_OLD_DELAY = [
    "Constant", "BoundedNormal", "Exponential", "Lognormal", "Gamma", "Weibull", "DiscreteUniform",
    "Triangular", "Uniform", "Beta", "Erlang", "LogLogistic", "Poisson",
    "InverseGaussian", "Rayleigh", "PearsonTypeV", "PearsonTypeVI", "EmpiricalContinuous",
    "EmpiricalDiscrete", "CustomExpression",
]

_OLD_SPECIAL_PURPOSE = [
    "Normal", "BoundedNormal", "JohnsonSU", "JohnsonSB", "Cauchy", "ChiSquare",
    "Bernoulli", "Binomial", "Geometric", "NegativeBinomial",
    "DiscreteUniform", "Hypergeometric",
]

_OLD_PARAMS = {
    "Constant": ["value"], "Uniform": ["low", "high"], "Exponential": ["rate"],
    "BoundedNormal": ["mean", "stddev", "min", "max"], "Normal": ["mean", "stddev"],
    "Lognormal": ["mu", "sigma"], "Triangular": ["low", "high", "mode"],
    "Gamma": ["alpha", "beta"], "Weibull": ["scale", "shape"],
    "Beta": ["min", "max", "alpha", "beta"], "Erlang": ["k", "rate"],
    "Cauchy": ["x0", "gamma"], "InverseGaussian": ["mu", "lambda"], "Rayleigh": ["sigma"],
    "ChiSquare": ["df"], "LogLogistic": ["c", "scale"],
    "JohnsonSU": ["a", "b", "loc", "scale"], "JohnsonSB": ["a1", "a2", "min", "max"],
    "PearsonTypeV": ["a", "scale"], "PearsonTypeVI": ["a", "b", "scale"],
    "Bernoulli": ["p"], "Binomial": ["n", "p"], "Geometric": ["p"], "Poisson": ["lambda"],
    "NegativeBinomial": ["r", "p"], "DiscreteUniform": ["low", "high"],
    "Hypergeometric": ["N", "K", "n"],
    "EmpiricalContinuous": ["values", "probs"], "EmpiricalDiscrete": ["values", "probs"],
    "FixedSchedule": ["schedule"], "CustomExpression": ["expression"],
}

# The old DISTRIBUTION_USE_CASES/DISTRIBUTION_PARAM_HINTS tables (31 entries
# each) are large; rather than duplicate every string here (a second place
# to drift), this test checks the *names* match exactly (nothing silently
# dropped or added) and that every value still ends in the distribution's
# own param list, the invariant the old tables held by construction.


def test_every_registered_distribution_samples_with_its_defaults():
    rng = np.random.default_rng(0)
    for name, spec in DISTRIBUTION_REGISTRY.items():
        defaults = {p.name: p.default for p in spec.params}
        dist = StochasticDistribution(name, defaults)
        dist.sample(rng)   # must not raise


def test_arrival_delay_special_purpose_lists_match_the_old_tables():
    assert set(ARRIVAL_DISTRIBUTIONS) == set(_OLD_ARRIVAL)
    assert set(DELAY_DISTRIBUTIONS) == set(_OLD_DELAY)
    assert set(SPECIAL_PURPOSE_DISTRIBUTIONS) == set(_OLD_SPECIAL_PURPOSE)


def test_get_distribution_params_matches_the_old_mapping():
    for name, expected in _OLD_PARAMS.items():
        assert get_distribution_params(name) == expected


def test_distribution_use_cases_and_hints_cover_exactly_the_old_names():
    old_names = set(_OLD_PARAMS)
    assert set(DISTRIBUTION_USE_CASES) == old_names
    assert set(DISTRIBUTION_PARAM_HINTS) == old_names
    # every hint is still literally the use case's own last line, as before
    for name in old_names:
        assert DISTRIBUTION_PARAM_HINTS[name] == DISTRIBUTION_USE_CASES[name].rsplit("\n", 1)[-1]


def test_get_available_distributions_by_block_type():
    assert get_available_distributions("CREATE") is ARRIVAL_DISTRIBUTIONS
    for block_type in ("DELAY", "SEIZE", "RELEASE", "SIGNAL", "DISPOSE"):
        assert get_available_distributions(block_type) is DELAY_DISTRIBUTIONS
    everything = get_available_distributions()
    assert set(everything) == set(ARRIVAL_DISTRIBUTIONS) | set(DELAY_DISTRIBUTIONS) | set(SPECIAL_PURPOSE_DISTRIBUTIONS)


def test_removing_a_distribution_leaves_the_rest_of_the_registry_working(qapp):
    """ARCHITECTURE 10.3 rule 7 (removal test), mirrored from
    test_block_registry.py's block-registry version."""
    # product.ui is product, not engine; skip cleanly under the engine wheel.
    _ui_editors = pytest.importorskip("product.ui.editors")
    DistributionEditor, EditorHost = _ui_editors.DistributionEditor, _ui_editors.EditorHost

    removed = DISTRIBUTION_REGISTRY.pop("Uniform")
    try:
        _rebuild_derived_tables()
        assert "Uniform" not in ARRIVAL_DISTRIBUTIONS
        assert get_distribution_params("Uniform") == []

        # sampling and validation still work for every remaining distribution
        rng = np.random.default_rng(0)
        for name, spec in DISTRIBUTION_REGISTRY.items():
            defaults = {p.name: p.default for p in spec.params}
            StochasticDistribution(name, defaults).sample(rng)

        # the distribution editor still builds for a remaining distribution
        host = EditorHost(document=_FakeDocument(), on_commit=lambda *_: None)
        editor = DistributionEditor(host, "DELAY", "some_block", "distribution",
                                    initial={"distribution": "Exponential", "dist_params": {"rate": 1.0}})
        assert editor.combo.currentText() == "Exponential"
    finally:
        DISTRIBUTION_REGISTRY["Uniform"] = removed
        _rebuild_derived_tables()


class _FakeDocument:
    """Just enough of ModelDocument for DistributionEditor's constructor."""
    variables = []
    simulation = {}
