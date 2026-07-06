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

def get_yfinance_data(pair, interval):
    try:
        df = yf.download(f"{pair}=X", period="2d", interval=interval, progress=False)
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

    # 5-Minute Timeframe: Pull current and previous values
    m5_m0, m5_m1 = macd_m5.iloc[-1], macd_m5.iloc[-2]
    m5_s0, m5_s1 = signal_m5.iloc[-1], signal_m5.iloc[-2]
    
    # 1-Minute Timeframe: Pull current state
    m1_m0, m1_s0 = macd_m1.iloc[-1], signal_m1.iloc[-1]

    from state_db import is_on_cooldown, set_cooldown
    if is_on_cooldown(pair, COOLDOWN_TIME):
        return

    # Calculate absolute current gap on 5-minute chart
    gap_m5 = abs(m5_m0 - m5_s0)
    
    # =========================================================================
    # 🗂️ DYNAMIC ASSET STRUCTURING MATRIX (FROM YOUR SCREENSHOT)
    # =========================================================================
    is_jpy_pair = "JPY" in pair
    # Note: Define your lists or string matches for exotics/majors if needed
    is_exotic_pair = any(exotic in pair for exotic in ["TRY", "ZAR", "MXN"]) 
    is_standard_major = any(major in pair for major in ["EUR", "GBP", "AUD", "NZD", "CHF", "CAD"])

    # Default fallback values to prevent errors
    DYNAMIC_THRESHOLD = 0.0003
    PRE_ALERT_ZONE = 0.0008

    if is_jpy_pair or is_exotic_pair or price > 100:
        DYNAMIC_THRESHOLD = 0.03
        PRE_ALERT_ZONE = 0.08
    elif is_standard_major:
        DYNAMIC_THRESHOLD = 0.0003
        PRE_ALERT_ZONE = 0.0008 # Adjusted from text cursor '0.0' to represent an active zone

    # Check if the raw gap falls inside your designated raw pre-alert space
    is_in_pre_alert_zone = gap_m5 <= PRE_ALERT_ZONE

    # SETUP: Check for a completed crossover on the 5-MINUTE chart
    is_m5_bullish_cross = (m5_m1 <= m5_s1) and (m5_m0 > m5_s0)
    is_m5_bearish_cross = (m5_m1 >= m5_s1) and (m5_m0 < m5_s0)

    # --- STRATEGY ROUTING EXECUTIONS ---
    
    # 🟢 TARGET BUY SIGNAL: Fresh 5m Cross + 1m Bullish Trend + RSI between 30 and 45
    if is_m5_bullish_cross and (m1_m0 > m1_s0) and (30.0 <= rsi <= 45.0):
        send_buy_signal(pair, price, rsi, gap_m5)
        set_cooldown(pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    # 🔴 TARGET SELL SIGNAL: Fresh 5m Cross + 1m Bearish Trend + RSI between 55 and 70
    elif is_m5_bearish_cross and (m1_m0 < m1_s0) and (55.0 <= rsi <= 70.0):
        send_sell_signal(pair, price, rsi, gap_m5)
        set_cooldown(pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    # ⏳ WATCHLIST ZONE: If lines haven't crossed yet but are within your PRE_ALERT_ZONE
    elif is_in_pre_alert_zone:
        bias = "BULLISH" if m5_m0 < m5_s0 else "BEARISH"
        send_touching_pre_alert(pair, price, bias)

# =========================================================================
# 🚀 CORE ENGINE MONITORING LOOP RUNTIME
# =========================================================================
if __name__ == "__main__":
    print(f"✅ Loaded watchlist matrix from config.py: {WATCHLIST}")
    print("📈 Initializing Strategy Scanner System...")
    
    start_bot_polling()
    print("🚀 Scanner actively running. Scanning matrix watchlists...")
    
    try:
        while IS_RUNNING:
            for target_pair in WATCHLIST:
                print(f"Scanning metrics for: {target_pair}")
                analyze_ticker(target_pair)
                time.sleep(1.5)  
                
            print("Iteration sweep complete. Pausing before next market update loop...")
            time.sleep(60)  
            
    except KeyboardInterrupt:
        print("\n🛑 Intercepted shutdown command! Stopping loop matrix...")
        IS_RUNNING = False
        sys.exit(0)
