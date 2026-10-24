"""Offline historical research for the production strategy selector.

This module uses public OHLCV only. It never loads credentials and contains no
order-submission calls. Signal rules are imported unchanged from strategy_engine.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
from collections import defaultdict

import ccxt
from strategy_engine import STRATEGY_RISK, evaluate_symbol

TIMEFRAME_MS = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}
REQUIRED_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
SPOT_SYMBOLS = ('BTC/USDT','ETH/USDT','BNB/USDT','SOL/USDT','XRP/USDT','ADA/USDT','DOGE/USDT','LINK/USDT','AVAX/USDT','DOT/USDT','LTC/USDT','BCH/USDT','TRX/USDT','NEAR/USDT')
FUTURES_SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT")
PROFILES = {
    "baseline": {"stop": 1.0, "target": 1.0, "trail": 1.0},
    "cautious": {"stop": 0.90, "target": 0.90, "trail": 0.85},
    "balanced": {"stop": 0.95, "target": 1.15, "trail": 0.90},
    "runner": {"stop": 1.10, "target": 1.35, "trail": 1.10},
}


class HistoricalExchange:
    """Small CCXT-compatible view over candles available at one historical time."""

    def __init__(self, candles_by_symbol, now_ms):
        self.candles_by_symbol = candles_by_symbol
        self.now_ms = now_ms

    def fetch_ohlcv(self, symbol, timeframe="1m", limit=90, since=None, params=None):
        duration = TIMEFRAME_MS[timeframe]
        all_candles = self.candles_by_symbol.get(symbol, {}).get(timeframe, [])
        # Candle rows are sorted by open time. Binary-search the last candle
        # closed at this historical timestamp instead of rescanning all history.
        cutoff_ms = self.now_ms - duration
        lo, hi = 0, len(all_candles)
        while lo < hi:
            mid = (lo + hi) // 2
            if all_candles[mid][0] <= cutoff_ms:
                lo = mid + 1
            else:
                hi = mid
        end = lo
        if end == 0:
            return []
        # Production strategy_engine drops the final (still-open) candle. Add a
        # harmless synthetic open candle so its 90-candle window behaves alike.
        closed = all_candles[max(0, end - max(1, int(limit) - 1)):end]
        last = float(closed[-1][4])
        synthetic_open = [self.now_ms, last, last, last, last, 0.0]
        return closed + [synthetic_open]


def fetch_history(exchange, symbol, timeframe, since_ms, until_ms):
    duration = TIMEFRAME_MS[timeframe]
    cursor = since_ms - (since_ms % duration)
    rows = []
    seen = set()
    for _ in range(500):
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not batch:
            break
        for row in batch:
            stamp = int(row[0])
            if since_ms <= stamp < until_ms and stamp not in seen:
                seen.add(stamp)
                rows.append([int(row[0]), *[float(v) for v in row[1:6]]])
        last_stamp = int(batch[-1][0])
        if last_stamp >= until_ms - duration or len(batch) < 2:
            break
        next_cursor = last_stamp + duration
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    rows.sort(key=lambda row: row[0])
    return rows


def load_market_data(exchange, symbols, days):
    until_ms = exchange.milliseconds()
    since_ms = until_ms - int(days * 24 * 60 * 60_000)
    result = {symbol: {} for symbol in symbols}
    for symbol in symbols:
        for timeframe in REQUIRED_TIMEFRAMES:
            print("تحميل {} {} ...".format(symbol, timeframe), flush=True)
            result[symbol][timeframe] = fetch_history(
                exchange, symbol, timeframe, since_ms, until_ms)
            if not result[symbol][timeframe]:
                raise RuntimeError("لا توجد بيانات تاريخية لـ {} {}".format(symbol, timeframe))
    return result


def generate_signal_events(data, symbols, timeline, allow_short=False):
    events = defaultdict(list)
    for now_ms in timeline:
        historical = HistoricalExchange(data, now_ms)
        for symbol in symbols:
            setup = evaluate_symbol(historical, symbol, allow_short=allow_short)
            if setup and setup.get("signal"):
                events[now_ms].append({"symbol": symbol, "setup": dict(setup)})
    return events


def adjusted_risk(strategy, profile):
    base = STRATEGY_RISK[strategy]
    return {
        "stop_atr": base["stop_atr"] * profile["stop"],
        "target_atr": base["target_atr"] * profile["target"],
        "trail_atr": base["trail_atr"] * profile["trail"],
    }


def close_position(pos, raw_exit, timestamp, reason, fee_rate, slippage_rate):
    direction = pos["direction"]
    exit_price = raw_exit * (1.0 - slippage_rate if direction == "long" else 1.0 + slippage_rate)
    exit_notional = pos["quantity"] * exit_price
    exit_fee = exit_notional * fee_rate
    gross = pos["quantity"] * (
        exit_price - pos["entry_price"] if direction == "long"
        else pos["entry_price"] - exit_price
    )
    net = gross - pos["entry_fee"] - exit_fee
    return {
        "signal_id": pos["signal_id"], "symbol": pos["symbol"],
        "strategy": pos["strategy"], "direction": direction,
        "timeframe": pos["timeframe"], "signal_strength": pos["signal_strength"],
        "signal_time_utc": pos["signal_time_utc"],
        "entry_time_utc": pos["entry_time_utc"],
        "exit_time_utc": iso_utc(timestamp),
        "entry_price": round(pos["entry_price"], 10),
        "exit_price": round(exit_price, 10),
        "notional_usdt": round(pos["notional"], 4),
        "gross_pnl_usdt": round(gross, 6),
        "fees_usdt": round(pos["entry_fee"] + exit_fee, 6),
        "net_pnl_usdt": round(net, 6),
        "net_pnl_percent": round(net / pos["notional"] * 100.0, 6),
        "result": "WIN" if net > 0 else "LOSS",
        "exit_reason": reason,
        "bars_held": max(0, int((timestamp - pos["entry_ms"]) / TIMEFRAME_MS["5m"])),
    }


def iso_utc(timestamp_ms):
    return dt.datetime.fromtimestamp(timestamp_ms / 1000.0, tz=dt.timezone.utc).isoformat()


def manage_position(pos, candle, timestamp, fee_rate, slippage_rate):
    """Conservative OHLC exit handling; if stop and target both touch, stop wins."""
    op, high, low = float(candle[1]), float(candle[2]), float(candle[3])
    long_side = pos["direction"] == "long"
    stop = pos["stop_price"]
    target = pos["target_price"]

    if long_side:
        if op <= stop:
            return close_position(pos, op, timestamp, "stop_gap", fee_rate, slippage_rate)
        if low <= stop:
            return close_position(pos, stop, timestamp, "stop", fee_rate, slippage_rate)
        if op >= target:
            return close_position(pos, op, timestamp, "target_gap", fee_rate, slippage_rate)
        if high >= target:
            return close_position(pos, target, timestamp, "target", fee_rate, slippage_rate)
        pos["extreme"] = max(pos["extreme"], high)
        pos["stop_price"] = max(pos["stop_price"], pos["extreme"] - pos["trailing_distance"])
        if low <= pos["stop_price"]:
            return close_position(pos, pos["stop_price"], timestamp, "trailing_stop", fee_rate, slippage_rate)
    else:
        if op >= stop:
            return close_position(pos, op, timestamp, "stop_gap", fee_rate, slippage_rate)
        if high >= stop:
            return close_position(pos, stop, timestamp, "stop", fee_rate, slippage_rate)
        if op <= target:
            return close_position(pos, op, timestamp, "target_gap", fee_rate, slippage_rate)
        if low <= target:
            return close_position(pos, target, timestamp, "target", fee_rate, slippage_rate)
        pos["extreme"] = min(pos["extreme"], low)
        pos["stop_price"] = min(pos["stop_price"], pos["extreme"] + pos["trailing_distance"])
        if high >= pos["stop_price"]:
            return close_position(pos, pos["stop_price"], timestamp, "trailing_stop", fee_rate, slippage_rate)
    return None


def simulate_segment(data, symbols, timeline, events, start_ms, end_ms, profile,
                     mode, notional, capital, max_positions, fee_rate, slippage_rate):
    segment_times = [t for t in timeline if start_ms <= t < end_ms]
    if not segment_times:
        return {"metrics": empty_metrics(), "trades": [], "signals": []}
    base_by_open = {
        symbol: {int(row[0]): row for row in data[symbol]["5m"]}
        for symbol in symbols
    }
    positions = {}
    trades = []
    signals = {}
    entered_keys = set()
    effective_slots = max(1, min(max_positions, int(capital // notional)))
    allow_short = mode == "futures"

    for now_ms in segment_times:
        closed_bar_open = now_ms - TIMEFRAME_MS["5m"]
        # Match production scan order: manage exits before considering new entries.
        for symbol in list(positions):
            candle = base_by_open[symbol].get(closed_bar_open)
            if candle is None:
                continue
            trade = manage_position(positions[symbol], candle, now_ms, fee_rate, slippage_rate)
            if trade:
                trades.append(trade)
                sid = trade["signal_id"]
                if sid in signals:
                    signals[sid].update({"status": "closed", "result": trade["result"],
                                         "net_pnl_usdt": trade["net_pnl_usdt"],
                                         "exit_time_utc": trade["exit_time_utc"],
                                         "exit_reason": trade["exit_reason"]})
                del positions[symbol]

        for event in events.get(now_ms, []):
            symbol, setup = event["symbol"], event["setup"]
            signal_key = (symbol, setup.get("strategy"), setup.get("direction"),
                          setup.get("timeframe"), setup.get("candle_ts"))
            signal_id = "|".join(str(part) for part in signal_key)
            if signal_id not in signals:
                signals[signal_id] = {
                    "signal_id": signal_id, "symbol": symbol,
                    "strategy": setup.get("strategy"), "direction": setup.get("direction"),
                    "timeframe": setup.get("timeframe"),
                    "signal_strength": setup.get("signal_strength", 0),
                    "signal_time_utc": iso_utc(now_ms), "strategy_candle": setup.get("candle_ts"),
                    "status": "not_entered", "result": "", "net_pnl_usdt": "",
                    "exit_time_utc": "", "exit_reason": "",
                }
            if signal_id in entered_keys:
                continue
            if symbol in positions:
                signals[signal_id]["status"] = "position_open"
                continue
            if len(positions) >= effective_slots:
                signals[signal_id]["status"] = "capacity_limit"
                continue
            if mode == "spot" and setup.get("direction") != "long":
                signals[signal_id]["status"] = "unsupported_direction"
                continue

            entry_bar = base_by_open[symbol].get(now_ms)
            if entry_bar is None or now_ms + TIMEFRAME_MS["5m"] >= end_ms:
                signals[signal_id]["status"] = "no_next_bar"
                continue
            raw_entry = float(entry_bar[1])
            direction = "short" if allow_short and setup.get("direction") == "short" else "long"
            entry_price = raw_entry * (1.0 + slippage_rate if direction == "long" else 1.0 - slippage_rate)
            quantity = notional / entry_price
            entry_fee = quantity * entry_price * fee_rate
            atr = max(float(setup.get("atr") or 0.0), entry_price * 0.0005)
            risk = adjusted_risk(setup["strategy"], profile)
            if direction == "long":
                stop = entry_price - atr * risk["stop_atr"]
                target = entry_price + atr * risk["target_atr"]
                extreme = entry_price
            else:
                stop = entry_price + atr * risk["stop_atr"]
                target = entry_price - atr * risk["target_atr"]
                extreme = entry_price
            positions[symbol] = {
                "signal_id": signal_id, "symbol": symbol, "strategy": setup["strategy"],
                "direction": direction, "timeframe": setup["timeframe"],
                "signal_strength": setup.get("signal_strength", 0),
                "signal_time_utc": iso_utc(now_ms), "entry_time_utc": iso_utc(now_ms),
                "entry_ms": now_ms, "entry_price": entry_price, "quantity": quantity,
                "notional": notional, "entry_fee": entry_fee, "stop_price": stop,
                "target_price": target, "trailing_distance": atr * risk["trail_atr"],
                "extreme": extreme,
            }
            entered_keys.add(signal_id)
            signals[signal_id].update({"status": "open", "entry_price": round(entry_price, 10),
                                       "entry_time_utc": iso_utc(now_ms)})
    # Close remaining positions at the final available close and charge exit costs.
    final_close_ms = segment_times[-1]
    last_open = final_close_ms - TIMEFRAME_MS["5m"]
    for symbol, pos in list(positions.items()):
        candle = base_by_open[symbol].get(last_open)
        if candle:
            trade = close_position(pos, float(candle[4]), final_close_ms,
                                   "period_end_mark", fee_rate, slippage_rate)
            trades.append(trade)
            signals[pos["signal_id"]].update({
                "status": "marked_to_market", "result": trade["result"],
                "net_pnl_usdt": trade["net_pnl_usdt"], "exit_time_utc": trade["exit_time_utc"],
                "exit_reason": trade["exit_reason"],
            })
    return {"metrics": metrics(trades, capital), "trades": trades,
            "signals": list(signals.values())}


def empty_metrics():
    return {"trades": 0, "wins": 0, "losses": 0, "win_rate_percent": 0.0,
            "net_pnl_usdt": 0.0, "expectancy_usdt": 0.0, "profit_factor": None,
            "max_drawdown_usdt": 0.0, "return_on_capital_percent": 0.0}


def metrics(trades, capital):
    if not trades:
        return empty_metrics()
    pnls = [float(t["net_pnl_usdt"]) for t in trades]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    equity = peak = float(capital)
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate_percent": round(len(wins) / len(pnls) * 100.0, 3),
        "net_pnl_usdt": round(sum(pnls), 6),
        "expectancy_usdt": round(sum(pnls) / len(pnls), 6),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else None,
        "max_drawdown_usdt": round(max_dd, 6),
        "return_on_capital_percent": round(sum(pnls) / capital * 100.0, 4),
    }


def write_csv(path, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def profile_score(result):
    m = result["metrics"]
    return m["net_pnl_usdt"] - m["max_drawdown_usdt"]


def run(args):
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise ValueError("حدد زوجًا واحدًا على الأقل عبر --symbols")
    exchange = ccxt.binanceusdm({"enableRateLimit": True}) if args.market == "futures" else ccxt.binance({"enableRateLimit": True})
    data = load_market_data(exchange, symbols, args.days)
    # The simulator uses 5m close timestamps as a common clock. All strategy
    # timeframes are multiples of 5m.
    timeline = sorted({
        int(row[0]) + TIMEFRAME_MS["5m"]
        for symbol in symbols for row in data[symbol]["5m"]
    })
    if len(timeline) < 200:
        raise RuntimeError("البيانات المتاحة قصيرة جدًا لإجراء مقارنة موثوقة")
    split_index = max(1, min(len(timeline) - 1, int(len(timeline) * args.train_percent / 100.0)))
    split_ms = timeline[split_index]
    end_ms = timeline[-1] + 1
    events = generate_signal_events(data, symbols, timeline, allow_short=args.market == "futures")

    fee_percent = args.fee_percent
    if fee_percent is None:
        fee_percent = 0.05 if args.market == "futures" else 0.10
    fee_rate = fee_percent / 100.0
    slippage_rate = args.slippage_percent / 100.0
    summaries = {}
    details = {}
    for name, profile in PROFILES.items():
        train = simulate_segment(
            data, symbols, timeline, events, timeline[0], split_ms, profile,
            args.market, args.trade_usdt, args.capital_usdt, args.max_positions,
            fee_rate, slippage_rate)
        test = simulate_segment(
            data, symbols, timeline, events, split_ms, end_ms, profile,
            args.market, args.trade_usdt, args.capital_usdt, args.max_positions,
            fee_rate, slippage_rate)
        summaries[name] = {"profile": profile, "train": train["metrics"], "out_of_sample": test["metrics"]}
        details[name] = {"train": train, "out_of_sample": test}

    baseline_oos = summaries["baseline"]["out_of_sample"]
    eligible = [
        name for name in PROFILES if name != "baseline"
        and summaries[name]["train"]["trades"] >= args.min_trades
        and summaries[name]["out_of_sample"]["trades"] >= args.min_trades
    ]
    winner = max(eligible, key=lambda n: profile_score({"metrics": summaries[n]["train"]})) if eligible else None
    recommendation = {
        "status": "no_change",
        "message": "لا توجد توصية مدعومة بعينة كافية وتحسن خارج العينة.",
        "candidate": None,
        "requires_manual_review": True,
    }
    if winner:
        candidate_oos = summaries[winner]["out_of_sample"]
        if (candidate_oos["net_pnl_usdt"] > baseline_oos["net_pnl_usdt"]
                and candidate_oos["max_drawdown_usdt"] <= baseline_oos["max_drawdown_usdt"]):
            recommendation = {
                "status": "review_candidate",
                "message": "تحسن المرشح خارج العينة مع عدم زيادة أقصى تراجع؛ راجعه يدويًا قبل تجربة Demo.",
                "candidate": winner,
                "profile": PROFILES[winner],
                "requires_manual_review": True,
            }

    report = {
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "offline_research_only",
        "market": args.market,
        "symbols": symbols,
        "history_days_requested": args.days,
        "train_percent": args.train_percent,
        "out_of_sample_start_utc": iso_utc(split_ms),
        "out_of_sample_end_utc": iso_utc(end_ms),
        "cost_model": {
            "fee_percent_per_side": fee_percent,
            "slippage_percent_per_side": args.slippage_percent,
            "trade_notional_usdt": args.trade_usdt,
            "capital_usdt": args.capital_usdt,
            "max_concurrent_positions": min(args.max_positions, int(args.capital_usdt // args.trade_usdt)),
        },
        "execution_safety": {
            "uses_api_credentials": False, "submits_orders": False,
            "changes_strategy_engine": False, "automatic_promotion": False,
        },
        "limitations": [
            "إشارات الدخول مأخوذة من strategy_engine.py دون تعديل.",
            "الاختبارات تقارن مخارج ATR فقط؛ لا تغيّر قواعد الإشارة.",
            "الرسوم والانزلاق تقديرات ثابتة لكل جانب وليست رسوم حسابك الفعلية.",
            "لا يشمل هذا النموذج فلتر الأخبار التاريخي أو تمويل Futures أو الإضافة إلى مراكز Spot.",
            "تُستخدم شموع 5m مع معالجة محافظة؛ إذا لامس الوقف والهدف في الشمعة نفسها يُحتسب الوقف أولاً.",
            "النتيجة التاريخية لا تضمن أداءً مستقبليًا.",
        ],
        "variants": summaries,
        "recommendation": recommendation,
        "artifacts": {"trades": args.trades_csv, "signals": args.signals_csv},
    }
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    baseline_signals = details["baseline"]["out_of_sample"]["signals"]
    baseline_trades = details["baseline"]["out_of_sample"]["trades"]
    write_csv(args.signals_csv, baseline_signals)
    write_csv(args.trades_csv, baseline_trades)

    print("\n=== مقارنة الاستراتيجيات الحالية · {}".format(args.market))
    print("الرسوم {:.3f}% لكل جانب | الانزلاق {:.3f}% لكل جانب | فترة الاختبار خارج العينة تبدأ {}".format(
        fee_percent, args.slippage_percent, iso_utc(split_ms)))
    for name, result in summaries.items():
        tr, oos = result["train"], result["out_of_sample"]
        print("{}: تدريب {} صفقة، PnL {:+.2f} | خارج العينة {} صفقة، PnL {:+.2f}، تراجع {:.2f}".format(
            name, tr["trades"], tr["net_pnl_usdt"], oos["trades"],
            oos["net_pnl_usdt"], oos["max_drawdown_usdt"]))
    print("التوصية: {}".format(recommendation["message"]))
    if recommendation["candidate"]:
        print("مرشح للمراجعة فقط: {} {}".format(recommendation["candidate"], recommendation["profile"]))
    print("حُفظ التقرير والإشارات والصفقات في: {}, {}, {}".format(
        args.report, args.signals_csv, args.trades_csv))
    print("هذا المسار للبحث فقط؛ لم تُرسل أي أوامر ولم تتغير إعدادات Demo.")


def parse_args():
    parser = argparse.ArgumentParser(description="اختبار تاريخي وتحليل ظلّي لاستراتيجيات التداول")
    parser.add_argument("--market", choices=("spot", "futures"), default="spot")
    parser.add_argument("--symbols", default=None,
                        help="قائمة أزواج مفصولة بفواصل؛ Futures بصيغة CCXT مثل BTC/USDT:USDT")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--train-percent", type=int, default=70)
    parser.add_argument("--trade-usdt", type=float, default=200.0)
    parser.add_argument("--capital-usdt", type=float, default=5000.0)
    parser.add_argument("--max-positions", type=int, default=6)
    parser.add_argument("--fee-percent", type=float, default=None,
                        help="رسوم كل جانب بالنسبة المئوية؛ الافتراضي 0.10 Spot و0.05 Futures")
    parser.add_argument("--slippage-percent", type=float, default=0.05,
                        help="انزلاق تقديري لكل جانب بالنسبة المئوية")
    parser.add_argument("--min-trades", type=int, default=20,
                        help="الحد الأدنى للصفقات في التدريب وخارج العينة لقبول مرشح")
    parser.add_argument("--report", default="research_report.json")
    parser.add_argument("--signals-csv", default="signal_results.csv")
    parser.add_argument("--trades-csv", default="trade_results.csv")
    args = parser.parse_args()
    if args.symbols is None:
        args.symbols = ",".join(FUTURES_SYMBOLS if args.market == "futures" else SPOT_SYMBOLS)
    if args.days < 30:
        parser.error("--days يجب أن يكون 30 أو أكثر")
    if not 50 <= args.train_percent <= 90:
        parser.error("--train-percent يجب أن يكون بين 50 و90")
    if min(args.trade_usdt, args.capital_usdt) <= 0 or args.max_positions < 1:
        parser.error("قيم رأس المال والصفقة والحد الأقصى يجب أن تكون موجبة")
    if args.capital_usdt < args.trade_usdt:
        parser.error("--capital-usdt يجب ألا يقل عن --trade-usdt")
    if args.fee_percent is not None and args.fee_percent < 0:
        parser.error("--fee-percent لا يمكن أن يكون سالبًا")
    if args.slippage_percent < 0 or args.min_trades < 1:
        parser.error("الانزلاق يجب أن يكون غير سالب والحد الأدنى للصفقات موجبًا")
    return args


if __name__ == "__main__":
    try:
        run(parse_args())
    except KeyboardInterrupt:
        print("\nأُلغي التحليل؛ لم تُرسل أوامر.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print("فشل التحليل: {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        sys.exit(1)
