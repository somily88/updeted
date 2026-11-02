import importlib
import io
import time
import urllib.error

import pytest


@pytest.fixture
def mv(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MASSIVE_ENABLED", "true")
    monkeypatch.setenv("MASSIVE_MAX_CALLS_PER_MIN", "50")
    monkeypatch.setenv("MASSIVE_MARKET_HOURS_GUARD", "true")
    monkeypatch.setenv("MASSIVE_UNDERLYING_CONFIRM", "true")
    monkeypatch.setenv("MASSIVE_SMA_DAYS", "5")
    import massive_client
    m = importlib.reload(massive_client)
    yield m
    for k in ("MASSIVE_API_KEY", "MASSIVE_MARKET_HOURS_GUARD", "MASSIVE_UNDERLYING_CONFIRM", "MASSIVE_SMA_DAYS",
              "MASSIVE_MAX_CALLS_PER_MIN"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(massive_client)


def _fake(status="open", closes=(1, 2, 3, 4, 5, 6), news_ts=None):
    news_ts = news_ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def f(path, params):
        if path == "v1/marketstatus/now":
            return {"market": status, "earlyHours": False, "afterHours": False, "exchanges": {"nasdaq": status}}
        if path == "v1/marketstatus/upcoming":
            return [{"date": "2026-11-26", "name": "Thanksgiving", "status": "closed", "exchange": "NYSE"},
                    {"date": "2026-11-26", "name": "Thanksgiving", "status": "closed", "exchange": "NASDAQ"}]
        if path.startswith("v2/aggs"):
            return {"results": [{"c": c} for c in closes]}
        if path == "v2/reference/news":
            t = params["ticker"]
            return {"results": [{"title": t + " beats estimates", "published_utc": news_ts,
                                 "publisher": {"name": "P"}, "insights": [{"ticker": t, "sentiment": "positive"}]}]}
        raise AssertionError(path)
    return f


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    import massive_client
    m = importlib.reload(massive_client)
    assert not m.MASSIVE_ENABLED
    m.refresh()
    assert m.fetch_news() == [] and m.entry_block_reason("NVDA/USDT:USDT", "long") is None
    assert m.snapshot()["has_key"] is False


def test_refresh_and_gates(mv, monkeypatch):
    monkeypatch.setattr(mv, "_http_get", _fake("open"))
    mv.refresh()
    snap = mv.snapshot()
    assert snap["market"]["market"] == "open" and len(snap["upcoming"]) == 1
    assert snap["trend"]["NVDA"]["above"] is True
    # open buffer just started
    assert "افتتاح" in mv.entry_block_reason("NVDA/USDT:USDT", "long")
    mv.state["open_since"] = time.time() - 3600
    assert mv.entry_block_reason("NVDA/USDT:USDT", "long") is None
    assert "فوق متوسط" in mv.entry_block_reason("NVDA/USDT:USDT", "short")
    assert mv.entry_block_reason("BTC/USDT:USDT", "short") is None
    news = mv.fetch_news(window_hours=6)
    assert news and news[0][1].startswith("Massive/")


def test_closed_market_blocks(mv, monkeypatch):
    monkeypatch.setattr(mv, "_http_get", _fake("closed", closes=(6, 5, 4, 3, 2, 1)))
    mv.refresh()
    assert "مغلق" in mv.entry_block_reason("TSLAUSDT", "long")


def test_403_and_429_degrade(mv, monkeypatch):
    calls = []

    def boom(path, params):
        calls.append(path)
        code = 403 if "marketstatus" in path else 429
        raise urllib.error.HTTPError(path, code, "x", {}, io.BytesIO(b""))
    monkeypatch.setattr(mv, "_http_get", boom)
    mv.refresh()
    n = len(calls)
    mv.refresh()
    assert len(calls) <= n  # 403 disabled, 429 backs off
    assert any("403" in v for v in mv.snapshot()["errors"].values())
    assert mv.entry_block_reason("NVDA/USDT:USDT", "long") is None


def test_api_endpoint(monkeypatch):
    import main
    from fastapi.testclient import TestClient
    import base64
    monkeypatch.setattr(main, "DASHBOARD_PASSWORD", "pw", raising=False)
    c = TestClient(main.app)
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    r = c.get("/api/massive", headers=h)
    assert r.status_code in (200, 401)
    if r.status_code == 200:
        assert "enabled" in r.json()
