"""CONVEYOR reverse during a run (conveyor system A, Task 9), written from the brief.

API assumptions (not from the implementation):
- CONVEYOR params: `reversible` (bool), `reverse_signal` (a named signal toggling the direction on
  each raise), `reverse_to` (the NAME OF A LINKED TARGET BLOCK that receives parts leaving at the
  start while reversed; the belt's other link, the end target, is the normal exit).
- Item code: `item.reverse("belt", forward)` inside a hook; forward False = reversed, True = forward
  again. Calling it with the state it already has is a no-op.
- Common model: belt 4.0 m, 1.0 m/s, spacing 0.5 (single parts never meet the spacing rule).
  Parts are entity type PART. A SIG creator raises the named signal "rev" through a SIGNAL block at
  its arrival times (start_time + k * interval); the signal instant is the arrival instant.
- Path taken is read from two counters n_end / n_back (ASSIGN before each DISPOSE); times from
  `entity.deletion_time` of the PART entities.
- Problems: validate_model_structured gives an error problem with code E_CONVEYOR_REVERSE and a
  non-empty plain message for a refused model.

Reverse rule: at the reverse instant t an item at x has remaining distance x (the distance it has
travelled) and moves toward the start at the belt speed.
"""
import copy
import math

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_divert_spec import belt, divert_at

from nagasim.core import schema
from nagasim.core.schema import validate_model_structured
from nagasim.settings import trustwrite

TOL = 1e-9


def _assign(name, counter):
    return {"name": name, "description": name, "type": "ASSIGN", "position": [1, 1],
            "params": {"assignments": [{"target": counter, "expr": counter + " + 1"}]}}


def _signal(name="fire", signal_name="rev"):
    return {"name": name, "description": name, "type": "SIGNAL", "position": [1, 2],
            "params": {"distribution": "Constant", "dist_params": {"value": 0},
                       "signal_name": signal_name}}


def _doc(part_times, signal_times):
    """Parts arrive at part_times, signals at signal_times (each: first time, then a constant
    interval). belt -> n_end -> dispose_1 (end); belt -> n_back -> dispose_back (reverse_to)."""
    conveyor = belt(length=4.0, speed=1.0, spacing=0.5)
    conveyor["params"].update({"reversible": True, "reverse_to": "n_back", "reverse_signal": "rev"})
    blocks = [conveyor, _assign("n_end", "n_end"), dispose(),
              _assign("n_back", "n_back"), dispose(name="dispose_back")]
    links = [{"source": "belt", "target": "n_end"}, {"source": "n_end", "target": "dispose_1"},
             {"source": "belt", "target": "n_back"}, {"source": "n_back", "target": "dispose_back"}]

    def feed(name, e_type, times, target):
        gap = (times[1] - times[0]) if len(times) > 1 else 1.0
        blocks.append(create(name=name, e_type=e_type, max_arrivals=len(times),
                             dist_params={"value": gap}, start_time=times[0]))
        links.append({"source": name, "target": target})

    feed("parts", "PART", part_times, "belt")
    if signal_times:
        feed("sig_src", "SIG", signal_times, "fire")
        blocks += [_signal(), dispose(name="sig_out")]
        links.append({"source": "fire", "target": "sig_out"})
    return layout(blocks, links, simulation={"time_limit": None, "warmup": 0, "replications": 1},
                  variables=[{"name": "n_end", "initial": 0}, {"name": "n_back", "initial": 0}])


def _observe(doc, horizon=30):
    env, _ = run_env(doc, horizon)
    times = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "PART")
    return times, env.ctx.variables["n_end"], env.ctx.variables["n_back"]


def _close(got, want):
    assert len(got) == len(want), got
    assert all(math.isclose(g, w, abs_tol=TOL) for g, w in zip(got, want)), got


def test_forward_only_control():
    """No signal ever: 4.0 m / 1 m/s, enters 0, exits the end at 4.0."""
    times, n_end, n_back = _observe(_doc([0.0], []))
    _close(times, [4.0])
    assert (n_end, n_back) == (1, 0)


def test_reverse_mid_run():
    """Enters 0 (x = t). Signal 1.5: x = 1.5, remaining distance becomes 1.5 m at 1 m/s, so it
    reaches the start at 1.5 + 1.5 = 3.0 and goes to reverse_to (n_back)."""
    times, n_end, n_back = _observe(_doc([0.0], [1.5]))
    _close(times, [3.0])
    assert (n_end, n_back) == (0, 1)


def test_reverse_twice():
    """Signals at 1.0 and 1.5 (start 1.0, interval 0.5). x = 1.0 at 1.0; reversed 0.5 s: x = 0.5
    at 1.5. Forward again: remaining 4.0 - 0.5 = 3.5, exit the end at 1.5 + 3.5 = 5.0."""
    times, n_end, n_back = _observe(_doc([0.0], [1.0, 1.5]))
    _close(times, [5.0])
    assert (n_end, n_back) == (1, 0)


def test_entry_is_refused_while_reversed():
    """A enters 0. Signals 1.5 (reverse) and 2.0 (forward). B is made at 1.7, while the belt is
    reversed, so it holds upstream. At 2.0 A is at 1.5 - 0.5 = 1.0 m; forward: remaining 3.0, exits
    at 5.0. B boards when the belt goes forward at 2.0 (A is 1.0 m ahead, spacing 0.5) and exits at
    2.0 + 4.0 = 6.0 on the end path. A B that boarded at 1.7 could not end at 6.0 there."""
    times, n_end, n_back = _observe(_doc([0.0, 1.7], [1.5, 2.0]))
    _close(times, [5.0, 6.0])
    assert (n_end, n_back) == (2, 0)


def test_two_parts_reversed_leave_the_start_in_spatial_order():
    """A enters 0, B enters 2.0. Signal 3.0: A at 3.0, B at 1.0. Reversed: B reaches the start after
    1.0 s, at 4.0; A after 3.0 s, at 6.0. Both go to reverse_to."""
    times, n_end, n_back = _observe(_doc([0.0, 2.0], [3.0]))
    _close(times, [4.0, 6.0])
    assert (n_end, n_back) == (0, 2)


# -- refusal at load ------------------------------------------------------------------

def _refusal_doc(extra):
    doc = _doc([0.0], [1.5])
    doc["schema_version"] = schema.CURRENT_VERSION
    conveyor = next(b for b in doc["blocks"] if b["name"] == "belt")
    conveyor["params"].update(extra.get("params", {}))
    doc["blocks"] += extra.get("blocks", [])
    doc["links"] += extra.get("links", [])
    return doc


def _codes(doc):
    return [p.code for p in validate_model_structured(doc)]


CASES = {
    "stops": {"params": {"stops": [{"at": 2.0, "time": 0}]}},
    "diverts": {"params": {"diverts": [divert_at(2.0)]},
                "blocks": [dispose(name="side")], "links": [{"source": "belt", "target": "side"}]},
    "merges": {"params": {"merges": [{"at": 2.0, "from": "feeder"}]},
               "blocks": [create(name="feeder", e_type="SIDE", max_arrivals=1, start_time=9)],
               "links": [{"source": "feeder", "target": "belt"}]},
    "zones": {"params": {"zones": {"count": 3, "release_delay": 1.0}}},
    "release": {"params": {"release": {"metering": {"gap": 0.5}}}},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_reversible_belt_with_a_mechanism_is_refused(case):
    doc = _refusal_doc(CASES[case])
    found = [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_REVERSE"]
    assert found, _codes(doc)
    assert found[0].severity == "error"
    assert found[0].message and found[0].message.isascii()


def test_a_non_reversible_belt_with_a_mechanism_is_not_refused():
    """The restriction is on reversible belts only: the same stops on a plain belt pass."""
    doc = _refusal_doc(CASES["stops"])
    params = next(b for b in doc["blocks"] if b["name"] == "belt")["params"]
    params["reversible"] = False
    assert "E_CONVEYOR_REVERSE" not in _codes(doc)


def test_a_plain_reversible_belt_is_accepted():
    doc = _refusal_doc({})
    assert "E_CONVEYOR_REVERSE" not in _codes(doc)
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"]


# -- reverse from item code -----------------------------------------------------------

REVERSER = ("class Rev:\n"
            "    def on_signal(self, name, item):\n"
            "        self.n = getattr(self, 'n', 0) + 1\n"
            "        item.reverse('belt', self.n % 2 == 0)\n")


@pytest.fixture
def trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path))
    trustwrite.trust_source(REVERSER, label="reverser", by="test")


def _coded(signal_times):
    """The belt has no reverse_signal; item code on the signal "rev" calls item.reverse: the
    1st raise reverses (forward False), the 2nd goes forward again."""
    doc = copy.deepcopy(_doc([0.0], signal_times))
    next(b for b in doc["blocks"] if b["name"] == "belt")["params"].pop("reverse_signal")
    doc["submodels"] = {"Rev": {"parameters": [], "blocks": [], "links": [],
                                "code": {"class": "Rev", "source": REVERSER, "signals": ["rev"]}}}
    doc["blocks"].append({"name": "rev_rule", "type": "SUBMODEL", "position": [0, 0],
                          "params": {"definition": "Rev", "arguments": {}}})
    doc["schema_version"] = schema.CURRENT_VERSION
    return doc


def test_item_code_reverses_mid_run(trusted):
    """Same numbers as test_reverse_mid_run: reverse at 1.5 from item code, leaves at the start 3.0."""
    times, n_end, n_back = _observe(_coded([1.5]))
    _close(times, [3.0])
    assert (n_end, n_back) == (0, 1)


def test_item_code_reverses_twice(trusted):
    """Same numbers as test_reverse_twice: reverse at 1.0, forward at 1.5, exits the end at 5.0."""
    times, n_end, n_back = _observe(_coded([1.0, 1.5]))
    _close(times, [5.0])
    assert (n_end, n_back) == (1, 0)
