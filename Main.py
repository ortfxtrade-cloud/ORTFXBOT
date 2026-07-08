import os
import re
import time
import logging
import threading
import requests
import pandas as pd
import yfinance as yf
from flask import Flask
import telebot
from telebot import types

# ==============================================================================
# 0. LOGGING & INITIALIZATION CONFIGURATION
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TradingBot")

# Secure retrieval of environment configurations with default fallbacks
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
RENDER_DEPLOY_HOOK = os.environ.get("RENDER_DEPLOY_HOOK", "")
OCR_API_KEY = os.environ.get("OCR_API_KEY", "helloworld")
JSONBIN_KEY = os.environ.get("JSONBIN_KEY", "")
JSONBIN_ID = os.environ.get("JSONBIN_ID", "")
PORT = int(os.environ.get("PORT", 8080))

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="MARKDOWN")
app = Flask(__name__)

# ==============================================================================
# 1. GLOBAL STATE & THREAD LOCKING
# ==============================================================================
state_lock = threading.Lock()
IS_RUNNING = True
STRATEGY_PAIRS = []
last_alerts = {}  # Format: { "PAIR_ALERT_TYPE": timestamp }
USER_STATE = {}   # Simple state tracker for chat flows: { chat_id: state }

# Immutable Master list for native UI generation
MASTER_BINARY_ARRAY = [
    "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "USDCAD=X", "USDCHF=X",
    "USDJPY=X", "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "CHFJPY=X",
    "CADJPY=X", "NZDJPY=X"
]
DEFAULT_FALLBACK_ARRAY = ["EURUSD=X", "GBPUSD=X", "AUDUSD=X", "USDJPY=X", "EURJPY=X"]

# ==============================================================================
# 2. CLOUD PERSISTENCE ENGINE (JSONBIN)
# ==============================================================================
def load_watchlist_from_cloud():
    global STRATEGY_PAIRS
    if not JSONBIN_KEY or not JSONBIN_ID:
        logger.warning("Cloud configuration missing. Loading local defaults.")
        with state_lock:
            STRATEGY_PAIRS = list(DEFAULT_FALLBACK_ARRAY)
        return

    url = f"https://api.jsonbin.v3/b/{JSONBIN_ID}/latest"
    headers = {"X-Master-Key": JSONBIN_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            # Handle standard JSONBin record schema structures
            pairs = data.get("record", {}).get("watchlist", [])
            if pairs:
                with state_lock:
                    STRATEGY_PAIRS = list(pairs)
                logger.info(f"Watchlist successfully synchronized from cloud: {STRATEGY_PAIRS}")
                return
        logger.error(f"JSONBin read failed ({response.status_code}). Using fallback.")
    except Exception as e:
        logger.error(f"Exception loading from JSONBin: {e}")
    
    with state_lock:
        STRATEGY_PAIRS = list(DEFAULT_FALLBACK_ARRAY)

def save_watchlist_to_cloud():
    if not JSONBIN_KEY or not JSONBIN_ID:
        return
    url = f"https://api.jsonbin.v3/b/{JSONBIN_ID}"
    headers = {
        "X-Master-Key": JSONBIN_KEY,
        "Content-Type": "application/json"
    }
    with state_lock:
        payload = {"watchlist": STRATEGY_PAIRS}
    try:
        res = requests.put(url, headers=headers, json=payload, timeout=10)
        if res.status_code == 200:
            logger.info("Watchlist successfully duplicated to JSONBin Cloud Store.")
        else:
            logger.error(f"JSONBin save failed with status: {res.status_code}")
    except Exception as e:
        logger.error(f"Exception sync writing to cloud state: {e}")

# Load watchlist before starting background tasks
load_watchlist_from_cloud()

# ==============================================================================
# 3. NATIVE TELEGRAM KEYBOARD INTERFACE LAYOUTS
# ==============================================================================
def get_main_dashboard_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_add = types.KeyboardButton("➕ Quick Add Asset")
    btn_rem = types.KeyboardButton("🗑️ Quick Remove Asset")
    btn_show = types.KeyboardButton("📋 Show Active Watchlist")
    btn_ctrl = types.KeyboardButton("⚙️ Open Control Panel")
    markup.add(btn_add, btn_rem, btn_show, btn_ctrl)
    return markup

def get_add_asset_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    with state_lock:
        available_assets = [item for item in MASTER_BINARY_ARRAY if item not in STRATEGY_PAIRS]
    
    buttons = [types.KeyboardButton(asset) for asset in available_assets]
    markup.add(*buttons)
    markup.add(types.KeyboardButton("🔙 Cancel"))
    return markup

def get_remove_asset_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    with state_lock:
        current_assets = list(STRATEGY_PAIRS)
    
    buttons = [types.KeyboardButton(asset) for asset in current_assets]
    markup.add(*buttons)
    markup.add(types.KeyboardButton("🔙 Cancel"))
    return markup

def get_control_panel_inline():
    markup = types.InlineKeyboardMarkup(row_width=1)
    status_btn = types.InlineKeyboardButton("📊 System Status", callback_data="ctrl_status")
    
    global IS_RUNNING
    toggle_text = "🛑 Pause Scanning" if IS_RUNNING else "🚀 Start Scanning"
    toggle_btn = types.InlineKeyboardButton(toggle_text, callback_data="ctrl_toggle")
    redeploy_btn = types.InlineKeyboardButton("🔄 Re-Deploy Bot", callback_data="ctrl_redeploy")
    
    markup.add(status_btn, toggle_btn, redeploy_btn)
    return markup

# ==============================================================================
# 4. MATH & TECHNICAL ANALYSIS INDICATORS
# ==============================================================================
def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calculate_macd(series: pd.Series, fast=12, slow=26, signal=9):
    fast_ema = calculate_ema(series, fast)
    slow_ema = calculate_ema(series, slow)
    macd_line = fast_ema - slow_ema
    signal_line = calculate_ema(macd_line, signal)
    return macd_line, signal_line

def calculate_rsi(series: pd.Series, period=14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))

# ==============================================================================
# 5. MARKET SCANNER BACKGROUND LOGIC (THREAD 3)
# ==============================================================================
def parse_asset_parameters(symbol: str, last_price: float):
    # Parameter check covering currencies, structural index pricing, and cross-pairs
    is_jpy = "JPY" in symbol
    is_exotic = any(ex in symbol for ex in ["ZAR", "TRY", "MXN", "INR", "SGD", "HKD", "CNH"])
    
    if is_jpy or is_exotic or last_price > 10:
        return {
            "PRE_ALERT_ZONE": 0.08,
            "MIN_PRE_ALERT": 0.06,
            "DYNAMIC_THRESHOLD": 0.03,
            "MIN_VOLATILITY": 0.04
        }
    else:
        return {
            "PRE_ALERT_ZONE": 0.0008,
            "MIN_PRE_ALERT": 0.0006,
            "DYNAMIC_THRESHOLD": 0.0003,
            "MIN_VOLATILITY": 0.0004
        }

def process_market_analysis():
    global IS_RUNNING, STRATEGY_PAIRS, last_alerts
    
    while True:
        try:
            # Weekend validation logic (UTC)
            current_utc = time.gmtime()
            if current_utc.tm_wday in (5, 6): # 5 = Saturday, 6 = Sunday
                logger.info("Market is closed (Weekend). Engine sleeping for 60 seconds.")
                time.sleep(60)
                continue
            
            if not IS_RUNNING:
                time.sleep(5)
                continue
                
            with state_lock:
                pairs_to_scan = list(STRATEGY_PAIRS)
            
            logger.info(f"Starting analysis scan sequence across {len(pairs_to_scan)} assets.")
            
            for pair in pairs_to_scan:
                # Execution buffer boundary protection for yfinance requests
                time.sleep(1) 
                
                # Download historical sets 
                data_5m = yf.download(pair, period="2d", interval="5m", progress=False, group_by='ticker')
                data_1m = yf.download(pair, period="2d", interval="1m", progress=False, group_by='ticker')
                
                if data_5m.empty or data_1m.empty:
                    continue
                
                # Multi-index safely flattened via cross-section isolation
                if isinstance(data_5m.columns, pd.MultiIndex):
                    try: data_5m = data_5m.xs(pair, axis=1, level=0)
                    except KeyError: pass
                if isinstance(data_1m.columns, pd.MultiIndex):
                    try: data_1m = data_1m.xs(pair, axis=1, level=0)
                    except KeyError: pass

                if len(data_5m) < 40 or len(data_1m) < 5:
                    logger.warning(f"Ticker {pair} skipped due to insufficient structural historical bars.")
                    continue
                
                close_5m = data_5m['Close'].ffill().dropna()
                close_1m = data_1m['Close'].ffill().dropna()
                
                # Derive core indicator profiles
                macd_5m, signal_5m = calculate_macd(close_5m)
                macd_1m, signal_1m = calculate_macd(close_1m)
                rsi_5m = calculate_rsi(close_5m)
                
                # Extract values for the current and prior periods
                c_macd_5m, c_sig_5m = macd_5m.iloc[-1], signal_5m.iloc[-1]
                p_macd_5m, p_sig_5m = macd_5m.iloc[-2], signal_5m.iloc[-2]
                c_macd_1m, c_sig_1m = macd_1m.iloc[-1], signal_1m.iloc[-1]
                c_rsi_5m = rsi_5m.iloc[-1]
                
                # Operational Metrics calculations
                last_5_close_1m = close_1m.iloc[-5:]
                velocity = "⚠️ STALE" if (last_5_close_1m.max() - last_5_close_1m.min()) == 0 else "🟢 SAFE"
                
                last_5_close_5m = close_5m.iloc[-5:]
                volatility_range = abs(last_5_close_5m.max() - last_5_close_5m.min())
                
                # Structural asset assignment logic
                current_price = close_5m.iloc[-1]
                params = parse_asset_parameters(pair, current_price)
                
                is_compressed = volatility_range < params["MIN_VOLATILITY"]
                
                # Evaluate convergence/divergence behavior
                gap = abs(c_macd_5m - c_sig_5m)
                prev_gap = abs(p_macd_5m - p_sig_5m)
                is_shrinking = gap < prev_gap
                
                now = time.time()
                
                # --------------------------------------------------------------
                # SIGNAL STRATEGY DECISION MATRIX
                # --------------------------------------------------------------
                # A. OVERSOLD SETUP (BUY LOOKOUT)
                if 30 <= c_rsi_5m <= 45:
                    if not is_compressed and is_shrinking and (c_macd_5m < c_sig_5m):
                        if params["MIN_PRE_ALERT"] <= gap <= params["PRE_ALERT_ZONE"]:
                            alert_key = f"{pair}_PRE_BUY"
                            if now - last_alerts.get(alert_key, 0) > 300:
                                msg = (
                                    f"🔍 *[GET READY] BUY SETUP*\n\n"
                                    f"• *Asset:* `{pair}`\n• *Price:* `{current_price:.5f}`\n"
                                    f"• *5m RSI:* `{c_rsi_5m:.2f}`\n• *Gap:* `{gap:.6f}`\n"
                                    f"• *Velocity:* {velocity}\n• *Status:* Normal Structural Horizon"
                                )
                                bot.send_message(CHAT_ID, msg)
                                last_alerts[alert_key] = now
                                
                    # Bullish Confirmation Execution Matrix
                    if gap >= params["DYNAMIC_THRESHOLD"]:
                        if (p_macd_5m < p_sig_5m and c_macd_5m > c_sig_5m) and (c_macd_1m > c_sig_1m):
                            alert_key = f"{pair}_CONFIRMED_BUY"
                            if now - last_alerts.get(alert_key, 0) > 300:
                                msg = (
                                    f"🔥⬆️✅ *[BUY] SIGNAL CONFIRMED*\n\n"
                                    f"• *Asset:* `{pair}`\n• *Execution Entry:* `{current_price:.5f}`\n"
                                    f"• *Macro 5m RSI:* `{c_rsi_5m:.2f}`\n• *Micro 1m MACD:* Bullish Convergence\n"
                                    f"• *Velocity:* {velocity}"
                                )
                                bot.send_message(CHAT_ID, msg)
                                last_alerts[alert_key] = now

                # B. OVERBOUGHT SETUP (SELL LOOKOUT)
                elif 55 <= c_rsi_5m <= 70:
                    if not is_compressed and is_shrinking and (c_macd_5m > c_sig_5m):
                        if params["MIN_PRE_ALERT"] <= gap <= params["PRE_ALERT_ZONE"]:
                            alert_key = f"{pair}_PRE_SELL"
                            if now - last_alerts.get(alert_key, 0) > 300:
                                msg = (
                                    f"🔍 *[GET READY] SELL SETUP*\n\n"
                                    f"• *Asset:* `{pair}`\n• *Price:* `{current_price:.5f}`\n"
                                    f"• *5m RSI:* `{c_rsi_5m:.2f}`\n• *Gap:* `{gap:.6f}`\n"
                                    f"• *Velocity:* {velocity}\n• *Status:* Normal Structural Horizon"
                                )
                                bot.send_message(CHAT_ID, msg)
                                last_alerts[alert_key] = now
                                
                    # Bearish Confirmation Execution Matrix
                    if gap >= params["DYNAMIC_THRESHOLD"]:
                        if (p_macd_5m > p_sig_5m and c_macd_5m < c_sig_5m) and (c_macd_1m < c_sig_1m):
                            alert_key = f"{pair}_CONFIRMED_SELL"
                            if now - last_alerts.get(alert_key, 0) > 300:
                                msg = (
                                    f"📉⬇️✅ *[SELL] SIGNAL CONFIRMED*\n\n"
                                    f"• *Asset:* `{pair}`\n• *Execution Entry:* `{current_price:.5f}`\n"
                                    f"• *Macro 5m RSI:* `{c_rsi_5m:.2f}`\n• *Micro 1m MACD:* Bearish Convergence\n"
                                    f"• *Velocity:* {velocity}"
                                )
                                bot.send_message(CHAT_ID, msg)
                                last_alerts[alert_key] = now
                                
            logger.info("Scan sequence completed across watchlist. Entering 5-minute cycle throttle.")
            time.sleep(300)
            
        except Exception as e:
            logger.error(f"Error caught inside the main scanner engine iteration loop: {e}", exc_info=True)
            time.sleep(30) # Cool-off before retry during connection failure

# ==============================================================================
# 6. PYTELEGRAMBOTAPI INTERACTION GATEWAY
# ==============================================================================
@bot.message_handler(func=lambda msg: str(msg.chat.id) != str(CHAT_ID))
def handle_unauthorized_access(message):
    logger.warning(f"Unauthorized interception blocked from chat ID: {message.chat.id}")
    return

@bot.message_handler(commands=['start', 'menu'])
def handle_start_command(message):
    USER_STATE[message.chat.id] = None
    bot.send_message(
        message.chat.id, 
        "🤖 *Algorithmic Matrix Dashboard Engine Active.*\nSelect operational workflows via touch inputs below.",
        reply_markup=get_main_dashboard_keyboard()
    )

@bot.message_handler(func=lambda msg: True)
def handle_text_inputs(message):
    chat_id = message.chat.id
    text = message.text.strip()
    
    # Process Cancel input across all workflows
    if text == "🔙 Cancel":
        USER_STATE[chat_id] = None
        bot.send_message(chat_id, "❌ Action aborted. Returning to Main Dashboard.", reply_markup=get_main_dashboard_keyboard())
        return

    current_state = USER_STATE.get(chat_id)

    # 1. Handle Active Input States
    if current_state == "WAITING_FOR_ADD":
        if text in MASTER_BINARY_ARRAY:
            with state_lock:
                if text not in STRATEGY_PAIRS:
                    STRATEGY_PAIRS.append(text)
                    msg = f"✅ Asset `{text}` added to tracking list."
                else:
                    msg = f"⚠️ Asset `{text}` is already tracked."
            USER_STATE[chat_id] = None
            save_watchlist_to_cloud()
            bot.send_message(chat_id, msg, reply_markup=get_main_dashboard_keyboard())
        else:
            bot.send_message(chat_id, "⚠️ Invalid selection. Please use the touch grid options.", reply_markup=get_add_asset_keyboard())
        return

    if current_state == "WAITING_FOR_REMOVE":
        with state_lock:
            if text in STRATEGY_PAIRS:
                STRATEGY_PAIRS.remove(text)
                msg = f"🗑️ Asset `{text}` removed from active scanner pipelines."
            else:
                msg = f"⚠️ Asset `{text}` not found inside tracking pool."
        USER_STATE[chat_id] = None
        save_watchlist_to_cloud()
        bot.send_message(chat_id, msg, reply_markup=get_main_dashboard_keyboard())
        return

    # 2. Process Native Main Dashboard Button Clicks
    if text == "➕ Quick Add Asset":
        USER_STATE[chat_id] = "WAITING_FOR_ADD"
        bot.send_message(chat_id, "Select an asset to append to the background engine tracker:", reply_markup=get_add_asset_keyboard())
        
    elif text == "🗑️ Quick Remove Asset":
        with state_lock:
            empty = len(STRATEGY_PAIRS) == 0
        if empty:
            bot.send_message(chat_id, "⚠️ The current watchlist profile is completely empty.", reply_markup=get_main_dashboard_keyboard())
        else:
            USER_STATE[chat_id] = "WAITING_FOR_REMOVE"
            bot.send_message(chat_id, "Select an asset to purge from execution workflows:", reply_markup=get_remove_asset_keyboard())
            
    elif text == "📋 Show Active Watchlist":
        with state_lock:
            current_watchlist = list(STRATEGY_PAIRS)
        if current_watchlist:
            formatted = "\n".join([f"• `{item}`" for item in current_watchlist])
            msg = f"📋 *Active Operational Watchlist:*\n\n{formatted}"
        else:
            msg = "⚠️ Watchlist contains 0 targets."
        bot.send_message(chat_id, msg, reply_markup=get_main_dashboard_keyboard())
        
    elif text == "⚙️ Open Control Panel":
        bot.send_message(chat_id, "⚙️ *System Control Infrastructure Panel:*", reply_markup=get_control_panel_inline())

# ==============================================================================
# 7. INLINE INTERACTIVE ROUTING QUERIES
# ==============================================================================
@bot.callback_query_handler(func=lambda call: True)
def process_inline_callbacks(call):
    if str(call.message.chat.id) != str(CHAT_ID):
        return
        
    global IS_RUNNING, STRATEGY_PAIRS
    
    if call.data == "ctrl_status":
        with state_lock:
            status_str = "🟢 RUNNING" if IS_RUNNING else "🛑 PAUSED"
            total_pairs = len(STRATEGY_PAIRS)
        metrics = (
            f"📊 *System Infrastructure Telemetry Metrics:*\n\n"
            f"• *Scanner Loop:* {status_str}\n"
            f"• *Active Tracked Pairs:* `{total_pairs}`\n"
            f"• *Thread Handlers Running:* `{threading.active_count()}`"
        )
        bot.answer_callback_query(call.id, "Telemetry Extracted")
        bot.edit_message_text(metrics, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_control_panel_inline())
        
    elif call.data == "ctrl_toggle":
        IS_RUNNING = not IS_RUNNING
        status_txt = "Engine Activated" if IS_RUNNING else "Engine Paused"
        bot.answer_callback_query(call.id, status_txt)
        bot.edit_message_text(f"🔄 System state altered to: *{'RUNNING' if IS_RUNNING else 'PAUSED'}*", chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_control_panel_inline())
        
    elif call.data == "ctrl_redeploy":
        if RENDER_DEPLOY_HOOK:
            try:
                res = requests.post(RENDER_DEPLOY_HOOK, timeout=10)
                if res.status_code in [200, 201, 202]:
                    bot.answer_callback_query(call.id, "Deployment Pipeline Initiated")
                    bot.send_message(call.message.chat.id, "🚀 *Render Deploy hook fired successfully.* Server instance resetting.")
                else:
                    bot.answer_callback_query(call.id, "Hook Failure")
            except Exception as e:
                logger.error(f"Deployment hook execution exception: {e}")
                bot.answer_callback_query(call.id, "API Connection Error")
        else:
            bot.answer_callback_query(call.id, "Hook Missing")
            bot.send_message(call.message.chat.id, "⚠️ `RENDER_DEPLOY_HOOK` variable environment key is empty.")

# ==============================================================================
# 8. ADVANCED OCR VISION MEDIA PROCESSING LAYER
# ==============================================================================
@bot.message_handler(content_types=['photo'])
def handle_incoming_chart_vision(message):
    try:
        bot.send_message(message.chat.id, "📥 *Processing image upload... Running OCR analytics Extraction Engine.*")
        
        # Pull high resolution photo profile matrix
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        
        # Dispatch to remote parsing engine
        payload = {
            'url': file_url,
            'apikey': OCR_API_KEY,
            'isOverlayRequired': False,
            'scale': True,
            'OCREngine': 2
        }
        response = requests.post("https://api.ocr.space/parse/image", data=payload, timeout=20)
        parsed_results = response.json()
        
        if parsed_results.get("OCRExitCode") == 1:
            extracted_text = ""
            for item in parsed_results.get("ParsedResults", []):
                extracted_text += " " + str(item.get("ParsedText", ""))
            
            logger.info(f"Raw OCR Engine text parsed: {extracted_text}")
            
            # Match major currency tickers via regex
            found_pairs = re.findall(r'\b[A-Z]{6}\b', extracted_text.upper())
            # Secondary regex pattern fallback to capture formatted/slashed standard structures (e.g., EUR/USD)
            slashed_pairs = re.findall(r'\b([A-Z]{3})/([A-Z]{3})\b', extracted_text.upper())
            for match in slashed_pairs:
                found_pairs.append(f"{match[0]}{match[1]}")
                
            discovered = []
            if found_pairs:
                with state_lock:
                    for raw_symbol in set(found_pairs):
                        formatted_symbol = f"{raw_symbol}=X"
                        if formatted_symbol in MASTER_BINARY_ARRAY and formatted_symbol not in STRATEGY_PAIRS:
                            STRATEGY_PAIRS.append(formatted_symbol)
                            discovered.append(formatted_symbol)
                            
                if discovered:
                    save_watchlist_to_cloud()
                    bot.send_message(message.chat.id, f"✅ *Vision Matrix complete.* Synthesized assets appended to watchlist:\n`{discovered}`", reply_markup=get_main_dashboard_keyboard())
                    return
                    
            bot.send_message(message.chat.id, "⚠️ Image processing completed. No missing master binary options asset signatures discovered.", reply_markup=get_main_dashboard_keyboard())
        else:
            bot.send_message(message.chat.id, "❌ OCR Space API parsing engine failed to analyze the image.", reply_markup=get_main_dashboard_keyboard())
            
    except Exception as e:
        logger.error(f"Image vision processing exception handling: {e}")
        bot.send_message(message.chat.id, "❌ Fatal error parsing graphic file pipeline assets.", reply_markup=get_main_dashboard_keyboard())

# ==============================================================================
# 9. FLASK WEB SERVER INFRASTRUCTURE (THREAD 2)
# ==============================================================================
@app.route('/')
def handle_render_health_check_ping():
    with state_lock:
        status = "RUNNING" if IS_RUNNING else "STOPPED"
        count = len(STRATEGY_PAIRS)
    return f"SCANNER STATUS: {status} | ACTIVE PAIRS COUNT: {count}", 200

def run_flask_web_server():
    logger.info(f"Initializing Flask health web engine backend thread on port: {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ==============================================================================
# 10. MULTI-THREADED RUNTIME ARCHITECTURE EXECUTION (MAIN THREAD = POLLING)
# ==============================================================================
if __name__ == "__main__":
    # Thread 2 initialization: Flask Web Server Interface
    flask_thread = threading.Thread(target=run_flask_web_server, name="FlaskWebEngineThread", daemon=True)
    flask_thread.start()
    
    # Thread 3 initialization: Market Analysis Core Scanner Engine
    scanner_thread = threading.Thread(target=process_market_analysis, name="MarketScannerEngineThread", daemon=True)
    scanner_thread.start()
    
    # Thread 1: Dedicated to handling blocking bot Polling on the Main Thread
    logger.info("Starting Telegram Infinity Polling execution framework on main thread instance.")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            logger.error(f"Main Polling Engine crash intercepted. Re-establishing loop linkage: {e}")
            time.sleep(10)
