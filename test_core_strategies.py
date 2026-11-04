"""Signal, sizing, regime, daily-loss and kill-switch tests for the two spot strategies."""
import pytest

import core_strategies as cs
import risk_controls
import strategy_engine as se


TF4 = cs.TF_4H_MS
TF1 = cs.TF_1D_MS


@pytest.fixture(autouse=True)
def _reset_kill_switch():
    cs.kill_switch.reset()
    yield
    cs.kill_switch.reset()


def _bar(i, close, high=None, low=None, timeframe=TF4, volume=1000.0):
    close = float(close)
    high = close + 1.0 if high is None else float(high)
    low = close - 1.0 if low is None else float(low)
    opened = close - 0.2
    high = max(high, opened, close)
    low = min(low, opened, close)
    return [i * timeframe, opened, high, low, close, volume]


def _uptrend(n=260, start=100.0, step=0.2, span=1.5):
    candles = []
    price = float(start)
    for i in range(n):
        price += step
        candles.append(_bar(i, price, high=price + span, low=price - span))
    return candles


def _flat(n=260, price=100.0, high=100.0, low=99.0):
    return [_bar(i, price, high=high, low=low) for i in range(n)]


def _rising_daily(n=220, start=50.0):
    candles = []
    price = float(start)
    for i in range(n):
        price += 1.0
        candles.append(_bar(i, price, high=price + 0.5, low=price - 0.5, timeframe=TF1))
    return candles


def test_default_active_strategies_are_only_the_new_ones():
    assert cs.active_strategy_names() == ("trend_pullback", "donchian_breakout")
    assert cs.core_entries_enabled()
    assert not cs.legacy_entries_enabled()
    assert not cs.futures_new_entries_enabled()


def test_legacy_mode_and_explicit_list_reenable_old_strategies(monkeypatch):
    monkeypatch.setenv("STRATEGY_MODE", "legacy")
    monkeypatch.delenv("ACTIVE_STRATEGIES", raising=False)
    names = cs.active_strategy_names()
    assert "bounce" in names and "breakout" in names
    assert "trend_pullback" not in names
    assert cs.legacy_entries_enabled()
    monkeypatch.setenv("ACTIVE_STRATEGIES", "bounce,trend_pullback")
    assert cs.active_strategy_names() == ("bounce", "trend_pullback")
    assert cs.legacy_entries_enabled() and cs.core_entries_enabled()


def test_database_strategy_rows_are_not_the_active_set():
    stored = cs.ignored_db_strategy_settings({"active_strategies": "bounce,breakout", "trade_amount_usdt": "200"})
    assert stored == {"active_strategies": "bounce,breakout"}
    assert cs.active_strategy_names() == ("trend_pullback", "donchian_breakout")


def test_closed_candles_drop_the_unfinished_bar():
    bars = _flat(5)
    forming_open = bars[-1][0]
    assert cs.closed_candles(bars, TF4, forming_open + TF4 - 1) == bars[:-1]
    assert cs.closed_candles(bars, TF4, forming_open + TF4) == bars
    assert cs.closed_candles(bars, TF4, now_ms=None) == bars[:-1]


def test_trend_pullback_requires_reclaim_and_ignores_the_forming_bar():
    candles = _uptrend()
    signal = cs.trend_pullback_setup(candles, spread_pct=0.01)
    assert signal and signal["strategy"] == "trend_pullback" and signal["direction"] == "long"
    assert signal["stop_distance"] == pytest.approx(2 * signal["atr"])

    below = [row[:] for row in candles]
    for _ in range(6):
        ema20 = cs.ema_series([row[4] for row in below], 20)[-1]
        below[-1][4] = ema20 - 1.0
        below[-1][3] = min(below[-1][3], below[-1][4])
        below[-1][2] = max(below[-1][2], below[-1][1], below[-1][4])
    closes = [row[4] for row in below]
    assert closes[-1] < cs.ema_series(closes, 20)[-1]
    assert closes[-1] > cs.ema_series(closes, 200)[-1]
    assert cs.trend_pullback_setup(below, spread_pct=0.01) is None

    forming = _bar(len(candles), candles[-1][4] + 5, high=candles[-1][4] + 6, low=candles[-1][4])
    raw = candles + [forming]
    closed = cs.closed_candles(raw, TF4, forming[0] + 1000)
    assert closed == candles
    assert cs.trend_pullback_setup(closed, spread_pct=0.01)["candle_ts"] == candles[-1][0]


def test_trend_pullback_blocks_downtrend_spike_and_wide_spread():
    down = []
    price = 300.0
    for i in range(260):
        price -= 0.2
        down.append(_bar(i, price, high=price + 1.5, low=price - 1.5))
    assert cs.trend_pullback_setup(down, spread_pct=0.01) is None

    spiked = _uptrend()
    last = spiked[-1][:]
    last[2] = last[4] + 80
    last[3] = last[4] - 80
    spiked[-1] = last
    assert cs.trend_pullback_setup(spiked, spread_pct=0.01) is None
    assert cs.trend_pullback_setup(_uptrend(), spread_pct=1.0) is None


def test_donchian_uses_previous_highs_only_and_btc_regime():
    candles = _flat(260)
    daily = _rising_daily()
    assert cs.btc_daily_bull(daily) is True
    assert cs.donchian_breakout_setup(candles, daily, spread_pct=0.01) is None

    wick = [row[:] for row in candles]
    wick[-1][2] = 130.0  # intrabar high breaks, close does not
    assert cs.donchian_breakout_setup(wick, daily, spread_pct=0.01) is None

    closed_break = [row[:] for row in candles]
    closed_break[-1][4] = 101.0
    closed_break[-1][2] = 101.0
    signal = cs.donchian_breakout_setup(closed_break, daily, spread_pct=0.01)
    assert signal and signal["strategy"] == "donchian_breakout"
    assert signal["breakout_level"] == 100.0
    assert signal["ref_close"] == 101.0

    # The breakout bar is still forming: it must not create a signal.
    forming_open = closed_break[-1][0]
    assert cs.donchian_breakout_setup(
        cs.closed_candles(closed_break, TF4, forming_open + 1000), daily, spread_pct=0.01) is None

    bear = _rising_daily()
    bear[-1] = _bar(len(bear) - 1, 1.0, high=2.0, low=0.5, timeframe=TF1)
    assert cs.btc_daily_bull(bear) is False
    assert cs.donchian_breakout_setup(closed_break, bear, spread_pct=0.01) is None
    assert cs.donchian_breakout_setup(closed_break, _rising_daily(50), spread_pct=0.01) is None
    assert cs.donchian_breakout_setup(closed_break[:100], daily, spread_pct=0.01) is None


def test_donchian_channel_exit_uses_previous_lows_only():
    candles = _flat(20, price=100.0, high=101.0, low=100.0)
    exited = [row[:] for row in candles]
    exited[-1][4] = 99.0
    assert cs.donchian_channel_exit(exited, lookback=10) is True
    wick_only = [row[:] for row in candles]
    wick_only[-1][3] = 50.0
    wick_only[-1][4] = 100.0
    assert cs.donchian_channel_exit(wick_only, lookback=10) is False


def test_symbol_filter_skips_stables_and_futures():
    assert cs.symbol_allowed("BTC/USDT")
    assert not cs.symbol_allowed("USDC/USDT")
    assert not cs.symbol_allowed("BTC/USDT:USDT")
    assert cs.signals_for_symbol("USDC/USDT", _uptrend(), _rising_daily(), 0.01) == []


def test_sizing_is_risk_over_stop_and_refuses_leverage():
    pullback = cs.size_position(10_000, 100, 2, "trend_pullback")
    assert pullback["allowed"]
    assert pullback["risk_pct"] == pytest.approx(0.5)
    assert pullback["risk_usdt"] == pytest.approx(50)
    assert pullback["qty"] == pytest.approx(25)
    assert pullback["notional"] == pytest.approx(2500)

    donchian = cs.size_position(10_000, 100, 2, "donchian_breakout")
    assert donchian["risk_pct"] == pytest.approx(0.25)
    assert donchian["qty"] == pytest.approx(12.5)
    assert not cs.size_position(1_000, 100, 0.01, "trend_pullback")["allowed"]


def test_risk_pct_respects_global_ceiling_and_validation(monkeypatch):
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.2")
    assert cs.effective_risk_pct("trend_pullback") == pytest.approx(0.2)
    monkeypatch.setenv("TREND_PULLBACK_RISK_PCT", "3")
    with pytest.raises(ValueError):
        cs.effective_risk_pct("trend_pullback")
    monkeypatch.delenv("TREND_PULLBACK_RISK_PCT", raising=False)
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0")
    with pytest.raises(ValueError):
        cs.effective_risk_pct("donchian_breakout")


def test_daily_loss_limit_blocks_new_entries_and_fails_closed():
    assert cs.daily_loss_blocks(1000, 980, 2.0) is True
    assert cs.daily_loss_blocks(1000, 981, 2.0) is False
    assert cs.daily_loss_blocks(0, 1000, 2.0) is True
    assert cs.daily_loss_blocks(None, 1000, 2.0) is True
    assert cs.daily_loss_limit_pct(5.0) == pytest.approx(2.0)


def test_entry_gate_covers_kill_switch_positions_and_portfolio_risk():
    ok, reasons = cs.entry_blocked(
        False, 10_000, 10_000, 1, 50, open_risk_usdt=100,
        daily_limit_pct=2, position_cap=4, portfolio_cap_pct=4)
    assert ok is False and reasons == []

    blocked, reasons = cs.entry_blocked(
        True, 10_000, 9_700, 4, 50, open_risk_usdt=400,
        daily_limit_pct=2, position_cap=4, portfolio_cap_pct=4)
    assert blocked
    assert reasons == ["kill_switch", "daily_loss", "max_positions", "portfolio_risk"]


def test_kill_switch_latches_on_bad_data_and_clears_after_a_clean_scan():
    switch = cs.KillSwitch()
    switch.begin_scan()
    assert not switch.blocks_entries()
    stale = [_bar(0, 100)]
    with pytest.raises(cs.DataFault) as caught:
        cs.assert_fresh(stale, TF4, stale[0][0] + TF4 + 7 * 3600 * 1000, 6 * 3600)
    assert caught.value.kind == "stale"
    switch.trip(str(caught.value))
    with pytest.raises(cs.DataFault):
        cs.validate_candles([[1, 10, 9, 11, 10, 1]])  # low above high
    switch.finish_scan()
    assert switch.blocks_entries()
    switch.begin_scan()
    assert switch.blocks_entries()
    switch.finish_scan()
    assert not switch.blocks_entries()


def test_kill_switch_can_be_disabled(monkeypatch):
    monkeypatch.setenv("KILL_SWITCH_ENABLED", "false")
    cs.kill_switch.trip("fetch failed")
    assert not cs.kill_switch.blocks_entries()


def test_evaluate_symbol_does_not_run_legacy_entries(monkeypatch):
    calls = []
    monkeypatch.setattr(se, "_breakout_candidate", lambda *args, **kwargs: calls.append("breakout") or None)
    monkeypatch.setattr(se, "_bounce_candidate", lambda *args, **kwargs: calls.append("bounce") or None)
    monkeypatch.setattr(se, "_trend_candidate", lambda *args, **kwargs: calls.append("trend") or None)

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe="1h", limit=90):
            return [_bar(i, 100, high=101, low=99) for i in range(90)]

    result = se.evaluate_symbol(Exchange(), "BTC/USDT")
    assert calls == []
    assert result["signal"] is False
    assert result["strategy"] == "wait"


def test_portfolio_cap_default_is_the_tighter_limit():
    assert risk_controls.portfolio_open_risk_cap() == pytest.approx(4.0)
    blocked = risk_controls.portfolio_risk_status(
        [], {"symbol": "BTC/USDT", "market": "Spot", "risk_usdt": 500}, 10_000)
    allowed = risk_controls.portfolio_risk_status(
        [], {"symbol": "ETH/USDT", "market": "Spot", "risk_usdt": 300}, 10_000)
    assert not blocked["allowed"]
    assert allowed["allowed"]
