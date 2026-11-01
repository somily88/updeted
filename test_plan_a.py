"""Plan A quick wins: sizing cap, cost-aware targets, -4130 stop replacement, per-symbol freeze."""
import pytest

import futures_engine
import strategy_engine as se
from futures_engine import BinanceFuturesDemo


def test_notional_cap_defaults_to_trade_amount():
    assert se.effective_notional_cap(10000, 200, equity_pct=0) == 200


def test_notional_cap_uses_equity_pct_when_enabled():
    assert se.effective_notional_cap(10000, 200, equity_pct=15) == pytest.approx(1500)
    # Risk sizing then binds instead of the fixed 200 cap.
    n = se.risk_sized_notional(10000, 100, 2, se.effective_notional_cap(10000, 200, 15), 1.0)
    assert n == pytest.approx(1500)
    n = se.risk_sized_notional(10000, 100, 5, se.effective_notional_cap(10000, 200, 15), 0.5)
    assert n == pytest.approx(1000)


def test_target_cost_filter():
    setup = {"strategy": "bounce", "atr": 0.1, "ref_close": 100.0}  # target 0.15%
    assert se.target_covers_costs(setup, multiple=0)
    assert not se.target_covers_costs(setup, multiple=3, cost_pct=0.25)
    setup["atr"] = 1.0  # target 1.5% >= 0.75%
    assert se.target_covers_costs(setup, multiple=3, cost_pct=0.25)


class _StopExchange:
    urls = {"api": {"fapiPrivate": "https://demo-fapi.binance.com/fapi/v1"}}

    def __init__(self, open_orders, fail_create=False):
        self.orders = list(open_orders)
        self.fail_create = fail_create
        self.created, self.deleted = [], []

    def market(self, symbol):
        return {"id": "NVDAUSDT", "contractSize": 1}

    def price_to_precision(self, symbol, price):
        return "{:.2f}".format(float(price))

    def fapiPrivateGetOpenAlgoOrders(self, params):
        return list(self.orders)

    def fapiPrivatePostAlgoOrder(self, params):
        if self.fail_create and params["triggerPrice"] != "210.00":
            raise Exception('binance {"code":-4130}')
        if any(str(o.get("closePosition")) == "true" for o in self.orders):
            raise Exception('binance {"code":-4130}')
        self.created.append(params)
        self.orders.append({"algoId": len(self.created) + 100, "clientAlgoId": params["clientAlgoId"],
                            "triggerPrice": params["triggerPrice"], "closePosition": "true", "side": params["side"]})
        return {"algoId": len(self.created) + 100}

    def fapiPrivateDeleteAlgoOrder(self, params):
        self.deleted.append(params["algoId"])
        self.orders = [o for o in self.orders if str(o["algoId"]) != str(params["algoId"])]
        return {}


def _engine(tmp_path, ex):
    e = BinanceFuturesDemo.__new__(BinanceFuturesDemo)
    e.db_path = str(tmp_path / "f.db")
    e.exchange = ex
    e.on_event = lambda *a, **k: None
    e.on_decision = lambda *a, **k: None
    e.protection_degraded = False
    e._init_db()
    e._open_algo_orders = lambda symbol: list(ex.orders)
    return e


def _pos():
    return {"symbol": "NVDA/USDT:USDT", "side": "short", "entry_price": 205.0, "amount": 1, "contracts": 1,
            "cost": 205, "highest_price": 200, "stop_price": 208.0, "target_price": 195,
            "trailing_distance": 2, "strategy": "trend", "timeframe": "1d", "opened_at": "2026-01-01 00:00:00"}


def test_tightening_close_position_stop_does_not_hit_4130(tmp_path):
    ex = _StopExchange([{"algoId": 7, "clientAlgoId": "stbot_x", "triggerPrice": "210", "closePosition": "true", "side": "BUY"}])
    e = _engine(tmp_path, ex)
    pos = _pos()
    pos["protective_algo_id"] = "7"
    assert e._ensure_exchange_stop("NVDA/USDT:USDT", pos, 208.0)
    assert ex.deleted == ["7"] and ex.created[0]["triggerPrice"] == "208.00"
    assert pos["protective_stop_price"] == 208.0


def test_failed_replacement_restores_previous_stop(tmp_path):
    ex = _StopExchange([{"algoId": 7, "clientAlgoId": "stbot_x", "triggerPrice": "210", "closePosition": "true", "side": "BUY"}],
                       fail_create=True)
    e = _engine(tmp_path, ex)
    pos = _pos()
    pos["protective_algo_id"] = "7"
    with pytest.raises(Exception):
        e._ensure_exchange_stop("NVDA/USDT:USDT", pos, 208.0)
    assert pos["protective_stop_price"] == 210.0  # previous trigger restored, never loosened


def test_foreign_close_position_stop_is_adopted(tmp_path):
    ex = _StopExchange([{"algoId": 9, "clientAlgoId": "manual", "triggerPrice": "208", "closePosition": "true", "side": "BUY"}])
    e = _engine(tmp_path, ex)
    pos = _pos()
    assert e._ensure_exchange_stop("NVDA/USDT:USDT", pos, 208.0)
    assert pos["protective_algo_id"] == "9" and ex.created == []


def test_freeze_scope_defaults():
    assert futures_engine.FUTURES_MONITOR_FREEZE_SCOPE == "symbol"
    assert futures_engine.FUTURES_MONITOR_MAX_FAILED_SYMBOLS >= 0


def test_hedge_only_mode_default_keeps_previous_behaviour():
    assert futures_engine.FUTURES_HEDGE_ONLY_MODE == "hedge"
