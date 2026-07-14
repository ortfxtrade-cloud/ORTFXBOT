import os
import re
import time
import threading
import logging
import telebot
import yfinance as yf
from flask import Flask
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

# --- Configuration ---
TELEGRAM_TOKEN ="8686769653:AAHtda3UTFMxsW9MgnD-TTCa7qOKo7ypBms"
CHAT_ID ="8701685996"
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

# Flask setup to satisfy Render's Web Service requirement
app = Flask(__name__)

# Global State
data_lock = threading.Lock()
STRATEGY_PAIRS = ["EURUSD=X"]
IS_RUNNING = True
alert_cooldowns = {}

@app.route('/')
def health_check():
    return "Bot is running!"

# --- Core Scanner Engine ---
def calculate_strategy(df):
    fast_ema = df['Close'].ewm(span=12, adjust=False).mean()
    slow_ema = df['Close'].ewm(span=26, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return macd, signal, hist, rsi, rsi.diff()

def scanner_engine():
    global alert_cooldowns
    while True:
        if IS_RUNNING:
            with data_lock: 
                current_pairs = list(STRATEGY_PAIRS)
            
            for symbol in current_pairs:
                try:
                    df = yf.Ticker(symbol).history(period="5d", interval="5m")
                    if len(df) < 50: continue
                        
                    m, s, h, rsi, vel = calculate_strategy(df)
                    
                    # ADAPTIVE COMPRESSION
                    recent_volatility = abs(h.tail(20)).mean()
                    is_compressed = abs(h.iloc[-1]) < (recent_volatility * 0.4) 
                    
                    is_bull = (m.iloc[-2] <= s.iloc[-2]) and (m.iloc[-1] > s.iloc[-1])
                    is_bear = (m.iloc[-2] >= s.iloc[-2]) and (m.iloc[-1] < s.iloc[-1])
                    
                    if not is_compressed:
                        if (is_bull and 30 <= rsi.iloc[-1] <= 45) or (is_bear and 55 <= rsi.iloc[-1] <= 70):
                            if time.time() - alert_cooldowns.get(f"{symbol}_last", 0) > 300:
                                diff = m.iloc[-1] - s.iloc[-1]
                                direction = "BUY" if is_bull else "SELL"
                                icon = "🟢" if is_bull else "🔴"
                                
                                msg = (f"{icon} *{direction}* {symbol}\n"
                                       f"MACD Cross: Confirmed (5m)\n"
                                       f"MACD: {m.iloc[-1]:.5f} | Signal: {s.iloc[-1]:.5f}\n"
                                       f"Diff: {diff:.5f}\n"
                                       f"RSI: {rsi.iloc[-1]:.2f}\n"
                                       f"Status: SAFE (Active Liquidity)")
                                bot.send_message(CHAT_ID, msg)
                                alert_cooldowns[f"{symbol}_last"] = time.time()
                                
                except Exception as e: 
                    logging.error(f"Scanner Error for {symbol}: {e}")
            
            time.sleep(60)
        else: 
            time.sleep(5)

# --- Telegram UI ---
def get_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("📊 Status"), KeyboardButton("📋 Watchlist"))
    return kb

@bot.message_handler(commands=['start'])
def start(m):
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(m.chat.id, "Engine Online.", reply_markup=get_kb())

@bot.message_handler(commands=['add'])
def add(m):
    if str(m.chat.id) != CHAT_ID: return
    syms = re.findall(r'[A-Z0-9=]{3,10}', m.text.upper())
    for s in syms:
        if s == "ADD": continue
        ticker = yf.Ticker(s)
        if len(ticker.history(period="1d")) > 0:
            with data_lock:
                if s not in STRATEGY_PAIRS: STRATEGY_PAIRS.append(s)
    bot.reply_to(m, f"✅ Updated Watchlist: {', '.join(STRATEGY_PAIRS)}")

@bot.message_handler(commands=['remove'])
def remove(m):
    if str(m.chat.id) != CHAT_ID: return
    syms = re.findall(r'[A-Z0-9=]{3,10}', m.text.upper())
    with data_lock:
        for s in syms:
            if s in STRATEGY_PAIRS: STRATEGY_PAIRS.remove(s)
    bot.reply_to(m, f"🗑️ Removed: {', '.join(STRATEGY_PAIRS)}")

@bot.message_handler(func=lambda m: m.text in ["📊 Status", "📋 Watchlist"])
def handle_buttons(m):
    if m.text == "📊 Status": bot.reply_to(m, f"State: {'🟢 RUNNING' if IS_RUNNING else '🛑 PAUSED'}")
    else: bot.reply_to(m, f"Watchlist: {', '.join(STRATEGY_PAIRS)}")
    # Start web server on Render's assigned port (or 8080 locally)
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()
    
    print(f"Bot and Web Server starting on port {port}...")
    
    # Telegram polling
    last_update_id = 0
    while True:
        try:
            updates = bot.get_updates(offset=last_update_id + 1, timeout=30, limit=100)
            for update in updates:
                last_update_id = update.update_id
                bot.process_new_updates([update])
        except Exception as e:
            print(f"Polling error: {e}. Retrying...")
            time.sleep(5)
