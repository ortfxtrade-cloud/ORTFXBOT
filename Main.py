import os
import re
import time
import datetime
import threading
import logging
import pandas as pd
import numpy as np
import requests
import telebot
import yfinance as yf
from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Configure structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s")
logger = logging.getLogger(__name__)

# ==============================================================================
# 1. ENVIRONMENT CONFIGURATION & DEFAULTS
# ==============================================================================
TELEGRAM_TOKEN = "8686769653:AAFHxNO5l8Oe6_QIQiY1vqXKwaFeUDywFTE"
CHAT_ID = "8701685996"
RENDER_DEPLOY_HOOK = "https://api.render.com/deploy/srv-d8slig6gvqtc738d9rjg?key=oAz0lVAFCyc"
OCR_API_KEY = "K89169183488957"
JSONBIN_KEY = "$2a$10$r5OJ.Ut/MaT2dYCZTZ4Im./0w3SvtdviC1c/IAWNNaMLmYGySb7T."
JSONBIN_ID = "6a4d4662f5f4af5e296dcd83"
PORT = int(os.environ.get("PORT", 8080))

DEFAULT_WATCHLIST = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCAD=X", "USDCHF=X"]

data_lock = threading.Lock()
STRATEGY_PAIRS = []
last_alerts = {}
IS_RUNNING = True

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

# ==============================================================================
# 2. DATA PROCESSING & INDICATORS
# ==============================================================================
def fetch_and_clean_data(pair, interval):
    df = yf.download(pair, period="2d", interval=interval, progress=False)
    if not df.empty:
        df.columns = df.columns.get_level_values(0) if isinstance(df.columns, pd.MultiIndex) else df.columns
    return df

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig

def calculate_rsi(series, periods=14):
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(periods).mean()
    loss = (-delta.clip(upper=0)).rolling(periods).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))

def is_unauthorized(message):
    return str(message.chat.id) != str(CHAT_ID)

# ==============================================================================
# 3. CLOUD PERSISTENCE (JSONBin)
# ==============================================================================
def load_watchlist():
    global STRATEGY_PAIRS
    url = f"https://api.jsonbin.v3/b/{JSONBIN_ID}/latest"
    try:
        response = requests.get(url, headers={"X-Master-Key": JSONBIN_KEY}, timeout=10)
        if response.status_code == 200:
            data = response.json().get("record", {})
            with data_lock: STRATEGY_PAIRS = list(data.get("pairs", DEFAULT_WATCHLIST))
            logger.info(f"Watchlist initialized: {STRATEGY_PAIRS}")
    except Exception as e:
        logger.error(f"Load failed: {e}")
        with data_lock: STRATEGY_PAIRS = list(DEFAULT_WATCHLIST)

def sync_watchlist():
    with data_lock: payload = {"pairs": STRATEGY_PAIRS}
    try:
        requests.put(f"https://api.jsonbin.v3/b/{JSONBIN_ID}", json=payload, 
                     headers={"Content-Type": "application/json", "X-Master-Key": JSONBIN_KEY}, timeout=10)
    except Exception as e: logger.error(f"Sync failed: {e}")

# ==============================================================================
# 4. SCANNER ENGINE
# ==============================================================================
def scan_market_assets():
    global IS_RUNNING
    logger.info("Adaptive Scanner (Touch + Compression Filter) initialized.")
    while True:
        try:
            if IS_RUNNING:
                with data_lock: pairs_to_scan = list(STRATEGY_PAIRS)
                for pair in pairs_to_scan:
                    df_5m = fetch_and_clean_data(pair, "5m")
                    if len(df_5m) < 40: continue

                    high_low = df_5m['High'] - df_5m['Low']
                    atr = high_low.rolling(window=14).mean().iloc[-1]
                    TOUCH_ZONE = atr * 0.15 
                    
                    macd, signal = calculate_macd(df_5m['Close'])
                    rsi = calculate_rsi(df_5m['Close']).iloc[-1]
                    
                    last_5_gaps = [abs(macd.iloc[i] - signal.iloc[i]) for i in range(-5, 0)]
                    is_active_market = any(g > (atr * 0.05) for g in last_5_gaps)
                    
                    curr_m, curr_s = macd.iloc[-1], signal.iloc[-1]
                    prev_m, prev_s = macd.iloc[-2], signal.iloc[-2]
                    
                    if is_active_market:
                        alert_type, msg = None, ""
                        if 30 <= rsi <= 45:
                            if (prev_m < prev_s) and (curr_m >= curr_s):
                                alert_type, msg = "CONFIRMED_BUY", "🔥⬆️✅ *[BUY TRADE CONFIRMED]*\n"
                            elif abs(curr_m - curr_s) <= TOUCH_ZONE and (curr_m < curr_s):
                                alert_type, msg = "PRE_BUY", "🔍 *[GET READY] BUY SETUP*\n"
                        elif 55 <= rsi <= 70:
                            if (prev_m > prev_s) and (curr_m <= curr_s):
                                alert_type, msg = "CONFIRMED_SELL", "📉⬇️✅ *[SELL TRADE CONFIRMED]*\n"
                            elif abs(curr_m - curr_s) <= TOUCH_ZONE and (curr_m > curr_s):
                                alert_type, msg = "PRE_SELL", "🔍 *[GET READY] SELL SETUP*\n"

                        if alert_type:
                            key = f"{pair}_{alert_type}"
                            if key not in last_alerts or (time.time() - last_alerts[key]) >= 600:
                                last_alerts[key] = time.time()
                                bot.send_message(CHAT_ID, f"{msg}📌 *Asset:* {pair}\n💰 *Price:* {df_5m['Close'].iloc[-1]:.5f}")
            time.sleep(60)
        except Exception as e:
            logger.error(f"Scanner error: {e}")
            time.sleep(15)

# ==============================================================================
# 5. TELEGRAM COMMANDS & OCR
# ==============================================================================
def generate_interactive_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("📊 System Status", callback_data="btn_status"),
               InlineKeyboardButton("📋 Watchlist", callback_data="btn_watchlist"))
    markup.add(InlineKeyboardButton("🚀 Start", callback_data="btn_start"),
               InlineKeyboardButton("🛑 Stop", callback_data="btn_stop"))
    return markup

@bot.message_handler(commands=['start', 'menu'])
def handle_menu(m):
    if is_unauthorized(m): return
    bot.send_message(m.chat.id, "⚙️ Control Board", reply_markup=generate_interactive_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global IS_RUNNING
    if call.data == "btn_status": bot.send_message(call.message.chat.id, f"State: {'🟢 RUNNING' if IS_RUNNING else '🛑 PAUSED'}")
    elif call.data == "btn_start": IS_RUNNING = True; bot.answer_callback_query(call.id, "Activated")
    elif call.data == "btn_stop": IS_RUNNING = False; bot.answer_callback_query(call.id, "Paused")

@bot.message_handler(commands=['add'])
def add(m):
    if is_unauthorized(m): return
    raw = m.text.replace('/add', '').strip().upper()
    with data_lock:
        for sym in re.findall(r'[A-Z0-9=]{3,10}', raw):
            if sym not in STRATEGY_PAIRS: STRATEGY_PAIRS.append(sym)
    sync_watchlist()
    bot.reply_to(m, f"✅ Registry: {STRATEGY_PAIRS}")

@bot.message_handler(commands=['remove'])
def remove(m):
    if is_unauthorized(m): return
    raw = m.text.replace('/remove', '').strip().upper()
    with data_lock:
        for sym in re.findall(r'[A-Z0-9=]{3,10}', raw):
            if sym in STRATEGY_PAIRS: STRATEGY_PAIRS.remove(sym)
    sync_watchlist()
    bot.reply_to(m, f"🗑️ Registry: {STRATEGY_PAIRS}")

@bot.message_handler(content_types=['photo'])
def handle_ocr(m):
    if is_unauthorized(m): return
    file_info = bot.get_file(m.photo[-1].file_id)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
    res = requests.post("https://api.ocr.space/parse/image", data={'url': url, 'apikey': OCR_API_KEY}).json()
    extracted = re.findall(r'\b[A-Z]{6}\b', res.get("ParsedResults", [{}])[0].get("ParsedText", "").upper())
    with data_lock:
        for sym in [f"{s}=X" for s in extracted]:
            if sym not in STRATEGY_PAIRS: STRATEGY_PAIRS.append(sym)
    sync_watchlist()
    bot.reply_to(m, f"🎯 OCR Added: {extracted}")

# ==============================================================================
# 6. BOOTSTRAP
# ==============================================================================
if __name__ == "__main__":
    load_watchlist()
    threading.Thread(target=scan_market_assets, name="ScannerThread", daemon=True).start()
    server = Flask(__name__)
    @server.route('/')
    def health(): return "RUNNING", 200
    threading.Thread(target=lambda: server.run(host="0.0.0.0", port=PORT), daemon=True).start()
    bot.infinity_polling()
