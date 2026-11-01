"""Walk-forward comparison: baseline vs RL (contextual bandit, live mode).

The RL agent starts empty and learns online in time order from trades that
already closed (no lookahead). Research only: no API keys, no orders.

    python rl_backtest.py --market futures --days 60 --seeds 5
"""
import argparse
import json
import random
import statistics

import ccxt

from backtest import (PROFILES, TIMEFRAME_MS, generate_signal_events, load_market_data,
                      simulate_segment)
from rl_bandit import BanditAgent


def summarize(trades, start_ms=None):
    from datetime import datetime, timezone
    picked = trades
    if start_ms is not None:
        picked = [t for t in trades if datetime.fromisoformat(t["entry_time_utc"].replace("Z", "+00:00")).timestamp() * 1000 >= start_ms]
    rs = [float(t["r_multiple"]) for t in picked if t.get("r_multiple") is not None]
    net = sum(float(t["net_pnl_usdt"]) for t in picked)
    wins = sum(1 for t in picked if float(t["net_pnl_usdt"]) > 0)
    return {"trades": len(picked), "net_usdt": round(net, 2),
            "win_rate_pct": round(wins / len(picked) * 100, 1) if picked else 0.0,
            "avg_r": round(statistics.mean(rs), 3) if rs else None,
            "sum_r_sized": round(sum(float(t["r_multiple"]) * float(t.get("rl_multiplier") or 1.0)
                                     for t in picked if t.get("r_multiple") is not None), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=("spot", "futures"), default="futures")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--trade-usdt", type=float, default=200.0)
    ap.add_argument("--capital-usdt", type=float, default=2000.0)
    ap.add_argument("--max-positions", type=int, default=6)
    ap.add_argument("--slippage-percent", type=float, default=0.03)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--min-samples", default="30,10")
    ap.add_argument("--oos-percent", type=int, default=70)
    ap.add_argument("--report", default="rl_report.json")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.market == "futures":
        symbols = [s if ":" in s else s + ":USDT" for s in symbols]
        exchange = ccxt.binanceusdm({"enableRateLimit": True})
    else:
        exchange = ccxt.binance({"enableRateLimit": True})
    data = load_market_data(exchange, symbols, args.days, include_funding=args.market == "futures")
    timeline = sorted({int(r[0]) + TIMEFRAME_MS["5m"] for s in symbols for r in data[s]["5m"]})
    events = generate_signal_events(data, symbols, timeline, allow_short=args.market == "futures")
    fee = (0.05 if args.market == "futures" else 0.10) / 100.0
    slip = args.slippage_percent / 100.0
    start, end = timeline[0], timeline[-1] + 1
    oos_ms = timeline[int(len(timeline) * args.oos_percent / 100.0)]

    def run(agent=None):
        return simulate_segment(data, symbols, timeline, events, start, end, PROFILES["baseline"],
                                args.market, args.trade_usdt, args.capital_usdt, args.max_positions,
                                fee, slip, rl_agent=agent)["trades"]

    base = run()
    report = {"market": args.market, "days": args.days, "symbols": symbols,
              "baseline": {"all": summarize(base), "last_30pct": summarize(base, oos_ms)}, "rl": {}}
    for ms in [int(x) for x in args.min_samples.split(",")]:
        runs_all, runs_oos = [], []
        for seed in range(args.seeds):
            agent = BanditAgent(":memory:", mode="live", min_samples=ms, rng=random.Random(seed))
            trades = run(agent)
            runs_all.append(summarize(trades))
            runs_oos.append(summarize(trades, oos_ms))
        def avg(rows):
            keys = ("trades", "net_usdt", "win_rate_pct", "sum_r_sized")
            out = {k: round(statistics.mean(r[k] for r in rows), 2) for k in keys}
            ars = [r["avg_r"] for r in rows if r["avg_r"] is not None]
            out["avg_r"] = round(statistics.mean(ars), 3) if ars else None
            out["net_min"] = min(r["net_usdt"] for r in rows)
            out["net_max"] = max(r["net_usdt"] for r in rows)
            return out
        report["rl"]["min_samples_{}".format(ms)] = {"all": avg(runs_all), "last_30pct": avg(runs_oos)}
    with open(args.report, "w") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
