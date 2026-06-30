import pandas as pd

def calculate_yfinance_velocity(df_m1):
    try:
        if df_m1 is None or len(df_m1) < 5:
            return "⚠️ UNCONFIRMED (Data Missing)"
        
        recent_closes = df_m1['Close'].tail(5)
        max_price = recent_closes.max()
        min_price = recent_closes.min()
        volatility = max_price - min_price
        
        if volatility > 0:
            return "🟢 SAFE (Active Liquidity)"
        else:
            return "⚠️ LOW VELOCITY (Stale Session)"
    except Exception:
        return "⚠️ UNCONFIRMED (Calculation Error)"

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line
