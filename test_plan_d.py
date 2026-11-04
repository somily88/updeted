import sqlite3
import backtest
import trade_stats as ts


def test_initial_risk_and_r():
    assert ts.initial_risk_usdt(100, 2, 1000) == 20
    assert ts.initial_risk_usdt(100, 0, 1000) is None
    assert ts.r_multiple(-20, 20) == -1
    assert ts.r_multiple(10, None) is None


def test_summarize_groups_and_metrics():
    rows = [{"market": "spot", "strategy": "trend", "pnl_usdt": 30, "fees_usdt": 1, "r_multiple": 1.5},
            {"market": "spot", "strategy": "trend", "pnl_usdt": -10, "fees_usdt": 1, "r_multiple": -0.5},
            {"market": "futures", "strategy": "bounce", "pnl_usdt": -5, "fees_usdt": 0, "r_multiple": None}]
    out = {(r["market"], r["strategy"]): r for r in ts.summarize(rows)}
    t = out[("spot", "trend")]
    assert t["trades"] == 2 and t["win_rate_pct"] == 50 and t["profit_factor"] == 3
    assert t["expectancy_r"] == 0.5 and t["sample_warning"]
    assert out[("futures", "bounce")]["expectancy_r"] is None


def test_load_stats_skips_simulated_and_old_schema(tmp_path):
    db = str(tmp_path / "t.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE trades (strategy TEXT, pnl_usdt REAL, cost_quality TEXT)")
        c.execute("INSERT INTO trades VALUES ('trend', 5, 'actual')")
        c.execute("INSERT INTO trades VALUES ('trend', 99, 'simulated')")
        c.execute("CREATE TABLE futures_trades (strategy TEXT, pnl_usdt REAL, fees_usdt REAL, r_multiple REAL)")
        c.execute("INSERT INTO futures_trades VALUES ('range', -2, 0.1, -1)")
    out = {r["market"]: r for r in ts.load_strategy_stats(db)}
    assert out["spot"]["net_pnl_usdt"] == 5 and out["spot"]["trades"] == 1
    assert out["futures"]["expectancy_r"] == -1


def test_backtest_close_position_r_multiple():
    pos = {"direction": "long", "quantity": 10, "entry_price": 100.0, "notional": 1000.0,
           "risk_distance": 2.0, "entry_fee": 0, "signal_id": 1, "symbol": "X", "strategy": "trend",
           "timeframe": "1h", "signal_strength": 1, "signal_time_utc": "", "entry_time_utc": "", "entry_ms": 0}
    t = backtest.close_position(pos, 98.0, 0, "SL", 0.0, 0.0)
    assert t["risk_usdt"] == 20 and t["r_multiple"] == -1
    m = backtest.metrics([t], 1000)
    assert m["expectancy_r"] == -1 and m["by_strategy"]["trend"]["trades"] == 1
