"""Plan B: limit-then-market entries and the fee/slippage model."""
import pytest

import execution


class _Ex:
    def __init__(self, fill_limit=0.0, status_after_cancel="canceled", reject_post=False, last=100.0):
        self.fill_limit, self.status_after_cancel = fill_limit, status_after_cancel
        self.reject_post, self.last = reject_post, last
        self.calls, self.orders = [], {}

    def fetch_order_book(self, symbol, limit):
        return {"bids": [[99.9, 1]], "asks": [[100.1, 1]]}

    def price_to_precision(self, symbol, p):
        return str(p)

    def amount_to_precision(self, symbol, a):
        return "{:.3f}".format(a)

    def fetch_ticker(self, symbol):
        return {"last": self.last}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append((type_, side, amount, price, params))
        if type_ == "limit" and self.reject_post:
            raise Exception("Order would immediately match and take. -2010")
        oid = str(len(self.calls))
        if type_ == "market":
            o = {"id": oid, "type": "market", "status": "closed", "filled": amount, "cost": amount * 100.2,
                 "average": 100.2, "fees": [{"cost": amount * 100.2 * 0.001, "currency": "USDT"}]}
        else:
            o = {"id": oid, "type": "limit", "status": "open", "filled": 0.0, "cost": 0.0, "price": price}
        self.orders[oid] = o
        return dict(o)

    def fetch_order(self, oid, symbol):
        o = self.orders[oid]
        if o["type"] == "limit" and o["status"] == "canceled":
            return dict(o)
        if o["type"] == "limit" and self.fill_limit >= 1.0:
            o.update(status="closed", filled=1.0, cost=99.9, average=99.9, fees=[{"cost": 0.0, "currency": "USDT"}])
        return dict(o)

    def cancel_order(self, oid, symbol):
        o = self.orders[oid]
        o["status"] = self.status_after_cancel
        if self.fill_limit and self.fill_limit < 1.0:
            o.update(filled=self.fill_limit, cost=self.fill_limit * 99.9, average=99.9,
                     fees=[{"cost": 0.0, "currency": "USDT"}])


def _run(ex, **kw):
    clock = iter(range(0, 1000, 5))
    return execution.place_entry_order(ex, "BTC/USDT", "buy", 1.0, mode="limit_then_market",
                                       reference_price=100.0, sleep=lambda s: None,
                                       clock=lambda: next(clock), **kw)


def test_market_mode_is_unchanged():
    ex = _Ex()
    o = execution.place_entry_order(ex, "BTC/USDT", "buy", 1.0, mode="market")
    assert ex.calls[0][0] == "market" and o["filled"] == 1.0


def test_limit_fills_as_maker():
    ex = _Ex(fill_limit=1.0)
    o = _run(ex)
    assert [c[0] for c in ex.calls] == ["limit"] and o["average"] == pytest.approx(99.9)
    assert ex.calls[0][4] == {"postOnly": True}


def test_partial_fill_then_market_remainder():
    ex = _Ex(fill_limit=0.4)
    o = _run(ex)
    assert [c[0] for c in ex.calls] == ["limit", "market"]
    assert ex.calls[1][2] == pytest.approx(0.6)
    assert o["filled"] == pytest.approx(1.0) and len(o["fees"]) == 2


def test_no_chase_when_price_ran_away():
    ex = _Ex(last=101.0)
    o = _run(ex)
    assert [c[0] for c in ex.calls] == ["limit"] and o["filled"] == 0


def test_unconfirmed_cancel_never_sends_extra_market():
    ex = _Ex(status_after_cancel="open")
    with pytest.raises(RuntimeError):
        _run(ex)
    assert [c[0] for c in ex.calls] == ["limit"]


def test_post_only_rejection_falls_back_to_market():
    ex = _Ex(reject_post=True)
    o = _run(ex)
    assert [c[0] for c in ex.calls] == ["limit", "market"] and o["filled"] == 1.0


def test_futures_uses_gtx():
    ex = _Ex(fill_limit=1.0)
    clock = iter(range(0, 1000, 5))
    execution.place_entry_order(ex, "BTC/USDT:USDT", "buy", 1.0, mode="limit_then_market",
                                sleep=lambda s: None, clock=lambda: next(clock))
    assert ex.calls[0][4] == {"timeInForce": "GTX"}


def test_cost_model():
    assert execution.expected_round_trip_cost_pct("spot", False, 0.05) == pytest.approx(0.3)
    assert execution.expected_round_trip_cost_pct("futures", True, 0.05) == pytest.approx(0.12)
    assert execution.ENTRY_ORDER_MODE == "market"  # safe default
