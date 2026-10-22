"""Binance USDⓈ-M Futures Demo connector for oil and gold contracts.

Execution is isolated to Binance Demo mode. The engine uses 1x isolated margin,
long-only entries, and keeps a local ATR stop/target monitor. It must never be
constructed with live credentials or a live endpoint.
"""
import datetime
import sqlite3
import time

import ccxt
from strategy_engine import evaluate_symbol, STRATEGY_RISK

TARGET_IDS = ("CLUSDT", "BZUSDT", "XAUUSDT")


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
        self.symbols = []
        for market in self.exchange.markets.values():
            if (market.get("id") in TARGET_IDS and market.get("active") is not False
                    and market.get("swap") and market.get("linear")
                    and market.get("quote") == "USDT"):
                self.symbols.append(market["symbol"])
        self.symbols.sort()
        self.positions = self._load_positions()
        self.snapshot = {}
        self.last_entry_candle = {}
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=20)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS futures_positions (
                symbol TEXT PRIMARY KEY, entry_price REAL NOT NULL, amount REAL NOT NULL,
                contracts REAL NOT NULL, cost REAL NOT NULL, highest_price REAL NOT NULL,
                stop_price REAL NOT NULL, target_price REAL NOT NULL, trailing_distance REAL NOT NULL,
                strategy TEXT NOT NULL, timeframe TEXT NOT NULL, opened_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS futures_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                entry_price REAL NOT NULL, exit_price REAL NOT NULL, pnl_pct REAL NOT NULL,
                pnl_usdt REAL NOT NULL, status TEXT NOT NULL, timestamp TEXT NOT NULL,
                strategy TEXT NOT NULL, timeframe TEXT NOT NULL
            )""")

    def _load_positions(self):
        try:
            with self._connect() as conn:
                rows = conn.execute("""SELECT symbol, entry_price, amount, contracts, cost,
                    highest_price, stop_price, target_price, trailing_distance, strategy,
                    timeframe, opened_at FROM futures_positions""").fetchall()
            return {row[0]: {
                "symbol": row[0], "entry_price": row[1], "amount": row[2],
                "contracts": row[3], "cost": row[4], "highest_price": row[5],
                "stop_price": row[6], "target_price": row[7],
                "trailing_distance": row[8], "strategy": row[9],
                "timeframe": row[10], "opened_at": row[11],
            } for row in rows}
        except sqlite3.OperationalError:
            return {}

    def balance(self):
        try:
            data = self.exchange.fetch_balance()
            return {
                "free": float((data.get("free") or {}).get("USDT") or 0),
                "total": float((data.get("total") or {}).get("USDT") or 0),
            }
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
        return {
            "connected": True, "environment": "demo", "trading_enabled": True,
            "symbols": self.symbols, "open_positions": len(self.positions),
            "balance": self.balance()["total"], "free_balance": self.balance()["free"],
            **stats,
        }

    def _save_position(self, pos):
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO futures_positions
                (symbol, entry_price, amount, contracts, cost, highest_price, stop_price,
                 target_price, trailing_distance, strategy, timeframe, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pos["symbol"], pos["entry_price"], pos["amount"], pos["contracts"],
                 pos["cost"], pos["highest_price"], pos["stop_price"], pos["target_price"],
                 pos["trailing_distance"], pos["strategy"], pos["timeframe"], pos["opened_at"]))

    def _close(self, symbol, pos, price, reason):
        order = self.exchange.create_order(
            symbol, "market", "sell", pos["contracts"], None, {"reduceOnly": True})
        filled = float(order.get("filled") or pos["contracts"])
        exit_price = float(order.get("average") or price)
        market = self.exchange.market(symbol)
        multiplier = float(market.get("contractSize") or 1)
        pnl = (exit_price - pos["entry_price"]) * filled * multiplier
        pnl_pct = pnl / pos["cost"] * 100 if pos["cost"] else 0.0
        status = "WIN" if pnl > 0 else "LOSS"
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("DELETE FROM futures_positions WHERE symbol = ?", (symbol,))
            conn.execute("""INSERT INTO futures_trades
                (symbol, entry_price, exit_price, pnl_pct, pnl_usdt, status, timestamp,
                 strategy, timeframe) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, pos["entry_price"], exit_price, pnl_pct, pnl, status, now,
                 pos["strategy"], pos["timeframe"]))
        self.positions.pop(symbol, None)
        self.on_event("FUTURES", "إغلاق {} {} ({:+.2f} USDT)".format(symbol, reason, pnl))

    def _monitor_positions(self):
        for symbol, pos in list(self.positions.items()):
            try:
                price = float(self.exchange.fetch_ticker(symbol)["last"])
                self.snapshot[symbol] = {"symbol": symbol, "price": price, "ready": True,
                                         "in_position": True}
                pos["highest_price"] = max(pos["highest_price"], price)
                trailing_stop = pos["highest_price"] - pos["trailing_distance"]
                active_stop = max(pos["stop_price"], trailing_stop)
                self._save_position(pos)
                if price >= pos["target_price"]:
                    self._close(symbol, pos, price, "هدف الربح")
                elif price <= active_stop:
                    self._close(symbol, pos, price, "وقف الخسارة")
            except Exception as exc:
                self.on_event("ERR", "متابعة Futures {} فشلت ({})".format(symbol, type(exc).__name__))

    def scan(self, trade_amount_usdt, can_open):
        if not self.symbols:
            return
        self._monitor_positions()
        for symbol in self.symbols:
            try:
                setup = evaluate_symbol(self.exchange, symbol)
                price = float(self.exchange.fetch_ticker(symbol)["last"])
                self.snapshot[symbol] = {
                    "symbol": symbol, "price": price, "ready": bool(setup),
                    "signal": bool(setup and setup.get("signal")),
                    "strategy": (setup or {}).get("strategy", "wait"),
                    "timeframe": (setup or {}).get("timeframe", "15m"),
                    "reason": (setup or {}).get("reason", ""),
                    "in_position": symbol in self.positions,
                }
                if (not setup or not setup.get("signal") or symbol in self.positions
                        or not can_open() or self.last_entry_candle.get(symbol) == setup.get("candle_ts")):
                    continue
                account = self.balance()
                if account.get("error"):
                    raise RuntimeError("تعذر قراءة رصيد Futures Demo")
                if account["free"] < trade_amount_usdt:
                    raise ValueError("رصيد Futures Demo المتاح غير كافٍ")
                market = self.exchange.market(symbol)
                multiplier = float(market.get("contractSize") or 1)
                contracts = float(self.exchange.amount_to_precision(
                    symbol, trade_amount_usdt / (price * multiplier)))
                notional = contracts * multiplier * price
                minimum = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 5)
                if contracts <= 0 or notional < minimum:
                    raise ValueError("قيمة الصفقة أقل من الحد الأدنى لهذا العقد")
                try:
                    self.exchange.set_margin_mode("isolated", symbol)
                except Exception as exc:
                    if "no need to change margin type" not in str(exc).lower():
                        raise
                self.exchange.set_leverage(1, symbol)
                order = self.exchange.create_order(symbol, "market", "buy", contracts)
                filled = float(order.get("filled") or contracts)
                entry = float(order.get("average") or price)
                risk = STRATEGY_RISK[setup["strategy"]]
                atr = max(float(setup.get("atr") or 0), entry * 0.0005)
                pos = {
                    "symbol": symbol, "entry_price": entry, "amount": filled * multiplier,
                    "contracts": filled, "cost": filled * multiplier * entry,
                    "highest_price": entry, "stop_price": entry - atr * risk["stop_atr"],
                    "target_price": entry + atr * risk["target_atr"],
                    "trailing_distance": atr * risk["trail_atr"],
                    "strategy": setup["strategy"], "timeframe": setup["timeframe"],
                    "opened_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.positions[symbol] = pos
                self._save_position(pos)
                self.last_entry_candle[symbol] = setup.get("candle_ts")
                self.on_event("FUTURES", "فتح Demo {} · {} · رافعة 1x".format(
                    symbol, setup.get("strategy_label", setup["strategy"])))
            except Exception as exc:
                self.on_event("ERR", "Futures {}: {}".format(symbol, str(exc)[:100]))
