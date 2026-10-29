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
