import os
import time
import threading
from flask import Flask
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton  # 🚀 Added for button layouts
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

# --- CONFIGURATION FROM ENVIRONMENT (SECURE SETUP) ---
TOKEN = os.environ.get("TELEGRAM_TOKEN", "8686769653:AAEkb4gEe5ruW8XMcj0Cntsu6VvZ1j_ZgnU")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8701685996")
DEPLOY_HOOK = os.environ.get("RENDER_DEPLOY_HOOK", "https://api.render.com/deploy/srv-d8slig6gvqtc738d9rjg?key=oAz0lVAFCyc")
OCR_API_KEY = os.environ.get("OCR_API_KEY", "K89169183488957") 

JSONBIN_KEY = os.environ.get("JSONBIN_KEY", "$2a$10$r5OJ.Ut/MaT2dYCZTZ4Im./0w3SvtdviC1c/IAWNNaMLmYGySb7T.")
JSONBIN_ID = os.environ.get("JSONBIN_ID", "6a4d4662f5f4af5e296dcd83")

sync_bot = telebot.TeleBot(TOKEN)

STRATEGY_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF"]
last_alerts = {}
alert_lock = threading.Lock()
pairs_lock = threading.Lock()  

# --- CLOUD PERSISTENCE DATABASE LOGIC ---
def load_watchlist_from_cloud():
    global STRATEGY_PAIRS
    try:
        headers = {"X-Master-Key": JSONBIN_KEY}
        url = f"https://api.jsonbin.v3/b/{JSONBIN_ID}/latest"
        response = requests.get(url, headers=headers, timeout=10).json()
        if "record" in response:
            with pairs_lock:
                STRATEGY_PAIRS = list(response["record"])
    except Exception as e:
        print(f"Cloud Read Fail: {e}")

def save_watchlist_to_cloud():
    try:
        headers = {"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"}
        url = f"https://api.jsonbin.v3/b/{JSONBIN_ID}"
        with pairs_lock:
            data = list(STRATEGY_PAIRS)
        requests.put(url, json=data, headers=headers, timeout=10)
    except Exception as e:
        print(f"Cloud Save Fail: {e}")

def clean_and_add_pairs(text_input):
    import re
    potential_pairs = re.findall(r'[A-Za-z]{3}[/-]?[A-Za-z]{3}', text_input)
    added_pairs = []
    with pairs_lock:
        for p in potential_pairs:
            cleaned = p.replace('/', '').replace('-', '').upper()
            if len(cleaned) == 6 and cleaned not in STRATEGY_PAIRS:
                STRATEGY_PAIRS.append(cleaned)
                added_pairs.append(cleaned)
    if added_pairs:
        save_watchlist_to_cloud()
    return added_pairs

# --- NATIVE YFINANCE VELOCITY CHECKER ---
def calculate_yfinance_velocity(series_m1):
    try:
        if series_m1 is None or len(series_m1) < 5:
            return "⚠️ DATA MISSING"
        recent_closes = series_m1.tail(5)
        volatility = recent_closes.max() - recent_closes.min()
        return "🟢 SAFE" if volatility > 0 else "⚠️ STALE"
    except Exception:
        return "⚠️ ERROR"

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
        if df is None or df.empty: return None
        if isinstance(df.columns, pd.MultiIndex):
            if 'Close' in df.columns.get_level_values(0):
                extracted = df.xs('Close', axis=1, level=0).squeeze()
                return extracted if isinstance(extracted, pd.Series) else extracted.iloc[:, 0]
        elif 'Close' in df.columns:
            return df['Close'].squeeze()
        return None
    except Exception as e:
        print(f"Fetch Error: {e}")
        return None

def analyze_ticker(pair):
    close_m5 = get_yfinance_data(pair, "5m")
    close_m1 = get_yfinance_data(pair, "1m")
    if close_m5 is None or close_m1 is None or len(close_m5) < 40 or len(close_m1) < 40: return

    macd_m5, signal_m5 = calculate_macd(close_m5)
    macd_m1, signal_m1 = calculate_macd(close_m1)
    rsi_series = calculate_rsi(close_m5)
    if rsi_series.empty: return
    
    rsi = float(rsi_series.iloc[-1])
    price = float(close_m5.iloc[-1])
    curr_m, prev_m = float(macd_m5.iloc[-1]), float(macd_m5.iloc[-2])
    curr_s, prev_s = float(signal_m5.iloc[-1]), float(signal_m5.iloc[-2])
    gap = abs(curr_m - curr_s)
    prev_gap = abs(prev_m - prev_s)
    m1_m, m1_s = float(macd_m1.iloc[-1]), float(macd_m1.iloc[-1])

    alert_key = f"{pair}_alert"
    with alert_lock:
        if time.time() - last_alerts.get(alert_key, 0) < 300: return

    is_jpy_pair = "JPY" in pair
    is_exotic_pair = any(exotic in pair for exotic in ["ZAR", "TRY", "INR", "MXN", "SGD", "HKD", "CNH"])
    DYNAMIC_THRESHOLD = 0.03 if (is_jpy_pair or is_exotic_pair or price > 10) else 0.0003
    PRE_ALERT_ZONE = 0.08 if (is_jpy_pair or is_exotic_pair or price > 10) else 0.0008

    if gap < DYNAMIC_THRESHOLD: return
    has_crossed_bullish = prev_m < prev_s and curr_m > curr_s
    has_crossed_bearish = prev_m > prev_s and curr_m < curr_s
    is_shrinking = gap < prev_gap

    triggered = False
    msg_to_send = ""

    if 30 <= rsi <= 45:
        if is_shrinking and not has_crossed_bullish and gap <= PRE_ALERT_ZONE:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"🔍 *[GET READY]* {pair}\nConverging for BUY.\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVel: *{velocity}*"
            triggered = True
        elif has_crossed_bullish and m1_m > m1_s:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"🔥⬆️✅ *[BUY]* {pair}\nCross validated!\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVel: *{velocity}*"
            triggered = True
    elif 55 <= rsi <= 70:
        if is_shrinking and not has_crossed_bearish and gap <= PRE_ALERT_ZONE:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"🔍 *[GET READY]* {pair}\nConverging for SELL.\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVel: *{velocity}*"
            triggered = True
        elif has_crossed_bearish and m1_m < m1_s:
            velocity = calculate_yfinance_velocity(close_m1)
            msg_to_send = f"📉⬇️✅ *[SELL]* {pair}\nCross validated!\nPrice: `{price:.5f}`\nRSI: `{rsi:.2f}`\nVel: *{velocity}*"
            triggered = True

    if triggered:
        with alert_lock: last_alerts[alert_key] = time.time()
        send_telegram_signal(msg_to_send)

def responsive_sleep(seconds):
    for _ in range(int(seconds)):
        if not IS_RUNNING: break
        time.sleep(1)

def strategy_loop():
    global IS_RUNNING
    while True:
        current_day = time.gmtime().tm_wday
        if current_day < 5: 
            if IS_RUNNING:
                with pairs_lock: current_watchlist = list(STRATEGY_PAIRS)
                for pair in current_watchlist:
                    if not IS_RUNNING: break
                    try: analyze_ticker(pair)
                    except Exception: pass
                    time.sleep(1)
                responsive_sleep(300)
            else: time.sleep(2)
        else: time.sleep(60)

# --- 🚀 NEW FEATURE: THE BUTTON CONTROL DASHBOARD ---
def generate_main_menu():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("📊 System Status", callback_data="btn_status"),
        InlineKeyboardButton("📋 Watchlist", callback_data="btn_watchlist")
    )
    markup.row(
        InlineKeyboardButton("🚀 Start Scanning", callback_data="btn_start"),
        InlineKeyboardButton("🛑 Pause Scanning", callback_data="btn_stop")
    )
    markup.row(
        InlineKeyboardButton("🔄 Re-Deploy Bot", callback_data="btn_deploy")
    )
    return markup

@sync_bot.message_handler(commands=['menu', 'start'])
def send_menu_dashboard(message):
    if str(message.chat.id) != str(CHAT_ID): return
    sync_bot.send_message(
        message.chat.id, 
        "⚙️ *FX Bot Control Matrix*\nClick any button below to manage the scanner.", 
        parse_mode="Markdown", 
        reply_markup=generate_main_menu()
    )

# --- BUTTON CLICK EVENT LISTENER ---
@sync_bot.callback_query_handler(func=lambda call: True)
def handle_menu_clicks(call):
    global IS_RUNNING
    if str(call.message.chat.id) != str(CHAT_ID): return

    # 1. Status Button
    if call.data == "btn_status":
        state = "🟢 ACTIVE" if IS_RUNNING else "🔴 PAUSED"
        sync_bot.answer_callback_query(call.id, "Fetched system status!")
        sync_bot.send_message(call.message.chat.id, f"Strategy Scan Status: {state}\nTracking `{len(STRATEGY_PAIRS)}` tickers.")

    # 2. Watchlist Button
    elif call.data == "btn_watchlist":
        with pairs_lock: pairs_string = ", ".join(STRATEGY_PAIRS)
        sync_bot.answer_callback_query(call.id)
        sync_bot.send_message(call.message.chat.id, f"📋 *Active Watchlist:* \n`{pairs_string}`\n\n💡 _To edit this list, send a handwritten note photo, or use:_ \n`/add PAIR` or `/remove PAIR`", parse_mode="Markdown")

    # 3. Start Button
    elif call.data == "btn_start":
        IS_RUNNING = True
        sync_bot.answer_callback_query(call.id, "Scanner Activated!")
        sync_bot.send_message(call.message.chat.id, "🚀 Technical strategy matrix active. Scanning markets...")

    # 4. Stop Button
    elif call.data == "btn_stop":
        IS_RUNNING = False
        sync_bot.answer_callback_query(call.id, "Scanner Paused!")
        sync_bot.send_message(call.message.chat.id, "🛑 Technical scans paused.")

    # 5. Deploy Button
    elif call.data == "btn_deploy":
        sync_bot.answer_callback_query(call.id, "Sending hook...")
        sync_bot.send_message(call.message.chat.id, "🔄 Triggering remote build architecture on Render...")
        try:
            response = requests.post(DEPLOY_HOOK, timeout=10)
            if response.status_code in [200, 204, 201]:
                sync_bot.send_message(call.message.chat.id, "🚀 Deploy command accepted!")
            else:
                sync_bot.send_message(call.message.chat.id, f"⚠️ Hook response: {response.status_code}")
        except Exception as e:
            sync_bot.send_message(call.message.chat.id, f"❌ Request error: {e}")

# --- KEEPING RAW TEXT / TEXT OVERRIDES ALIVE ---
@sync_bot.message_handler(commands=['add'])
def add_pairs_text_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    raw_text = message.text.replace('/add', '').strip()
    if not raw_text:
        sync_bot.reply_to(message, "⚠️ Usage: `/add USDCAD, EURGBP`")
        return
    added = clean_and_add_pairs(raw_text)
    sync_bot.reply_to(message, f"✅ Added & Saved: `{', '.join(added)}`" if added else "⚠️ No new pairs detected.")

@sync_bot.message_handler(commands=['remove'])
def remove_pairs_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    raw_text = message.text.replace('/remove', '').strip().upper()
    import re
    targets = re.findall(r'[A-Z]{6}', raw_text)
    removed_pairs = []
    with pairs_lock:
        for target in targets:
            if target in STRATEGY_PAIRS:
                STRATEGY_PAIRS.remove(target)
                removed_pairs.append(target)
    if removed_pairs:
        save_watchlist_to_cloud()
        sync_bot.reply_to(message, f"❌ Removed: `{', '.join(removed_pairs)}`")
    else:
        sync_bot.reply_to(message, "⚠️ Pair not found.")

@sync_bot.message_handler(content_types=['photo'])
def handle_image_watchlist(message):
    if str(message.chat.id) != str(CHAT_ID): return
    sync_bot.reply_to(message, "⚡ Scanning handwriting for asset keys...")
    try:
        file_info = sync_bot.get_file(message.photo[-1].file_id)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        payload = {'url': file_url, 'apikey': OCR_API_KEY, 'scale': True, 'OCREngine': 3}
        response = requests.post("https://api.ocr.space/parse/image", data=payload, timeout=20).json()
        if response.get("OCRExitCode") == 1:
            parsed_text = response["ParsedResults"][0]["ParsedText"]
            added = clean_and_add_pairs(parsed_text)
            sync_bot.reply_to(message, f"🎉 Added to Cloud Watchlist:\n`{', '.join(added)}`" if added else f"🔍 No new pairs found.\nRead: `{parsed_text}`", parse_mode="Markdown")
        else:
            sync_bot.reply_to(message, "❌ OCR Engine read error.")
    except Exception as e:
        sync_bot.reply_to(message, f"❌ Error: {e}")

if __name__ == "__main__":
    load_watchlist_from_cloud()
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=strategy_loop, daemon=True).start()
    sync_bot.infinity_polling()
