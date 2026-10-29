"""Environment-driven configuration for the trading bot (moved verbatim from main.py)."""
import os

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
TAKE_PROFIT_USDT = float(os.getenv("TAKE_PROFIT_USDT", "2"))  # قديم: لم يعد يُستخدم كهدف (للتوافق فقط)
# هدف الربح كمضاعف لمسافة وقف الخسارة (R). 2 = الهدف ضعف المخاطرة
TP_R_MULTIPLE = float(os.getenv("TP_R_MULTIPLE", "2"))
if not 0.5 <= TP_R_MULTIPLE <= 10:
    raise ValueError("TP_R_MULTIPLE must be between 0.5 and 10")
# فاصل فحص السوق بالدقائق (الفحص يستغرق ~60 ثانية، فدقيقة واحدة تسبب تخطي نصف الفحوص)
SCAN_INTERVAL_MINUTES = max(1, int(os.getenv("SCAN_INTERVAL_MINUTES", "3")))
# قيم حجم الصفقة القديمة الصغيرة التي تُرحَّل مرة واحدة إلى الحد الافتراضي الجديد
LEGACY_TRADE_AMOUNTS = (100.0, 150.0)
# أقصى نقص في رصيد Spot (٪) يُعتبر رسوماً مخصومة من العملة نفسها (مثل BNB) فيُعتمد الرصيد الفعلي تلقائياً
SPOT_RECONCILE_ADOPT_PCT = float(os.getenv("SPOT_RECONCILE_ADOPT_PCT", "15"))
# عدد خيوط جلب الشموع المتوازي أثناء الفحص
SCAN_FETCH_WORKERS = max(1, min(8, int(os.getenv("SCAN_FETCH_WORKERS", "5"))))
INITIAL_SL_PCT = 0.5       # وقف الخسارة الأولي %
TRAILING_SL_PCT = 0.3      # وقف الخسارة المتحرك %

SYMBOLS = ["BTC/USDT","ETH/USDT","BNB/USDT","SOL/USDT","XRP/USDT","ADA/USDT","DOGE/USDT","LINK/USDT","AVAX/USDT","DOT/USDT","LTC/USDT","BCH/USDT","TRX/USDT","NEAR/USDT","SPCXB/USDT"]
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
