"""Offline tests for futures_engine.BinanceFuturesDemo (no network, no API keys)."""
import pytest

import futures_engine
from futures_engine import BinanceFuturesDemo


class _FakeExchange:
    def __init__(self, taker=None, fail=False):
        self.taker, self.fail = taker, fail

    def market(self, symbol):
        if self.fail:
            raise KeyError(symbol)
        return {"taker": self.taker, "contractSize": 1.0}


def _engine(tmp_path, exchange=None):
    engine = BinanceFuturesDemo.__new__(BinanceFuturesDemo)  # skip network in __init__
    engine.db_path = str(tmp_path / "futures.db")
    engine.exchange = exchange or _FakeExchange()
    engine.on_event = lambda *a, **k: None
    engine.on_decision = lambda *a, **k: None
    engine._init_db()
    return engine


def test_requires_credentials():
    with pytest.raises(ValueError):
        BinanceFuturesDemo("", "", ":memory:")


def test_hedge_only_symbols_are_targets():
    assert futures_engine.HEDGE_ONLY_IDS.issubset(set(futures_engine.TARGET_IDS))


def test_estimated_taker_fee_uses_market_rate(tmp_path):
    engine = _engine(tmp_path, _FakeExchange(taker=0.001))
    assert engine._estimated_taker_fee("XAU/USDT:USDT", -1000) == pytest.approx(1.0)


def test_estimated_taker_fee_falls_back(tmp_path):
    engine = _engine(tmp_path, _FakeExchange(fail=True))
    assert engine._estimated_taker_fee("XAU/USDT:USDT", 1000) == pytest.approx(0.5)


def test_position_round_trip(tmp_path):
    engine = _engine(tmp_path)
    pos = {"symbol": "XAU/USDT:USDT", "entry_price": 2000.0, "amount": 0.5, "contracts": 0.5,
           "cost": 1000.0, "highest_price": 2010.0, "stop_price": 1990.0, "target_price": 2030.0,
           "trailing_distance": 5.0, "strategy": "trend", "timeframe": "1h",
           "opened_at": "2026-01-01 00:00:00", "side": "short", "fees_usdt": 0.5}
    engine._save_position(pos)
    loaded = engine._load_positions()
    assert "XAU/USDT:USDT" in loaded
    got = loaded["XAU/USDT:USDT"]
    assert got["side"] == "short"
    assert got["entry_price"] == 2000.0
    assert got["fees_usdt"] == pytest.approx(0.5)


def test_dust_position_is_archived_not_loaded(tmp_path):
    engine = _engine(tmp_path)
    pos = {"symbol": "META/USDT:USDT", "entry_price": 641.85, "amount": 1.3877787807814457e-17,
           "contracts": 1.3877787807814457e-17, "cost": 1.4e-14, "highest_price": 610.57,
           "stop_price": 641.85, "target_price": 621.0, "trailing_distance": 5.0,
           "strategy": "trend", "timeframe": "4h", "opened_at": "2026-10-26 16:00:52",
           "side": "short", "fees_usdt": 0.1}
    engine._save_position(pos)
    assert "META/USDT:USDT" not in engine._load_positions()
    assert "META/USDT:USDT" not in engine._load_positions()
    with engine._connect() as conn:
        reasons = [r[0] for r in conn.execute("SELECT exit_reason FROM futures_trades")]
    assert reasons == ["zero_qty_cleanup"]
