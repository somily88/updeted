"""Binance USDⓈ-M Futures Demo connector for commodities, equities, and Spot-linked crypto hedges.

Execution is isolated to Binance Demo mode. The engine uses 1x isolated margin,
long and short entries, and keeps a direction-aware ATR stop/target monitor. It must never be
constructed with live credentials or a live endpoint.
"""
import contextlib
import datetime
import os
import sqlite3
import time

import ccxt
from strategy_engine import (evaluate_symbol, STRATEGY_RISK, RISK_PER_TRADE_PCT,
                             TRAILING_ACTIVATION_PCT, risk_sized_notional,
                             market_liquidity_check, retry_api_read, fetch_fresh_ticker,
                             MAX_DAILY_LOSS_PCT, MAX_SPOT_HEDGE_RATIO)
from risk_controls import daily_loss_status
from database import get_db_connection

HEDGE_ONLY_IDS = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "TRXUSDT", "NEARUSDT", "SPCXBUSDT",
}
TARGET_IDS = ["CLUSDT","BZUSDT","XAUUSDT","MUUSDT","NVDAUSDT","SPCXUSDT","AAPLUSDT","TSLAUSDT","SNDKUSDT","METAUSDT","AMDUSDT","MSFTUSDT","BEUSDT","AMZNUSDT"] + sorted(HEDGE_ONLY_IDS)
TAKE_PROFIT_USDT = float(os.getenv("TAKE_PROFIT_USDT", "2"))


class BinanceFuturesDemo:
    def __init__(self, api_key, secret_key, db_path, on_event=None, on_decision=None,
                 spot_exposure_provider=None):
        if not api_key or not secret_key:
            raise ValueError("Futures Demo API credentials are required")
        self.db_path = db_path
        self.on_event = on_event or (lambda tag, message: None)
        self.on_decision = on_decision or (lambda *args, **kwargs: None)
        self.spot_exposure_provider = spot_exposure_provider
        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": secret_key,
            "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True},
        })
        if not hasattr(self.exchange, "enable_demo_trading"):
            raise RuntimeError("Installed ccxt does not support Binance Demo Trading")
        self.exchange.enable_demo_trading(True)
        self.exchange.load_markets()
        available_ids = set()
        self.symbols = []
        self.hedge_only_symbols = []
        for market in self.exchange.markets.values():
            if (market.get("id") in TARGET_IDS and market.get("active") is not False
                    and market.get("swap") and market.get("linear")
                    and market.get("quote") == "USDT"):
                self.symbols.append(market["symbol"])
                if market.get("id") in HEDGE_ONLY_IDS:
                    self.hedge_only_symbols.append(market["symbol"])
                available_ids.add(market.get("id"))
        self.symbols.sort()
        self.unavailable_symbols = [symbol_id for symbol_id in TARGET_IDS
                                    if symbol_id not in available_ids]
        self.snapshot = {}
        self.last_entry_candle = {}
        self._balance_cache = {"time": 0.0, "value": None}
        self._init_db()
        self.positions = self._load_positions()
        self.daily_risk_state = None
        self._daily_risk_block_reported = False

    @contextlib.contextmanager
    def _connect(self):
        """Use the shared transaction helper for Futures state and history."""
        with get_db_connection(self.db_path) as conn:
            yield conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS futures_positions (
                symbol TEXT PRIMARY KEY, entry_price REAL NOT NULL, amount REAL NOT NULL,
                contracts REAL NOT NULL, cost REAL NOT NULL, highest_price REAL NOT NULL,
                stop_price REAL NOT NULL, target_price REAL NOT NULL, trailing_distance REAL NOT NULL,
                strategy TEXT NOT NULL, timeframe TEXT NOT NULL, opened_at TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT 'long'
            )""")
            position_columns = {row[1] for row in conn.execute("PRAGMA table_info(futures_positions)")}
            if "side" not in position_columns:
                conn.execute("ALTER TABLE futures_positions ADD COLUMN side TEXT NOT NULL DEFAULT 'long'")
            for column, definition in (("risk_distance", "REAL"),
                                       ("partial_taken", "INTEGER NOT NULL DEFAULT 0"),
                                       ("partial_attempted", "INTEGER NOT NULL DEFAULT 0"),
                                       ("partial_pnl_usdt", "REAL NOT NULL DEFAULT 0"),
                                       ("initial_cost", "REAL"),
                                       ("remaining_entry_fee_usdt", "REAL NOT NULL DEFAULT 0"),
                                       ("fees_usdt", "REAL NOT NULL DEFAULT 0"),
                                       ("fees_estimated", "INTEGER NOT NULL DEFAULT 1"),
                                       ("funding_usdt", "REAL NOT NULL DEFAULT 0"),
                                       ("funding_cursor_ms", "INTEGER NOT NULL DEFAULT 0"),
                                       ("funding_available", "INTEGER NOT NULL DEFAULT 0"),
                                       ("exit_attempted", "INTEGER NOT NULL DEFAULT 0"),
                                       ("opened_at_ms", "INTEGER NOT NULL DEFAULT 0"),
                                       ("hedge_base", "TEXT"),
                                       ("hedge_ratio", "REAL"),
                                       ("hedge_exposure_usdt", "REAL")):
                if column not in position_columns:
                    conn.execute("ALTER TABLE futures_positions ADD COLUMN {} {}".format(column, definition))
            conn.execute("""CREATE TABLE IF NOT EXISTS futures_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                entry_price REAL NOT NULL, exit_price REAL NOT NULL, pnl_pct REAL NOT NULL,
                pnl_usdt REAL NOT NULL, status TEXT NOT NULL, timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL, timeframe TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT 'long'
            )""")
            trade_columns = {row[1] for row in conn.execute("PRAGMA table_info(futures_trades)")}
            for column, definition in (("fees_usdt", "REAL NOT NULL DEFAULT 0"),
                                       ("funding_usdt", "REAL NOT NULL DEFAULT 0"),
                                       ("gross_pnl_usdt", "REAL NOT NULL DEFAULT 0"),
                                       ("cost_quality", "TEXT NOT NULL DEFAULT 'legacy_gross_before_fees'")):
                if column not in trade_columns:
                    conn.execute("ALTER TABLE futures_trades ADD COLUMN {} {}".format(column, definition))
            conn.execute("""CREATE TABLE IF NOT EXISTS futures_funding (
                tran_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, income_usdt REAL NOT NULL,
                timestamp_ms INTEGER NOT NULL, income_type TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_futures_funding_symbol_time ON futures_funding(symbol, timestamp_ms)")
            conn.execute("""CREATE TABLE IF NOT EXISTS futures_test_runs (
                test_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL,
                symbol TEXT, side TEXT, order_id TEXT, error TEXT
            )""")

    def _estimated_taker_fee(self, symbol, notional):
        try:
            rate = self.exchange.market(symbol).get("taker")
            rate = float(rate) if rate is not None else 0.0005
        except Exception:
            rate = 0.0005
        return abs(float(notional)) * max(rate, 0.0)

    def _order_fee_usdt(self, order, symbol, filled, price):
        """Read fill commissions when available; otherwise return an explicit estimate."""
        market = self.exchange.market(symbol)
        multiplier = float(market.get("contractSize") or 1.0)
        fee_items = []
        if order:
            fee_items = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        if not fee_items and order and order.get("id"):
            fetch_trades = getattr(self.exchange, "fetch_order_trades", None)
            if callable(fetch_trades):
                try:
                    trades = retry_api_read(fetch_trades, order["id"], symbol)
                    fee_items = [fee for trade in trades or []
                                 for fee in (trade.get("fees") or ([trade["fee"]] if trade.get("fee") else []))]
                except Exception:
                    fee_items = []
        actual = 0.0
        found = False
        unknown_currency = False
        for fee in fee_items:
            try:
                amount = abs(float(fee.get("cost") or 0.0))
                currency = str(fee.get("currency") or "").upper()
                if amount <= 0:
                    continue
                if currency in ("USDT", "USDC"):
                    actual += amount
                elif currency == str(market.get("base") or "").upper():
                    actual += amount * float(price)
                else:
                    unknown_currency = True
                    continue
                found = True
            except (AttributeError, TypeError, ValueError):
                continue
        if found and not unknown_currency:
            return actual, False
        notional = abs(float(filled) * multiplier * float(price))
        return self._estimated_taker_fee(symbol, notional), True

    def _position_opened_ms(self, pos):
        if pos.get("opened_at_ms"):
            return int(pos["opened_at_ms"])
        try:
            opened = datetime.datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S")
            return int(opened.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        except (TypeError, ValueError):
            return int(time.time() * 1000) - 120000

    def _sync_funding(self, symbol, pos):
        """Persist Binance funding income idempotently; failures never block exits."""
        method = getattr(self.exchange, "fapiPrivateGetIncome", None)
        if not callable(method):
            pos["funding_available"] = False
            if not pos.get("funding_warning_sent"):
                self.on_event("WARN", "إصدار CCXT الحالي لا يتيح سجل تمويل Binance؛ صافي Futures سيُعلّم غير مكتمل")
                pos["funding_warning_sent"] = True
            return False
        now_ms = int(time.time() * 1000)
        opened_ms = self._position_opened_ms(pos)
        cursor_ms = int(pos.get("funding_cursor_ms") or opened_ms)
        try:
            market_id = self.exchange.market(symbol).get("id")
            rows = method({
                "symbol": market_id,
                "incomeType": "FUNDING_FEE",
                "startTime": max(opened_ms, cursor_ms - 120000),
                "endTime": now_ms,
                "limit": 1000,
            }) or []
            with self._connect() as conn:
                for item in rows:
                    timestamp_ms = int(item.get("time") or 0)
                    income = float(item.get("income") or 0.0)
                    if timestamp_ms < opened_ms or not income:
                        continue
                    tran_id = str(item.get("tranId") or "{}:{}:{}:{}".format(
                        market_id, timestamp_ms, item.get("tradeId", ""), income))
                    conn.execute("""INSERT OR IGNORE INTO futures_funding
                        (tran_id, symbol, income_usdt, timestamp_ms, income_type)
                        VALUES (?, ?, ?, ?, ?)""", (tran_id, symbol, income, timestamp_ms,
                                                     item.get("incomeType") or "FUNDING_FEE"))
                row = conn.execute("""SELECT COALESCE(SUM(income_usdt), 0) FROM futures_funding
                    WHERE symbol=? AND timestamp_ms>=?""", (symbol, opened_ms)).fetchone()
            pos["funding_usdt"] = float(row[0] or 0.0)
            pos["funding_cursor_ms"] = max(cursor_ms, now_ms - 120000)
            pos["funding_available"] = True
            return True
        except Exception as exc:
            pos["funding_available"] = False
            if not pos.get("funding_warning_sent"):
                self.on_event("WARN", "تعذر قراءة تمويل Futures لـ {}؛ صافي الربح سيُعلّم غير مكتمل ({})".format(
                    symbol, type(exc).__name__))
                pos["funding_warning_sent"] = True
            return False

    def _load_positions(self):
        try:
            with self._connect() as conn:
                rows = conn.execute("""SELECT symbol, entry_price, amount, contracts, cost,
                    highest_price, stop_price, target_price, trailing_distance, strategy,
                    timeframe, opened_at, side, risk_distance, partial_taken,
                    partial_attempted, partial_pnl_usdt, initial_cost,
                    remaining_entry_fee_usdt, fees_usdt, fees_estimated, funding_usdt,
                    funding_cursor_ms, funding_available, exit_attempted, opened_at_ms,
                    hedge_base, hedge_ratio, hedge_exposure_usdt
                    FROM futures_positions""").fetchall()
            positions = {}
            for row in rows:
                pos = {
                    "symbol": row[0], "entry_price": row[1], "amount": row[2],
                    "contracts": row[3], "cost": row[4], "highest_price": row[5],
                    "stop_price": row[6], "target_price": row[7],
                    "trailing_distance": row[8], "strategy": row[9],
                    "timeframe": row[10], "opened_at": row[11], "side": row[12] or "long",
                    "risk_distance": float(row[13] or 0.0), "partial_taken": bool(row[14]),
                    "partial_attempted": bool(row[15]), "partial_pnl_usdt": float(row[16] or 0.0),
                    "initial_cost": float(row[17] or row[4] or 0.0),
                    "remaining_entry_fee_usdt": float(row[18] or 0.0),
                    "fees_usdt": float(row[19] or 0.0), "fees_estimated": bool(row[20]),
                    "funding_usdt": float(row[21] or 0.0),
                    "funding_cursor_ms": int(row[22] or 0),
                    "funding_available": bool(row[23]), "exit_attempted": bool(row[24]),
                    "opened_at_ms": int(row[25] or 0),
                    "hedge_base": row[26], "hedge_ratio": float(row[27] or 0.0),
                    "hedge_exposure_usdt": float(row[28] or 0.0),
                }
                if pos["fees_usdt"] <= 0 and pos["cost"] > 0:
                    pos["fees_estimated"] = True
                    if pos["partial_taken"]:
                        initial_cost = max(float(pos["initial_cost"] or 0.0), float(pos["cost"]))
                        sold_fraction = min(1.0, max(0.0,
                            (initial_cost - float(pos["cost"])) / initial_cost)) if initial_cost else 0.0
                        original_entry_fee = self._estimated_taker_fee(row[0], initial_cost)
                        remaining_entry_fee = original_entry_fee * (1.0 - sold_fraction)
                        partial_exit_fee = self._estimated_taker_fee(
                            row[0], initial_cost * sold_fraction)
                        pos["partial_pnl_usdt"] -= ((original_entry_fee - remaining_entry_fee)
                                                     + partial_exit_fee)
                        pos["remaining_entry_fee_usdt"] = remaining_entry_fee
                        pos["fees_usdt"] = original_entry_fee + partial_exit_fee
                    else:
                        estimated_fee = self._estimated_taker_fee(row[0], pos["cost"])
                        pos["remaining_entry_fee_usdt"] = estimated_fee
                        pos["fees_usdt"] = estimated_fee
                if not pos["opened_at_ms"]:
                    pos["opened_at_ms"] = self._position_opened_ms(pos)
                if not pos["funding_cursor_ms"]:
                    try:
                        opened = datetime.datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S")
                        pos["funding_cursor_ms"] = int(opened.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
                    except (TypeError, ValueError):
                        pos["funding_cursor_ms"] = int(time.time() * 1000) - 120000
                positions[row[0]] = pos
            return positions
        except sqlite3.OperationalError:
            return {}

    def balance(self, ttl=10):
        now = time.time()
        cached = self._balance_cache
        if cached["value"] is not None and now - cached["time"] < ttl:
            return dict(cached["value"])
        try:
            data = retry_api_read(self.exchange.fetch_balance)
            value = {
                "free": float((data.get("free") or {}).get("USDT") or 0),
                "total": float((data.get("total") or {}).get("USDT") or 0),
            }
            self._balance_cache = {"time": now, "value": value}
            return dict(value)
        except Exception as exc:
            return {"free": 0.0, "total": 0.0, "error": type(exc).__name__}

    def public_state(self):
        stats = {"closed": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0}
        try:
            with self._connect() as conn:
                row = conn.execute("""SELECT
                    SUM(CASE WHEN cost_quality IN ('net_actual_costs','net_estimated_costs') THEN 1 ELSE 0 END),
                    SUM(CASE WHEN cost_quality IN ('net_actual_costs','net_estimated_costs') AND pnl_usdt > 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN cost_quality IN ('net_actual_costs','net_estimated_costs') AND pnl_usdt < 0 THEN 1 ELSE 0 END),
                    COALESCE(SUM(CASE WHEN cost_quality IN ('net_actual_costs','net_estimated_costs') THEN pnl_usdt ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN cost_quality='legacy_gross_before_fees' THEN pnl_usdt ELSE 0 END), 0),
                    SUM(CASE WHEN cost_quality='legacy_gross_before_fees' THEN 1 ELSE 0 END)
                    FROM futures_trades""").fetchone()
            stats = {"closed": row[0] or 0, "wins": row[1] or 0,
                     "losses": row[2] or 0, "realized_pnl": row[3] or 0.0,
                     "legacy_gross_pnl_unadjusted": row[4] or 0.0,
                     "unverified_closed": row[5] or 0}
        except sqlite3.OperationalError:
            pass
        balance = self.balance()
        return {
            "connected": True, "environment": "demo", "trading_enabled": True,
            "symbols": self.symbols, "unavailable_symbols": self.unavailable_symbols,
            "open_positions": len(self.positions),
            "balance": balance["total"], "free_balance": balance["free"],
            "realized_pnl": stats["realized_pnl"] + sum(
                float(p.get("partial_pnl_usdt") or 0.0) + float(p.get("funding_usdt") or 0.0)
                for p in self.positions.values()),
            **{key: value for key, value in stats.items() if key != "realized_pnl"},
            "daily_risk": self.daily_risk_state,
        }

    def _save_position(self, pos):
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO futures_positions
                (symbol, entry_price, amount, contracts, cost, highest_price, stop_price,
                 target_price, trailing_distance, strategy, timeframe, opened_at, side,
                 risk_distance, partial_taken, partial_attempted, partial_pnl_usdt, initial_cost,
                 remaining_entry_fee_usdt, fees_usdt, fees_estimated, funding_usdt,
                 funding_cursor_ms, funding_available, exit_attempted, opened_at_ms,
                 hedge_base, hedge_ratio, hedge_exposure_usdt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pos["symbol"], pos["entry_price"], pos["amount"], pos["contracts"],
                 pos["cost"], pos["highest_price"], pos["stop_price"], pos["target_price"],
                 pos["trailing_distance"], pos["strategy"], pos["timeframe"], pos["opened_at"],
                 pos.get("side", "long"), pos.get("risk_distance"),
                 1 if pos.get("partial_taken") else 0,
                 1 if pos.get("partial_attempted") else 0, float(pos.get("partial_pnl_usdt") or 0.0),
                 float(pos.get("initial_cost") or pos.get("cost") or 0.0),
                 float(pos.get("remaining_entry_fee_usdt") or 0.0),
                 float(pos.get("fees_usdt") or 0.0), 1 if pos.get("fees_estimated", True) else 0,
                 float(pos.get("funding_usdt") or 0.0), int(pos.get("funding_cursor_ms") or 0),
                 1 if pos.get("funding_available") else 0,
                 1 if pos.get("exit_attempted") else 0, int(pos.get("opened_at_ms") or 0),
                 pos.get("hedge_base"), pos.get("hedge_ratio"), pos.get("hedge_exposure_usdt")))

    def _close(self, symbol, pos, price, reason, contracts_to_close=None, partial=False):
        self._sync_funding(symbol, pos)
        side = pos.get("side", "long")
        close_side = "buy" if side == "short" else "sell"
        previous_contracts = float(pos["contracts"])
        requested = min(previous_contracts, float(contracts_to_close or previous_contracts))
        requested = float(self.exchange.amount_to_precision(symbol, requested))
        if requested <= 0:
            raise ValueError("كمية الإغلاق أقل من دقة العقد")
        # Persist the intent before sending. If the response is ambiguous, the
        # monitor freezes this position rather than submitting a duplicate close.
        pos["exit_attempted"] = True
        self._save_position(pos)
        # Never retry create_order; only read the status of the submitted order.
        order = self.exchange.create_order(
            symbol, "market", close_side, requested, None, {"reduceOnly": True})
        if order.get("status") != "closed" and order.get("id"):
            order = retry_api_read(self.exchange.fetch_order, order["id"], symbol)
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            self.on_decision('Futures Demo', symbol, {'signal': False, 'strategy': pos.get('strategy'),
                             'timeframe': pos.get('timeframe')}, price, 'تعذر تأكيد أمر الإغلاق',
                             reason, order_status=order.get('status') or 'غير مؤكد',
                             order_id=order.get('id'))
            raise RuntimeError("أمر الإغلاق لم يُؤكد؛ لن يُعاد إرساله تلقائيًا")
        exit_price = float(order.get("average") or price)
        multiplier = float(self.exchange.market(symbol).get("contractSize") or 1)
        self.on_decision('Futures Demo', symbol, {'signal': False, 'strategy': pos.get('strategy'),
                         'timeframe': pos.get('timeframe')}, price, 'تم إرسال أمر إغلاق', reason,
                         order_status=order.get('status'), order_id=order.get('id'))
        closed_contracts = min(filled, previous_contracts)
        direction = -1.0 if side == "short" else 1.0
        gross_tranche_pnl = direction * (exit_price - pos["entry_price"]) * closed_contracts * multiplier
        close_fee, close_fee_estimated = self._order_fee_usdt(order, symbol, closed_contracts, exit_price)
        remaining_entry_fee = float(pos.get("remaining_entry_fee_usdt") or 0.0)
        entry_fee_allocated = remaining_entry_fee * closed_contracts / previous_contracts
        pos["remaining_entry_fee_usdt"] = max(remaining_entry_fee - entry_fee_allocated, 0.0)
        pos["fees_usdt"] = float(pos.get("fees_usdt") or 0.0) + close_fee
        pos["fees_estimated"] = bool(pos.get("fees_estimated")) or close_fee_estimated
        net_tranche_pnl = gross_tranche_pnl - close_fee - entry_fee_allocated
        closed_cost = float(pos["cost"]) * closed_contracts / previous_contracts
        remaining = max(previous_contracts - closed_contracts, 0.0)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if remaining > 0:
            pos["exit_attempted"] = False
            pos["contracts"] = remaining
            pos["amount"] = remaining * multiplier
            pos["cost"] = max(float(pos["cost"]) - closed_cost, 0.0)
        if partial:
            pos["partial_taken"] = True
            pos["partial_pnl_usdt"] = float(pos.get("partial_pnl_usdt") or 0.0) + net_tranche_pnl
            pos["stop_price"] = (min(float(pos["stop_price"]), float(pos["entry_price"]))
                                 if side == "short"
                                 else max(float(pos["stop_price"]), float(pos["entry_price"])))
            try:
                self._save_position(pos)
            except Exception:
                pos["exit_attempted"] = True
                raise
            self.on_event("FUTURES", "جني جزئي {} {} · {:+.2f} USDT عند 1R · الوقف للتعادل".format(
                symbol, "Short" if side == "short" else "Long", net_tranche_pnl))
            return
        if remaining <= 0:
            funding_usdt = float(pos.get("funding_usdt") or 0.0)
            total_pnl = float(pos.get("partial_pnl_usdt") or 0.0) + net_tranche_pnl + funding_usdt
            basis = float(pos.get("initial_cost") or (pos["cost"] + closed_cost) or 0.0)
            pnl_pct = total_pnl / basis * 100.0 if basis else 0.0
            status = "WIN" if total_pnl > 0 else "LOSS"
            fees_usdt = float(pos.get("fees_usdt") or 0.0)
            gross_total = total_pnl - funding_usdt + fees_usdt
            cost_quality = ("net_estimated_costs" if pos.get("fees_estimated")
                            or not pos.get("funding_available") else "net_actual_costs")
            with self._connect() as conn:
                conn.execute("DELETE FROM futures_positions WHERE symbol = ?", (symbol,))
                conn.execute("""INSERT INTO futures_trades
                    (symbol, entry_price, exit_price, pnl_pct, pnl_usdt, status, timestamp,
                     strategy, timeframe, side, fees_usdt, funding_usdt, gross_pnl_usdt, cost_quality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (symbol, pos["entry_price"], exit_price, pnl_pct, total_pnl, status, now,
                     pos["strategy"], pos["timeframe"], side, fees_usdt, funding_usdt,
                     gross_total, cost_quality))
            self.positions.pop(symbol, None)
            self.on_event("FUTURES", "إغلاق {} {} {} (صافي {:+.2f} USDT · رسوم {:.4f} · تمويل {:+.4f})".format(
                symbol, "Short" if side == "short" else "Long", reason, total_pnl,
                fees_usdt, funding_usdt))
        else:
            pos["partial_pnl_usdt"] = float(pos.get("partial_pnl_usdt") or 0.0) + net_tranche_pnl
            try:
                with self._connect() as conn:
                    conn.execute("""UPDATE futures_positions SET amount=?, contracts=?, cost=?,
                        highest_price=?, partial_pnl_usdt=?, remaining_entry_fee_usdt=?, fees_usdt=?,
                        fees_estimated=?, funding_usdt=?, funding_cursor_ms=?, funding_available=?,
                        exit_attempted=0 WHERE symbol=?""",
                        (pos["amount"], pos["contracts"], pos["cost"], pos["highest_price"],
                         pos["partial_pnl_usdt"], pos["remaining_entry_fee_usdt"], pos["fees_usdt"],
                         1 if pos["fees_estimated"] else 0, pos.get("funding_usdt", 0.0),
                         pos.get("funding_cursor_ms", 0), 1 if pos.get("funding_available") else 0, symbol))
            except Exception:
                pos["exit_attempted"] = True
                raise
            self.on_event("WARN", "إغلاق {} نُفذ جزئيًا؛ سيُتابع المتبقي دون تكرار الأمر".format(symbol))

    def _monitor_positions(self):
        monitor_ok = True
        for symbol, pos in list(self.positions.items()):
            order = None
            try:
                side = pos.get("side", "long")
                if pos.get("partial_attempted") and not pos.get("partial_taken"):
                    monitor_ok = False
                    continue  # ambiguous tranche order: require reconciliation before another order
                if pos.get("exit_attempted"):
                    monitor_ok = False
                    self.on_decision('Futures Demo', symbol, {'signal': False, 'strategy': pos.get('strategy'),
                                     'timeframe': pos.get('timeframe')}, outcome='تجميد المركز',
                                     reason='أمر إغلاق سابق غير مؤكد؛ طابق سجل Binance قبل أي أمر آخر',
                                     order_status='غير مؤكد')
                    continue
                price = float(fetch_fresh_ticker(self.exchange, symbol)["last"])
                self._sync_funding(symbol, pos)
                self.snapshot[symbol] = {"symbol": symbol, "price": price, "ready": True,
                                         "in_position": True, "direction": side,
                                         "updated_at": time.time()}
                if pos.get("hedge_base"):
                    if not self.spot_exposure_provider:
                        spot_exposure = None
                    else:
                        try:
                            spot_exposure = self.spot_exposure_provider()
                        except Exception:
                            spot_exposure = None
                    if spot_exposure is None:
                        monitor_ok = False
                        self.on_event("WARN", "تعذر التحقق من تعرض Spot لتحوط {}؛ تستمر مراقبة الوقف دون إعادة موازنة".format(symbol))
                    else:
                        remaining_spot = float(spot_exposure.get(pos["hedge_base"]) or 0.0)
                        if remaining_spot <= 0:
                            self._close(symbol, pos, price, "زوال تعرض Spot والتحوط المرتبط")
                            continue
                        multiplier = float(self.exchange.market(symbol).get("contractSize") or 1)
                        desired = remaining_spot * float(pos.get("hedge_ratio") or MAX_SPOT_HEDGE_RATIO)
                        desired_contracts = float(self.exchange.amount_to_precision(
                            symbol, desired / max(price * multiplier, 1e-12)))
                        excess = max(0.0, float(pos["contracts"]) - desired_contracts)
                        market_min = float((((self.exchange.market(symbol).get("limits") or {}).get("cost") or {}).get("min")) or 5.0)
                        if excess * multiplier * price >= market_min:
                            self._close(symbol, pos, price, "إعادة موازنة تحوط Spot", excess, partial=True)
                            continue
                if pos.get("strategy") == "forced_test":
                    opened = datetime.datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S")
                    if (datetime.datetime.now() - opened).total_seconds() >= 120:
                        self._close(symbol, pos, price, "انتهاء اختبار Futures")
                        continue
                if side == "short":
                    pos["highest_price"] = min(pos["highest_price"], price)
                    trail_armed = (bool(pos.get("partial_taken"))
                                   and pos["highest_price"] <= pos["entry_price"] * (1 - TRAILING_ACTIVATION_PCT / 100))
                    trailing_stop = pos["highest_price"] + pos["trailing_distance"]
                    active_stop = min(pos["stop_price"], trailing_stop) if trail_armed else pos["stop_price"]
                    hit_target = not pos.get("partial_taken") and price <= pos["target_price"]
                    hit_stop = price >= active_stop
                else:
                    pos["highest_price"] = max(pos["highest_price"], price)
                    trail_armed = (bool(pos.get("partial_taken"))
                                   and pos["highest_price"] >= pos["entry_price"] * (1 + TRAILING_ACTIVATION_PCT / 100))
                    trailing_stop = pos["highest_price"] - pos["trailing_distance"]
                    active_stop = max(pos["stop_price"], trailing_stop) if trail_armed else pos["stop_price"]
                    hit_target = not pos.get("partial_taken") and price >= pos["target_price"]
                    hit_stop = price <= active_stop
                self._save_position(pos)
                if hit_target:
                    self._close(symbol, pos, price, "هدف الربح")
                elif hit_stop:
                    self._close(symbol, pos, price, "وقف الخسارة")
                elif (pos.get("strategy") != "forced_test" and not pos.get("partial_taken")
                      and not pos.get("partial_attempted")
                      and float(pos.get("risk_distance") or 0.0) > 0):
                    reached_1r = (price <= pos["entry_price"] - pos["risk_distance"] if side == "short"
                                  else price >= pos["entry_price"] + pos["risk_distance"])
                    if reached_1r:
                        requested = float(self.exchange.amount_to_precision(symbol, pos["contracts"] * 0.5))
                        market = self.exchange.market(symbol)
                        min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 5)
                        if requested > 0 and requested * float(market.get("contractSize") or 1) * price >= min_cost:
                            pos["partial_attempted"] = True
                            self._save_position(pos)
                            self._close(symbol, pos, price, "جني 50% عند 1R", requested, partial=True)
                        else:
                            pos["partial_attempted"] = True
                            pos["partial_taken"] = True
                            pos["stop_price"] = min(pos["stop_price"], pos["entry_price"]) if side == "short" else max(pos["stop_price"], pos["entry_price"])
                            self._save_position(pos)
                            self.on_event("WARN", "تخطي البيع الجزئي {}: نصف المركز أقل من الحد الأدنى؛ ستتم متابعة الوقف فقط".format(symbol))
            except Exception as exc:
                monitor_ok = False
                snapshot = self.snapshot.get(symbol) or {}
                self.on_decision('Futures Demo', symbol, {'signal': False, 'strategy': pos.get('strategy'),
                                 'timeframe': pos.get('timeframe')}, snapshot.get('price'),
                                 'خطأ في متابعة مركز Futures', 'تعذر تحديث السعر أو متابعة الوقف',
                                 order_status='غير مؤكد' if order is not None else 'لم يُرسل',
                                 api_message=exc, price_updated_at=snapshot.get('price_updated_at'))
                self.on_event("ERR", "متابعة Futures {} فشلت ({})".format(symbol, type(exc).__name__))
        return monitor_ok

    def _claim_test_run(self, test_id):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""INSERT OR IGNORE INTO futures_test_runs
                (test_id, state, created_at) VALUES (?, 'starting', ?)""", (test_id, now))
            return cursor.rowcount == 1

    def _finish_test_run(self, test_id, state, symbol=None, side=None, order_id=None, error=None):
        with self._connect() as conn:
            conn.execute("""UPDATE futures_test_runs SET state=?, symbol=?, side=?, order_id=?, error=?
                WHERE test_id=?""", (state, symbol, side, order_id, error, test_id))

    def _run_forced_test(self, trade_amount_usdt, can_open, test_id, side):
        symbol = next((s for s in self.symbols if self.exchange.market(s).get("id") == "XAUUSDT"), self.symbols[0])
        setup = {'signal': True, 'direction': side, 'strategy': 'forced_test', 'timeframe': 'demo-test'}
        price, price_updated_at = None, None
        if not test_id:
            self.on_decision('Futures Demo', symbol, setup, outcome='اختبار مرفوض', reason='لم يتم تحديد معرف اختبار فريد', order_status='لم يُرسل')
            self.on_event("WARN", "اختبار Futures مفعّل بلا FORCE_FUTURES_TEST_ID؛ لم يُرسل أي أمر")
            return
        if side not in ("long", "short"):
            self.on_decision('Futures Demo', symbol, setup, outcome='اختبار مرفوض', reason='اتجاه الاختبار غير صالح', order_status='لم يُرسل')
            self.on_event("ERR", "اتجاه اختبار Futures غير صالح؛ استخدم long أو short")
            return
        if not can_open():
            self.on_decision('Futures Demo', symbol, setup, outcome='اختبار مرفوض', reason='بلغ حد المراكز المفتوحة', order_status='لم يُرسل')
            self.on_event("WARN", "تعذر اختبار Futures: بلغ حد المراكز المفتوحة")
            return
        if self.positions:
            self.on_decision('Futures Demo', symbol, setup, outcome='اختبار مرفوض', reason='توجد مراكز Futures مفتوحة بالفعل', order_status='لم يُرسل')
            self.on_event("WARN", "تعذر الاختبار: أغلق مراكز Futures الحالية أولًا")
            return
        if symbol in self.positions:
            self.on_event("WARN", "تخطي اختبار Futures: يوجد مركز مُدار على {}".format(symbol))
            return

        claimed = False
        order_attempted = False
        order_id = None
        try:
            market = self.exchange.market(symbol)
            external_positions = retry_api_read(self.exchange.fetch_positions, [symbol])
            if any(abs(float(p.get("contracts") or 0)) > 0 for p in external_positions):
                self.on_event("WARN", "تخطي اختبار Futures: يوجد مركز غير مُدار على {}".format(symbol))
                return
            account = self.balance(ttl=0)
            if account.get("error"):
                raise RuntimeError("تعذرت قراءة رصيد Futures Demo")
            amount_usdt = float(trade_amount_usdt)
            if amount_usdt <= 0 or account["free"] < amount_usdt:
                raise ValueError("الرصيد المتاح أقل من قيمة صفقة الاختبار")
            price = float(fetch_fresh_ticker(self.exchange, symbol)["last"])
            price_updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            multiplier = float(market.get("contractSize") or 1)
            contracts = float(self.exchange.amount_to_precision(symbol, amount_usdt / (price * multiplier)))
            notional = contracts * multiplier * price
            minimum = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 5)
            if contracts <= 0 or notional < minimum:
                raise ValueError("قيمة الاختبار أقل من الحد الأدنى لهذا العقد")

            if not self._claim_test_run(test_id):
                self.on_event("WARN", "اختبار Futures بهذا المعرّف نُفذ أو بدأ سابقًا؛ لن يتكرر")
                return
            claimed = True
            try:
                self.exchange.set_margin_mode("isolated", symbol)
            except Exception as exc:
                if "no need to change margin type" not in str(exc).lower():
                    raise
            self.exchange.set_leverage(1, symbol)
            order_side = "sell" if side == "short" else "buy"
            self.on_decision('Futures Demo', symbol, setup, price, 'إرسال أمر اختبار ' + side, 'اختبار يدوي مفعّل؛ انتظار رد Binance', order_status='جارٍ الإرسال', price_updated_at=price_updated_at)
            order_attempted = True
            order = self.exchange.create_order(symbol, "market", order_side, contracts)
            order_id = order.get("id")
            if order.get("status") != "closed" and order.get("id"):
                order = retry_api_read(self.exchange.fetch_order, order["id"], symbol)
            filled = float(order.get("filled") or 0)
            if filled <= 0:
                raise RuntimeError("لم يؤكد Binance تنفيذ أمر اختبار Futures")
            entry = float(order.get("average") or price)
            actual_cost = filled * multiplier * entry
            entry_fee, entry_fee_estimated = self._order_fee_usdt(order, symbol, filled, entry)
            stop_pct = 0.005
            stop_price = entry * (1 + stop_pct if side == "short" else 1 - stop_pct)
            target_delta = TAKE_PROFIT_USDT / (filled * multiplier)
            target_price = entry - target_delta if side == "short" else entry + target_delta
            opened_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            opened_at_ms = int(time.time() * 1000)
            pos = {
                "symbol": symbol, "entry_price": entry, "amount": filled * multiplier,
                "contracts": filled, "cost": actual_cost, "highest_price": entry,
                "remaining_entry_fee_usdt": entry_fee, "fees_usdt": entry_fee,
                "fees_estimated": entry_fee_estimated, "funding_usdt": 0.0,
                "funding_cursor_ms": opened_at_ms,
                "funding_available": False, "opened_at_ms": opened_at_ms,
                "stop_price": stop_price, "target_price": target_price,
                "trailing_distance": 0.0, "risk_distance": abs(entry - stop_price),
                "partial_taken": True, "partial_pnl_usdt": 0.0, "initial_cost": actual_cost,
                "strategy": "forced_test",
                "timeframe": "demo-test", "opened_at": opened_at, "side": side,
            }
            self.positions[symbol] = pos
            self._save_position(pos)
            self._finish_test_run(test_id, "opened", symbol, side, order.get("id"))
            self.on_decision('Futures Demo', symbol, setup, price, 'تم فتح صفقة الاختبار', 'تأكد التنفيذ وحُفظ المركز', order.get('status'), order.get('id'), price_updated_at=price_updated_at)
            self.on_event("FUTURES", "اختبار Binance Futures Demo: {} {} بقيمة نحو {:.2f} USDT؛ إغلاق تلقائي خلال دقيقتين".format(
                "Short" if side == "short" else "Long", symbol, actual_cost))
        except Exception as exc:
            if claimed:
                # Recover a possibly filled market order if order confirmation failed.
                try:
                    recovered = next((p for p in retry_api_read(self.exchange.fetch_positions, [symbol])
                                      if abs(float(p.get("contracts") or 0)) > 0), None)
                    if recovered:
                        filled = abs(float(recovered.get("contracts") or 0))
                        entry = float(recovered.get("entryPrice") or fetch_fresh_ticker(self.exchange, symbol)["last"])
                        recovered_side = str(recovered.get("side") or side).lower()
                        if recovered_side not in ("long", "short"):
                            recovered_side = side
                        multiplier = float(self.exchange.market(symbol).get("contractSize") or 1)
                        stop_price = entry * (1.005 if recovered_side == "short" else 0.995)
                        target_delta = TAKE_PROFIT_USDT / (filled * multiplier)
                        opened_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        opened_at_ms = int(time.time() * 1000)
                        recovered_cost = filled * multiplier * entry
                        recovered_fee = self._estimated_taker_fee(symbol, recovered_cost)
                        pos = {
                            "symbol": symbol, "entry_price": entry, "amount": filled * multiplier,
                            "contracts": filled, "cost": recovered_cost,
                            "remaining_entry_fee_usdt": recovered_fee, "fees_usdt": recovered_fee,
                            "fees_estimated": True, "funding_usdt": 0.0,
                            "funding_cursor_ms": opened_at_ms,
                            "funding_available": False, "opened_at_ms": opened_at_ms,
                            "highest_price": entry, "stop_price": stop_price,
                            "target_price": entry - target_delta if recovered_side == "short" else entry + target_delta,
                            "trailing_distance": 0.0, "risk_distance": abs(entry - stop_price),
                            "partial_taken": True, "partial_pnl_usdt": 0.0,
                            "initial_cost": filled * multiplier * entry, "strategy": "forced_test",
                            "timeframe": "demo-test", "opened_at": opened_at, "side": recovered_side,
                        }
                        self.positions[symbol] = pos
                        self._save_position(pos)
                        self._finish_test_run(test_id, "opened_recovered", symbol, recovered_side, order_id)
                        self.on_decision('Futures Demo', symbol, setup, entry, 'استعادة مركز اختبار بعد رد ملتبس', 'تمت مطابقة المركز من حساب Binance', 'مستعاد', order_id, price_updated_at=price_updated_at)
                        self.on_event("WARN", "تم استرداد مركز اختبار Futures غير مؤكد وربطه بمؤقت الإغلاق")
                    else:
                        self._finish_test_run(test_id, "failed", symbol, side, order_id,
                                              error=type(exc).__name__)
                except Exception:
                    self._finish_test_run(test_id, "needs_review", symbol, side, order_id,
                                          error="confirmation_uncertain")
            self.on_decision('Futures Demo', symbol, setup, price, 'فشل أو لم يتأكد أمر الاختبار', 'راجع سجل Binance قبل أي إعادة', order_status='غير مؤكد' if order_attempted else 'لم يُرسل', order_id=order_id, api_message=exc, price_updated_at=price_updated_at)
            self.on_event("ERR", "فشل اختبار Futures Demo: {}".format(str(exc)[:100]))

    def scan(self, trade_amount_usdt, can_open, force_test_trade=False,
             force_test_id="", force_test_side="long", market_context=None):
        if not self.symbols:
            return
        positions_monitor_ok = self._monitor_positions()
        account = self.balance(ttl=0)
        try:
            if account.get("error") or float(account.get("total") or 0.0) <= 0:
                raise RuntimeError("رصيد Futures Demo غير متاح")
            equity = float(account["total"])
            for symbol, pos in self.positions.items():
                price = float(fetch_fresh_ticker(self.exchange, symbol)["last"])
                market = self.exchange.market(symbol)
                multiplier = float(market.get("contractSize") or 1)
                direction = -1.0 if pos.get("side") == "short" else 1.0
                equity += direction * (price - float(pos["entry_price"])) * float(pos["contracts"]) * multiplier
            self.daily_risk_state = daily_loss_status(
                self.db_path, "futures", equity, MAX_DAILY_LOSS_PCT)
        except Exception as exc:
            self.daily_risk_state = {"market": "futures", "blocked": True,
                "equity": 0.0, "baseline": None, "loss_pct": None,
                "limit_pct": MAX_DAILY_LOSS_PCT,
                "reason": "تعذر التحقق من حد خسارة Futures اليومي ({})".format(type(exc).__name__)}
        if self.daily_risk_state.get("blocked"):
            if not self._daily_risk_block_reported:
                self.on_event("GATE", "Futures: {}".format(self.daily_risk_state["reason"]))
                self._daily_risk_block_reported = True
        else:
            self._daily_risk_block_reported = False
        if force_test_trade:
            if not self.daily_risk_state.get("blocked"):
                self._run_forced_test(trade_amount_usdt, can_open, force_test_id, force_test_side)
            return
        try:
            scan_spot_exposure = self.spot_exposure_provider() if self.spot_exposure_provider else None
        except Exception:
            scan_spot_exposure = None
        for symbol in self.symbols:
            order = None
            order_attempted = False
            try:
                strategy_context = dict(market_context or {})
                spot_exposure = scan_spot_exposure
                if isinstance(spot_exposure, dict):
                    base = symbol.split("/")[0]
                    strategy_context.update({
                        "spot_base": base,
                        "spot_exposure_usdt": float(spot_exposure.get(base) or 0.0),
                    })
                else:
                    strategy_context["spot_exposure_usdt"] = 0.0
                setup = evaluate_symbol(self.exchange, symbol, allow_short=True, context=strategy_context,
                                        allowed_strategies=({"volatility_breakout_hedge"} if symbol in self.hedge_only_symbols else None))
                if symbol in self.hedge_only_symbols and not (setup or {}).get("signal"):
                    base = symbol.split("/")[0]
                    if spot_exposure is None:
                        reason = "عقد تحوّط فقط؛ تعذر التحقق من تعرض Spot الحالي"
                    elif float(spot_exposure.get(base) or 0.0) <= 0:
                        reason = "عقد تحوّط فقط؛ لا يوجد مركز Spot طويل مطابق"
                    else:
                        reason = "يوجد تعرض Spot مطابق، لكن شروط اختراق التقلب الهابط لم تكتمل"
                    setup = dict(setup or {})
                    setup["reason"] = reason
                price = float(fetch_fresh_ticker(self.exchange, symbol)["last"])
                direction = str((setup or {}).get("direction", "flat")).lower()
                price_updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.snapshot[symbol] = {
                    "symbol": symbol, "price": price, "ready": bool(setup),
                    "price_updated_at": price_updated_at,
                    "signal": bool(setup and setup.get("signal")),
                    "direction": direction,
                    "strategy": (setup or {}).get("strategy", "wait"),
                    "timeframe": (setup or {}).get("timeframe", "15m"),
                    "signal_strength": (setup or {}).get("signal_strength", 0),
                    "reason": (setup or {}).get("reason", ""),
                    "updated_at": time.time(),
                    "in_position": symbol in self.positions,
                }
                self.on_event("FUTURES_SCAN", "{} | {} {} | strength {}/100 | {}".format(
                    symbol, (setup or {}).get("strategy_label", "انتظار"),
                    (setup or {}).get("timeframe", "15m"),
                    (setup or {}).get("signal_strength", 0),
                    (setup or {}).get("reason", "بيانات غير كافية")))
                if not setup or not setup.get("signal"):
                    self.on_decision('Futures Demo', symbol, setup, price, 'مراقبة',
                                     (setup or {}).get('reason') or 'لا توجد إشارة مكتملة',
                                     order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                if not positions_monitor_ok:
                    self.on_decision('Futures Demo', symbol, setup, price, 'مرفوض',
                                     'أُوقف الدخول بسبب تعذر متابعة مركز Futures مفتوح',
                                     order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                if self.daily_risk_state.get("blocked"):
                    self.on_decision('Futures Demo', symbol, setup, price, 'مرفوض',
                                     self.daily_risk_state.get('reason') or 'بلغ حد الخسارة اليومي',
                                     order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                if symbol in self.positions:
                    self.on_decision('Futures Demo', symbol, setup, price, 'تجاهل الإشارة',
                                     'يوجد مركز مفتوح على هذا العقد', order_status='لم يُرسل',
                                     price_updated_at=price_updated_at)
                    continue
                if not can_open():
                    self.on_decision('Futures Demo', symbol, setup, price, 'مرفوض',
                                     'بلغت المراكز حد صفقات Futures المفتوحة', order_status='لم يُرسل',
                                     price_updated_at=price_updated_at)
                    continue
                if self.last_entry_candle.get(symbol) == setup.get('candle_ts'):
                    self.on_decision('Futures Demo', symbol, setup, price, 'تجاهل مكرر',
                                     'سبق فحص أو تنفيذ إشارة هذه الشمعة', order_status='لم يُرسل',
                                     price_updated_at=price_updated_at)
                    continue
                if direction not in ("long", "short"):
                    reason = 'اتجاه إشارة غير معروف'
                    self.on_event("WARN", "تخطي {}: {}".format(symbol, reason))
                    self.on_decision('Futures Demo', symbol, setup, price, 'مرفوض', reason,
                                     order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                liquid, liquidity_reason = market_liquidity_check(self.exchange, symbol, "Futures Demo")
                if not liquid:
                    self.on_event("GATE", "تخطي Futures {}: {}".format(symbol, liquidity_reason))
                    self.on_decision('Futures Demo', symbol, setup, price, 'مرفوض', liquidity_reason,
                                     order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                # Fail closed if the account has an unmanaged position on this contract.
                exchange_positions = retry_api_read(self.exchange.fetch_positions, [symbol])
                unmanaged = [p for p in exchange_positions if abs(float(p.get("contracts") or 0)) > 0]
                if unmanaged:
                    reason = 'توجد صفقة Futures على الزوج خارج إدارة البوت'
                    self.on_event("WARN", "تخطي {}: {}".format(symbol, reason))
                    self.on_decision('Futures Demo', symbol, setup, price, 'مرفوض', reason,
                                     order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                account = self.balance(ttl=0)
                if account.get("error"):
                    raise RuntimeError("تعذر قراءة رصيد Futures Demo")
                market = self.exchange.market(symbol)
                multiplier = float(market.get("contractSize") or 1)
                risk = STRATEGY_RISK[setup["strategy"]]
                atr = max(float(setup.get("atr") or 0), price * 0.0005)
                stop_distance = atr * risk["stop_atr"]
                notional_cap = float(trade_amount_usdt)
                hedge_exposure = 0.0
                if setup["strategy"] == "volatility_breakout_hedge":
                    current_exposure = self.spot_exposure_provider() if self.spot_exposure_provider else None
                    hedge_base = str(setup.get("hedge_base") or symbol.split("/")[0])
                    hedge_exposure = float((current_exposure or {}).get(hedge_base) or 0.0)
                    if hedge_exposure <= 0 or current_exposure is None:
                        self.on_decision('Futures Demo', symbol, setup, price, 'إلغاء التحوط',
                                         'تعرض Spot المقابل غير متاح أو انتهى؛ لم يُرسل أمر',
                                         order_status='لم يُرسل', price_updated_at=price_updated_at)
                        continue
                    notional_cap = min(notional_cap, hedge_exposure * MAX_SPOT_HEDGE_RATIO)
                amount_usdt = risk_sized_notional(
                    account["total"], price, stop_distance,
                    notional_cap, RISK_PER_TRADE_PCT)
                if account["free"] < amount_usdt:
                    raise ValueError("رصيد Futures Demo المتاح أقل من قيمة المركز المحسوبة {:.2f} USDT".format(amount_usdt))
                contracts = float(self.exchange.amount_to_precision(
                    symbol, amount_usdt / (price * multiplier)))
                notional = contracts * multiplier * price
                minimum = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 5)
                if contracts <= 0 or notional < minimum:
                    raise ValueError("قيمة المركز المحسوبة أقل من الحد الأدنى لهذا العقد")
                try:
                    self.exchange.set_margin_mode("isolated", symbol)
                except Exception as exc:
                    if "no need to change margin type" not in str(exc).lower():
                        raise
                self.exchange.set_leverage(1, symbol)
                order_side = "sell" if direction == "short" else "buy"
                self.on_decision('Futures Demo', symbol, setup, price, 'إرسال أمر ' + direction,
                                 'اجتازت الإشارة فلاتر المخاطر والسيولة والحساب',
                                 order_status='جارٍ الإرسال', price_updated_at=price_updated_at)
                order_attempted = True
                order = self.exchange.create_order(symbol, "market", order_side, contracts)
                if order.get("status") != "closed" and order.get("id"):
                    order = retry_api_read(self.exchange.fetch_order, order["id"], symbol)
                filled = float(order.get("filled") or 0)
                if filled <= 0:
                    raise RuntimeError("أمر الدخول لم يُنفذ؛ لن تُسجل الصفقة كمفتوحة")
                entry = float(order.get("average") or price)
                atr = max(float(setup.get("atr") or 0), entry * 0.0005)
                if direction == "short":
                    stop_price = entry + atr * risk["stop_atr"]
                    target_price = entry - atr * risk["target_atr"]
                else:
                    stop_price = entry - atr * risk["stop_atr"]
                    target_price = entry + atr * risk["target_atr"]
                entry_fee, entry_fee_estimated = self._order_fee_usdt(order, symbol, filled, entry)
                opened_at_ms = int(time.time() * 1000)
                pos = {
                    "symbol": symbol, "entry_price": entry, "amount": filled * multiplier,
                    "contracts": filled, "cost": filled * multiplier * entry,
                    "remaining_entry_fee_usdt": entry_fee, "fees_usdt": entry_fee,
                    "fees_estimated": entry_fee_estimated, "funding_usdt": 0.0,
                    "funding_cursor_ms": opened_at_ms,
                    "funding_available": False, "opened_at_ms": opened_at_ms,
                    "highest_price": entry, "stop_price": stop_price,
                    "target_price": target_price, "trailing_distance": atr * risk["trail_atr"],
                    "risk_distance": stop_distance, "partial_taken": False, "partial_pnl_usdt": 0.0,
                    "initial_cost": filled * multiplier * entry,
                    "strategy": setup["strategy"], "timeframe": setup["timeframe"],
                    "opened_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "side": direction,
                    "hedge_base": setup.get("hedge_base") if setup.get("strategy") == "volatility_breakout_hedge" else None,
                    "hedge_ratio": MAX_SPOT_HEDGE_RATIO if setup.get("strategy") == "volatility_breakout_hedge" else 0.0,
                    "hedge_exposure_usdt": hedge_exposure if setup.get("strategy") == "volatility_breakout_hedge" else 0.0,
                }
                self.positions[symbol] = pos
                self._save_position(pos)
                self.last_entry_candle[symbol] = setup.get("candle_ts")
                self.on_decision('Futures Demo', symbol, setup, price, 'تم فتح المركز',
                                 'تأكد التنفيذ وحُفظ المركز', order.get('status'), order.get('id'),
                                 price_updated_at=price_updated_at)
                self.on_event("FUTURES", "فتح Demo {} {} · {} · {:.2f} USDT · مخاطرة مستهدفة ≤{:.1f}% · رافعة 1x".format(
                    "Short" if direction == "short" else "Long", symbol,
                    setup.get("strategy_label", setup["strategy"]), notional, RISK_PER_TRADE_PCT))
            except Exception as exc:
                snapshot = self.snapshot.get(symbol) or {}
                self.on_decision('Futures Demo', symbol, snapshot, snapshot.get('price'),
                                 'خطأ في فحص أو أمر Futures', 'تعذر إكمال القرار؛ راجع رسالة API',
                                 order_status='غير مؤكد' if order_attempted else 'لم يُرسل',
                                 api_message=exc, price_updated_at=snapshot.get('price_updated_at'))
                self.on_event("ERR", "Futures {}: {}".format(symbol, str(exc)[:100]))
