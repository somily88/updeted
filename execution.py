"""Entry execution helpers: post-only limit first, market fallback after a timeout.

Only used for ENTRY orders. Protective stops and exits stay market orders so a
stop is never delayed by waiting for a maker fill.
"""
import os
import time

ENTRY_ORDER_MODE = os.getenv("ENTRY_ORDER_MODE", "market").strip().lower()
if ENTRY_ORDER_MODE not in ("market", "limit_then_market"):
    raise ValueError("ENTRY_ORDER_MODE must be market or limit_then_market")
LIMIT_ENTRY_TIMEOUT_SECONDS = float(os.getenv("LIMIT_ENTRY_TIMEOUT_SECONDS", "20"))
LIMIT_ENTRY_POLL_SECONDS = float(os.getenv("LIMIT_ENTRY_POLL_SECONDS", "2"))
# Skip the market fallback when price ran away more than this percent (0 = always fall back).
LIMIT_ENTRY_MAX_CHASE_PCT = float(os.getenv("LIMIT_ENTRY_MAX_CHASE_PCT", "0.3"))
if not 1 <= LIMIT_ENTRY_TIMEOUT_SECONDS <= 300 or LIMIT_ENTRY_POLL_SECONDS <= 0:
    raise ValueError("LIMIT_ENTRY_TIMEOUT_SECONDS must be 1..300 and poll > 0")

# Default fee schedule (fraction per side) used for estimates and the backtest.
FEE_SCHEDULE = {
    "spot": {"maker": 0.001, "taker": 0.001},
    "futures": {"maker": 0.0002, "taker": 0.0005},
    "tradfi": {"maker": 0.0, "taker": 0.0004},
}


def fee_rate(venue, maker=False):
    return FEE_SCHEDULE.get(venue, FEE_SCHEDULE["spot"])["maker" if maker else "taker"]


def expected_round_trip_cost_pct(venue, maker_entry=False, slippage_pct=0.05):
    """Entry + market exit fees plus slippage on the taker legs, in percent."""
    entry = fee_rate(venue, maker_entry) * 100 + (0 if maker_entry else slippage_pct)
    exit_ = fee_rate(venue, False) * 100 + slippage_pct
    return entry + exit_


def _order_fees(exchange, order, symbol):
    fees = list(order.get("fees") or ([order["fee"]] if order.get("fee") else []))
    if not fees and order.get("id") and float(order.get("filled") or 0) > 0:
        fetch = getattr(exchange, "fetch_order_trades", None)
        if callable(fetch):
            try:
                for trade in fetch(order["id"], symbol) or []:
                    fees.extend(trade.get("fees") or ([trade["fee"]] if trade.get("fee") else []))
            except Exception:
                pass
    return fees


def merge_orders(exchange, symbol, orders):
    """Combine several fills into one CCXT-like order with real fees."""
    orders = [o for o in orders if o and float(o.get("filled") or 0) > 0]
    if not orders:
        return None
    filled = sum(float(o.get("filled") or 0) for o in orders)
    cost = sum(float(o.get("cost") or 0) or float(o.get("filled") or 0) * float(o.get("average") or o.get("price") or 0)
               for o in orders)
    fees = [fee for o in orders for fee in _order_fees(exchange, o, symbol)]
    merged = dict(orders[-1])
    merged.update({"filled": filled, "cost": cost, "average": cost / filled if filled else None,
                   "status": "closed", "fees": fees, "fee": None,
                   "child_order_ids": [o.get("id") for o in orders],
                   "maker_filled": sum(float(o.get("filled") or 0) for o in orders if o.get("type") == "limit")})
    return merged


def place_entry_order(exchange, symbol, side, amount, mode=None, reference_price=None,
                      params=None, sleep=time.sleep, clock=time.monotonic):
    """Submit an entry. Returns a CCXT-like order (possibly merged from two fills)."""
    mode = mode or ENTRY_ORDER_MODE
    params = dict(params or {})
    if mode == "market":
        return exchange.create_order(symbol, "market", side, amount, None, params or None) if params \
            else exchange.create_order(symbol, "market", side, amount)
    book = exchange.fetch_order_book(symbol, 5)
    best = (book.get("bids") or [[None]])[0][0] if side == "buy" else (book.get("asks") or [[None]])[0][0]
    if not best:
        return exchange.create_order(symbol, "market", side, amount)
    price = float(exchange.price_to_precision(symbol, best))
    limit_params = dict(params, timeInForce="GTX") if ":" in symbol else dict(params, postOnly=True)
    try:
        limit = exchange.create_order(symbol, "limit", side, amount, price, limit_params)
    except Exception as exc:
        # Post-only rejected (would take liquidity): go straight to market.
        if "post" in str(exc).lower() or "-5022" in str(exc) or "-2010" in str(exc):
            return exchange.create_order(symbol, "market", side, amount)
        raise
    deadline = clock() + LIMIT_ENTRY_TIMEOUT_SECONDS
    while True:
        limit = exchange.fetch_order(limit["id"], symbol)
        if str(limit.get("status")) in ("closed", "canceled", "cancelled", "expired", "rejected"):
            break
        if clock() >= deadline:
            try:
                exchange.cancel_order(limit["id"], symbol)
            except Exception:
                pass
            limit = exchange.fetch_order(limit["id"], symbol)
            break
        sleep(LIMIT_ENTRY_POLL_SECONDS)
    filled = float(limit.get("filled") or 0)
    remaining = float(amount) - filled
    if str(limit.get("status")) in ("open", "new", "partially_filled"):
        raise RuntimeError("تعذر تأكيد إلغاء أمر الدخول المحدد؛ لم يُرسل أمر سوق إضافي")
    orders = [limit]
    if remaining > 0:
        remaining = float(exchange.amount_to_precision(symbol, remaining))
    if remaining > 0:
        ref = float(reference_price or price)
        last = float((exchange.fetch_ticker(symbol) or {}).get("last") or ref)
        chase = (last / ref - 1) * 100 * (1 if side == "buy" else -1)
        if filled > 0 or LIMIT_ENTRY_MAX_CHASE_PCT <= 0 or chase <= LIMIT_ENTRY_MAX_CHASE_PCT:
            try:
                orders.append(exchange.create_order(symbol, "market", side, remaining))
            except Exception:
                if filled <= 0:
                    raise
    merged = merge_orders(exchange, symbol, orders)
    if merged is None:
        return dict(limit, filled=0.0, status="canceled")
    return merged
