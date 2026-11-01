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
