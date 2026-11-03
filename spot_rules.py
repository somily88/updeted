"""Spot-only profitability rules (pure functions, shared by main.py and backtest.py).

All knobs come from env so the live bot and the backtest use identical logic.
"""
import os


def _bool(name, default):
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "on")


# 1) Bounce target must cover k x round-trip fee.
SPOT_MIN_TARGET_FEE_MULT = float(os.getenv("SPOT_MIN_TARGET_FEE_MULT", "3"))
SPOT_MIN_TARGET_STRATEGIES = tuple(
    s.strip() for s in os.getenv("SPOT_MIN_TARGET_STRATEGIES", "bounce").split(",") if s.strip())
SPOT_TAKER_FEE_PCT = float(os.getenv("SPOT_TAKER_FEE_PCT", "0.1"))
SPOT_BNB_FEE_DISCOUNT_PCT = float(os.getenv("SPOT_BNB_FEE_DISCOUNT_PCT", "25"))
# Trailing: arm only after +N R, distance = ATR x mult (never tighter than the strategy default).
SPOT_TRAIL_ARM_R = float(os.getenv("SPOT_TRAIL_ARM_R", "1.0"))
SPOT_TRAIL_ATR_MULT = float(os.getenv("SPOT_TRAIL_ATR_MULT", "1.0"))
# 2) Trend longs only in a daily bull regime (symbol and BTC), capped stop.
SPOT_TREND_REQUIRE_BULL = _bool("SPOT_TREND_REQUIRE_BULL", "true")
SPOT_TREND_MAX_STOP_PCT = float(os.getenv("SPOT_TREND_MAX_STOP_PCT", "6"))
SPOT_TREND_STRATEGIES = tuple(
    s.strip() for s in os.getenv("SPOT_TREND_STRATEGIES", "trend,donchian_trend,macro_momentum_hybrid").split(",")
    if s.strip())
SPOT_REGIME_SYMBOL = os.getenv("SPOT_REGIME_SYMBOL", "BTC/USDT")
# 3) Sizing: fixed (legacy TRADE_AMOUNT_USDT cap) or risk %% of equity.
SPOT_SIZING = os.getenv("SPOT_SIZING", "fixed").strip().lower()
SPOT_RISK_PCT = float(os.getenv("SPOT_RISK_PCT", "0.5"))
SPOT_MAX_POSITION_EQUITY_PCT = float(os.getenv("SPOT_MAX_POSITION_EQUITY_PCT",
                                               os.getenv("MAX_POSITION_EQUITY_PCT", "0") or "0") or 0) or 10.0

if SPOT_SIZING not in ("fixed", "risk_pct"):
    raise ValueError("SPOT_SIZING must be fixed or risk_pct")
if not 0 < SPOT_RISK_PCT <= 2.0:
    raise ValueError("SPOT_RISK_PCT must be in (0, 2]")
if not 0 < SPOT_MAX_POSITION_EQUITY_PCT <= 50:
    raise ValueError("SPOT_MAX_POSITION_EQUITY_PCT must be in (0, 50]")


def round_trip_fee_pct(bnb_discount=False, taker_pct=None):
    taker = SPOT_TAKER_FEE_PCT if taker_pct is None else float(taker_pct)
    if bnb_discount:
        taker *= (1.0 - SPOT_BNB_FEE_DISCOUNT_PCT / 100.0)
    return 2.0 * taker


def target_covers_fees(strategy, entry, target, bnb_discount=False, mult=None):
    """Return (ok, target_pct, required_pct). Only enforced for SPOT_MIN_TARGET_STRATEGIES."""
    mult = SPOT_MIN_TARGET_FEE_MULT if mult is None else float(mult)
    entry, target = float(entry or 0), float(target or 0)
    target_pct = (target - entry) / entry * 100.0 if entry > 0 else 0.0
    required = mult * round_trip_fee_pct(bnb_discount)
    if mult <= 0 or strategy not in SPOT_MIN_TARGET_STRATEGIES or entry <= 0:
        return True, target_pct, required
    return target_pct >= required, target_pct, required


def stop_distance(strategy, entry, atr, stop_atr, max_stop_pct=None):
    dist = float(atr) * float(stop_atr)
    cap_pct = SPOT_TREND_MAX_STOP_PCT if max_stop_pct is None else float(max_stop_pct)
    if strategy in SPOT_TREND_STRATEGIES and cap_pct > 0:
        dist = min(dist, float(entry) * cap_pct / 100.0)
    return dist


def trail_distance(atr, strategy_trail_atr, mult=None):
    mult = SPOT_TRAIL_ATR_MULT if mult is None else float(mult)
    return float(atr) * max(float(strategy_trail_atr), mult if mult > 0 else 0.0)


def legacy_trail_distance(entry, risk_distance, legacy_pct):
    """Positions saved without ATR trail: fall back to 1R instead of a fixed tiny %."""
    if risk_distance and float(risk_distance) > 0:
        return float(risk_distance)
    return float(entry) * float(legacy_pct) / 100.0


def trail_armed(entry, highest, risk_distance, partial_taken, legacy_activation_pct, arm_r=None):
    arm_r = SPOT_TRAIL_ARM_R if arm_r is None else float(arm_r)
    entry, highest = float(entry), float(highest)
    if arm_r <= 0:  # legacy behaviour
        return bool(partial_taken) and highest >= entry * (1 + legacy_activation_pct / 100.0)
    rd = float(risk_distance or 0)
    if rd <= 0:
        return bool(partial_taken) and highest >= entry * (1 + legacy_activation_pct / 100.0)
    return highest >= entry + arm_r * rd


def ema_last(values, period):
    values = [float(v) for v in values if v is not None]
    if len(values) < period:
        return None
    ema = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for v in values[period:]:
        ema = v * alpha + ema * (1 - alpha)
    return ema


def bull_regime(daily_closes, price=None):
    """price > EMA50 and price > EMA200 on daily closes (None when not enough data)."""
    if not daily_closes:
        return None
    price = float(daily_closes[-1] if price is None else price)
    e50, e200 = ema_last(daily_closes, 50), ema_last(daily_closes, 200)
    if e50 is None or e200 is None:
        return None
    return price > e50 and price > e200


def trend_entry_allowed(strategy, symbol_closes, price, btc_closes, btc_price=None, enabled=None):
    enabled = SPOT_TREND_REQUIRE_BULL if enabled is None else enabled
    if not enabled or strategy not in SPOT_TREND_STRATEGIES:
        return True, ""
    sym = bull_regime(symbol_closes, price)
    btc = bull_regime(btc_closes, btc_price)
    if sym is None or btc is None:
        return False, "بيانات يومية غير كافية لتأكيد الاتجاه الصاعد (EMA50/EMA200)"
    if not sym:
        return False, "السعر تحت EMA50 أو EMA200 اليومي؛ الشراء بالاتجاه ممنوع في سوق هابط"
    if not btc:
        return False, "BTC تحت EMA50 أو EMA200 اليومي؛ لا شراء اتجاه في Spot"
    return True, ""


def spot_notional(equity, entry, stop_dist, trade_amount, legacy_sizer, sizing=None,
                  risk_pct=None, max_equity_pct=None):
    """fixed: legacy sizer (capped at trade_amount). risk_pct: risk %% of equity, capped at
    max_equity_pct %% of equity."""
    sizing = SPOT_SIZING if sizing is None else sizing
    if sizing != "risk_pct":
        return legacy_sizer()
    risk_pct = SPOT_RISK_PCT if risk_pct is None else float(risk_pct)
    cap_pct = SPOT_MAX_POSITION_EQUITY_PCT if max_equity_pct is None else float(max_equity_pct)
    equity, entry, stop_dist = float(equity), float(entry), float(stop_dist)
    if equity <= 0 or entry <= 0 or stop_dist <= 0:
        return legacy_sizer()
    return min(equity * cap_pct / 100.0, equity * risk_pct / 100.0 / (stop_dist / entry))


# ---------------------------------------------------------------------------
# v2 guards (feat/spot-guard-v2)
# ---------------------------------------------------------------------------
# 1) Legacy stop cap + stop-breach watchdog for already-open Spot positions.
SPOT_ENFORCE_STOP_CAP_LEGACY = _bool("SPOT_ENFORCE_STOP_CAP_LEGACY", "true")
SPOT_STOP_WATCHDOG = _bool("SPOT_STOP_WATCHDOG", "true")
# 3) Trend on Spot: paused by default; optional 1d + volume confirmation when enabled.
SPOT_TREND_ENABLED = _bool("SPOT_TREND_ENABLED", "false")
SPOT_TREND_REQUIRE_1D_CONFIRM = _bool("SPOT_TREND_REQUIRE_1D_CONFIRM", "true")
SPOT_TREND_MIN_VOL_MULT = float(os.getenv("SPOT_TREND_MIN_VOL_MULT", "1.2"))
SPOT_TREND_VOL_LOOKBACK = int(os.getenv("SPOT_TREND_VOL_LOOKBACK", "20"))
SPOT_TREND_1D_EMA = int(os.getenv("SPOT_TREND_1D_EMA", "20"))

if SPOT_TREND_MIN_VOL_MULT < 0:
    raise ValueError("SPOT_TREND_MIN_VOL_MULT must be >= 0")
if SPOT_TREND_VOL_LOOKBACK < 2:
    raise ValueError("SPOT_TREND_VOL_LOOKBACK must be >= 2")


def capped_legacy_stop(entry, stop, cap_pct=None, enabled=None):
    """Return the tightened stop when an open long's stop is wider than cap_pct, else None.

    Only ever raises a long stop (never loosens it)."""
    enabled = SPOT_ENFORCE_STOP_CAP_LEGACY if enabled is None else enabled
    cap_pct = SPOT_TREND_MAX_STOP_PCT if cap_pct is None else float(cap_pct)
    entry = float(entry or 0)
    if not enabled or cap_pct <= 0 or entry <= 0:
        return None
    floor = entry * (1.0 - cap_pct / 100.0)
    stop = float(stop or 0)
    if stop <= 0 or stop < floor - entry * 1e-9:
        return floor
    return None


def stop_breached(price, stop, enabled=None):
    enabled = SPOT_STOP_WATCHDOG if enabled is None else enabled
    price, stop = float(price or 0), float(stop or 0)
    return bool(enabled) and price > 0 and stop > 0 and price <= stop


def is_trend_strategy(strategy):
    return strategy in SPOT_TREND_STRATEGIES


def daily_confirm(daily_closes, price=None, ema_period=None):
    """Price above a rising daily EMA (default EMA20). None when data is insufficient."""
    ema_period = SPOT_TREND_1D_EMA if ema_period is None else int(ema_period)
    closes = [float(c) for c in (daily_closes or []) if c is not None]
    if len(closes) < ema_period + 2:
        return None
    price = float(closes[-1] if price is None else price)
    now, prev = ema_last(closes, ema_period), ema_last(closes[:-1], ema_period)
    return price > now and now >= prev


def volume_confirm(volumes, mult=None, lookback=None):
    """Last closed bar volume >= mult x average of the previous `lookback` bars."""
    mult = SPOT_TREND_MIN_VOL_MULT if mult is None else float(mult)
    lookback = SPOT_TREND_VOL_LOOKBACK if lookback is None else int(lookback)
    if mult <= 0:
        return True, 0.0
    vols = [float(v) for v in (volumes or []) if v is not None]
    if len(vols) < lookback + 1:
        return None, 0.0
    avg = sum(vols[-lookback - 1:-1]) / lookback
    if avg <= 0:
        return None, 0.0
    ratio = vols[-1] / avg
    return ratio >= mult, ratio


def trend_v2_allowed(strategy, daily_closes_sym=None, price=None, volumes=None,
                     enabled=None, require_1d=None, vol_mult=None):
    """Return (ok, rule, reason). rule is a stable key for blocked_by counters."""
    if not is_trend_strategy(strategy):
        return True, "", ""
    enabled = SPOT_TREND_ENABLED if enabled is None else enabled
    require_1d = SPOT_TREND_REQUIRE_1D_CONFIRM if require_1d is None else require_1d
    if not enabled:
        return False, "trend_paused", "استراتيجية الاتجاه موقوفة على Spot (SPOT_TREND_ENABLED=false)"
    if require_1d:
        ok = daily_confirm(daily_closes_sym, price)
        if ok is None:
            return False, "trend_1d_confirm", "بيانات يومية غير كافية لتأكيد اتجاه 1d"
        if not ok:
            return False, "trend_1d_confirm", "السعر تحت EMA{} اليومي أو المتوسط لا يصعد".format(SPOT_TREND_1D_EMA)
    ok, ratio = volume_confirm(volumes, vol_mult)
    if ok is None:
        return False, "trend_volume", "بيانات حجم غير كافية"
    if not ok:
        return False, "trend_volume", "الحجم {:.2f}× أقل من المطلوب {:g}× من متوسط {} شمعة".format(
            ratio, SPOT_TREND_MIN_VOL_MULT if vol_mult is None else float(vol_mult), SPOT_TREND_VOL_LOOKBACK)
    return True, "", ""


# 2) Rejection counters: rule key -> count (in-memory since start; main.py also persists).
BLOCK_RULE_LABELS = {
    "unsupported_symbol": "زوج غير مدعوم",
    "frozen_symbol": "مركز يحتاج مطابقة",
    "monitor_error": "خطأ متابعة المراكز",
    "entries_paused": "إيقاف يدوي",
    "daily_loss_limit": "حد الخسارة اليومي",
    "max_open_trades": "حد الصفقات المفتوحة",
    "liquidity": "السيولة/ظروف السوق",
    "duplicate_candle": "إشارة مكررة",
    "news_block": "حظر الأخبار",
    "finnhub_block": "حظر Finnhub (أرباح/أخبار)",
    "negative_news": "خبر سلبي",
    "fee_target": "الهدف لا يغطي العمولة",
    "bull_filter": "فلتر الاتجاه الصاعد اليومي",
    "trend_paused": "الاتجاه موقوف على Spot",
    "trend_1d_confirm": "تأكيد اتجاه 1d",
    "trend_volume": "تأكيد الحجم",
    "rl_skip": "التعلم المعزز",
    "portfolio_risk": "مخاطر المحفظة",
}


class BlockCounter:
    def __init__(self):
        self.counts = {}

    def add(self, rule):
        if rule:
            self.counts[rule] = self.counts.get(rule, 0) + 1

    def snapshot(self):
        return dict(sorted(self.counts.items(), key=lambda kv: -kv[1]))


block_counter = BlockCounter()
