"""Deterministic algorithmic strategy selector for Binance Demo.

Signal strength is a heuristic ranking, not a probability of profit.
"""
from math import sqrt

STRATEGY_RISK = {
    "trend": {"stop_atr": 1.5, "target_atr": 3.0, "trail_atr": 1.5},
    "breakout": {"stop_atr": 1.0, "target_atr": 2.0, "trail_atr": 1.0},
    "bounce": {"stop_atr": 1.0, "target_atr": 1.5, "trail_atr": 0.75},
}
STRATEGY_LABELS = {"trend": "تتبّع الاتجاه", "breakout": "الاختراق", "bounce": "الارتداد"}


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
        candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=90)
        cache[timeframe] = candles[:-1]
    return cache[timeframe]


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
            return _candidate("trend", timeframe, v, "السعر تحت EMA50 والاتجاه والزخم هابطان",
                              "short", strength)
        return None
    rising = v["ema50"] > v["ema50_old"]
    momentum_ok = v["hist"] >= 0 or v["hist"] > previous_hist
    if (v["ref_close"] > v["ema50"] and v["ema20"] > v["ema50"]
            and rising and momentum_ok and 42 <= v["rsi"] <= 73):
        return _candidate("trend", timeframe, v, "السعر فوق EMA50 والاتجاه والزخم صاعدان",
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
                              "إغلاق تحت قاع 20 شمعة مع حجم تداول مرتفع",
                              "short", strength)
        return None
    level = max(c[2] for c in previous)
    if last[4] > level and last[4] > last[1] and v["rsi"] < 75:
        extension = abs(last[4] - level) / v["atr"]
        strength = (58.0 + min(20.0, max(0.0, (volume_ratio - 1.5) * 12.0))
                    + min(12.0, max(0.0, 1.0 - extension) * 12.0)
                    - min(18.0, max(0.0, extension - 1.0) * 8.0))
        return _candidate("breakout", timeframe, v,
                          "إغلاق فوق قمة 20 شمعة مع حجم تداول مرتفع",
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
            return _candidate("bounce", timeframe, v, "رفض مؤكد من النطاق العلوي مع RSI مرتفع",
                              "short", strength)
        return None
    if (prev_lower is not None and lower is not None and candles[-2][3] <= prev_lower
            and prev_rsi <= 38 and last[4] > candles[-2][4] and last[4] > lower):
        reversal_atr = abs(last[4] - candles[-2][4]) / v["atr"]
        strength = min(95.0, 56.0 + min(22.0, (38.0 - prev_rsi) * 1.1)
                       + min(16.0, reversal_atr * 10.0))
        return _candidate("bounce", timeframe, v, "ارتداد مؤكد من النطاق السفلي مع RSI منخفض",
                          "long", strength)
    return None

def evaluate_symbol(exchange, symbol, allow_short=False):
    """Evaluate every strategy/timeframe and return the strongest confirmed setup."""
    directions = ("long", "short") if allow_short else ("long",)
    cache = {}
    candidates = []

    # Fast breakouts: 1h/15m. Rank both directions instead of taking the first match.
    for timeframe in ("1h", "15m"):
        for direction in directions:
            found = _breakout_candidate(exchange, symbol, timeframe, cache, direction)
            if found:
                candidates.append(found)

    # Swing trend following: daily/4h.
    for timeframe in ("1d", "4h"):
        for direction in directions:
            found = _trend_candidate(exchange, symbol, timeframe, cache, direction)
            if found:
                candidates.append(found)

    # Mean reversion is considered only when the higher-timeframe regime is ranging.
    range_view = _view(_load(exchange, symbol, "4h", cache))
    if _range_ok(range_view):
        for timeframe in ("15m", "5m"):
            for direction in directions:
                found = _bounce_candidate(exchange, symbol, timeframe, cache, direction)
                if found:
                    candidates.append(found)

    selected = _select_best(candidates)
    if selected:
        return selected

    v = _view(_load(exchange, symbol, "15m", cache))
    if v:
        return {
            "signal": False, "strategy": "wait", "strategy_label": "انتظار",
            "direction": "flat", "timeframe": "15m", "reason": "لا تتوفر شروط دخول مكتملة",
            "atr": v["atr"], "rsi": v["rsi"], "ema": v["ema50"], "hist": v["hist"],
            "ref_close": v["ref_close"], "candle_ts": v["candles"][-1][0],
            "signal_strength": 0.0,
            "conds": {"ema": False, "macd": False, "rsi": False},
        }
    return {
        "signal": False, "strategy": "wait", "strategy_label": "انتظار",
        "direction": "flat", "timeframe": "15m", "reason": "بيانات غير كافية", "atr": 0.0,
        "rsi": 50.0, "ema": 0.0, "hist": 0.0, "ref_close": 0.0,
        "candle_ts": None, "signal_strength": 0.0,
        "conds": {"ema": False, "macd": False, "rsi": False},
    }

