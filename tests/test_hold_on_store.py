"""A HOLD whose condition reads a store (`NS`) is woken when the store changes.

The bundled `hold_on_store.json` feeds four cases at a time into a BATCH of
three, so the store's count moves more than once inside one instant. The times
below were followed by hand (site_docs/tutorials/hold-on-store.md) and are the
same whether the store wakes the HOLD when an item enters or when its put event
is processed: DEVLOG #344 moved that point, and 24 variants of this model
(batch 2/3/6, bursts of 1/4, thresholds 1-4) released their trucks at the same
times before and after."""
import copy
import json
from pathlib import Path

import pytest

from nagasim import api

MODEL = json.loads((Path(__file__).resolve().parents[1] / "nagasim" / "models"
                    / "hold_on_store.json").read_text(encoding="utf-8"))


def _truck_exits(layout):
    log = api.run_model(layout, replications=1, seed=0)["entity_log"]
    trucks = log[log["Type"] == "TRUCK"]
    return sorted(trucks["Exited At"].dropna().tolist())


def test_trucks_leave_when_two_cases_are_staged():
    assert _truck_exits(MODEL)[:6] == [10.0, 10.0, 14.0, 40.0, 40.0, 40.0]


@pytest.mark.parametrize("batch, burst, threshold, first_exits", [
    (2, 1, 1, [0.0, 7.0, 20.0, 21.0]),       # one case at a time: staged 1, packed at the second
    (3, 1, 2, [10.0, 10.0, 14.0, 40.0]),
    (3, 4, 1, [0.0, 7.0, 14.0, 30.0]),       # the case a burst leaves behind is enough
    (2, 4, 2, []),                           # bursts of 4 into packs of 2 leave nothing staged
])
def test_the_hold_sees_what_the_packer_leaves_behind(batch, burst, threshold, first_exits):
    layout = copy.deepcopy(MODEL)
    for block in layout["blocks"]:
        if block["name"] == "pack":
            block["params"]["batch_size"] = batch
        elif block["name"] == "cases_in":
            block["params"]["entities_per_arrival"] = burst
        elif block["name"] == "wait_for_cases":
            block["params"]["conditions"] = [f'NS("pack") >= {threshold}']
    assert _truck_exits(layout)[:4] == first_exits
