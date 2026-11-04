"""Read-only reconciliation of closed Spot trades against Binance fills.

This module never creates, cancels, or edits orders. Prices and fees come only
from exchange fills (or, for a non-quote fee, from that minute's candle). When a
fill cannot be proven, the trade stays unverified and the stored prices are left
as they were.
"""
import datetime
import json
import logging
import os
import sqlite3
import time

from trade_stats import r_multiple, spot_unverified_sql, spot_verified_sql

logger = logging.getLogger("spot_fill_reconcile")

VERIFIED_QUALITIES = ("actual", "estimated")
ENTRY_OUTCOMES = ("تم فتح الصفقة", "أُضيف إلى المركز", "صفقة اختبار فُتحت")
EXIT_OUTCOMES = (
    "تم تنفيذ الإغلاق",
    "تم تنفيذ وقف Binance Demo",
    "تم تنفيذ جني جزئي",
    "إغلاق جزئي؛ المركز ما زال مفتوحًا",
)
REASON_AR = {
    "order_id_missing": "معرف الأمر غير محفوظ",
    "no_fills": "لا تنفيذات",
    "quantity_mismatch": "اختلاف الكمية",
    "fee_currency_unpriced": "عمولة بعملة بلا سعر",
    "incomplete_fill": "بيانات تنفيذ ناقصة",
    "ambiguous_order": "أوامر غير محسومة",
}
_KNOWN_FEE = ("actual", "estimated", "simulated")
_SNAPSHOT_FIELDS = (
    "entry_price", "exit_price", "pnl_pct", "pnl_usdt", "status", "cost_quality",
    "gross_pnl_usdt", "fees_usdt", "risk_usdt", "r_multiple", "strategy", "timeframe",
    "timestamp", "entry_order_ids", "exit_order_ids",
)


class ExchangeUnavailable(Exception):
    """Binance could not be read. The message is only an exception class name."""


class ReadOnlySpotExchange:
    """Whitelist of private reads. Anything else, including order placement, is refused."""

    def __init__(self, raw):
        self._raw = raw

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        return self._raw.fetch_my_trades(symbol, since, limit, params or {})

    def fetch_order(self, order_id, symbol):
        return self._raw.fetch_order(order_id, symbol)

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
        return self._raw.fetch_ohlcv(symbol, timeframe, since, limit)

    def __getattr__(self, name):
        raise AttributeError("read-only spot reconcile client has no " + name)


def enabled():
    return str(os.getenv("SPOT_RECONCILE_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "on")


def interval_minutes():
    try:
        value = int(str(os.getenv("SPOT_RECONCILE_INTERVAL_MIN", "60")).strip() or "60")
    except ValueError:
        value = 60
    return max(1, min(value, 24 * 60))


def pause_seconds():
    try:
        return max(0.0, float(os.getenv("SPOT_RECONCILE_PAUSE_SEC", "0.25")))
    except ValueError:
        return 0.25


def qty_tolerance():
    try:
        return max(0.0, float(os.getenv("SPOT_RECONCILE_QTY_TOLERANCE", "0.005")))
    except ValueError:
        return 0.005


def append_order_id(existing, order_id):
    text = str(order_id or "").strip()
    if not text or any(ch.isspace() for ch in text) or len(text) > 64:
        return existing or None
    parts = split_order_ids(existing)
    if text not in parts:
        parts.append(text)
    return ",".join(parts) if parts else None


def split_order_ids(value):
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]


def combine_fee_quality(*qualities):
    """Keep a real fill's fee quality. Never downgrade a known fill to unverified."""
    vals = [q if q in _KNOWN_FEE else "unverified" for q in qualities]
    if not vals:
        return "unverified"
    if all(v == "simulated" for v in vals):
        return "simulated"
    if any(v == "simulated" for v in vals):
        return "unverified"
    if all(v == "actual" for v in vals):
        return "actual"
    if any(v in ("actual", "estimated") for v in vals):
        return "estimated"
    return "unverified"


def closed_spot_quality(simulated, entry_quality, exit_quality):
    if simulated:
        return "simulated"
    return combine_fee_quality(entry_quality, exit_quality)


def warning_ar(unverified, matched, unmatched, reasons, exchange_unavailable=False):
    parts = []
    if unverified:
        parts.append(
            "تنبيه: {} صفقة سبوت غير موثّقة. ربحها غير داخل في المحقق حتى تُطابق التكلفة.".format(int(unverified))
        )
    if unverified or matched or unmatched:
        text = "{} طُوبقت، {} لم تُطابق".format(int(matched), int(unmatched))
        labels = []
        for key, count in sorted((reasons or {}).items()):
            if not count:
                continue
            labels.append("{} {}".format(int(count), REASON_AR.get(key, key)))
        if labels:
            text += " (" + "، ".join(labels) + ")"
        parts.append(text + ".")
    if exchange_unavailable:
        parts.append("Binance غير متاح الآن؛ ستُعاد المطابقة لاحقًا.")
    return " ".join(parts)


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _columns(conn, table):
    found = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not found:
        return set()
    return {row[1] for row in conn.execute("PRAGMA table_info({})".format(table))}


def ensure_schema(conn):
    if "trades" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return False
    existing = _columns(conn, "trades")
    for column, definition in (
        ("entry_order_ids", "TEXT"),
        ("exit_order_ids", "TEXT"),
        ("stop_price", "REAL"),
        ("reconcile_status", "TEXT"),
        ("reconcile_reason", "TEXT"),
        ("reconciled_at", "TEXT"),
    ):
        if column not in existing:
            conn.execute("ALTER TABLE trades ADD COLUMN {} {}".format(column, definition))
    conn.execute("""CREATE TABLE IF NOT EXISTS spot_reconcile_original (
        trade_id INTEGER PRIMARY KEY,
        original_json TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS spot_reconcile_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        original_json TEXT NOT NULL,
        reconciled_json TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS spot_reconcile_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        updated_at TEXT,
        last_error TEXT,
        matched INTEGER NOT NULL DEFAULT 0,
        unmatched INTEGER NOT NULL DEFAULT 0,
        reasons_json TEXT,
        exchange_unavailable INTEGER NOT NULL DEFAULT 0,
        r_backfilled INTEGER NOT NULL DEFAULT 0
    )""")
    return True


def _parse_ts(value):
    if value is None:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return parsed.timestamp()


def _row_dict(row):
    return {key: row[key] for key in row.keys()}


def _snapshot(row):
    data = _row_dict(row)
    return {key: data.get(key) for key in _SNAPSHOT_FIELDS if key in data or True}


def _read(fn, *args, empty_on_not_found=False):
    """Read wrapper. Only a per-order NotFound becomes an empty result.

    Any other failure, including a rejected trade-history call, aborts the run
    so the trade is not labeled "no fills" and a later run can retry.
    """
    try:
        return fn(*args)
    except Exception as exc:
        if empty_on_not_found and "notfound" in type(exc).__name__.lower():
            return None
        raise ExchangeUnavailable(type(exc).__name__) from None


def _fee_items(fill):
    fees = fill.get("fees")
    if fees:
        return list(fees)
    fee = fill.get("fee")
    if isinstance(fee, dict):
        return [fee]
    info = fill.get("info") or {}
    if info.get("commission") is not None and info.get("commissionAsset"):
        return [{"cost": info.get("commission"), "currency": info.get("commissionAsset")}]
    return None


def _side(fill):
    side = str(fill.get("side") or (fill.get("info") or {}).get("side") or "").lower()
    return side if side in ("buy", "sell") else ""


def _amount_cost(fill):
    amount = fill.get("amount")
    if amount is None:
        amount = fill.get("filled")
    try:
        amount = float(amount or 0.0)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    try:
        cost = float(fill.get("cost") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    price = fill.get("price")
    if price is None:
        price = fill.get("average")
    try:
        price = float(price or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    if cost <= 0 and price > 0:
        cost = price * amount
    if cost <= 0:
        return None
    return amount, cost, (price if price > 0 else cost / amount)


def _quote_price(client, currency, timestamp_ms, quote, cache, pause, sleeper):
    minute = int((timestamp_ms or 0) // 60000)
    key = (currency, quote, minute)
    if key in cache:
        return cache[key]
    symbol = "{}/{}".format(currency, quote)
    since = int(timestamp_ms) - 60000 if timestamp_ms else None
    candles = _read(client.fetch_ohlcv, symbol, "1m", since, 3)
    sleeper(pause)
    price = None
    if candles:
        chosen = None
        for candle in candles:
            if not timestamp_ms or int(candle[0]) <= int(timestamp_ms):
                chosen = candle
        if chosen is None:
            chosen = candles[0]
        try:
            close = float(chosen[4] or 0.0)
        except (TypeError, ValueError, IndexError):
            close = 0.0
        if close > 0:
            price = close
    cache[key] = price
    return price


def summarize_leg(fills, symbol, expected_side, quote_price_fn):
    """Sum one order leg. Returns (summary, reason). Reason is set when the leg is not provable."""
    if not fills:
        return None, "no_fills"
    base, quote = symbol.split("/")
    base, quote = base.upper(), quote.upper()
    qty = quote_cost = quote_fee = base_fee = 0.0
    used = 0
    for fill in fills:
        side = _side(fill)
        if side and side != expected_side:
            continue
        parsed = _amount_cost(fill)
        if not parsed:
            return None, "incomplete_fill"
        amount, cost, _price = parsed
        fees = _fee_items(fill)
        if fees is None:
            return None, "incomplete_fill"
        qty += amount
        quote_cost += cost
        used += 1
        for fee in fees:
            try:
                fee_cost = abs(float(fee.get("cost") or 0.0))
            except (TypeError, ValueError):
                return None, "incomplete_fill"
            currency = str(fee.get("currency") or "").upper()
            if fee_cost == 0:
                continue
            if not currency:
                return None, "incomplete_fill"
            if currency == quote or (quote == "USDT" and currency == "USDC"):
                quote_fee += fee_cost
            elif currency == base:
                base_fee += fee_cost
            else:
                px = quote_price_fn(currency, fill.get("timestamp"), quote)
                if not px or px <= 0:
                    return None, "fee_currency_unpriced"
                quote_fee += fee_cost * px
    if used == 0 or qty <= 0 or quote_cost <= 0:
        return None, "no_fills"
    net_qty = (qty - base_fee) if expected_side == "buy" else (qty + base_fee)
    if net_qty <= 0:
        return None, "incomplete_fill"
    return {
        "qty": qty,
        "net_qty": net_qty,
        "quote_cost": quote_cost,
        "quote_fee": quote_fee,
        "base_fee": base_fee,
        "price": quote_cost / qty,
    }, None


def quantities_match(entry_net, exit_net, tolerance):
    if entry_net <= 0 or exit_net <= 0:
        return False
    scale = max(entry_net, exit_net)
    return abs(entry_net - exit_net) <= max(1e-8, float(tolerance) * scale)


def build_reconciliation(entry, exit_leg):
    basis = float(entry["quote_cost"]) + float(entry["quote_fee"])
    if basis <= 0:
        return None
    gross = float(exit_leg["quote_cost"]) - float(entry["quote_cost"])
    fees = float(entry["quote_fee"]) + float(exit_leg["quote_fee"])
    pnl = float(exit_leg["quote_cost"]) - float(exit_leg["quote_fee"]) - basis
    return {
        "entry_price": float(entry["price"]),
        "exit_price": float(exit_leg["price"]),
        "pnl_usdt": pnl,
        "pnl_pct": pnl / basis * 100.0,
        "gross_pnl_usdt": gross,
        "fees_usdt": fees,
        "status": "WIN" if pnl > 0 else "LOSS",
        "cost_quality": "actual",
    }


def assign_decision_orders(trades, decisions):
    """Give each trade the bot's own order ids from the non-overlapping window around its close."""
    ordered = sorted(trades, key=lambda row: (_parse_ts(row["timestamp"]) or 0.0, int(row["id"])))
    times = [(_parse_ts(row["timestamp"]) or 0.0) for row in ordered]
    bounds = []
    for index, moment in enumerate(times):
        start = (moment - 7 * 86400) if index == 0 else (times[index - 1] + moment) / 2.0
        end = moment + 15 * 60
        bounds.append((start, end))
    assigned = {int(row["id"]): {"entry": [], "exit": []} for row in ordered}
    for decision in decisions:
        moment = _parse_ts(decision.get("created_at"))
        order_id = str(decision.get("order_id") or "").strip()
        outcome = str(decision.get("outcome") or "")
        if moment is None or not order_id:
            continue
        leg = "entry" if outcome in ENTRY_OUTCOMES else "exit" if outcome in EXIT_OUTCOMES else None
        if leg is None:
            continue
        for row, (start, end) in zip(ordered, bounds):
            if start <= moment <= end:
                bucket = assigned[int(row["id"])][leg]
                if order_id not in bucket:
                    bucket.append(order_id)
                break
    return assigned


def _order_as_fill(order):
    if not order:
        return None
    try:
        filled = float(order.get("filled") or order.get("amount") or 0.0)
    except (TypeError, ValueError):
        return None
    if filled <= 0:
        return None
    fill = dict(order)
    fill["amount"] = filled
    if not _side(fill):
        return None
    return fill


def fetch_symbol_trades(client, symbol, since_ms, until_ms, pause, sleeper, page_limit=500, max_pages=20):
    rows = []
    seen = set()
    since = since_ms
    for _page in range(max_pages):
        batch = _read(client.fetch_my_trades, symbol, since, page_limit, {}) or []
        sleeper(pause)
        if not batch:
            break
        last_ts = 0
        for trade in batch:
            key = trade.get("id")
            if key is None:
                key = (trade.get("order"), trade.get("timestamp"), trade.get("amount"), trade.get("side"))
            if key in seen:
                continue
            seen.add(key)
            last_ts = int(trade.get("timestamp") or last_ts or 0)
            if until_ms and trade.get("timestamp") and int(trade["timestamp"]) > int(until_ms):
                continue
            rows.append(trade)
        if len(batch) < page_limit or not last_ts or (since and last_ts <= since):
            break
        since = last_ts + 1
    return rows


def _index_by_order(trades):
    grouped = {}
    for trade in trades:
        order_id = str(trade.get("order") or (trade.get("info") or {}).get("orderId") or "").strip()
        if order_id:
            grouped.setdefault(order_id, []).append(trade)
    return grouped


def _fills_for_orders(client, symbol, order_ids, cache, pause, sleeper):
    fills = []
    for order_id in order_ids:
        if order_id in cache and cache[order_id]:
            fills.extend(cache[order_id])
            continue
        order = _read(client.fetch_order, str(order_id), symbol, empty_on_not_found=True)
        sleeper(pause)
        fill = _order_as_fill(order)
        if fill is not None:
            fills.append(fill)
            cache.setdefault(order_id, []).append(fill)
    return fills


def _capture_original(conn, trade_id, snapshot, now):
    conn.execute(
        "INSERT OR IGNORE INTO spot_reconcile_original (trade_id, original_json, captured_at) VALUES (?, ?, ?)",
        (trade_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str), now),
    )


def _write_audit(conn, trade_id, action, reason, snapshot, reconciled, now):
    previous = conn.execute(
        "SELECT action, reason FROM spot_reconcile_audit WHERE trade_id=? ORDER BY id DESC LIMIT 1",
        (trade_id,),
    ).fetchone()
    if previous and previous["action"] == action and (previous["reason"] or None) == (reason or None) and action != "matched":
        return
    conn.execute(
        """INSERT INTO spot_reconcile_audit
           (trade_id, created_at, action, reason, original_json, reconciled_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (trade_id, now, action, reason,
         json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str),
         json.dumps(reconciled, ensure_ascii=False, sort_keys=True, default=str) if reconciled is not None else None),
    )


def backfill_r_multiples(conn):
    """Fill r_multiple only where a positive initial risk was already stored. Never invent a stop."""
    cols = _columns(conn, "trades")
    if "r_multiple" not in cols or "risk_usdt" not in cols or "pnl_usdt" not in cols:
        return 0
    rows = conn.execute(
        """SELECT id, pnl_usdt, risk_usdt FROM trades
           WHERE r_multiple IS NULL AND risk_usdt IS NOT NULL AND risk_usdt > 0
             AND pnl_usdt IS NOT NULL AND """ + spot_verified_sql()
    ).fetchall()
    updated = 0
    for row in rows:
        value = r_multiple(row["pnl_usdt"], row["risk_usdt"])
        if value is None:
            continue
        conn.execute("UPDATE trades SET r_multiple=? WHERE id=? AND r_multiple IS NULL", (value, row["id"]))
        updated += 1
    return updated


def _load_decisions(conn, symbols):
    if "decision_log" not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return []
    cols = _columns(conn, "decision_log")
    if not {"symbol", "order_id", "outcome"}.issubset(cols):
        return []
    created = "created_at" if "created_at" in cols else "NULL AS created_at"
    market = "market" if "market" in cols else "'Spot' AS market"
    query = "SELECT symbol, outcome, {}, {}, order_id FROM decision_log".format(created, market)
    rows = []
    wanted = set(symbols)
    for row in conn.execute(query):
        if row["symbol"] not in wanted:
            continue
        market_name = str(row["market"] or "")
        if market_name and "spot" not in market_name.lower():
            continue
        rows.append({"symbol": row["symbol"], "created_at": row["created_at"],
                     "outcome": row["outcome"], "order_id": row["order_id"]})
    return rows


def _candidate_rows(conn, min_age_sec):
    if "reconcile_reason" not in _columns(conn, "trades"):
        return []
    rows = []
    now = time.time()
    for row in conn.execute("SELECT * FROM trades WHERE " + spot_unverified_sql()):
        if str(row["cost_quality"] or "") == "simulated":
            continue
        moment = _parse_ts(row["timestamp"]) if "timestamp" in row.keys() else None
        if moment is not None and min_age_sec and (now - moment) < float(min_age_sec):
            continue
        rows.append(row)
    return rows


def reconcile_closed_spot_trades(db_path, exchange, pause_sec=None, qty_tolerance_pct=None,
                                 min_age_sec=120, page_limit=500, sleeper=None):
    """Match unverified closed Spot trades to fills. Safe to run repeatedly.

    A transport or auth failure aborts the run without marking the remaining
    trades as having no fills, so a later run can retry.
    """
    client = exchange if isinstance(exchange, ReadOnlySpotExchange) else ReadOnlySpotExchange(exchange)
    pause_sec = pause_seconds() if pause_sec is None else max(0.0, float(pause_sec))
    tolerance = qty_tolerance() if qty_tolerance_pct is None else max(0.0, float(qty_tolerance_pct))
    sleeper = sleeper or time.sleep
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    matched_this_run = 0
    exchange_down = False
    down_name = None
    conn = _connect(db_path)
    try:
        if not ensure_schema(conn):
            r_count = 0
            snapshot = {"matched": 0, "unmatched": 0, "reasons": {}}
        else:
            r_count = backfill_r_multiples(conn)
            conn.commit()
            try:
                matched_this_run = _reconcile_candidates(
                    conn, client, pause_sec, tolerance, min_age_sec, page_limit, sleeper, now
                )
            except ExchangeUnavailable as exc:
                exchange_down = True
                down_name = str(exc) or "ExchangeUnavailable"
                logger.warning("spot reconcile paused: %s", down_name)
            r_count += backfill_r_multiples(conn)
            snapshot = _live_counts(conn)
            _save_state(conn, now, snapshot, exchange_down, r_count)
            conn.commit()
    finally:
        conn.close()
    warning = warning_ar(
        snapshot["unmatched"], snapshot["matched"], snapshot["unmatched"],
        snapshot["reasons"], exchange_down,
    )
    if exchange_down:
        summary = "تعذر الاتصال بـ Binance أثناء مطابقة السبوت؛ ستُعاد المحاولة لاحقًا ({} طُوبقت في هذه الجولة).".format(
            matched_this_run)
    else:
        summary = "مطابقة سبوت: {} طُوبقت، {} لم تُطابق.".format(matched_this_run, snapshot["unmatched"])
    logger.info("spot reconcile matched_this_run=%s unmatched=%s", matched_this_run, snapshot["unmatched"])
    return {
        "ok": not exchange_down,
        "exchange_unavailable": exchange_down,
        "matched": snapshot["matched"],
        "matched_this_run": matched_this_run,
        "unmatched": snapshot["unmatched"],
        "reasons": snapshot["reasons"],
        "r_backfilled": r_count,
        "warning_ar": warning,
        "summary_ar": summary,
    }


def _reconcile_candidates(conn, client, pause, tolerance, min_age_sec, page_limit, sleeper, now):
    rows = _candidate_rows(conn, min_age_sec)
    by_symbol = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    decisions = _load_decisions(conn, list(by_symbol))
    decisions_by_symbol = {}
    for decision in decisions:
        decisions_by_symbol.setdefault(decision["symbol"], []).append(decision)
    matched = 0
    price_cache = {}
    for symbol, symbol_rows in by_symbol.items():
        assigned = assign_decision_orders(symbol_rows, decisions_by_symbol.get(symbol, []))
        prepared = []
        for row in symbol_rows:
            entry_ids = split_order_ids(row["entry_order_ids"]) or assigned.get(int(row["id"]), {}).get("entry", [])
            exit_ids = split_order_ids(row["exit_order_ids"]) or assigned.get(int(row["id"]), {}).get("exit", [])
            prepared.append((row, entry_ids, exit_ids))
        needs_network = [item for item in prepared if item[1] and item[2]]
        cache = {}
        if needs_network:
            moments = [_parse_ts(item[0]["timestamp"]) for item in needs_network]
            moments = [m for m in moments if m is not None]
            since_ms = int((min(moments) - 3 * 86400) * 1000) if moments else None
            until_ms = int((max(moments) + 3600) * 1000) if moments else None
            trades = fetch_symbol_trades(client, symbol, since_ms, until_ms, pause, sleeper, page_limit=page_limit)
            cache = _index_by_order(trades)
            sleeper(pause)
        for row, entry_ids, exit_ids in prepared:
            outcome = _reconcile_one(
                conn, client, row, entry_ids, exit_ids, cache, price_cache, pause, sleeper, tolerance, now
            )
            if outcome == "matched":
                matched += 1
    return matched


def _quote_price_fn(client, cache, pause, sleeper):
    def lookup(currency, timestamp_ms, quote):
        return _quote_price(client, currency, timestamp_ms, quote, cache, pause, sleeper)
    return lookup


def _reconcile_one(conn, client, row, entry_ids, exit_ids, cache, price_cache, pause, sleeper, tolerance, now):
    trade_id = int(row["id"])
    snapshot = _snapshot(row)
    _capture_original(conn, trade_id, snapshot, now)
    if not entry_ids or not exit_ids:
        _mark_unmatched(conn, row, snapshot, "order_id_missing", entry_ids, exit_ids, now)
        return "unmatched"
    try:
        entry_fills = _fills_for_orders(client, row["symbol"], entry_ids, cache, pause, sleeper)
        exit_fills = _fills_for_orders(client, row["symbol"], exit_ids, cache, pause, sleeper)
    except ExchangeUnavailable:
        conn.commit()
        raise
    lookup = _quote_price_fn(client, price_cache, pause, sleeper)
    entry, entry_reason = summarize_leg(entry_fills, row["symbol"], "buy", lookup)
    if entry is None:
        _mark_unmatched(conn, row, snapshot, entry_reason or "no_fills", entry_ids, exit_ids, now)
        return "unmatched"
    exit_leg, exit_reason = summarize_leg(exit_fills, row["symbol"], "sell", lookup)
    if exit_leg is None:
        _mark_unmatched(conn, row, snapshot, exit_reason or "no_fills", entry_ids, exit_ids, now)
        return "unmatched"
    if not quantities_match(entry["net_qty"], exit_leg["net_qty"], tolerance):
        _mark_unmatched(conn, row, snapshot, "quantity_mismatch", entry_ids, exit_ids, now)
        return "unmatched"
    reconciled = build_reconciliation(entry, exit_leg)
    if reconciled is None:
        _mark_unmatched(conn, row, snapshot, "incomplete_fill", entry_ids, exit_ids, now)
        return "unmatched"
    risk = row["risk_usdt"] if "risk_usdt" in row.keys() else None
    try:
        risk_value = float(risk) if risk is not None else None
    except (TypeError, ValueError):
        risk_value = None
    r_value = r_multiple(reconciled["pnl_usdt"], risk_value) if risk_value and risk_value > 0 else None
    conn.execute(
        """UPDATE trades SET entry_price=?, exit_price=?, pnl_pct=?, pnl_usdt=?, status=?,
           cost_quality=?, gross_pnl_usdt=?, fees_usdt=?, r_multiple=COALESCE(?, r_multiple),
           entry_order_ids=?, exit_order_ids=?, reconcile_status='matched', reconcile_reason=NULL,
           reconciled_at=?
           WHERE id=?""",
        (reconciled["entry_price"], reconciled["exit_price"], reconciled["pnl_pct"], reconciled["pnl_usdt"],
         reconciled["status"], reconciled["cost_quality"], reconciled["gross_pnl_usdt"], reconciled["fees_usdt"],
         r_value, ",".join(entry_ids), ",".join(exit_ids), now, trade_id),
    )
    _write_audit(conn, trade_id, "matched", None, snapshot, reconciled, now)
    conn.commit()
    return "matched"


def _mark_unmatched(conn, row, snapshot, reason, entry_ids, exit_ids, now):
    if reason not in REASON_AR:
        reason = "incomplete_fill"
    conn.execute(
        """UPDATE trades SET reconcile_status='unmatched', reconcile_reason=?, reconciled_at=?,
           entry_order_ids=COALESCE(?, entry_order_ids), exit_order_ids=COALESCE(?, exit_order_ids)
           WHERE id=?""",
        (reason, now, ",".join(entry_ids) if entry_ids else None, ",".join(exit_ids) if exit_ids else None, int(row["id"])),
    )
    _write_audit(conn, int(row["id"]), "unmatched", reason, snapshot, None, now)
    conn.commit()


def _live_counts(conn):
    if not _columns(conn, "trades"):
        return {"matched": 0, "unmatched": 0, "reasons": {}}
    matched = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE reconcile_status='matched'"
    ).fetchone()[0]
    unmatched = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE " + spot_unverified_sql()
    ).fetchone()[0]
    reasons = {}
    if "reconcile_reason" in _columns(conn, "trades"):
        for row in conn.execute(
            "SELECT reconcile_reason, COUNT(*) AS n FROM trades WHERE " + spot_unverified_sql()
            + " GROUP BY reconcile_reason"
        ):
            if row["reconcile_reason"]:
                reasons[row["reconcile_reason"]] = int(row["n"])
    return {"matched": int(matched or 0), "unmatched": int(unmatched or 0), "reasons": reasons}


def _save_state(conn, now, snapshot, exchange_down, r_count):
    conn.execute(
        """INSERT INTO spot_reconcile_state
           (id, updated_at, last_error, matched, unmatched, reasons_json, exchange_unavailable, r_backfilled)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             updated_at=excluded.updated_at, last_error=excluded.last_error, matched=excluded.matched,
             unmatched=excluded.unmatched, reasons_json=excluded.reasons_json,
             exchange_unavailable=excluded.exchange_unavailable, r_backfilled=excluded.r_backfilled""",
        (now, "exchange_unavailable" if exchange_down else None, snapshot["matched"], snapshot["unmatched"],
         json.dumps(snapshot["reasons"], ensure_ascii=False, sort_keys=True), 1 if exchange_down else 0, int(r_count)),
    )


def status_snapshot(db_path, unverified_count=None):
    """Dashboard payload. Read-only; does not call the exchange."""
    empty = {
        "enabled": enabled(),
        "interval_min": interval_minutes(),
        "last_run_at": None,
        "last_error": None,
        "matched": 0,
        "unmatched": int(unverified_count or 0),
        "reasons": {},
        "exchange_unavailable": False,
        "r_backfilled": 0,
        "warning_ar": warning_ar(int(unverified_count or 0), 0, int(unverified_count or 0), {}, False),
    }
    try:
        conn = _connect(db_path)
    except Exception:
        return empty
    try:
        if not ensure_schema(conn):
            conn.commit()
            return empty
        conn.commit()
        live = _live_counts(conn)
        state = conn.execute("SELECT * FROM spot_reconcile_state WHERE id=1").fetchone()
    finally:
        conn.close()
    unmatched = live["unmatched"] if unverified_count is None else int(unverified_count)
    # Prefer the live unverified count passed by /api/state so the banner matches stats.
    if unverified_count is not None:
        unmatched = int(unverified_count)
    else:
        unmatched = live["unmatched"]
    exchange_down = bool(state["exchange_unavailable"]) if state else False
    warning = warning_ar(unmatched, live["matched"], unmatched, live["reasons"], exchange_down)
    return {
        "enabled": enabled(),
        "interval_min": interval_minutes(),
        "last_run_at": state["updated_at"] if state else None,
        "last_error": state["last_error"] if state else None,
        "matched": live["matched"],
        "unmatched": unmatched,
        "reasons": live["reasons"],
        "exchange_unavailable": exchange_down,
        "r_backfilled": int(state["r_backfilled"] or 0) if state else 0,
        "warning_ar": warning,
    }
