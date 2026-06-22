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
TOKEN = '8686769653:AAHyRTeszG4ss1XotEhQ41hu01dH3vsn9Go'  
CHAT_ID = '8701685996'
PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY", "EURJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD", "EURGBP"]
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
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None

def analyze_ticker(pair):
    df_m5 = get_data(pair, "5m")
    df_m1 = get_data(pair, "1m")

    if df_m5 is None or df_m1 is None or len(df_m5) < 40:
        return

    macd_m5 = df_m5.ta.macd(fast=12, slow=26, signal=9)
    macd_m1 = df_m1.ta.macd(fast=12, slow=26, signal=9)
    rsi = df_m5.ta.rsi(length=14).iloc[-1]
    price = df_m5['Close'].iloc[-1]

    curr_m, prev_m = macd_m5['MACD_12_26_9'].iloc[-1], macd_m5['MACD_12_26_9'].iloc[-2]
    curr_s, prev_s = macd_m5['MACDs_12_26_9'].iloc[-1], macd_m5['MACDs_12_26_9'].iloc[-2]

    gap = abs(curr_m - curr_s)
    m1_m, m1_s = macd_m1['MACD_12_26_9'].iloc[-1], macd_m1['MACDs_12_26_9'].iloc[-1]

    alert_key = f"{pair}_alert"

    if time.time() - last_alerts.get(alert_key, 0) < 300:
        return

    # --- BUY LOGIC ---
    if 30 <= rsi <= 45:
        if gap < TOUCH_THRESHOLD and curr_m < curr_s:
            send_telegram(f"🔍 [GET READY😁] {pair}\nLines are effectively touching. Price: {price:.5f}")
            last_alerts[alert_key] = time.time()
        elif prev_m < prev_s and curr_m > curr_s and m1_m > m1_s:
            send_telegram(f"🔥⬆️✅ [BUY] {pair}\nBullish cross confirmed! Price: {price:.5f}")
            last_alerts[alert_key] = time.time()

    # --- SELL LOGIC ---
    elif 55 <= rsi <= 70:
        if gap < TOUCH_THRESHOLD and curr_m > curr_s:
            send_telegram(f"📉 [GET READY😁] {pair}\nLines are effectively touching. Price: {price:.5f}")
            last_alerts[alert_key] = time.time()
        elif prev_m > prev_s and curr_m < curr_s and m1_m < m1_s:
            send_telegram(f"📉⬇️✅ [SELL] {pair}\nBearish cross confirmed! Price: {price:.5f}")
            last_alerts[alert_key] = time.time()

# Asynchronous background loop to allow simultaneous processing of commands
async def continuous_market_scanner():
    print("Market Engine Loop initialized...")
    while state.is_running:
        for pair in PAIRS:
            if not state.is_running:  
                break
            try:
                analyze_ticker(pair)
            except Exception as e:
                print(f"Error analyzing {pair}: {e}")
            await asyncio.sleep(2)  
        
        for _ in range(30):
            if not state.is_running:
                break
            await asyncio.sleep(1)

# --- 5. TELEGRAM COMMAND HANDLERS ---
@bot.message_handler(commands=['start'])
def handle_start(message):
    if state.is_running:
        bot.reply_to(message, "⚠️ System Core is already scanning market streams.")
        return
    
    state.is_running = True
    bot.reply_to(message, "🚀 Signal Engine Started. Market scans active.")
    
    loop = asyncio.get_event_loop()
    state.monitor_task = loop.create_task(continuous_market_scanner())

@bot.message_handler(commands=['stop'])
def handle_stop(message):
    if not state.is_running:
        bot.reply_to(message, "⚠️ System Core is already completely idle.")
        return

    bot.reply_to(message, "🛑 Disengaging analysis loops safely...")
    state.is_running = False
    
    if state.monitor_task:
        state.monitor_task.cancel()
        state.monitor_task = None

# --- 6. EXECUTION PIPELINE ---
def main():
    server_thread = Thread(target=run_http_server)
    server_thread.daemon = True
    server_thread.start()

    print("Bot framework up. Polling listeners active...")
    bot.infinity_polling()

if __name__ == "__main__":
    main()
