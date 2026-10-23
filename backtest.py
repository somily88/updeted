import ccxt
import time

exchange = ccxt.binance()
symbols = ["BTC/USDT", "ETH/USDT"]

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_ema(closes, period=50):
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_macd(closes):
    if len(closes) < 35:
        return 0, 0, 0
    def get_ema_list(data, period):
        k = 2 / (period + 1)
        ema_list = [data[0]]
        for price in data[1:]:
            ema = (price * k) + (ema_list[-1] * (1 - k))
            ema_list.append(ema)
        return ema_list
    fast_list = get_ema_list(closes, 12)
    slow_list = get_ema_list(closes, 26)
    macd_line = [f - s for f, s in zip(fast_list, slow_list)]
    signal_list = get_ema_list(macd_line, 9)
    return macd_line[-1], signal_list[-1], macd_line[-1] - signal_list[-1]

def run_backtest(symbol):
    print(f"\n==================== Backtest Report for {symbol} (Target: 1%) ====================")
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=720)
    except Exception as e:
        print(f"Error fetching historical data: {e}")
        return

    in_position = False
    entry_price = 0.0
    highest_price = 0.0
    trades = []
    initial_balance = 1000.0
    balance = initial_balance

    for i in range(50, len(candles)):
        subset = candles[:i+1]
        closes = [c[4] for c in subset]
        current_price = closes[-1]
        
        rsi = calculate_rsi(closes)
        ema = calculate_ema(closes)
        macd, signal, hist = calculate_macd(closes)

        if not in_position:
            if rsi <= 70 and ema and current_price > ema and hist >= -(current_price * 0.10 / 100.0):
                in_position = True
                entry_price = current_price
                highest_price = current_price
        else:
            if current_price > highest_price:
                highest_price = current_price

            pnl_pct = ((current_price - entry_price) / entry_price) * 100
            
            if pnl_pct >= 1.0:
                balance *= (1 + 0.01)
                trades.append({"type": "WIN", "pnl": 1.0, "price": current_price})
                in_position = False
            else:
                initial_sl = entry_price * (1 - 0.005)
                trailing_sl = highest_price * (1 - 0.003)
                active_sl = max(initial_sl, trailing_sl)
                
                if current_price <= active_sl:
                    loss_pct = ((active_sl - entry_price) / entry_price) * 100
                    balance *= (1 + loss_pct / 100)
                    trades.append({"type": "LOSS", "pnl": loss_pct, "price": current_price})
                    in_position = False

    wins = [t for t in trades if t['type'] == 'WIN']
    losses = [t for t in trades if t['type'] == 'LOSS']
    win_rate = (len(wins) / len(trades) * 100) if trades else 0
    total_profit_loss = balance - initial_balance

    print(f"Total Trades: {len(trades)}")
    print(f"Winning Trades: {len(wins)} | Losing Trades: {len(losses)}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Initial Balance: ${initial_balance:.2f} -> Final Balance: ${balance:.2f}")
    print(f"Net PnL: ${total_profit_loss:+.2f}")

if __name__ == "__main__":
    for sym in symbols:
        run_backtest(sym)