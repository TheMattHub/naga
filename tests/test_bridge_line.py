"""Plan D, Task 1: one palletize-and-wrap line of the bridge case, against a hand-derived cycle.

The expected value below is derived from the E3D timing rules, not from the builder. Do not
change it to make the test pass; change the builder.

Derivation of the station cycle (docs/history/E3D-CONVEYOR-TIMING-2026-11-21.md, bridge spike README):
  Rule (the hand-over the timing rigs measured): the wrapper is held from the moment a full
  pallet leaves the station until it reaches the out roller's eye, and the station is held
  until the wrapper is free. So one wrapper cycle = ride station -> wrap roller + wrap time
  + ride wrap roller -> out roller's eye, and nothing overlaps it (a single wrapper, a single
  station roller feeding it).
  Inputs: rollers 1.5 m at 0.5 m/s (spike.py V, ROLLER), wrap 60 s (WRAP_S, E3D ProWrapper:
  10 rotations x 6 s).
    ride 1 (station eye to wrap roller eye): 1.5 m / 0.5 m/s = 3.0 s
    wrap                                                       = 60.0 s
    ride 2 (wrap roller eye to out roller eye): 1.5 m / 0.5    =  3.0 s
    cycle = 3.0 + 60.0 + 3.0 = 66.0 s  ->  3600 / 66.0 = 54.55 pallets/h.
  E3D measured "a steady 66.6 s" (README). The 0.6 s difference is 0.3 s per ride of
  acceleration/handshake overhead (rig F: a 1.5 m pallet leg took 3.3 s, not 3.0 s). Built
  from 1.5 m rollers at 0.5 m/s the Naga line carries no such overhead, so the kinematic
  66.0 s is the oracle. The 2 % band (53.45 to 55.64 pallets/h) holds 66.0 s and E3D's 66.6 s
  (54.05/h) alike, and the counts 54 and 55; 65.6 s (spike's shortened last leg) also fits.

"A box supply that never starves the line", for one line: boxes arrive every 5 s (720/h, 180
  pallets/h of boxes), the robot places one per 6 s so 4 boxes take 24 s per pallet (150
  pallets/h), the wrapper takes 66 s per pallet (54.5/h). The robot is not the bottleneck by a
  factor 2.75 and the box supply outruns even the robot, so the boxes queue on the belt
  (the spike's Line limit of 4 holds the trunk) and the wrapper is always the limit. Empty
  pallets every 15 s (240/h) never starve the station either.

Measured: 1 h after a 10 min warm-up, one replication, as the written file's own
`simulation` section says; wrapped pallets = completed PALLET entities in `entity_summary`.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nagasim import api  # noqa: E402

# The export does not ship the builder: the module skips in the engine clone.
pytest.importorskip("tools.build_bridge_line")

WRAP_S = 60.0
ROLLER_M = 1.5
SPEED = 0.5
CYCLE_S = ROLLER_M / SPEED + WRAP_S + ROLLER_M / SPEED  # 66.0 s, see docstring
EXPECTED_PER_HOUR = 3600.0 / CYCLE_S                      # 54.545...


def test_one_line_cycles_at_the_hand_derived_station_cycle(tmp_path):
    from tools.build_bridge_line import build

    path = build(tmp_path, wrap_s=WRAP_S, lines=(1,))
    results = api.run_model(str(path), replications=1)
    summary = results["entity_summary"]
    row = summary[summary["Entity Type"] == "PALLET"]
    assert len(row) == 1, "expected one PALLET row in entity_summary"
    wrapped = float(row["Total Completed"].iloc[0])
    assert wrapped == pytest.approx(EXPECTED_PER_HOUR, rel=0.02)


# -- Task 2: the whole line (trunk, router, three lines) through the agent tools --------------------

SPIKE_PLAN = ROOT / "docs" / "history" / "bridge-spike-2026-11-21" / "plan.json"
Z0 = -10.0      # the trunk's z in Emulate3D (spike.py)
# Emulate3D item -> the builder's floor piece; sources, palletizer, wrapper and sink are no pieces.
PIECES = {"S_T1": "Trunk 1", "S_T2": "Trunk 2", "S_Div": "Divert"}
for _i in (1, 2, 3):
    if _i != 2:     # the middle line has one belt
        PIECES[f"L{_i}_Belt2"] = f"L{_i} Belt 2"
    PIECES.update({f"L{_i}_Belt": f"L{_i} Belt", f"L{_i}_Handoff": f"L{_i} Handoff", f"L{_i}_PalIn": f"L{_i} Pallet in",
                   f"L{_i}_Station": f"L{_i} Station", f"L{_i}_Wrap": f"L{_i} Wrap",
                   f"L{_i}_Out": f"L{_i} Out"})


def _floor_pose(item):
    """Emulate3D (x, y, z) location and yaw to the floor plan: x stays, y = -(z - Z0) (E3D's
    left of travel is +z, the floor's is north, -y), and the heading is the yaw (clockwise on
    screen once z is flipped)."""
    x, _y, z = (float(v) for v in item["location"].strip("()").split(","))
    yaw = float(item["rotation"].strip("()").split(",")[1])
    return x, -(z - Z0), yaw % 360.0


@pytest.fixture(scope="module")
def full_line(tmp_path_factory):
    from tools.build_bridge_line import build
    path = build(tmp_path_factory.mktemp("bridge"), wrap_s=WRAP_S)
    return json.loads(path.read_text(encoding="utf-8"))


def _blocks(layout):
    return {b["name"]: b for b in layout["blocks"]}


def test_the_built_line_has_the_spike_layout(full_line):
    blocks = _blocks(full_line)
    plan = json.loads(SPIKE_PLAN.read_text(encoding="utf-8"))
    placed = [item for item in plan["items"] if item["name"] in PIECES]
    assert len(placed) == len(PIECES)
    for item in placed:
        block = blocks[PIECES[item["name"]]]
        x, y, heading = _floor_pose(item)
        assert block["layout_position"] == pytest.approx([x, y], abs=1e-3), item["name"]
        assert block.get("layout_rotation", 0.0) % 360.0 == pytest.approx(heading, abs=1e-6)
    pieces = [b for b in full_line["blocks"] if b["type"] == "CONVEYOR"]
    assert all(b["params"]["speed"] == 0.5 for b in pieces)

    def length(name):
        return sum(s["straight"] for s in blocks[name]["params"]["path"])
    assert length("Trunk 1") == length("Trunk 2") == 1.8
    assert blocks["Trunk 2"]["params"]["release"] == {"metering": {"gap": 0.8}}
    diverts = blocks["Divert"]["params"]["diverts"]
    rule = diverts[0]["rule"]
    assert rule["kind"] == "round_robin" and rule["limit"] == 4
    assert [e.split(":")[1] for e in rule["exits"]] == ["L1 Handoff", "L2 Handoff", "L3 Handoff"]
    for i in (1, 2, 3):
        belts = [f"L{i} Belt"] + ([f"L{i} Belt 2"] if i != 2 else [])
        assert (f"L{i} Belt 2" in blocks) == (i != 2)
        for name in belts:
            assert length(name) == 1.8 and blocks[name]["params"]["zones"]["count"] == 3
        assert length(f"L{i} Handoff") == 0.6
        assert blocks[f"L{i} Palletizer"]["type"] == "BATCH"
        for gate, target in (("In gate", "Wrap"), ("Station gate", "Out"), ("Robot gate", "Wrap")):
            block = blocks[f"L{i} {gate}"]
            assert block["type"] == "CONVEYOR_TRANSFER" and block.get("description"), gate
            assert block["params"]["rule"] == {"kind": "fixed", "limit": 1,
                                               "exits": [f"straight:L{i} {target}"]}
        for name in ("Pallet in", "Station", "Wrap", "Out"):
            assert blocks[f"L{i} {name}"]["params"]["type"] == "pallet" and length(f"L{i} {name}") == 1.5
    # the trunk's only source; the lines take their boxes from the router
    creates = [b for b in full_line["blocks"] if b["type"] == "CREATE"]
    assert sorted(b["params"]["e_type"] for b in creates) == ["BOX", "PALLET", "PALLET", "PALLET"]


def test_the_built_line_checks_clean_under_strict_checks(full_line):
    from nagasim.core.conveyor_checks import static_checks
    assert full_line["simulation"].get("strict_checks") is True
    assert static_checks(full_line) == []


def test_a_short_pallet_warns_on_every_pallet_piece_after_its_create(full_line):
    """The 0.41 m box reaches the pallet rollers only batched onto a pallet, so it is never
    warned there (D1481). A pallet shortened to 0.5 m (< 3 x 0.1905 = 0.5715 m) warns on the
    two pieces before each palletizer and the two after it: 12."""
    import copy
    from nagasim.core.conveyor_checks import roller_pitch_problems
    doc = copy.deepcopy(full_line)
    for row in doc["entities"]:
        if row["name"].upper() == "PALLET":
            row["size"]["length"] = 0.5
    assert sorted(p.block for p in roller_pitch_problems(doc)) == sorted(
        f"L{n} {belt}" for n in (1, 2, 3) for belt in ("Pallet in", "Station", "Wrap", "Out"))


def test_the_builder_writes_the_same_file_twice(tmp_path):
    from tools.build_bridge_line import build
    first = build(tmp_path / "a", wrap_s=70.0).read_bytes()
    second = build(tmp_path / "b", wrap_s=70.0).read_bytes()
    assert first == second


def _spike_constants() -> dict:
    """spike.py's module-level number constants, read with ast (importing it reads sys.argv);
    WRAP_S is its default, the wrap of 10 rotations x 6 s plus 10 s cut and clamp."""
    import ast
    tree = ast.parse((SPIKE_PLAN.parent / "spike.py").read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                    out[target.id] = node.value.value
                elif isinstance(target, ast.Name) and isinstance(node.value, ast.IfExp):
                    out[target.id] = node.value.orelse.value
    return out


def test_the_builders_inputs_are_the_spikes(full_line):
    """M2 (checkpoint 1 review): every input the builder and the built file carry is spike.py's
    own, so no number can drift from the bridge spike unseen."""
    import tools.build_bridge_line as B
    spike = _spike_constants()
    for name in ("V", "BOX_EVERY", "PALLET_EVERY", "ROBOT_S", "LIMIT", "BELT", "HANDOFF",
                 "ROLLER", "EYE_BACK"):
        assert getattr(B, name) == spike[name], name
    assert B.DIVERT_LEN == spike["DIVERT"]
    assert spike["WRAP_S"] == 70.0
    source = (SPIKE_PLAN.parent / "spike.py").read_text(encoding="utf-8")
    assert "S_T2|MeteringGap|0.8" in source and B.METERING_GAP == 0.8
    assert '"batch_size": 4' in source and B.BOXES_PER_PALLET == 4
    blocks = _blocks(full_line)
    assert blocks["Boxes"]["params"]["dist_params"]["value"] == spike["BOX_EVERY"]
    assert full_line["simulation"]["warmup"] == 600 and full_line["simulation"]["time_limit"] == 4200
    for i in (1, 2, 3):
        assert blocks[f"L{i} Empty pallets"]["params"]["dist_params"]["value"] == spike["PALLET_EVERY"]
        palletizer = blocks[f"L{i} Palletizer"]["params"]
        assert float(palletizer["member_time"]) == spike["ROBOT_S"]
        assert palletizer["batch_size"] == 4 and float(palletizer["carrier_capacity"]) == 4
        assert blocks[f"L{i} Wrap"]["params"]["stops"] == [
            {"at": spike["ROLLER"] - spike["EYE_BACK"], "time": WRAP_S}]
    rule = blocks["Divert"]["params"]["diverts"][0]["rule"]
    assert rule["limit"] == spike["LIMIT"]


def test_the_wrap70_file_wraps_70_s(tmp_path):
    import tools.build_bridge_line as B
    layout = json.loads(B.build(tmp_path, wrap_s=_spike_constants()["WRAP_S"]).read_text(encoding="utf-8"))
    assert {b["params"]["stops"][0]["time"] for b in layout["blocks"]
            if b["name"].endswith(" Wrap")} == {70.0}


# -- Task 3: the acceptance run ---------------------------------------------------------------------
# Thresholds come from the plan's brief, not from what the builder reported: Emulate3D wrapped 162
# pallets/h at 60 s (54 per line, three lines) and the spike's Naga model 142 at 70 s; the band is
# 5 %. Both runs read the committed files, 1 h after the 10 min warm-up, one replication.

BRIDGE_DIR = ROOT / "docs" / "history" / "bridge-line-2026-11-25"
E3D_WRAP_60 = 162.0
E3D_PER_LINE = 54.0
SPIKE_NAGA_WRAP_70 = 142.0


@pytest.fixture(scope="module")
def committed_runs():
    return {name: api.run_model(str(BRIDGE_DIR / f"bridge_line{name}.json"), replications=1)
            for name in ("", "_wrap70")}


def _wrapped_total(results) -> float:
    summary = results["entity_summary"]
    return float(summary[summary["Entity Type"] == "PALLET"]["Total Completed"].iloc[0])


def test_the_committed_files_are_what_the_builder_writes(tmp_path):
    """The acceptance tests read the committed files; a drifted builder must not pass unseen."""
    from tools.build_bridge_line import build
    for wrap, name in ((60.0, "bridge_line.json"), (70.0, "bridge_line_wrap70.json")):
        fresh = build(tmp_path / name, wrap_s=wrap).read_bytes()
        assert fresh == (BRIDGE_DIR / name).read_bytes(), name


def test_wrap_60_lands_within_5_percent_of_162(committed_runs):
    """Wrapped pallets per hour: the total is `entity_summary` PALLET "Total Completed" (a pallet
    completes when the Out roller hands it to the sink); each line's count is the `Exited` column
    of `conveyor_summary` for that line's "Lx Out" belt, the last piece before the sink."""
    results = committed_runs[""]
    total = _wrapped_total(results)
    assert E3D_WRAP_60 * 0.95 <= total <= E3D_WRAP_60 * 1.05
    belts = results["conveyor_summary"].set_index("Conveyor")
    per_line = [float(belts.loc[f"L{i} Out", "Exited"]) for i in (1, 2, 3)]
    for i, count in enumerate(per_line, 1):
        assert E3D_PER_LINE * 0.95 <= count <= E3D_PER_LINE * 1.05, f"line {i}: {count}"
    assert sum(per_line) == total


def test_wrap_70_lands_within_5_percent_of_142(committed_runs):
    total = _wrapped_total(committed_runs["_wrap70"])
    assert SPIKE_NAGA_WRAP_70 * 0.95 <= total <= SPIKE_NAGA_WRAP_70 * 1.05


def test_the_router_limit_binds(committed_runs):
    """The divert's limit of 4 under way per line holds a box at least once: frame `route_holds`,
    row Block "Divert", column "Holds" (loads held with every exit, i.e. all three lines, at its
    limit) and "Hold %" (the share of the hour a box was held). Both wraps, since the line is
    saturated either way."""
    for name, results in committed_runs.items():
        row = results["route_holds"].set_index("Block").loc["Divert"]
        assert row["Holds"] >= 1 and row["Hold %"] > 0, name
