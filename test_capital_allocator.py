import importlib
import os
import pytest


@pytest.fixture
def ca(monkeypatch):
    monkeypatch.setenv("CAPITAL_ALLOCATOR_ENABLED", "true")
    for k, v in {"ALLOCATION_PCT": "50", "PORTFOLIO_MAX_OPEN_RISK_PCT": "4", "SCORE_MULT_MIN": "0.5",
                 "SCORE_MULT_MAX": "1.2", "SIZING_MIN_STOP_PCT": "1.5", "SPOT_MAX_DIRECTIONAL_RISK_PCT": "3"}.items():
        monkeypatch.setenv(k, v)
    import capital_allocator
    mod = importlib.reload(capital_allocator)
    yield mod
    monkeypatch.delenv("CAPITAL_ALLOCATOR_ENABLED")
    importlib.reload(capital_allocator)


def pos(entry, qty, stop, side="long", cost=None):
    return {"entry_price": entry, "amount": qty, "stop_price": stop, "side": side,
            "cost": entry * qty if cost is None else cost}


def test_default_off():
    import capital_allocator
    os.environ.pop("CAPITAL_ALLOCATOR_ENABLED", None)
    importlib.reload(capital_allocator)
    assert capital_allocator.enabled() is False
    assert capital_allocator.concurrency_allows(6, 6) is False


def test_risk_sizing_math(ca):
    # 5,058 equity, 0.75% risk, 5% stop -> 758.7
    p = ca.plan_entry(5058, 100, 5, {}, 1.0, 1.0, 0.75, 5)
    assert p["allowed"] and p["notional"] == pytest.approx(758.7)
    assert p["risk_usdt"] == pytest.approx(37.935)


def test_min_stop_floor(ca):
    # 0.5% stop is floored to 1.5% for sizing: 10000*1%/1.5% = 6666 -> budget 5000 binds
    p = ca.plan_entry(10000, 100, 0.5, {}, 1.0, 1.0, 0.5, 5)
    assert p["wanted"] == pytest.approx(10000 * 0.005 / 0.015)
    assert p["notional"] == pytest.approx(10000 * 0.005 / 0.015)


def test_budget_cap_and_full(ca):
    positions = {"A": pos(100, 24, 95)}  # 2400 used of 2500 budget
    p = ca.plan_entry(5000, 100, 5, positions, 1.0, 1.0, 0.75, 5)
    assert p["allowed"] and p["notional"] == pytest.approx(100) and p["binding_cap"] == "budget_full"
    positions["B"] = pos(100, 0.98, 99)
    p = ca.plan_entry(5000, 100, 5, positions, 1.0, 1.0, 0.75, 5)
    assert not p["allowed"] and p["rule"] == "budget_full"


def test_open_risk_cap(ca):
    # open risk 195 of 200 limit (4% of 5000); 5% stop -> headroom notional 100
    positions = {"A": pos(100, 10, 80.5, cost=1)}
    p = ca.plan_entry(5000, 100, 5, positions, 1.0, 1.0, 0.75, 5)
    assert p["notional"] == pytest.approx(100) and p["binding_cap"] == "open_risk_cap"
    p = ca.plan_entry(5000, 100, 5, positions, 1.0, 1.0, 0.75, 150)
    assert not p["allowed"] and p["rule"] == "open_risk_cap"


def test_below_min_notional(ca):
    p = ca.plan_entry(100, 100, 5, {}, 1.0, 1.0, 0.75, 50)
    assert not p["allowed"] and p["rule"] == "below_min_notional"


def test_no_averaging_down(ca):
    positions = {"BTC/USDT": pos(100, 1, 95)}
    p = ca.plan_entry(5000, 90, 5, positions, 1.0, 1.0, 0.75, 5, symbol="BTC/USDT")
    assert not p["allowed"] and p["rule"] == "no_add_open_position"
    with pytest.raises(RuntimeError):
        ca.assert_no_increase(positions, "BTC/USDT")
    ca.assert_no_increase(positions, "ETH/USDT")


def test_db_concurrent_override_ignored(ca):
    # DB stored MAX_*_CONCURRENT_TRADES=6 with 10 open must not block when enabled
    assert ca.concurrency_allows(10, 6) is True
    assert ca.concurrency_allows(10, 6, allocator_enabled=False) is False


def test_score_mult_bounds(ca):
    assert ca.score_multiplier({"signal_strength": 100}) == 1.2
    assert ca.score_multiplier({"signal_strength": 0}, strategy_mult=0.5) == 0.5
    assert ca.score_multiplier({"signal_strength": 50}) == 1.0
    # stats ignored under 30 trades, used at 30+
    assert ca.score_multiplier({"signal_strength": 50}, {"trades": 29, "expectancy_r": -0.4}) == 1.0
    assert ca.score_multiplier({"signal_strength": 50}, {"trades": 30, "expectancy_r": -0.4}) == pytest.approx(0.6)
    # RL only when live
    assert ca.score_multiplier({"signal_strength": 50}, rl_multiplier=0.5, rl_mode="shadow") == 1.0
    assert ca.score_multiplier({"signal_strength": 50}, rl_multiplier=0.5, rl_mode="live") == 0.5


def test_open_risk_zero_when_stop_past_entry(ca):
    assert ca.position_open_risk(pos(100, 1, 101)) == 0
    assert ca.position_open_risk(pos(100, 2, 106, side="short")) == pytest.approx(12)


def test_state_reserve(ca):
    st = ca.market_state(10000, {"A": pos(100, 10, 95)})
    assert st["budget"] == 5000 and st["used"] == 1000 and st["remaining"] == 4000
    assert st["reserve"] == 5000 and st["open_risk_pct"] == pytest.approx(0.5)


def test_spot_corr_guard(ca):
    positions = {"A": pos(100, 10, 85)}  # 150 risk; limit 3% of 5000 = 150
    ok, rule, _ = ca.spot_correlation_guard(positions, 10, 5000)
    assert not ok and rule == "corr_directional_risk"
    ok, _, _ = ca.spot_correlation_guard({}, 100, 5000)
    assert ok
