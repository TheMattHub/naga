"""A chain of reversible CONVEYORs reverses as one (conveyor plan A2, Task 7), from the brief.

API assumptions (not from the implementation; same builders as test_conveyor_reverse.py):
- A chain = reversible CONVEYORs linked directly end to start (A -> B, no block between).
- `reverse_to` is given on the chain's FIRST piece (A) and names one of A's linked targets
  (here the block n_back); A's other link is the chain's internal end target B. B's own end link
  (to n_end) is the chain's normal exit.
- `reverse_signal` on any member (A or B) toggles the whole chain at the same instant.
  `item.reverse("A" | "B", forward)` from item code does the same.
- Pieces: A 4 m, B 4 m, both 1 m/s, spacing 0.5; the chain is 8 m. A part entering A at time e is
  at chain position x = t - e while forward (x < 4 on A, x > 4 on B). Parts are points.
- At a reverse instant t a part at x moves toward the chain start at 1 m/s and, when reversed,
  crosses B -> A at the joint in the running direction; it leaves A's start into reverse_to at
  t + x. Back to forward, a part at x leaves B's end after (8 - x) more.
- Entry at A's start is refused while reversed (upstream holds), as on a single belt.
- Refusals: validate_model_structured gives an error with code E_CONVEYOR_REVERSE.
All derivations are in the docstrings and in the A2 sdd spec-tests/task-7-derivations.md.
"""
import copy

import pytest

from conftest import create, dispose, layout, run_env
from test_conveyor_divert_spec import belt
from test_conveyor_reverse import _assign, _close, _signal

from nagasim.core import schema
from nagasim.core.schema import validate_model_structured
from nagasim.settings import trustwrite


def _chain_doc(part_times, signal_times, signal_on="A"):
    """Parts arrive at part_times, signals at signal_times (first time, constant interval).
    parts -> A -> B -> n_end -> dispose_1 (chain end); A -> n_back -> dispose_back (reverse_to)."""
    a = belt(name="A", length=4.0, speed=1.0, spacing=0.5)
    b = belt(name="B", length=4.0, speed=1.0, spacing=0.5)
    a["params"].update({"reversible": True, "reverse_to": "n_back"})
    b["params"].update({"reversible": True})
    if signal_on:
        (a if signal_on == "A" else b)["params"]["reverse_signal"] = "rev"
    blocks = [a, b, _assign("n_end", "n_end"), dispose(),
              _assign("n_back", "n_back"), dispose(name="dispose_back")]
    links = [{"source": "A", "target": "B"}, {"source": "B", "target": "n_end"},
             {"source": "n_end", "target": "dispose_1"},
             {"source": "A", "target": "n_back"}, {"source": "n_back", "target": "dispose_back"}]

    def feed(name, e_type, times, target):
        gap = (times[1] - times[0]) if len(times) > 1 else 1.0
        blocks.append(create(name=name, e_type=e_type, max_arrivals=len(times),
                             dist_params={"value": gap}, start_time=times[0]))
        links.append({"source": name, "target": target})

    feed("parts", "PART", part_times, "A")
    if signal_times:
        feed("sig_src", "SIG", signal_times, "fire")
        blocks += [_signal(), dispose(name="sig_out")]
        links.append({"source": "fire", "target": "sig_out"})
    return layout(blocks, links, simulation={"time_limit": None, "warmup": 0, "replications": 1},
                  variables=[{"name": "n_end", "initial": 0}, {"name": "n_back", "initial": 0}])


def _observe(doc, horizon=40):
    env, _ = run_env(doc, horizon)
    times = sorted(e.deletion_time for e in env.ctx.all_entities if e.e_type == "PART")
    return times, env.ctx.variables["n_end"], env.ctx.variables["n_back"]


def test_plain_chain_runs_forward():
    """Control, no signal: a part entering at 0 crosses 8 m at 1 m/s and leaves B's end at 8.0."""
    times, n_end, n_back = _observe(_chain_doc([0.0], []))
    _close(times, [8.0])
    assert (n_end, n_back) == (1, 0)


def test_chain_reversed_mid_run_parts_on_both_pieces():
    """P1 enters 0, P2 enters 2. Signal 5: P1 at x = 5 (1 m into B), P2 at x = 3 (on A, 1 m from
    the joint). Reversed, each travels back x metres to the chain start: P2 leaves at 5 + 3 = 8.0,
    P1 crosses B -> A and leaves at 5 + 5 = 10.0. Both through reverse_to (n_back)."""
    times, n_end, n_back = _observe(_chain_doc([0.0, 2.0], [5.0]))
    _close(times, [8.0, 10.0])
    assert (n_end, n_back) == (0, 2)


def test_chain_reversed_with_a_part_at_the_joint():
    """P enters 0, Q enters 2. Signal 4.0: P is exactly at the joint (x = 4), Q at x = 2. Whichever
    side of the joint P is counted on, it is 4 m from the start: Q leaves at 4 + 2 = 6.0, P at
    4 + 4 = 8.0, both to reverse_to. (Only the exit times are asserted, not the side.)"""
    times, n_end, n_back = _observe(_chain_doc([0.0, 2.0], [4.0]))
    _close(times, [6.0, 8.0])
    assert (n_end, n_back) == (0, 2)


def test_signal_on_the_second_piece_reverses_the_whole_chain():
    """Same as the mid-run test but the signal is wired to B only: A reverses at the same
    instant 5, so the result is identical: [8.0, 10.0], both via reverse_to."""
    times, n_end, n_back = _observe(_chain_doc([0.0, 2.0], [5.0], signal_on="B"))
    _close(times, [8.0, 10.0])
    assert (n_end, n_back) == (0, 2)


def test_chain_reversed_twice_delivers_at_the_chain_end():
    """P1 enters 0, P2 enters 2. Signals 5.0 and 6.5. At 5: P1 x = 5, P2 x = 3. Reversed 1.5 s:
    P1 x = 3.5 (back across the joint onto A), P2 x = 1.5. Forward again at 6.5: P1 has
    8 - 3.5 = 4.5 m left, leaves B's end at 6.5 + 4.5 = 11.0; P2 has 8 - 1.5 = 6.5 m, leaves at
    6.5 + 6.5 = 13.0. Both on the end path (parts are 2 m apart, spacing 0.5 holds)."""
    times, n_end, n_back = _observe(_chain_doc([0.0, 2.0], [5.0, 6.5]))
    _close(times, [11.0, 13.0])
    assert (n_end, n_back) == (2, 0)


def test_entry_is_refused_at_the_running_start_while_reversed():
    """P enters 0. Signals 1.5 (reverse) and 2.0 (forward). Q is made at 1.7, while the chain is
    reversed, so it holds upstream (a Q boarding at 1.7 would sit at the start moving backwards
    and leave by reverse_to). At 2.0 P is at x = 1.5 - 0.5 = 1.0; forward: 7 m left, P leaves B's
    end at 2.0 + 7 = 9.0. Q boards at 2.0 (P is 1.0 m ahead, spacing 0.5) and leaves at
    2.0 + 8 = 10.0. Both on the end path: n_back stays 0."""
    times, n_end, n_back = _observe(_chain_doc([0.0, 1.7], [1.5, 2.0]))
    _close(times, [9.0, 10.0])
    assert (n_end, n_back) == (2, 0)


# -- reverse from item code on the second piece -----------------------------------------

REVERSER = ("class Rev:\n"
            "    def on_signal(self, name, item):\n"
            "        self.n = getattr(self, 'n', 0) + 1\n"
            "        item.reverse('B', self.n % 2 == 0)\n")


@pytest.fixture
def trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("NAGASIM_USER_DIR", str(tmp_path))
    trustwrite.trust_source(REVERSER, label="chain reverser", by="test")


def test_item_call_on_the_second_piece_reverses_the_whole_chain(trusted):
    """item.reverse('B', False) at 5.0 reverses A too: same numbers as the mid-run test,
    [8.0, 10.0] via reverse_to."""
    doc = copy.deepcopy(_chain_doc([0.0, 2.0], [5.0], signal_on=None))
    doc["submodels"] = {"Rev": {"parameters": [], "blocks": [], "links": [],
                                "code": {"class": "Rev", "source": REVERSER, "signals": ["rev"]}}}
    doc["blocks"].append({"name": "rev_rule", "type": "SUBMODEL", "position": [0, 0],
                          "params": {"definition": "Rev", "arguments": {}}})
    doc["schema_version"] = schema.CURRENT_VERSION
    times, n_end, n_back = _observe(doc)
    _close(times, [8.0, 10.0])
    assert (n_end, n_back) == (0, 2)


# -- validation ---------------------------------------------------------------------------

def _codes(doc):
    return [p.code for p in validate_model_structured(doc)]


def _valid(doc):
    doc["schema_version"] = schema.CURRENT_VERSION
    return doc


def test_a_plain_chain_validates_clean():
    doc = _valid(_chain_doc([0.0], [5.0]))
    assert "E_CONVEYOR_REVERSE" not in _codes(doc)
    assert not [p for p in validate_model_structured(doc) if p.severity == "error"]


def _refused(doc):
    found = [p for p in validate_model_structured(doc) if p.code == "E_CONVEYOR_REVERSE"]
    assert found, _codes(doc)
    assert found[0].severity == "error"
    assert found[0].message and found[0].message.isascii()


def test_plain_belt_into_a_reversible_one_keeps_the_back_to_back_refusal():
    """X (not reversible) feeds Y (reversible) directly and Y's reverse_to names X: not a chain
    (mixed), the part would land at the wrong end, R48 still refuses."""
    x = belt(name="X", length=4.0, speed=1.0, spacing=0.5)
    y = belt(name="Y", length=4.0, speed=1.0, spacing=0.5)
    y["params"].update({"reversible": True, "reverse_to": "X", "reverse_signal": "rev"})
    doc = layout([create(max_arrivals=1), x, y, dispose()],
                 [{"source": "create_1", "target": "X"}, {"source": "X", "target": "Y"},
                  {"source": "Y", "target": "dispose_1"}])
    _refused(_valid(doc))


def test_reversible_pieces_with_a_block_between_that_feeds_back_stay_refused():
    """A (reversible) -> blk (ASSIGN) -> B (reversible), and B's reverse_to names blk, the block
    that feeds B: not a direct chain, so R48's refusal stays."""
    a = belt(name="A", length=4.0, speed=1.0, spacing=0.5)
    b = belt(name="B", length=4.0, speed=1.0, spacing=0.5)
    a["params"].update({"reversible": True})
    b["params"].update({"reversible": True, "reverse_to": "blk", "reverse_signal": "rev"})
    doc = layout([create(max_arrivals=1), a, _assign("blk", "n"), b, dispose()],
                 [{"source": "create_1", "target": "A"}, {"source": "A", "target": "blk"},
                  {"source": "blk", "target": "B"}, {"source": "B", "target": "dispose_1"}],
                 variables=[{"name": "n", "initial": 0}])
    _refused(_valid(doc))
