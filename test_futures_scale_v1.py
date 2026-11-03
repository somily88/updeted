"""Futures scale v1: equity sizing, correlation guard, time stop, strategy size mult."""
import time
import pytest
import futures_engine as fe
from strategy_engine import effective_notional_cap, risk_sized_notional
from futures_engine import BinanceFuturesDemo

DAY = 86400000


def test_equity_pct_sizing_scales_with_equity_and_respects_risk():
    cap = effective_notional_cap(5058, 200, 8)
    assert cap == pytest.approx(404.64)
    # 3% stop, 2% risk -> risk budget allows far more than cap, cap binds
    assert risk_sized_notional(5058, 100, 3, cap, 2.0) <= cap
    # very wide stop: risk limit binds below the cap
    wide = risk_sized_notional(5058, 100, 60, cap, 0.5)
    assert wide < cap and wide * 0.6 <= 5058 * 0.005 + 1e-6


def test_strategy_size_mult_parse():
    assert fe.parse_strategy_size_mult("bounce:0.5, breakout:0") == {"bounce": 0.5, "breakout": 0.0}
    assert fe.FUTURES_STRATEGY_SIZE_MULT.get("bounce") == 0.5
    with pytest.raises(ValueError):
        fe.parse_strategy_size_mult("bounce:2")


def _p(side="long", strategy="trend", contracts=1.0, risk=100.0):
    return {"side": side, "strategy": strategy, "contracts": contracts, "risk_distance": risk}


def test_correlation_guard_count_and_risk():
    pos = {"BTC/USDT:USDT": _p(), "ETH/USDT:USDT": _p(), "SOL/USDT:USDT": _p()}
    ok, rule, _ = fe.correlation_guard(pos, "XRP/USDT:USDT", "long", 1, 100000, 3, 0)
    assert not ok and rule == "corr_max_same_direction"
    assert fe.correlation_guard(pos, "XRP/USDT:USDT", "short", 1, 100000, 3, 0)[0]
    assert fe.correlation_guard(pos, "NVDA/USDT:USDT", "long", 1, 100000, 3, 0)[0]  # TradFi exempt
    ok, rule, _ = fe.correlation_guard(pos, "XRP/USDT:USDT", "long", 10, 5000, 0, 3)
    assert not ok and rule == "corr_directional_risk"  # 300 + 10 > 150
    hedges = {"BTC/USDT:USDT": _p(strategy="volatility_breakout_hedge")}
    assert fe.correlation_guard(hedges, "ETH/USDT:USDT", "long", 1, 5000, 1, 3)[0]


def test_time_stop_rules():
    now = time.time() * 1000
    base = {"entry_price": 100, "risk_distance": 5, "timeframe": "1d", "strategy": "trend", "side": "long"}
    old = dict(base, opened_at_ms=now - 6 * DAY)
    assert fe.time_stop_reason(old, 101, now, 5, 0.5, 3, {"1d"})[0] == "time_stop"
    assert fe.time_stop_reason(old, 103, now, 5, 0.5, 3, {"1d"})[0] is None  # +0.6R
    assert fe.time_stop_reason(dict(base, opened_at_ms=now - 2 * DAY), 100, now, 5, 0.5, 3, {"1d"})[0] is None
    assert fe.time_stop_reason(dict(old, timeframe="4h"), 100, now, 5, 0.5, 3, {"1d"})[0] is None
    be = dict(base, opened_at_ms=now - 4 * DAY, partial_taken=True)
    assert fe.time_stop_reason(be, 100.5, now, 5, 0.5, 3, {"1d"})[0] == "time_stop_breakeven"
    short = dict(old, side="short")
    assert fe.time_stop_reason(short, 96, now, 5, 0.5, 3, {"1d"})[0] is None  # +0.8R
    assert BinanceFuturesDemo.exit_reason_code("إيقاف زمني: x") == "time_stop"


def test_trades_table_has_holding_hours(tmp_path):
    engine = BinanceFuturesDemo.__new__(BinanceFuturesDemo)
    engine.db_path = str(tmp_path / "f.db")
    engine._init_db()
    with engine._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(futures_trades)")}
    assert {"holding_hours", "opened_at", "exit_reason", "r_multiple"} <= cols
