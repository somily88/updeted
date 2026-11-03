"""Per-market capital allocator (Spot and Futures are separate Binance wallets).

When CAPITAL_ALLOCATOR_ENABLED=true:
  * budget      = equity x ALLOCATION_PCT / 100 (the rest is a reserve that is
                  never used for new entries; on Futures it stays as free margin).
  * used        = sum of open position cost (Spot) / margin (Futures, 1x => notional).
  * open_risk   = sum of qty x distance-to-stop on the losing side (0 once the stop
                  is at or beyond entry).
  * size        = equity x RISK_PER_TRADE_PCT x score_mult x vol_scale
                  / max(stop%, SIZING_MIN_STOP_PCT)
                  capped only by remaining budget, open-risk headroom
                  (PORTFOLIO_MAX_OPEN_RISK_PCT), free balance and exchange max.
                  Below the exchange minimum => skip (never rounded up).
  * No fixed TRADE_AMOUNT_USDT / MAX_POSITION_EQUITY_PCT cap and no max-trades count.
  * Never adds to an open position (no averaging down / martingale).
Default is OFF so deploying changes nothing until the user enables it.
"""
import os
import time


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _b(name, default="false"):
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "on")


CAPITAL_ALLOCATOR_ENABLED = _b("CAPITAL_ALLOCATOR_ENABLED", "false")
ALLOCATION_PCT = _f("ALLOCATION_PCT", 50)
PORTFOLIO_MAX_OPEN_RISK_PCT = _f("PORTFOLIO_MAX_OPEN_RISK_PCT", 4)
SCORE_MULT_MIN = _f("SCORE_MULT_MIN", 0.5)
SCORE_MULT_MAX = _f("SCORE_MULT_MAX", 1.2)
SIZING_MIN_STOP_PCT = _f("SIZING_MIN_STOP_PCT", 1.5)
SPOT_MAX_DIRECTIONAL_RISK_PCT = _f("SPOT_MAX_DIRECTIONAL_RISK_PCT", 3)
SPOT_MAX_SAME_DIRECTION_ALTS = int(_f("SPOT_MAX_SAME_DIRECTION_ALTS", 0))  # 0 = no count limit
STATS_MIN_TRADES = int(_f("ALLOCATOR_STATS_MIN_TRADES", 30))

if not 1 <= ALLOCATION_PCT <= 100:
    raise ValueError("ALLOCATION_PCT must be in [1, 100]")
if not 0 < PORTFOLIO_MAX_OPEN_RISK_PCT <= 20:
    raise ValueError("PORTFOLIO_MAX_OPEN_RISK_PCT must be in (0, 20]")
if not 0 < SCORE_MULT_MIN <= SCORE_MULT_MAX <= 3:
    raise ValueError("SCORE_MULT_MIN/MAX must satisfy 0 < MIN <= MAX <= 3")
if not 0 <= SIZING_MIN_STOP_PCT <= 20:
    raise ValueError("SIZING_MIN_STOP_PCT must be in [0, 20]")

IGNORED_WHEN_ENABLED = ["MAX_POSITION_EQUITY_PCT", "SPOT_MAX_POSITION_EQUITY_PCT", "TRADE_AMOUNT_USDT",
                        "SPOT_SIZING", "MAX_SPOT_CONCURRENT_TRADES", "MAX_FUTURES_CONCURRENT_TRADES"]

RULE_LABELS_AR = {
    "budget_full": "ميزانية التداول (50%) ممتلئة",
    "open_risk_cap": "بلغت المخاطرة المفتوحة الحد الأقصى",
    "below_min_notional": "الحجم المسموح أقل من الحد الأدنى للمنصة",
    "insufficient_free": "الرصيد المتاح لا يكفي",
    "corr_max_same_direction": "حد الترابط: عدد المراكز في نفس الاتجاه",
    "corr_directional_risk": "حد الترابط: مخاطرة الاتجاه الواحد",
    "no_add_open_position": "ممنوع زيادة مركز مفتوح (لا تعديل للمتوسط)",
    "invalid_stop": "مسافة وقف غير صالحة",
}


def enabled():
    return CAPITAL_ALLOCATOR_ENABLED


def concurrency_allows(open_count, max_count, allocator_enabled=None):
    """Legacy max-trades count (env or DB value). Ignored when the allocator is on."""
    on = CAPITAL_ALLOCATOR_ENABLED if allocator_enabled is None else allocator_enabled
    if on:
        return True
    return int(open_count) < int(max_count)


def position_open_risk(pos):
    """USDT lost if the stop is hit now (0 when the stop already locks entry or better)."""
    qty = float(pos.get("amount") or 0)
    entry = float(pos.get("entry_price") or 0)
    stop = pos.get("stop_price")
    if qty <= 0 or entry <= 0:
        return 0.0
    if stop is None or float(stop) <= 0:
        dist = float(pos.get("risk_distance") or 0)
        return max(0.0, qty * dist)
    stop = float(stop)
    if str(pos.get("side", "long")) == "short":
        return max(0.0, (stop - entry) * qty)
    return max(0.0, (entry - stop) * qty)


def position_used(pos):
    """Capital tied up: Spot cost, Futures margin (1x isolated => entry notional)."""
    cost = pos.get("cost")
    if cost is not None:
        return max(0.0, float(cost))
    return max(0.0, float(pos.get("amount") or 0) * float(pos.get("entry_price") or 0))


def market_state(equity, positions, allocation_pct=None, max_open_risk_pct=None):
    alloc = ALLOCATION_PCT if allocation_pct is None else float(allocation_pct)
    risk_pct = PORTFOLIO_MAX_OPEN_RISK_PCT if max_open_risk_pct is None else float(max_open_risk_pct)
    equity = max(0.0, float(equity or 0))
    pos = [p for p in (positions or {}).values() if not p.get("simulated")]
    budget = equity * alloc / 100.0
    used = sum(position_used(p) for p in pos)
    open_risk = sum(position_open_risk(p) for p in pos)
    risk_limit = equity * risk_pct / 100.0
    return {
        "enabled": CAPITAL_ALLOCATOR_ENABLED, "equity": round(equity, 2),
        "allocation_pct": alloc, "budget": round(budget, 2), "used": round(used, 2),
        "remaining": round(max(0.0, budget - used), 2),
        "reserve": round(equity - budget, 2),
        "open_risk": round(open_risk, 2),
        "open_risk_pct": round(open_risk / equity * 100.0, 3) if equity else 0.0,
        "max_open_risk_pct": risk_pct, "risk_headroom": round(max(0.0, risk_limit - open_risk), 2),
        "positions": len(pos),
    }


def score_multiplier(setup=None, strategy_stats=None, strategy_mult=1.0, rl_multiplier=None,
                     rl_mode="off", lo=None, hi=None, min_trades=None):
    """0.5..1.2 by default. Signal strength (0-100, 50 = neutral) x strategy
    expectancy (only after min_trades closed trades) x FUTURES_STRATEGY_SIZE_MULT
    x RL (only when RL_MODE=live)."""
    lo = SCORE_MULT_MIN if lo is None else float(lo)
    hi = SCORE_MULT_MAX if hi is None else float(hi)
    min_trades = STATS_MIN_TRADES if min_trades is None else int(min_trades)
    setup = setup or {}
    strength = setup.get("signal_strength")
    m = 1.0
    if strength is not None:
        s = max(0.0, min(100.0, float(strength)))
        m *= 0.5 + s / 100.0          # 0 -> 0.5, 50 -> 1.0, 100 -> 1.5
    st = strategy_stats or {}
    if int(st.get("trades") or 0) >= min_trades and st.get("expectancy_r") is not None:
        m *= 1.0 + max(-0.5, min(0.5, float(st["expectancy_r"])))
    m *= max(0.0, float(strategy_mult if strategy_mult is not None else 1.0))
    if str(rl_mode).lower() == "live" and rl_multiplier is not None:
        m *= max(0.0, float(rl_multiplier))
    return max(lo, min(hi, m))


def plan_entry(equity, price, stop_distance, positions, score_mult=1.0, vol_scale=1.0,
               risk_pct=None, min_notional=0.0, max_notional=None, free_balance=None,
               symbol=None, allocation_pct=None, max_open_risk_pct=None, min_stop_pct=None):
    """Return dict(allowed, rule, reason, notional, risk_usdt, state)."""
    if risk_pct is None:
        from strategy_engine import RISK_PER_TRADE_PCT as risk_pct
    min_stop = SIZING_MIN_STOP_PCT if min_stop_pct is None else float(min_stop_pct)
    state = market_state(equity, positions, allocation_pct, max_open_risk_pct)
    out = {"allowed": False, "rule": None, "reason": None, "notional": 0.0, "risk_usdt": 0.0, "state": state}
    price, stop_distance, equity = float(price or 0), float(stop_distance or 0), float(equity or 0)
    if price <= 0 or stop_distance <= 0 or equity <= 0:
        out.update(rule="invalid_stop", reason=RULE_LABELS_AR["invalid_stop"])
        return out
    if symbol and symbol in (positions or {}):
        out.update(rule="no_add_open_position", reason=RULE_LABELS_AR["no_add_open_position"])
        return out
    stop_frac = stop_distance / price
    sizing_frac = max(stop_frac, min_stop / 100.0)
    want = equity * float(risk_pct) / 100.0 * float(score_mult) * float(vol_scale) / sizing_frac
    min_notional = float(min_notional or 0)
    caps = [("budget_full", state["remaining"]), ("open_risk_cap", state["risk_headroom"] / stop_frac)]
    if free_balance is not None:
        caps.append(("insufficient_free", float(free_balance)))
    if max_notional:
        caps.append((None, float(max_notional)))
    notional, binding = want, None
    for rule, cap in caps:
        if cap < notional:
            notional, binding = cap, rule
    if notional <= 0 or notional < min_notional:
        rule = binding or "below_min_notional"
        out.update(rule=rule, reason="{} (المتاح {:.2f} < الحد الأدنى {:.2f} USDT)".format(
            RULE_LABELS_AR[rule], max(0.0, notional), min_notional))
        return out
    out.update(allowed=True, notional=notional, risk_usdt=notional * stop_frac, wanted=want, binding_cap=binding)
    return out


def spot_correlation_guard(positions, candidate_risk_usdt, equity, max_risk_pct=None, max_count=None):
    """Spot is long-only and alts track BTC: cap total long open risk and (optionally) count."""
    max_risk_pct = SPOT_MAX_DIRECTIONAL_RISK_PCT if max_risk_pct is None else float(max_risk_pct)
    max_count = SPOT_MAX_SAME_DIRECTION_ALTS if max_count is None else int(max_count)
    live = [p for p in (positions or {}).values() if not p.get("simulated")]
    if max_count > 0 and len(live) >= max_count:
        return False, "corr_max_same_direction", "حد الترابط: {} مراكز Spot شراء مفتوحة (الحد {})".format(len(live), max_count)
    if max_risk_pct > 0 and float(equity or 0) > 0:
        total = sum(position_open_risk(p) for p in live) + float(candidate_risk_usdt or 0)
        limit = float(equity) * max_risk_pct / 100.0
        if total > limit:
            return False, "corr_directional_risk", \
                "حد الترابط: مخاطرة شراء Spot ستصبح {:.2f} USDT > {:.1f}% من الرصيد ({:.2f})".format(total, max_risk_pct, limit)
    return True, None, None


def assert_no_increase(positions, symbol):
    """Hard guard used before any order that would raise qty on an open position."""
    if CAPITAL_ALLOCATOR_ENABLED and symbol in (positions or {}):
        raise RuntimeError("capital allocator: refusing to add to open position {}".format(symbol))


_stats_cache = {"t": 0.0, "v": {}}


def strategy_stats(db_path, market, strategy, ttl=600):
    now = time.time()
    if now - _stats_cache["t"] > ttl:
        try:
            from trade_stats import load_strategy_stats
            _stats_cache["v"] = {(r["market"], r["strategy"]): r for r in load_strategy_stats(db_path)}
        except Exception:
            _stats_cache["v"] = {}
        _stats_cache["t"] = now
    return _stats_cache["v"].get((market, strategy)) or {}


def config_snapshot():
    return {"enabled": CAPITAL_ALLOCATOR_ENABLED, "allocation_pct": ALLOCATION_PCT,
            "max_open_risk_pct": PORTFOLIO_MAX_OPEN_RISK_PCT, "score_mult_min": SCORE_MULT_MIN,
            "score_mult_max": SCORE_MULT_MAX, "sizing_min_stop_pct": SIZING_MIN_STOP_PCT,
            "spot_max_directional_risk_pct": SPOT_MAX_DIRECTIONAL_RISK_PCT,
            "ignored_when_enabled": IGNORED_WHEN_ENABLED if CAPITAL_ALLOCATOR_ENABLED else []}
