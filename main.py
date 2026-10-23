import os
import secrets
import time
import html
import ccxt
import sqlite3
import datetime
import threading
from collections import deque
import urllib.request
import urllib.error
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
from strategy_engine import evaluate_symbol, STRATEGY_RISK
from futures_engine import BinanceFuturesDemo

# =====================================================================
# 0. الإعدادات (من متغيرات البيئة)
# =====================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip().strip('"').strip("'")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "").strip().strip('"').strip("'")
BINANCE_FUTURES_API_KEY = os.getenv("BINANCE_FUTURES_API_KEY", "").strip().strip('"').strip("'")
BINANCE_FUTURES_SECRET_KEY = os.getenv("BINANCE_FUTURES_SECRET_KEY", "").strip().strip('"').strip("'")
BINANCE_ENV = os.getenv("BINANCE_ENV", "demo").strip().lower()  # demo | testnet | live

# حجم كل صفقة بالدولار (USDT)
TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "1000"))
MAX_SPOT_CONCURRENT_TRADES = int(os.getenv("MAX_SPOT_CONCURRENT_TRADES", "6"))
MAX_FUTURES_CONCURRENT_TRADES = int(os.getenv("MAX_FUTURES_CONCURRENT_TRADES", "6"))
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()

# تنفيذ الأوامر الفعلية:
#  - demo/testnet: مفعّل افتراضياً (TRADING_ENABLED=false لجعله محاكاة فقط)
#  - live: لا يُنفَّذ أي أمر إلا إذا وضعت CONFIRM_LIVE_TRADING=yes صراحةً
if BINANCE_ENV == "live":
    TRADING_ENABLED = os.getenv("CONFIRM_LIVE_TRADING", "").strip().lower() == "yes"
else:
    TRADING_ENABLED = os.getenv("TRADING_ENABLED", "true").strip().lower() == "true"

# --- معاملات الاستراتيجية (مطابقة لـ backtest.py) ---
TIMEFRAME = os.getenv("TIMEFRAME", "1h").strip()          # 1h افتراضياً (مثل backtest.py)، جرّب 15m لإشارات أكثر
CANDLES_LIMIT = 500
RSI_MAX_ENTRY = float(os.getenv("RSI_MAX_ENTRY", "70"))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "50"))
REQUIRE_MACD = os.getenv("REQUIRE_MACD", "true").strip().lower() == "true"
MACD_HIST_TOLERANCE_PCT = float(os.getenv("MACD_HIST_TOLERANCE_PCT", "0.10"))
if not 0.0 <= MACD_HIST_TOLERANCE_PCT <= 0.50:
    raise ValueError("MACD_HIST_TOLERANCE_PCT must be between 0 and 0.50")
TAKE_PROFIT_USDT = float(os.getenv("TAKE_PROFIT_USDT", "2"))  # هدف الربح الإجمالي لكل صفقة، قبل الرسوم
INITIAL_SL_PCT = 0.5       # وقف الخسارة الأولي %
TRAILING_SL_PCT = 0.3      # وقف الخسارة المتحرك %

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT"]
SUPPORTED_SYMBOLS = set(SYMBOLS)
UNSUPPORTED_SYMBOLS = {}

# صفقة اختبار واحدة: تُفتح فوراً عند التشغيل وتُغلق تلقائياً بعد دقيقتين (للتأكد من عمل الأوامر)
FORCE_TEST_TRADE = os.getenv("FORCE_TEST_TRADE", "false").strip().lower() == "true"
TEST_TRADE_SECONDS = 120
test_trade_done = False
FORCE_FUTURES_TEST_TRADE = os.getenv("FORCE_FUTURES_TEST_TRADE", "false").strip().lower() == "true"
FORCE_FUTURES_TEST_ID = os.getenv("FORCE_FUTURES_TEST_ID", "").strip()
FORCE_FUTURES_TEST_SIDE = os.getenv("FORCE_FUTURES_TEST_SIDE", "long").strip().lower()

# --- فلتر الأخبار: يوقف الشراء الجديد فقط عند أخبار سلبية قوية (لا يغيّر الاستراتيجية ولا يغلق صفقات مفتوحة) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip().strip('"').strip("'")
GROQ_MODEL = os.getenv("GROQ_MODEL", "").strip()   # فارغ = يكتشف البوت نموذجاً متاحاً لحسابك تلقائياً
NEWS_FILTER = os.getenv("NEWS_FILTER", "true").strip().lower() == "true"
NEWS_PAUSE_HOURS = float(os.getenv("NEWS_PAUSE_HOURS", "3"))     # مدة إيقاف الشراء بعد آخر خبر سلبي قوي
NEWS_WINDOW_HOURS = float(os.getenv("NEWS_WINDOW_HOURS", "6"))   # نافذة العناوين المعتبرة
NEWS_BLOCK_SEVERITY = int(os.getenv("NEWS_BLOCK_SEVERITY", "4")) # الخطورة (1-5) التي تُوقف الشراء
NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]

print("🔐 [CONFIG] Binance credentials loaded:", bool(BINANCE_API_KEY and BINANCE_SECRET_KEY))
print("🌐 [ENV] Binance environment:", BINANCE_ENV)
print("📐 [STRATEGY] Automatic selection: trend (1d/4h), breakout (1h/15m), bounce (15m/5m) | risk exits: ATR-based")
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
        print("Telegram Error: {}".format(e))


# =====================================================================
# 1. قاعدة البيانات
# =====================================================================
DB_NAME = os.getenv("DB_PATH", "data/trading_bot.db")
_db_dir = os.path.dirname(DB_NAME)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


def init_db():
    try:
        conn = sqlite3.connect(DB_NAME)
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
        try:
            cursor.execute('ALTER TABLE trades ADD COLUMN pnl_usdt REAL')
        except sqlite3.OperationalError:
            pass  # العمود موجود مسبقاً
        conn.commit()
        conn.close()
    except Exception as e:
        print("Database Init Error: {}".format(e))



def _credential_cipher():
    if not CREDENTIAL_ENCRYPTION_KEY:
        raise RuntimeError("Credential encryption is not configured")
    return Fernet(CREDENTIAL_ENCRYPTION_KEY.encode("ascii"))


def load_runtime_settings():
    global TRADE_AMOUNT_USDT, MAX_SPOT_CONCURRENT_TRADES, MAX_FUTURES_CONCURRENT_TRADES
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        values = dict(conn.execute('SELECT key, value FROM bot_settings').fetchall())
        stored_amount = float(values.get('trade_amount_usdt', TRADE_AMOUNT_USDT))
        # One-time migration: the user requested changing the existing 100 USDT
        # trade size to 1000 USDT. Preserve any other saved dashboard amount.
        if abs(stored_amount - 100.0) < 1e-9 and abs(TRADE_AMOUNT_USDT - 1000.0) < 1e-9:
            stored_amount = 1000.0
            conn.execute('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)',
                         ('trade_amount_usdt', str(stored_amount)))
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
        print("Settings Load Error: {}".format(e))
    finally:
        if conn is not None:
            conn.close()


def load_saved_binance_credentials(platform='binance'):
    if not CREDENTIAL_ENCRYPTION_KEY:
        return None
    try:
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute('SELECT api_key_enc, secret_enc FROM platform_credentials WHERE platform = ?', (platform,)).fetchone()
        conn.close()
        if row:
            cipher = _credential_cipher()
            return cipher.decrypt(row[0].encode()).decode(), cipher.decrypt(row[1].encode()).decode()
    except Exception as e:
        print("Saved credential load error (details omitted): {}".format(type(e).__name__))
    return None


def make_binance_exchange(api_key, secret_key):
    client = ccxt.binance({
        'apiKey': api_key, 'secret': secret_key, 'enableRateLimit': True,
        'options': {'defaultType': 'spot', 'adjustForTimeDifference': True}
    })
    if BINANCE_ENV == "demo":
        if not hasattr(client, "enable_demo_trading"):
            raise RuntimeError("Installed ccxt does not support Binance Demo")
        client.enable_demo_trading(True)
    elif BINANCE_ENV == "testnet":
        client.set_sandbox_mode(True)
    return client
def log_trade(symbol, entry_price, exit_price, pnl_pct, status, pnl_usdt=None):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''
            INSERT INTO trades (symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, entry_price, exit_price, pnl_pct, status, now_str, pnl_usdt))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Log Trade Error: {}".format(e))


def get_recent_trades(limit=15):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, symbol, entry_price, exit_price, pnl_pct, status, timestamp,
                   COALESCE(pnl_usdt, pnl_pct / 100.0 * ?)
            FROM trades ORDER BY id DESC LIMIT ?
        ''', (TRADE_AMOUNT_USDT, limit))
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print("Get Trades Error: {}".format(e))
        return []


def get_performance_stats():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*), SUM(CASE WHEN status="WIN" THEN 1 ELSE 0 END), SUM(pnl_pct), MAX(pnl_pct), MIN(pnl_pct) FROM trades')
        row = cursor.fetchone()
        conn.close()
        if row and row[0] > 0:
            total = row[0]
            wins = row[1] or 0
            losses = total - wins
            win_rate = (wins / total) * 100
            total_pnl = row[2] or 0.0
            best = row[3] or 0.0
            worst = row[4] or 0.0
            return total, wins, losses, win_rate, total_pnl, best, worst
        return 0, 0, 0, 0.0, 0.0, 0.0, 0.0
    except Exception as e:
        print("Stats Error: {}".format(e))
        return 0, 0, 0, 0.0, 0.0, 0.0, 0.0


def get_realized_stats():
    """إجمالي الربح/الخسارة المحققة بالدولار (الكلي + اليوم)"""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        expr = "COALESCE(pnl_usdt, pnl_pct / 100.0 * ?)"
        cursor.execute("SELECT COALESCE(SUM({}), 0) FROM trades".format(expr), (TRADE_AMOUNT_USDT,))
        total = cursor.fetchone()[0] or 0.0
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        cursor.execute("SELECT COALESCE(SUM({}), 0) FROM trades WHERE timestamp LIKE ?".format(expr),
                       (TRADE_AMOUNT_USDT, today + "%"))
        today_val = cursor.fetchone()[0] or 0.0
        conn.close()
        return float(total), float(today_val)
    except Exception as e:
        print("Realized Stats Error: {}".format(e))
        return 0.0, 0.0


EQUITY_RANGES = {'1h': 3600, '8h': 8 * 3600, '1d': 86400, '1w': 7 * 86400, '1m': 30 * 86400, 'all': None}


def record_equity():
    """حفظ قيمة المحفظة الحالية (تُستدعى مع كل فحص)"""
    try:
        _, total = get_binance_balance()
        if total and total > 0:
            conn = sqlite3.connect(DB_NAME)
            conn.execute('INSERT OR REPLACE INTO equity (ts, value) VALUES (?, ?)', (int(time.time()), float(total)))
            conn.commit()
            conn.close()
    except Exception as e:
        print("Record Equity Error: {}".format(e))


def get_equity_series(range_key="1d", max_points=300):
    try:
        seconds = EQUITY_RANGES.get(range_key, 86400)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        if seconds:
            cursor.execute('SELECT ts, value FROM equity WHERE ts >= ? ORDER BY ts', (int(time.time()) - seconds,))
        else:
            cursor.execute('SELECT ts, value FROM equity ORDER BY ts')
        rows = cursor.fetchall()
        conn.close()
        if len(rows) > max_points:
            step = len(rows) / float(max_points)
            rows = [rows[int(i * step)] for i in range(max_points)] + [rows[-1]]
        return [{'t': r[0], 'v': r[1]} for r in rows]
    except Exception as e:
        print("Equity Series Error: {}".format(e))
        return []


def save_position(symbol, pos):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute('''
            INSERT OR REPLACE INTO open_positions
            (symbol, entry_price, amount, cost, highest_price, opened_at, simulated,
             strategy, timeframe, stop_price, target_price, trailing_distance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, pos['entry_price'], pos['amount'], pos['cost'],
              pos['highest_price'], pos['time'], 1 if pos.get('simulated') else 0,
              pos.get('strategy', 'trend'), pos.get('timeframe', '1h'),
              pos.get('stop_price'), pos.get('target_price'), pos.get('trailing_distance')))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Save Position Error: {}".format(e))


def delete_position(symbol):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute('DELETE FROM open_positions WHERE symbol = ?', (symbol,))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Delete Position Error: {}".format(e))


def load_positions():
    result = {}
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''SELECT symbol, entry_price, amount, cost, highest_price, opened_at,
                          simulated, strategy, timeframe, stop_price, target_price, trailing_distance
                          FROM open_positions''')
        for r in cursor.fetchall():
            result[r[0]] = {
                'entry_price': r[1], 'amount': r[2], 'cost': r[3],
                'highest_price': r[4], 'time': r[5], 'simulated': bool(r[6]),
                'strategy': r[7] or 'trend', 'timeframe': r[8] or '1h',
                'stop_price': r[9], 'target_price': r[10], 'trailing_distance': r[11],
            }
        conn.close()
    except Exception as e:
        print("Load Positions Error: {}".format(e))
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

exchange = make_binance_exchange(BINANCE_API_KEY, BINANCE_SECRET_KEY)
if BINANCE_ENV == "live":
    print("⚠️ [ENV] الاتصال بالحساب الحقيقي! الأوامر {}".format("مفعّلة" if TRADING_ENABLED else "معطّلة (محاكاة)"))
elif BINANCE_ENV not in ("demo", "testnet"):
    print("❌ [ENV] قيمة BINANCE_ENV غير معروفة: {} (استخدم demo أو testnet أو live)".format(BINANCE_ENV))

active_positions = load_positions()
market_snapshot = {}
last_entry_candle = {}   # آخر شمعة تمت محاولة الدخول عليها لكل عملة (لمنع تكرار الدخول على نفس الشمعة)
scan_lock = threading.Lock()
event_log = deque(maxlen=40)   # آخر الأحداث المهمة لعرضها في لوحة التحكم


def log_event(tag, text):
    event_log.appendleft({'t': datetime.datetime.now().strftime("%H:%M:%S"), 'tag': tag, 'text': text})
    if tag in ("FUTURES", "FUTURES_SCAN", "WARN", "ERR"):
        print("[{}] {}".format(tag, text), flush=True)
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
        print("Load Markets Error: {}".format(e))
    global futures_engine
    if BINANCE_ENV == 'demo' and BINANCE_FUTURES_API_KEY and BINANCE_FUTURES_SECRET_KEY:
        try:
            futures_engine = BinanceFuturesDemo(BINANCE_FUTURES_API_KEY, BINANCE_FUTURES_SECRET_KEY, DB_NAME, log_event)
            print('✅ [FUTURES DEMO] Active contracts: {}'.format(', '.join(futures_engine.symbols) or 'none'))
            if not futures_engine.symbols:
                log_event('WARN', 'Binance Futures Demo لا يعرض عقود CL/BZ/XAU المطلوبة')
        except Exception as e:
            futures_engine = None
            print('❌ [FUTURES DEMO] Connection unavailable ({})'.format(type(e).__name__))
            log_event('ERR', 'تعذر الاتصال بـ Binance Futures Demo')
    else:
        print('ℹ️ [FUTURES DEMO] بانتظار مفاتيح API الخاصة بـ Futures Demo')
    scheduler.add_job(automated_market_scan, 'interval', minutes=1, max_instances=1, coalesce=True)
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
_balance_cache = {'t': 0.0, 'v': (0.0, 0.0)}


def get_binance_balance(ttl=10):
    now = time.time()
    if now - _balance_cache['t'] < ttl:
        return _balance_cache['v']
    value = _get_binance_balance_uncached()
    if value != (0.0, 0.0):
        _balance_cache['t'] = now
        _balance_cache['v'] = value
    return value


def _get_binance_balance_uncached():
    try:
        balance = exchange.fetch_balance()
        free_usdt = float(balance.get('free', {}).get('USDT', 0.0) or 0.0)
        free_usdc = float(balance.get('free', {}).get('USDC', 0.0) or 0.0)
        total_usdt = float(balance.get('total', {}).get('USDT', 0.0) or 0.0) + float(balance.get('total', {}).get('USDC', 0.0) or 0.0)

        if 'info' in balance and 'balances' in balance['info']:
            total_usdt = 0.0
            for b in balance['info']['balances']:
                asset = b.get('asset')
                free_val = float(b.get('free', 0.0))
                locked_val = float(b.get('locked', 0.0))
                if asset == 'USDT':
                    free_usdt = free_val
                    total_usdt += free_val + locked_val
                elif asset == 'USDC':
                    free_usdc = free_val
                    total_usdt += free_val + locked_val

        # أضف قيمة العملات المحتفظ بها في الصفقات المفتوحة الفعلية
        for sym, pos in active_positions.items():
            if pos.get('simulated'):
                continue
            price = market_snapshot.get(sym, {}).get('price')
            if isinstance(price, (int, float)):
                total_usdt += pos['amount'] * price

        available_total = free_usdt if free_usdt > 0 else free_usdc
        return available_total, total_usdt if total_usdt > 0 else available_total
    except Exception as e:
        print("Balance Fetch Error details: {}".format(e))
        return 0.0, 0.0


# =====================================================================
# 3. المؤشرات الفنية (نفس دوال backtest.py)
# =====================================================================
def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_ema(closes, period=50):
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_macd(closes):
    if len(closes) < 35:
        return 0, 0, 0

    def get_ema_list(data, period):
        k = 2 / (period + 1)
        ema_list = [data[0]]
        for price in data[1:]:
            ema_list.append((price * k) + (ema_list[-1] * (1 - k)))
        return ema_list

    fast_list = get_ema_list(closes, 12)
    slow_list = get_ema_list(closes, 26)
    macd_line = [f - s for f, s in zip(fast_list, slow_list)]
    signal_list = get_ema_list(macd_line, 9)
    return macd_line[-1], signal_list[-1], macd_line[-1] - signal_list[-1]


# =====================================================================
# 4. تنفيذ الأوامر (شراء / بيع)
# =====================================================================
def _fees_in(order, currency):
    total = 0.0
    fees = order.get('fees') or ([order['fee']] if order.get('fee') else [])
    for f in fees:
        if f and f.get('currency') == currency and f.get('cost'):
            total += float(f['cost'])
    return total


def _complete_order(order, symbol):
    """تأكد أن الأمر نُفّذ بالكامل وفيه بيانات التنفيذ."""
    if order.get('status') != 'closed' or not order.get('cost'):
        time.sleep(1)
        order = exchange.fetch_order(order['id'], symbol)
    return order


def _parse_fill(order, symbol):
    base, quote = symbol.split('/')
    filled = float(order.get('filled') or 0)
    cost = float(order.get('cost') or 0)
    if filled <= 0 or cost <= 0:
        raise RuntimeError("الأمر لم يُنفَّذ (الحالة: {})".format(order.get('status')))
    avg = float(order.get('average') or (cost / filled))
    return filled, cost, avg, _fees_in(order, base), _fees_in(order, quote)


def place_buy(symbol, ref_price):
    """يرجع dict فيه: price, amount (الصافي بعد العمولة), cost, simulated"""
    if not TRADING_ENABLED:
        return {'price': ref_price, 'amount': TRADE_AMOUNT_USDT / ref_price,
                'cost': TRADE_AMOUNT_USDT, 'simulated': True}

    market = exchange.market(symbol)
    min_cost = ((market.get('limits') or {}).get('cost') or {}).get('min')
    if min_cost and TRADE_AMOUNT_USDT < float(min_cost):
        raise ValueError("حجم الصفقة {} أقل من الحد الأدنى {} USDT".format(TRADE_AMOUNT_USDT, min_cost))

    free_usdt = float(exchange.fetch_balance().get('free', {}).get('USDT', 0.0) or 0.0)
    if free_usdt < TRADE_AMOUNT_USDT:
        raise ValueError("الرصيد المتاح {:.2f} USDT أقل من حجم الصفقة".format(free_usdt))

    amount = float(exchange.amount_to_precision(symbol, TRADE_AMOUNT_USDT / ref_price))
    order = exchange.create_order(symbol, 'market', 'buy', amount)
    order = _complete_order(order, symbol)
    filled, cost, avg, base_fee, quote_fee = _parse_fill(order, symbol)
    return {'price': avg, 'amount': filled - base_fee, 'cost': cost + quote_fee, 'simulated': False}


def place_sell(symbol, pos):
    """يرجع dict فيه: price, proceeds (الصافي بعد العمولة)"""
    if not TRADING_ENABLED or pos.get('simulated'):
        price = exchange.fetch_ticker(symbol)['last']
        return {'price': price, 'proceeds': pos['amount'] * price}

    base = symbol.split('/')[0]
    free_base = float(exchange.fetch_balance().get('free', {}).get(base, 0.0) or 0.0)
    amount = float(exchange.amount_to_precision(symbol, min(pos['amount'], free_base)))
    if amount <= 0:
        raise ValueError("لا يوجد رصيد كافٍ من {} للبيع".format(base))

    order = exchange.create_order(symbol, 'market', 'sell', amount)
    order = _complete_order(order, symbol)
    filled, cost, avg, base_fee, quote_fee = _parse_fill(order, symbol)
    # الكمية التي لم تُبع بسبب تقريب Binance لخطوة الكمية (dust) ما زالت ملكاً لك في الحساب، فتُحسب بسعر الخروج
    dust = max(pos['amount'] - filled, 0.0)
    return {'price': avg, 'proceeds': cost - quote_fee + dust * avg}


# =====================================================================
# 4b. فلتر الأخبار (RSS + Groq)
# =====================================================================
news_state = {
    'sentiment': 'neutral', 'severity': 0, 'reason': 'لم يُفحص بعد', 'source': '',
    'checked_at': None, 'blocked_until': 0.0, 'headlines': [], 'headlines_ar': [], 'alerted': False, 'error': ''
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
        print("📰 [NEWS] خطأ: {}".format(e))


# =====================================================================
# 5. إدارة الصفقات والفحص الدوري
# =====================================================================
def check_and_close_positions():
    for symbol in list(active_positions.keys()):
        try:
            pos = active_positions[symbol]
            current_price = exchange.fetch_ticker(symbol)['last']
            entry_price = pos['entry_price']

            highest_price = max(pos.get('highest_price', entry_price), current_price)
            if highest_price != pos.get('highest_price'):
                pos['highest_price'] = highest_price
                save_position(symbol, pos)

            target = pos.get('target_price') or entry_price + TAKE_PROFIT_USDT / max(pos.get('amount') or 0, 1e-12)
            initial_stop = pos.get('stop_price') or entry_price * (1 - INITIAL_SL_PCT / 100)
            trailing_distance = pos.get('trailing_distance')
            active_stop = max(initial_stop, highest_price - trailing_distance) if trailing_distance else max(
                entry_price * (1 - INITIAL_SL_PCT / 100),
                highest_price * (1 - TRAILING_SL_PCT / 100)
            )

            reason = None
            if pos.get('test'):
                opened = datetime.datetime.strptime(pos['time'], "%Y-%m-%d %H:%M:%S")
                if (datetime.datetime.now() - opened).total_seconds() >= TEST_TRADE_SECONDS:
                    reason = "TEST"
            elif current_price >= target:
                reason = "TP"
            elif current_price <= active_stop:
                reason = "SL"

            if not reason:
                continue

            try:
                sale = place_sell(symbol, pos)
            except Exception as e:
                print("❌ [SELL ERROR] {}: {}".format(symbol, e))
                log_event("ERR", "بيع {}: {}".format(symbol, str(e)[:80]))
                if not pos.get('sell_error_notified'):
                    pos['sell_error_notified'] = True
                    send_telegram_message("❌ <b>فشل بيع {}</b>\n{}".format(symbol, html.escape(str(e))))
                continue

            realized_pnl = ((sale['proceeds'] - pos['cost']) / pos['cost']) * 100
            status = "WIN" if realized_pnl > 0 else "LOSS"
            title = {"TP": "🎯 تم تحقيق الهدف", "TEST": "🧪 إغلاق صفقة الاختبار"}.get(reason, "🛑 إغلاق بوقف الخسارة")
            print("{} [{}] {} | PnL: {:+.2f}% | {} ({})".format(
                title, status, symbol, realized_pnl, pos.get('strategy', 'trend'), pos.get('timeframe', '1h')))
            log_event({"TP": "TP", "TEST": "TEST"}.get(reason, "SL"),
                      "{} {:+.2f}% · {}".format(symbol, realized_pnl, pos.get('strategy', 'trend')))
            log_trade(symbol, entry_price, sale['price'], realized_pnl, status, sale['proceeds'] - pos['cost'])
            send_telegram_message(
                "{} ({})\n🪙 العملة: {}\n📥 سعر الدخول: {}\n📤 سعر الخروج: {}\n📊 النتيجة (بعد العمولة): {:+.2f}%\n🧭 الاستراتيجية: {} · {}{}".format(
                    title, status, symbol, entry_price, sale['price'], realized_pnl,
                    pos.get('strategy', 'trend'), pos.get('timeframe', '1h'),
                    "\n🧪 محاكاة" if (not TRADING_ENABLED or pos.get('simulated')) else ""))
            del active_positions[symbol]
            delete_position(symbol)
        except Exception as e:
            print("Error in position monitoring ({}): {}".format(symbol, e))


def automated_market_scan():
    if not scan_lock.acquire(blocking=False):
        print("⏳ [SCANNER] فحص آخر قيد التنفيذ، تم التخطي")
        return
    try:
        print("🔄 [SCANNER] فحص الاستراتيجيات الآلية الثلاث ({} | Demo-only safety)...".format(BINANCE_ENV))
        check_and_close_positions()

        for symbol in SYMBOLS:
            try:
                if symbol not in SUPPORTED_SYMBOLS:
                    continue
                setup = evaluate_symbol(exchange, symbol)
                current_price = exchange.fetch_ticker(symbol)['last']
                if not setup:
                    continue
                setup = dict(setup)
                setup.update({'price': current_price, 'time': datetime.datetime.now().strftime("%H:%M:%S")})
                market_snapshot[symbol] = setup
                print("📊 {} | {} {} | RSI: {:.2f} | strength: {}/100 | setup: {}".format(
                    symbol, setup['strategy'], setup['timeframe'], setup['rsi'],
                    setup.get('signal_strength', 0), setup['reason']))

                if symbol not in active_positions and len(active_positions) >= MAX_SPOT_CONCURRENT_TRADES:
                    continue

                global test_trade_done
                if FORCE_TEST_TRADE and not test_trade_done and symbol not in active_positions:
                    test_trade_done = True
                    try:
                        fill = place_buy(symbol, current_price)
                        pos = {
                            'entry_price': fill['price'], 'amount': fill['amount'],
                            'cost': fill['cost'], 'highest_price': fill['price'],
                            'time': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            'simulated': fill['simulated'], 'test': True,
                            'strategy': 'test', 'timeframe': setup.get('timeframe', '15m'),
                        }
                        active_positions[symbol] = pos
                        save_position(symbol, pos)
                        print("🧪 [TEST BUY] {} بسعر {}".format(symbol, fill['price']))
                        send_telegram_message("🧪 <b>صفقة اختبار (BUY)</b>\n🪙 {}\n💵 {}\n⏱ ستُغلق بعد {} ثانية".format(
                            symbol, fill['price'], TEST_TRADE_SECONDS))
                    except Exception as e:
                        print("❌ [TEST BUY ERROR] {}: {}".format(symbol, e))
                        send_telegram_message("❌ <b>فشل شراء الاختبار</b>\n{}".format(html.escape(str(e))))
                    continue

                if symbol in active_positions or not setup['signal']:
                    continue
                if last_entry_candle.get(symbol) == setup.get('candle_ts'):
                    continue

                if news_blocked():
                    print("📰 [NEWS] إشارة {} على {} لكن الشراء موقوف بسبب الأخبار".format(
                        setup['strategy'], symbol))
                    continue
                if (news_state['sentiment'] == 'negative' and news_state['severity'] >= 2
                        and setup['strategy'] != 'trend'):
                    print("📰 [NEWS] خبر سلبي؛ السماح فقط بإشارات الاتجاه وتجاهل {} على {}".format(
                        setup['strategy'], symbol))
                    continue

                last_entry_candle[symbol] = setup.get('candle_ts')
                try:
                    fill = place_buy(symbol, current_price)
                except Exception as e:
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
                }
                active_positions[symbol] = pos
                save_position(symbol, pos)
                print("🚀 [BUY] {} | {} {} | price {} | SL {} | TP {}".format(
                    symbol, setup['strategy'], setup['timeframe'], entry, stop_price, target_price))
                log_event("BUY", "{} · {} · {}".format(symbol, setup['strategy'], setup['timeframe']))
                send_telegram_message(
                    "🚀 <b>فتح صفقة Demo جديدة</b>\n🪙 {}\n🧭 {} · {}\n💵 الدخول: {}\n🛑 الوقف: {}\n🎯 الهدف: {}\n📦 القيمة: {:.2f} USDT{}".format(
                        symbol, setup['strategy_label'], setup['timeframe'], entry,
                        stop_price, target_price, fill['cost'],
                        "\n🧪 محاكاة" if fill['simulated'] else ""))
            except Exception as e:
                print("Error scanning {}: {}".format(symbol, e))
        if futures_engine and BINANCE_ENV == 'demo' and TRADING_ENABLED:
            futures_engine.scan(
                TRADE_AMOUNT_USDT,
                lambda: len(futures_engine.positions) < MAX_FUTURES_CONCURRENT_TRADES,
                force_test_trade=FORCE_FUTURES_TEST_TRADE,
                force_test_id=FORCE_FUTURES_TEST_ID,
                force_test_side=FORCE_FUTURES_TEST_SIDE,
            )
        record_equity()
    except Exception as e:
        print("Error in automated market scan: {}".format(e))
    finally:
        scan_lock.release()


# =====================================================================
# 6. لوحة التحكم (صفحة واحدة تقرأ البيانات الحية من /api/state)
# =====================================================================
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_dashboard_auth)])
def read_root():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except Exception as e:
        return HTMLResponse(content="<h1>Error loading index.html: {}</h1>".format(html.escape(str(e))))


@app.get("/api/state", dependencies=[Depends(require_dashboard_auth)])
def api_state():
    total, wins, losses, win_rate, total_pnl, best, worst = get_performance_stats()
    realized_total, realized_today = get_realized_stats()
    free_usdt, total_usdt = get_binance_balance()

    positions = []
    invested = 0.0
    unrealized = 0.0
    for sym, pos in active_positions.items():
        entry = pos['entry_price']
        high = pos.get('highest_price', entry)
        price = market_snapshot.get(sym, {}).get('price')
        price = price if isinstance(price, (int, float)) else entry
        pnl_usdt = pos['amount'] * price - pos['cost']
        invested += pos['cost']
        unrealized += pnl_usdt
        positions.append({
            'symbol': sym, 'entry': entry, 'amount': pos['amount'], 'cost': pos['cost'],
            'highest': high, 'price': price, 'pnl_usdt': pnl_usdt,
            'pnl_pct': pnl_usdt / pos['cost'] * 100 if pos['cost'] else 0.0,
            'stop': max(pos.get('stop_price') or entry * (1 - INITIAL_SL_PCT / 100),
                        high - pos.get('trailing_distance') if pos.get('trailing_distance') else high * (1 - TRAILING_SL_PCT / 100)),
            'target': pos.get('target_price') or entry + TAKE_PROFIT_USDT / max(pos.get('amount') or 0, 1e-12),
            'strategy': pos.get('strategy', 'trend'), 'timeframe': pos.get('timeframe', '1h'),
            'simulated': bool(pos.get('simulated')), 'time': pos.get('time', '')
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
            'time': d['time'], 'in_position': sym in active_positions,
            'strategy': d.get('strategy', 'wait'), 'strategy_label': d.get('strategy_label', 'انتظار'),
            'timeframe': d.get('timeframe', '15m'), 'signal_strength': d.get('signal_strength', 0),
            'reason': d.get('reason', '')
        })

    if futures_engine:
        for sym, pos in futures_engine.positions.items():
            price = float((futures_engine.snapshot.get(sym) or {}).get('price') or pos['entry_price'])
            side = pos.get('side', 'long')
            direction = -1.0 if side == 'short' else 1.0
            pnl = direction * (price - pos['entry_price']) * pos['contracts'] * float(futures_engine.exchange.market(sym).get('contractSize') or 1)
            positions.append({'symbol': sym, 'entry': pos['entry_price'], 'amount': pos['amount'],
                'cost': pos['cost'], 'highest': pos['highest_price'], 'price': price, 'pnl_usdt': pnl,
                'pnl_pct': pnl / pos['cost'] * 100 if pos['cost'] else 0, 'stop': pos['stop_price'],
                'target': pos['target_price'], 'strategy': pos['strategy'], 'timeframe': pos['timeframe'],
                'side': side, 'simulated': False, 'time': pos['opened_at'], 'market_type': 'Futures Demo'})
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
                    'reason': data.get('reason', ''), 'market_type': 'Futures Demo'})
            else:
                markets.append({'symbol': sym, 'ready': False, 'supported': True,
                    'in_position': sym in futures_engine.positions, 'market_type': 'Futures Demo',
                    'reason': 'بانتظار أول فحص Futures'})
    trades = [{
        'id': t[0], 'symbol': t[1], 'entry': t[2], 'exit': t[3], 'pnl_pct': t[4],
        'status': t[5], 'time': t[6], 'pnl_usdt': t[7]
    } for t in get_recent_trades(limit=8)]
    if futures_engine:
        try:
            with futures_engine._connect() as conn:
                rows = conn.execute('SELECT id, symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, side FROM futures_trades ORDER BY id DESC LIMIT 8').fetchall()
            trades.extend({'id': 'f-' + str(t[0]), 'symbol': t[1], 'entry': t[2], 'exit': t[3], 'pnl_pct': t[4],
                'status': t[5], 'time': t[6], 'pnl_usdt': t[7], 'side': t[8], 'market_type': 'Futures Demo'} for t in rows)
            trades = sorted(trades, key=lambda t: t['time'], reverse=True)[:8]
        except Exception:
            pass

    future_state = futures_engine.public_state() if futures_engine else {
        'connected': False, 'environment': 'demo', 'trading_enabled': False, 'symbols': [],
        'open_positions': 0, 'balance': 0.0, 'free_balance': 0.0, 'closed': 0,
        'wins': 0, 'losses': 0, 'realized_pnl': 0.0}
    return {
        'futures': future_state,
        'env': BINANCE_ENV,
        'trading_enabled': TRADING_ENABLED,
        'telegram': bool(TELEGRAM_BOT_TOKEN),
        'trade_amount': TRADE_AMOUNT_USDT,
        'settings': {
            'spot_max_concurrent_trades': MAX_SPOT_CONCURRENT_TRADES,
            'spot_active_trades': len(active_positions),
            'futures_max_concurrent_trades': MAX_FUTURES_CONCURRENT_TRADES,
            'futures_active_trades': len(futures_engine.positions) if futures_engine else 0,
            'active_trades': len(active_positions) + (len(futures_engine.positions) if futures_engine else 0),
        },
        'wallet': {'total': total_usdt, 'free': free_usdt},
        'stats': {
            'realized_total': realized_total, 'realized_today': realized_today,
            'unrealized': unrealized, 'wins': wins, 'losses': losses, 'total': total,
            'win_rate': win_rate, 'best': best, 'worst': worst,
            'exposure': min(invested / total_usdt * 100, 100.0) if total_usdt > 0 else 0.0
        },
        'params': {
            'timeframe': 'auto', 'strategy_mode': 'automatic',
            'strategies': ['trend', 'breakout', 'bounce'],
            'ema_period': EMA_PERIOD, 'rsi_max': RSI_MAX_ENTRY,
            'require_macd': REQUIRE_MACD, 'macd_tolerance_pct': MACD_HIST_TOLERANCE_PCT, 'tp_usdt': TAKE_PROFIT_USDT, 'sl': INITIAL_SL_PCT,
            'trail': TRAILING_SL_PCT, 'news_severity': NEWS_BLOCK_SEVERITY,
            'news_pause_h': NEWS_PAUSE_HOURS
        },
        'markets': markets,
        'positions': positions,
        'trades': trades,
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
        'events': list(event_log)
    }



@app.post("/api/settings", dependencies=[Depends(require_dashboard_auth)])
def update_runtime_settings(payload: RuntimeSettings):
    global TRADE_AMOUNT_USDT, MAX_SPOT_CONCURRENT_TRADES, MAX_FUTURES_CONCURRENT_TRADES
    if not scan_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Market scan in progress; retry shortly")
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.executemany('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', [
            ('trade_amount_usdt', str(payload.trade_amount_usdt)),
            ('spot_max_concurrent_trades', str(payload.spot_max_concurrent_trades)),
            ('futures_max_concurrent_trades', str(payload.futures_max_concurrent_trades))
        ])
        conn.commit()
        conn.close()
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
    finally:
        scan_lock.release()


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
            candidate = make_binance_exchange(payload.api_key.strip(), payload.secret_key.strip())
            candidate.load_markets()
            candidate.fetch_balance()
        except Exception:
            raise HTTPException(status_code=400, detail="Binance Demo could not verify these credentials")
        cipher = _credential_cipher()
        conn = sqlite3.connect(DB_NAME)
        conn.execute('INSERT OR REPLACE INTO platform_credentials (platform, api_key_enc, secret_enc, updated_at) VALUES (?, ?, ?, ?)',
                     ('binance', cipher.encrypt(payload.api_key.strip().encode()).decode(), cipher.encrypt(payload.secret_key.strip().encode()).decode(), datetime.datetime.now().isoformat(timespec='seconds')))
        conn.commit()
        conn.close()
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
            candidate = BinanceFuturesDemo(payload.api_key.strip(), payload.secret_key.strip(), DB_NAME, log_event)
            balance_check = candidate.balance()
            if balance_check.get('error'):
                raise RuntimeError('Futures API authentication failed')
            if not candidate.symbols:
                raise RuntimeError('No supported oil or gold contracts in Futures Demo markets')
        except Exception:
            raise HTTPException(status_code=400, detail='تعذر التحقق من Binance Futures Demo أو أن عقود النفط/الذهب غير متاحة في حساب الديمو')
        cipher = _credential_cipher()
        conn = sqlite3.connect(DB_NAME)
        conn.execute('INSERT OR REPLACE INTO platform_credentials (platform, api_key_enc, secret_enc, updated_at) VALUES (?, ?, ?, ?)',
            ('binance_futures', cipher.encrypt(payload.api_key.strip().encode()).decode(), cipher.encrypt(payload.secret_key.strip().encode()).decode(), datetime.datetime.now().isoformat(timespec='seconds')))
        conn.commit(); conn.close()
        futures_engine = candidate
        BINANCE_FUTURES_API_KEY = payload.api_key.strip()
        BINANCE_FUTURES_SECRET_KEY = payload.secret_key.strip()
        log_event('PLATFORM', 'تم ربط Binance Futures Demo')
        return {'ok': True, 'platform': 'binance_futures', 'environment': 'demo', 'symbols': candidate.symbols,
            'message': 'Binance Futures Demo credentials saved encrypted'}
    finally:
        scan_lock.release()


@app.get("/api/equity", dependencies=[Depends(require_dashboard_auth)])
def api_equity(range: str = "1d"):
    return get_equity_series(range)


@app.get("/check", dependencies=[Depends(require_dashboard_auth)])
def manual_check():
    automated_market_scan()
    return RedirectResponse(url="/", status_code=303)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
