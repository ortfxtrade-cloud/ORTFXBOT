import os
import time
import threading
from flask import Flask
import telebot
import requests
import yfinance as yf
import pandas as pd

# --- WEB SERVER FOR RENDER HEALTH CHECKS ---
app = Flask('')
IS_RUNNING = True 

@app.route('/')
def home():
    status = "RUNNING" if IS_RUNNING else "STOPPED"
    return f"System status: {status}. Monitoring market strategies 24/5."

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- CONFIGURATION FROM ENVIRONMENT ---
TOKEN ='8686769653:AAH_E703LLE-rpV6mpOZh7ifd9_UpH85pb0'
CHAT_ID =  '8701685996'
FINNHUB_API_KEY = 'd8tq131r01qhcnk5gh9gd8tq131r01qhcnk5gha0'

# Initialize Unified Telebot Engine
sync_bot = telebot.TeleBot(TOKEN)

# Full 24 Pairs for Strategy Scanning
STRATEGY_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCAD", "EURAUD", "EURNZD", "EURCHF",
    "GBPJPY", "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY", "AUDCAD", "AUDNZD"
]
TOUCH_THRESHOLD = 0.000003
last_alerts = {}

# --- ON-DEMAND VELOCITY CHECKER ---
def get_pair_velocity_status(pair):
    if not FINNHUB_API_KEY:
        return "UNKNOWN (API Key Missing)"
        
    finnhub_symbol = f"OANDA:{pair[:3]}_{pair[3:]}"
    url = f"https://finnhub.io/api/v1/quote?symbol={finnhub_symbol}&token={FINNHUB_API_KEY}"
    
    try:
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            current_price = data.get('c', 0)
            prev_close = data.get('pc', 0)
            
            if current_price == 0:
                return "🚨 UNSAFE (No Liquid Vol)"
                
            movement = abs(current_price - prev_close)
            if movement > 0:
                return "🟢 SAFE (Active Liquidity)"
            else:
                return "⚠️ LOW VELOCITY (Stale Session)"
    except Exception:
        pass
    return "⚠️ UNCONFIRMED (API Timeout)"

# --- NATIVE MATH TECHNICAL INDICATORS ---
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

# --- MACD & RSI STRATEGY ANALYSIS ---
def send_telegram_signal(msg):
    try:
        sync_bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
    except Exception as e:
        print(f"Signal Routing Error: {e}")

def get_yfinance_data(pair, interval):
    try:
        df = yf.download(f"{pair}=X", period="2d", interval=interval, progress=False)
        if df is None or df.empty: 
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None

def analyze_ticker(pair):
    df_m5 = get_yfinance_data(pair, "5m")
    df_m1 = get_yfinance_data(pair, "1m")

    # Defensively skip execution if either timeframe dataset is missing or incomplete
    if df_m5 is None or df_m1 is None or len(df_m5) < 40 or len(df_m1) < 40:
        return

    # Native calculation routines bypassing pandas-ta completely
    macd_m5, signal_m5 = calculate_macd(df_m5['Close'])
    macd_m1, signal_m1 = calculate_macd(df_m1['Close'])
    
    # Verify indicator calculations yielded valid numerical records
    if macd_m5 is None or signal_m5 is None or macd_m1 is None or signal_m1 is None:
        return
        
    rsi_series = calculate_rsi(df_m5['Close'])
    if rsi_series.empty:
        return
    
    rsi = rsi_series.iloc[-1]
    price = df_m5['Close'].iloc[-1]

    curr_m, prev_m = macd_m5.iloc[-1], macd_m5.iloc[-2]
    curr_s, prev_s = signal_m5.iloc[-1], signal_m5.iloc[-2]

    gap = abs(curr_m - curr_s)
    m1_m, m1_s = macd_m1.iloc[-1], signal_m1.iloc[-1]

    alert_key = f"{pair}_alert"
    if time.time() - last_alerts.get(alert_key, 0) < 300:
        return

    if 30 <= rsi <= 45:
        if gap < TOUCH_THRESHOLD and curr_m < curr_s:
            velocity = get_pair_velocity_status(pair)
            send_telegram_signal(f"🔍 *[GET READY]* {pair}\nLines touching.\nPrice: `{price:.5f}`\nVelocity: *{velocity}*")
            last_alerts[alert_key] = time.time()
        elif prev_m < prev_s and curr_m > curr_s and m1_m > m1_s:
            velocity = get_pair_velocity_status(pair)
            send_telegram_signal(f"🔥⬆️✅ *[BUY]* {pair}\nBullish cross!\nPrice: `{price:.5f}`\nVelocity: *{velocity}*")
            last_alerts[alert_key] = time.time()

    elif 55 <= rsi <= 70:
        if gap < TOUCH_THRESHOLD and curr_m > curr_s:
            velocity = get_pair_velocity_status(pair)
            send_telegram_signal(f"📉 *[GET READY]* {pair}\nLines touching.\nPrice: `{price:.5f}`\nVelocity: *{velocity}*")
            last_alerts[alert_key] = time.time()
        elif prev_m > prev_s and curr_m < curr_s and m1_m < m1_s:
            velocity = get_pair_velocity_status(pair)
            send_telegram_signal(f"📉⬇️✅ *[SELL]* {pair}\nBearish cross!\nPrice: `{price:.5f}`\nVelocity: *{velocity}*")
            last_alerts[alert_key] = time.time()

def strategy_loop():
    global IS_RUNNING
    print("Strategy scanning mechanism initialized...")
    while True:
        current_day = time.gmtime().tm_wday
        if current_day < 5: 
            if IS_RUNNING:
                for pair in STRATEGY_PAIRS:
                    if not IS_RUNNING: 
                        break
                    try:
                        analyze_ticker(pair)
                    except Exception as e:
                        print(f"Error checking {pair}: {e}")
                    time.sleep(2)
                time.sleep(300)
            else:
                time.sleep(5)
        else:
            time.sleep(3600)

# --- TELEGRAM ADMIN INTERFACE ---
@sync_bot.message_handler(commands=['start_bot'])
def start_bot_cmd(message):
    global IS_RUNNING
    if str(message.chat.id) != str(CHAT_ID): return
    IS_RUNNING = True
    sync_bot.reply_to(message, "🚀 Technical strategy matrix active. Scanning markets...")

@sync_bot.message_handler(commands=['stop_bot'])
def stop_bot_cmd(message):
    global IS_RUNNING
    if str(message.chat.id) != str(CHAT_ID): return
    IS_RUNNING = False
    sync_bot.reply_to(message, "🛑 Technical scans paused.")

@sync_bot.message_handler(commands=['status'])
def status_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    state = "🟢 ACTIVE" if IS_RUNNING else "🔴 PAUSED"
    sync_bot.reply_to(message, f"Strategy Scan Status: {state}")

# --- INIT AND RUN ---
if __name__ == "__main__":
    t_web = threading.Thread(target=run_web_server)
    t_web.daemon = True
    t_web.start()

    t_strategy = threading.Thread(target=strategy_loop)
    t_strategy.daemon = True
    t_strategy.start()

    print("Background components online. Starting command sync listener...")
    sync_bot.infinity_polling()
