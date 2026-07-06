import time
import sys
import yfinance as yf
import pandas as pd
from datetime import datetime
from config import COOLDOWN_TIME, WATCHLIST

# Direct integration bridges
from indicators import calculate_macd, calculate_rsi
from state_db import is_on_cooldown, set_cooldown
from alerts import format_and_send_trade_signal, start_bot_polling

# Shared state memory accessible by admin modules
IS_RUNNING = True

def get_yfinance_data(pair, interval):
    try:
        df = yf.download(f"{pair}=X", period="2d", interval=interval, progress=False)
        if df is None or df.empty: 
            return None
            
        # Hardening Layer: Detect and completely flatten Multi-Index data column structures
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        # Enforce strict data type and capitalization schema uniformity
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

    # M5 MACD values for setup detection
    curr_m, prev_m = macd_m5.iloc[-1], macd_m5.iloc[-2]
    curr_s, prev_s = signal_m5.iloc[-1], signal_m5.iloc[-2]

    # Calculate absolute differences for current bars
    gap = abs(curr_m - curr_s)
    
    # M1 MACD confirmation values
    m1_m, m1_s = macd_m1.iloc[-1], signal_m1.iloc[-1]

    # SQLite Persistent Check replacing volatile dictionary rate limits
    if is_on_cooldown(pair, COOLDOWN_TIME):
        return

    # --- DYNAMIC ASSET STRUCTURING MATRIX ---
    is_jpy_pair = "JPY" in pair
    is_exotic_pair = any(exotic in pair for exotic in ["ZAR", "TRY", "INR", "MXN", "SGD", "HKD", "CNH"])
    is_standard_major = any(major in pair for major in ["USD", "EUR", "AUD", "GBP", "CAD", "CHF", "NZD"])

    if is_jpy_pair or is_exotic_pair or price > 10:
        DYNAMIC_THRESHOLD = 0.03
        PRE_ALERT_ZONE = 0.08  
    elif is_standard_major:
        DYNAMIC_THRESHOLD = 0.0003
        PRE_ALERT_ZONE = 0.0008  
    else:
        DYNAMIC_THRESHOLD = 0.0003
        PRE_ALERT_ZONE = 0.0008

    # --- STRATEGY EXECUTION TRIGGER LOGIC ---
    if gap <= DYNAMIC_THRESHOLD:
        # M1 confirmation: Trend direction matches momentum expansion
        if m1_m > m1_s and rsi < 70:
            direction = "BUY"
        elif m1_m < m1_s and rsi > 30:
            direction = "SELL"
        else:
            return  # No structural confirmation on M1 timeframe
        
        # Dispatch the signal via alerts module
        format_and_send_trade_signal(pair, price, rsi, gap, direction)
        
        # Instantly lock asset via SQLite tracking layer to enforce cooldown
        set_cooldown(pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

# =========================================================================
# 🚀 CORE ENGINE MONITORING LOOP RUNTIME
# =========================================================================
if __name__ == "__main__":
    print(f"✅ Loaded matrix watchlist from config.py: {WATCHLIST}")
    print("📈 Initializing Strategy Scanner System...")
    
    # Wake up incoming commands listeners
    start_bot_polling()
    
    print("🚀 Scanner actively running. Scanning matrix watchlists...")
    
    try:
        while IS_RUNNING:
            for target_pair in WATCHLIST:
                print(f"Scanning metrics for: {target_pair}")
                analyze_ticker(target_pair)
                time.sleep(1.5)  # Safe buffer time delay between API inquiries
                
            print("Iteration sweep complete. Pausing before next market update loop...")
            time.sleep(60)  # Wait 1 minute before scraping and evaluating data metrics again
            
    except KeyboardInterrupt:
        # Catch termination command (Ctrl+C) and flip state to False safely
        print("\n🛑 Intercepted shutdown command! Stopping loop matrix...")
        IS_RUNNING = False
        print("⚠️ Engine stopped. Telegram listener remains open until terminal window exits.")
        # Keeps the terminal alive just enough so you can verify the status message turned red
        sys.exit(0)
