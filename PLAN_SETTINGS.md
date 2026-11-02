# إعدادات خطة تحسين الجودة (كلها اختيارية؛ القيم الافتراضية تحافظ على السلوك السابق ما لم يُذكر غير ذلك)

## A — مكاسب سريعة
| المتغير | الافتراضي | الوصف |
|---|---|---|
| `MAX_POSITION_EQUITY_PCT` | `0` (معطّل) | سقف الصفقة = نسبة من رأس المال بدل مبلغ الصفقة الثابت؛ يجعل `RISK_PER_TRADE_PCT` هو المتحكم الفعلي. مقترح: `15` مع `RISK_PER_TRADE_PCT=0.75` |
| `BOUNCE_TIMEFRAMES` | `15m,5m` | أطر الارتداد. مقترح: `15m` |
| `MIN_TARGET_COST_MULTIPLE` | `0` | رفض إشارة هدفها أقل من k × تكلفة الذهاب والعودة. مقترح: `3` |
| `ROUND_TRIP_COST_PCT` | `0.25` | التكلفة المفترضة (رسوم+انزلاق) بالنسبة المئوية |
| `RANGE_MAX_EMA_SLOPE` / `RANGE_MAX_EMA_DISTANCE` | `0.006` / `0.025` | شروط سوق النطاق |
| `BREAKOUT_VOLUME_MULTIPLE` | `1.25` | مضاعف الحجم للاختراق |
| `DISABLED_STRATEGIES` | فارغ | استراتيجيات تُعطّل (مفصولة بفواصل) |
| `SPOT_DUST_MAX_USDT` | `1.0` | بقايا Spot أصغر من هذا (وأصغر من حد Binance) تُزال من المتابعة بدل تجميد الزوج للأبد |
| `FUTURES_MONITOR_FREEZE_SCOPE` | `symbol` | فشل متابعة مركز Futures يجمّد ذلك الرمز فقط (`global` = السلوك القديم) |
| `FUTURES_MONITOR_MAX_FAILED_SYMBOLS` | `2` | إذا فشلت متابعة أكثر من هذا العدد يتوقف كل الدخول |
| `FUTURES_STOP_REPLACE_MODE` | `cancel_then_create` | إصلاح خطأ Binance ‎-4130 عند تشديد وقف closePosition، مع استعادة الوقف القديم عند الفشل |
| `FUTURES_ADOPT_EXISTING_CLOSE_STOP` | `true` | اعتماد وقف closePosition موجود بدل محاولة إنشاء مكرر |
| `FUTURES_HEDGE_ONLY_MODE` | `hedge` | `all` يسمح لعقود العملات الرقمية بكل الاستراتيجيات |

## B — التنفيذ والرسوم
| المتغير | الافتراضي | الوصف |
|---|---|---|
| `ENTRY_ORDER_MODE` | `market` | `limit_then_market`: أمر دخول محدد post-only (GTX في Futures) عند أفضل سعر، ثم سوق للباقي بعد المهلة. أوامر الوقف والخروج تبقى سوقًا دائمًا |
| `LIMIT_ENTRY_TIMEOUT_SECONDS` | `20` | مهلة انتظار التنفيذ كـ Maker |
| `LIMIT_ENTRY_POLL_SECONDS` | `2` | فترة الاستعلام |
| `LIMIT_ENTRY_MAX_CHASE_PCT` | `0.3` | لا يُرسل أمر سوق إن ابتعد السعر أكثر من هذا (تُلغى الصفقة بدل مطاردة السعر) |

الرسوم الفعلية تُقرأ من التنفيذ (fees) ومن `fetch_order_trades` عند غيابها. جدول الرسوم الافتراضي في `execution.FEE_SCHEDULE`.

## C — الاستراتيجية والمخاطرة
| المتغير | الافتراضي | الوصف |
|---|---|---|
| `REGIME_FILTER` | `off` | `adx`: الاتجاه يتطلب ADX ≥ `ADX_TREND_MIN`؛ سوق النطاق (الارتداد/حجم التداول) يُعرّف بـ ADX(4h) < `ADX_RANGE_MAX` بدل شرط ميل EMA الضيق؛ والاختراق يُقبل أيضًا عند ADX(4h) قوي مع اتجاه 4h متوافق |
| `ADX_PERIOD` / `ADX_TREND_MIN` / `ADX_RANGE_MAX` | `14` / `20` / `20` | |
| `MTF_CONFIRM` | `false` | إشارات الاتجاه على 4h تتطلب توافق اتجاه اليومي (EMA20 مقابل EMA50) |
| `VOL_TARGET_STOP_PCT` | `0` | تحجيم بحسب التقلب: المخاطرة × (الهدف ÷ نسبة الوقف) بين `VOL_SCALE_MIN` و`VOL_SCALE_MAX` (الأقصى 1.0 افتراضيًا: لا تزيد المخاطرة أبدًا) |
| `PARTIAL_TP_R` / `PARTIAL_TP_FRACTION` | `1.0` / `0.5` | الجني الجزئي ونسبته؛ بعده يبقى الوقف عند التعادل كما كان |
| `ENABLE_DONCHIAN_TREND` | `false` | استراتيجية اتجاه دونشيان متعددة الفترات `DONCHIAN_LOOKBACKS=20,40,55` على 1d/4h |
| `MAX_POSITIONS_PER_CLUSTER` | `0` | حد عدد المراكز في مجموعة مترابطة (crypto/commodities/us_equities) فوق حدود المخاطرة الحالية 8%/4% |

فلتر التمويل (Funding) موجود مسبقًا ولم يتغير.

## PR D — التحقق والقياس (R-multiple + إحصاءات الاستراتيجيات)
لا إعدادات جديدة. التغييرات قراءة/تسجيل فقط:
- عمودان جديدان `risk_usdt` و`r_multiple` في `trades` و`futures_trades` (ترحيل تلقائي). المخاطرة الأولية = الكمية × مسافة الوقف الأولى.
- `GET /api/strategy-stats` (يحتاج كلمة مرور اللوحة): لكل سوق/استراتيجية عدد الصفقات، نسبة الفوز، الصافي، Profit Factor، الرسوم، Expectancy بوحدة R، وتحذير إذا العينة < 30. الصفقات المحاكاة مستبعدة.
- الباك-تست يضيف `r_multiple` لكل صفقة، و`expectancy_r` و`by_strategy` في المقاييس.

## التعلم المعزز (Contextual Bandit)

| الإعداد | الافتراضي | الوصف |
|---|---|---|
| `RL_MODE` | `off` | `off` لا شيء · `shadow` يسجّل قراره ويتعلّم دون تغيير التداول · `live` يطبّق التخطي/نصف الحجم |
| `RL_MIN_SAMPLES` | `30` | عدد الصفقات المغلقة لكل سياق قبل أن يُسمح له بالتأثير في وضع live |
| `RL_EXPLORATION` | `0.1` | احتمال أخذ الصفقة بنصف الحجم بدل تخطيها (حتى يستمر التعلّم) |
| `RL_FULL_ABOVE_R` | `0.15` | فوق هذا الـ R المتوقع يأخذ الحجم الكامل |
| `RL_SKIP_BELOW_R` | `0` | تحت هذا يتخطى الصفقة |
| `RL_PRIOR_STRENGTH` | `3` | قوة الافتراض المبدئي (R=0) بعدد صفقات وهمية |

- لا يرفع الحجم أبدًا فوق ما تسمح به قواعد المخاطرة (المضاعف دائمًا بين 0 و 1).
- الحالة محفوظة في جدولي `rl_arms` و `rl_decisions` داخل قاعدة SQLite.
- `GET /api/rl` للقراءة فقط، وتبويب «التعلم المعزز» في لوحة التحكم.
- مقارنة تاريخية: `python rl_backtest.py --market futures --days 60`.

## Finnhub (news + calendars)

Module `finnhub_client.py`. Enabled automatically when `FINNHUB_API_KEY` is set.

| Setting | Default | Meaning |
|---|---|---|
| `FINNHUB_API_KEY` | empty | Finnhub key (free tier works) |
| `FINNHUB_ENABLED` | `true` (if key set) | Master switch |
| `FINNHUB_EVENT_PAUSE_MIN` | `30` | Pause **new entries** N minutes before/after high-impact US events (CPI, FOMC, NFP…). `0` = off. Needs `/calendar/economic` (premium; free tier returns 403 → logged once, feature inactive) |
| `FINNHUB_EARNINGS_BLOCK_H` | `24` | Block new entries on a TradFi stock perp N hours around its earnings. `0` = off |
| `FINNHUB_STOCKS` | `META,NVDA,TSLA,AMZN,MU` | Stocks used for company news + earnings |
| `FINNHUB_MAX_CALLS_PER_MIN` | `30` | Local rate limit (free tier = 60/min) |
| `FINNHUB_EVENT_KEYWORDS` | cpi,fomc,nonfarm,… | Event names treated as high impact |

- News: `/news?category=crypto|general` + `/company-news` (max 3 per stock) are merged into the RSS headlines, deduped, then go through the existing LLM/keyword filter. Cached 15–30 min.
- Calendars refresh hourly. Existing positions are never touched.
- Read-only status: `GET /api/finnhub`. Shown in the dashboard News tab; block reasons appear in bot decisions.
- Free-tier check (2026-11-02): news ✅, company-news ✅, earnings ✅, economic ❌ 403.

## Massive (Polygon.io) — TradFi underlyings

Module `massive_client.py`. Free plan verified: market status (`/v1/marketstatus/now`, `/upcoming`), ticker news with sentiment (`/v2/reference/news`), daily aggregates (`/v2/aggs`). `/v2/snapshot` returns 403 on free plan and is not used. A background job every 2 min refreshes stale data within the rate budget; scan gates read only the cache, so scans are never slowed. 403 disables that endpoint for the session; 429 backs off for a minute.

| Setting | Default | Meaning |
|---|---|---|
| `MASSIVE_API_KEY` | — | Required to enable |
| `MASSIVE_ENABLED` | true | Master switch (needs key) |
| `MASSIVE_MAX_CALLS_PER_MIN` | 4 | Free tier is 5/min |
| `MASSIVE_STOCKS` | META,NVDA,TSLA,AMZN,MU | Underlyings tracked |
| `MASSIVE_NEWS_ENABLED` | true | Merge ticker news into news pipeline (deduped) |
| `MASSIVE_MARKET_HOURS_GUARD` | false | Block new entries on stock perps while US market is not `open` (pre/after-hours, weekends, holidays) |
| `MASSIVE_OPEN_BUFFER_MIN` | 15 | With guard on: also block first N minutes after open |
| `MASSIVE_UNDERLYING_CONFIRM` | false | Long only if stock close > SMA, short only if below |
| `MASSIVE_SMA_DAYS` | 50 | SMA length for confirm |

Endpoints: `GET /api/massive`, and `massive` key in `/api/state`.
