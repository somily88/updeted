"""Plan C: ADX regime, MTF, volatility-scaled risk, partial TP settings, Donchian, cluster count cap."""
import pytest

import risk_controls
import strategy_engine as se


def _trend_candles(n=90, step=1.0):
    out, p = [], 100.0
    for i in range(n):
        p += step
        out.append([i * 3600_000, p - 0.5, p + 1.0, p - 1.0, p, 1000.0])
    return out


def _chop_candles(n=90):
    out = []
    for i in range(n):
        p = 100.0 + (1.0 if i % 2 else -1.0)
        out.append([i * 3600_000, p, p + 1.5, p - 1.5, p, 1000.0])
    return out


def test_adx_high_in_trend_low_in_chop():
    assert se._adx(_trend_candles()) > 40
    assert se._adx(_chop_candles()) < 20
    assert se._adx(_trend_candles(10)) is None


def test_volatility_scale_never_increases_risk_by_default():
    assert se.volatility_risk_scale(2.0, target_stop_pct=0) == 1.0
    assert se.volatility_risk_scale(4.0, target_stop_pct=2.0) == pytest.approx(0.5)
    assert se.volatility_risk_scale(1.0, target_stop_pct=2.0) == pytest.approx(se.VOL_SCALE_MAX)
    assert se.VOL_SCALE_MAX <= 1.0


def test_defaults_preserve_behaviour():
    assert se.REGIME_FILTER == "off" and not se.MTF_CONFIRM and not se.ENABLE_DONCHIAN_TREND
    assert se.PARTIAL_TP_R == 1.0 and se.PARTIAL_TP_FRACTION == 0.5
    assert risk_controls.MAX_POSITIONS_PER_CLUSTER == 0


class _Ex:
    def __init__(self, candles):
        self.candles = candles

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=90, since=None, params=None):
        return self.candles + [[10**13, 0, 0, 0, self.candles[-1][4], 0]]


def test_donchian_votes_breakout():
    found = se._donchian_candidate(_Ex(_trend_candles(step=2.0)), "X/USDT", "1d", {}, "long")
    assert found and found["strategy"] == "donchian_trend" and found["direction"] == "long"
    assert se._donchian_candidate(_Ex(_chop_candles()), "X/USDT", "1d", {}, "long") is None
    assert "donchian_trend" in se.STRATEGY_RISK


def test_cluster_count_cap(monkeypatch):
    monkeypatch.setattr(risk_controls, "MAX_POSITIONS_PER_CLUSTER", 2)
    open_risks = [{"symbol": "BTC/USDT", "risk_usdt": 10}, {"symbol": "ETH/USDT", "risk_usdt": 10}]
    r = risk_controls.portfolio_risk_status(open_risks, {"symbol": "SOL/USDT", "risk_usdt": 10}, 10000)
    assert not r["allowed"]
    r = risk_controls.portfolio_risk_status(open_risks, {"symbol": "XAU/USDT:USDT", "risk_usdt": 10}, 10000)
    assert r["allowed"]
