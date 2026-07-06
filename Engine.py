import time
import sys
import yfinance as yf
import pandas as pd
from datetime import datetime
from config import COOLDOWN_TIME

# Direct integration bridges
from indicators import calculate_macd, calculate_rsi
from alerts import send_buy_signal, send_sell_signal, start_bot_polling

# Shared state memory accessible by admin modules
IS_RUNNING = True

# --- EXPLICIT PAIR LISTS ---
JPY_PAIRS = ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CHFJPY", "CADJPY"]
MAJOR_PAIRS = ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "EURGBP", "EURCAD", "EURAUD", "EURNZD", "GBPAUD", "GBPCAD"]

# Combine both lists to create your total scanning matrix watchlist
ALL_STRATEGY_PAIRS = JPY_PAIRS + MAJOR_PAIRS

def clean_forex_ticker(pair):
    pair_str = str(pair).strip().upper()
    if not pair_str.endswith("=X"):
        return f"{pair_str}=X"
    return pair_str

def get_yfinance_data(pair, interval):
    try:
        ticker = clean_forex_ticker(pair)
        df = yf.download(ticker, period="2d", interval=interval, progress=False)
        if df is None or df.empty: 
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(col).strip().capitalize() for col in df.columns]
        return df
    except Exception as e:
        print(f"Data ingestion extraction failure for {pair}: {e}")
        return None

def analyze_ticker(pair):
    clean_pair = str(pair).strip().upper().replace("=X", "")
    
    df_m5 = get_yfinance_data(clean_pair, "5m")
    df_m1 = get_yfinance_data(clean_pair, "1m")

    if df_m5 is None or df_m1 is None or len(df_m5) < 40 or len(df_m1) < 40:
        return

    macd_m5, signal_m5 = calculate_macd(df_m5['Close'])
    macd_m1, signal_m1 = calculate_macd(df_m1['Close'])
    
    if macd_m5 is None or signal_m5 is None or macd_m1 is None or signal_m1 is None:
        return
        
    rsi_series = calculate_rsi(df_m5['Close'])
    if rsi_series.empty:
        return
    
    rsi = rsi_series.iloc[-1]
    price = df_m5['Close'].iloc[-1]

    # 5-Minute Timeframe Matrix matching your setup
    curr_m, prev_m = macd_m5.iloc[-1], macd_m5.iloc[-2]
    curr_s, prev_s = signal_m5.iloc[-1], signal_m5.iloc[-2]
    
    # 1-Minute Timeframe Matrix
    m1_m, m1_s = macd_m1.iloc[-1], signal_m1.iloc[-1]

    from state_db import is_on_cooldown, set_cooldown
    if is_on_cooldown(clean_pair, COOLDOWN_TIME):
        return

    # --- CODE RULES FROM YOUR SCREENSHOT ---
    gap = abs(curr_m - curr_s)
    
    # --- 👀 HERE ARE YOUR VALUES FOR JPY AND MAJORS ---
    if clean_pair in JPY_PAIRS:
        DYNAMIC_THRESHOLD = 0.03
        PRE_ALERT_ZONE = 0.03
    elif clean_pair in MAJOR_PAIRS:
        DYNAMIC_THRESHOLD = 0.0003
        PRE_ALERT_ZONE = 0.0003
    else:
        return 

    # SETUP: Check for completed crossovers across the 5-Minute chart blocks
    is_m5_bullish_cross = (prev_m <= prev_s) and (curr_m > curr_s)
    is_m5_bearish_cross = (prev_m >= prev_s) and (curr_m < curr_s)

    # --- STRATEGY ROUTING EXECUTIONS ---
    
    # 🟢 BUY LIMIT SIGNAL
    if is_m5_bullish_cross and (m1_m > m1_s) and (30.0 <= rsi <= 45.0) and (gap >= DYNAMIC_THRESHOLD):
        send_buy_signal(clean_pair, price, rsi, gap)
        set_cooldown(clean_pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    # 🔴 SELL LIMIT SIGNAL
    elif is_m5_bearish_cross and (m1_m < m1_s) and (55.0 <= rsi <= 70.0) and (gap >= DYNAMIC_THRESHOLD):
        send_sell_signal(clean_pair, price, rsi, gap)
        set_cooldown(clean_pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
