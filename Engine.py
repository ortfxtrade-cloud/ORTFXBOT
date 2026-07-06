import time
import sys
import yfinance as yf
import pandas as pd
from datetime import datetime
from config import COOLDOWN_TIME, STRATEGY_PAIRS

# Direct integration bridges
from indicators import calculate_macd, calculate_rsi
from alerts import send_buy_signal, send_sell_signal, send_pre_crossing_alert, start_bot_polling

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

    # 5-Minute Timeframe: Pull values going back 3 bars to look for a FRESH crossover
    m5_m0, m5_m1, m5_m2 = macd_m5.iloc[-1], macd_m5.iloc[-2], macd_m5.iloc[-3]
    m5_s0, m5_s1, m5_s2 = signal_m5.iloc[-1], signal_m5.iloc[-2], signal_m5.iloc[-3]
    
    # 1-Minute Timeframe: Pull current state to check the latest direction alignment
    m1_m0, m1_s0 = macd_m1.iloc[-1], signal_m1.iloc[-1]

    # Import local configuration dynamic check
    from state_db import is_on_cooldown, set_cooldown
    if is_on_cooldown(pair, COOLDOWN_TIME):
        return

    # Calculate 5-minute distance gaps for your watchlist "About to cross" pre-alerts
    gap_m5_0 = abs(m5_m0 - m5_s0)
    gap_m5_1 = abs(m5_m1 - m5_s1)
    gap_m5_2 = abs(m5_m2 - m5_s2)

    # SETUP: Check for a fresh, absolute crossover on the 5-MINUTE chart
    is_m5_bullish_cross = (m5_m1 <= m5_s1) and (m5_m0 > m5_s0)
    is_m5_bearish_cross = (m5_m1 >= m5_s1) and (m5_m0 < m5_s0)

    # --- STRATEGY ROUTING EXECUTIONS ---
    
    # 🟢 TARGET BUY RANGE: 5m Cross + 1m Bullish Trend + RSI between 30 and 45
    if is_m5_bullish_cross and (m1_m0 > m1_s0) and (30.0 <= rsi <= 45.0):
        send_buy_signal(pair, price, rsi, gap_m5_0)
        set_cooldown(pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    # 🔴 TARGET SELL RANGE: 5m Cross + 1m Bearish Trend + RSI between 55 and 70
    elif is_m5_bearish_cross and (m1_m0 < m1_s0) and (55.0 <= rsi <= 70.0):
        send_sell_signal(pair, price, rsi, gap_m5_0)
        set_cooldown(pair, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    # WATCHLIST ZONE: Check if 5-minute lines are sequentially compressing toward an upcoming cross
    elif gap_m5_0 < gap_m5_1 < gap_m5_2:
        bias = "BULLISH" if m5_m0 < m5_s0 else "BEARISH"
        send_pre_crossing_alert(pair, price, bias)

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
