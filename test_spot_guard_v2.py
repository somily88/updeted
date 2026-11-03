import spot_rules as sr


def test_legacy_stop_cap_tightens_wide_stop():
    assert abs(sr.capped_legacy_stop(100.0, 92.5, cap_pct=6, enabled=True) - 94.0) < 1e-9
    assert sr.capped_legacy_stop(100.0, 0, cap_pct=6, enabled=True) == 94.0


def test_legacy_stop_cap_never_loosens_or_runs_when_disabled():
    assert sr.capped_legacy_stop(100.0, 95.0, cap_pct=6, enabled=True) is None
    assert sr.capped_legacy_stop(100.0, 94.0, cap_pct=6, enabled=True) is None
    assert sr.capped_legacy_stop(100.0, 80.0, cap_pct=6, enabled=False) is None
    assert sr.capped_legacy_stop(100.0, 80.0, cap_pct=0, enabled=True) is None


def test_stop_breach_watchdog():
    assert sr.stop_breached(84.0, 94.0, enabled=True)  # gapped through (NEAR -15% case)
    assert sr.stop_breached(94.0, 94.0, enabled=True)
    assert not sr.stop_breached(94.1, 94.0, enabled=True)
    assert not sr.stop_breached(80.0, 94.0, enabled=False)
    assert not sr.stop_breached(80.0, 0, enabled=True)


def test_trend_paused_by_default_and_bounce_untouched():
    ok, rule, _ = sr.trend_v2_allowed("trend", enabled=False)
    assert not ok and rule == "trend_paused"
    assert sr.trend_v2_allowed("bounce", enabled=False) == (True, "", "")


def test_trend_daily_confirm():
    up = [100 + i for i in range(40)]
    down = [200 - i for i in range(40)]
    vols = [10.0] * 20 + [15.0]
    assert sr.trend_v2_allowed("trend", up, up[-1], vols, enabled=True, require_1d=True, vol_mult=1.2)[0]
    ok, rule, _ = sr.trend_v2_allowed("trend", down, down[-1], vols, enabled=True, require_1d=True, vol_mult=1.2)
    assert not ok and rule == "trend_1d_confirm"
    ok, rule, _ = sr.trend_v2_allowed("trend", [1, 2], 2, vols, enabled=True, require_1d=True, vol_mult=1.2)
    assert not ok and rule == "trend_1d_confirm"


def test_trend_volume_confirm():
    assert sr.volume_confirm([10.0] * 20 + [12.0], mult=1.2, lookback=20)[0]
    assert sr.volume_confirm([10.0] * 20 + [11.9], mult=1.2, lookback=20)[0] is False
    assert sr.volume_confirm([10.0] * 5, mult=1.2, lookback=20)[0] is None
    assert sr.volume_confirm([], mult=0)[0] is True
    ok, rule, _ = sr.trend_v2_allowed("trend", None, 1, [10.0] * 20 + [5.0], enabled=True,
                                      require_1d=False, vol_mult=1.2)
    assert not ok and rule == "trend_volume"


def test_block_counter():
    c = sr.BlockCounter()
    for r in ("fee_target", "news_block", "fee_target", None):
        c.add(r)
    assert c.snapshot() == {"fee_target": 2, "news_block": 1}
    assert list(c.snapshot())[0] == "fee_target"
