import os
import json
import time
import asyncio
import threading
from collections import defaultdict
from flask import Flask
import websockets
import telebot
import yfinance as yf
import pandas as pd
import pandas_ta as ta
from telegram.ext import ApplicationBuilder, CommandHandler

# --- WEB SERVER FOR RENDER HEALTH CHECKS ---
app = Flask('')
IS_RUNNING = True  # Strategy Loop Flag

@app.route('/')
def home():
    status = "RUNNING" if IS_RUNNING else "STOPPED"
    return f"System status: {status}. Monitoring market strategies and tracking tick velocity 24/5."

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- CONFIGURATION FROM ENVIRONMENT ---
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
FINNHUB_API_KEY = os.getenv('FINNHUB_API_KEY')

# Full 24 Pairs for Sequential Strategy Scanning (Yahoo Format)
STRATEGY_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCAD", "EURAUD", "EURNZD", "EURCHF",
    "GBPJPY", "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY", "AUDCAD", "AUDNZD"
]
TOUCH_THRESHOLD = 0.000003
last_alerts = {}

# Free Tier Fix: Slice the matrix to ONLY track the first 5 pairs for Velocity via WebSocket
VELOCITY_PAIRS = [f"OANDA:{p[:3]}_{p[3:]}" for p in STRATEGY_PAIRS[:5]]
SAFETY_THRESHOLD = 500
market_history = defaultdict(lambda: defaultdict(int))

# Initialize Sync Bot Core for background loop signals
sync_bot = telebot.TeleBot(TOKEN)

# --- FINNHUB WEBSOCKET DATA STREAM (ASYNC) ---
async def websocket_listener():
    if not FINNHUB_API_KEY:
        print("Finnhub API Key missing! Velocity tracking disabled.")
        return
        
    uri = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
    while True:
        try:
            async with websockets.connect(uri) as ws:
                for pair in VELOCITY_PAIRS:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": pair}))
                print(f"Successfully subscribed to {len(VELOCITY_PAIRS)} streams for free tier velocity tracking.")

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("type") == "trade":
                        for trade in data['data']:
                            symbol = trade['s']
                            hour = int(time.strftime("%H"))
                            if 6 <= hour <= 22:
                                market_history[symbol][hour] += 1
        except Exception as e:
            print(f"WebSocket Error: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

# --- MACD & RSI STRATEGY ANALYSIS (SYNC FORK) ---
def send_telegram_signal(msg):
    try:
        sync_bot.send_message(CHAT_ID, msg)
    except Exception as e:
        print(f"Signal Routing Error: {e}")

def get_yfinance_data(pair, interval):
    try:
        df = yf.download(f"{pair}=X", period="2d", interval=interval, progress=False)
        if df.empty: return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None

def analyze_ticker(pair):
    df_m5 = get_yfinance_data(pair, "5m")
    df_m1 = get_yfinance_data(pair, "1m")

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

    if 30 <= rsi <= 45:
        if gap < TOUCH_THRESHOLD and curr_m < curr_s:
            send_telegram_signal(f"🔍 [GET READY😁] {pair}\nLines touching. Price: {price:.5f}")
            last_alerts[alert_key] = time.time()
        elif prev_m < prev_s and curr_m > curr_s and m1_m > m1_s:
            send_telegram_signal(f"🔥⬆️✅ [BUY] {pair}\nBullish cross! Price: {price:.5f}")
            last_alerts[alert_key] = time.time()

    elif 55 <= rsi <= 70:
        if gap < TOUCH_THRESHOLD and curr_m > curr_s:
            send_telegram_signal(f"📉 [GET READY😁] {pair}\nLines touching. Price: {price:.5f}")
            last_alerts[alert_key] = time.time()
        elif prev_m > prev_s and curr_m < curr_s and m1_m < m1_s:
            send_telegram_signal(f"📉⬇️✅ [SELL] {pair}\nBearish cross! Price: {price:.5f}")
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
                    time.sleep(2)  # Defensive 2-second sleep per pair request
                
                # Free Tier Fix: Sleep for 5 minutes (300 seconds) to avoid Yahoo IP blocks
                time.sleep(300)
            else:
                time.sleep(5)
        else:
            time.sleep(3600)

# --- TELEGRAM ASYNC CONTROL CHANNELS ---
async def start_bot_cmd(update, context):
    global IS_RUNNING
    if str(update.effective_chat.id) != str(CHAT_ID): return
    IS_RUNNING = True
    await update.message.reply_text("🚀 Technical strategy matrix active. Scanning markets...")

async def stop_bot_cmd(update, context):
    global IS_RUNNING
    if str(update.effective_chat.id) != str(CHAT_ID): return
    IS_RUNNING = False
    await update.message.reply_text("🛑 Technical scans paused. Market stream remaining active.")

async def status_cmd(update, context):
    if str(update.effective_chat.id) != str(CHAT_ID): return
    state = "🟢 ACTIVE" if IS_RUNNING else "🔴 PAUSED"
    await update.message.reply_text(f"Strategy Scan Status: {state}")

async def velocity_cmd(update, context):
    if str(update.effective_chat.id) != str(CHAT_ID): return
    current_hour_int = int(time.strftime("%H"))
    display_hour = max(6, min(current_hour_int, 22))

    header = f"📈 FOREX VELOCITY (06:00 - {time.strftime('%H')}:00)\n```\n"
    header += "PAIR       | Ticks | STATUS\n-----------|-------|-------\n"

    rows = ""
    for pair in VELOCITY_PAIRS:
        hist = market_history.get(pair, {})
        total_vol = sum(hist.get(h, 0) for h in range(6, display_hour + 1))
        hours_elapsed = max(1, display_hour - 6 + 1)
        status = "SAFE" if total_vol >= (SAFETY_THRESHOLD * hours_elapsed) else "UNSAFE"
        
        clean_name = pair.replace('OANDA:', '').replace('_', '')
        rows += f"{clean_name.ljust(10)} | {str(total_vol).rjust(5)} | {status}\n"

    await update.message.reply_text(header + rows + "```", parse_mode="Markdown")

# --- ENVIRONMENT COUPLING ---
async def main():
    if not TOKEN:
        print("TELEGRAM_TOKEN missing from environment variables!")
        return

    app_tg = ApplicationBuilder().token(TOKEN).build()
    
    app_tg.add_handler(CommandHandler("start_bot", start_bot_cmd))
    app_tg.add_handler(CommandHandler("stop_bot", stop_bot_cmd))
    app_tg.add_handler(CommandHandler("status", status_cmd))
    app_tg.add_handler(CommandHandler("velocity", velocity_cmd))

    await app_tg.initialize()
    await app_tg.start()
    await app_tg.updater.start_polling()

    await websocket_listener()

if __name__ == "__main__":
    t_strategy = threading.Thread(target=strategy_loop)
    t_strategy.daemon = True
    t_strategy.start()

    t_web = threading.Thread(target=run_web_server)
    t_web.daemon = True
    t_web.start()

    asyncio.run(main())


