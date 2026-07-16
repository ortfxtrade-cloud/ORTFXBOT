import os
import re
import time
import random
import threading
import logging
import telebot
import yfinance as yf
from flask import Flask
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# --- Configuration ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8686769653:AAHtda3UTFMxsW9MgnD-TTCa7qOKo7ypBms")
CHAT_ID = os.environ.get("CHAT_ID", "8701685996")
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# Flask setup to satisfy Render's Web Service requirement
app = Flask(__name__)

# Global State
data_lock = threading.Lock()
STRATEGY_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "NZDUSD=X", "USDCAD=X", "EURGBP=X", "EURJPY=X", "EURCHF=X",
    "EURAUD=X", "EURNZD=X", "EURCAD=X", "GBPJPY=X", "GBPCHF=X",
    "GBPAUD=X", "GBPNZD=X", "GBPCAD=X", "CHFJPY=X", "CADJPY=X",
    "AUDJPY=X", "NZDJPY=X", "AUDNZD=X", "AUDCAD=X", "AUDCHF=X",
    "NZDCAD=X", "NZDCHF=X", "CADCHF=X",
]
IS_RUNNING = True
alert_cooldowns = {}

logging.basicConfig(level=logging.INFO)

@app.route('/')
def health_check():
    return "Bot is running!"

# --- Inline Keyboards ---
def get_main_menu():
    """Main inline menu"""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Status", callback_data="status"),
        InlineKeyboardButton("📋 Watchlist", callback_data="watchlist"),
        InlineKeyboardButton("▶️ Start Scanner", callback_data="start_scanner"),
        InlineKeyboardButton("⏸️ Pause Scanner", callback_data="pause_scanner"),
        InlineKeyboardButton("➕ Add Pair", callback_data="add_menu"),
        InlineKeyboardButton("➖ Remove Pair", callback_data="remove_menu"),
        InlineKeyboardButton("📈 Quick Scan", callback_data="quick_scan"),
        InlineKeyboardButton("ℹ️ Help", callback_data="help"),
    )
    return kb

def get_pairs_keyboard(pairs, action="info", page=0, per_page=10):
    """Paginated pairs keyboard for watchlist/remove"""
    kb = InlineKeyboardMarkup(row_width=2)
    
    # Paginate
    total_pages = (len(pairs) + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    page_pairs = pairs[start:end]
    
    # Add pair buttons
    for pair in page_pairs:
        if action == "info":
            kb.add(InlineKeyboardButton(f"📌 {pair}", callback_data=f"info_{pair}"))
        elif action == "remove":
            kb.add(InlineKeyboardButton(f"❌ {pair}", callback_data=f"remove_{pair}"))
    
    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{action}_{page-1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{action}_{page+1}"))
    if nav_row:
        kb.add(*nav_row)
    
    kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
    return kb

def get_add_suggestions():
    """Suggest popular pairs to add"""
    kb = InlineKeyboardMarkup(row_width=3)
    popular = [
        "EURUSD=X", "GBPUSD=X", "USDJPY=X", "BTC-USD", "ETH-USD",
        "GC=F", "CL=F", "^GSPC", "^IXIC"
    ]
    buttons = [InlineKeyboardButton(p, callback_data=f"add_{p}") for p in popular]
    kb.add(*buttons)
    kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
    return kb

# --- Alert with Inline Buttons ---
def send_alert(direction, symbol, macd_val, signal_val, diff_val, rsi_val):
    icon = "🟢" if direction == "BUY" else "🔴"
    msg = (
        f"{icon} *{direction}* {symbol}\n"
        f"MACD Cross: Confirmed (5m)\n"
        f"MACD: {macd_val:.5f} | Signal: {signal_val:.5f}\n"
        f"Diff: {diff_val:.5f}\n"
        f"RSI: {rsi_val:.2f}\n"
        f"Status: SAFE (Active Liquidity)"
    )
    
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"📊 Quick Scan {symbol}", callback_data=f"quick_{symbol}"),
        InlineKeyboardButton("🔕 Mute 30min", callback_data=f"mute_{symbol}"),
        InlineKeyboardButton("❌ Remove Pair", callback_data=f"remove_{symbol}"),
    )
    
    try:
        bot.send_message(CHAT_ID, msg, reply_markup=kb, parse_mode="Markdown")
    except:
        bot.send_message(CHAT_ID, msg, reply_markup=kb, parse_mode=None)

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
    loss = loss.replace(0, 1e-10)
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return macd, signal, hist, rsi

def quick_scan_single(symbol):
    """Quick scan for inline button response"""
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m")
        if len(df) < 50:
            return None, "Insufficient data"
        
        m, s, h, rsi = calculate_strategy(df)
        recent_volatility = abs(h.tail(20)).mean()
        is_compressed = abs(h.iloc[-1]) < (recent_volatility * 0.4)
        
        is_bull = (m.iloc[-2] <= s.iloc[-2]) and (m.iloc[-1] > s.iloc[-1])
        is_bear = (m.iloc[-2] >= s.iloc[-2]) and (m.iloc[-1] < s.iloc[-1])
        
        result = {
            "symbol": symbol,
            "macd": m.iloc[-1],
            "signal": s.iloc[-1],
            "diff": m.iloc[-1] - s.iloc[-1],
            "rsi": rsi.iloc[-1],
            "is_compressed": is_compressed,
            "is_bull": is_bull,
            "is_bear": is_bear,
        }
        
        if not is_compressed:
            if is_bull and 30 <= rsi.iloc[-1] <= 45:
                return "BUY", result
            elif is_bear and 55 <= rsi.iloc[-1] <= 70:
                return "SELL", result
        
        return "NEUTRAL", result
    except Exception as e:
        return None, str(e)

def scanner_engine():
    global alert_cooldowns
    while True:
        if IS_RUNNING:
            with data_lock:
                current_pairs = list(STRATEGY_PAIRS)

            random.shuffle(current_pairs)

            for symbol in current_pairs:
                try:
                    df = yf.Ticker(symbol).history(period="5d", interval="5m")
                    if len(df) < 50:
                        continue

                    m, s, h, rsi = calculate_strategy(df)

                    recent_volatility = abs(h.tail(20)).mean()
                    is_compressed = abs(h.iloc[-1]) < (recent_volatility * 0.4)

                    is_bull = (m.iloc[-2] <= s.iloc[-2]) and (m.iloc[-1] > s.iloc[-1])
                    is_bear = (m.iloc[-2] >= s.iloc[-2]) and (m.iloc[-1] < s.iloc[-1])

                    if not is_compressed:
                        if (is_bull and 30 <= rsi.iloc[-1] <= 45) or \
                           (is_bear and 55 <= rsi.iloc[-1] <= 70):
                            if time.time() - alert_cooldowns.get(symbol, 0) > 300:
                                diff = m.iloc[-1] - s.iloc[-1]
                                direction = "BUY" if is_bull else "SELL"
                                send_alert(direction, symbol, m.iloc[-1], s.iloc[-1], diff, rsi.iloc[-1])
                                alert_cooldowns[symbol] = time.time()

                    time.sleep(1)

                except Exception as e:
                    logging.error(f"Scanner Error for {symbol}: {e}")
                    time.sleep(2)

            time.sleep(60)
        else:
            time.sleep(5)

# --- Callback Handlers ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if str(call.message.chat.id) != CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return
    
    data = call.data
    
    try:
        # Main Menu
        if data == "main_menu":
            bot.edit_message_text(
                "📋 *Main Menu*",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=get_main_menu(),
                parse_mode="Markdown"
            )
        
        # Status
        elif data == "status":
            status_text = f"🟢 Scanner: {'RUNNING' if IS_RUNNING else 'PAUSED'}\n📊 Pairs: {len(STRATEGY_PAIRS)}"
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(status_text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        
        # Watchlist (paginated)
        elif data == "watchlist" or data.startswith("page_info_"):
            page = int(data.split("_")[-1]) if data.startswith("page_info_") else 0
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
            kb = get_pairs_keyboard(pairs, "info", page)
            bot.edit_message_text(f"📋 *Watchlist ({len(pairs)} pairs)*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
        
        # Remove menu (paginated)
        elif data == "remove_menu" or data.startswith("page_remove_"):
            page = int(data.split("_")[-1]) if data.startswith("page_remove_") else 0
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
            if not pairs:
                bot.edit_message_text("📭 No pairs to remove.", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())
            else:
                kb = get_pairs_keyboard(pairs, "remove", page)
                bot.edit_message_text("❌ *Select pair to remove:*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
        
        # Add menu
        elif data == "add_menu":
            bot.edit_message_text("➕ *Add a pair:*\nSend /add SYMBOL or choose below:", call.message.chat.id, call.message.message_id, reply_markup=get_add_suggestions(), parse_mode="Markdown")
        
        # Add specific pair
        elif data.startswith("add_"):
            pair = data.replace("add_", "")
            ticker = yf.Ticker(pair)
            if len(ticker.history(period="1d")) > 0:
                with data_lock:
                    if pair not in STRATEGY_PAIRS:
                        STRATEGY_PAIRS.append(pair)
                bot.answer_callback_query(call.id, f"✅ {pair} added!")
            else:
                bot.answer_callback_query(call.id, f"❌ {pair} not found", show_alert=True)
        
        # Remove specific pair
        elif data.startswith("remove_"):
            pair = data.replace("remove_", "")
            with data_lock:
                if pair in STRATEGY_PAIRS:
                    STRATEGY_PAIRS.remove(pair)
            bot.answer_callback_query(call.id, f"🗑️ {pair} removed!")
            # Refresh remove menu
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
            kb = get_pairs_keyboard(pairs, "remove", 0) if pairs else None
            if kb:
                bot.edit_message_text("❌ *Select pair to remove:*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            else:
                bot.edit_message_text("📭 No pairs left.", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())
        (call.id, "⏸️ Scanner paused!")
        # Start scanner
       elif data == "start_scanner":
            STATE["running"] = True
             bot.answer_callback_query(call.id, "✅ Scanner started!")

         # Pause scanner
       elif data == "pause_scanner":
            STATE["running"] = False
            bot.answer_callback_query(call.id, "⏸️ Scanner paused!")

        # Quick scan
        elif data == "quick_scan":
            with data_lock:
                pairs = list(STRATEGY_PAIRS[:5])  # First 5 pairs
            bot.edit_message_text("🔍 *Quick scanning...*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
            results = []
            for pair in pairs:
                direction, result = quick_scan_single(pair)
                if direction and direction != "NEUTRAL":
                    icon = "🟢" if direction == "BUY" else "🔴"
                    r = result if isinstance(result, dict) else None
                    if r:
                        results.append(f"{icon} {pair}: {direction} (RSI: {r['rsi']:.1f})")
            msg = "📊 *Quick Scan Results:*\n" + ("\n".join(results) if results else "No signals found.")
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="quick_scan"), InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
        
        # Quick scan single from alert
        elif data.startswith("quick_"):
            pair = data.replace("quick_", "")
            bot.edit_message_text(f"🔍 *Scanning {pair}...*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
            direction, result = quick_scan_single(pair)
            if isinstance(result, dict):
                icon = {"BUY": "🟢", "SELL": "🔴"}.get(direction, "⚪")
                msg = (
                    f"{icon} *{pair}*\n"
                    f"MACD: {result['macd']:.5f}\n"
                    f"Signal: {result['signal']:.5f}\n"
                    f"RSI: {result['rsi']:.2f}\n"
                    f"Compressed: {'Yes' if result['is_compressed'] else 'No'}\n"
                    f"Signal: {direction}"
                )
            else:
                msg = f"❌ Error: {result}"
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Rescan", callback_data=f"quick_{pair}"), InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
        
        # Mute pair
        elif data.startswith("mute_"):
            pair = data.replace("mute_", "")
            alert_cooldowns[pair] = time.time() + 1800  # Mute for 30 min
            bot.answer_callback_query(call.id, f"🔕 {pair} muted for 30 min")
        
        # Info on specific pair
        elif data.startswith("info_"):
            pair = data.replace("info_", "")
            bot.edit_message_text(f"🔍 *Scanning {pair}...*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
            direction, result = quick_scan_single(pair)
            if isinstance(result, dict):
                icon = {"BUY": "🟢", "SELL": "🔴"}.get(direction, "⚪")
                msg = (
                    f"{icon} *{pair}*\n"
                    f"MACD: {result['macd']:.5f}\n"
                    f"Signal: {result['signal']:.5f}\n"
                    f"Diff: {result['diff']:.5f}\n"
                    f"RSI: {result['rsi']:.2f}\n"
                    f"Compressed: {'Yes' if result['is_compressed'] else 'No'}\n"
                    f"Signal: {direction}"
                )
            else:
                msg = f"❌ Error: {result}"
            kb = InlineKeyboardMarkup()
            kb.add(
                InlineKeyboardButton("🔄 Refresh", callback_data=f"info_{pair}"),
                InlineKeyboardButton("🔙 Watchlist", callback_data="watchlist"),
            )
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
        
        # Help
        elif data == "help":
            help_text = (
                "🤖 *Forex Scanner Bot*\n\n"
                "*Commands:*\n"
                "/start - Launch bot\n"
                "/add SYMBOL - Add pair\n"
                "/remove SYMBOL - Remove pair\n\n"
                "*Strategy:* MACD Crossover + RSI Filter (5m timeframe)\n"
                "*BUY:* MACD bull cross + RSI 30-45\n"
                "*SELL:* MACD bear cross + RSI 55-70"
            )
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
        
        else:
            bot.answer_callback_query(call.id, "Unknown action")
    
    except Exception as e:
        logging.error(f"Callback error: {e}")
        bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)

# --- Command Handlers ---
def get_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("📊 Status"), KeyboardButton("📋 Watchlist"))
    return kb

@bot.message_handler(commands=['start'])
def start(m):
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(
            m.chat.id,
            "🚀 *Forex Scanner Online*\n\nSelect an option:",
            reply_markup=get_main_menu(),
            parse_mode="Markdown"
        )

@bot.message_handler(commands=['menu'])
def menu_cmd(m):
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(m.chat.id, "📋 *Main Menu*", reply_markup=get_main_menu(), parse_mode="Markdown")

@bot.message_handler(commands=['add'])
def add(m):
    if str(m.chat.id) != CHAT_ID:
        return
    syms = re.findall(r'[A-Z0-9=X]{3,10}', m.text.upper())
    added = []
    for s in syms:
        if s == "ADD":
            continue
        ticker = yf.Ticker(s)
        if len(ticker.history(period="1d")) > 0:
            with data_lock:
                if s not in STRATEGY_PAIRS:
                    STRATEGY_PAIRS.append(s)
                    added.append(s)
    if added:
        bot.reply_to(m, f"✅ Added: {', '.join(added)}", reply_markup=get_main_menu())
    else:
        bot.reply_to(m, "❌ No valid symbols found.", reply_markup=get_main_menu())

@bot.message_handler(commands=['remove'])
def remove(m):
    if str(m.chat.id) != CHAT_ID:
        return
    syms = re.findall(r'[A-Z0-9=X]{3,10}', m.text.upper())
    removed = []
    with data_lock:
        for s in syms:
            if s in STRATEGY_PAIRS:
                STRATEGY_PAIRS.remove(s)
                removed.append(s)
    if removed:
        bot.reply_to(m, f"🗑️ Removed: {', '.join(removed)}", reply_markup=get_main_menu())
    else:
        bot.reply_to(m, "❌ No matching pairs found.", reply_markup=get_main_menu())

@bot.message_handler(func=lambda m: m.text in ["📊 Status", "📋 Watchlist"])
def handle_buttons(m):
    if m.text == "📊 Status":
        bot.reply_to(m, f"🟢 Scanner: {'RUNNING' if IS_RUNNING else 'PAUSED'}\n📊 Pairs: {len(STRATEGY_PAIRS)}", reply_markup=get_main_menu())
    else:
        with data_lock:
            pairs = list(STRATEGY_PAIRS)
        bot.reply_to(m, f"📋 Watchlist ({len(pairs)}):\n" + "\n".join(pairs), reply_markup=get_main_menu())

# --- Main Entry ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    # Start scanner in daemon thread
    threading.Thread(target=scanner_engine, daemon=True).start()

    # Start bot polling in daemon thread
    threading.Thread(target=bot.infinity_polling, daemon=True).start()

    print(f"Bot and Web Server starting on port {port}...")
    app.run(host="0.0.0.0", port=port)
