"""Firecrawl news source and LLM base URL tests with mocked HTTP."""
import importlib
import io
import json
import sys

import pytest


@pytest.fixture()
def main_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setenv("FIRECRAWL_MAX_CALLS_PER_HOUR", "2")
    monkeypatch.setenv("GROQ_API_KEY", "")
    for name in ("main", "config", "database", "futures_engine", "risk_controls"):
        sys.modules.pop(name, None)
    return importlib.import_module("main")


class _Resp(io.BytesIO):
    pass


def _fake_urlopen(calls, payload):
    def fake(req, timeout=None):
        calls.append((req.full_url, timeout))
        return _Resp(json.dumps(payload).encode())
    return fake


def test_firecrawl_headlines_cached(main_mod, monkeypatch):
    calls = []
    payload = {"success": True, "data": {"web": [{"title": "BTC hits record"}, {"title": "ETH upgrade"}]}}
    monkeypatch.setattr(main_mod.urllib.request, "urlopen", _fake_urlopen(calls, payload))
    first = main_mod.fetch_firecrawl_headlines()
    assert [t for (_, _, t) in first] == ["BTC hits record", "ETH upgrade"]
    assert all(timeout for (_, timeout) in calls)
    n = len(calls)
    second = main_mod.fetch_firecrawl_headlines()
    assert len(calls) == n  # served from cache
    assert second == first


def test_firecrawl_fails_open(main_mod, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(main_mod.urllib.request, "urlopen", boom)
    assert main_mod.fetch_firecrawl_headlines() == []


def test_firecrawl_hourly_limit(main_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(main_mod.urllib.request, "urlopen", _fake_urlopen(calls, {"data": {"web": []}}))
    monkeypatch.setattr(main_mod, "FIRECRAWL_QUERIES", ["a", "b", "c", "d"])
    main_mod.fetch_firecrawl_headlines()
    main_mod.fetch_firecrawl_headlines()
    assert len(calls) == 2


def test_firecrawl_disabled_without_key(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "FIRECRAWL_API_KEY", "")
    assert main_mod.fetch_firecrawl_headlines() == []


def test_firecrawl_headlines_fed_into_news(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "NEWS_FEEDS", [])
    monkeypatch.setattr(main_mod, "fetch_firecrawl_headlines",
                        lambda: [(main_mod.datetime.datetime.now(main_mod.datetime.timezone.utc), "Firecrawl", "Exchange hacked")])
    items = main_mod.fetch_headlines()
    assert items[0][1] == "Firecrawl"


def test_llm_base_detects_xai(main_mod, monkeypatch):
    monkeypatch.setattr(main_mod, "LLM_BASE_URL", "")
    monkeypatch.setattr(main_mod, "GROQ_API_KEY", "xai-abc")
    assert main_mod._llm_base() == "https://api.x.ai/v1"
    monkeypatch.setattr(main_mod, "GROQ_API_KEY", "gsk_abc")
    assert main_mod._llm_base() == "https://api.groq.com/openai/v1"


def test_pick_model_prefers_grok_chat(main_mod, monkeypatch):
    ids = ["grok-imagine-image", "grok-4.20-multi-agent-0309", "grok-4.20-0309-non-reasoning", "grok-4.7"]
    monkeypatch.setattr(main_mod, "_groq_call", lambda path, body=None: {"data": [{"id": i} for i in ids]})
    assert main_mod.groq_pick_model() == "grok-4.20-0309-non-reasoning"
