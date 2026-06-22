import os
import time
import asyncio
from threading import Thread
from flask import Flask
import telebot
import yfinance as yf
import pandas as pd

# --- 1. CONFIGURATION ---
TOKEN = '8686769653:AAG7HgP3q3DcHfFG_pZpQoxJrW9neUKetWc'
CHAT_ID = '8701685996'
PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "GBPJPY=X", "EURJPY=X", "USDCAD=X", "USDCHF=X", "AUDUSD=X", "NZDUSD=X", "EURGBP=X"]
TOUCH_THRESHOLD = 0.000003

bot = telebot.TeleBot(TOKEN)
last_alerts = {}

# --- 2. RENDER FLASK KEEPALIVE ENGINE ---
app = Flask('')

@app.route('/')
def home():
    return "Core Engine Live"

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- 3. SYSTEM STATE TRACKING ---
class ControlState:
    def __init__(self):
        self.is_running = False
        self.monitor_task = None

state = ControlState()

# --- 4. CORE DATA ENGINE ---
def send_telegram(msg):
    try:
        bot.send_message(CHAT_ID, msg)
    except Exception as e:
        print(f"Telegram Error: {e}")

def get_data(pair, interval):
    try:
        df = yf.download(f"{pair}", period="2d", interval=interval, progress=False)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None

def calculate_macd(df):
    # Native EMA and MACD calculation without pandas_ta
    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return macd, signal, hist

def calculate_rsi(df, periods=14):
    close_delta = df['Close'].diff()
    up = close_delta.clip(lower=0)
    down = -1 * close_delta.clip(upper=0)
    ma_up = up.ewm(com=periods - 1, adjust=False).mean()
    ma_down = down.ewm(com=periods - 1, adjust=False).mean()
    rsi = ma_up / ma_down
    return 100 - (100 / (1 + rsi))

def check_market_conditions():
    alerts = []
    for pair in PAIRS:
        df_5m = get_data(pair, "5m")
        df_1h = get_data(pair, "1h")
        
        if df_5m is None or df_1h is None or len(df_5m) < 30 or len(df_1h) < 30:
            continue
            
        # 1-Hour Trend Direction
        macd_h, signal_h, _ = calculate_macd(df_1h)
        rsi_h = calculate_rsi(df_1h)
        
        last_macd_h = macd_h.iloc[-1].item()
        last_sig_h = signal_h.iloc[-1].item()
        last_rsi_h = rsi_h.iloc[-1].item()
        
        higher_tf_trend = "UP" if (last_macd_h > last_sig_h and last_rsi_h > 50) else "DOWN" if (last_macd_h < last_sig_h and last_rsi_h < 50) else "NEUTRAL"
        
        # 5-Minute Entry Execution
        macd_m, signal_m, _ = calculate_macd(df_5m)
        rsi_m = calculate_rsi(df_5m)
        
        prev_macd_m = macd_m.iloc[-2].item()
        prev_sig_m = signal_m.iloc[-2].item()
        curr_macd_m = macd_m.iloc[-1].item()
        curr_sig_m = signal_m.iloc[-1].item()
        curr_rsi_m = rsi_m.iloc[-1].item()
        
        clean_name = pair.replace("=X", "")
        current_time = time.time()
        
        if higher_tf_trend == "UP" and prev_macd_m <= prev_sig_m and curr_macd_m > curr_sig_m and curr_rsi_m < 70:
            if current_time - last_alerts.get(f"{clean_name}_CALL", 0) > 1800:
                alerts.append(f"🟢 CORE ALGORITHM: CALL SETUP\nAsset: {clean_name}\nTimeframe: 5M Entry (1H Alignment)\nExecution: Market Order")
                last_alerts[f"{clean_name}_CALL"] = current_time
                
        elif higher_tf_trend == "DOWN" and prev_macd_m >= prev_sig_m and curr_macd_m < curr_sig_m and curr_rsi_m > 30:
            if current_time - last_alerts.get(f"{clean_name}_PUT", 0) > 1800:
                alerts.append(f"🔴 CORE ALGORITHM: PUT SETUP\nAsset: {clean_name}\nTimeframe: 5M Entry (1H Alignment)\nExecution: Market Order")
                last_alerts[f"{clean_name}_PUT"] = current_time
                
    return alerts

async def analytical_monitor_loop():
    print("[SYSTEM] Core processing routine initialized.")
    while state.is_running:
        try:
            signals = check_market_conditions()
            for signal in signals:
                send_telegram(signal)
        except Exception as e:
            print(f"Execution Error: {e}")
        await asyncio.sleep(60)

def start_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(analytical_monitor_loop())

# --- 5. TELEGRAM INTERFACE ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    if state.is_running:
        bot.reply_to(message, "System Architecture Status: Active. Monitoring live data matrices.")
        return
        
    state.is_running = True
    send_telegram("🚀 SYSTEM ARCHITECTURE ONLINE\nInitialization: Successful\nMode: Native Mathematical Calculation\nStatus: Scanning Market Feeds...")
    
    loop = asyncio.new_event_loop()
    t = Thread(target=start_async_loop, args=(loop,), daemon=True)
    t.start()

@bot.message_handler(commands=['stop'])
def handle_stop(message):
    if not state.is_running:
        bot.reply_to(message, "System Architecture Status: Terminated/Idle.")
        return
        
    state.is_running = False
    bot.reply_to(message, "System parsing routines suspended. Monitoring offline.")

if __name__ == '__main__':
    Thread(target=run_http_server, daemon=True).start()
    print("[SYSTEM] HTTP webserver verification layer active.")
    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            print(f"Network error encountered: {e}")
            time.sleep(15)
