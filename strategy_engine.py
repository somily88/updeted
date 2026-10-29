"""Deterministic algorithmic strategy selector for Binance Demo.

Signal strength is a heuristic ranking, not a probability of profit.
"""
import os
import time
from math import sqrt

TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", "1.0"))
if not 0.0 < TRAILING_ACTIVATION_PCT <= 5.0:
    raise ValueError("TRAILING_ACTIVATION_PCT must be greater than 0 and at most 5")

MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))
if not 0.1 <= MAX_DAILY_LOSS_PCT <= 25.0:
    raise ValueError("MAX_DAILY_LOSS_PCT must be between 0.1 and 25")
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "2.0"))
if not 0.0 < RISK_PER_TRADE_PCT <= 2.0:
    raise ValueError("RISK_PER_TRADE_PCT must be greater than 0 and at most 2")

MIN_SPOT_QUOTE_VOLUME_USDT = float(os.getenv("MIN_SPOT_QUOTE_VOLUME_USDT", "1000000"))
MIN_FUTURES_QUOTE_VOLUME_USDT = float(os.getenv("MIN_FUTURES_QUOTE_VOLUME_USDT", "100000"))
MAX_SPOT_SPREAD_PCT = float(os.getenv("MAX_SPOT_SPREAD_PCT", "0.15"))
MAX_FUTURES_SPREAD_PCT = float(os.getenv("MAX_FUTURES_SPREAD_PCT", "0.25"))
MAX_TICKER_AGE_SECONDS = float(os.getenv("MAX_TICKER_AGE_SECONDS", "120"))
if not 5.0 <= MAX_TICKER_AGE_SECONDS <= 900.0:
    raise ValueError("MAX_TICKER_AGE_SECONDS must be between 5 and 900")
MAX_SPOT_HEDGE_RATIO = float(os.getenv("MAX_SPOT_HEDGE_RATIO", "0.5"))
if not 0.0 < MAX_SPOT_HEDGE_RATIO <= 1.0:
    raise ValueError("MAX_SPOT_HEDGE_RATIO must be greater than 0 and at most 1")


def retry_api_read(operation, *args, attempts=3, **kwargs):
    """Retry only transient read errors; never use this wrapper for order creation."""
    for attempt in range(max(1, int(attempts))):
        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            name = type(exc).__name__.lower()
            transient = any(token in name for token in (
                "network", "timeout", "ratelimit", "ddos", "exchangeunavailable", "internalerror"
            )) or getattr(exc, "status_code", None) in (429, 500, 502, 503, 504)
            if not transient or attempt + 1 >= attempts:
                raise
            time.sleep(min(0.25 * (2 ** attempt), 1.0))


def fetch_fresh_ticker(exchange, symbol):
    """Fetch a ticker and reject it when the exchange supplies an old timestamp."""
    ticker = retry_api_read(exchange.fetch_ticker, symbol)
    timestamp = ticker.get("timestamp")
    if timestamp is not None:
        age_seconds = (time.time() * 1000.0 - float(timestamp)) / 1000.0
        if age_seconds > MAX_TICKER_AGE_SECONDS:
            raise TimeoutError("Ticker for {} is stale ({:.0f}s)".format(symbol, age_seconds))
    price = float(ticker.get("last") or 0.0)
    if price <= 0:
        raise ValueError("Ticker for {} has no usable last price".format(symbol))
    return ticker


def market_liquidity_check(exchange, symbol, market_type):
    """Fail closed on thin 24h volume or an excessively wide live spread."""
    ticker = retry_api_read(exchange.fetch_ticker, symbol)
    price = float(ticker.get("last") or 0.0)
    quote_volume = float(ticker.get("quoteVolume") or 0.0)
    if quote_volume <= 0:
        base_volume = float(ticker.get("baseVolume") or 0.0)
        quote_volume = base_volume * price
    min_volume = (MIN_FUTURES_QUOTE_VOLUME_USDT if market_type == "Futures Demo"
                  else MIN_SPOT_QUOTE_VOLUME_USDT)
    if price <= 0 or quote_volume < min_volume:
        return False, "حجم 24 ساعة أقل من الحد الأدنى ({:.0f} USDT)".format(min_volume)

    bid = float(ticker.get("bid") or 0.0)
    ask = float(ticker.get("ask") or 0.0)
    if bid <= 0 or ask <= 0 or ask < bid:
        book = retry_api_read(exchange.fetch_order_book, symbol, limit=5)
        bids, asks = book.get("bids") or [], book.get("asks") or []
        bid = float(bids[0][0]) if bids else 0.0
        ask = float(asks[0][0]) if asks else 0.0
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, "تعذر التحقق من فرق سعر العرض والطلب"
    spread_pct = (ask - bid) / ((ask + bid) / 2.0) * 100.0
    max_spread = MAX_FUTURES_SPREAD_PCT if market_type == "Futures Demo" else MAX_SPOT_SPREAD_PCT
    if spread_pct > max_spread:
        return False, "فرق السعر {:.3f}% يتجاوز الحد {:.3f}%".format(spread_pct, max_spread)
    return True, "سيولة مناسبة · حجم 24 ساعة {:.0f} USDT · فرق {:.3f}%".format(
        quote_volume, spread_pct)



def risk_sized_notional(equity, entry_price, stop_distance, max_notional,
                        risk_percent=RISK_PER_TRADE_PCT):
    """Size a position so a stop hit risks at most risk_percent of equity,
    while keeping max_notional as a hard cap.
    """
    equity = float(equity)
    entry_price = float(entry_price)
    stop_distance = float(stop_distance)
    max_notional = float(max_notional)
    risk_percent = float(risk_percent)
    if (equity <= 0 or entry_price <= 0 or stop_distance <= 0
            or max_notional <= 0 or not 0 < risk_percent <= 2.0):
        raise ValueError("Invalid equity, stop, price, notional cap, or risk percent")
    risk_budget = equity * risk_percent / 100.0
    stop_fraction = stop_distance / entry_price
    return min(max_notional, risk_budget / stop_fraction)


STRATEGY_RISK = {
    "trend": {"stop_atr": 1.5, "target_atr": 3.0, "trail_atr": 1.5},
    "breakout": {"stop_atr": 1.0, "target_atr": 2.0, "trail_atr": 1.0},
    "bounce": {"stop_atr": 1.0, "target_atr": 1.5, "trail_atr": 0.75},
    "macro_momentum_hybrid": {"stop_atr": 1.5, "target_atr": 2.7, "trail_atr": 1.25},
    "algorithmic_discretionary_hybrid": {"stop_atr": 1.25, "target_atr": 2.2, "trail_atr": 1.0},
    "volume_profile_range": {"stop_atr": 0.9, "target_atr": 1.4, "trail_atr": 0.7},
    "volatility_breakout_hedge": {"stop_atr": 1.1, "target_atr": 2.0, "trail_atr": 1.0},
}
STRATEGY_LABELS = {
    "trend": "تتبّع الاتجاه", "breakout": "الاختراق", "bounce": "الارتداد",
    "macro_momentum_hybrid": "هجين الزخم الكلي",
    "algorithmic_discretionary_hybrid": "خوارزمي مع سياق الأخبار",
    "volume_profile_range": "نطاق حجم التداول",
    "volatility_breakout_hedge": "اختراق تقلب مع تحوّط",
}


def _ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    out = [None] * (period - 1) + [value]
    for price in values[period:]:
        value = (price - value) * alpha + value
        out.append(value)
    return out


def _rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(x, 0.0) for x in changes[:period]) / period
    loss = sum(max(-x, 0.0) for x in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / period


def _bands(values, period=20, deviations=2.0):
    if len(values) < period:
        return None, None, None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    width = deviations * sqrt(variance)
    return mean - width, mean, mean + width


def _macd_hist(values):
    fast, slow = _ema(values, 12), _ema(values, 26)
    if not fast or not slow:
        return 0.0
    macd = [f - s for f, s in zip(fast, slow) if f is not None and s is not None]
    signal = _ema(macd, 9)
    return macd[-1] - signal[-1] if signal and signal[-1] is not None else 0.0


def _load(exchange, symbol, timeframe, cache):
    if timeframe not in cache:
        candles = retry_api_read(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=90)
        cache[timeframe] = candles[:-1]
    return cache[timeframe]


PREFETCH_TIMEFRAMES = ("1d", "4h", "1h", "15m", "5m")


def prefetch_ohlcv(exchange, symbols, timeframes=PREFETCH_TIMEFRAMES, workers=5):
    """Fetch candles for many symbols in parallel; returns {symbol: cache}.

    Only read-only OHLCV requests run in the pool (ccxt enableRateLimit still
    throttles them). Failed fetches are left out so evaluate_symbol retries lazily.
    """
    from concurrent.futures import ThreadPoolExecutor
    caches = {symbol: {} for symbol in symbols}
    jobs = [(symbol, tf) for symbol in symbols for tf in timeframes]

    def fetch(job):
        symbol, tf = job
        try:
            return job, retry_api_read(exchange.fetch_ohlcv, symbol, timeframe=tf, limit=90)
        except Exception:
            return job, None

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        for (symbol, tf), candles in pool.map(fetch, jobs):
            if candles:
                caches[symbol][tf] = candles[:-1]
    return caches


def _view(candles):
    if len(candles) < 55:
        return None
    closes = [c[4] for c in candles]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    return {
        "candles": candles, "closes": closes, "ema20": ema20[-1],
        "ema50": ema50[-1], "ema50_old": ema50[-6], "rsi": _rsi(closes),
        "hist": _macd_hist(closes), "atr": _atr(candles), "ref_close": closes[-1],
    }


def _candidate(name, timeframe, v, reason, direction="long", signal_strength=60.0):
    is_short = direction == "short"
    return {
        "signal": True, "strategy": name, "strategy_label": STRATEGY_LABELS[name],
        "direction": direction, "timeframe": timeframe, "reason": reason, "atr": v["atr"],
        "rsi": v["rsi"], "ema": v["ema50"], "hist": v["hist"],
        "ref_close": v["ref_close"],
        "candle_ts": v["candles"][-1][0],
        "signal_strength": round(max(0.0, min(100.0, float(signal_strength))), 1),
        "conds": {
            "ema": v["ref_close"] < v["ema50"] if is_short else v["ref_close"] > v["ema50"],
            "macd": v["hist"] <= 0 if is_short else v["hist"] >= 0,
            "rsi": v["rsi"] >= 30 if is_short else v["rsi"] <= 70,
        },
    }


def _select_best(candidates):
    """Choose the strongest qualifying setup, with a deterministic tie break."""
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            float(item.get("signal_strength", 0.0)),
            STRATEGY_RISK[item["strategy"]]["target_atr"]
            / STRATEGY_RISK[item["strategy"]]["stop_atr"],
        ),
    )

def _trend_candidate(exchange, symbol, timeframe, cache, direction="long"):
    v = _view(_load(exchange, symbol, timeframe, cache))
    if not v or not v["atr"]:
        return None
    previous_hist = _macd_hist(v["closes"][:-1])
    unit = max(v["atr"], v["ref_close"] * 0.0005)
    spread = abs(v["ema20"] - v["ema50"]) / unit
    slope = abs(v["ema50"] - v["ema50_old"]) / unit
    momentum = abs(v["hist"]) / unit
    strength = min(96.0, 58.0 + min(15.0, spread * 4.0)
                   + min(14.0, slope * 8.0) + min(9.0, momentum * 20.0))
    if direction == "short":
        falling = v["ema50"] < v["ema50_old"]
        momentum_ok = v["hist"] <= 0 or v["hist"] < previous_hist
        if (v["ref_close"] < v["ema50"] and v["ema20"] < v["ema50"]
                and falling and momentum_ok and 27 <= v["rsi"] <= 58):
            return _candidate("trend", timeframe, v, "السعر دون المتوسط المتحرك الأسي لآخر 50 شمعة، ومتوسط 20 شمعة أدنى منه؛ الاتجاه والزخم هابطان",
                              "short", strength)
        return None
    rising = v["ema50"] > v["ema50_old"]
    momentum_ok = v["hist"] >= 0 or v["hist"] > previous_hist
    if (v["ref_close"] > v["ema50"] and v["ema20"] > v["ema50"]
            and rising and momentum_ok and 42 <= v["rsi"] <= 73):
        return _candidate("trend", timeframe, v, "السعر أعلى من المتوسط المتحرك الأسي لآخر 50 شمعة، ومتوسط 20 شمعة أعلى منه؛ الاتجاه والزخم صاعدان",
                          "long", strength)
    return None

def _breakout_candidate(exchange, symbol, timeframe, cache, direction="long"):
    candles = _load(exchange, symbol, timeframe, cache)
    v = _view(candles)
    if not v or not v["atr"] or len(candles) < 22:
        return None
    previous = candles[-21:-1]
    average_volume = sum(c[5] for c in previous) / len(previous)
    last = candles[-1]
    volume_ok = average_volume > 0 and last[5] >= average_volume * 1.25
    if not volume_ok:
        return None
    volume_ratio = last[5] / average_volume
    if direction == "short":
        level = min(c[3] for c in previous)
        if last[4] < level and last[4] < last[1] and v["rsi"] > 25:
            extension = abs(last[4] - level) / v["atr"]
            strength = (58.0 + min(20.0, max(0.0, (volume_ratio - 1.5) * 12.0))
                        + min(12.0, max(0.0, 1.0 - extension) * 12.0)
                        - min(18.0, max(0.0, extension - 1.0) * 8.0))
            return _candidate("breakout", timeframe, v,
                              "أغلق السعر دون أدنى سعر خلال آخر 20 شمعة، مع حجم تداول أعلى من المعتاد",
                              "short", strength)
        return None
    level = max(c[2] for c in previous)
    if last[4] > level and last[4] > last[1] and v["rsi"] < 75:
        extension = abs(last[4] - level) / v["atr"]
        strength = (58.0 + min(20.0, max(0.0, (volume_ratio - 1.5) * 12.0))
                    + min(12.0, max(0.0, 1.0 - extension) * 12.0)
                    - min(18.0, max(0.0, extension - 1.0) * 8.0))
        return _candidate("breakout", timeframe, v,
                          "أغلق السعر فوق أعلى سعر خلال آخر 20 شمعة، مع حجم تداول أعلى من المعتاد",
                          "long", strength)
    return None

def _range_ok(v):
    if not v or not v["ema50"] or not v["ema50_old"]:
        return False
    slope = abs(v["ema50"] / v["ema50_old"] - 1.0)
    distance = abs(v["ref_close"] / v["ema50"] - 1.0)
    return slope <= 0.006 and distance <= 0.025


def _bounce_candidate(exchange, symbol, timeframe, cache, direction="long"):
    candles = _load(exchange, symbol, timeframe, cache)
    v = _view(candles)
    if not v or not v["atr"] or len(candles) < 22:
        return None
    previous_closes = v["closes"][:-1]
    prev_lower, _, prev_upper = _bands(previous_closes)
    lower, _, upper = _bands(v["closes"])
    prev_rsi = _rsi(previous_closes)
    last = candles[-1]
    if direction == "short":
        if (prev_upper is not None and upper is not None and candles[-2][2] >= prev_upper
                and prev_rsi >= 62 and last[4] < candles[-2][4] and last[4] < upper):
            reversal_atr = abs(candles[-2][4] - last[4]) / v["atr"]
            strength = min(95.0, 56.0 + min(22.0, (prev_rsi - 62.0) * 1.1)
                           + min(16.0, reversal_atr * 10.0))
            return _candidate("bounce", timeframe, v, "تراجع السعر بعد ملامسة النطاق العلوي مع ارتفاع مؤشر RSI",
                              "short", strength)
        return None
    if (prev_lower is not None and lower is not None and candles[-2][3] <= prev_lower
            and prev_rsi <= 38 and last[4] > candles[-2][4] and last[4] > lower):
        reversal_atr = abs(last[4] - candles[-2][4]) / v["atr"]
        strength = min(95.0, 56.0 + min(22.0, (38.0 - prev_rsi) * 1.1)
                       + min(16.0, reversal_atr * 10.0))
        return _candidate("bounce", timeframe, v, "ارتفع السعر بعد ملامسة النطاق السفلي مع انخفاض مؤشر RSI",
                          "long", strength)
    return None


def _regime_direction(v):
    if not v or not v["atr"]:
        return None
    if (v["ref_close"] > v["ema50"] and v["ema20"] > v["ema50"]
            and v["ema50"] > v["ema50_old"] and v["hist"] >= 0):
        return "long"
    if (v["ref_close"] < v["ema50"] and v["ema20"] < v["ema50"]
            and v["ema50"] < v["ema50_old"] and v["hist"] <= 0):
        return "short"
    return None


def _news_allows(context, direction):
    context = context or {}
    sentiment = str(context.get("news_sentiment") or "neutral").lower()
    severity = int(context.get("news_severity") or 0)
    if direction == "long" and sentiment == "negative" and severity >= 3:
        return False
    if direction == "short" and sentiment == "positive" and severity >= 4:
        return False
    return True


def _macro_momentum_candidate(exchange, symbol, cache, context, direction):
    daily = _view(_load(exchange, symbol, "1d", cache))
    four_hour = _view(_load(exchange, symbol, "4h", cache))
    hourly = _view(_load(exchange, symbol, "1h", cache))
    if not daily or not four_hour or not hourly:
        return None
    if (_regime_direction(daily) != direction or _regime_direction(four_hour) != direction
            or not _news_allows(context, direction)):
        return None
    hourly_candidate = _trend_candidate(exchange, symbol, "1h", cache, direction)
    if not hourly_candidate:
        return None
    slope_score = min(10.0, abs(daily["ema50"] / daily["ema50_old"] - 1.0) * 1000.0)
    strength = min(97.0, float(hourly_candidate["signal_strength"]) + slope_score)
    sentiment = str((context or {}).get("news_sentiment") or "neutral").lower()
    reason = ("اتفق اتجاه اليومي و4 ساعات مع زخم الساعة؛ مرشح الأخبار: {}"
              .format({"positive":"إيجابي", "negative":"سلبي", "neutral":"محايد"}.get(sentiment, "محايد")))
    return _candidate("macro_momentum_hybrid", "1h", hourly, reason, direction, strength)


def _algorithmic_discretionary_candidate(exchange, symbol, cache, context, direction):
    context = context or {}
    sentiment = str(context.get("news_sentiment") or "neutral").lower()
    severity = int(context.get("news_severity") or 0)
    if not _news_allows(context, direction):
        return None
    for timeframe in ("1h", "15m"):
        technical = _breakout_candidate(exchange, symbol, timeframe, cache, direction)
        if not technical or not _higher_timeframe_confirms(exchange, symbol, cache, direction):
            continue
        view = _view(_load(exchange, symbol, timeframe, cache))
        if not view:
            continue
        supports_direction = ((direction == "long" and sentiment == "positive")
                              or (direction == "short" and sentiment == "negative"))
        opposes_direction = ((direction == "long" and sentiment == "negative")
                             or (direction == "short" and sentiment == "positive"))
        news_boost = 6.0 if supports_direction else 0.0
        strength = min(98.0, float(technical["signal_strength"]) + news_boost)
        context_text = ("يدعم الاتجاه" if supports_direction else
                        "يعارض الاتجاه دون بلوغ حد الاستبعاد" if opposes_direction else
                        "محايد ولا يضيف تأكيدًا")
        if severity:
            context_text += " (خطورة {})".format(severity)
        reason = "اختراق خوارزمي مؤكد على 4 ساعات مع مرشح سياقي للأخبار: {}؛ الأخبار لا تنشئ إشارة وحدها".format(context_text)
        return _candidate("algorithmic_discretionary_hybrid", timeframe, view,
                          reason, direction, strength)
    return None


def _volume_profile(candles, bins=24, value_area_fraction=0.70):
    sample = candles[-60:]
    if len(sample) < 30:
        return None
    low = min(float(c[3]) for c in sample)
    high = max(float(c[2]) for c in sample)
    if high <= low:
        return None
    width = (high - low) / bins
    volumes = [0.0] * bins
    for candle in sample:
        typical = (float(candle[2]) + float(candle[3]) + float(candle[4])) / 3.0
        index = max(0, min(bins - 1, int((typical - low) / width)))
        volumes[index] += max(0.0, float(candle[5]))
    total = sum(volumes)
    if total <= 0:
        return None
    poc_index = max(range(bins), key=lambda i: volumes[i])
    left = right = poc_index
    accumulated = volumes[poc_index]
    target = total * value_area_fraction
    while accumulated < target and (left > 0 or right < bins - 1):
        left_volume = volumes[left - 1] if left > 0 else -1.0
        right_volume = volumes[right + 1] if right < bins - 1 else -1.0
        if right_volume > left_volume:
            right += 1
            accumulated += max(0.0, right_volume)
        else:
            left -= 1
            accumulated += max(0.0, left_volume)
    return {"poc": low + (poc_index + 0.5) * width,
            "val": low + left * width, "vah": low + (right + 1) * width,
            "volume": total}


def _volume_profile_candidate(exchange, symbol, cache, direction):
    if not _range_ok(_view(_load(exchange, symbol, "4h", cache))):
        return None
    profile_candles = _load(exchange, symbol, "1h", cache)
    profile = _volume_profile(profile_candles)
    timeframe = "15m"
    candles = _load(exchange, symbol, timeframe, cache)
    v = _view(candles)
    if not profile or not v or not v["atr"]:
        return None
    last, previous = candles[-1], candles[-2]
    if direction == "short":
        distance = (profile["vah"] - float(last[4])) / v["atr"]
        rejection = (float(last[2]) >= profile["vah"] - 0.25 * v["atr"]
                     and float(last[4]) < float(last[1]) and float(last[4]) < float(previous[4])
                     and v["rsi"] >= 50)
        if not rejection:
            return None
    else:
        distance = (float(last[4]) - profile["val"]) / v["atr"]
        rejection = (float(last[3]) <= profile["val"] + 0.25 * v["atr"]
                     and float(last[4]) > float(last[1]) and float(last[4]) > float(previous[4])
                     and v["rsi"] <= 50)
        if not rejection:
            return None
    if distance < -0.35 or distance > 0.65:
        return None
    strength = min(94.0, 62.0 + max(0.0, 0.65 - abs(distance)) * 24.0)
    return _candidate("volume_profile_range", timeframe, v,
                      "رفض سعري عند حد منطقة القيمة {:.8g} مع بقاء سوق 4 ساعات ضمن نطاق؛ الهدف الأول قرب نقطة التحكم {:.8g}".format(
                          profile["vah"] if direction == "short" else profile["val"], profile["poc"]),
                      direction, strength)


def _volatility_breakout_hedge_candidate(exchange, symbol, cache, context):
    context = context or {}
    exposure = float(context.get("spot_exposure_usdt") or 0.0)
    if exposure <= 0:
        return None
    timeframe = "15m"
    candles = _load(exchange, symbol, timeframe, cache)
    v = _view(candles)
    if not v or len(candles) < 60 or not v["atr"]:
        return None
    previous = candles[-21:-1]
    average_volume = sum(float(c[5]) for c in previous) / len(previous)
    if average_volume <= 0 or float(candles[-1][5]) < average_volume * 1.4:
        return None
    atr_samples = [_atr(candles[:i], 14) for i in range(max(15, len(candles) - 36), len(candles))]
    atr_samples = [value for value in atr_samples if value > 0]
    if len(atr_samples) < 16:
        return None
    older_atr = sorted(atr_samples[:-8])
    recent_atr = sorted(atr_samples[-8:])
    older_median = older_atr[len(older_atr) // 2]
    recent_median = recent_atr[len(recent_atr) // 2]
    last = candles[-1]
    floor = min(float(c[3]) for c in previous)
    contraction = recent_median <= older_median * 0.8
    expansion = v["atr"] >= recent_median * 1.15
    bearish_break = float(last[4]) < floor and float(last[4]) < float(last[1])
    if not (contraction and expansion and bearish_break):
        return None
    ratio = MAX_SPOT_HEDGE_RATIO
    extension = abs(float(last[4]) - floor) / v["atr"]
    strength = min(96.0, 64.0 + min(18.0, max(0.0, float(last[5]) / average_volume - 1.4) * 12.0)
                   + max(0.0, 8.0 - extension * 6.0))
    setup = _candidate("volatility_breakout_hedge", timeframe, v,
                       "كسر هابط بعد انضغاط ثم اتساع التقلب مع حجم مرتفع؛ تحوّط Futures محدود إلى {:.0f}% من تعرض Spot الطويل".format(ratio * 100),
                       "short", strength)
    setup.update({"hedge_base": str(context.get("spot_base") or symbol.split("/")[0]),
                  "hedge_exposure_usdt": exposure, "hedge_ratio": ratio})
    return setup

def _higher_timeframe_confirms(exchange, symbol, cache, direction):
    """Require the 4h trend to agree with 15m/1h breakout entries."""
    v = _view(_load(exchange, symbol, "4h", cache))
    if not v or not v["atr"]:
        return False
    previous_hist = _macd_hist(v["closes"][:-1])
    if direction == "short":
        return (v["ref_close"] < v["ema50"] and v["ema20"] < v["ema50"]
                and v["ema50"] < v["ema50_old"]
                and (v["hist"] <= 0 or v["hist"] < previous_hist))
    return (v["ref_close"] > v["ema50"] and v["ema20"] > v["ema50"]
            and v["ema50"] > v["ema50_old"]
            and (v["hist"] >= 0 or v["hist"] > previous_hist))


def evaluate_symbol(exchange, symbol, allow_short=False, context=None,
                    enable_hybrids=True, allowed_strategies=None, cache=None):
    """Evaluate established and hybrid strategies using closed candles only."""
    context = context or {}
    directions = ("long", "short") if allow_short else ("long",)
    cache = dict(cache) if cache else {}
    candidates = []

    for timeframe in ("1h", "15m"):
        for direction in directions:
            found = _breakout_candidate(exchange, symbol, timeframe, cache, direction)
            if found and _higher_timeframe_confirms(exchange, symbol, cache, direction):
                candidates.append(found)

    for timeframe in ("1d", "4h"):
        for direction in directions:
            found = _trend_candidate(exchange, symbol, timeframe, cache, direction)
            if found:
                candidates.append(found)

    range_view = _view(_load(exchange, symbol, "4h", cache))
    if _range_ok(range_view):
        for timeframe in ("15m", "5m"):
            for direction in directions:
                found = _bounce_candidate(exchange, symbol, timeframe, cache, direction)
                if found:
                    candidates.append(found)
        if enable_hybrids:
            for direction in directions:
                found = _volume_profile_candidate(exchange, symbol, cache, direction)
                if found:
                    candidates.append(found)

    if enable_hybrids:
        for direction in directions:
            found = _macro_momentum_candidate(exchange, symbol, cache, context, direction)
            if found:
                candidates.append(found)
            found = _algorithmic_discretionary_candidate(exchange, symbol, cache, context, direction)
            if found:
                candidates.append(found)
        if allow_short:
            found = _volatility_breakout_hedge_candidate(exchange, symbol, cache, context)
            if found:
                candidates.append(found)

    if allowed_strategies is not None:
        candidates = [item for item in candidates if item.get("strategy") in allowed_strategies]
    selected = _select_best(candidates)
    if selected:
        return selected

    v = _view(_load(exchange, symbol, "15m", cache))
    if v:
        return {
            "signal": False, "strategy": "wait", "strategy_label": "انتظار",
            "direction": "flat", "timeframe": "15m",
            "reason": "لم تكتمل شروط الاستراتيجيات الفنية والهجينة المتاحة",
            "atr": v["atr"], "rsi": v["rsi"], "ema": v["ema50"], "hist": v["hist"],
            "ref_close": v["ref_close"], "candle_ts": v["candles"][-1][0],
            "signal_strength": 0.0,
            "conds": {"ema": False, "macd": False, "rsi": False},
        }
    return {
        "signal": False, "strategy": "wait", "strategy_label": "انتظار",
        "direction": "flat", "timeframe": "15m", "reason": "لا تتوفر بيانات كافية لحساب المؤشرات", "atr": 0.0,
        "rsi": 50.0, "ema": 0.0, "hist": 0.0, "ref_close": 0.0,
        "candle_ts": None, "signal_strength": 0.0,
        "conds": {"ema": False, "macd": False, "rsi": False},
    }
