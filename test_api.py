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
    assert "/api/trading" in resp.text and "sample-data.js" not in resp.text
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
