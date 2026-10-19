"""nagasim/studyformulas.py: known answers and tail shares."""
import copy
import json
from pathlib import Path

import pytest

from nagasim import api
from nagasim import studyformulas as F

MODELS = Path(__file__).resolve().parents[1] / "nagasim" / "models"


@pytest.fixture
def single():
    return json.loads((MODELS / "single_server.json").read_text(encoding="utf-8"))


def with_service(layout, dist, params, c=1):
    m = copy.deepcopy(layout)
    m["blocks"][1]["params"]["distribution"] = dist
    m["blocks"][1]["params"]["dist_params"] = params
    m["resources"][0]["capacity"] = c
    return m


def test_mm1_rho_08(single):
    f = F.queue_formula(single)      # lambda 1, mu 1.25: rho 0.8, Wq = rho / (mu - lambda) = 3.2
    assert f["kind"] == "M/M/c" and f["wq"] == pytest.approx(3.2) and f["cs2"] == pytest.approx(1.0)


def test_mmc_two_servers(single):
    # lambda 1, mu 0.625, c 2: a = 1.6, rho = 0.8, Erlang C = 0.71111, Wq = C / (c mu - lambda)
    f = F.queue_formula(with_service(single, "Exponential", {"rate": 0.625}, c=2))
    assert f["wq"] == pytest.approx(0.711111111 / 0.25)


def test_mg1_pollaczek_khinchine(single):
    # constant service 0.8: cs2 = 0, Wq = rho * mean * (1 + cs2) / (2 (1 - rho)) = 0.8 * 0.8 / 0.4 = 1.6
    f = F.queue_formula(with_service(single, "Constant", {"value": 0.8}))
    assert f["kind"] == "M/G/1" and f["wq"] == pytest.approx(1.6)
    f = F.queue_formula(with_service(single, "Uniform", {"low": 0.4, "high": 1.2}))   # mean 0.8, var 0.64/12
    assert f["cs2"] == pytest.approx((0.64 / 12) / 0.64)
    assert f["wq"] == pytest.approx(0.8 * 0.8 * (1 + f["cs2"]) / (2 * 0.2))


def test_not_applicable(single):
    assert F.why_not(single) == ""
    two = copy.deepcopy(single)
    two["resources"].append({"name": "Other", "capacity": 1, "discipline": "FIFO"})
    assert F.queue_formula(two) is None and "one resource" in F.why_not(two)
    assert F.queue_formula(with_service(single, "Constant", {"value": 2.0})) is None     # rho >= 1
    assert F.queue_formula(with_service(single, "Constant", {"value": 0.8}, c=2)) is None


def test_share_above():
    assert F.share_above([1, 2, 3, 4], 2) == 0.5 and F.share_above([], 1) == 0.0


def test_wait_shares_on_a_short_run(single):
    res = api.run_model(single, replications=3, seed=1, run_length=300, warmup=0)
    shares = F.wait_shares(res, "Server", 1.0)
    assert shares is not None and len(shares) == 3 and all(0 <= s <= 1 for s in shares)
    assert any(s > 0 for s in shares)


def test_wait_shares_none_without_logs(single):
    res = api.run_model(single, replications=1, run_length=100, record_logs=False)
    assert F.wait_shares(res, "Server", 1.0) is None
    assert F.wait_shares(res, "Nope", 1.0) is None


def test_wait_shares_with_warmup_follow_the_m_m_1_tail(single):
    # P(W > x) = rho * exp(-(mu - lambda) x) = 0.8 * exp(-0.25 * 3) = 0.378 for the shipped model
    res = api.run_model(single, replications=10, seed=0)
    shares = F.wait_shares(res, "Server", 3.0, single)
    assert shares is not None and sum(shares) / len(shares) == pytest.approx(0.378, abs=0.08)
    single["resources"][0]["discipline"] = "LIFO"
    assert F.wait_shares(res, "Server", 3.0, single) is None
