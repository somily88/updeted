"""Quick public-data backtest for trend_pullback and donchian_breakout.

Uses public Binance spot klines from data-api.binance.vision (the market-data
host; api.binance.com returns HTTP 451 from some regions). No API key.
Fees and slippage are applied on
every fill. Parameters are the live defaults; this script does not search them.

Stops are checked on the bar after the signal bar, so the signal close is not
used as a same-bar exit. The unfinished candle is dropped before the run.
"""
import datetime
import json
import time
import urllib.parse
import urllib.request

import core_strategies as cs

FEE_RATE = 0.001       # 0.10% taker per side
SLIPPAGE = 0.0005      # 5 bps per side, stand-in for spread plus slippage
EQUITY0 = 10_000.0
SYMBOLS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT")


def fetch_klines(symbol, interval, start_ms, end_ms):
    market = symbol.replace("/", "")
    step = cs.TF_4H_MS if interval == "4h" else cs.TF_1D_MS
    rows = []
    seen = set()
    cursor = int(start_ms)
    while cursor < end_ms:
        query = urllib.parse.urlencode({
            "symbol": market,
            "interval": interval,
            "startTime": cursor,
            "endTime": int(end_ms),
            "limit": 1000,
        })
        request = urllib.request.Request(
            "https://data-api.binance.vision/api/v3/klines?" + query,
            headers={"User-Agent": "sobhi-trader-backtest"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.loads(response.read().decode())
        if not batch:
            break
        for item in batch:
            stamp = int(item[0])
            if stamp in seen or stamp >= end_ms:
                continue
            seen.add(stamp)
            rows.append([stamp, float(item[1]), float(item[2]), float(item[3]),
                         float(item[4]), float(item[5])])
        last = int(batch[-1][0])
        nxt = last + step
        if nxt <= cursor or len(batch) < 2:
            break
        cursor = nxt
        if len(batch) < 1000:
            break
        time.sleep(0.12)
    rows.sort(key=lambda row: row[0])
    now_ms = int(time.time() * 1000)
    return cs.closed_candles(rows + [[now_ms, 0, 0, 0, 0, 0]], step, now_ms)


def _day(ts):
    return datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")


def _signal(strategy, window, btc_daily, ts):
    # Same 500-bar cap the live scanner requests from the exchange.
    window = window[-500:]
    if strategy == "trend_pullback":
        return cs.trend_pullback_setup(window, spread_pct=0.0)
    closed_daily = [row for row in btc_daily if row[0] + cs.TF_1D_MS <= ts + cs.TF_4H_MS][-500:]
    return cs.donchian_breakout_setup(window, closed_daily, spread_pct=0.0)


def _exit_fill(strategy, pos, candles):
    bar = candles[-1]
    opened, high, low, close = (float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]))
    stop = float(pos["stop"])
    if low <= stop:
        raw = opened if opened < stop else stop
        return raw * (1.0 - SLIPPAGE), "stop"
    method = pos["method"]
    if method == "tp_2r" and high >= pos["target"]:
        return float(pos["target"]) * (1.0 - SLIPPAGE), "tp"
    if method == "channel" and cs.donchian_channel_exit(candles):
        return close * (1.0 - SLIPPAGE), "channel"
    if method in ("trail", "atr_trail"):
        value = cs.atr(candles)
        mult = cs.tp_trail_atr() if method == "trail" else cs.donchian_trail_atr()
        if value:
            pos["stop"] = max(stop, high - mult * value)
    return None, None


def _close(pos, fill, reason, cash, trades):
    proceeds = pos["qty"] * fill
    fee = proceeds * FEE_RATE
    cash += proceeds - fee
    net = (proceeds - fee) - pos["cost"]
    trades.append({
        "symbol": pos["symbol"],
        "net": net,
        "r": net / pos["risk_usdt"] if pos["risk_usdt"] else 0.0,
        "reason": reason,
    })
    return cash


def run_strategy(strategy, series, btc_daily, equity0=EQUITY0):
    symbols = [symbol for symbol in SYMBOLS if symbol in series]
    index = {symbol: {row[0]: i for i, row in enumerate(candles)}
             for symbol, candles in series.items()}
    timeline = sorted(index[symbols[0]]) if symbols else []
    cash = float(equity0)
    positions = {}
    trades = []
    curve = []
    day_base = {}
    cap = cs.max_open_positions()
    risk_cap = 4.0
    method = cs.exit_method_for(strategy)

    for ts in timeline:
        marks = {}
        equity = cash
        for symbol, pos in positions.items():
            i = index[symbol].get(ts)
            price = series[symbol][i][4] if i is not None else pos["last"]
            pos["last"] = price
            marks[symbol] = price
            equity += pos["qty"] * price
        day = _day(ts)
        if day not in day_base:
            day_base[day] = equity

        for symbol in list(positions):
            i = index[symbol].get(ts)
            pos = positions[symbol]
            if i is None or i <= pos["entry_i"]:
                continue
            window = series[symbol][max(0, i + 1 - 80):i + 1]
            fill, reason = _exit_fill(strategy, pos, window)
            if fill is None:
                continue
            cash = _close(pos, fill, reason, cash, trades)
            del positions[symbol]

        equity = cash + sum(positions[s]["qty"] * positions[s]["last"] for s in positions)
        entries_blocked = cs.daily_loss_blocks(day_base[day], equity, cs.daily_loss_limit_pct(5.0))
        if not entries_blocked:
            for symbol in symbols:
                if symbol in positions or len(positions) >= cap:
                    continue
                i = index[symbol].get(ts)
                if i is None or i + 1 < cs.MIN_4H_BARS:
                    continue
                window = series[symbol][max(0, i + 1 - 500):i + 1]
                signal = _signal(strategy, window, btc_daily, ts)
                if not signal:
                    continue
                fill = float(signal["ref_close"]) * (1.0 + SLIPPAGE)
                stop_distance = cs.STOP_ATR * float(signal["atr"])
                sized = cs.size_position(equity, fill, stop_distance, strategy)
                if not sized["allowed"]:
                    continue
                open_risk = sum(p["risk_usdt"] for p in positions.values())
                if cs.portfolio_risk_blocks(open_risk, sized["risk_usdt"], equity, risk_cap):
                    continue
                notional = sized["qty"] * fill
                fee = notional * FEE_RATE
                if notional + fee > cash:
                    continue
                cash -= notional + fee
                stop = fill - stop_distance
                target = None
                if method == "tp_2r":
                    target = fill + cs.tp_r_multiple() * stop_distance
                positions[symbol] = {
                    "symbol": symbol, "qty": sized["qty"], "entry": fill, "stop": stop,
                    "target": target, "risk_usdt": sized["risk_usdt"], "cost": notional + fee,
                    "entry_i": i, "last": fill, "method": method,
                }
                equity = cash + sum(p["qty"] * p["last"] for p in positions.values())
        curve.append(equity)

    last_ts = timeline[-1] if timeline else None
    for symbol, pos in list(positions.items()):
        i = index[symbol].get(last_ts)
        fill = float(series[symbol][i][4]) * (1.0 - SLIPPAGE)
        cash = _close(pos, fill, "open_mark", cash, trades)
    final_equity = cash
    peak = equity0
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    closed = [trade for trade in trades if trade["reason"] != "open_mark"]
    marked = [trade for trade in trades if trade["reason"] == "open_mark"]
    wins = sum(1 for trade in closed if trade["net"] > 0)
    count = len(closed)
    return {
        "strategy": strategy,
        "trades": count,
        "win_rate_pct": round((wins / count * 100.0) if count else 0.0, 2),
        "total_r": round(sum(trade["r"] for trade in closed), 3),
        "realized_pnl_usdt": round(sum(trade["net"] for trade in closed), 2),
        "marked_open_pnl_usdt": round(sum(trade["net"] for trade in marked), 2),
        "total_pnl_usdt": round(final_equity - equity0, 2),
        "final_equity": round(final_equity, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "open_at_end": len(marked),
        "fees_model": "0.10% taker per side + 5 bps slippage per side",
        "window": "about 420 days of 4h bars, BTC daily EMA200 from ~900 days",
    }


def main():
    end_ms = int(time.time() * 1000)
    start_4h = end_ms - 420 * 24 * 60 * 60 * 1000
    start_1d = end_ms - 900 * 24 * 60 * 60 * 1000
    series = {}
    for symbol in SYMBOLS:
        print("fetch 4h {}".format(symbol), flush=True)
        series[symbol] = fetch_klines(symbol, "4h", start_4h, end_ms)
        print("  {} bars".format(len(series[symbol])), flush=True)
    print("fetch 1d BTC/USDT", flush=True)
    btc_daily = fetch_klines("BTC/USDT", "1d", start_1d, end_ms)
    print("  {} daily bars".format(len(btc_daily)), flush=True)
    report = []
    for strategy in cs.CORE_STRATEGY_NAMES:
        print("run {}".format(strategy), flush=True)
        report.append(run_strategy(strategy, series, btc_daily))
    print(json.dumps({"symbols": list(SYMBOLS), "results": report}, indent=2))


if __name__ == "__main__":
    main()
