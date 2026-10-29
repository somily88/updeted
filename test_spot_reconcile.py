import importlib
import sys

import pytest


@pytest.fixture
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "x")
    monkeypatch.setenv("TRADING_ENABLED", "false")
    for name in ("main", "config", "database", "futures_engine", "risk_controls"):
        sys.modules.pop(name, None)
    main = importlib.import_module("main")
    main.init_db()
    monkeypatch.setattr(main, "_spot_demo_endpoint_ready", lambda: True)
    monkeypatch.setattr(main, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(main, "record_decision", lambda *a, **k: None)
    stops = []
    monkeypatch.setattr(main, "_ensure_spot_protective_stop",
                        lambda s, p, price=None: stops.append((s, p['amount'], price)) or True)
    main._test_stops = stops
    main.active_positions.clear()
    yield main
    main.active_positions.clear()


def _pos(**kw):
    p = {'amount': 0.30461675, 'cost': 224.89, 'stop_price': 720.68, 'simulated': False,
         'partial_attempted': False, 'partial_taken': False, 'exit_attempted': False,
         'reconciliation_required': True, 'protective_order_id': None}
    p.update(kw)
    return p


def _balance(monkeypatch, m, bnb):
    monkeypatch.setattr(m, "retry_api_read", lambda fn, *a, **k: {'total': {'BNB': bnb}})
    monkeypatch.setattr(m.exchange, "market", lambda s: {'precision': {'amount': 0.001}, 'limits': {}})


def test_stale_partial_flag_cleared_when_balance_proves_no_sale(m, monkeypatch):
    _balance(monkeypatch, m, 0.30461675)
    m.active_positions['BNB/USDT'] = _pos(partial_attempted=True)
    assert m._reconcile_spot_positions() is True
    p = m.active_positions['BNB/USDT']
    assert not p['reconciliation_required'] and not p['partial_attempted']
    assert m._test_stops


def test_exit_flag_cleared_with_bnb_fee_shortfall_and_amount_clamped(m, monkeypatch):
    _balance(monkeypatch, m, 0.3044)  # BNB fee/lot dust missing
    m.active_positions['BNB/USDT'] = _pos(exit_attempted=True)
    assert m._reconcile_spot_positions() is True
    p = m.active_positions['BNB/USDT']
    assert p['amount'] == pytest.approx(0.3044) and not p['exit_attempted']


def test_real_shortfall_stays_frozen(m, monkeypatch):
    _balance(monkeypatch, m, 0.15)  # partial sell actually filled
    m.active_positions['BNB/USDT'] = _pos(partial_attempted=True)
    assert m._reconcile_spot_positions() is False
    p = m.active_positions['BNB/USDT']
    assert p['reconciliation_required'] and p['partial_attempted']
    assert not m._test_stops


def test_min_cost_skip_combo_does_not_freeze(m, monkeypatch):
    _balance(monkeypatch, m, 0.30461675)
    m.active_positions['BNB/USDT'] = _pos(partial_attempted=True, partial_taken=True)
    assert m._reconcile_spot_positions() is True
    assert not m.active_positions['BNB/USDT']['reconciliation_required']


def test_frozen_symbol_does_not_block_other_entries(m, monkeypatch):
    _balance(monkeypatch, m, 0.15)
    m.active_positions['BNB/USDT'] = _pos(exit_attempted=True)
    assert m.check_and_close_positions() is True
    assert m.spot_frozen_symbols == {'BNB/USDT'}
