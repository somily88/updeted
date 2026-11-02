import sqlite3

import spot_rules as sr
import trade_stats


def test_bounce_target_must_cover_three_round_trips():
    ok, pct, need = sr.target_covers_fees("bounce", 100.0, 100.4, mult=3)
    assert not ok and abs(need - 0.6) < 1e-9 and abs(pct - 0.4) < 1e-9
    assert sr.target_covers_fees("bounce", 100.0, 100.7, mult=3)[0]
    # Other strategies are not filtered by default.
    assert sr.target_covers_fees("trend", 100.0, 100.1, mult=3)[0]


def test_bnb_discount_lowers_required_target():
    assert abs(sr.round_trip_fee_pct(True) - 0.15) < 1e-9
    assert sr.target_covers_fees("bounce", 100.0, 100.5, bnb_discount=True, mult=3)[0]


def test_trail_arms_only_after_one_r():
    assert not sr.trail_armed(100, 100.9, 1.0, True, 1.0, arm_r=1.0)
    assert sr.trail_armed(100, 101.0, 1.0, False, 1.0, arm_r=1.0)
    # arm_r=0 keeps legacy behaviour
    assert not sr.trail_armed(100, 101.5, 1.0, False, 1.0, arm_r=0)
    assert sr.trail_armed(100, 101.5, 1.0, True, 1.0, arm_r=0)


def test_trail_distance_is_atr_based_and_never_tighter():
    assert sr.trail_distance(2.0, 0.75, mult=1.0) == 2.0
    assert sr.trail_distance(2.0, 1.5, mult=1.0) == 3.0
    assert sr.legacy_trail_distance(100, 1.2, 0.3) == 1.2
    assert abs(sr.legacy_trail_distance(100, 0, 0.3) - 0.3) < 1e-9


def test_trend_stop_capped():
    assert sr.stop_distance("trend", 100, 5, 1.5, max_stop_pct=6) == 6.0
    assert sr.stop_distance("bounce", 100, 5, 1.5, max_stop_pct=6) == 7.5


def test_trend_requires_daily_bull_for_symbol_and_btc():
    up = [float(i) for i in range(1, 260)]
    down = list(reversed(up))
    assert sr.trend_entry_allowed("trend", up, up[-1], up, enabled=True)[0]
    assert not sr.trend_entry_allowed("trend", down, down[-1], up, enabled=True)[0]
    assert not sr.trend_entry_allowed("trend", up, up[-1], down, enabled=True)[0]
    assert not sr.trend_entry_allowed("trend", up[:100], 100, up, enabled=True)[0]
    assert sr.trend_entry_allowed("bounce", down, down[-1], down, enabled=True)[0]


def test_risk_pct_sizing_and_cap():
    legacy = lambda: 200.0
    assert sr.spot_notional(10000, 100, 1, 200, legacy, sizing="fixed") == 200.0
    # 0.5% of 10k = 50 risk / 1% stop = 5000 -> capped at 10% = 1000
    assert sr.spot_notional(10000, 100, 1, 200, legacy, sizing="risk_pct", risk_pct=0.5, max_equity_pct=10) == 1000
    assert abs(sr.spot_notional(10000, 100, 5, 200, legacy, sizing="risk_pct", risk_pct=0.5,
                                max_equity_pct=10) - 1000) < 1e-9
    assert abs(sr.spot_notional(10000, 100, 10, 200, legacy, sizing="risk_pct", risk_pct=0.5,
                                max_equity_pct=10) - 500) < 1e-9


def _db(tmp_path):
    path = str(tmp_path / "t.db")
    with sqlite3.connect(path) as c:
        c.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, entry_price REAL,
            exit_price REAL, pnl_pct REAL, status TEXT, timestamp TEXT, pnl_usdt REAL,
            strategy TEXT, fees_usdt REAL, r_multiple REAL, cost_quality TEXT)""")
        c.executemany("INSERT INTO trades (symbol,entry_price,exit_price,pnl_pct,status,timestamp,pnl_usdt,strategy)"
                      " VALUES ('X',1,1,0,'LOSS','t',?,?)", [(-1.0, None), (-2.0, ''), (3.0, 'bounce')])
    return path


def test_legacy_cleanup_idempotent_and_excluded(tmp_path):
    import main
    path = _db(tmp_path)
    assert main.cleanup_legacy_spot_trades(path) == 2
    assert main.cleanup_legacy_spot_trades(path) == 0
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM trades_backup_legacy_v1").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM trades WHERE strategy='legacy'").fetchone()[0] == 2
    stats = trade_stats.load_strategy_stats(path)
    assert [s["strategy"] for s in stats] == ["bounce"]


def test_backtest_trail_waits_for_arm_r():
    import backtest
    pos = {"direction": "long", "entry_price": 100.0, "stop_price": 99.0, "target_price": None,
           "risk_distance": 1.0, "partial_taken": True, "extreme": 100.0, "trailing_distance": 0.5,
           "trail_arm_r": 2.0, "quantity": 1.0}
    assert backtest.manage_position(pos, [0, 100.0, 101.5, 100.0, 101.0], 0, 0.001, 0) is None
    assert pos["stop_price"] == 99.0
    backtest.manage_position(pos, [0, 101.0, 102.2, 101.0, 102.0], 0, 0.001, 0)
    assert abs(pos["stop_price"] - 101.7) < 1e-9
