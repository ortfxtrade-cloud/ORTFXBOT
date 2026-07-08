import os
import re
import time
import threading
import logging
import requests
import telebot
import yfinance as yf
from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Configure structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s")
logger = logging.getLogger(__name__)

# --- Configuration ---
TELEGRAM_TOKEN = "8686769653:AAFHxNO5l8Oe6_QIQiY1vqXKwaFeUDywFTE"
CHAT_ID = "8701685996"
OCR_API_KEY = "K89169183488957"
JSONBIN_KEY = "$2a$10$r5OJ.Ut/MaT2dYCZTZ4Im./0w3SvtdviC1c/IAWNNaMLmYGySb7T."
JSONBIN_ID = "6a4d4662f5f4af5e296dcd83"
PORT = int(os.environ.get("PORT", 8080))

data_lock = threading.Lock()
STRATEGY_PAIRS = []
IS_RUNNING = True
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

# --- Persistence & Logic ---
def load_watchlist():
    global STRATEGY_PAIRS
    res = requests.get(f"https://api.jsonbin.v3/b/{JSONBIN_ID}/latest", headers={"X-Master-Key": JSONBIN_KEY}, timeout=10)
    if res.status_code == 200:
        with data_lock: STRATEGY_PAIRS = list(res.json().get("record", {}).get("pairs", ["EURUSD=X"]))

def sync_watchlist():
    with data_lock: payload = {"pairs": STRATEGY_PAIRS}
    requests.put(f"https://api.jsonbin.v3/b/{JSONBIN_ID}", json=payload, headers={"Content-Type": "application/json", "X-Master-Key": JSONBIN_KEY}, timeout=10)

def is_unauthorized(m): return str(m.chat.id) != str(CHAT_ID)

# --- Command Interface (Restored) ---
def generate_interactive_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("📊 Status", callback_data="btn_status"),
               InlineKeyboardButton("📋 Watchlist", callback_data="btn_watchlist"))
    markup.add(InlineKeyboardButton("🚀 Start", callback_data="btn_start"),
               InlineKeyboardButton("🛑 Stop", callback_data="btn_stop"))
    return markup

@bot.message_handler(commands=['start', 'menu'])
def handle_menu(m):
    if is_unauthorized(m): return
    bot.send_message(m.chat.id, "⚙️ *Engine Control Board*", reply_markup=generate_interactive_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global IS_RUNNING
    if call.data == "btn_status": bot.send_message(call.message.chat.id, f"State: {'🟢 RUNNING' if IS_RUNNING else '🛑 PAUSED'}")
    elif call.data == "btn_watchlist": bot.send_message(call.message.chat.id, f"📋 Watchlist: `{', '.join(STRATEGY_PAIRS)}`")
    elif call.data == "btn_start": IS_RUNNING = True; bot.answer_callback_query(call.id, "Scanning Activated")
    elif call.data == "btn_stop": IS_RUNNING = False; bot.answer_callback_query(call.id, "Scanning Paused")

@bot.message_handler(commands=['add'])
def add(m):
    if is_unauthorized(m): return
    syms = re.findall(r'[A-Z0-9=]{3,10}', m.text.upper())
    with data_lock:
        for s in syms:
            if s != "ADD" and s not in STRATEGY_PAIRS: STRATEGY_PAIRS.append(s)
    sync_watchlist()
    bot.reply_to(m, f"✅ Added: {STRATEGY_PAIRS}")

@bot.message_handler(commands=['remove'])
def remove(m):
    if is_unauthorized(m): return
    syms = re.findall(r'[A-Z0-9=]{3,10}', m.text.upper())
    with data_lock:
        for s in syms:
            if s in STRATEGY_PAIRS: STRATEGY_PAIRS.remove(s)
    sync_watchlist()
    bot.reply_to(m, f"🗑️ Removed: {STRATEGY_PAIRS}")

@bot.message_handler(content_types=['photo'])
def handle_ocr(m):
    if is_unauthorized(m): return
    # OCR Logic
    file_info = bot.get_file(m.photo[-1].file_id)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
    res = requests.post("https://api.ocr.space/parse/image", data={'url': url, 'apikey': OCR_API_KEY}).json()
    extracted = re.findall(r'\b[A-Z]{6}\b', res.get("ParsedResults", [{}])[0].get("ParsedText", "").upper())
    with data_lock:
        for s in [f"{sym}=X" for sym in extracted]:
            if s not in STRATEGY_PAIRS: STRATEGY_PAIRS.append(s)
    sync_watchlist()
    bot.reply_to(m, f"🎯 OCR Added: {extracted}")

# --- Bootstrap ---
if __name__ == "__main__":
    try: bot.remove_webhook()
    except: pass
    load_watchlist()
    # (Scanner thread and Flask app omitted for brevity, ensure they remain in your main file)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
