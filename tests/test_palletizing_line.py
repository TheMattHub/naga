"""The bundled palletizing line: every part it shows does its job in a headless run."""
import json
from pathlib import Path

import pytest

from nagasim.api import run_model

MODEL = Path(__file__).resolve().parents[1] / "nagasim" / "models" / "palletizing_line.json"


@pytest.fixture(scope="module")
def run():
    model = json.loads(MODEL.read_text(encoding="utf-8"))
    return model, run_model(model, seed=0)


def flow(results, source, target):
    f = results["material_flow_summary"]
    return int(f[(f["From"] == source) & (f["To"] == target)]["Entities"].sum())


def test_the_router_code_sends_every_box_to_a_line_and_skips_the_slow_one(run):
    model, results = run
    boxes = model["blocks"][0]["params"]["max_arrivals"]
    lines = [flow(results, "router/route", f"to_line_{k}") for k in (1, 2, 3)]
    assert sum(lines) == boxes
    assert lines[2] < lines[0] == lines[1]          # hand packing is skipped at its limit


def test_each_full_pallet_holds_the_pattern_and_the_robot_picks_from_the_belt(run):
    model, results = run
    per_pallet = sum(len(model["pallet_patterns"][0]["layers"][s["layer"]])
                     for s in model["pallet_patterns"][0]["stack"])
    assert per_pallet == 8
    assert flow(results, "palletizer", "full_pallets") == flow(results, "pallet_belt", "palletizer") // per_pallet
    assert flow(results, "pick_belt", "pick_robot") == flow(results, "pick_robot", "picked") > 0


def test_the_photo_eye_sees_every_box(run):
    model, results = run
    eyes = results["conveyor_eyes"]
    assert int(eyes[eyes["Conveyor"] == "infeed"]["Fired"].sum()) == model["blocks"][0]["params"]["max_arrivals"]
