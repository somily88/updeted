"""Binance USDⓈ-M Futures Demo connector for oil and gold contracts.

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
                             market_liquidity_check, retry_api_read, MAX_DAILY_LOSS_PCT)
from risk_controls import daily_loss_status

TARGET_IDS = ["CLUSDT","BZUSDT","XAUUSDT","MUUSDT","NVDAUSDT","SPCXUSDT","AAPLUSDT","TSLAUSDT","SNDKUSDT","METAUSDT","AMDUSDT","MSFTUSDT","BEUSDT","AMZNUSDT"]
TAKE_PROFIT_USDT = float(os.getenv("TAKE_PROFIT_USDT", "2"))


class BinanceFuturesDemo:
    def __init__(self, api_key, secret_key, db_path, on_event=None):
        if not api_key or not secret_key:
            raise ValueError("Futures Demo API credentials are required")
        self.db_path = db_path
        self.on_event = on_event or (lambda tag, message: None)
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
        for market in self.exchange.markets.values():
            if (market.get("id") in TARGET_IDS and market.get("active") is not False
                    and market.get("swap") and market.get("linear")
                    and market.get("quote") == "USDT"):
                self.symbols.append(market["symbol"])
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
        """Yield one transaction and always close the SQLite connection."""
        conn = sqlite3.connect(self.db_path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
                                       ("initial_cost", "REAL")):
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
            if "side" not in trade_columns:
                conn.execute("ALTER TABLE futures_trades ADD COLUMN side TEXT NOT NULL DEFAULT 'long'")
            conn.execute("""CREATE TABLE IF NOT EXISTS futures_test_runs (
                test_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL,
                symbol TEXT, side TEXT, order_id TEXT, error TEXT
            )""")

    def _load_positions(self):
        try:
            with self._connect() as conn:
                rows = conn.execute("""SELECT symbol, entry_price, amount, contracts, cost,
                    highest_price, stop_price, target_price, trailing_distance, strategy,
                    timeframe, opened_at, side, risk_distance, partial_taken,
                    partial_attempted, partial_pnl_usdt, initial_cost FROM futures_positions""").fetchall()
            return {row[0]: {
                "symbol": row[0], "entry_price": row[1], "amount": row[2],
                "contracts": row[3], "cost": row[4], "highest_price": row[5],
                "stop_price": row[6], "target_price": row[7],
                "trailing_distance": row[8], "strategy": row[9],
                "timeframe": row[10], "opened_at": row[11], "side": row[12] or "long",
                "risk_distance": float(row[13] or 0.0), "partial_taken": bool(row[14]),
                "partial_attempted": bool(row[15]), "partial_pnl_usdt": float(row[16] or 0.0),
                "initial_cost": float(row[17] or row[4] or 0.0),
            } for row in rows}
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
                row = conn.execute("""SELECT COUNT(*),
                    SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END), COALESCE(SUM(pnl_usdt), 0)
                    FROM futures_trades""").fetchone()
            stats = {"closed": row[0] or 0, "wins": row[1] or 0,
                     "losses": row[2] or 0, "realized_pnl": row[3] or 0.0}
        except sqlite3.OperationalError:
            pass
        balance = self.balance()
        return {
            "connected": True, "environment": "demo", "trading_enabled": True,
            "symbols": self.symbols, "unavailable_symbols": self.unavailable_symbols,
            "open_positions": len(self.positions),
            "balance": balance["total"], "free_balance": balance["free"],
            "realized_pnl": stats["realized_pnl"] + sum(float(p.get("partial_pnl_usdt") or 0.0) for p in self.positions.values()),
            **{key: value for key, value in stats.items() if key != "realized_pnl"},
            "daily_risk": self.daily_risk_state,
        }

    def _save_position(self, pos):
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO futures_positions
                (symbol, entry_price, amount, contracts, cost, highest_price, stop_price,
                 target_price, trailing_distance, strategy, timeframe, opened_at, side,
                 risk_distance, partial_taken, partial_attempted, partial_pnl_usdt, initial_cost)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pos["symbol"], pos["entry_price"], pos["amount"], pos["contracts"],
                 pos["cost"], pos["highest_price"], pos["stop_price"], pos["target_price"],
                 pos["trailing_distance"], pos["strategy"], pos["timeframe"], pos["opened_at"],
                 pos.get("side", "long"), pos.get("risk_distance"),
                 1 if pos.get("partial_taken") else 0,
                 1 if pos.get("partial_attempted") else 0, float(pos.get("partial_pnl_usdt") or 0.0),
                 float(pos.get("initial_cost") or pos.get("cost") or 0.0)))

    def _close(self, symbol, pos, price, reason, contracts_to_close=None, partial=False):
        side = pos.get("side", "long")
        close_side = "buy" if side == "short" else "sell"
        previous_contracts = float(pos["contracts"])
        requested = min(previous_contracts, float(contracts_to_close or previous_contracts))
        requested = float(self.exchange.amount_to_precision(symbol, requested))
        if requested <= 0:
            raise ValueError("كمية الإغلاق أقل من دقة العقد")
        # Never retry create_order; only read the status of the submitted order.
        order = self.exchange.create_order(
            symbol, "market", close_side, requested, None, {"reduceOnly": True})
        if order.get("status") != "closed" and order.get("id"):
            order = retry_api_read(self.exchange.fetch_order, order["id"], symbol)
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            raise RuntimeError("أمر الإغلاق لم يُؤكد؛ لن يُعاد إرساله تلقائيًا")
        exit_price = float(order.get("average") or price)
        multiplier = float(self.exchange.market(symbol).get("contractSize") or 1)
        closed_contracts = min(filled, previous_contracts)
        direction = -1.0 if side == "short" else 1.0
        tranche_pnl = direction * (exit_price - pos["entry_price"]) * closed_contracts * multiplier
        closed_cost = float(pos["cost"]) * closed_contracts / previous_contracts
        remaining = max(previous_contracts - closed_contracts, 0.0)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if remaining > 0:
            pos["contracts"] = remaining
            pos["amount"] = remaining * multiplier
            pos["cost"] = max(float(pos["cost"]) - closed_cost, 0.0)
        if partial:
            pos["partial_taken"] = True
            pos["partial_pnl_usdt"] = float(pos.get("partial_pnl_usdt") or 0.0) + tranche_pnl
            pos["stop_price"] = (min(float(pos["stop_price"]), float(pos["entry_price"]))
                                 if side == "short"
                                 else max(float(pos["stop_price"]), float(pos["entry_price"])))
            self._save_position(pos)
            self.on_event("FUTURES", "جني جزئي {} {} · {:+.2f} USDT عند 1R · الوقف للتعادل".format(
                symbol, "Short" if side == "short" else "Long", tranche_pnl))
            return
        if remaining <= 0:
            total_pnl = float(pos.get("partial_pnl_usdt") or 0.0) + tranche_pnl
            basis = float(pos.get("initial_cost") or (pos["cost"] + closed_cost) or 0.0)
            pnl_pct = total_pnl / basis * 100.0 if basis else 0.0
            status = "WIN" if total_pnl > 0 else "LOSS"
            with self._connect() as conn:
                conn.execute("DELETE FROM futures_positions WHERE symbol = ?", (symbol,))
                conn.execute("""INSERT INTO futures_trades
                    (symbol, entry_price, exit_price, pnl_pct, pnl_usdt, status, timestamp,
                     strategy, timeframe, side) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (symbol, pos["entry_price"], exit_price, pnl_pct, total_pnl, status, now,
                     pos["strategy"], pos["timeframe"], side))
            self.positions.pop(symbol, None)
            self.on_event("FUTURES", "إغلاق {} {} {} (إجمالي {:+.2f} USDT)".format(
                symbol, "Short" if side == "short" else "Long", reason, total_pnl))
        else:
            pos["partial_pnl_usdt"] = float(pos.get("partial_pnl_usdt") or 0.0) + tranche_pnl
            with self._connect() as conn:
                conn.execute("""UPDATE futures_positions SET amount=?, contracts=?, cost=?,
                    highest_price=?, partial_pnl_usdt=? WHERE symbol=?""",
                    (pos["amount"], pos["contracts"], pos["cost"], pos["highest_price"],
                     pos["partial_pnl_usdt"], symbol))
            self.on_event("WARN", "إغلاق {} نُفذ جزئيًا؛ سيُتابع المتبقي دون تكرار الأمر".format(symbol))

    def _monitor_positions(self):
        for symbol, pos in list(self.positions.items()):
            try:
                side = pos.get("side", "long")
                if pos.get("partial_attempted") and not pos.get("partial_taken"):
                    continue  # ambiguous tranche order: require reconciliation before another order
                price = float(retry_api_read(self.exchange.fetch_ticker, symbol)["last"])
                self.snapshot[symbol] = {"symbol": symbol, "price": price, "ready": True,
                                         "in_position": True, "direction": side,
                                         "updated_at": time.time()}
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
                self.on_event("ERR", "متابعة Futures {} فشلت ({})".format(symbol, type(exc).__name__))

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
        if not test_id:
            self.on_event("WARN", "اختبار Futures مفعّل بلا FORCE_FUTURES_TEST_ID؛ لم يُرسل أي أمر")
            return
        if side not in ("long", "short"):
            self.on_event("ERR", "اتجاه اختبار Futures غير صالح؛ استخدم long أو short")
            return
        if not can_open():
            self.on_event("WARN", "تعذر اختبار Futures: بلغ حد المراكز المفتوحة")
            return
        if self.positions:
            self.on_event("WARN", "تعذر الاختبار: أغلق مراكز Futures الحالية أولًا")
            return
        symbol = next((s for s in self.symbols if self.exchange.market(s).get("id") == "XAUUSDT"), self.symbols[0])
        if symbol in self.positions:
            self.on_event("WARN", "تخطي اختبار Futures: يوجد مركز مُدار على {}".format(symbol))
            return

        claimed = False
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
            price = float(retry_api_read(self.exchange.fetch_ticker, symbol)["last"])
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
            order = self.exchange.create_order(symbol, "market", order_side, contracts)
            order_id = order.get("id")
            if order.get("status") != "closed" and order.get("id"):
                order = retry_api_read(self.exchange.fetch_order, order["id"], symbol)
            filled = float(order.get("filled") or 0)
            if filled <= 0:
                raise RuntimeError("لم يؤكد Binance تنفيذ أمر اختبار Futures")
            entry = float(order.get("average") or price)
            actual_cost = filled * multiplier * entry
            stop_pct = 0.005
            stop_price = entry * (1 + stop_pct if side == "short" else 1 - stop_pct)
            target_delta = TAKE_PROFIT_USDT / (filled * multiplier)
            target_price = entry - target_delta if side == "short" else entry + target_delta
            opened_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            pos = {
                "symbol": symbol, "entry_price": entry, "amount": filled * multiplier,
                "contracts": filled, "cost": actual_cost, "highest_price": entry,
                "stop_price": stop_price, "target_price": target_price,
                "trailing_distance": 0.0, "risk_distance": abs(entry - stop_price),
                "partial_taken": True, "partial_pnl_usdt": 0.0, "initial_cost": actual_cost,
                "strategy": "forced_test",
                "timeframe": "demo-test", "opened_at": opened_at, "side": side,
            }
            self.positions[symbol] = pos
            self._save_position(pos)
            self._finish_test_run(test_id, "opened", symbol, side, order.get("id"))
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
                        entry = float(recovered.get("entryPrice") or retry_api_read(self.exchange.fetch_ticker, symbol)["last"])
                        recovered_side = str(recovered.get("side") or side).lower()
                        if recovered_side not in ("long", "short"):
                            recovered_side = side
                        multiplier = float(self.exchange.market(symbol).get("contractSize") or 1)
                        stop_price = entry * (1.005 if recovered_side == "short" else 0.995)
                        target_delta = TAKE_PROFIT_USDT / (filled * multiplier)
                        opened_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        pos = {
                            "symbol": symbol, "entry_price": entry, "amount": filled * multiplier,
                            "contracts": filled, "cost": filled * multiplier * entry,
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
                        self.on_event("WARN", "تم استرداد مركز اختبار Futures غير مؤكد وربطه بمؤقت الإغلاق")
                    else:
                        self._finish_test_run(test_id, "failed", symbol, side, order_id,
                                              error=type(exc).__name__)
                except Exception:
                    self._finish_test_run(test_id, "needs_review", symbol, side, order_id,
                                          error="confirmation_uncertain")
            self.on_event("ERR", "فشل اختبار Futures Demo: {}".format(str(exc)[:100]))

    def scan(self, trade_amount_usdt, can_open, force_test_trade=False,
             force_test_id="", force_test_side="long"):
        if not self.symbols:
            return
        self._monitor_positions()
        account = self.balance(ttl=0)
        try:
            if account.get("error") or float(account.get("total") or 0.0) <= 0:
                raise RuntimeError("رصيد Futures Demo غير متاح")
            equity = float(account["total"])
            for symbol, pos in self.positions.items():
                price = float(retry_api_read(self.exchange.fetch_ticker, symbol)["last"])
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
        for symbol in self.symbols:
            try:
                setup = evaluate_symbol(self.exchange, symbol, allow_short=True)
                price = float(retry_api_read(self.exchange.fetch_ticker, symbol)["last"])
                direction = str((setup or {}).get("direction", "flat")).lower()
                self.snapshot[symbol] = {
                    "symbol": symbol, "price": price, "ready": bool(setup),
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
                if self.daily_risk_state.get("blocked"):
                    continue
                if (not setup or not setup.get("signal") or symbol in self.positions
                        or not can_open() or self.last_entry_candle.get(symbol) == setup.get("candle_ts")):
                    continue
                if direction not in ("long", "short"):
                    self.on_event("WARN", "تخطي {}: اتجاه إشارة غير معروف".format(symbol))
                    continue
                liquid, liquidity_reason = market_liquidity_check(self.exchange, symbol, "Futures Demo")
                if not liquid:
                    self.on_event("GATE", "تخطي Futures {}: {}".format(symbol, liquidity_reason))
                    continue
                # Fail closed if the account has an unmanaged position on this contract.
                exchange_positions = retry_api_read(self.exchange.fetch_positions, [symbol])
                unmanaged = [p for p in exchange_positions if abs(float(p.get("contracts") or 0)) > 0]
                if unmanaged:
                    self.on_event("WARN", "تخطي {}: توجد صفقة Futures خارج إدارة البوت".format(symbol))
                    continue
                account = self.balance(ttl=0)
                if account.get("error"):
                    raise RuntimeError("تعذر قراءة رصيد Futures Demo")
                market = self.exchange.market(symbol)
                multiplier = float(market.get("contractSize") or 1)
                risk = STRATEGY_RISK[setup["strategy"]]
                atr = max(float(setup.get("atr") or 0), price * 0.0005)
                stop_distance = atr * risk["stop_atr"]
                amount_usdt = risk_sized_notional(
                    account["total"], price, stop_distance,
                    float(trade_amount_usdt), RISK_PER_TRADE_PCT)
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
                pos = {
                    "symbol": symbol, "entry_price": entry, "amount": filled * multiplier,
                    "contracts": filled, "cost": filled * multiplier * entry,
                    "highest_price": entry, "stop_price": stop_price,
                    "target_price": target_price, "trailing_distance": atr * risk["trail_atr"],
                    "risk_distance": stop_distance, "partial_taken": False, "partial_pnl_usdt": 0.0,
                    "initial_cost": filled * multiplier * entry,
                    "strategy": setup["strategy"], "timeframe": setup["timeframe"],
                    "opened_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "side": direction,
                }
                self.positions[symbol] = pos
                self._save_position(pos)
                self.last_entry_candle[symbol] = setup.get("candle_ts")
                self.on_event("FUTURES", "فتح Demo {} {} · {} · {:.2f} USDT · مخاطرة مستهدفة ≤{:.1f}% · رافعة 1x".format(
                    "Short" if direction == "short" else "Long", symbol,
                    setup.get("strategy_label", setup["strategy"]), notional, RISK_PER_TRADE_PCT))
            except Exception as exc:
                self.on_event("ERR", "Futures {}: {}".format(symbol, str(exc)[:100]))
