import os
import secrets
import hashlib
import hmac
import time
import html
import sqlite3
import datetime
import threading
import logging
from collections import deque
import urllib.request
import urllib.error
import urllib.parse
import json
import re
from cryptography.fernet import Fernet
from pydantic import BaseModel, Field
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from strategy_engine import (evaluate_symbol, STRATEGY_RISK, RISK_PER_TRADE_PCT,
                             TRAILING_ACTIVATION_PCT, MAX_DAILY_LOSS_PCT, risk_sized_notional,
                             market_liquidity_check, retry_api_read, fetch_fresh_ticker)
from futures_engine import BinanceFuturesDemo
from exchange_client import make_binance_exchange
from risk_controls import (daily_loss_status, portfolio_risk_status,
                           MAX_PORTFOLIO_OPEN_RISK_PCT, MAX_CORRELATED_CLUSTER_RISK_PCT)
import database
from database import get_db_connection, open_db_connection

from config import *  # noqa: F401,F403  (configuration moved to config.py)

# Guards runtime settings/credentials that are mutated from API threads and the scheduler.
settings_lock = threading.RLock()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sobhi_trader")

print("🔐 [CONFIG] Binance credentials loaded:", bool(BINANCE_API_KEY and BINANCE_SECRET_KEY))
print("🌐 [ENV] Binance environment:", BINANCE_ENV)
print("📐 [STRATEGY] Automatic selection: trend, breakout, bounce, macro-momentum, news-context and volume-profile | Futures hedge is Spot-linked | ATR risk exits")
print("📰 [NEWS] Filter: {} | Classifier: {} | Pause: {}h".format(
    "ON" if NEWS_FILTER else "OFF", "Groq ({})".format(GROQ_MODEL or "auto model") if GROQ_API_KEY else "Keywords (no GROQ_API_KEY)", NEWS_PAUSE_HOURS))
print("🛒 [TRADING] Order execution:", "ENABLED" if TRADING_ENABLED else "SIMULATION ONLY", "| Amount per trade: {} USDT".format(TRADE_AMOUNT_USDT))


def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN)
        data = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.error("Telegram Error: %s", e)


# =====================================================================
# 1. قاعدة البيانات
# =====================================================================
DB_NAME = database.DB_NAME
_db_dir = os.path.dirname(DB_NAME)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


def init_db():
    try:
        with get_db_connection(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    pnl_pct REAL NOT NULL,
                    status TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
            ''')
            # الصفقات المفتوحة تُحفظ هنا كي لا تضيع عند إعادة تشغيل الحاوية
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS open_positions (
                    symbol TEXT PRIMARY KEY,
                    entry_price REAL NOT NULL,
                    amount REAL NOT NULL,
                    cost REAL NOT NULL,
                    highest_price REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    simulated INTEGER NOT NULL DEFAULT 0
                )
            ''')
            for column, definition in [
                ('strategy', "TEXT NOT NULL DEFAULT 'trend'"),
                ('timeframe', "TEXT NOT NULL DEFAULT '1h'"),
                ('stop_price', 'REAL'),
                ('target_price', 'REAL'),
                ('trailing_distance', 'REAL'),
                ('entries', 'INTEGER NOT NULL DEFAULT 1'),
                ('last_add_candle', 'TEXT'),
                ('add_attempted', 'INTEGER NOT NULL DEFAULT 0'),
                ('risk_distance', 'REAL'),
                ('partial_taken', 'INTEGER NOT NULL DEFAULT 0'),
                ('partial_attempted', 'INTEGER NOT NULL DEFAULT 0'),
                ('partial_pnl_usdt', 'REAL NOT NULL DEFAULT 0'),
                ('initial_cost', 'REAL'),
                ('exit_attempted', 'INTEGER NOT NULL DEFAULT 0'),
                ('fee_quality', "TEXT NOT NULL DEFAULT 'unverified'"),
                ('protective_order_id', 'TEXT'),
                ('protective_client_id', 'TEXT'),
                ('protective_stop_price', 'REAL'),
                ('reconciliation_required', 'INTEGER NOT NULL DEFAULT 0'),
                ('fees_usdt', 'REAL NOT NULL DEFAULT 0'),
            ]:
                existing = {row[1] for row in cursor.execute('PRAGMA table_info(open_positions)').fetchall()}
                if column not in existing:
                    cursor.execute('ALTER TABLE open_positions ADD COLUMN {} {}'.format(column, definition))
            # لقطات قيمة المحفظة (للرسم البياني في لوحة التحكم)
            cursor.execute('CREATE TABLE IF NOT EXISTS equity (ts INTEGER PRIMARY KEY, value REAL NOT NULL)')
            cursor.execute('CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            cursor.executemany('INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)', [
                ('trade_amount_usdt', str(TRADE_AMOUNT_USDT)),
                ('spot_max_concurrent_trades', str(MAX_SPOT_CONCURRENT_TRADES)),
                ('futures_max_concurrent_trades', str(MAX_FUTURES_CONCURRENT_TRADES))
            ])
            cursor.execute('CREATE TABLE IF NOT EXISTS platform_credentials (platform TEXT PRIMARY KEY, api_key_enc TEXT NOT NULL, secret_enc TEXT NOT NULL, updated_at TEXT NOT NULL)')
            cursor.execute('''CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                signal TEXT NOT NULL,
                strategy TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                price REAL,
                price_updated_at TEXT,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                order_status TEXT,
                order_id TEXT,
                api_message TEXT
            )''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_decision_log_created_at ON decision_log(id DESC)')
            try:
                cursor.execute('ALTER TABLE trades ADD COLUMN pnl_usdt REAL')
            except sqlite3.OperationalError:
                pass  # العمود موجود مسبقاً
            trade_columns = {row[1] for row in cursor.execute('PRAGMA table_info(trades)').fetchall()}
            for column, definition in [('strategy', 'TEXT'), ('timeframe', 'TEXT'),
                                       ('cost_quality', "TEXT NOT NULL DEFAULT 'legacy_or_unverified'"),
                                       ('gross_pnl_usdt', 'REAL'), ('fees_usdt', 'REAL'),
                                       ('exit_reason', 'TEXT')]:
                if column not in trade_columns:
                    cursor.execute('ALTER TABLE trades ADD COLUMN {} {}'.format(column, definition))
    except Exception as e:
        logger.error("Database Init Error: %s", e)



def _credential_cipher():
    if not CREDENTIAL_ENCRYPTION_KEY:
        raise RuntimeError("Credential encryption is not configured")
    return Fernet(CREDENTIAL_ENCRYPTION_KEY.encode("ascii"))


def load_runtime_settings():
    global TRADE_AMOUNT_USDT, MAX_SPOT_CONCURRENT_TRADES, MAX_FUTURES_CONCURRENT_TRADES
    conn = None
    try:
        conn = open_db_connection(DB_NAME)
        values = dict(conn.execute('SELECT key, value FROM bot_settings').fetchall())
        stored_amount = float(values.get('trade_amount_usdt', TRADE_AMOUNT_USDT))
        # One-time migration: old small defaults (100/150 USDT) silently capped every
        # risk-sized order. Lift them once to the configured cap; the marker key keeps
        # any value the user saves afterwards (even 150) untouched.
        if ('trade_amount_migrated_v2' not in values
                and any(abs(stored_amount - old) < 1e-9 for old in LEGACY_TRADE_AMOUNTS)
                and TRADE_AMOUNT_USDT > stored_amount):
            stored_amount = float(TRADE_AMOUNT_USDT)
            conn.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                         ('trade_amount_usdt', str(stored_amount)))
        conn.execute('INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)',
                     ('trade_amount_migrated_v2', '1'))
        conn.commit()
        TRADE_AMOUNT_USDT = stored_amount
        for setting_key, env_name, global_name, current_value in [
            ('spot_max_concurrent_trades', 'MAX_SPOT_CONCURRENT_TRADES', 'spot', MAX_SPOT_CONCURRENT_TRADES),
            ('futures_max_concurrent_trades', 'MAX_FUTURES_CONCURRENT_TRADES', 'futures', MAX_FUTURES_CONCURRENT_TRADES),
        ]:
            env_value = os.getenv(env_name, '').strip()
            # Migrate older installs that stored one shared limit into independent caps.
            stored_value = values.get(setting_key, values.get('max_concurrent_trades', current_value))
            requested_value = int(env_value or stored_value)
            if not 1 <= requested_value <= 20:
                raise ValueError("{} must be between 1 and 20".format(env_name))
            if global_name == 'spot':
                MAX_SPOT_CONCURRENT_TRADES = requested_value
            else:
                MAX_FUTURES_CONCURRENT_TRADES = requested_value
            if env_value or setting_key not in values:
                conn.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                             (setting_key, str(requested_value)))
                conn.commit()
    except Exception as e:
        logger.error("Settings Load Error: %s", e)
    finally:
        if conn is not None:
            conn.close()


def load_saved_binance_credentials(platform='binance'):
    if not CREDENTIAL_ENCRYPTION_KEY:
        return None
    try:
        with get_db_connection(DB_NAME) as conn:
            row = conn.execute('SELECT api_key_enc, secret_enc FROM platform_credentials WHERE platform = ?', (platform,)).fetchone()
        if row:
            cipher = _credential_cipher()
            return cipher.decrypt(row[0].encode()).decode(), cipher.decrypt(row[1].encode()).decode()
    except Exception as e:
        print("Saved credential load error (details omitted): {}".format(type(e).__name__))
    return None


def r_multiple_target(entry_price, stop_price, r_multiple=None):
    """Long take-profit at entry + R x (entry - stop), replacing the old fixed USDT target."""
    r_multiple = TP_R_MULTIPLE if r_multiple is None else float(r_multiple)
    entry_price = float(entry_price)
    risk = max(entry_price - float(stop_price), entry_price * 0.0005)
    return entry_price + r_multiple * risk


def spot_exit_reason(reason, partial_taken=False, trail_armed=False):
    """Normalize a spot close reason to TP/SL/breakeven/trailing/TEST/manual."""
    if reason == 'SL':
        if trail_armed:
            return 'trailing'
        return 'breakeven' if partial_taken else 'SL'
    return reason if reason in ('TP', 'TEST') else 'manual'


def log_trade(symbol, entry_price, exit_price, pnl_pct, status, pnl_usdt=None,
              strategy=None, timeframe=None):
    try:
        with get_db_connection(DB_NAME) as conn:
            cursor = conn.cursor()
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                INSERT INTO trades
                    (symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, strategy, timeframe)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, entry_price, exit_price, pnl_pct, status, now_str, pnl_usdt, strategy, timeframe))
    except Exception as e:
        logger.error("Log Trade Error: %s", e)


def record_closed_spot_trade(symbol, entry_price, exit_price, pnl_pct, status,
                              pnl_usdt=None, strategy=None, timeframe=None, cost_quality='unverified',
                              gross_pnl_usdt=None, fees_usdt=None, exit_reason=None):
    """Atomically record the realized trade and remove its persisted open position."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection(DB_NAME) as conn:
        conn.execute("""INSERT INTO trades
            (symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, strategy, timeframe,
             cost_quality, gross_pnl_usdt, fees_usdt, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, entry_price, exit_price, pnl_pct, status, now_str,
             pnl_usdt, strategy, timeframe, cost_quality, gross_pnl_usdt, fees_usdt, exit_reason))
        conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))


def get_recent_trades(limit=15):
    try:
        with get_db_connection(DB_NAME) as conn:
            rows = conn.execute("""SELECT id, symbol, entry_price, exit_price, pnl_pct, status, timestamp,
                pnl_usdt, cost_quality, gross_pnl_usdt, fees_usdt FROM trades ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
            return [tuple(row) for row in rows]
    except Exception as e:
        print("Get Trades Error: {}".format(type(e).__name__))
        return []
def get_performance_stats():
    """Summarize only closed trades with an explicitly recorded net result."""
    try:
        with get_db_connection(DB_NAME) as conn:
            row = conn.execute("""SELECT COUNT(*),
                SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN pnl_pct IS NOT NULL THEN pnl_pct END),
                MAX(pnl_pct), MIN(pnl_pct)
                FROM trades
                WHERE pnl_usdt IS NOT NULL AND cost_quality IN ('actual','estimated')""").fetchone()
        if row and row[0] > 0:
            total = int(row[0])
            wins = int(row[1] or 0)
            losses = total - wins
            win_rate = wins / total * 100.0
            total_pnl_pct = float(row[2] or 0.0)
            return total, wins, losses, win_rate, total_pnl_pct, float(row[3] or 0.0), float(row[4] or 0.0)
        return 0, 0, 0, 0.0, 0.0, 0.0, 0.0
    except Exception as e:
        print("Stats Error: {}".format(type(e).__name__))
        return 0, 0, 0, 0.0, 0.0, 0.0, 0.0
def get_realized_stats():
    """Return net Spot PnL only when the result and cost quality are known."""
    known = "pnl_usdt IS NOT NULL AND cost_quality IN ('actual','estimated')"
    try:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        with get_db_connection(DB_NAME) as conn:
            total = conn.execute("SELECT COALESCE(SUM(pnl_usdt), 0) FROM trades WHERE " + known).fetchone()[0]
            today_total = conn.execute("SELECT COALESCE(SUM(pnl_usdt), 0) FROM trades WHERE " + known +
                                       " AND timestamp LIKE ?", (today + "%",)).fetchone()[0]
            unverified = conn.execute("""SELECT COUNT(*) FROM trades
                WHERE pnl_usdt IS NULL OR cost_quality IS NULL
                   OR cost_quality NOT IN ('actual','estimated','simulated')""").fetchone()[0]
            estimated = conn.execute("""SELECT COUNT(*) FROM trades
                WHERE pnl_usdt IS NOT NULL AND cost_quality='estimated'""").fetchone()[0]
        return float(total or 0.0), float(today_total or 0.0), int(unverified or 0), int(estimated or 0)
    except Exception as e:
        print("Realized Stats Error: {}".format(type(e).__name__))
        return 0.0, 0.0, 0, 0
EQUITY_RANGES = {'1h': 3600, '8h': 8 * 3600, '1d': 86400, '1w': 7 * 86400, '1m': 30 * 86400, 'all': None}


def record_equity():
    """حفظ قيمة المحفظة الحالية (تُستدعى مع كل فحص)"""
    try:
        _, total = get_binance_balance()
        if total and total > 0:
            with get_db_connection(DB_NAME) as conn:
                conn.execute('INSERT OR REPLACE INTO equity (ts, value) VALUES (?, ?)', (int(time.time()), float(total)))
    except Exception as e:
        logger.error("Record Equity Error: %s", e)


def get_equity_series(range_key="1d", max_points=300):
    try:
        seconds = EQUITY_RANGES.get(range_key, 86400)
        with get_db_connection(DB_NAME) as conn:
            cursor = conn.cursor()
            if seconds:
                cursor.execute('SELECT ts, value FROM equity WHERE ts >= ? ORDER BY ts', (int(time.time()) - seconds,))
            else:
                cursor.execute('SELECT ts, value FROM equity ORDER BY ts')
            rows = cursor.fetchall()
        if len(rows) > max_points:
            step = len(rows) / float(max_points)
            rows = [rows[int(i * step)] for i in range(max_points)] + [rows[-1]]
        return [{'t': r[0], 'v': r[1]} for r in rows]
    except Exception as e:
        logger.error("Equity Series Error: %s", e)
        return []


def save_position(symbol, pos):
    try:
        with get_db_connection(DB_NAME) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO open_positions
                (symbol, entry_price, amount, cost, highest_price, opened_at, simulated,
                 strategy, timeframe, stop_price, target_price, trailing_distance, entries, last_add_candle, add_attempted,
                 risk_distance, partial_taken, partial_attempted, partial_pnl_usdt, initial_cost,
                 exit_attempted, fee_quality, protective_order_id, protective_client_id,
                 protective_stop_price, reconciliation_required, fees_usdt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, pos['entry_price'], pos['amount'], pos['cost'],
                  pos['highest_price'], pos['time'], 1 if pos.get('simulated') else 0,
                  pos.get('strategy', 'trend'), pos.get('timeframe', '1h'),
                  pos.get('stop_price'), pos.get('target_price'), pos.get('trailing_distance'),
                  max(1, int(pos.get('entries', 1))), pos.get('last_add_candle'),
                  1 if pos.get('add_attempted') else 0, pos.get('risk_distance'),
                  1 if pos.get('partial_taken') else 0, 1 if pos.get('partial_attempted') else 0,
                  float(pos.get('partial_pnl_usdt') or 0.0),
                  float(pos.get('initial_cost') or pos.get('cost') or 0.0),
                  1 if pos.get('exit_attempted') else 0, pos.get('fee_quality', 'unverified'),
                  pos.get('protective_order_id'), pos.get('protective_client_id'),
                  pos.get('protective_stop_price'), 1 if pos.get('reconciliation_required') else 0,
                  float(pos.get('fees_usdt') or 0.0)))
        return True
    except Exception as e:
        logger.error("Save Position Error: %s", e)
        return False


def delete_position(symbol):
    try:
        with get_db_connection(DB_NAME) as conn:
            conn.execute('DELETE FROM open_positions WHERE symbol = ?', (symbol,))
    except Exception as e:
        logger.error("Delete Position Error: %s", e)


def load_positions():
    result = {}
    try:
        with get_db_connection(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''SELECT symbol, entry_price, amount, cost, highest_price, opened_at,
                              simulated, strategy, timeframe, stop_price, target_price, trailing_distance,
                              entries, last_add_candle, add_attempted, risk_distance, partial_taken, partial_attempted,
                              partial_pnl_usdt, initial_cost, exit_attempted, fee_quality, protective_order_id,
                              protective_client_id, protective_stop_price, reconciliation_required, fees_usdt
                              FROM open_positions''')
            for r in cursor.fetchall():
                result[r[0]] = {
                    'entry_price': r[1], 'amount': r[2], 'cost': r[3],
                    'highest_price': r[4], 'time': r[5], 'simulated': bool(r[6]),
                    'strategy': r[7] or 'trend', 'timeframe': r[8] or '1h',
                    'stop_price': r[9], 'target_price': r[10], 'trailing_distance': r[11],
                    'entries': max(1, int(r[12] or 1)), 'last_add_candle': r[13],
                    'add_attempted': bool(r[14]), 'risk_distance': float(r[15] or 0.0),
                    'partial_taken': bool(r[16]), 'partial_attempted': bool(r[17]),
                    'partial_pnl_usdt': float(r[18] or 0.0), 'initial_cost': float(r[19] or r[3] or 0.0),
                    'exit_attempted': bool(r[20]), 'fee_quality': r[21] or 'unverified',
                    'protective_order_id': r[22], 'protective_client_id': r[23],
                    'protective_stop_price': float(r[24] or 0.0),
                    'reconciliation_required': bool(r[25]), 'fees_usdt': float(r[26] or 0.0),
                }
                if result[r[0]]['fees_usdt'] <= 0 and result[r[0]]['initial_cost'] > 0:
                    result[r[0]]['fees_usdt'] = result[r[0]]['initial_cost'] * 0.001
                    if result[r[0]].get('fee_quality') == 'unverified':
                        result[r[0]]['fee_quality'] = 'estimated'
    except Exception as e:
        logger.error("Load Positions Error: %s", e)
    return result


# =====================================================================
# 2. الاتصال بـ Binance (demo / testnet / live)
# =====================================================================
init_db()
load_runtime_settings()
print("⚙️ [SETTINGS] Max concurrent trades: Spot={} Futures={}".format(
    MAX_SPOT_CONCURRENT_TRADES, MAX_FUTURES_CONCURRENT_TRADES))
saved_binance_credentials = load_saved_binance_credentials()
if saved_binance_credentials:
    BINANCE_API_KEY, BINANCE_SECRET_KEY = saved_binance_credentials
saved_futures_credentials = load_saved_binance_credentials('binance_futures')
if saved_futures_credentials:
    BINANCE_FUTURES_API_KEY, BINANCE_FUTURES_SECRET_KEY = saved_futures_credentials

exchange = make_binance_exchange(BINANCE_API_KEY, BINANCE_SECRET_KEY, BINANCE_ENV)
if BINANCE_ENV == "live":
    print("⚠️ [ENV] الاتصال بالحساب الحقيقي! الأوامر {}".format("مفعّلة" if TRADING_ENABLED else "معطّلة (محاكاة)"))
elif BINANCE_ENV not in ("demo", "testnet"):
    print("❌ [ENV] قيمة BINANCE_ENV غير معروفة: {} (استخدم demo أو testnet أو live)".format(BINANCE_ENV))

active_positions = load_positions()
market_snapshot = {}
last_entry_candle = {}   # آخر شمعة تمت محاولة الدخول عليها لكل عملة (لمنع تكرار الدخول على نفس الشمعة)
scan_lock = threading.Lock()
scanner_state = {
    'status': 'waiting', 'last_started_at': None, 'last_completed_at': None,
    'last_error': None, 'duration_seconds': None
}
event_log = deque(maxlen=40)   # آخر الأحداث المهمة لعرضها في لوحة التحكم
daily_risk_state = {'spot': None, 'futures': None}
_daily_risk_block_reported = {'spot': False, 'futures': False}


def update_daily_risk_state(market, state):
    daily_risk_state[market] = state
    blocked = bool(state.get('blocked'))
    if blocked and not _daily_risk_block_reported[market]:
        log_event('GATE', '{}: {}'.format(market, state.get('reason', 'تم إيقاف الدخول اليوم')))
        _daily_risk_block_reported[market] = True
    elif not blocked:
        _daily_risk_block_reported[market] = False


def log_event(tag, text):
    event_log.appendleft({'t': datetime.datetime.now().strftime("%H:%M:%S"), 'tag': tag, 'text': text})
    if tag in ("FUTURES", "FUTURES_SCAN", "WARN", "ERR"):
        print("[{}] {}".format(tag, text), flush=True)


def _safe_decision_text(value, limit=300):
    text = str(value or '').replace('\x00', '').strip()
    for secret in (BINANCE_API_KEY, BINANCE_SECRET_KEY, BINANCE_FUTURES_API_KEY,
                   BINANCE_FUTURES_SECRET_KEY, DASHBOARD_PASSWORD, GROQ_API_KEY,
                   TELEGRAM_BOT_TOKEN, CREDENTIAL_ENCRYPTION_KEY):
        if secret:
            text = text.replace(str(secret), '[محجوب]')
    return text[:limit]


_STRATEGY_LABELS_AR = {
    'trend': 'تتبّع الاتجاه', 'breakout': 'الاختراق', 'bounce': 'الارتداد',
    'macro_momentum_hybrid': 'هجين الزخم الكلي',
    'algorithmic_discretionary_hybrid': 'خوارزمي مع سياق الأخبار',
    'volume_profile_range': 'نطاق حجم التداول',
    'volatility_breakout_hedge': 'اختراق تقلب مع تحوّط',
    'wait': 'انتظار', 'forced_test': 'اختبار Futures', 'test': 'اختبار Spot',
}
_DECISION_REASON_LABELS_AR = {
    'TP': 'تحقق هدف الربح', 'SL': 'بلغ السعر وقف الخسارة',
    'TEST': 'إغلاق صفقة اختبار', 'stop_loss': 'بلغ السعر وقف الخسارة',
    'take_profit': 'تحقق هدف الربح',
}


def record_decision(market, symbol, setup=None, price=None, outcome='مراقبة', reason='',
                    order_status=None, order_id=None, api_message=None, price_updated_at=None):
    setup = setup or {}
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    direction = str(setup.get('direction') or ('long' if setup.get('signal') else 'none'))
    if not setup.get('signal'):
        direction = 'none'
    strategy = setup.get('strategy_label') or _STRATEGY_LABELS_AR.get(
        str(setup.get('strategy') or ''), setup.get('strategy') or '—')
    reason = _DECISION_REASON_LABELS_AR.get(str(reason), reason)
    values = (
        created_at, str(market), str(symbol), direction,
        _safe_decision_text(strategy, 80),
        str(setup.get('timeframe') or '—'),
        float(price) if price is not None else None,
        price_updated_at or setup.get('price_updated_at'),
        _safe_decision_text(outcome, 80), _safe_decision_text(reason, 400),
        _safe_decision_text(order_status, 80) if order_status else None,
        _safe_decision_text(order_id, 100) if order_id else None,
        _safe_decision_text(api_message, 300) if api_message else None,
    )
    conn = None
    try:
        conn = open_db_connection(DB_NAME)
        conn.execute('''INSERT INTO decision_log
            (created_at, market, symbol, signal, strategy, timeframe, price, price_updated_at,
             outcome, reason, order_status, order_id, api_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', values)
        conn.execute('''DELETE FROM decision_log WHERE id NOT IN
            (SELECT id FROM decision_log ORDER BY id DESC LIMIT 10000)''')
        conn.commit()
    except Exception as exc:
        print('[DECISION_LOG] تعذر حفظ سجل القرار: {}'.format(type(exc).__name__), flush=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def _explain_api_message(message):
    text = str(message or '').strip()
    lowered = text.lower()
    if not text:
        return ''
    if any(term in lowered for term in ('insufficient balance', 'insufficient funds', 'not enough balance')):
        return 'الرصيد المتاح لا يكفي لتنفيذ الأمر.'
    if any(term in lowered for term in ('invalid api-key', 'invalid api key', 'unauthorized', 'authentication')):
        return 'رفض Binance المصادقة؛ تحقق من المفتاح وصلاحياته.'
    if 'timestamp' in lowered or 'recvwindow' in lowered:
        return 'وقت الطلب لا يتوافق مع ساعة Binance؛ تحقق من اتصال الخادم والوقت.'
    if any(term in lowered for term in ('lot_size', 'precision is over', 'invalid quantity')):
        return 'كمية الأمر لا تطابق الدقة أو الحد الأدنى المسموح للزوج.'
    if any(term in lowered for term in ('min_notional', 'notional', 'minimum order')):
        return 'قيمة الأمر أقل من الحد الأدنى الذي يقبله الزوج.'
    if any(term in lowered for term in ('too many requests', 'rate limit', '429')):
        return 'أوقف Binance الطلبات مؤقتًا بسبب كثرتها.'
    if any(term in lowered for term in ('timeout', 'network', 'exchange unavailable', 'connection')):
        return 'تعذر الاتصال بـ Binance؛ راجع سجل الأوامر قبل إعادة المحاولة.'
    if 'market is closed' in lowered or 'market closed' in lowered:
        return 'السوق مغلق أو لا يقبل الأوامر حاليًا.'
    return 'تفاصيل رد Binance: ' + text


def _clarify_indicator_terms(text):
    labels = {
        'EMA50': 'المتوسط المتحرك الأسي لآخر 50 شمعة',
        'EMA20': 'المتوسط المتحرك الأسي لآخر 20 شمعة',
        'RSI': 'مؤشر قوة الحركة',
        'MACD': 'مؤشر اتجاه الزخم',
        'ATR': 'مقياس تقلب السعر',
    }
    for index, (term, arabic) in enumerate(labels.items()):
        token = '__PROTECTED_INDICATOR_{}__'.format(index)
        text = text.replace('({})'.format(term), token)
        text = text.replace(term, '{} ({})'.format(arabic, term))
        text = text.replace(token, '({})'.format(term))
    return text


def get_recent_decisions(limit=80):
    conn = open_db_connection(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('''SELECT created_at, market, symbol, signal, strategy, timeframe,
            price, price_updated_at, outcome, reason, order_status, order_id, api_message
            FROM decision_log ORDER BY id DESC LIMIT ?''', (max(1, min(int(limit), 200)),)).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row['strategy'] = _STRATEGY_LABELS_AR.get(row.get('strategy'), row.get('strategy') or '—')
            row['reason'] = _DECISION_REASON_LABELS_AR.get(row.get('reason'), row.get('reason') or '—')
            row['reason'] = _clarify_indicator_terms(row['reason'])
            row['api_message_ar'] = _explain_api_message(row.get('api_message'))
        return result
    except Exception as exc:
        print('[DECISION_LOG] تعذر قراءة سجل القرارات: {}'.format(type(exc).__name__), flush=True)
        return []
    finally:
        conn.close()
scheduler = BackgroundScheduler()
futures_engine = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        exchange.load_markets()
        if exchange.markets:
            SUPPORTED_SYMBOLS.clear()
            for symbol in SYMBOLS:
                market = exchange.markets.get(symbol)
                if market and market.get('spot') and market.get('active') is not False:
                    SUPPORTED_SYMBOLS.add(symbol)
                else:
                    UNSUPPORTED_SYMBOLS[symbol] = "الزوج غير متاح في Binance Spot Demo"
            if UNSUPPORTED_SYMBOLS:
                print("⚠️ [MARKETS] Pairs unavailable in Demo: {}".format(", ".join(sorted(UNSUPPORTED_SYMBOLS))))
            print("✅ [MARKETS] Active spot pairs: {}".format(", ".join(sorted(SUPPORTED_SYMBOLS))))
    except Exception as e:
        logger.error("Load Markets Error: %s", e)
    try:
        if not _reconcile_spot_positions():
            log_event('ERR', 'توقفت إدخالات Spot الجديدة: توجد مراكز تحتاج مطابقة')
    except Exception as exc:
        log_event('ERR', 'تعذر بدء مطابقة Spot Demo ({})'.format(type(exc).__name__))
    global futures_engine
    if BINANCE_ENV == 'demo' and BINANCE_FUTURES_API_KEY and BINANCE_FUTURES_SECRET_KEY:
        try:
            futures_engine = BinanceFuturesDemo(BINANCE_FUTURES_API_KEY, BINANCE_FUTURES_SECRET_KEY, DB_NAME, log_event, record_decision, get_spot_hedge_exposures, portfolio_risk_snapshot)
            print('✅ [FUTURES DEMO] Active contracts: {}'.format(', '.join(futures_engine.symbols) or 'none'))
            if not futures_engine.symbols:
                log_event('WARN', 'Binance Futures Demo لا يعرض عقود CL/BZ/XAU المطلوبة')
        except Exception as e:
            futures_engine = None
            print('❌ [FUTURES DEMO] Connection unavailable ({})'.format(type(e).__name__))
            log_event('ERR', 'تعذر الاتصال بـ Binance Futures Demo')
    else:
        print('ℹ️ [FUTURES DEMO] بانتظار مفاتيح API الخاصة بـ Futures Demo')
    scheduler.add_job(automated_market_scan, 'interval', minutes=SCAN_INTERVAL_MINUTES,
                      max_instances=1, coalesce=True, misfire_grace_time=60)
    if NEWS_FILTER:
        scheduler.add_job(check_news, 'interval', minutes=60, max_instances=1, coalesce=True,
                          next_run_time=datetime.datetime.now())
    scheduler.start()
    log_event("START", "البوت يعمل ({} | اختيار الاستراتيجية تلقائي)".format(BINANCE_ENV))
    print("🚀 [SCHEDULER] تم تفعيل الجدولة الآلية (البيئة: {})".format(BINANCE_ENV))
    if active_positions:
        print("♻️ [RESTORE] تم استرجاع {} صفقة مفتوحة من قاعدة البيانات".format(len(active_positions)))
    send_telegram_message("🤖 <b>تم تشغيل بوت التداول ({})</b>\n🛒 تنفيذ الأوامر: {}\n💵 حجم الصفقة: {} USDT".format(
        BINANCE_ENV, "فعلي" if TRADING_ENABLED else "محاكاة", TRADE_AMOUNT_USDT))
    yield
    scheduler.shutdown()


_dashboard_auth = HTTPBasic()
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")


def require_dashboard_auth(credentials: HTTPBasicCredentials = Depends(_dashboard_auth)):
    valid_user = secrets.compare_digest(credentials.username.encode("utf-8"), DASHBOARD_USER.encode("utf-8"))
    valid_password = secrets.compare_digest(credentials.password.encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8"))
    if not DASHBOARD_USER or not DASHBOARD_PASSWORD or not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


app = FastAPI(title="Crypto Trading Bot Spot Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
class RuntimeSettings(BaseModel):
    trade_amount_usdt: float = Field(gt=0, le=100000)
    spot_max_concurrent_trades: int = Field(default=6, ge=1, le=20)
    futures_max_concurrent_trades: int = Field(default=6, ge=1, le=20)


class BinanceCredentials(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    secret_key: str = Field(min_length=8, max_length=256)


class FuturesCredentials(BaseModel):
    api_key: str = Field(min_length=8, max_length=256)
    secret_key: str = Field(min_length=8, max_length=256)


class TradFiAgreementConsent(BaseModel):
    accepted: bool
_balance_cache = {'t': 0.0, 'v': (0.0, 0.0), 'error': False}
class TTLCache(dict):
    """Thread-protected caller-managed short TTL cache with a hard size bound."""
    def __init__(self, ttl=5, maxsize=100):
        super().__init__()
        self.ttl = float(ttl)
        self.maxsize = max(1, int(maxsize))

    def set_price(self, key, price, now=None):
        now = time.time() if now is None else float(now)
        for expired in [k for k, item in self.items() if now - item['time'] >= self.ttl]:
            super().pop(expired, None)
        if key not in self and len(self) >= self.maxsize:
            oldest = min(self, key=lambda k: self[k]['time'])
            super().pop(oldest, None)
        super().__setitem__(key, {'price': float(price), 'time': now})

    def get_price(self, key, now=None, ttl=None):
        item = super().get(key)
        if item is None:
            return None
        now = time.time() if now is None else float(now)
        if now - item['time'] >= (self.ttl if ttl is None else float(ttl)):
            super().pop(key, None)
            return None
        return item['price']


_position_price_cache = TTLCache(ttl=5, maxsize=100)
_position_price_lock = threading.Lock()


def get_spot_mark_price(symbol, fallback_price=None, ttl=5):
    """Fresh Demo ticker for portfolio marks, with a short cache and stale fallback."""
    now = time.time()
    with _position_price_lock:
        cached_price = _position_price_cache.get_price(symbol, now, ttl)
        if cached_price is not None:
            return float(cached_price), True
    try:
        price = float(fetch_fresh_ticker(exchange, symbol)['last'])
        if price <= 0:
            raise ValueError("non-positive ticker")
        with _position_price_lock:
            _position_price_cache.set_price(symbol, price, now)
        snapshot = market_snapshot.get(symbol)
        if isinstance(snapshot, dict):
            snapshot['price'] = price
            snapshot['price_updated_at'] = now
        return price, True
    except Exception as exc:
        snapshot = market_snapshot.get(symbol) or {}
        stale = snapshot.get('price')
        print("Spot mark price unavailable for {} ({})".format(symbol, type(exc).__name__))
        if isinstance(stale, (int, float)) and stale > 0:
            return float(stale), False
        if fallback_price and float(fallback_price) > 0:
            return float(fallback_price), False
        raise


def get_spot_hedge_exposures():
    """Return fresh managed Spot exposure by base asset, or None when any mark is stale."""
    exposures = {}
    for symbol, pos in list(active_positions.items()):
        if pos.get('simulated'):
            continue
        try:
            price, fresh = get_spot_mark_price(symbol, pos.get('entry_price'))
        except Exception:
            logger.debug("Suppressed error in %s", "get_spot_hedge_exposures", exc_info=True)
            return None
        if not fresh or price <= 0:
            return None
        base = symbol.split('/')[0]
        exposures[base] = exposures.get(base, 0.0) + float(pos.get('amount') or 0.0) * price
    return exposures


def strategy_news_context():
    if (not NEWS_FILTER or not news_state.get('checked_epoch')
            or time.time() - float(news_state.get('checked_epoch') or 0.0) > NEWS_WINDOW_HOURS * 3600):
        return {'news_sentiment': 'neutral', 'news_severity': 0}
    return {'news_sentiment': news_state.get('sentiment', 'neutral'),
            'news_severity': int(news_state.get('severity') or 0)}

def get_binance_balance(ttl=10):
    """Return free cash and Spot equity marked with current prices for managed holdings."""
    now = time.time()
    cache_fresh = _balance_cache['v'] != (0.0, 0.0) and now - _balance_cache['t'] < ttl
    if cache_fresh:
        free_cash, cash_total = _balance_cache['v']
    else:
        value = _get_binance_balance_uncached()
        if value != (0.0, 0.0):
            _balance_cache.update({'t': now, 'v': value, 'error': False})
            free_cash, cash_total = value
        elif _balance_cache['v'] != (0.0, 0.0):
            _balance_cache['error'] = True
            free_cash, cash_total = _balance_cache['v']
        else:
            _balance_cache['error'] = True
            free_cash, cash_total = 0.0, 0.0

    holdings_value = 0.0
    for symbol, pos in list(active_positions.items()):
        if pos.get('simulated'):
            continue
        price, _ = get_spot_mark_price(symbol, pos.get('entry_price'))
        holdings_value += float(pos.get('amount') or 0.0) * price
    return free_cash, cash_total + holdings_value


def _get_binance_balance_uncached():
    try:
        balance = retry_api_read(exchange.fetch_balance)
        free_usdt = float(balance.get('free', {}).get('USDT', 0.0) or 0.0)
        free_usdc = float(balance.get('free', {}).get('USDC', 0.0) or 0.0)
        total_usdt = float(balance.get('total', {}).get('USDT', 0.0) or 0.0) + float(balance.get('total', {}).get('USDC', 0.0) or 0.0)
        if 'info' in balance and 'balances' in balance['info']:
            total_usdt = 0.0
            for item in balance['info']['balances']:
                asset = item.get('asset')
                free_val = float(item.get('free', 0.0))
                locked_val = float(item.get('locked', 0.0))
                if asset == 'USDT':
                    free_usdt = free_val
                    total_usdt += free_val + locked_val
                elif asset == 'USDC':
                    free_usdc = free_val
                    total_usdt += free_val + locked_val
        available_cash = free_usdt if free_usdt > 0 else free_usdc
        return available_cash, total_usdt if total_usdt > 0 else available_cash
    except Exception as exc:
        print("Balance Fetch Error details: {}".format(type(exc).__name__))
        return 0.0, 0.0


# =====================================================================
def portfolio_risk_snapshot():
    """Return managed open risk and combined Demo equity across venues."""
    risks = []
    for symbol, pos in list(active_positions.items()):
        entry = float(pos.get('entry_price') or 0.0)
        distance = float(pos.get('risk_distance') or 0.0)
        if distance <= 0 and pos.get('stop_price') is not None:
            distance = abs(entry - float(pos.get('stop_price') or entry))
        if distance <= 0:
            distance = entry * RISK_PER_TRADE_PCT / 100.0
        if not pos.get('simulated'):
            risks.append({'symbol': symbol, 'market': 'Spot',
                          'risk_usdt': max(0.0, float(pos.get('amount') or 0.0) * distance)})

    engine = globals().get('futures_engine')
    futures_equity = 0.0
    if engine is not None:
        try:
            account = engine.balance(ttl=10)
            if account.get('error'):
                raise RuntimeError('Futures balance unavailable')
            futures_equity = float(account.get('total') or 0.0)
            for symbol, pos in list(engine.positions.items()):
                entry = float(pos.get('entry_price') or 0.0)
                distance = float(pos.get('risk_distance') or 0.0)
                if distance <= 0 and pos.get('stop_price') is not None:
                    distance = abs(entry - float(pos.get('stop_price') or entry))
                if distance <= 0:
                    distance = entry * RISK_PER_TRADE_PCT / 100.0
                multiplier = float(engine.exchange.market(symbol).get('contractSize') or 1.0)
                risks.append({'symbol': symbol, 'market': 'Futures Demo',
                              'risk_usdt': max(0.0, float(pos.get('contracts') or 0.0)
                                               * multiplier * distance)})
        except Exception:
            logger.debug("Suppressed error in %s", "portfolio_risk_snapshot", exc_info=True)
            return {'open_risks': risks, 'equity': 0.0, 'available': False}

    try:
        _, spot_equity = get_binance_balance(ttl=10)
        available = (not bool(_balance_cache.get('error'))
                     and float(spot_equity or 0.0) > 0)
        if engine is not None:
            available = available and futures_equity > 0
        return {'open_risks': risks, 'equity': float(spot_equity or 0.0) + futures_equity,
                'available': available}
    except Exception:
        logger.debug("Suppressed error in %s", "portfolio_risk_snapshot", exc_info=True)
        return {'open_risks': risks, 'equity': 0.0, 'available': False}


def _fees_in(order, currency):
    total = 0.0
    fees = order.get('fees') or ([order['fee']] if order.get('fee') else [])
    for f in fees:
        if f and f.get('currency') == currency and f.get('cost'):
            total += float(f['cost'])
    return total


def _fees_quote_equivalent(order, symbol, price):
    """Return commission value in quote currency and whether the result is exact."""
    base, quote = symbol.split('/')
    fee_items = (order or {}).get('fees') or ([order['fee']] if order and order.get('fee') else [])
    market = exchange.market(symbol)
    if not fee_items:
        rate = market.get('taker')
        rate = float(rate) if rate is not None else 0.001
        notional = float((order or {}).get('cost') or 0.0)
        if notional <= 0:
            notional = float((order or {}).get('filled') or 0.0) * float(price)
        return notional * max(rate, 0.0), 'estimated'

    total = 0.0
    try:
        for fee in fee_items:
            amount = abs(float(fee.get('cost') or 0.0))
            currency = str(fee.get('currency') or '').upper()
            if amount <= 0:
                continue
            if currency == quote.upper() or (quote.upper() == 'USDT' and currency == 'USDC'):
                total += amount
            elif currency == base.upper():
                # Base-asset commissions are already reflected in the net amount consumed.
                continue
            elif currency:
                conversion_symbol = '{}/{}'.format(currency, quote)
                conversion_price = float(fetch_fresh_ticker(exchange, conversion_symbol)['last'])
                total += amount * conversion_price
            else:
                raise ValueError('Commission currency is missing')
        return total, 'actual'
    except Exception:
        logger.debug("Suppressed error in %s", "_fees_quote_equivalent", exc_info=True)
        rate = market.get('taker')
        rate = float(rate) if rate is not None else 0.001
        notional = float((order or {}).get('cost') or 0.0)
        if notional <= 0:
            notional = float((order or {}).get('filled') or 0.0) * float(price)
        return notional * max(rate, 0.0), 'estimated'


def _complete_order(order, symbol):
    """تأكد أن الأمر نُفّذ بالكامل وفيه بيانات التنفيذ."""
    if order.get('status') != 'closed' or not order.get('cost'):
        time.sleep(1)
        order = retry_api_read(exchange.fetch_order, order['id'], symbol)
    return order


def _parse_fill(order, symbol):
    base, quote = symbol.split('/')
    filled = float(order.get('filled') or 0)
    cost = float(order.get('cost') or 0)
    if filled <= 0 or cost <= 0:
        raise RuntimeError("الأمر لم يُنفَّذ (الحالة: {})".format(order.get('status')))
    avg = float(order.get('average') or (cost / filled))
    return filled, cost, avg, _fees_in(order, base), _fees_in(order, quote)


def place_buy(symbol, ref_price, stop_distance=None):
    """Buy at the configured notional cap, reduced when ATR risk sizing requires it."""
    if TRADING_ENABLED and BINANCE_ENV != 'demo':
        raise RuntimeError('هذا المسار مقفل على Binance Demo ولا يرسل أوامر إلى بيئة حية')
    if not TRADING_ENABLED:
        fee_rate = float((exchange.market(symbol).get('taker') or 0.001))
        simulated_notional = float(TRADE_AMOUNT_USDT)
        estimated_fee = simulated_notional * fee_rate
        return {'price': ref_price, 'amount': simulated_notional / ref_price,
                'cost': simulated_notional + estimated_fee, 'fee_usdt': estimated_fee,
                'fee_quality': 'estimated', 'simulated': True,
                'order_status': 'simulated', 'order_id': None}

    amount_usdt = float(TRADE_AMOUNT_USDT)
    if stop_distance is not None:
        _, total_equity = get_binance_balance(ttl=0)
        amount_usdt = risk_sized_notional(
            total_equity, ref_price, stop_distance, TRADE_AMOUNT_USDT, RISK_PER_TRADE_PCT)

    market = exchange.market(symbol)
    min_cost = ((market.get('limits') or {}).get('cost') or {}).get('min')
    if min_cost and amount_usdt < float(min_cost):
        raise ValueError("حجم الصفقة المحسوب {:.2f} USDT أقل من الحد الأدنى {} USDT".format(amount_usdt, min_cost))

    free_usdt = float(retry_api_read(exchange.fetch_balance).get('free', {}).get('USDT', 0.0) or 0.0)
    expected_fee_rate = float(market.get('taker') if market.get('taker') is not None else 0.001)
    required_usdt = amount_usdt * (1.0 + max(expected_fee_rate, 0.0))
    if free_usdt < required_usdt:
        raise ValueError("الرصيد المتاح {:.2f} USDT أقل من قيمة الصفقة مع هامش العمولة {:.2f} USDT".format(free_usdt, required_usdt))

    amount = float(exchange.amount_to_precision(symbol, amount_usdt / ref_price))
    order = exchange.create_order(symbol, 'market', 'buy', amount)
    order = _complete_order(order, symbol)
    filled, cost, avg, base_fee, _quote_fee = _parse_fill(order, symbol)
    fee_quote, fee_quality = _fees_quote_equivalent(order, symbol, avg)
    return {'price': avg, 'amount': filled - base_fee, 'cost': cost + fee_quote,
            'fee_usdt': fee_quote, 'fee_quality': fee_quality, 'notional_cap': TRADE_AMOUNT_USDT,
            'risk_sized_notional': amount_usdt, 'simulated': False,
            'order_status': order.get('status'), 'order_id': order.get('id')}



def _spot_demo_endpoint_ready():
    urls = exchange.urls.get('api') or {}
    private_url = urls.get('private', '') if isinstance(urls, dict) else str(urls)
    return BINANCE_ENV == 'demo' and 'demo-api.binance.com' in str(private_url)


def _mark_spot_reconciliation_required(symbol, pos, reason):
    pos['reconciliation_required'] = True
    pos['protection_status'] = 'needs_reconciliation'
    active_positions[symbol] = pos
    save_position(symbol, pos)
    log_event('ERR', 'تجميد Spot {}: {}'.format(symbol, reason))
    record_decision('Spot', symbol,
        {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')},
        None, 'مطابقة مطلوبة', reason, order_status='غير مؤكد',
        order_id=pos.get('protective_order_id'))


def _spot_order_stop_price(order):
    info = (order or {}).get('info') or {}
    value = (order or {}).get('stopPrice') or info.get('stopPrice') or info.get('triggerPrice')
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _spot_order_client_id(order):
    info = (order or {}).get('info') or {}
    return ((order or {}).get('clientOrderId') or info.get('clientOrderId')
            or info.get('origClientOrderId') or info.get('newClientOrderId'))


def _clear_spot_protection(pos):
    pos['protective_order_id'] = None
    pos['protective_client_id'] = None
    pos['protective_stop_price'] = 0.0
    pos['protection_status'] = 'not_installed'


def _cancel_spot_protective_stop(symbol, pos):
    order_id = pos.get('protective_order_id')
    if not order_id:
        return True
    if not _spot_demo_endpoint_ready():
        raise RuntimeError('رفض إلغاء وقف Spot: اتصال الحساب ليس Binance Demo')
    order = retry_api_read(exchange.fetch_order, str(order_id), symbol)
    status_value = str(order.get('status') or '').lower()
    filled = float(order.get('filled') or 0.0)
    if filled > 0 or status_value in ('closed', 'filled'):
        _mark_spot_reconciliation_required(symbol, pos,
            'وقف Binance بدأ التنفيذ؛ لن يُرسل أمر سوق حتى تُطابق الكمية المنفذة')
        raise RuntimeError('وقف Spot بدأ التنفيذ؛ أوقف الإغلاق اليدوي للمطابقة')
    if status_value in ('canceled', 'cancelled', 'expired', 'rejected'):
        _clear_spot_protection(pos)
        save_position(symbol, pos)
        return True
    try:
        exchange.cancel_order(str(order_id), symbol)
    except Exception:
        logger.debug("Suppressed error in %s", "_cancel_spot_protective_stop", exc_info=True)
        confirmed = retry_api_read(exchange.fetch_order, str(order_id), symbol)
        confirmed_status = str(confirmed.get('status') or '').lower()
        if confirmed_status not in ('canceled', 'cancelled', 'expired', 'rejected'):
            if float(confirmed.get('filled') or 0.0) > 0:
                _mark_spot_reconciliation_required(symbol, pos,
                    'تزامن تنفيذ الوقف مع محاولة إلغائه؛ يلزم احتساب التنفيذ قبل أي أمر آخر')
            raise RuntimeError('تعذر تأكيد إلغاء وقف Spot؛ لم يُرسل أمر بيع آخر')
    confirmed = retry_api_read(exchange.fetch_order, str(order_id), symbol)
    confirmed_status = str(confirmed.get('status') or '').lower()
    if float(confirmed.get('filled') or 0.0) > 0:
        _mark_spot_reconciliation_required(symbol, pos,
            'نُفذت كمية من وقف Spot أثناء الإلغاء؛ يلزم مطابقة التنفيذ')
        raise RuntimeError('وقف Spot نُفذ جزئيًا أثناء الإلغاء؛ لا تُكرر البيع')
    if confirmed_status not in ('canceled', 'cancelled', 'expired', 'rejected'):
        raise RuntimeError('لم يؤكد Binance إلغاء وقف Spot')
    _clear_spot_protection(pos)
    save_position(symbol, pos)
    return True


def _ensure_spot_protective_stop(symbol, pos, stop_price=None):
    """Maintain one exchange-side market stop for an actual Spot Demo position."""
    if pos.get('simulated'):
        return True
    if not _spot_demo_endpoint_ready():
        raise RuntimeError('وقف الحماية الآلي متاح في Binance Demo فقط؛ لم يُرسل أمر وقف')
    if pos.get('reconciliation_required'):
        raise RuntimeError('مركز Spot مجمّد حتى اكتمال المطابقة')
    desired = float(stop_price or pos.get('stop_price') or 0.0)
    amount = float(pos.get('amount') or 0.0)
    if desired <= 0 or amount <= 0:
        raise ValueError('كمية أو سعر وقف Spot غير صالح')
    amount_text = exchange.amount_to_precision(symbol, amount)
    amount = float(amount_text)
    stop_text = exchange.price_to_precision(symbol, desired)
    desired = float(stop_text)
    old_id = pos.get('protective_order_id')
    if not old_id:
        open_orders = retry_api_read(exchange.fetch_open_orders, symbol)
        bot_orders = [item for item in open_orders
                      if str(_spot_order_client_id(item) or '').startswith('sbot_')]
        if len(bot_orders) > 1:
            _mark_spot_reconciliation_required(symbol, pos,
                'عُثر على أكثر من وقف Spot تابع للبوت؛ يلزم فحصها قبل المتابعة')
            raise RuntimeError('هناك عدة أوامر وقف Spot تابعة للبوت')
        if bot_orders:
            old = bot_orders[0]
            pos['protective_order_id'] = str(old.get('id'))
            pos['protective_client_id'] = _spot_order_client_id(old)
            pos['protective_stop_price'] = _spot_order_stop_price(old)
            save_position(symbol, pos)
            old_id = pos['protective_order_id']
    if old_id:
        order = retry_api_read(exchange.fetch_order, str(old_id), symbol)
        status_value = str(order.get('status') or '').lower()
        filled = float(order.get('filled') or 0.0)
        if filled > 0 or status_value in ('closed', 'filled'):
            _mark_spot_reconciliation_required(symbol, pos,
                'وقف Binance ممتلئ أو بدأ التنفيذ؛ يلزم تسجيل التعبئة قبل إنشاء وقف جديد')
            raise RuntimeError('وقف Spot منفذ أو قيد التنفيذ؛ أوقفت إنشاء وقف مكرر')
        if status_value in ('canceled', 'cancelled', 'expired', 'rejected'):
            _clear_spot_protection(pos)
            save_position(symbol, pos)
        elif status_value in ('open', 'new', 'partially_filled'):
            old_stop = _spot_order_stop_price(order) or float(pos.get('protective_stop_price') or 0.0)
            old_amount = float(order.get('amount') or (order.get('info') or {}).get('origQty') or 0.0)
            # A Spot long stop may only move upward, never farther from the market.
            desired = max(desired, old_stop)
            desired_text = exchange.price_to_precision(symbol, desired)
            if (desired_text == exchange.price_to_precision(symbol, old_stop)
                    and abs(old_amount - amount) <= max(amount * 1e-8, 1e-12)):
                pos['protective_stop_price'] = float(desired_text)
                pos['protection_status'] = 'active'
                save_position(symbol, pos)
                return True
            _cancel_spot_protective_stop(symbol, pos)
        else:
            raise RuntimeError('حالة وقف Spot غير معروفة؛ يلزم مطابقة يدوية')

    client_id = 'sbot_' + secrets.token_hex(10)
    try:
        order = exchange.create_order(symbol, 'market', 'sell', amount, None,
            {'stopLossPrice': desired, 'clientOrderId': client_id})
    except Exception:
        logger.debug("Suppressed error in %s", "_ensure_spot_protective_stop", exc_info=True)
        # The create response may have been lost. Search by the unique client ID;
        # never submit another stop until the first request has been reconciled.
        try:
            orders = retry_api_read(exchange.fetch_open_orders, symbol)
            order = next((item for item in orders if _spot_order_client_id(item) == client_id), None)
        except Exception:
            logger.debug("Suppressed error in %s", "_ensure_spot_protective_stop", exc_info=True)
            order = None
        if not order:
            _mark_spot_reconciliation_required(symbol, pos,
                'رد إنشاء وقف Binance غير مؤكد؛ يلزم فحص الأوامر المفتوحة قبل إعادة المحاولة')
            raise RuntimeError('إنشاء وقف Spot غير مؤكد؛ لم يُعاد إرسال الطلب')
    if not order or not order.get('id'):
        _mark_spot_reconciliation_required(symbol, pos, 'لم يُرجع Binance معرفًا لوقف الحماية')
        raise RuntimeError('لم يؤكد Binance إنشاء وقف Spot')
    status_value = str(order.get('status') or '').lower()
    if status_value not in ('open', 'new', 'partially_filled'):
        if float(order.get('filled') or 0.0) > 0:
            pos['protective_order_id'] = str(order['id'])
            pos['protective_client_id'] = client_id
            _mark_spot_reconciliation_required(symbol, pos,
                'أُطلق وقف Spot فور إنشائه؛ يلزم احتساب التعبئة')
        else:
            _mark_spot_reconciliation_required(symbol, pos,
                'أعاد Binance حالة وقف غير مفتوحة: {}'.format(status_value or 'غير معروفة'))
        raise RuntimeError('وقف Spot ليس مفتوحًا بعد إنشائه')
    pos['protective_order_id'] = str(order['id'])
    pos['protective_client_id'] = client_id
    pos['protective_stop_price'] = float(stop_text)
    pos['protection_status'] = 'active'
    pos['reconciliation_required'] = False
    if not save_position(symbol, pos):
        _mark_spot_reconciliation_required(symbol, pos, 'تعذر حفظ معرف وقف Binance في قاعدة البيانات')
        raise RuntimeError('وقف الحماية أُرسل لكن تعذر حفظ مرجعه محليًا')
    return True


def _poll_spot_protective_stop(symbol, pos):
    """Return an accounted sale when a Binance Demo stop has filled."""
    order_id = pos.get('protective_order_id')
    if not order_id:
        return None
    if not _spot_demo_endpoint_ready():
        _mark_spot_reconciliation_required(symbol, pos,
            'المركز يحتوي وقفًا محفوظًا لكن إعداد الحساب لم يعد Binance Demo')
        raise RuntimeError('تعذر التحقق من وقف Spot المحفوظ')
    order = retry_api_read(exchange.fetch_order, str(order_id), symbol)
    status_value = str(order.get('status') or '').lower()
    filled = float(order.get('filled') or 0.0)
    if filled <= 0 and status_value in ('open', 'new', 'partially_filled'):
        return None
    if filled <= 0 and status_value in ('canceled', 'cancelled', 'expired', 'rejected'):
        _clear_spot_protection(pos)
        save_position(symbol, pos)
        _ensure_spot_protective_stop(symbol, pos, pos.get('stop_price'))
        return None
    if filled <= 0 and status_value not in ('closed', 'filled'):
        _mark_spot_reconciliation_required(symbol, pos, 'حالة وقف Binance غير معروفة: {}'.format(status_value))
        raise RuntimeError('حالة وقف Spot غير مؤكدة')
    if filled > 0 and status_value in ('open', 'new', 'partially_filled'):
        try:
            exchange.cancel_order(str(order_id), symbol)
        except Exception:
            logger.debug("Suppressed error in %s", "_poll_spot_protective_stop", exc_info=True)
            pass
        order = retry_api_read(exchange.fetch_order, str(order_id), symbol)
        status_value = str(order.get('status') or '').lower()
        filled = float(order.get('filled') or 0.0)
        if status_value in ('open', 'new', 'partially_filled'):
            _mark_spot_reconciliation_required(symbol, pos,
                'تعذر إيقاف باقي كمية أمر الوقف بعد تنفيذ جزئي')
            raise RuntimeError('وقف Spot ما زال نشطًا بعد تعبئة جزئية')
    if filled <= 0:
        return None
    avg = float(order.get('average') or pos.get('protective_stop_price') or pos['entry_price'])
    cost = float(order.get('cost') or (filled * avg))
    if cost <= 0:
        _mark_spot_reconciliation_required(symbol, pos, 'بيانات تنفيذ وقف Binance لا تحتوي قيمة قابلة للحساب')
        raise RuntimeError('بيانات تنفيذ وقف Spot غير مكتملة')
    fee_quote, fee_quality = _fees_quote_equivalent(order, symbol, avg)
    base = symbol.split('/')[0]
    base_fee = _fees_in(order, base)
    consumed = min(filled + base_fee, float(pos.get('amount') or 0.0))
    _clear_spot_protection(pos)
    # Persist an in-flight marker before applying the fill to local inventory.
    # A crash at this point will freeze the row for reconciliation, never duplicate a sale.
    pos['exit_attempted'] = True
    pos['reconciliation_required'] = False
    if not save_position(symbol, pos):
        _mark_spot_reconciliation_required(symbol, pos, 'تعذر حفظ حالة التعبئة المؤكدة لوقف Binance')
        raise RuntimeError('تعذر حفظ تعبئة وقف Spot')
    return {'price': avg, 'proceeds': cost - fee_quote, 'filled': consumed,
            'fee_usdt': fee_quote, 'fee_quality': fee_quality, 'order_status': status_value or 'closed',
            'order_id': order.get('id')}


def _reconcile_spot_positions():
    """Verify persisted Spot Demo inventory and stop orders after a process restart."""
    if not active_positions:
        return True
    okay = True
    for symbol, pos in list(active_positions.items()):
        if pos.get('simulated'):
            continue
        try:
            if pos.get('partial_attempted') or pos.get('exit_attempted'):
                _mark_spot_reconciliation_required(symbol, pos,
                    'يوجد أمر بيع جزئي أو إغلاق سابق غير محسوم')
                okay = False
                continue
            if not _spot_demo_endpoint_ready():
                _mark_spot_reconciliation_required(symbol, pos,
                    'إعداد الاتصال لا يشير إلى Binance Demo؛ لم تُعدّل أوامر الحساب')
                okay = False
                continue
            if pos.get('protective_order_id'):
                # A stop may have filled while the service was down, reducing inventory.
                order = retry_api_read(exchange.fetch_order, str(pos['protective_order_id']), symbol)
                status_value = str(order.get('status') or '').lower()
                if float(order.get('filled') or 0.0) > 0:
                    continue
                if status_value not in ('open', 'new', 'partially_filled'):
                    if status_value in ('canceled', 'cancelled', 'expired', 'rejected'):
                        _clear_spot_protection(pos)
                    else:
                        _mark_spot_reconciliation_required(symbol, pos,
                            'تعذر العثور على وقف الحماية المحفوظ في Binance')
                        okay = False
                        continue
            base = symbol.split('/')[0]
            balances = retry_api_read(exchange.fetch_balance)
            inventory = float(balances.get('total', {}).get(base)
                              or (float(balances.get('free', {}).get(base) or 0.0)
                                  + float(balances.get('used', {}).get(base) or 0.0)))
            tracked = float(pos.get('amount') or 0.0)
            tolerance = max(float(exchange.amount_to_precision(symbol, max(tracked, 1e-12))) * 1e-6, 1e-12)
            if inventory + tolerance < tracked:
                _mark_spot_reconciliation_required(symbol, pos,
                    'رصيد Binance من {} ({:.8g}) أقل من الكمية المحفوظة ({:.8g})'.format(base, inventory, tracked))
                okay = False
                continue
            pos['reconciliation_required'] = False
            active_positions[symbol] = pos
            _ensure_spot_protective_stop(symbol, pos, pos.get('protective_stop_price') or pos.get('stop_price'))
            log_event('INFO', 'تمت مطابقة Spot Demo {} والوقف بعد التشغيل'.format(symbol))
        except Exception as exc:
            _mark_spot_reconciliation_required(symbol, pos,
                'فشلت مطابقة الرصيد أو وقف الحماية ({})'.format(type(exc).__name__))
            okay = False
    return okay



def _check_sized_spot_risk(symbol, price, stop_distance):
    _, equity = get_binance_balance(ttl=10)
    notional = risk_sized_notional(equity, price, stop_distance,
                                   TRADE_AMOUNT_USDT, RISK_PER_TRADE_PCT)
    risk_amount = notional * float(stop_distance) / max(float(price), 1e-12)
    result = _check_combined_portfolio_risk(symbol, 'Spot', risk_amount)
    result['candidate_notional_usdt'] = notional
    return result


def _check_combined_portfolio_risk(symbol, market, risk_usdt):
    snapshot = portfolio_risk_snapshot()
    if not snapshot.get('available'):
        return {'allowed': False, 'reason': 'رصيد إحدى محافظ Demo غير متاح؛ أوقف حاجز المخاطر المجمعة'}
    return portfolio_risk_status(
        snapshot.get('open_risks'), {'symbol': symbol, 'market': market, 'risk_usdt': risk_usdt},
        snapshot.get('equity'), MAX_PORTFOLIO_OPEN_RISK_PCT, MAX_CORRELATED_CLUSTER_RISK_PCT)


def place_sell(symbol, pos, amount_to_sell=None):
    """Sell a requested tranche once; order creation is never retried."""
    requested = min(float(pos['amount']), float(amount_to_sell or pos['amount']))
    if pos.get('simulated'):
        price = float(fetch_fresh_ticker(exchange, symbol)['last'])
        fee_rate = float((exchange.market(symbol).get('taker') or 0.001))
        estimated_fee = requested * price * fee_rate
        return {'price': price, 'proceeds': requested * price - estimated_fee,
                'filled': requested, 'fee_usdt': estimated_fee, 'fee_quality': 'estimated',
                'order_status': 'simulated', 'order_id': None}

    if BINANCE_ENV != 'demo':
        raise RuntimeError('إغلاق أوامر Spot الآلي مقفل على Binance Demo')
    if not TRADING_ENABLED:
        raise RuntimeError('تنفيذ الأوامر معطل؛ لن تُسوى صفقة Spot حقيقية على أنها محاكاة')
    if pos.get('protective_order_id'):
        _cancel_spot_protective_stop(symbol, pos)
    base = symbol.split('/')[0]
    free_base = float(retry_api_read(exchange.fetch_balance).get('free', {}).get(base, 0.0) or 0.0)
    amount = float(exchange.amount_to_precision(symbol, min(requested, free_base)))
    if amount <= 0:
        raise ValueError("لا يوجد رصيد كافٍ من {} للبيع".format(base))
    try:
        order = exchange.create_order(symbol, 'market', 'sell', amount)
        order = _complete_order(order, symbol)
    except Exception as exc:
        _mark_spot_reconciliation_required(symbol, pos,
            'نتيجة أمر البيع غير مؤكدة بعد إلغاء وقف الحماية ({})'.format(type(exc).__name__))
        raise RuntimeError('أمر بيع Spot غير مؤكد؛ لا تعِد الإرسال قبل المطابقة')
    filled, cost, avg, base_fee, _quote_fee = _parse_fill(order, symbol)
    fee_quote, fee_quality = _fees_quote_equivalent(order, symbol, avg)
    # Credit proceeds only for the quantity Binance actually filled. Any residual
    # inventory remains in the position and is managed on the next scan.
    proceeds = cost - fee_quote
    return {'price': avg, 'proceeds': proceeds, 'filled': min(filled + base_fee, requested),
            'fee_usdt': fee_quote, 'fee_quality': fee_quality,
            'order_status': order.get('status'), 'order_id': order.get('id')}


# =====================================================================
# 4b. فلتر الأخبار (RSS + Groq)
# =====================================================================
news_state = {
    'sentiment': 'neutral', 'severity': 0, 'reason': 'لم يُفحص بعد', 'source': '',
    'checked_at': None, 'checked_epoch': 0.0, 'blocked_until': 0.0, 'headlines': [], 'headlines_ar': [], 'alerted': False, 'error': ''
}

STRONG_NEGATIVE_WORDS = [
    "hack", "hacked", "exploit", "exploited", "stolen", "theft", "ban", "bans", "banned", "lawsuit",
    "sues", "sued", "bankrupt", "bankruptcy", "insolvent", "insolvency", "collapse", "depeg", "seized",
    "arrest", "arrested", "fraud", "crackdown", "halts withdrawals", "suspends withdrawals"
]


class _FollowRedirects(urllib.request.HTTPRedirectHandler):
    """Python 3.10 لا يتبع التوجيه 308، وبعض المواقع (مثل CoinDesk) تستعمله"""
    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_302(req, fp, 307, msg, headers)


_opener = urllib.request.build_opener(_FollowRedirects)


def news_blocked():
    return NEWS_FILTER and time.time() < news_state['blocked_until']


def fetch_headlines():
    """يجلب أحدث العناوين من مواقع RSS ضمن نافذة زمنية. يرجع قائمة (وقت, مصدر, عنوان)"""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=NEWS_WINDOW_HOURS)
    items, ok_feeds = [], 0
    for name, url in NEWS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (crypto-bot)'})
            data = _opener.open(req, timeout=15).read()
            root = ET.fromstring(data)
            ok_feeds += 1
            for it in root.iter('item'):
                title = (it.findtext('title') or '').strip()
                if not title:
                    continue
                pub = it.findtext('pubDate')
                try:
                    ts = parsedate_to_datetime(pub) if pub else None
                    if ts is not None and ts.tzinfo is None:
                        ts = ts.replace(tzinfo=datetime.timezone.utc)
                except Exception:
                    logger.debug("Suppressed error in %s", "fetch_headlines", exc_info=True)
                    ts = None
                if ts is not None and ts < cutoff:
                    continue
                items.append((ts or datetime.datetime.now(datetime.timezone.utc), name, title))
        except Exception as e:
            print("📰 [NEWS] فشل جلب {}: {}".format(name, e))
    if ok_feeds == 0:
        raise RuntimeError("تعذر جلب أي مصدر أخبار")
    items.sort(key=lambda x: x[0], reverse=True)
    return items[:20]


GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_NON_CHAT = ("whisper", "tts", "guard", "embed", "orpheus", "playai", "transcribe", "moderation", "safeguard")
GROQ_PREFERRED = ("llama-3.3-70b", "gpt-oss-120b", "llama-4", "gpt-oss-20b", "qwen", "llama-3.1-70b", "llama-3.1-8b")
_groq_model = {'name': GROQ_MODEL}


def _groq_call(path, body=None):
    req = urllib.request.Request(
        GROQ_BASE + path,
        data=json.dumps(body).encode('utf-8') if body is not None else None,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + GROQ_API_KEY,
                 'User-Agent': 'crypto-bot/1.0'})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))


def groq_pick_model():
    """يختار نموذج دردشة متاحاً لحساب المستخدم من قائمة Groq الحالية"""
    data = _groq_call("/models")
    ids = [m.get('id', '') for m in data.get('data', []) if m.get('active', True)]
    chat = [i for i in ids if i and not any(h in i.lower() for h in GROQ_NON_CHAT)]
    for pref in GROQ_PREFERRED:
        for i in chat:
            if pref in i.lower():
                return i
    if chat:
        return chat[0]
    raise RuntimeError("لا توجد نماذج دردشة متاحة لهذا المفتاح")


def _groq_chat(model, messages, json_mode=True):
    body = {"model": model, "temperature": 0, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    resp = _groq_call("/chat/completions", body)
    return resp['choices'][0]['message']['content']


def classify_with_groq(titles):
    system = (
        "You are a crypto market risk analyst. Given recent crypto news headlines, judge the overall "
        "impact on BTC and ETH prices over the next few hours. Respond ONLY with a JSON object: "
        '{"sentiment":"positive|neutral|negative","severity":1-5,"reason":"one short sentence in Arabic"}. '
        "Use severity 4-5 ONLY for major market-wide negative events such as exchange hacks, major country bans, "
        "large lawsuits or enforcement actions, stablecoin depegs, insolvencies, or protocol exploits affecting BTC or ETH. "
        "Ordinary price drops, opinions, predictions and minor news are severity 1-2."
    )
    user = "Headlines:\n" + "\n".join("{}. {}".format(i + 1, t) for i, t in enumerate(titles))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if not _groq_model['name']:
        _groq_model['name'] = groq_pick_model()
        print("📰 [NEWS] تم اختيار نموذج Groq تلقائياً: {}".format(_groq_model['name']))
    name = _groq_model['name']
    try:
        content = _groq_chat(name, messages, True)
    except urllib.error.HTTPError as e:
        if e.code == 400:      # قد لا يدعم النموذج وضع JSON، نعيد بدونه
            content = _groq_chat(name, messages, False)
        elif e.code == 404 and not GROQ_MODEL:   # النموذج لم يعد متاحاً، نختار غيره
            name = _groq_model['name'] = groq_pick_model()
            print("📰 [NEWS] تغيير نموذج Groq إلى: {}".format(name))
            content = _groq_chat(name, messages, True)
        else:
            raise

    match = re.search(r'\{.*\}', content, re.S)
    result = json.loads(match.group(0) if match else content)
    sentiment = str(result.get('sentiment', 'neutral')).lower()
    if sentiment not in ('positive', 'neutral', 'negative'):
        sentiment = 'neutral'
    severity = max(1, min(5, int(result.get('severity', 1))))
    return sentiment, severity, str(result.get('reason', '')).strip() or 'لا توجد تفاصيل'


def translate_headlines_with_groq(titles):
    """ترجمة عناوين الأخبار منفصلة عن تصنيف المخاطر حتى لا يؤثر تعطل الترجمة على قرار التداول."""
    if not titles or not GROQ_API_KEY:
        return []
    if not _groq_model['name']:
        _groq_model['name'] = groq_pick_model()
    messages = [
        {"role": "system", "content":
         "Translate each crypto news headline into concise Modern Standard Arabic. "
         "Preserve tickers, company names, numbers, and meaning. Return only JSON "
         "with one key: translations, an array with exactly one Arabic translation per input headline in the same order."},
        {"role": "user", "content": "Headlines:\n" + "\n".join("{}. {}".format(i + 1, t) for i, t in enumerate(titles))}
    ]
    name = _groq_model['name']
    try:
        content = _groq_chat(name, messages, True)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            content = _groq_chat(name, messages, False)
        elif e.code == 404 and not GROQ_MODEL:
            name = _groq_model['name'] = groq_pick_model()
            content = _groq_chat(name, messages, True)
        else:
            raise
    match = re.search(r'\{.*\}', content, re.S)
    result = json.loads(match.group(0) if match else content)
    translations = result.get('translations', [])
    if not isinstance(translations, list) or len(translations) != len(titles):
        raise ValueError("عدد الترجمات لا يطابق عدد العناوين")
    translations = [str(value).strip() for value in translations]
    if any(not value for value in translations):
        raise ValueError("إحدى الترجمات فارغة")
    return translations


def classify_with_keywords(titles):
    hits = []
    for t in titles:
        low = t.lower()
        for w in STRONG_NEGATIVE_WORDS:
            if re.search(r'\b' + re.escape(w) + r'\b', low):
                hits.append(t)
                break
    if len(hits) >= 2:
        return 'negative', 4, "عدة عناوين تحتوي كلمات خطر ({} عناوين)".format(len(hits))
    if len(hits) == 1:
        return 'negative', 3, "عنوان واحد يحتوي كلمة خطر (لا يكفي لإيقاف الشراء)"
    return 'neutral', 1, "لا توجد كلمات خطر في العناوين"


def check_news():
    """يُستدعى كل ساعة: يجلب العناوين، يصنّفها، ويحدّد هل يُوقف الشراء."""
    if not NEWS_FILTER:
        return
    try:
        items = fetch_headlines()
        titles = [t for (_, _, t) in items]
        headline_translations = []
        if not titles:
            sentiment, severity, reason, source = 'neutral', 1, "لا توجد أخبار حديثة ضمن النافذة", "-"
        else:
            try:
                if not GROQ_API_KEY:
                    raise RuntimeError("no groq key")
                sentiment, severity, reason = classify_with_groq(titles)
                source = "Groq"
            except Exception as e:
                if GROQ_API_KEY:
                    print("📰 [NEWS] فشل تصنيف Groq ({}), استخدام الكلمات المفتاحية".format(e))
                sentiment, severity, reason = classify_with_keywords(titles)
                source = "Keywords"

        if titles and GROQ_API_KEY:
            try:
                headline_translations = translate_headlines_with_groq(titles)
                print("📰 [NEWS] تمت ترجمة {} عنواناً إلى العربية".format(len(headline_translations)))
            except Exception as e:
                print("📰 [NEWS] تعذرت ترجمة العناوين: {}".format(type(e).__name__))
                headline_translations = []

        strong = (sentiment == 'negative' and severity >= NEWS_BLOCK_SEVERITY)
        if strong:
            news_state['blocked_until'] = max(news_state['blocked_until'], time.time() + NEWS_PAUSE_HOURS * 3600)

        news_state.update({
            'sentiment': sentiment, 'severity': severity, 'reason': reason, 'source': source,
            'checked_at': datetime.datetime.now().strftime("%H:%M"),
            'checked_epoch': time.time(),
            'headlines': [(n, t) for (_, n, t) in items[:5]],
            'headlines_ar': headline_translations[:5], 'error': ''
        })
        print("📰 [NEWS] {} | خطورة {} | {} ({})".format(sentiment, severity, reason, source))
        log_event("NEWS", "{} {}/5 ({})".format(sentiment, severity, source))

        if news_blocked() and not news_state['alerted']:
            news_state['alerted'] = True
            log_event("GATE", "الشراء موقوف بسبب الأخبار")
            top = "\n".join("• " + html.escape(t) for (_, _, t) in items[:3])
            send_telegram_message("📰 <b>إيقاف الشراء الجديد مؤقتاً</b>\n⚠️ {}\n{}\n⏱ حتى {} (الصفقات المفتوحة تبقى كما هي)".format(
                html.escape(reason), top,
                datetime.datetime.fromtimestamp(news_state['blocked_until']).strftime("%H:%M")))
        elif news_state['alerted'] and not news_blocked():
            news_state['alerted'] = False
            log_event("GATE", "عاد الشراء للعمل")
            send_telegram_message("📰 <b>عاد الشراء الجديد للعمل</b>\nانتهت مدة الإيقاف ولا توجد أخبار سلبية قوية الآن.")
    except Exception as e:
        news_state['error'] = str(e)
        logger.error("📰 [NEWS] خطأ: %s", e)


# =====================================================================
# 5. إدارة الصفقات والفحص الدوري
# =====================================================================
def check_and_close_positions():
    monitor_ok = True
    for symbol in list(active_positions.keys()):
        current_price, sale = None, None
        try:
            pos = active_positions[symbol]
            if pos.get('reconciliation_required') and pos.get('protective_order_id'):
                # If the stop race was the only ambiguity, recover its confirmed fill first.
                sale = _poll_spot_protective_stop(symbol, pos)
                if sale is not None:
                    pos['reconciliation_required'] = False
            if pos.get('reconciliation_required'):
                monitor_ok = False
                record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, None,
                                'تجميد متابعة المركز', 'المركز يحتاج مطابقة مع Binance قبل أي أمر جديد', order_status='غير مؤكد')
                continue
            if pos.get('partial_attempted') and not pos.get('partial_taken'):
                monitor_ok = False
                record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, None, 'تجميد متابعة المركز', 'أمر بيع جزئي غير مؤكد؛ يلزم مطابقته مع Binance قبل المتابعة', order_status='غير مؤكد')
                continue  # ambiguous tranche order: freeze automated entries and exits until reconciled
            if pos.get('exit_attempted') and sale is None:
                monitor_ok = False
                record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, None, 'تجميد متابعة المركز', 'حالة أمر الإغلاق السابق غير مؤكدة؛ طابق سجل Binance قبل أي أمر آخر', order_status='غير مؤكد')
                continue
            current_price = fetch_fresh_ticker(exchange, symbol)['last']
            entry_price = pos['entry_price']
            if pos.get('protective_order_id') and not pos.get('simulated'):
                sale = _poll_spot_protective_stop(symbol, pos)

            highest_price = max(pos.get('highest_price', entry_price), current_price)
            if highest_price != pos.get('highest_price'):
                pos['highest_price'] = highest_price
                save_position(symbol, pos)

            target = pos.get('target_price')
            initial_stop = pos.get('stop_price') or entry_price * (1 - INITIAL_SL_PCT / 100)
            if not pos.get('partial_taken') and target is None:
                target = r_multiple_target(entry_price, initial_stop)
            trailing_distance = float(pos.get('trailing_distance') or entry_price * TRAILING_SL_PCT / 100)
            trail_armed = (bool(pos.get('partial_taken'))
                           and highest_price >= entry_price * (1 + TRAILING_ACTIVATION_PCT / 100))
            active_stop = max(initial_stop, highest_price - trailing_distance) if trail_armed else initial_stop

            reason = 'SL' if sale is not None else None
            if reason is None and pos.get('test'):
                opened = datetime.datetime.strptime(pos['time'], "%Y-%m-%d %H:%M:%S")
                if (datetime.datetime.now() - opened).total_seconds() >= TEST_TRADE_SECONDS:
                    reason = "TEST"
            elif reason is None and not pos.get('partial_taken') and target is not None and current_price >= target:
                reason = "TP"
            elif reason is None and current_price <= active_stop:
                reason = "SL"

            if not reason and not pos.get('test') and not pos.get('partial_taken') and not pos.get('partial_attempted'):
                risk_distance = float(pos.get('risk_distance') or max(entry_price - initial_stop, entry_price * 0.0005))
                if risk_distance > 0 and current_price >= entry_price + risk_distance:
                    try:
                        tranche = float(pos['amount']) * 0.5
                        min_cost = float((((exchange.market(symbol).get('limits') or {}).get('cost') or {}).get('min')) or 0.0)
                        if min_cost and tranche * float(current_price) < min_cost:
                            pos['partial_attempted'] = True
                            pos['partial_taken'] = True  # no partial is possible below exchange minimum
                            pos['target_price'] = None
                            pos['stop_price'] = max(float(pos.get('stop_price') or 0.0), entry_price)
                            save_position(symbol, pos)
                            if not pos.get('simulated'):
                                _ensure_spot_protective_stop(symbol, pos, pos['stop_price'])
                            log_event("GATE", "تخطي الجني الجزئي {}: نصف المركز دون الحد الأدنى؛ متابعة الوقف فقط".format(symbol))
                            continue
                        pos['partial_attempted'] = True
                        if not save_position(symbol, pos):
                            raise RuntimeError("تعذر حفظ حالة محاولة البيع الجزئي؛ لم يُرسل الأمر")
                        partial_sale = place_sell(symbol, pos, tranche)
                        record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, current_price, 'تم تنفيذ جني جزئي', 'بيع نصف المركز عند 1R', partial_sale.get('order_status'), partial_sale.get('order_id'))
                        sold = float(partial_sale.get('filled') or 0.0)
                        if sold <= 0:
                            raise RuntimeError("أمر البيع الجزئي لم يؤكد كمية منفذة")
                        if sold > 0:
                            cost_sold = float(pos['cost']) * min(sold / float(pos['amount']), 1.0)
                            partial_pnl = float(partial_sale['proceeds']) - cost_sold
                            pos['amount'] = max(float(pos['amount']) - sold, 0.0)
                            pos['cost'] = max(float(pos['cost']) - cost_sold, 0.0)
                            pos['partial_pnl_usdt'] = float(pos.get('partial_pnl_usdt') or 0.0) + partial_pnl
                            pos['fees_usdt'] = float(pos.get('fees_usdt') or 0.0) + float(partial_sale.get('fee_usdt') or 0.0)
                            pos['partial_taken'] = True
                            pos['partial_attempted'] = False
                            if partial_sale.get('fee_quality') != 'actual':
                                pos['fee_quality'] = partial_sale.get('fee_quality', 'unverified')
                            pos['target_price'] = None  # let the remaining half run under the trailing stop
                            pos['stop_price'] = max(float(pos.get('stop_price') or 0.0), entry_price)
                            active_positions[symbol] = pos
                            if not save_position(symbol, pos):
                                monitor_ok = False
                                raise RuntimeError("تعذر حفظ الرصيد المتبقي بعد البيع الجزئي")
                            if not pos.get('simulated'):
                                _ensure_spot_protective_stop(symbol, pos, pos['stop_price'])
                            log_event("PARTIAL", "{} · بيع 50% عند 1R · {:+.2f} USDT · الوقف للتعادل".format(symbol, partial_pnl))
                            send_telegram_message("✅ <b>جني جزئي Spot عند 1R</b>\n🪙 {}\n💰 المحقق: {:+.2f} USDT\n🔒 تم رفع الوقف إلى نقطة الدخول".format(symbol, partial_pnl))
                            continue
                    except Exception as exc:
                        monitor_ok = False
                        record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, current_price, 'فشل أو لم يتأكد أمر الجني الجزئي', 'راجع سجل Binance قبل أي إعادة', order_status='غير مؤكد', api_message=exc)
                        log_event("ERR", "تعذر الجني الجزئي {}: {}".format(symbol, type(exc).__name__))
                        continue

            if not reason:
                continue

            try:
                if sale is None:
                    pos['exit_attempted'] = True
                    if not save_position(symbol, pos):
                        pos['exit_attempted'] = False
                        raise RuntimeError("تعذر حفظ حالة الإغلاق؛ لم يُرسل الأمر")
                    sale = place_sell(symbol, pos)
                    record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, current_price, 'تم تنفيذ الإغلاق', reason, sale.get('order_status'), sale.get('order_id'))
                else:
                    pos['exit_attempted'] = False
                    save_position(symbol, pos)
                    record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, current_price, 'تم تنفيذ وقف Binance Demo', 'وقف حماية Spot أُطلق خارج البوت', sale.get('order_status'), sale.get('order_id'))
            except Exception as e:
                record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, current_price, 'فشل أو لم يتأكد أمر البيع', 'راجع سجل Binance قبل أي إعادة', order_status='غير مؤكد', api_message=e)
                print("❌ [SELL ERROR] {}: {}".format(symbol, e))
                log_event("ERR", "بيع {}: {}".format(symbol, str(e)[:80]))
                if not pos.get('sell_error_notified'):
                    pos['sell_error_notified'] = True
                    send_telegram_message("❌ <b>فشل بيع {}</b>\n{}".format(symbol, html.escape(str(e))))
                continue

            old_amount = float(pos.get('amount') or 0.0)
            sold = min(float(sale.get('filled') or 0.0), old_amount)
            if old_amount <= 0 or sold <= 0:
                raise RuntimeError("أمر البيع لم يستهلك كمية قابلة للمطابقة")
            cost_sold = float(pos.get('cost') or 0.0) * sold / old_amount
            tranche_pnl = float(sale.get('proceeds') or 0.0) - cost_sold
            pos['fees_usdt'] = float(pos.get('fees_usdt') or 0.0) + float(sale.get('fee_usdt') or 0.0)
            remaining_amount = max(old_amount - sold, 0.0)
            pos['amount'] = remaining_amount
            pos['cost'] = max(float(pos.get('cost') or 0.0) - cost_sold, 0.0)
            pos['partial_pnl_usdt'] = float(pos.get('partial_pnl_usdt') or 0.0) + tranche_pnl
            if sale.get('fee_quality') != 'actual':
                pos['fee_quality'] = sale.get('fee_quality', 'unverified')

            # A market order can fill less than requested. Preserve the unsold
            # amount instead of recording a full close or crediting phantom proceeds.
            precision_amount = float(exchange.amount_to_precision(symbol, max(old_amount, 1e-12)))
            dust_tolerance = max(precision_amount * 1e-8, 1e-12)
            if remaining_amount > dust_tolerance:
                pos['exit_attempted'] = False
                active_positions[symbol] = pos
                save_position(symbol, pos)
                if not pos.get('simulated'):
                    _ensure_spot_protective_stop(symbol, pos, pos.get('stop_price'))
                record_decision('Spot', symbol,
                    {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')},
                    current_price, 'إغلاق جزئي؛ المركز ما زال مفتوحًا',
                    'نُفذت كمية أقل من كامل المركز؛ سيبقى المتبقي محفوظًا ومراقبًا',
                    sale.get('order_status'), sale.get('order_id'))
                log_event('WARN', 'إغلاق {} جزئيًا؛ المتبقي {:.8g} {}'.format(
                    symbol, remaining_amount, symbol.split('/')[0]))
                continue

            total_pnl = float(pos.get('partial_pnl_usdt') or 0.0)
            pnl_basis = float(pos.get('initial_cost') or (cost_sold + pos['cost']) or 0.0)
            realized_pnl = (total_pnl / pnl_basis * 100.0) if pnl_basis else 0.0
            status = "WIN" if total_pnl > 0 else "LOSS"
            title = {"TP": "🎯 تم تحقيق الهدف", "TEST": "🧪 إغلاق صفقة الاختبار"}.get(reason, "🛑 إغلاق بوقف الخسارة")
            print("{} [{}] {} | PnL: {:+.2f}% | {} ({})".format(
                title, status, symbol, realized_pnl, pos.get('strategy', 'trend'), pos.get('timeframe', '1h')))
            log_event({"TP": "TP", "TEST": "TEST"}.get(reason, "SL"),
                      "{} {:+.2f}% · {}".format(symbol, realized_pnl, pos.get('strategy', 'trend')))
            fees_total = float(pos.get('fees_usdt') or 0.0)
            exit_reason = spot_exit_reason(reason, pos.get('partial_taken'), trail_armed)
            record_closed_spot_trade(symbol, entry_price, sale['price'], realized_pnl, status,
                                      total_pnl, pos.get('strategy'), pos.get('timeframe'),
                                      ('simulated' if pos.get('simulated') else pos.get('fee_quality', 'unverified')),
                                      gross_pnl_usdt=total_pnl + fees_total, fees_usdt=fees_total,
                                      exit_reason=exit_reason)
            send_telegram_message(
                "{} ({})\n🪙 العملة: {}\n📥 سعر الدخول: {}\n📤 سعر الخروج: {}\n📊 النتيجة (بعد العمولة): {:+.2f}%\n🧭 الاستراتيجية: {} · {}{}".format(
                    title, status, symbol, entry_price, sale['price'], realized_pnl,
                    pos.get('strategy', 'trend'), pos.get('timeframe', '1h'),
                    "\n🧪 محاكاة" if (not TRADING_ENABLED or pos.get('simulated')) else ""))
            del active_positions[symbol]
        except Exception as e:
            monitor_ok = False
            record_decision('Spot', symbol, {'signal': False, 'strategy': pos.get('strategy'), 'timeframe': pos.get('timeframe')}, current_price, 'خطأ في متابعة المركز', 'تعذر تحديث السعر أو تنفيذ إدارة المركز', order_status='غير مؤكد' if sale is not None else 'لم يُرسل', api_message=e)
            log_event('ERR', 'متابعة Spot {}: {}'.format(symbol, type(e).__name__))
            print("Error in position monitoring ({}): {}".format(symbol, e))
    return monitor_ok


def automated_market_scan():
    if not scan_lock.acquire(blocking=False):
        print("⏳ [SCANNER] فحص آخر قيد التنفيذ، تم التخطي")
        return
    try:
        scan_started = time.time()
        scanner_state.update({
            'status': 'running',
            'last_started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'last_error': None,
        })
        scan_succeeded = False
        print("🔄 [SCANNER] فحص الاستراتيجيات الفنية والهجينة ({} | Demo-only safety)...".format(BINANCE_ENV))
        position_monitor_ok = check_and_close_positions()
        try:
            _, spot_equity = get_binance_balance(ttl=0)
            update_daily_risk_state('spot', daily_loss_status(DB_NAME, 'spot', spot_equity, MAX_DAILY_LOSS_PCT))
        except Exception as exc:
            update_daily_risk_state('spot', {'market': 'spot', 'blocked': True,
                'equity': 0.0, 'baseline': None, 'loss_pct': None,
                'limit_pct': MAX_DAILY_LOSS_PCT,
                'reason': 'تعذر التحقق من حد الخسارة اليومية؛ أُوقف فتح الصفقات ({})'.format(type(exc).__name__)})

        for symbol in SYMBOLS:
            setup, current_price, price_updated_at = None, None, None
            try:
                if symbol not in SUPPORTED_SYMBOLS:
                    record_decision('Spot', symbol, outcome='مرفوض', reason='الزوج غير مدعوم في الإعداد الحالي')
                    continue
                setup = evaluate_symbol(exchange, symbol, context=strategy_news_context(), enable_hybrids=(BINANCE_ENV == 'demo'))
                current_price = float(fetch_fresh_ticker(exchange, symbol)['last'])
                price_updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if not setup:
                    record_decision('Spot', symbol, outcome='تعذر التقييم',
                                    reason='لم تكتمل بيانات المؤشرات لتكوين إشارة', price=current_price,
                                    price_updated_at=price_updated_at)
                    continue
                setup = dict(setup)
                setup.update({'price': current_price, 'time': datetime.datetime.now().strftime("%H:%M:%S"),
                               'updated_at': time.time(), 'price_updated_at': price_updated_at})
                market_snapshot[symbol] = setup
                if setup.get('signal') and not position_monitor_ok:
                    record_decision('Spot', symbol, setup, current_price, 'مرفوض',
                                    'أُوقف الدخول لأن متابعة مركز Spot مفتوح واجهت خطأ',
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                if (setup.get('signal') and daily_risk_state['spot']
                        and daily_risk_state['spot'].get('blocked')):
                    record_decision('Spot', symbol, setup, current_price, 'مرفوض',
                                    daily_risk_state['spot'].get('reason', 'بلغ حد الخسارة اليومي'),
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                print("📊 {} | {} {} | RSI: {:.2f} | strength: {}/100 | setup: {}".format(
                    symbol, setup['strategy'], setup['timeframe'], setup['rsi'],
                    setup.get('signal_strength', 0), setup['reason']))

                spot_open_slots = sum(max(1, int(p.get('entries', 1))) for p in active_positions.values())
                spot_open_slots += sum(1 for p in active_positions.values()
                                       if p.get('add_attempted') and int(p.get('entries', 1)) < 2)
                if setup.get('signal') and spot_open_slots >= MAX_SPOT_CONCURRENT_TRADES:
                    record_decision('Spot', symbol, setup, current_price, 'مرفوض',
                                    'بلغت المراكز حد الصفقات المفتوحة في Spot',
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue

                global test_trade_done
                if FORCE_TEST_TRADE and not test_trade_done and symbol not in active_positions:
                    test_trade_done = True
                    try:
                        test_stop_distance = current_price * INITIAL_SL_PCT / 100.0
                        if TRADING_ENABLED:
                            risk_check = _check_sized_spot_risk(symbol, current_price, test_stop_distance)
                            if not risk_check.get('allowed'):
                                record_decision('Spot', symbol, setup, current_price, 'اختبار مرفوض',
                                                risk_check.get('reason'), order_status='لم يُرسل',
                                                price_updated_at=price_updated_at)
                                continue
                        fill = place_buy(symbol, current_price, test_stop_distance)
                        pos = {
                            'entry_price': fill['price'], 'amount': fill['amount'],
                            'cost': fill['cost'], 'highest_price': fill['price'],
                            'time': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'simulated': fill['simulated'], 'fee_quality': fill.get('fee_quality', 'unverified'),
                            'fees_usdt': float(fill.get('fee_usdt') or 0.0), 'test': True,
                            'strategy': 'test', 'timeframe': setup.get('timeframe', '15m'),
                            'stop_price': fill['price'] - test_stop_distance,
                            'risk_distance': test_stop_distance,
                        }
                        active_positions[symbol] = pos
                        save_position(symbol, pos)
                        if not pos.get('simulated'):
                            _ensure_spot_protective_stop(symbol, pos, pos['stop_price'])
                        record_decision('Spot', symbol, setup, current_price, 'صفقة اختبار فُتحت',
                                        'فتح اختباري مفعّل يدويًا', fill.get('order_status'),
                                        fill.get('order_id'), price_updated_at=price_updated_at)
                        print("🧪 [TEST BUY] {} بسعر {}".format(symbol, fill['price']))
                        send_telegram_message("🧪 <b>صفقة اختبار (BUY)</b>\n🪙 {}\n💵 {}\n⏱ ستُغلق بعد {} ثانية".format(
                            symbol, fill['price'], TEST_TRADE_SECONDS))
                    except Exception as e:
                        record_decision('Spot', symbol, setup, current_price, 'خطأ أثناء إرسال أمر الاختبار',
                                        'راجع رسالة API', order_status='غير مؤكد', api_message=e,
                                        price_updated_at=price_updated_at)
                        print("❌ [TEST BUY ERROR] {}: {}".format(symbol, e))
                        send_telegram_message("❌ <b>فشل شراء الاختبار</b>\n{}".format(html.escape(str(e))))
                    continue

                if not setup['signal']:
                    record_decision('Spot', symbol, setup, current_price, 'مراقبة',
                                    setup.get('reason') or 'لا توجد إشارة دخول',
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                liquid, liquidity_reason = market_liquidity_check(exchange, symbol, "Spot")
                if not liquid:
                    log_event("GATE", "تخطي Spot {}: {}".format(symbol, liquidity_reason))
                    record_decision('Spot', symbol, setup, current_price, 'مرفوض', liquidity_reason,
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                candle = setup.get('candle_ts')
                if last_entry_candle.get(symbol) == candle:
                    record_decision('Spot', symbol, setup, current_price, 'تجاهل مكرر',
                                    'سبق فحص أو تنفيذ إشارة هذه الشمعة',
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue

                if news_blocked():
                    print("📰 [NEWS] إشارة {} على {} لكن الشراء موقوف بسبب الأخبار".format(
                        setup['strategy'], symbol))
                    record_decision('Spot', symbol, setup, current_price, 'مرفوض',
                                    news_state.get('reason') or 'حظر مرشح الأخبار',
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue
                if (news_state['sentiment'] == 'negative' and news_state['severity'] >= 2
                        and setup['strategy'] != 'trend'):
                    print("📰 [NEWS] خبر سلبي؛ السماح فقط بإشارات الاتجاه وتجاهل {} على {}".format(
                        setup['strategy'], symbol))
                    record_decision('Spot', symbol, setup, current_price, 'مرفوض',
                                    'خبر سلبي؛ يسمح النظام بإشارات الاتجاه فقط',
                                    order_status='لم يُرسل', price_updated_at=price_updated_at)
                    continue

                pos = active_positions.get(symbol)
                is_add = pos is not None
                if is_add:
                    if BINANCE_ENV != 'demo':
                        record_decision('Spot', symbol, setup, current_price, 'تجاهل الإضافة',
                                        'إضافة المراكز مسموحة في وضع Demo فقط',
                                        order_status='لم يُرسل', price_updated_at=price_updated_at)
                        continue
                    entries = max(1, int(pos.get('entries', 1)))
                    if entries >= 2 or pos.get('add_attempted'):
                        record_decision('Spot', symbol, setup, current_price, 'تجاهل الإضافة',
                                        'وصل المركز إلى حد الإضافات أو توجد إضافة قيد التحقق',
                                        order_status='لم يُرسل', price_updated_at=price_updated_at)
                        continue
                    if setup['strategy'] != pos.get('strategy'):
                        reason = 'استراتيجية الإشارة مختلفة عن استراتيجية المركز المفتوح'
                        record_decision('Spot', symbol, setup, current_price, 'تجاهل الإضافة', reason,
                                        order_status='لم يُرسل', price_updated_at=price_updated_at)
                        print("⏸️ [PYRAMID] {} تجاهل: الاستراتيجية الحالية مختلفة عن صفقة المركز".format(symbol))
                        continue
                    if str(pos.get('last_add_candle') or '') == str(candle):
                        record_decision('Spot', symbol, setup, current_price, 'تجاهل الإضافة',
                                        'تمت محاولة إضافة في هذه الشمعة',
                                        order_status='لم يُرسل', price_updated_at=price_updated_at)
                        continue

                    entry_before_add = float(pos['entry_price'])
                    initial_stop = float(pos.get('stop_price') or entry_before_add * (1 - INITIAL_SL_PCT / 100))
                    initial_risk = max(entry_before_add - initial_stop, entry_before_add * 0.0005)
                    atr = max(float(setup.get('atr') or 0.0), current_price * 0.0005)
                    unrealized_before_add = current_price * float(pos['amount']) - float(pos['cost'])
                    required_move = max(atr, initial_risk)
                    if unrealized_before_add <= 0 or current_price < entry_before_add + required_move:
                        record_decision('Spot', symbol, setup, current_price, 'تجاهل الإضافة',
                                        'المركز لم يحقق شرط الربح والحركة بمقدار ATR',
                                        order_status='لم يُرسل', price_updated_at=price_updated_at)
                        print("⏸️ [PYRAMID] {} لم يصل إلى شرط الربح/الحركة المطلوبة".format(symbol))
                        continue

                    # Reserve the one allowed add before sending. If the response is
                    # ambiguous, the bot fails closed instead of risking a duplicate order.
                    pos['last_add_candle'] = str(candle)
                    pos['add_attempted'] = True
                    save_position(symbol, pos)
                    last_entry_candle[symbol] = candle
                    try:
                        add_stop_distance = atr * STRATEGY_RISK[pos['strategy']]['stop_atr']
                        if TRADING_ENABLED:
                            risk_check = _check_sized_spot_risk(symbol, current_price, add_stop_distance)
                            if not risk_check.get('allowed'):
                                pos['add_attempted'] = False
                                save_position(symbol, pos)
                                record_decision('Spot', symbol, setup, current_price, 'إضافة مرفوضة',
                                                risk_check.get('reason'), order_status='لم يُرسل',
                                                price_updated_at=price_updated_at)
                                continue
                        record_decision('Spot', symbol, setup, current_price, 'إرسال أمر إضافة',
                                        'إضافة إلى مركز رابح؛ انتظار نتيجة Binance',
                                        order_status='جارٍ الإرسال', price_updated_at=price_updated_at)
                        fill = place_buy(symbol, current_price, add_stop_distance)
                    except Exception as e:
                        record_decision('Spot', symbol, setup, current_price, 'خطأ في أمر الإضافة',
                                        'لم يتأكد تنفيذ الأمر؛ راجع أوامر Binance قبل أي إعادة',
                                        order_status='غير مؤكد', api_message=e,
                                        price_updated_at=price_updated_at)
                        print("❌ [PYRAMID ERROR] {}: {}".format(symbol, e))
                        log_event("ERR", "إضافة {}: {}".format(symbol, str(e)[:80]))
                        send_telegram_message("⚠️ <b>تعذر تأكيد إضافة {}</b>\nلن يعيد البوت المحاولة تلقائيًا لتجنب تكرار أمر قد يكون وصل إلى Binance. راجع سجل الأوامر في Demo.".format(symbol))
                        continue

                    total_amount = float(pos['amount']) + float(fill['amount'])
                    total_cost = float(pos['cost']) + float(fill['cost'])
                    weighted_entry = total_cost / total_amount if total_amount else fill['price']
                    risk = STRATEGY_RISK[pos['strategy']]
                    pos.update({
                        'entry_price': weighted_entry,
                        'amount': total_amount,
                        'cost': total_cost,
                        'highest_price': max(float(pos.get('highest_price') or 0.0), float(fill['price'])),
                        'stop_price': max(initial_stop, entry_before_add),
                        'target_price': weighted_entry + atr * risk['target_atr'],
                        'trailing_distance': min(
                            float(pos.get('trailing_distance') or atr * risk['trail_atr']),
                            atr * risk['trail_atr']),
                        'entries': entries + 1,
                        'simulated': bool(pos.get('simulated') or fill['simulated']),
                        'initial_cost': float(pos.get('initial_cost') or pos['cost']) + float(fill['cost']),
                        'fees_usdt': float(pos.get('fees_usdt') or 0.0) + float(fill.get('fee_usdt') or 0.0),
                        'fee_quality': ('estimated' if 'estimated' in (pos.get('fee_quality'), fill.get('fee_quality'))
                                        else 'actual' if pos.get('fee_quality') == fill.get('fee_quality') == 'actual'
                                        else 'unverified'),
                    })
                    active_positions[symbol] = pos
                    save_position(symbol, pos)
                    if not pos.get('simulated'):
                        try:
                            _ensure_spot_protective_stop(symbol, pos, pos.get('stop_price'))
                        except Exception as protection_error:
                            _mark_spot_reconciliation_required(symbol, pos,
                                'أُكدت إضافة Spot لكن لم يتأكد تحديث وقف الحماية ({})'.format(type(protection_error).__name__))
                            record_decision('Spot', symbol, setup, current_price, 'إضافة مؤكدة؛ الوقف يحتاج مراجعة',
                                            'لم يُرسل أمر آخر حتى مطابقة وقف الحماية', fill.get('order_status'),
                                            fill.get('order_id'), price_updated_at=price_updated_at)
                            continue
                    record_decision('Spot', symbol, setup, current_price, 'أُضيف إلى المركز',
                                    'أُكد تنفيذ الإضافة على Binance', fill.get('order_status'),
                                    fill.get('order_id'), price_updated_at=price_updated_at)
                    print("➕ [PYRAMID] {} | دخول {}/2 | متوسط {} | كمية إجمالية {}".format(
                        symbol, pos['entries'], weighted_entry, total_amount))
                    log_event("PYRAMID", "{} · دخول {}/2 · متوسط {:.8g}".format(
                        symbol, pos['entries'], weighted_entry))
                    send_telegram_message(
                        "➕ <b>إضافة Demo إلى مركز رابح</b>\n🪙 {}\n🧭 {} · {}\n📥 دخول الإضافة: {}\n📊 متوسط الدخول الجديد: {}\n🔒 الوقف: {}\n🎯 الهدف: {}\n📦 القيمة المضافة: {:.2f} USDT\n🔢 عدد مرات الدخول: {}/2{}".format(
                            symbol, setup['strategy_label'], setup['timeframe'], fill['price'],
                            weighted_entry, pos['stop_price'], pos['target_price'], fill['cost'],
                            pos['entries'], "\n🧪 محاكاة" if fill['simulated'] else ""))
                    continue

                last_entry_candle[symbol] = candle
                record_decision('Spot', symbol, setup, current_price, 'إرسال أمر شراء',
                                'اجتازت الإشارة فلاتر المخاطر والسيولة والأخبار',
                                order_status='جارٍ الإرسال', price_updated_at=price_updated_at)
                entry_atr = max(float(setup.get('atr') or 0.0), current_price * 0.0005)
                entry_stop_distance = entry_atr * STRATEGY_RISK[setup['strategy']]['stop_atr']
                if TRADING_ENABLED:
                    risk_check = _check_sized_spot_risk(symbol, current_price, entry_stop_distance)
                    if not risk_check.get('allowed'):
                        record_decision('Spot', symbol, setup, current_price, 'مرفوض بسبب مخاطر المحفظة',
                                        risk_check.get('reason'), order_status='لم يُرسل',
                                        price_updated_at=price_updated_at)
                        continue
                try:
                    fill = place_buy(symbol, current_price, entry_stop_distance)
                except Exception as e:
                    record_decision('Spot', symbol, setup, current_price, 'فشل أو لم يتأكد أمر الشراء',
                                    'لا تعاد المحاولة تلقائيًا؛ راجع سجل Binance',
                                    order_status='غير مؤكد', api_message=e,
                                    price_updated_at=price_updated_at)
                    print("❌ [BUY ERROR] {}: {}".format(symbol, e))
                    log_event("ERR", "شراء {}: {}".format(symbol, str(e)[:80]))
                    send_telegram_message("❌ <b>فشل شراء {}</b>\n{}".format(symbol, html.escape(str(e))))
                    continue

                entry = fill['price']
                atr = max(float(setup.get('atr') or 0.0), entry * 0.0005)
                risk = STRATEGY_RISK[setup['strategy']]
                stop_price = entry - atr * risk['stop_atr']
                target_price = entry + atr * risk['target_atr']
                trailing_distance = atr * risk['trail_atr']
                pos = {
                    'entry_price': entry, 'amount': fill['amount'], 'cost': fill['cost'],
                    'highest_price': entry, 'time': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'simulated': fill['simulated'], 'strategy': setup['strategy'],
                    'timeframe': setup['timeframe'], 'stop_price': stop_price,
                    'target_price': target_price, 'trailing_distance': trailing_distance,
                    'risk_distance': entry_atr * risk['stop_atr'], 'partial_taken': False,
                    'partial_pnl_usdt': 0.0, 'initial_cost': fill['cost'],
                    'fee_quality': fill.get('fee_quality', 'unverified'),
                    'fees_usdt': float(fill.get('fee_usdt') or 0.0),
                    'entries': 1, 'last_add_candle': None,
                }
                active_positions[symbol] = pos
                save_position(symbol, pos)
                if not pos.get('simulated'):
                    try:
                        _ensure_spot_protective_stop(symbol, pos, stop_price)
                    except Exception as protection_error:
                        _mark_spot_reconciliation_required(symbol, pos,
                            'أُكد فتح Spot لكن لم يتأكد وقف الحماية ({})'.format(type(protection_error).__name__))
                        record_decision('Spot', symbol, setup, current_price, 'صفقة مؤكدة؛ الوقف يحتاج مراجعة',
                                        'أوقف النظام فتح مراكز إضافية حتى مطابقة وقف الحماية', fill.get('order_status'),
                                        fill.get('order_id'), price_updated_at=price_updated_at)
                        continue
                record_decision('Spot', symbol, setup, current_price, 'تم فتح الصفقة',
                                'تم تأكيد التنفيذ وحفظ المركز', fill.get('order_status'),
                                fill.get('order_id'), price_updated_at=price_updated_at)
                print("🚀 [BUY] {} | {} {} | price {} | SL {} | TP {}".format(
                    symbol, setup['strategy'], setup['timeframe'], entry, stop_price, target_price))
                log_event("BUY", "{} · {} · {}".format(symbol, setup['strategy'], setup['timeframe']))
                send_telegram_message(
                    "🚀 <b>فتح صفقة Demo جديدة</b>\n🪙 {}\n🧭 {} · {}\n💵 الدخول: {}\n🛑 الوقف: {}\n🎯 الهدف: {}\n📦 القيمة: {:.2f} USDT{}".format(
                        symbol, setup['strategy_label'], setup['timeframe'], entry,
                        stop_price, target_price, fill['cost'],
                        "\n🧪 محاكاة" if fill['simulated'] else ""))

            except Exception as e:
                record_decision('Spot', symbol, setup, current_price, 'خطأ في فحص السوق',
                                'تعذر إكمال تقييم الزوج', order_status='لم يُرسل',
                                api_message=e, price_updated_at=price_updated_at)
                log_event('ERR', 'فحص Spot {}: {}'.format(symbol, type(e).__name__))
                print("Error scanning {}: {}".format(symbol, e))
        if futures_engine and BINANCE_ENV == 'demo' and TRADING_ENABLED:
            futures_engine.scan(
                TRADE_AMOUNT_USDT,
                lambda: len(futures_engine.positions) < MAX_FUTURES_CONCURRENT_TRADES,
                force_test_trade=FORCE_FUTURES_TEST_TRADE,
                force_test_id=FORCE_FUTURES_TEST_ID,
                force_test_side=FORCE_FUTURES_TEST_SIDE,
                market_context=strategy_news_context(),
            )
        record_equity()
        scan_succeeded = True
    except Exception as e:
        print("Error in automated market scan: {}".format(type(e).__name__))
        scanner_state['last_error'] = type(e).__name__
    finally:
        scanner_state.update({
            'status': 'idle' if scan_succeeded else 'error',
            'last_completed_at': datetime.datetime.now(datetime.timezone.utc).isoformat() if scan_succeeded else scanner_state.get('last_completed_at'),
            'duration_seconds': round(time.time() - scan_started, 1),
        })
        scan_lock.release()


# =====================================================================
# 6. لوحة التحكم (صفحة واحدة تقرأ البيانات الحية من /api/state)
# =====================================================================
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_dashboard_auth)])
def read_root():
    no_cache_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers=no_cache_headers)
    except Exception as e:
        return HTMLResponse(
            content="<h1>Error loading index.html: {}</h1>".format(html.escape(str(e))),
            headers=no_cache_headers,
        )


PERFORMANCE_REPORT_CACHE = {"updated_at": 0.0, "data": None}
def get_performance_report(days=30, limit=5000):
    """Aggregate closed trades by venue, strategy, and symbol without hiding cost gaps."""
    cached = PERFORMANCE_REPORT_CACHE
    if cached["data"] is not None and time.time() - cached["updated_at"] < 60:
        return cached["data"]
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=max(1, min(int(days), 3650)))).strftime("%Y-%m-%d %H:%M:%S")
    records = []
    unverified_spot_trades = 0
    try:
        with get_db_connection(DB_NAME) as conn:
            rows = conn.execute("""SELECT symbol, status, timestamp, pnl_usdt, pnl_pct, strategy, timeframe,
                cost_quality, gross_pnl_usdt, fees_usdt FROM trades
                WHERE timestamp >= ? ORDER BY id DESC LIMIT ?""", (cutoff, limit)).fetchall()
        for row in rows:
            quality = row[7] or "legacy_or_unverified"
            if row[3] is None or quality not in ("actual", "estimated", "simulated"):
                unverified_spot_trades += 1
                continue
            pnl = float(row[3])
            quality_label = {
                "actual": "صافي بعد رسوم فعلية",
                "estimated": "صافي بعد رسوم مقدرة",
                "simulated": "محاكاة برسوم مقدرة",
                "legacy_or_unverified": "سجل قديم أو تكلفة غير مؤكدة",
                "unverified": "تكلفة غير مؤكدة",
            }.get(quality, "تكلفة غير مؤكدة")
            records.append({"market":"Spot", "symbol":row[0], "status":row[1], "time":row[2],
                "pnl_usdt":pnl, "strategy":row[5] or "غير مسجل", "timeframe":row[6] or "غير مسجل",
                "cost_basis":quality_label,
                "gross_pnl_usdt": (float(row[8]) if row[8] is not None else None),
                "fees_usdt": (float(row[9]) if row[9] is not None else None),
                "funding_usdt": None})
    except Exception as exc:
        print("[PERFORMANCE] Spot report unavailable: {}".format(type(exc).__name__), flush=True)
    if futures_engine:
        try:
            with futures_engine._connect() as conn:
                rows = conn.execute("""SELECT symbol, status, timestamp, pnl_usdt, strategy, timeframe,
                    cost_quality, fees_usdt, funding_usdt, gross_pnl_usdt FROM futures_trades
                    WHERE timestamp >= ? ORDER BY id DESC LIMIT ?""", (cutoff, limit)).fetchall()
            for row in rows:
                quality = row[6] or "legacy_gross_before_fees"
                quality_label = {
                    "net_actual_costs": "صافي بعد رسوم فعلية وتمويل مُزامن",
                    "net_estimated_costs": "صافي مع تكلفة مقدرة أو تمويل غير متاح",
                    "legacy_gross_before_fees": "سجل قديم؛ إجمالي قبل الرسوم والتمويل",
                }.get(quality, "تكلفة غير مؤكدة")
                records.append({"market":"Futures Demo", "symbol":row[0], "status":row[1], "time":row[2],
                    "pnl_usdt":float(row[3] or 0.0), "strategy":row[4] or "غير مسجل",
                    "timeframe":row[5] or "غير مسجل", "cost_basis":quality_label,
                    "fees_usdt":float(row[7] or 0.0), "funding_usdt":float(row[8] or 0.0),
                    "gross_pnl_usdt":float(row[9] or 0.0)})
        except Exception as exc:
            print("[PERFORMANCE] Futures report unavailable: {}".format(type(exc).__name__), flush=True)

    grouped = {}
    for item in records:
        key=(item["market"], item["symbol"], item["strategy"], item["timeframe"], item["cost_basis"])
        grouped.setdefault(key, []).append(item)
    groups=[]
    for key, items in grouped.items():
        values=[item["pnl_usdt"] for item in items]
        gross_values=[item.get("gross_pnl_usdt") for item in items]
        fee_values=[item.get("fees_usdt") for item in items]
        funding_values=[item.get("funding_usdt") for item in items]
        wins=[value for value in values if value > 0]
        losses=[value for value in values if value < 0]
        groups.append({
            "market":key[0], "symbol":key[1], "strategy":key[2], "timeframe":key[3],
            "cost_basis":key[4], "sample_size":len(values),
            "wins":len(wins), "losses":len(losses),
            "win_rate":len(wins)/len(values)*100 if values else 0.0,
            "pnl_total":sum(values), "pnl_per_trade":sum(values)/len(values) if values else 0.0,
            "gross_pnl_total": (sum(v for v in gross_values if v is not None)
                                  if all(v is not None for v in gross_values) else None),
            "fees_total": (sum(v for v in fee_values if v is not None)
                             if all(v is not None for v in fee_values) else None),
            "funding_total": (sum(v for v in funding_values if v is not None)
                                if all(v is not None for v in funding_values) else None),
            "avg_win":sum(wins)/len(wins) if wins else 0.0,
            "avg_loss":sum(losses)/len(losses) if losses else 0.0,
            "sample_label":"عينة أولية" if len(values)<30 else "30 صفقة أو أكثر",
        })
    groups.sort(key=lambda item:(item["market"], -item["sample_size"], item["symbol"], item["strategy"]))
    result = {"days":max(1,min(int(days),3650)), "minimum_sample":30, "groups":groups,
              "unverified_spot_trades": unverified_spot_trades}
    PERFORMANCE_REPORT_CACHE.update({"updated_at":time.time(), "data":result})
    return result


@app.get("/api/state", dependencies=[Depends(require_dashboard_auth)])
def api_state():
    spot_prices_fresh = True
    total, wins, losses, win_rate, total_pnl, best, worst = get_performance_stats()
    realized_total, realized_today, unverified_spot_trades, estimated_spot_trades = get_realized_stats()
    free_usdt, total_usdt = get_binance_balance()

    positions = []
    invested = 0.0
    unrealized = 0.0
    spot_position_value = 0.0
    for sym, pos in active_positions.items():
        entry = pos['entry_price']
        high = pos.get('highest_price', entry)
        price, price_fresh = get_spot_mark_price(sym, entry)
        spot_prices_fresh = spot_prices_fresh and price_fresh
        pnl_usdt = (float(pos['amount']) * price - float(pos['cost'])
                    + float(pos.get('partial_pnl_usdt') or 0.0))
        if not pos.get('simulated'):
            spot_position_value += float(pos.get('amount') or 0.0) * price
        if not pos.get('simulated'):
            invested += float(pos.get('cost') or 0.0)
        unrealized += pnl_usdt
        positions.append({
            'symbol': sym, 'entry': entry, 'amount': pos['amount'], 'cost': pos['cost'],
            'highest': high, 'price': price, 'pnl_usdt': pnl_usdt,
            'pnl_pct': pnl_usdt / float(pos.get('initial_cost') or pos['cost'] or 1.0) * 100,
            'stop': max(pos.get('stop_price') or entry * (1 - INITIAL_SL_PCT / 100),
                        high - pos.get('trailing_distance') if pos.get('trailing_distance') else high * (1 - TRAILING_SL_PCT / 100)),
            'target': pos.get('target_price') or r_multiple_target(
                entry, pos.get('stop_price') or entry * (1 - INITIAL_SL_PCT / 100)),
            'strategy': pos.get('strategy', 'trend'), 'timeframe': pos.get('timeframe', '1h'),
            'simulated': bool(pos.get('simulated')), 'time': pos.get('time', ''),
            'protection_status': ('مطابقة مطلوبة' if pos.get('reconciliation_required') else
                                  'وقف Binance Demo نشط' if pos.get('protective_order_id') else
                                  'محاكاة' if pos.get('simulated') else 'وقف غير معروف'),
            'entries': max(1, int(pos.get('entries', 1))),
            'add_pending': bool(pos.get('add_attempted') and int(pos.get('entries', 1)) < 2)
        })

    markets = []
    for sym in SYMBOLS:
        d = market_snapshot.get(sym)
        if not d:
            markets.append({'symbol': sym, 'ready': False, 'in_position': sym in active_positions,
                            'supported': sym in SUPPORTED_SYMBOLS,
                            'reason': UNSUPPORTED_SYMBOLS.get(sym, 'بانتظار أول فحص')})
            continue
        markets.append({
            'symbol': sym, 'ready': True, 'price': d['price'], 'rsi': d['rsi'], 'ema': d['ema'],
            'ref_close': d.get('ref_close', d['price']), 'hist': d['hist'], 'signal': d['signal'], 'conds': d.get('conds', {}),
            'time': d['time'],
            'age_seconds': int(max(0.0, time.time() - float(d.get('updated_at') or time.time()))),
            'in_position': sym in active_positions,
            'strategy': d.get('strategy', 'wait'), 'strategy_label': d.get('strategy_label', 'انتظار'),
            'timeframe': d.get('timeframe', '15m'), 'signal_strength': d.get('signal_strength', 0),
            'reason': d.get('reason', '')
        })

    futures_position_value = 0.0
    if futures_engine:
        for sym, pos in futures_engine.positions.items():
            price = float((futures_engine.snapshot.get(sym) or {}).get('price') or pos['entry_price'])
            market = futures_engine.exchange.market(sym)
            multiplier = float(market.get('contractSize') or 1)
            contracts = float(pos.get('contracts') or 0.0)
            side = pos.get('side', 'long')
            direction = -1.0 if side == 'short' else 1.0
            gross_pnl = direction * (price - pos['entry_price']) * contracts * multiplier
            entry_fee_remaining = float(pos.get('remaining_entry_fee_usdt') or 0.0)
            close_fee_estimate = abs(contracts * multiplier * price) * float(market.get('taker') or 0.0005)
            pnl = (float(pos.get('partial_pnl_usdt') or 0.0) + gross_pnl - entry_fee_remaining
                   - close_fee_estimate + float(pos.get('funding_usdt') or 0.0))
            unrealized += pnl
            futures_position_value += abs(contracts * multiplier * price)
            positions.append({'symbol': sym, 'entry': pos['entry_price'], 'amount': pos['amount'],
                'cost': pos['cost'], 'highest': pos['highest_price'], 'price': price, 'pnl_usdt': pnl,
                'pnl_pct': pnl / float(pos.get('initial_cost') or pos['cost'] or 1.0) * 100, 'stop': pos['stop_price'],
                'target': pos['target_price'], 'strategy': pos['strategy'], 'timeframe': pos['timeframe'],
                'side': side, 'simulated': False, 'time': pos['opened_at'],
                'protection_status': ('مطابقة مطلوبة' if pos.get('reconciliation_required') else
                                      'وقف Binance Demo نشط' if pos.get('protective_algo_id') else 'وقف غير معروف'),
                'market_type': 'Futures Demo'})
    if futures_engine:
        for sym in futures_engine.symbols:
            data = futures_engine.snapshot.get(sym)
            if data:
                markets.append({'symbol': sym, 'ready': data.get('ready', False), 'supported': True,
                    'price': data.get('price'), 'rsi': data.get('rsi', 50), 'ema': data.get('ema', 0),
                    'ref_close': data.get('ref_close', data.get('price')), 'hist': data.get('hist', 0),
                    'signal': data.get('signal', False), 'conds': data.get('conds', {}), 'time': '',
                    'in_position': sym in futures_engine.positions, 'direction': data.get('direction', 'flat'),
                    'strategy': data.get('strategy', 'wait'),
                    'strategy_label': data.get('strategy_label', 'انتظار'), 'timeframe': data.get('timeframe', '15m'),
                    'signal_strength': data.get('signal_strength', 0),
                    'age_seconds': int(max(0.0, time.time() - float(data.get('updated_at') or time.time()))),
                    'reason': data.get('reason', ''), 'market_type': 'Futures Demo'})
            else:
                markets.append({'symbol': sym, 'ready': False, 'supported': True,
                    'in_position': sym in futures_engine.positions, 'market_type': 'Futures Demo',
                    'reason': 'بانتظار أول فحص Futures'})
    trades = [{
        'id': t[0], 'symbol': t[1], 'entry': t[2], 'exit': t[3], 'pnl_pct': t[4],
        'status': t[5], 'time': t[6], 'pnl_usdt': t[7], 'cost_quality': t[8],
        'pnl_known': (t[7] is not None and t[8] in ('actual', 'estimated', 'simulated')),
        'gross_pnl_usdt': t[9], 'fees_usdt': t[10], 'funding_usdt': None
    } for t in get_recent_trades(limit=8)]
    if futures_engine:
        try:
            with futures_engine._connect() as conn:
                rows = conn.execute('SELECT id, symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, side, cost_quality, gross_pnl_usdt, fees_usdt, funding_usdt FROM futures_trades ORDER BY id DESC LIMIT 8').fetchall()
            trades.extend({'id': 'f-' + str(t[0]), 'symbol': t[1], 'entry': t[2], 'exit': t[3], 'pnl_pct': t[4],
                'status': t[5], 'time': t[6], 'pnl_usdt': t[7], 'side': t[8], 'cost_quality': t[9],
                'pnl_known': t[9] in ('net_actual_costs', 'net_estimated_costs'),
                'gross_pnl_usdt': t[10], 'fees_usdt': t[11], 'funding_usdt': t[12],
                'market_type': 'Futures Demo'} for t in rows)
            trades = sorted(trades, key=lambda t: t['time'], reverse=True)[:8]
        except Exception:
            logger.debug("Suppressed error in %s", "api_state", exc_info=True)
            pass

    spot_open_trade_slots = sum(max(1, int(pos.get('entries', 1))) for pos in active_positions.values()) + sum(
        1 for pos in active_positions.values()
        if pos.get('add_attempted') and int(pos.get('entries', 1)) < 2)
    future_state = futures_engine.public_state() if futures_engine else {
        'connected': False, 'environment': 'demo', 'trading_enabled': False, 'symbols': [],
        'open_positions': 0, 'balance': 0.0, 'free_balance': 0.0, 'closed': 0,
        'wins': 0, 'losses': 0, 'realized_pnl': 0.0}
    try:
        with get_db_connection(DB_NAME) as conn:
            agreement_row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = 'tradfi_agreement_key_fingerprint'"
            ).fetchone()
        current_fingerprint = hashlib.sha256(BINANCE_FUTURES_API_KEY.encode()).hexdigest() if BINANCE_FUTURES_API_KEY else ''
        future_state['tradfi_agreement_signed'] = bool(
            agreement_row and current_fingerprint and secrets.compare_digest(agreement_row[0], current_fingerprint)
        )
    except Exception:
        logger.debug("Suppressed error in %s", "api_state", exc_info=True)
        future_state['tradfi_agreement_signed'] = False
    return {
        'as_of': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'scanner': dict(scanner_state),
        'daily_risk': {
            'spot': dict(daily_risk_state['spot']) if daily_risk_state.get('spot') else None,
            'futures': dict(future_state['daily_risk']) if future_state.get('daily_risk') else None,
        },
        'futures': future_state,
        'env': BINANCE_ENV,
        'trading_enabled': TRADING_ENABLED,
        'telegram': bool(TELEGRAM_BOT_TOKEN),
        'trade_amount': TRADE_AMOUNT_USDT,
        'settings': {
            'spot_max_concurrent_trades': MAX_SPOT_CONCURRENT_TRADES,
            'spot_active_trades': spot_open_trade_slots,
            'futures_max_concurrent_trades': MAX_FUTURES_CONCURRENT_TRADES,
            'futures_active_trades': len(futures_engine.positions) if futures_engine else 0,
            'active_trades': spot_open_trade_slots + (len(futures_engine.positions) if futures_engine else 0),
        },
        'wallet': {
            'total': total_usdt, 'free': free_usdt,
            'cash': max(0.0, total_usdt - spot_position_value),
            'positions_value': spot_position_value,
            'price_status': 'fresh' if spot_prices_fresh else 'stale',
        },
        'balance_status': ('unavailable' if _balance_cache['v'] == (0.0, 0.0)
                           else 'stale' if _balance_cache.get('error') else 'fresh'),
        'balance_age_seconds': int(max(0.0, time.time() - _balance_cache['t'])) if _balance_cache['t'] else None,
        'stats': {
            'realized_total': realized_total, 'realized_today': realized_today,
            'unverified_spot_trades': unverified_spot_trades,
            'estimated_spot_trades': estimated_spot_trades,
            'unrealized': unrealized, 'wins': wins, 'losses': losses, 'total': total,
            'win_rate': win_rate, 'best': best, 'worst': worst,
            'exposure': min((invested + futures_position_value) /
                           max(total_usdt + float(future_state.get('balance') or 0.0), 1e-12) * 100, 100.0)
                        if total_usdt + float(future_state.get('balance') or 0.0) > 0 else 0.0
        },
        'params': {
            'timeframe': 'auto', 'strategy_mode': 'automatic',
            'strategies': ['trend', 'breakout', 'bounce'],
            'ema_period': EMA_PERIOD, 'rsi_max': RSI_MAX_ENTRY,
            'require_macd': REQUIRE_MACD, 'macd_tolerance_pct': MACD_HIST_TOLERANCE_PCT, 'tp_usdt': None, 'tp_r_multiple': TP_R_MULTIPLE, 'sl': INITIAL_SL_PCT,
            'trail': TRAILING_SL_PCT, 'news_severity': NEWS_BLOCK_SEVERITY,
            'news_pause_h': NEWS_PAUSE_HOURS
        },
        'markets': markets,
        'positions': positions,
        'trades': trades,
        'performance_report': get_performance_report(days=30),
        'platforms': [
            {'id': 'binance', 'name': 'Binance Spot', 'connected': bool(BINANCE_API_KEY and BINANCE_SECRET_KEY), 'environment': BINANCE_ENV, 'trading_enabled': TRADING_ENABLED, 'balance': total_usdt, 'free_balance': free_usdt, 'open_positions': len(active_positions), 'realized_pnl': realized_total, 'closed_trades': total, 'status': 'connected' if BINANCE_API_KEY and BINANCE_SECRET_KEY else 'credentials missing'},
            {'id': 'binance_futures', 'name': 'Binance Futures', 'connected': future_state['connected'], 'environment': 'demo', 'trading_enabled': bool(future_state['connected'] and TRADING_ENABLED), 'balance': future_state['balance'], 'free_balance': future_state['free_balance'], 'open_positions': future_state['open_positions'], 'realized_pnl': future_state['realized_pnl'], 'closed_trades': future_state['closed'], 'symbols': future_state['symbols'], 'status': 'connected' if future_state['connected'] else 'credentials missing'},
            {'id': 'mitrade', 'name': 'Mitrade', 'connected': False, 'supported': False, 'status': 'No public trading API documentation found'}
        ],
        'news': {
            'enabled': NEWS_FILTER, 'sentiment': news_state['sentiment'],
            'severity': news_state['severity'], 'reason': news_state['reason'],
            'source': news_state['source'], 'checked_at': news_state['checked_at'],
            'blocked': bool(news_blocked()),
            'blocked_until': datetime.datetime.fromtimestamp(news_state['blocked_until']).strftime("%H:%M") if news_state['blocked_until'] else None,
            'model': _groq_model.get('name') or '',
            'headlines': [{'source': n, 'title': t, 'title_ar': (news_state['headlines_ar'][i] if i < len(news_state['headlines_ar']) else '')} for i, (n, t) in enumerate(news_state['headlines'])],
            'error': news_state['error']
        },
        'events': list(event_log),
        'decisions': get_recent_decisions(80)
    }



@app.post("/api/settings", dependencies=[Depends(require_dashboard_auth)])
def update_runtime_settings(payload: RuntimeSettings):
    """Persist and apply sizing limits immediately.

    Previously this required the scan lock and returned 409 whenever a scan was
    running; with ~60s scans every minute almost every save was rejected. The
    values are simple numbers read at order time, so settings_lock is enough.
    """
    global TRADE_AMOUNT_USDT, MAX_SPOT_CONCURRENT_TRADES, MAX_FUTURES_CONCURRENT_TRADES
    with settings_lock:
        with get_db_connection(DB_NAME) as conn:
            conn.executemany('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', [
                ('trade_amount_usdt', str(payload.trade_amount_usdt)),
                ('spot_max_concurrent_trades', str(payload.spot_max_concurrent_trades)),
                ('futures_max_concurrent_trades', str(payload.futures_max_concurrent_trades)),
                ('trade_amount_migrated_v2', '1')
            ])
        TRADE_AMOUNT_USDT = float(payload.trade_amount_usdt)
        MAX_SPOT_CONCURRENT_TRADES = int(payload.spot_max_concurrent_trades)
        MAX_FUTURES_CONCURRENT_TRADES = int(payload.futures_max_concurrent_trades)
        log_event("SETTINGS", "حد Spot {} · حد Futures {} · حجم الصفقة {} USDT".format(
            MAX_SPOT_CONCURRENT_TRADES, MAX_FUTURES_CONCURRENT_TRADES, TRADE_AMOUNT_USDT))
        return {
            'ok': True,
            'trade_amount_usdt': TRADE_AMOUNT_USDT,
            'spot_max_concurrent_trades': MAX_SPOT_CONCURRENT_TRADES,
            'futures_max_concurrent_trades': MAX_FUTURES_CONCURRENT_TRADES,
        }


@app.post("/api/platforms/binance/credentials", dependencies=[Depends(require_dashboard_auth)])
def update_binance_credentials(payload: BinanceCredentials):
    global exchange, BINANCE_API_KEY, BINANCE_SECRET_KEY
    if BINANCE_ENV != "demo":
        raise HTTPException(status_code=409, detail="Dashboard credential updates are restricted to Binance Demo")
    if not CREDENTIAL_ENCRYPTION_KEY:
        raise HTTPException(status_code=503, detail="Encrypted credential storage is not configured")
    if active_positions:
        raise HTTPException(status_code=409, detail="Close or wait for open positions before changing API credentials")
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Market scan in progress; retry shortly")
    try:
        try:
            candidate = make_binance_exchange(payload.api_key.strip(), payload.secret_key.strip(), BINANCE_ENV)
            candidate.load_markets()
            candidate.fetch_balance()
        except Exception:
            raise HTTPException(status_code=400, detail="Binance Demo could not verify these credentials")
        cipher = _credential_cipher()
        with get_db_connection(DB_NAME) as conn:
            conn.execute('INSERT OR REPLACE INTO platform_credentials (platform, api_key_enc, secret_enc, updated_at) VALUES (?, ?, ?, ?)',
                         ('binance', cipher.encrypt(payload.api_key.strip().encode()).decode(), cipher.encrypt(payload.secret_key.strip().encode()).decode(), datetime.datetime.now().isoformat(timespec='seconds')))
        with settings_lock:
            exchange = candidate
            BINANCE_API_KEY = payload.api_key.strip()
            BINANCE_SECRET_KEY = payload.secret_key.strip()
        log_event("PLATFORM", "تم تحديث اتصال Binance Demo")
        return {'ok': True, 'platform': 'binance', 'environment': BINANCE_ENV, 'message': 'Binance Demo credentials saved encrypted'}
    finally:
        scan_lock.release()
@app.post('/api/platforms/binance-futures/credentials', dependencies=[Depends(require_dashboard_auth)])
def update_binance_futures_credentials(payload: FuturesCredentials):
    global futures_engine, BINANCE_FUTURES_API_KEY, BINANCE_FUTURES_SECRET_KEY
    if BINANCE_ENV != 'demo':
        raise HTTPException(status_code=409, detail='Futures API credentials are accepted only in Binance Demo mode')
    if not CREDENTIAL_ENCRYPTION_KEY:
        raise HTTPException(status_code=503, detail='Encrypted credential storage is not configured')
    if futures_engine and futures_engine.positions:
        raise HTTPException(status_code=409, detail='Close or wait for Futures Demo positions before changing keys')
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='Market scan in progress; retry shortly')
    try:
        try:
            candidate = BinanceFuturesDemo(payload.api_key.strip(), payload.secret_key.strip(), DB_NAME, log_event, record_decision, get_spot_hedge_exposures, portfolio_risk_snapshot)
            balance_check = candidate.balance()
            if balance_check.get('error'):
                raise RuntimeError('Futures API authentication failed')
            if not candidate.symbols:
                raise RuntimeError('No supported oil or gold contracts in Futures Demo markets')
        except Exception:
            raise HTTPException(status_code=400, detail='تعذر التحقق من Binance Futures Demo أو أن عقود النفط/الذهب غير متاحة في حساب الديمو')
        cipher = _credential_cipher()
        with get_db_connection(DB_NAME) as conn:
            conn.execute('INSERT OR REPLACE INTO platform_credentials (platform, api_key_enc, secret_enc, updated_at) VALUES (?, ?, ?, ?)',
                ('binance_futures', cipher.encrypt(payload.api_key.strip().encode()).decode(), cipher.encrypt(payload.secret_key.strip().encode()).decode(), datetime.datetime.now().isoformat(timespec='seconds')))
        with settings_lock:
            futures_engine = candidate
            BINANCE_FUTURES_API_KEY = payload.api_key.strip()
            BINANCE_FUTURES_SECRET_KEY = payload.secret_key.strip()
        log_event('PLATFORM', 'تم ربط Binance Futures Demo')
        return {'ok': True, 'platform': 'binance_futures', 'environment': 'demo', 'symbols': candidate.symbols,
            'message': 'Binance Futures Demo credentials saved encrypted'}
    finally:
        scan_lock.release()


@app.post("/api/platforms/binance-futures/tradfi-agreement", dependencies=[Depends(require_dashboard_auth)])
def sign_binance_tradfi_agreement(payload: TradFiAgreementConsent):
    if payload.accepted is not True:
        raise HTTPException(status_code=400, detail="Explicit agreement confirmation is required")
    if BINANCE_ENV != "demo":
        raise HTTPException(status_code=409, detail="TradFi agreement signing is restricted to Binance Demo mode")
    if not futures_engine or not BINANCE_FUTURES_API_KEY or not BINANCE_FUTURES_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Binance Futures Demo API credentials are unavailable")
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Market scan in progress; retry shortly")
    try:
        params = urllib.parse.urlencode({
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        })
        signature = hmac.new(
            BINANCE_FUTURES_SECRET_KEY.encode("utf-8"),
            params.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = "https://demo-fapi.binance.com/fapi/v1/stock/contract?{}&signature={}".format(params, signature)
        request = urllib.request.Request(
            url,
            data=b"",
            method="POST",
            headers={"X-MBX-APIKEY": BINANCE_FUTURES_API_KEY},
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                error_data = json.loads(exc.read().decode("utf-8", errors="replace"))
                message = str(error_data.get("msg") or "Binance rejected the request")[:180]
                code = error_data.get("code")
            except Exception:
                logger.debug("Suppressed error in %s", "sign_binance_tradfi_agreement", exc_info=True)
                message, code = "Binance rejected the request", None
            detail = {"message": "لم يقبل Binance طلب توقيع الاتفاقية في Demo", "reason": message}
            if code is not None:
                detail["binance_code"] = code
            raise HTTPException(status_code=400, detail=detail)
        except Exception:
            raise HTTPException(status_code=502, detail="تعذر الاتصال بواجهة Binance Futures Demo")
        try:
            result = json.loads(response_body) if response_body else {}
        except ValueError:
            result = {}
        fingerprint = hashlib.sha256(BINANCE_FUTURES_API_KEY.encode()).hexdigest()
        with get_db_connection(DB_NAME) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                ("tradfi_agreement_key_fingerprint", fingerprint),
            )
        log_event("PLATFORM", "أُرسل طلب توقيع اتفاقية TradFi إلى Binance Futures Demo بعد موافقة المستخدم")
        return {"ok": True, "environment": "demo", "result": result}
    finally:
        scan_lock.release()


@app.get("/api/equity", dependencies=[Depends(require_dashboard_auth)])
def api_equity(range: str = "1d"):
    return get_equity_series(range)


@app.get("/check", dependencies=[Depends(require_dashboard_auth)])
def manual_check():
    # Run the scan in the background so the HTTP request is not blocked by
    # exchange calls (including the time.sleep() retry in _complete_order).
    threading.Thread(target=automated_market_scan, name="manual-scan", daemon=True).start()
    return RedirectResponse(url="/", status_code=303)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
