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
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask

# Configure structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s")
logger = logging.getLogger(__name__)

# ==============================================================================
# 1. ENVIRONMENT CONFIGURATION & DEFAULTS
# ==============================================================================
TELEGRAM_TOKEN = "8686769653:AAFHxNO5l8Oe6_QIQiY1vqXKwaFeUDywFTE"
CHAT_ID = "8701685996"
RENDER_DEPLOY_HOOK ="https://api.render.com/deploy/srv-d8slig6gvqtc738d9rjg?key=oAz0lVAFCyc"
OCR_API_KEY = "K89169183488957"
JSONBIN_KEY = "$2a$10$r5OJ.Ut/MaT2dYCZTZ4Im./0w3SvtdviC1c/IAWNNaMLmYGySb7T."
JSONBIN_ID =  "6a4d4662f5f4af5e296dcd83"
PORT = int(os.environ.get("PORT", 8080))

# Fallback Watchlist
DEFAULT_WATCHLIST = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCAD=X", "USDCHF=X"]

# Global Core Variables & Threading Protection Locks
data_lock = threading.Lock()
STRATEGY_PAIRS = []
last_alerts = {}  # Tracks { "PAIR_STRATEGY": timestamp }
IS_RUNNING = True

# Initialize Telegram Bot
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

# ==============================================================================
# 2. CLOUD PERSISTENCE MANAGERS (JSONBin API)
# ==============================================================================
def load_watchlist():
    global STRATEGY_PAIRS
    if not JSONBIN_KEY or not JSONBIN_ID:
        logger.warning("JSONBin configurations missing. Loading hardcoded fallbacks.")
        with data_lock:
            STRATEGY_PAIRS = list(DEFAULT_WATCHLIST)
        return

    url = f"https://api.jsonbin.v3/b/{JSONBIN_ID}/latest"
    headers = {"X-Master-Key": JSONBIN_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json().get("record", {})
            pairs = data.get("pairs", DEFAULT_WATCHLIST)
            with data_lock:
                STRATEGY_PAIRS = list(pairs)
            logger.info(f"Watchlist successfully initialized from JSONBin: {STRATEGY_PAIRS}")
        else:
            raise Exception(f"Status Code {response.status_code}")
    except Exception as e:
        logger.error(f"Failed to load watchlist from JSONBin ({e}). Using fallbacks.")
        with data_lock:
            STRATEGY_PAIRS = list(DEFAULT_WATCHLIST)

def sync_watchlist():
    if not JSONBIN_KEY or not JSONBIN_ID:
        return
    
    url = f"https://api.jsonbin.v3/b/{JSONBIN_ID}"
    headers = {
        "Content-Type": "application/json",
        "X-Master-Key": JSONBIN_KEY
    }
    with data_lock:
        payload = {"pairs": STRATEGY_PAIRS}
        
    try:
        response = requests.put(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            logger.info("Watchlist synced to JSONBin successfully.")
        else:
            logger.error(f"JSONBin sync failed. Status: {response.status_code}")
    except Exception as e:
        logger.error(f"Error executing JSONBin sync: {e}")

# ==============================================================================
# 3. MATHEMATICAL INDICATORS & CORE SIGNAL ENGINE
# ==============================================================================


                        full_payload = (
                            f"{alert_msg}"
                            f"📌 *Asset:* {pair}\n"
                            f"💰 *Price:* {current_price:.5f}\n"
                            f"📊 *5m RSI:* {rsi_5m:.2f}\n"
                            f"📐 *Macro Gap:* {gap:.6f} / Boundary: {DYNAMIC_THRESHOLD}\n"
                            f"⚡ *1m Velocity:* {velocity}\n"
                            f"🕒 *Timestamp:* {utc_now.strftime('%H:%M:%S')} UTC"
                        )
                        try:
                            bot.send_message(CHAT_ID, full_payload)
                            logger.info(f"Alert transmitted to Chat ID for asset {pair} [{alert_type}].")
                        except Exception as telegram_err:
                            logger.error(f"Telegram alert delivery failure: {telegram_err}")

            # Safe Throttling Guard
            time.sleep(300)

        except Exception as e:
            logger.error(f"Fatal disruption in TA scanning engine thread execution loops: {e}", exc_info=True)
            time.sleep(15) # Safety buffer padding to survive network dropped packets

# ==============================================================================
# 4. INTERACTIVE TELEGRAM INTERACTION INTERFACES
# ==============================================================================
# ==============================================================================
# 3. MATHEMATICAL INDICATORS & CORE SIGNAL ENGINE
# ==============================================================================
def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

def calculate_rsi(series, periods=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=periods).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=periods).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))

def fetch_and_clean_data(ticker, timeframe):
    import yfinance as yf
    df = yf.download(tickers=ticker, period="2d", interval=timeframe, progress=False, group_by='ticker')
    if df.empty: return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(ticker, axis=1, level=0) if ticker in df.columns.levels[0] else df.copy()
    return df

def scan_market_assets():
    global IS_RUNNING
    logger.info("Adaptive Scanner (ATR-Dynamic) initialized.")
    while True:
        try:
            if not IS_RUNNING:
                time.sleep(5)
                continue
            
            with data_lock: pairs_to_scan = list(STRATEGY_PAIRS)
            for pair in pairs_to_scan:
                df_5m = fetch_and_clean_data(pair, "5m")
                df_1m = fetch_and_clean_data(pair, "1m")
                if len(df_5m) < 40 or len(df_1m) < 40: continue

                # DYNAMIC ATR (Self-calibrating volatility)
                high_low = df_5m['High'] - df_5m['Low']
                atr = high_low.rolling(window=14).mean().iloc[-1]
                TOUCH_ZONE = atr * 0.5 
                
                # INDICATORS
                macd_5m, signal_5m = calculate_macd(df_5m['Close'])
                rsi_5m = calculate_rsi(df_5m['Close']).iloc[-1]
                
                # NOISE FILTERS (Pulse & Compression)
                last_5_gaps = [abs(macd_5m.iloc[i] - signal_5m.iloc[i]) for i in range(-5, 0)]
                is_compressed = all(g < (atr * 0.1) for g in last_5_gaps)
                
                curr_m, curr_s = macd_5m.iloc[-1], signal_5m.iloc[-1]
                prev_m, prev_s = macd_5m.iloc[-2], signal_5m.iloc[-2]
                
                alert_type, alert_msg = None, ""
                
                if not is_compressed:
                    # BUY LOGIC
                    if 30 <= rsi_5m <= 45:
                        if (prev_m < prev_s) and (curr_m >= curr_s) and (calculate_macd(df_1m['Close'])[0].iloc[-1] > calculate_macd(df_1m['Close'])[1].iloc[-1]):
                            alert_type, alert_msg = "CONFIRMED_BUY", "🔥⬆️✅ *[BUY TRADE CONFIRMED]*\n"
                        elif abs(curr_m - curr_s) <= TOUCH_ZONE and (curr_m < curr_s):
                            alert_type, alert_msg = "PRE_BUY", "🔍 *[GET READY] BUY SETUP*\n"
                    # SELL LOGIC
                    elif 55 <= rsi_5m <= 70:
                        if (prev_m > prev_s) and (curr_m <= curr_s) and (calculate_macd(df_1m['Close'])[0].iloc[-1] < calculate_macd(df_1m['Close'])[1].iloc[-1]):
                            alert_type, alert_msg = "CONFIRMED_SELL", "📉⬇️✅ *[SELL TRADE CONFIRMED]*\n"
                        elif abs(curr_m - curr_s) <= TOUCH_ZONE and (curr_m > curr_s):
                            alert_type, alert_msg = "PRE_SELL", "🔍 *[GET READY] SELL SETUP*\n"

                if alert_type:
                    key = f"{pair}_{alert_type}"
                    if key not in last_alerts or (time.time() - last_alerts[key]) >= 300:
                        last_alerts[key] = time.time()
                        bot.send_message(CHAT_ID, f"{alert_msg}📌 *Asset:* {pair}\n💰 *Price:* {df_5m['Close'].iloc[-1]:.5f}")
            time.sleep(60)
        except Exception as e:
            logger.error(f"Scanner error: {e}")
            time.sleep(15)
def is_unauthorized(message):
    return str(message.chat.id) != str(CHAT_ID)

def generate_interactive_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📊 System Status", callback_data="btn_status"),
        InlineKeyboardButton("📋 Watchlist", callback_data="btn_watchlist")
    )
    markup.add(
        InlineKeyboardButton("🚀 Start Scanning", callback_data="btn_start"),
        InlineKeyboardButton("🛑 Pause Scanning", callback_data="btn_stop")
    )
    markup.add(InlineKeyboardButton("🔄 Re-Deploy Bot", callback_data="btn_deploy"))
    return markup

@bot.message_handler(commands=['start', 'menu'])
def handle_menu_command(message):
    if is_unauthorized(message): return
    bot.send_message(
        message.chat.id, 
        "⚙️ *Forex/Crypto Multi-Threaded Engine Core Control Board*", 
        reply_markup=generate_interactive_menu()
    )

@bot.callback_query_handler(func=lambda call: True)
def process_menu_callbacks(call):
    global IS_RUNNING
    if str(call.message.chat.id) != str(CHAT_ID): return
    
    action = call.data
    with data_lock:
        active_count = len(STRATEGY_PAIRS)

    if action == "btn_status":
        status_str = "🟢 RUNNING" if IS_RUNNING else "🛑 PAUSED / STOPPED"
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, f"📊 *System Profile Status:*\nState: `{status_str}`\nTracked Pairs: `{active_count}`")
        
    elif action == "btn_watchlist":
        with data_lock:
            pairs_list = "\n".join([f"• `{p}`" for p in STRATEGY_PAIRS])
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, f"📋 *Active Watchlist Asset Pool:*\n{pairs_list if pairs_list else 'Empty Watchlist'}")
        
    elif action == "btn_start":
        IS_RUNNING = True
        bot.answer_callback_query(call.id, "Scanning Loop Activated!")
        bot.send_message(call.message.chat.id, "🚀 Market core scanner state modified: `RUNNING`")
        
    elif action == "btn_stop":
        IS_RUNNING = False
        bot.answer_callback_query(call.id, "Scanning Loop Suspended!")
        bot.send_message(call.message.chat.id, "🛑 Market core scanner state modified: `STOPPED`")
        
    elif action == "btn_deploy":
        bot.answer_callback_query(call.id)
        if RENDER_DEPLOY_HOOK:
            try:
                res = requests.post(RENDER_DEPLOY_HOOK, timeout=15)
                if res.status_code in [200, 201, 202]:
                    bot.send_message(call.message.chat.id, "🚀 Infrastructure rebuild pipeline executed cleanly on Render!")
                else:
                    bot.send_message(call.message.chat.id, f"❌ Cloud infrastructure hook returned error status: {res.status_code}")
            except Exception as ex:
                bot.send_message(call.message.chat.id, f"❌ Rebuild transmission failed: `{ex}`")
        else:
            bot.send_message(call.message.chat.id, "⚠️ Webhook address missing configuration. Set `RENDER_DEPLOY_HOOK` environment variable.")

@bot.message_handler(commands=['add'])
def append_watchlist_asset(message):
    if is_unauthorized(message): return
    raw_text = message.text.replace('/add', '').strip().upper()
    matches = re.findall(r'[A-Z0-9=]{3,10}', raw_text)
    
    if not matches:
        bot.reply_to(message, "❌ Invalid input formatting. Example usage: `/add EURUSD=X` or `/add BTC-USD`")
        return
        
    added = []
    with data_lock:
        for symbol in matches:
            if symbol not in STRATEGY_PAIRS:
                STRATEGY_PAIRS.append(symbol)
                added.append(symbol)
                
    if added:
        sync_watchlist()
        bot.reply_to(message, f"✅ Successfully added assets to engine registry: {added}")
    else:
        bot.reply_to(message, "⚠️ Specified symbols are already initialized in active watchlist.")

@bot.message_handler(commands=['remove'])
def extract_watchlist_asset(message):
    if is_unauthorized(message): return
    raw_text = message.text.replace('/remove', '').strip().upper()
    matches = re.findall(r'[A-Z0-9=]{3,10}', raw_text)
    
    if not matches:
        bot.reply_to(message, "❌ Invalid input formatting. Example usage: `/remove EURUSD=X`")
        return
        
    removed = []
    with data_lock:
        for symbol in matches:
            if symbol in STRATEGY_PAIRS:
                STRATEGY_PAIRS.remove(symbol)
                removed.append(symbol)
                
    if removed:
        sync_watchlist()
        bot.reply_to(message, f"🗑️ Successfully removed assets from registry: {removed}")
    else:
        bot.reply_to(message, "⚠️ No requested symbols matching existing records found.")

@bot.message_handler(content_types=['photo'])
def handle_incoming_ocr_images(message):
    if is_unauthorized(message): return
    try:
        bot.reply_to(message, "📥 Matrix image payload verified. Querying OCR Space Engines...")
        file_info = bot.get_file(message.photo[-1].file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        
        # Dispatch Request to Free OCR Space APIs
        ocr_payload = {
            'url': image_url,
            'apikey': OCR_API_KEY,
            'isOverlayRequired': False,
            'scale': True
        }
        res = requests.post("https://api.ocr.space/parse/image", data=ocr_payload, timeout=25).json()
        
        parsed_results = res.get("ParsedResults", [])
        if not parsed_results:
            bot.reply_to(message, "❌ Document mapping layout parse returned null content matches.")
            return
            
        extracted_text = parsed_results[0].get("ParsedText", "").upper()
        # Filter typical 6-character clean tickers or common formats
        discovered_symbols = re.findall(r'\b[A-Z]{6}\b', extracted_text)
        
        # Append structural modifiers suffix default to standard market items if matches are clean
        formatted_symbols = [f"{sym}=X" if not sym.endswith("=X") else sym for sym in discovered_symbols]
        
        if not formatted_symbols:
            bot.reply_to(message, f"🔍 Extracted text did not contain clear 6-letter currency codes.\nRaw Text:\n`{extracted_text[:200]}`")
            return
            
        added = []
        with data_lock:
            for symbol in formatted_symbols:
                if symbol not in STRATEGY_PAIRS:
                    STRATEGY_PAIRS.append(symbol)
                    added.append(symbol)
                    
        if added:
            sync_watchlist()
            bot.reply_to(message, f"🎯 *OCR Engine Intercept Success!*\nExtracted text contains actionable pairs: `{discovered_symbols}`\nAdded: `{added}`")
        else:
            bot.reply_to(message, f"ℹ️ OCR processing parsed symbols `{formatted_symbols}`, but they are already tracking.")
            
    except Exception as err:
        logger.error(f"Image pipeline engine failure: {err}")
        bot.reply_to(message, f"❌ Fatal interface fault processing server imagery parsing components: `{err}`")

# ==============================================================================
# 5. FLASK WEB FRAMEWORK SERVICE (Render Automated Health Check Binding)
# ==============================================================================
server = Flask(__name__)

@server.route('/')
def live_health_status_endpoint():
    status_msg = "RUNNING" if IS_RUNNING else "STOPPED"
    with data_lock:
        count = len(STRATEGY_PAIRS)
    return f"STATUS: {status_msg} | TRACKED_PAIRS: {count}", 200

def run_flask_app():
    logger.info(f"Booting Flask core web engine on port: {PORT}")
    server.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ==============================================================================
# 6. APPLICATION BOOTSTRAP INITIALIZATION PIPELINES
# ==============================================================================
if __name__ == "__main__":
    logger.info("Initializing multi-threaded architecture engines...")
    
    # 1. Fetch Cloud Memory Watchlist Store
    load_watchlist()

    # 2. Fire Thread 2: Flask Automated Diagnostics Monitoring Web Service
    flask_worker = threading.Thread(target=run_flask_app, name="FlaskWebServerThread", daemon=True)
    flask_worker.start()

    # 3. Fire Thread 3: Production Technical Analysis Engine Scanner System
    scanner_worker = threading.Thread(target=scan_market_assets, name="TechnicalScannerThread", daemon=True)
    scanner_worker.start()

    # 4. Bind Thread 1 (Main Thread) exclusively to Telegram Infinity connection polling listeners
    logger.info("Main Thread bound to Telegram Infinity Polling Loops. Platform Engine Live.")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=25)
        except Exception as e:
            logger.error(f"Telegram Bot network pooling crash recovery triggered: {e}")
            time.sleep(5)
