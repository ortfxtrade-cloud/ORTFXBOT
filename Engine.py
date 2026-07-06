import time
import yfinance as yf
import pandas as pd
from config import COOLDOWN_TIME
from indicators import calculate_macd, calculate_rsi, calculate_yfinance_velocity
from state_db import is_on_cooldown, set_cooldown
from alerts import send_telegram_signal

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

    # Calculate absolute differences for current and previous bars
    gap = abs(curr_m - curr_s)
    prev_gap = abs(prev_m - prev_s)
    
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
