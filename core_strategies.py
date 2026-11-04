"""Long-only spot entry strategies: trend_pullback and donchian_breakout.

Signal functions take already-closed candles and never read the bar under
construction. Live scanning drops the unfinished candle before calling them.
Old strategies stay in strategy_engine and can be turned back on with
STRATEGY_MODE or ACTIVE_STRATEGIES. Nothing here is read from bot_settings:
a value stored in the database must not put the previous strategies back in
charge of new entries.
"""
import os


CORE_STRATEGY_NAMES = ("trend_pullback", "donchian_breakout")
LEGACY_STRATEGIES = (
    "trend", "breakout", "bounce",
    "macro_momentum_hybrid", "algorithmic_discretionary_hybrid",
    "volume_profile_range", "volatility_breakout_hedge", "donchian_trend",
)
KNOWN_STRATEGIES = LEGACY_STRATEGIES + CORE_STRATEGY_NAMES

# Stablecoins and fiat-pegged bases are never traded by these strategies.
STABLE_BASES = frozenset({
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDD",
    "EUR", "AEUR", "USD1", "USDS", "PYUSD", "GUSD",
})
DEFAULT_CORE_SYMBOLS = (
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "LINK/USDT", "AVAX/USDT", "DOT/USDT",
    "LTC/USDT", "BCH/USDT", "TRX/USDT",
)

TF_4H_MS = 4 * 60 * 60 * 1000
TF_1D_MS = 24 * 60 * 60 * 1000
STOP_ATR = 2.0
MIN_4H_BARS = 250
MIN_DAILY_BARS = 200

# Keys that must not be loaded from bot_settings. Code and environment defaults
# decide new entries; a previously stored list would keep bounce/breakout alive.
IGNORED_DB_KEYS = (
    "active_strategies", "strategy_mode", "disabled_strategies",
    "daily_loss_limit_pct", "max_open_positions", "portfolio_max_open_risk_pct",
    "futures_new_entries_enabled",
)


class DataFault(Exception):
    """Market data that is missing, stale, or internally inconsistent.

    New entries stop until a later scan completes without one of these.
    Open positions keep their existing stop and exit logic.
    """

    def __init__(self, kind, message):
        self.kind = str(kind or "data")
        super().__init__(message)


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def strategy_mode():
    mode = os.getenv("STRATEGY_MODE", "core").strip().lower()
    if mode not in ("core", "legacy", "all"):
        raise ValueError("STRATEGY_MODE must be core, legacy, or all")
    return mode


def _disabled_names():
    return {part.strip() for part in os.getenv("DISABLED_STRATEGIES", "").split(",") if part.strip()}


def active_strategy_names():
    """Entry strategies that may open new trades. Read from the environment on
    each call so a database row cannot freeze an older list."""
    raw = os.getenv("ACTIVE_STRATEGIES", "").strip()
    if raw:
        names = [part.strip() for part in raw.split(",") if part.strip()]
    elif strategy_mode() == "legacy":
        names = list(LEGACY_STRATEGIES)
    elif strategy_mode() == "all":
        names = list(CORE_STRATEGY_NAMES) + list(LEGACY_STRATEGIES)
    else:
        names = list(CORE_STRATEGY_NAMES)
    unknown = [name for name in names if name not in KNOWN_STRATEGIES]
    if unknown:
        raise ValueError("Unknown strategy in ACTIVE_STRATEGIES: {}".format(", ".join(unknown)))
    disabled = _disabled_names()
    out = []
    for name in names:
        if name not in disabled and name not in out:
            out.append(name)
    return tuple(out)


def legacy_entries_enabled():
    return any(name in LEGACY_STRATEGIES for name in active_strategy_names())


def core_entries_enabled():
    return any(name in CORE_STRATEGY_NAMES for name in active_strategy_names())


def ignored_db_strategy_settings(values):
    """Strategy keys stored in bot_settings are ignored on purpose."""
    if not values:
        return {}
    return {key: values[key] for key in IGNORED_DB_KEYS if key in values}


def futures_new_entries_enabled():
    """Futures keeps managing open positions. New futures entries stay off
    unless FUTURES_NEW_ENTRIES_ENABLED is explicitly turned on. These two
    strategies are long-only spot systems and do not use leverage."""
    return _truthy(os.getenv("FUTURES_NEW_ENTRIES_ENABLED", "false"))


def _bounded_float(name, default, low, high, inclusive_low=False):
    raw = os.getenv(name, "").strip()
    value = float(raw) if raw else float(default)
    ok = (low <= value <= high) if inclusive_low else (low < value <= high)
    if not ok:
        bracket = "[{}, {}]".format(low, high) if inclusive_low else "({}, {}]".format(low, high)
        raise ValueError("{} must be in {}".format(name, bracket))
    return value


def effective_risk_pct(strategy):
    """Per-strategy risk, never above RISK_PER_TRADE_PCT, always inside (0, 2]."""
    if strategy == "donchian_breakout":
        specific = _bounded_float("DONCHIAN_BREAKOUT_RISK_PCT", 0.25, 0.0, 2.0)
    else:
        specific = _bounded_float("TREND_PULLBACK_RISK_PCT", 0.5, 0.0, 2.0)
    ceiling = _bounded_float("RISK_PER_TRADE_PCT", 2.0, 0.0, 2.0)
    return min(specific, ceiling)


def daily_loss_limit_pct(max_daily_loss_pct=None):
    """Limit used to block new entries. Default 2%.

    In core/all mode this is min(MAX_DAILY_LOSS_PCT, DAILY_LOSS_LIMIT_PCT),
    so a tighter value already set in the environment still wins and the new
    2% default applies even when MAX_DAILY_LOSS_PCT is the older 5%.
    Legacy mode keeps MAX_DAILY_LOSS_PCT unless DAILY_LOSS_LIMIT_PCT is set.
    """
    if max_daily_loss_pct is None:
        max_daily_loss_pct = _bounded_float("MAX_DAILY_LOSS_PCT", 5.0, 0.1, 25.0, inclusive_low=True)
    else:
        max_daily_loss_pct = float(max_daily_loss_pct)
    raw = os.getenv("DAILY_LOSS_LIMIT_PCT", "").strip()
    if strategy_mode() == "legacy" and not raw:
        return max_daily_loss_pct
    configured = float(raw) if raw else 2.0
    if not 0.0 < configured <= 25.0:
        raise ValueError("DAILY_LOSS_LIMIT_PCT must be in (0, 25]")
    return min(max_daily_loss_pct, configured)


def max_open_positions():
    raw = os.getenv("MAX_OPEN_POSITIONS", "").strip()
    value = int(raw) if raw else 4
    if not 1 <= value <= 20:
        raise ValueError("MAX_OPEN_POSITIONS must be between 1 and 20")
    return value


def tp_r_multiple():
    return _bounded_float("TP_R_MULTIPLE", 2.0, 0.5, 10.0, inclusive_low=True)


def exit_method_for(strategy):
    if strategy == "trend_pullback":
        method = os.getenv("TP_EXIT_METHOD", "tp_2r").strip().lower()
        if method not in ("tp_2r", "trail"):
            raise ValueError("TP_EXIT_METHOD must be tp_2r or trail")
        return method
    if strategy == "donchian_breakout":
        method = os.getenv("DONCHIAN_EXIT_METHOD", "channel").strip().lower()
        if method not in ("channel", "atr_trail"):
            raise ValueError("DONCHIAN_EXIT_METHOD must be channel or atr_trail")
        return method
    return None


def tp_trail_atr():
    return _bounded_float("TP_TRAIL_ATR", 2.0, 0.0, 10.0)


def donchian_trail_atr():
    return _bounded_float("DONCHIAN_TRAIL_ATR", 3.0, 0.0, 10.0)


def entry_order_mode():
    """Limit-first for the new spot strategies. The global ENTRY_ORDER_MODE
    default stays market so other paths are unchanged."""
    mode = os.getenv("CORE_ENTRY_ORDER_MODE", "limit_then_market").strip().lower()
    if mode in ("limit", "post_only", "limit-then-market"):
        mode = "limit_then_market"
    if mode not in ("market", "limit_then_market"):
        raise ValueError("CORE_ENTRY_ORDER_MODE must be market or limit_then_market")
    return mode


def max_candle_age_s():
    return _bounded_float("KILL_SWITCH_MAX_CANDLE_AGE_SECONDS", 6 * 3600, 60.0, 7 * 24 * 3600, inclusive_low=True)


def max_daily_age_s():
    return _bounded_float("KILL_SWITCH_MAX_DAILY_AGE_SECONDS", 36 * 3600, 60.0, 14 * 24 * 3600, inclusive_low=True)


def btc_symbol():
    return os.getenv("SPOT_REGIME_SYMBOL", "BTC/USDT").strip() or "BTC/USDT"


def core_symbols():
    raw = os.getenv("CORE_SYMBOLS", "").strip()
    if not raw:
        return DEFAULT_CORE_SYMBOLS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def max_spread_pct():
    raw = os.getenv("CORE_MAX_SPREAD_PCT", "").strip()
    if raw:
        return _bounded_float("CORE_MAX_SPREAD_PCT", 0.15, 0.0, 5.0)
    return _bounded_float("MAX_SPOT_SPREAD_PCT", 0.15, 0.0, 5.0)


def symbol_allowed(symbol, universe=None):
    symbol = str(symbol or "")
    if ":" in symbol:
        return False
    base = symbol.split("/")[0].upper()
    if base in STABLE_BASES:
        return False
    allowed = core_symbols() if universe is None else universe
    return symbol in allowed


def ema_series(values, period):
    period = int(period)
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    acc = sum(float(v) for v in values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    out = [None] * (period - 1) + [acc]
    for price in values[period:]:
        acc = (float(price) - acc) * alpha + acc
        out.append(acc)
    return out


def atr(candles, period=14):
    period = int(period)
    if not candles or len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        high, low = float(candles[i][2]), float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs[-period:]) / float(period)


def rsi(closes, period=14):
    closes = [float(v) for v in closes]
    if len(closes) < period + 1:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(change, 0.0) for change in changes[:period]) / period
    loss = sum(max(-change, 0.0) for change in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def closed_candles(raw, timeframe_ms, now_ms=None):
    """Drop the candle that has not closed yet.

    When now_ms is omitted, the last row is treated as the unfinished candle.
    That matches the exchange fetch used by the live scanner, which includes
    the current bar.
    """
    if not raw:
        return []
    if now_ms is None:
        return [list(row) for row in raw[:-1]]
    now_ms = int(now_ms)
    timeframe_ms = int(timeframe_ms)
    return [list(row) for row in raw if int(row[0]) + timeframe_ms <= now_ms]


def validate_candles(candles):
    if not candles:
        raise DataFault("abnormal", "no candles")
    previous_ts = -1
    for row in candles:
        if row is None or len(row) < 6:
            raise DataFault("abnormal", "candle row is incomplete")
        ts, opened, high, low, close, volume = (float(row[0]), float(row[1]), float(row[2]),
                                                 float(row[3]), float(row[4]), float(row[5]))
        if ts <= previous_ts:
            raise DataFault("abnormal", "candle timestamps are not increasing")
        if not (high >= low and high >= opened and high >= close and low <= opened and low <= close):
            raise DataFault("abnormal", "OHLC relationship is impossible")
        if high <= 0 or low <= 0 or close <= 0 or opened <= 0 or volume < 0:
            raise DataFault("abnormal", "non-positive price or negative volume")
        previous_ts = ts


def assert_fresh(candles, timeframe_ms, now_ms, max_age_s):
    if now_ms is None or not candles:
        return
    last_close_ms = int(candles[-1][0]) + int(timeframe_ms)
    age_s = (int(now_ms) - last_close_ms) / 1000.0
    if age_s > float(max_age_s):
        raise DataFault("stale", "last closed candle is {:.0f}s old".format(age_s))


def _atr_spiking(candles):
    mult = _bounded_float("ATR_SPIKE_MULT", 2.0, 1.0, 10.0, inclusive_low=True)
    window = int(os.getenv("ATR_MEDIAN_WINDOW", "48"))
    if window < 10:
        raise ValueError("ATR_MEDIAN_WINDOW must be at least 10")
    if len(candles) < 15 + window:
        return False
    samples = []
    start = len(candles) - window
    for end in range(start, len(candles) + 1):
        value = atr(candles[:end])
        close = float(candles[end - 1][4])
        if value and close > 0:
            samples.append(value / close)
    if len(samples) < window:
        return False
    history = sorted(samples[:-1])
    median = history[len(history) // 2]
    if median <= 0:
        return False
    return samples[-1] > median * mult


def _costs_too_high(entry, stop_distance, spread_pct):
    if entry <= 0 or stop_distance <= 0:
        return True
    spread_pct = 0.0 if spread_pct is None else float(spread_pct)
    fee = _bounded_float("CORE_TAKER_FEE_PCT", 0.1, 0.0, 1.0, inclusive_low=True)
    slip = _bounded_float("CORE_SLIPPAGE_PCT", 0.05, 0.0, 1.0, inclusive_low=True)
    fraction = _bounded_float("CORE_MAX_COST_FRACTION_OF_R", 0.35, 0.0, 1.0)
    if fraction <= 0:
        return False
    cost = entry * (fee * 2.0 + slip * 2.0 + max(spread_pct, 0.0)) / 100.0
    return cost >= stop_distance * fraction


def _spread_blocks(spread_pct):
    return spread_pct is not None and float(spread_pct) > max_spread_pct()


def trend_pullback_setup(candles, spread_pct=None):
    """Long after a pullback toward EMA20 that closes back above it.

    Trend filter (4h, closed bars only): close > EMA200 and EMA50 > EMA200.
    The touch test looks only at the last PULLBACK_LOOKBACK closed bars,
    including the signal bar. No future bar is read.
    """
    if not candles or len(candles) < MIN_4H_BARS:
        return None
    closes = [float(row[4]) for row in candles]
    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)
    if ema20[-1] is None or ema50[-1] is None or ema200[-1] is None:
        return None
    close = closes[-1]
    if not (close > ema200[-1] and ema50[-1] > ema200[-1]):
        return None
    if not (close > ema20[-1]):
        return None
    atr_value = atr(candles, 14)
    if not atr_value or atr_value <= 0:
        return None
    lookback = int(os.getenv("PULLBACK_LOOKBACK", "6"))
    if lookback < 2:
        raise ValueError("PULLBACK_LOOKBACK must be at least 2")
    near = _bounded_float("PULLBACK_NEAR_ATR", 0.25, 0.0, 5.0, inclusive_low=True)
    start = max(0, len(candles) - lookback)
    touched = False
    for i in range(start, len(candles)):
        if ema20[i] is None:
            continue
        if float(candles[i][3]) <= float(ema20[i]) + near * atr_value:
            touched = True
            break
    if not touched:
        return None
    if _atr_spiking(candles) or _spread_blocks(spread_pct):
        return None
    stop_distance = STOP_ATR * atr_value
    if _costs_too_high(close, stop_distance, spread_pct):
        return None
    separation = (ema50[-1] - ema200[-1]) / atr_value
    strength = min(92.0, 64.0 + min(20.0, max(0.0, separation) * 4.0))
    return {
        "strategy": "trend_pullback",
        "reason": "إغلاق 4 ساعات فوق EMA20 بعد تراجع نحوها، مع إغلاق وEMA50 فوق EMA200",
        "atr": atr_value,
        "ref_close": close,
        "candle_ts": int(candles[-1][0]),
        "strength": strength,
        "ema50": ema50[-1],
        "rsi": rsi(closes),
        "stop_distance": stop_distance,
        "direction": "long",
    }


def btc_daily_bull(daily_candles):
    """True when the last closed BTC daily close is above its EMA200.

    None means the regime cannot be confirmed (missing history). Callers must
    pass closed daily candles only.
    """
    if not daily_candles or len(daily_candles) < MIN_DAILY_BARS:
        return None
    closes = [float(row[4]) for row in daily_candles]
    ema200 = ema_series(closes, 200)
    if ema200[-1] is None:
        return None
    return closes[-1] > ema200[-1]


def donchian_breakout_setup(candles, btc_daily, spread_pct=None, lookback=None):
    """4h close above the highest high of the previous `lookback` closed bars.

    The signal bar is not part of the channel. Long only when BTC's last
    closed daily close is above the BTC daily EMA200.
    """
    lookback = int(lookback or os.getenv("DONCHIAN_ENTRY_LOOKBACK", "20"))
    if lookback < 2:
        raise ValueError("DONCHIAN_ENTRY_LOOKBACK must be at least 2")
    if not candles or len(candles) < max(MIN_4H_BARS, lookback + 2):
        return None
    regime = btc_daily_bull(btc_daily)
    if regime is not True:
        return None
    previous = candles[-lookback - 1:-1]
    if len(previous) < lookback:
        return None
    level = max(float(row[2]) for row in previous)
    close = float(candles[-1][4])
    if close <= level:
        return None
    atr_value = atr(candles, 14)
    if not atr_value or atr_value <= 0:
        return None
    if _spread_blocks(spread_pct):
        return None
    stop_distance = STOP_ATR * atr_value
    if _costs_too_high(close, stop_distance, spread_pct):
        return None
    extension = (close - level) / atr_value
    strength = min(90.0, 66.0 + min(16.0, max(0.0, 1.2 - extension) * 12.0))
    return {
        "strategy": "donchian_breakout",
        "reason": "إغلاق 4 ساعات فوق أعلى قمة لآخر {} شمعة مغلقة، وBTC اليومي فوق EMA200".format(lookback),
        "atr": atr_value,
        "ref_close": close,
        "candle_ts": int(candles[-1][0]),
        "strength": strength,
        "ema50": close,
        "rsi": rsi([float(row[4]) for row in candles]),
        "stop_distance": stop_distance,
        "breakout_level": level,
        "direction": "long",
    }


def donchian_channel_exit(candles, lookback=None):
    """True when the last closed 4h close is under the prior `lookback` lows.

    The signal bar's own low is not part of that floor.
    """
    lookback = int(lookback or os.getenv("DONCHIAN_EXIT_LOOKBACK", "10"))
    if lookback < 2 or not candles or len(candles) < lookback + 1:
        return False
    previous = candles[-lookback - 1:-1]
    if len(previous) < lookback:
        return False
    floor = min(float(row[3]) for row in previous)
    return float(candles[-1][4]) < floor


def signals_for_symbol(symbol, candles_4h, btc_daily=None, spread_pct=None):
    if not symbol_allowed(symbol):
        return []
    active = set(active_strategy_names())
    found = []
    if "trend_pullback" in active:
        setup = trend_pullback_setup(candles_4h, spread_pct)
        if setup:
            found.append(setup)
    if "donchian_breakout" in active:
        setup = donchian_breakout_setup(candles_4h, btc_daily, spread_pct)
        if setup:
            found.append(setup)
    return found


def size_position(equity, entry, stop_distance, strategy, risk_pct=None):
    """qty = risk_amount / stop_distance. Refuses a size that needs leverage."""
    pct = effective_risk_pct(strategy) if risk_pct is None else float(risk_pct)
    if not 0.0 < pct <= 2.0:
        raise ValueError("risk percent must be in (0, 2]")
    equity = float(equity or 0.0)
    entry = float(entry or 0.0)
    stop_distance = float(stop_distance or 0.0)
    rejected = {"allowed": False, "qty": 0.0, "notional": 0.0, "risk_usdt": 0.0,
                "risk_pct": pct, "stop_distance": stop_distance, "reason": ""}
    if equity <= 0 or entry <= 0 or stop_distance <= 0:
        rejected["reason"] = "invalid equity, price, or stop"
        return rejected
    risk_usdt = equity * pct / 100.0
    qty = risk_usdt / stop_distance
    notional = qty * entry
    if notional > equity:
        rejected["reason"] = "position notional would exceed equity (no leverage)"
        rejected["risk_usdt"] = risk_usdt
        return rejected
    return {"allowed": True, "qty": qty, "notional": notional, "risk_usdt": risk_usdt,
            "risk_pct": pct, "stop_distance": stop_distance, "reason": ""}


def daily_loss_blocks(day_start_equity, equity, limit_pct):
    """True when new entries must stop for the day. Invalid inputs fail closed."""
    try:
        day_start_equity = float(day_start_equity)
        equity = float(equity)
        limit_pct = float(limit_pct)
    except (TypeError, ValueError):
        return True
    if day_start_equity <= 0 or equity <= 0 or limit_pct <= 0:
        return True
    loss_pct = (day_start_equity - equity) / day_start_equity * 100.0
    return loss_pct >= limit_pct


def portfolio_risk_blocks(open_risk_usdt, new_risk_usdt, equity, cap_pct):
    try:
        open_risk_usdt = float(open_risk_usdt or 0.0)
        new_risk_usdt = float(new_risk_usdt or 0.0)
        equity = float(equity or 0.0)
        cap_pct = float(cap_pct)
    except (TypeError, ValueError):
        return True
    if equity <= 0 or new_risk_usdt <= 0 or cap_pct <= 0 or open_risk_usdt < 0:
        return True
    return (open_risk_usdt + new_risk_usdt) / equity * 100.0 > cap_pct


def entry_blocked(kill_tripped, day_start_equity, equity, open_positions, new_risk_usdt,
                  open_risk_usdt=0.0, daily_limit_pct=None, position_cap=None, portfolio_cap_pct=None):
    """Shared pre-trade gate. Returns (blocked, reasons)."""
    reasons = []
    if kill_tripped:
        reasons.append("kill_switch")
    limit = daily_loss_limit_pct() if daily_limit_pct is None else float(daily_limit_pct)
    if daily_loss_blocks(day_start_equity, equity, limit):
        reasons.append("daily_loss")
    cap = max_open_positions() if position_cap is None else int(position_cap)
    if int(open_positions) >= cap:
        reasons.append("max_positions")
    if portfolio_cap_pct is None:
        from risk_controls import portfolio_open_risk_cap
        portfolio_cap_pct = portfolio_open_risk_cap()
    if portfolio_risk_blocks(open_risk_usdt, new_risk_usdt, equity, portfolio_cap_pct):
        reasons.append("portfolio_risk")
    return (bool(reasons), reasons)


class KillSwitch:
    """Halts new entries after stale candles, fetch failures, or bad prints.

    Exits are not routed through this object. A clean later scan clears the
    latch when KILL_SWITCH_AUTO_CLEAR is on (the default).
    """

    def __init__(self):
        self.tripped = False
        self.reason = ""
        self._scan_errors = []

    def enabled(self):
        raw = os.getenv("KILL_SWITCH_ENABLED", "true")
        return _truthy(raw)

    def auto_clear(self):
        return _truthy(os.getenv("KILL_SWITCH_AUTO_CLEAR", "true"))

    def begin_scan(self):
        self._scan_errors = []

    def trip(self, reason):
        self.tripped = True
        self.reason = str(reason or "data error")
        self._scan_errors.append(self.reason)
        return self.reason

    def finish_scan(self):
        if not self.enabled():
            self.tripped = False
            self.reason = ""
            self._scan_errors = []
            return
        if self._scan_errors:
            self.tripped = True
            self.reason = self._scan_errors[-1]
        elif self.auto_clear():
            self.tripped = False
            self.reason = ""

    def blocks_entries(self):
        if not self.enabled():
            return False
        return self.tripped or bool(self._scan_errors)

    def reset(self):
        self.tripped = False
        self.reason = ""
        self._scan_errors = []


kill_switch = KillSwitch()
