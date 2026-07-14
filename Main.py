import os, re, time, threading, logging, telebot, yfinance as yf
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

# --- Configuration: Use Render Environment Variables ---
# In Render, go to: Settings -> Environment -> Add
# Add TELEGRAM_TOKEN and CHAT_ID here, do not hardcode them!
TELEGRAM_TOKEN = "8686769653:AAFHxNO5l8Oe6_QIQiY1vqXKwaFeUDywFTE"
CHAT_ID="8701685996"
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown")

# Global State
data_lock = threading.Lock()
STRATEGY_PAIRS = ["EURUSD=X"]
IS_RUNNING = True
alert_cooldowns = {}

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
            with data_lock: current_pairs = list(STRATEGY_PAIRS)
            for symbol in current_pairs:
                try:
                    df = yf.Ticker(symbol).history(period="1d", interval="1m")
                    if len(df) < 50: continue
                    m, s, h, rsi, vel = calculate_strategy(df)
                    
                    is_compressed = abs(h.iloc[-1]) < 0.0005
                    is_bull = (m.iloc[-2] <= s.iloc[-2]) and (m.iloc[-1] > s.iloc[-1])
                    is_bear = (m.iloc[-2] >= s.iloc[-2]) and (m.iloc[-1] < s.iloc[-1])
                   # --- CHANGE THIS ---
if not is_compressed:
    # Remove "or m.iloc[-1] > s.iloc[-1]" and "or m.iloc[-1] < s.iloc[-1]"
    if is_bull and (30 <= rsi.iloc[-1] <= 45) and vel.iloc[-1] > 0:
        if time.time() - alert_cooldowns.get(f"{symbol}_buy", 0) > 300:
            bot.send_message(CHAT_ID, f"🟢 *BUY* {symbol}\nRSI: {rsi.iloc[-1]:.2f} | Vel: {vel.iloc[-1]:.2f}")
            alert_cooldowns[f"{symbol}_buy"] = time.time()
            
    elif is_bear and (55 <= rsi.iloc[-1] <= 70) and vel.iloc[-1] < 0:
        if time.time() - alert_cooldowns.get(f"{symbol}_sell", 0) > 300:
            bot.send_message(CHAT_ID, f"🔴 *SELL* {symbol}\nRSI: {rsi.iloc[-1]:.2f} | Vel: {vel.iloc[-1]:.2f}")
            alert_cooldowns[f"{symbol}_sell"] = time.time()
 
                    
                except Exception as e: logging.error(f"Scanner Error: {e}")
            time.sleep(60)
        else: time.sleep(5)

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

if __name__ == "__main__":
    # Start the background scanner thread
    threading.Thread(target=scanner_engine, daemon=True).start()
    
    print("Bot is starting with manual 30s polling...")
    
    last_update_id = 0
    while True:
        try:
            # Manually fetch updates from Telegram
            # timeout=30 sets the long-polling duration to 30 seconds
            updates = bot.get_updates(
                offset=last_update_id + 1, 
                timeout=30, 
                limit=100
            )
            
            for update in updates:
                last_update_id = update.update_id
                # Process the message through your existing handlers
                bot.process_new_updates([update])
                
        except Exception as e:
            # Handle network drops or API errors gracefully
            print(f"Polling error: {e}. Retrying in 5 seconds...")
            time.sleep(5)

