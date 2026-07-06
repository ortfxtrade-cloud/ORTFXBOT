
import time
import sys
import yfinance as yf
import pandas as pd
from datetime import datetime
from config import COOLDOWN_TIME, STRATEGY_PAIRS

# Direct integration bridges
from indicators import calculate_macd, calculate_rsi
from alerts import send_buy_signal, send_sell_signal, send_touching_pre_alert, start_bot_polling

# Shared state memory accessible by admin modules
IS_RUNNING = True

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
    df_m5 = get_yfinance_data(pair, "5m")
    df_m1 = get_yfinance_data(pair, "1m")

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
    if is_on_cooldown(pair, COOLDOWN_TIME):
        return

    # --- CODE RULES FROM YOUR SCREENSHOT ---
    gap = abs(curr_m - curr_s)
    prev_gap = abs(prev_m - prev_s)
    
    # --- DYNAMIC ASSET STRUCTURING MATRIX ---
    is_jpy_pair = "JPY" in pair
    is_standard_major = any(major in pair for major in ["EUR", "GBP", "AUD", "NZD", "CHF", "CAD"])

    if is_jpy_pair or price > 100:
        DYNAMIC_THRESHOLD = 0.03
        PRE_ALERT_ZONE = 0.05    
    elif is_standard_major:
        DYNAMIC_THRESHOLD = 0.0003
        PRE_ALERT_ZONE = 0.0005  

    # Check if lines have compressed into your raw pre-alert target space
    is_in_pre_alert_zone = gap <= PRE_ALERT_ZONE

    # SETUP: Check for completed crossovers across the 5-Minute chart blocks
    is_m5_bullish_cross = (prev_m <= prev_s) and (curr_m > curr_s)
    is_m5_bearish_cross = (prev_m >= prev_s) and (curr_m < curr_s)

    # --- STRATEGY ROUTING EXECUTIONS ---
    
    # 🟢 BUY LIMIT SIGNAL
    if is_m5_bullish_cross and (m1_m > m1_s) and (30.0 <= rsi <= 45.0) and (gap >= DYNAMIC_THRESHOLD):
        send_buy_signal(pair, price, rsi, gap)
        set_cooldown(pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    # 🔴 SELL LIMIT SIGNAL
    elif is_m5_bearish_cross and (m1_m < m1_s) and (55.0 <= rsi <= 70.0) and (gap >= DYNAMIC_THRESHOLD):
        send_sell_signal(pair, price, rsi, gap)
        set_cooldown(pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    # ⏳ WATCHLIST ZONE (TOUCHING PRE-ALERT)
    elif is_in_pre_alert_zone:
        bias = "BULLISH" if curr_m < curr_s else "BEARISH"
        send_touching_pre_alert(pair, price, bias)

# =========================================================================
# 🚀 MONITORING RUNTIME ENGINE LOOP
# =========================================================================
if __name__ == "__main__":
    print(f"✅ Loaded Forex watchlist matrix from config.py: {STRATEGY_PAIRS}")
    print("📈 Initializing Strategy Scanner System...")
    
    start_bot_polling()
    print("🚀 Scanner actively running. Scanning matrix watchlists...")
    
    try:
        while IS_RUNNING:
            for target_pair in STRATEGY_PAIRS:
                print(f"Scanning metrics for: {target_pair}")
                analyze_ticker(target_pair)
                time.sleep(1.5)  
                
            print("Iteration sweep complete. Pausing before next market update loop...")
            time.sleep(60)  
            
    except KeyboardInterrupt:
        print("\n🛑 Intercepted shutdown command! Stopping loop matrix...")
        IS_RUNNING = False
        sys.exit(0)
