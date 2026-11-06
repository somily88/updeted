"""API route tests: authentication and settings, without starting the scheduler."""
import base64
import importlib
import os
import sys

import pytest


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("TRADING_ENABLED", "false")
    for name in ("main", "config", "database", "futures_engine", "risk_controls"):
        sys.modules.pop(name, None)
    main = importlib.import_module("main")
    main.init_db()
    return main


def _auth(user="admin", password="secret-pass"):
    token = base64.b64encode("{}:{}".format(user, password).encode()).decode()
    return {"Authorization": "Basic " + token}


def _client(main):
    from fastapi.testclient import TestClient
    return TestClient(main.app)  # no context manager -> lifespan/scheduler not started


@pytest.mark.parametrize("path", ["/", "/api/state", "/api/equity", "/check"])
def test_routes_require_auth(app_module, path):
    resp = _client(app_module).get(path, follow_redirects=False)
    assert resp.status_code == 401


def test_wrong_password_rejected(app_module):
    resp = _client(app_module).get("/api/equity", headers=_auth(password="nope"))
    assert resp.status_code == 401


def test_equity_ok_with_auth(app_module):
    resp = _client(app_module).get("/api/equity", headers=_auth())
    assert resp.status_code == 200


def test_settings_update_persists(app_module):
    payload = {"trade_amount_usdt": 250, "spot_max_concurrent_trades": 3,
               "futures_max_concurrent_trades": 2}
    resp = _client(app_module).post("/api/settings", json=payload, headers=_auth())
    assert resp.status_code == 200, resp.text
    assert app_module.TRADE_AMOUNT_USDT == 250
    assert app_module.MAX_SPOT_CONCURRENT_TRADES == 3
    with app_module.get_db_connection(app_module.DB_NAME) as conn:
        rows = dict(conn.execute("SELECT key, value FROM bot_settings").fetchall())
    assert rows["spot_max_concurrent_trades"] == "3"


def test_credentials_route_requires_auth(app_module):
    resp = _client(app_module).post("/api/platforms/binance/credentials",
                                    json={"api_key": "x" * 10, "secret_key": "y" * 10})
    assert resp.status_code == 401


def test_settings_save_not_blocked_by_running_scan(app_module):
    """Regression: saving during a market scan used to return 409 and never apply."""
    payload = {"trade_amount_usdt": 800, "spot_max_concurrent_trades": 4,
               "futures_max_concurrent_trades": 4}
    assert app_module.scan_lock.acquire(blocking=False)
    try:
        resp = _client(app_module).post("/api/settings", json=payload, headers=_auth())
    finally:
        app_module.scan_lock.release()
    assert resp.status_code == 200, resp.text
    assert app_module.TRADE_AMOUNT_USDT == 800


def test_legacy_small_trade_amount_migrated_once(app_module):
    with app_module.get_db_connection(app_module.DB_NAME) as conn:
        conn.execute("DELETE FROM bot_settings WHERE key='trade_amount_migrated_v2'")
        conn.execute("INSERT OR REPLACE INTO bot_settings VALUES ('trade_amount_usdt', '150.0')")
    app_module.TRADE_AMOUNT_USDT = 1000.0
    app_module.load_runtime_settings()
    assert app_module.TRADE_AMOUNT_USDT == 1000.0
    # A value saved after the migration (even 150) is respected.
    with app_module.get_db_connection(app_module.DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO bot_settings VALUES ('trade_amount_usdt', '150.0')")
    app_module.load_runtime_settings()
    assert app_module.TRADE_AMOUNT_USDT == 150.0


def test_risk_sizing_uses_cap_only_as_max(app_module):
    sized = app_module.risk_sized_notional(10000, 100, 2, 1000, 1.0)
    assert sized == 1000  # 1% risk / 2% stop = 5000, capped at 1000
    assert app_module.risk_sized_notional(10000, 100, 2, 100000, 1.0) == pytest.approx(5000)


def test_r_multiple_target_and_exit_reason(app_module):
    assert app_module.r_multiple_target(100, 98, 2) == pytest.approx(104)
    assert app_module.r_multiple_target(100, 98) == pytest.approx(100 + 2 * app_module.TP_R_MULTIPLE)
    assert app_module.spot_exit_reason("SL", True, True) == "trailing"
    assert app_module.spot_exit_reason("SL", True, False) == "breakeven"
    assert app_module.spot_exit_reason("TP") == "TP"
    cols = {r[1] for r in app_module.open_db_connection(app_module.DB_NAME).execute("PRAGMA table_info(trades)")}
    assert "exit_reason" in cols


def test_futures_exit_reason_codes():
    from futures_engine import BinanceFuturesDemo as F
    assert F.exit_reason_code("هدف الربح") == "TP"
    assert F.exit_reason_code("وقف الخسارة", partial_taken=True) == "breakeven"
    assert F.exit_reason_code("وقف حماية Binance Demo") == "SL"
    assert F.exit_reason_code("زوال تعرض Spot والتحوط المرتبط") == "manual"


def test_root_serves_new_dashboard(app_module):
    resp = _client(app_module).get("/", headers=_auth())
    assert resp.status_code == 200
    assert "/api/state" in resp.text and "stratLabel" in resp.text
    assert "trend_pullback" in resp.text and "donchian_breakout" in resp.text
    assert "sample-data.js" not in resp.text
    assert "SAMPLE_STATE" not in resp.text and "?cap" not in resp.text


def test_old_dashboard_and_font_require_auth(app_module):
    client = _client(app_module)
    for path in ("/old", "/fonts/Cairo.ttf"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=_auth()).status_code == 200
    assert "tradfi-agreement" in client.get("/old", headers=_auth()).text


def test_trading_toggle_requires_auth(app_module):
    resp = _client(app_module).post("/api/trading", json={"enabled": False})
    assert resp.status_code == 401
    assert app_module.SPOT_ENTRIES_PAUSED is False


def test_trading_toggle_persists_and_leaves_positions(app_module, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_telegram_message", lambda m: sent.append(m))
    position = {"symbol": "BNB/USDT", "protective_order_id": "123", "amount": 0.28}
    app_module.active_positions["BNB/USDT"] = position
    client = _client(app_module)
    resp = client.post("/api/trading", json={"enabled": False}, headers=_auth())
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries_enabled"] == {"spot": False, "futures": False}
    assert app_module.SPOT_ENTRIES_PAUSED and app_module.FUTURES_ENTRIES_PAUSED
    assert app_module.active_positions["BNB/USDT"] is position
    assert position["protective_order_id"] == "123"
    assert sent and "إيقاف" in sent[0]
    with app_module.get_db_connection(app_module.DB_NAME) as conn:
        rows = dict(conn.execute("SELECT key, value FROM bot_settings").fetchall())
    assert rows["spot_entries_paused"] == "1" and rows["futures_entries_paused"] == "1"
    # survives a restart
    app_module.SPOT_ENTRIES_PAUSED = app_module.FUTURES_ENTRIES_PAUSED = False
    app_module.load_runtime_settings()
    assert app_module.SPOT_ENTRIES_PAUSED and app_module.FUTURES_ENTRIES_PAUSED
    # resume only futures
    resp = client.post("/api/trading", json={"enabled": True, "market": "futures"}, headers=_auth())
    assert resp.json()["entries_enabled"] == {"spot": False, "futures": True}
    assert client.post("/api/trading", json={"enabled": True, "market": "x"}, headers=_auth()).status_code == 422
    app_module.active_positions.clear()


def test_dashboard_icons_served_with_auth(app_module):
    c = _client(app_module)
    assert c.get("/icons/BTC.svg").status_code == 401
    r = c.get("/icons/BTC.svg", headers=_auth())
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert c.get("/icons/NOPE.svg", headers=_auth()).status_code == 404
    assert c.get("/icons/..%2Fmain.svg", headers=_auth()).status_code == 404
    html = c.get("/", headers=_auth()).text
    for marker in ('id="marketGrid"', 'id="decisionList"', "stratLabel"):
        assert marker in html


def test_state_exposes_boot_and_kill_switch(app_module):
    data = _client(app_module).get("/api/state", headers=_auth()).json()
    assert data["boot_at"]
    switch = data["kill_switch"]
    assert set(switch) >= {"enabled", "tripped", "reason"}
    assert switch["tripped"] in (True, False)
    assert switch["enabled"] in (True, False)


def test_state_trades_include_strategy_and_market(app_module, monkeypatch):
    monkeypatch.setattr(app_module.exchange, "fetch_balance", lambda: {"free": {"USDT": 10}, "total": {"USDT": 10}})
    with app_module.get_db_connection(app_module.DB_NAME) as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, strategy, timeframe, cost_quality)
               VALUES ('BTC/USDT', 1, 2, 1, 'closed', '2026-11-04 10:00:00', 3.5, 'trend_pullback', '4h', 'actual')"""
        )
    data = _client(app_module).get("/api/state", headers=_auth()).json()
    row = next(item for item in data["trades"] if item["symbol"] == "BTC/USDT")
    assert row["strategy"] == "trend_pullback"
    assert row["market_type"] == "Spot"
    futures = next(item for item in data["platforms"] if item["id"] == "binance_futures")
    assert "realized_pnl" in futures
    assert data["params"]["strategies"]


def test_dashboard_bindings_and_neural_markup(app_module):
    html = _client(app_module).get("/", headers=_auth()).text
    for marker in (
        "binance_futures",
        "futures.realized_pnl",
        "لا إشارة",
        "Asia/Riyadh",
        "الخلية العصبية",
        "التعلم المعزز",
        "متاح",
        "unverified_spot_trades",
        "/api/rl",
        "/api/trading",
        "trend_pullback",
        "donchian_breakout",
        'id="riskCard"',
        'id="neuralCard"',
        'id="rlPanel"',
        'id="emergencyCard"',
        'id="listenPanel"',
        'id="positionCards"',
        'id="sourcesList"',
        "مفتاح الإيقاف",
        "syncFlow",
    ):
        assert marker in html, marker
    assert "futures.pnl_usdt" not in html
    assert "fallbackData" not in html
    assert "12540" not in html
    assert "مثبت" not in html


def test_three_assets_allowlisted_with_auth(app_module):
    c = _client(app_module)
    assert c.get("/three/three.module.js").status_code == 401
    r = c.get("/three/three.module.js", headers=_auth())
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert c.get("/three/jsm/controls/OrbitControls.js", headers=_auth()).status_code == 200
    assert c.get("/three/jsm/Addons.js", headers=_auth()).status_code == 404
    assert c.get("/three/../main.py", headers=_auth()).status_code == 404
    assert c.get("/three/..%2Fmain.py", headers=_auth()).status_code == 404
