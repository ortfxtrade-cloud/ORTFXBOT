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
    return f"System status: {status}. Monitoring {len(STRATEGY_PAIRS)} market strategies 24/5."

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- CONFIGURATION FROM ENVIRONMENT ---
TOKEN = os.environ.get("TELEGRAM_TOKEN", "8686769653:AAFGUPCasmvUo3UFyHtyCljAgtfCbysn-08")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8701685996")
DEPLOY_HOOK = os.environ.get("RENDER_DEPLOY_HOOK", "https://api.render.com/deploy/srv-d8slig6gvqtc738d9rjg?key=oAz0lVAFCyc")
OCR_API_KEY = os.environ.get("OCR_API_KEY", "K89169183488957") # Replace with your real OCR.space key

# Initialize Bot
sync_bot = telebot.TeleBot(TOKEN)

# Made this global list dynamic
STRATEGY_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCAD", "EURAUD", "EURNZD", "EURCHF",
    "GBPJPY", "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD"
]

last_alerts = {}
alert_lock = threading.Lock()
pairs_lock = threading.Lock()  # Lock to ensure thread safety when modifying the watchlist

# --- HELPER LOGIC FOR DYNAMIC PAIR PARSING ---
def clean_and_add_pairs(text_input):
    """Parses text, extracts valid 6-character forex pairs, and updates the watchlist."""
    import re
    # Find all words that look like forex combinations (e.g., EURUSD, EUR/USD, GBP-USD)
    potential_pairs = re.findall(r'[A-Za-z]{3}[/-]?[A-Za-z]{3}', text_input)
    
    added_pairs = []
    with pairs_lock:
        for p in potential_pairs:
            cleaned = p.replace('/', '').replace('-', '').upper()
            if len(cleaned) == 6 and cleaned not in STRATEGY_PAIRS:
                STRATEGY_PAIRS.append(cleaned)
                added_pairs.append(cleaned)
    return added_pairs

# --- NATIVE YFINANCE VELOCITY CHECKER ---
def calculate_yfinance_velocity(series_m1):
    try:
        if series_m1 is None or len(series_m1) < 5:
            return "⚠️ UNCONFIRMED (Data Missing)"
        recent_closes = series_m1.tail(5)
        volatility = recent_closes.max() - recent_closes.min()
        return "🟢 SAFE (Active Liquidity)" if volatility > 0 else "⚠️ LOW VELOCITY (Stale Session)"
    except Exception:
        return "⚠️ UNCONFIRMED (Calculation Error)"

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
            if 'Close' in df.columns.get_level_values(0):
                extracted = df.xs('Close', axis=1, level=0).squeeze()
                return extracted if isinstance(extracted, pd.Series) else extracted.iloc[:, 0]
        elif 'Close' in df.columns:
            return df['Close'].squeeze()
        return None
    except Exception as e:
        print(f"Data Fetch Error for {pair}: {e}")
        return None

# --- MACD & RSI STRATEGY ANALYSIS ---
def analyze_ticker(pair):
    close_m5 = get_yfinance_data(pair, "5m")
    close_m1 = get_yfinance_data(pair, "1m")

    if close_m5 is None or close_m1 is None or len(close_m5) < 40 or len(close_m1) < 40:
        return

    macd_m5, signal_m5 = calculate_macd(close_m5)
    macd_m1, signal_m1 = calculate_macd(close_m1)
    
    rsi_series = calculate_rsi(close_m5)
    if rsi_series.empty:
        return
    
    rsi = float(rsi_series.iloc[-1])
    price = float(close_m5.iloc[-1])

    curr_m, prev_m = float(macd_m5.iloc[-1]), float(macd_m5.iloc[-2])
    curr_s, prev_s = float(signal_m5.iloc[-1]), float(signal_m5.iloc[-2])

    gap = abs(curr_m - curr_s)
    prev_gap = abs(prev_m - prev_s)
    
    m1_m, m1_s = float(macd_m1.iloc[-1]), float(macd_m1.iloc[-1])

    alert_key = f"{pair}_alert"
    with alert_lock:
        if time.time() - last_alerts.get(alert_key, 0) < 300:
            return

    is_jpy_pair = "JPY" in pair
    is_exotic_pair = any(exotic in pair for exotic in ["ZAR", "TRY", "INR", "MXN", "SGD", "HKD", "CNH"])

    if is_jpy_pair or is_exotic_pair or price > 10:
        DYNAMIC_THRESHOLD = 0.03
        PRE_ALERT_ZONE = 0.08  
    else:
        DYNAMIC_THRESHOLD = 0.0003
        PRE_ALERT_ZONE = 0.0008

    if gap < DYNAMIC_THRESHOLD:
        return

    has_crossed_bullish = prev_m < prev_s and curr_m > curr_s
    has_crossed_bearish = prev_m > prev_s and curr_m < curr_s
    is_shrinking = gap < prev_gap

    triggered = False
    msg_to_send = ""

    if 30 <= rsi <= 45:
        if is_shrinking and not has_crossed_bullish and gap <= PRE_ALERT_ZONE:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"🔍 *[GET READY]* {pair}\nLines converging for potential BUY.\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVelocity: *{velocity}*"
            triggered = True
        elif has_crossed_bullish and m1_m > m1_s:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"🔥⬆️✅ *[BUY]* {pair}\nBullish cross validated!\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVelocity: *{velocity}*"
            triggered = True

    elif 55 <= rsi <= 70:
        if is_shrinking and not has_crossed_bearish and gap <= PRE_ALERT_ZONE:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"🔍 *[GET READY]* {pair}\nLines converging for potential SELL.\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVelocity: *{velocity}*"
            triggered = True
        elif has_crossed_bearish and m1_m < m1_s:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"📉⬇️✅ *[SELL]* {pair}\nBearish cross validated!\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVelocity: *{velocity}*"
            triggered = True

    if triggered:
        with alert_lock:
            last_alerts[alert_key] = time.time()
        send_telegram_signal(msg_to_send)

def responsive_sleep(seconds):
    for _ in range(int(seconds)):
        if not IS_RUNNING:
            break
        time.sleep(1)

def strategy_loop():
    global IS_RUNNING
    print("Strategy scanning mechanism initialized...")
    while True:
        current_day = time.gmtime().tm_wday
        if current_day < 5: 
            if IS_RUNNING:
                # We copy the list references safely before scanning to prevent threading iteration errors
                with pairs_lock:
                    current_watchlist = list(STRATEGY_PAIRS)
                
                for pair in current_watchlist:
                    if not IS_RUNNING: 
                        break
                    try:
                        analyze_ticker(pair)
                    except Exception as e:
                        print(f"Error checking {pair}: {e}")
                    time.sleep(1)
                
                responsive_sleep(300)
            else:
                time.sleep(2)
        else:
            time.sleep(60)
            
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
    sync_bot.reply_to(message, f"Strategy Scan Status: {state}\nTracking `{len(STRATEGY_PAIRS)}` tickers.")

@sync_bot.message_handler(commands=['watchlist'])
def view_watchlist_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    with pairs_lock:
        pairs_string = ", ".join(STRATEGY_PAIRS)
    sync_bot.reply_to(message, f"📋 *Active Watchlist ({len(STRATEGY_PAIRS)}):*\n`{pairs_string}`", parse_mode="Markdown")

# Feature: Add pairs via custom text command (e.g. /add EURCAD, AUDNZD)
@sync_bot.message_handler(commands=['add'])
def add_pairs_text_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    raw_text = message.text.replace('/add', '')
    if not raw_text.strip():
        sync_bot.reply_to(message, "⚠️ Usage: `/add USDCAD, EURGBP`", parse_mode="Markdown")
        return
        
    added = clean_and_add_pairs(raw_text)
    if added:
        sync_bot.reply_to(message, f"✅ Successfully added to scanning pool:\n`{', '.join(added)}`", parse_mode="Markdown")
    else:
        sync_bot.reply_to(message, "⚠️ No new or valid 6-letter asset pairs were detected.")

# Feature: Add pairs via screenshot ingestion (OCR Engine)
@sync_bot.message_handler(content_types=['photo'])
def handle_image_watchlist(message):
    if str(message.chat.id) != str(CHAT_ID): return
    
    sync_bot.reply_to(message, "⚡ Processing image layout. Scanning text for valid asset keys...")
    try:
        # Get image files details from Telegram cloud endpoints
        file_info = sync_bot.get_file(message.photo[-1].file_id)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        
        # Stream remote routing request straight into cloud OCR space platform
        payload = {
            'url': file_url,
            'apikey': OCR_API_KEY,
            'isOverlayRequired': False,
            'scale': True
        }
        response = requests.post("https://api.ocr.space/parse/image", data=payload, timeout=15).json()
        
        if response.get("OCRExitCode") == 1:
            parsed_text = response["ParsedResults"][0]["ParsedText"]
            added = clean_and_add_pairs(parsed_text)
            if added:
                sync_bot.reply_to(message, f"🎉 OCR Extraction Success!\nAdded to strategy loop:\n`{', '.join(added)}`", parse_mode="Markdown")
            else:
                sync_bot.reply_to(message, f"🔍 Image read completed, but no *new* valid forex pairs found.\nText scanned:\n`{parsed_text}`", parse_mode="Markdown")
        else:
            sync_bot.reply_to(message, "❌ OCR failed to read text. Please ensure the chart symbols are clearly visible.")
    except Exception as e:
        sync_bot.reply_to(message, f"❌ Error reading image asset matrix: {e}")

@sync_bot.message_handler(commands=['deploy'])
def deploy_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    if not DEPLOY_HOOK:
        sync_bot.reply_to(message, "❌ Deploy hook missing.")
        return
    sync_bot.reply_to(message, "🔄 Triggering remote build architecture on Render...")
    try:
        response = requests.post(DEPLOY_HOOK, timeout=10)
        if response.status_code in [200, 204, 201]:
            sync_bot.send_message(CHAT_ID, "🚀 Deploy command accepted!")
        else:
            sync_bot.send_message(CHAT_ID, f"⚠️ Response code: {response.status_code}")
    except Exception as e:
        sync_bot.send_message(CHAT_ID, f"❌ Request error: {e}")

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
