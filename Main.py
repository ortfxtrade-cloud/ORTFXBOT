import os
import time
import asyncio
from threading import Thread
from flask import Flask
import telebot
import yfinance as yf
import pandas as pd
import pandas_ta as ta

# --- 1. CONFIGURATION ---
TOKEN = '8686769653:AAGuWQxuAknw_zZXP6b9_NQIvINzgoxLcPM'
CHAT_ID = '8701685996'

# Complete IQ Option Comprehensive Asset List
PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "EURCAD", "EURAUD", "EURNZD",
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD", "AUDJPY", "CADJPY", "CHFJPY",
    "NZDJPY", "AUDCAD", "AUDCHF", "AUDNZD", "CADCHF", "USDZAR", "USDTRY"
]
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
        df = yf.download(f"{pair}=X", period="2d", interval=interval, progress=False)
        if df.empty: 
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None

def check_market_conditions():
    alerts = []
    for pair in PAIRS:
        # Pacing delay between assets to prevent rate limits or container throttling
        time.sleep(1.5)
        
        df_m5 = get_data(pair, "5m")
        df_m1 = get_data(pair, "1m")
        
        if df_m5 is None or df_m1 is None or len(df_m5) < 40 or len(df_m1) < 40:
            continue

        # Calculate Indicators via pandas_ta
        macd_m5 = df_m5.ta.macd(fast=12, slow=26, signal=9)
        macd_m1 = df_m1.ta.macd(fast=12, slow=26, signal=9)
        
        if macd_m5 is None or macd_m1 is None:
            continue
            
        rsi = df_m5.ta.rsi(length=14).iloc[-1]
        price = df_m5['Close'].iloc[-1]

        # Current/Prev MACD values for M5
        curr_m, prev_m = macd_m5['MACD_12_26_9'].iloc[-1], macd_m5['MACD_12_26_9'].iloc[-2]
        curr_s, prev_s = macd_m5['MACDs_12_26_9'].iloc[-1], macd_m5['MACDs_12_26_9'].iloc[-2]
        
        gap = abs(curr_m - curr_s)
        m1_m, m1_s = macd_m1['MACD_12_26_9'].iloc[-1], macd_m1['MACDs_12_26_9'].iloc[-1]
        
        alert_key = f"{pair}_alert"
        current_time = time.time()
        
        # Cooldown check (5 minutes)
        if current_time - last_alerts.get(alert_key, 0) < 300:
            continue

        # --- BUY LOGIC ---
        if 30 <= rsi <= 45:
            if gap < TOUCH_THRESHOLD and curr_m < curr_s:
                alerts.append(f"🔍 [TOUCH] {pair}\nLines are effectively touching. Price: {price:.5f}")
                last_alerts[alert_key] = current_time
            elif prev_m < prev_s and curr_m > curr_s and m1_m > m1_s:
                alerts.append(f"🔥 [CROSS] {pair}\nBullish cross confirmed! Price: {price:.5f}")
                last_alerts[alert_key] = current_time

        # --- SELL LOGIC ---
        elif 55 <= rsi <= 70:
            if gap < TOUCH_THRESHOLD and curr_m > curr_s:
                alerts.append(f"📉 [TOUCH] {pair}\nLines are effectively touching. Price: {price:.5f}")
                last_alerts[alert_key] = current_time
            elif prev_m > prev_s and curr_m < curr_s and m1_m < m1_s:
                alerts.append(f"💣 [CROSS] {pair}\nBearish cross confirmed! Price: {price:.5f}")
                last_alerts[alert_key] = current_time
                
    return alerts

async def analytical_monitor_loop():
    print("[SYSTEM] Core logic processing loop running with full asset matrix.")
    while state.is_running:
        try:
            signals = check_market_conditions()
            for signal in signals:
                send_telegram(signal)
        except Exception as e:
            print(f"Execution Error inside scanner: {e}")
        # Shorter sleep between sweep cycles since internal iteration takes longer
        await asyncio.sleep(10)

def start_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(analytical_monitor_loop())

# --- 5. TELEGRAM INTERFACE ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    if state.is_running:
        bot.reply_to(message, "System Status: Active. Monitoring matrix arrays.")
        return
        
    state.is_running = True
    send_telegram("🚀 SYSTEM ARCHITECTURE ONLINE\nStrategy: Comprehensive M5 Touch/Cross Engine\nStatus: Scanning Feeds...")
    
    loop = asyncio.new_event_loop()
    t = Thread(target=start_async_loop, args=(loop,), daemon=True)
    t.start()

@bot.message_handler(commands=['stop'])
def handle_stop(message):
    if not state.is_running:
        bot.reply_to(message, "System Status: Already offline.")
        return
        
    state.is_running = False
    bot.reply_to(message, "Monitoring suspended.")

if __name__ == '__main__':
    Thread(target=run_http_server, daemon=True).start()
    print("[SYSTEM] Keepalive HTTP layer bound.")
    while True:
        try:
            bot.polling(none_stop=True, timeout=60)
        except Exception as e:
            print(f"Network polling exception: {e}")
            time.sleep(15)

