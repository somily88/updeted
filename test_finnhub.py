import importlib
import io
import time
import urllib.error

import pytest


@pytest.fixture
def fh(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setenv("FINNHUB_ENABLED", "true")
    import finnhub_client
    m = importlib.reload(finnhub_client)
    yield m
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    importlib.reload(finnhub_client)


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    import finnhub_client
    m = importlib.reload(finnhub_client)
    assert not m.FINNHUB_ENABLED
    assert m.fetch_news() == [] and m.entry_block_reason("NVDA/USDT:USDT") is None
    assert m.snapshot()["has_key"] is False


def test_news_cache_and_dedupe(fh, monkeypatch):
    now = time.time()
    calls = []

    def fake(path, params):
        calls.append(path)
        if path == "news":
            return [{"datetime": now - 60, "headline": "Bitcoin rallies!", "source": "X"},
                    {"datetime": now - 99999, "headline": "old", "source": "X"}]
        return [{"datetime": now - 30, "headline": "bitcoin rallies", "source": "Y"}]
    monkeypatch.setattr(fh, "_http_get", fake)
    items = fh.fetch_news(window_hours=6, symbols=["NVDA"])
    n1 = len(calls)
    fh.fetch_news(window_hours=6, symbols=["NVDA"])
    assert len(calls) == n1  # cached
    assert len(fh.dedupe(items)) == 1


def test_economic_403_graceful(fh, monkeypatch, capsys):
    def fake(path, params):
        if path == "calendar/economic":
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        return {"earningsCalendar": []}
    monkeypatch.setattr(fh, "_http_get", fake)
    fh.refresh_calendars()
    fh._cache.clear()
    fh.refresh_calendars()
    assert fh.state["economic_available"] is False
    assert capsys.readouterr().out.count("غير متاح") == 1


def test_earnings_block(fh, monkeypatch):
    import datetime
    today = datetime.date.today().isoformat()

    def fake(path, params):
        if path == "calendar/earnings":
            return {"earningsCalendar": [{"symbol": params["symbol"], "date": today, "hour": "amc"}]}
        return {"economicCalendar": []}
    monkeypatch.setattr(fh, "_http_get", fake)
    fh.refresh_calendars()
    r = fh.entry_block_reason("NVDA/USDT:USDT")
    assert r and "أرباح" in r
    assert fh.entry_block_reason("BTC/USDT") is None
    assert fh.entry_block_reason("NVDA/USDT:USDT", now=time.time() + 10 * 86400) is None


def test_event_pause(fh, monkeypatch):
    t = time.time() + 600
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))

    def fake(path, params):
        if path == "calendar/economic":
            return {"economicCalendar": [{"country": "US", "event": "CPI MoM", "impact": "high", "time": ts},
                                         {"country": "DE", "event": "CPI", "impact": "high", "time": ts}]}
        return {"earningsCalendar": []}
    monkeypatch.setattr(fh, "_http_get", fake)
    fh.refresh_calendars()
    assert len(fh.state["events"]) == 1
    assert "حدث اقتصادي" in fh.entry_block_reason("BTC/USDT")
    assert fh.snapshot()["upcoming_events"][0]["event"] == "CPI MoM"


def test_network_error_never_raises(fh, monkeypatch):
    def boom(path, params):
        raise OSError("down")
    monkeypatch.setattr(fh, "_http_get", boom)
    assert fh.fetch_news() == []
    fh.refresh_calendars()
    assert fh.state["errors"]


def test_rate_limit(fh, monkeypatch):
    monkeypatch.setattr(fh, "FINNHUB_MAX_CALLS_PER_MIN", 2)
    monkeypatch.setattr(fh, "_http_get", lambda p, q: [])
    for i in range(5):
        fh._get("x", "news", {"i": i}, 0)
    assert fh.state["calls"] == 2
