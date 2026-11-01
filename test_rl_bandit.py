import os
import random
import sqlite3

import rl_bandit
from rl_bandit import BanditAgent, context_keys

SETUP = {"strategy": "trend", "direction": "long", "timeframe": "4h"}


def _agent(tmp_path, mode="live", **kw):
    return BanditAgent(str(tmp_path / "rl.db"), mode=mode, rng=random.Random(1), **kw)


def test_off_mode_is_neutral_and_logs_nothing(tmp_path):
    agent = _agent(tmp_path, mode="off")
    d = agent.decide("Spot", "BTC/USDT", SETUP, 100, 2)
    assert d["applied_multiplier"] == 1.0 and d["decision_id"] is None
    assert agent.stats()["recent"] == []


def test_bandit_update_moves_posterior(tmp_path):
    agent = _agent(tmp_path)
    spec, parent = context_keys("Spot", SETUP, 100, 2)
    for _ in range(20):
        agent.learn(spec, parent, -1.0)
    arm = {a["context"]: a for a in agent.stats()["arms"]}
    assert arm[spec]["samples"] == 20 and arm[parent]["samples"] == 20
    assert arm[spec]["expected_r"] < -0.8
    assert arm[spec]["preferred_action"] == "skip"


def test_rewards_are_clipped(tmp_path):
    agent = _agent(tmp_path)
    spec, parent = context_keys("Spot", SETUP, 100, 2)
    agent.learn(spec, parent, 1000.0)
    assert agent.stats()["arms"][0]["avg_r"] == rl_bandit.REWARD_CLIP[1]


def test_persistence_across_instances(tmp_path):
    agent = _agent(tmp_path)
    d = agent.decide("Futures Demo", "XAU/USDT:USDT", SETUP, 100, 2)
    agent.mark_opened(d["decision_id"])
    assert agent.record_outcome("Futures", "XAU/USDT:USDT", 1.5)
    again = _agent(tmp_path)
    stats = again.stats()
    assert stats["comparison"]["closed_trades"] == 1
    assert any(a["samples"] == 1 for a in stats["arms"])


def test_outcome_without_opened_decision_is_ignored(tmp_path):
    agent = _agent(tmp_path)
    agent.decide("Spot", "ETH/USDT", SETUP, 100, 2)  # pending, never opened
    assert agent.record_outcome("Spot", "ETH/USDT", 1.0) is False
    assert agent.stats()["arms"] == []


def test_live_gated_until_min_samples(tmp_path):
    agent = _agent(tmp_path, min_samples=30, exploration=0.0)
    spec, parent = context_keys("Spot", SETUP, 100, 2)
    for _ in range(10):
        agent.learn(spec, parent, -1.0)
    d = agent.decide("Spot", "BTC/USDT", SETUP, 100, 2)
    assert d["action"] == "skip" and d["applied_multiplier"] == 1.0
    for _ in range(25):
        agent.learn(spec, parent, -1.0)
    d = agent.decide("Spot", "BTC/USDT", SETUP, 100, 2)
    assert d["action"] == "skip" and d["applied_multiplier"] == 0.0


def test_shadow_never_changes_size(tmp_path):
    agent = _agent(tmp_path, mode="shadow", min_samples=1, exploration=0.0)
    spec, parent = context_keys("Spot", SETUP, 100, 2)
    for _ in range(40):
        agent.learn(spec, parent, -1.0)
    d = agent.decide("Spot", "BTC/USDT", SETUP, 100, 2)
    assert d["action"] == "skip" and d["applied_multiplier"] == 1.0
    assert "للمراقبة" in d["note_ar"]


def test_never_increases_size(tmp_path):
    agent = _agent(tmp_path, min_samples=1)
    spec, parent = context_keys("Spot", SETUP, 100, 2)
    for _ in range(50):
        agent.learn(spec, parent, 5.0)
    for _ in range(200):
        d = agent.decide("Spot", "BTC/USDT", SETUP, 100, 2)
        assert 0.0 <= d["applied_multiplier"] <= 1.0
        assert d["multiplier"] <= 1.0


def test_decide_failure_falls_back_to_neutral(tmp_path):
    agent = _agent(tmp_path)
    os.remove(str(tmp_path / "rl.db"))
    os.mkdir(str(tmp_path / "rl.db"))  # make db unusable
    d = agent.decide("Spot", "BTC/USDT", SETUP, 100, 2)
    assert d["applied_multiplier"] == 1.0


def test_context_keys_buckets():
    assert context_keys("Spot", SETUP, 100, 0.5)[0].endswith("vol:low")
    assert context_keys("Futures Demo", SETUP, 100, 3)[0].startswith("futures|")
    assert context_keys("Spot", SETUP)[0].endswith("vol:na")


from test_api import app_module, _auth, _client  # noqa: F401,E402


def test_api_rl_requires_auth_and_returns_stats(app_module):  # noqa: F811
    client = _client(app_module)
    assert client.get("/api/rl").status_code == 401
    body = client.get("/api/rl", headers=_auth()).json()
    assert body["mode"] in ("off", "shadow", "live")
    assert "arms" in body and "comparison" in body
