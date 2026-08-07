import os
import re
import time
import random
import threading
import logging
import csv
import requests
import telebot
import yfinance as yf
from flask import Flask
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# --- Configuration ---
TELEGRAM_TOKEN = "8686769653:AAEUvYlVgCAv9Rn1jL82aNl6wTxk1-w7g3Q"
CHAT_ID = "8701685996"
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

app = Flask(__name__)

# --- Global State ---
data_lock = threading.Lock()
STRATEGY_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X", "AUDUSD=X",
    "NZDUSD=X", "USDCAD=X", "EURGBP=X", "EURJPY=X", "EURCHF=X",
    "EURAUD=X", "EURNZD=X", "EURCAD=X", "GBPJPY=X", "GBPCHF=X",
    "GBPAUD=X", "GBPNZD=X", "GBPCAD=X", "CHFJPY=X", "CADJPY=X",
    "AUDJPY=X", "NZDJPY=X", "AUDNZD=X", "AUDCAD=X", "AUDCHF=X",
    "NZDCAD=X", "NZDCHF=X", "CADCHF=X",
]
STATE = {"running": True}
alert_cooldowns = {}
spread_blocked = {}

# Learning
signal_log = []
pair_loss_streak = {}
pair_gap_multiplier = {}
pair_entry_stats = {}
pending_feedback = {}
full_signal_messages = {}
loss_interview_state = {}

# Chat / Debug mode
chat_mode = {}

# --- Default settings & overrides ---
DEFAULT_RSI_BUY_MIN = 30
DEFAULT_RSI_BUY_MAX = 40
DEFAULT_RSI_SELL_MIN = 50
DEFAULT_RSI_SELL_MAX = 70
DEFAULT_GAP_MULTIPLIER = 0.15

# Per-pair overrides: {symbol: {"rsi_buy_min":..., "rsi_buy_max":..., "rsi_sell_min":..., "rsi_sell_max":..., "gap_multiplier":...}}
pair_settings = {}

# State for settings flow
settings_state = {}   # {chat_id: {"step": "choose_pair" or "set_value", "target": "all"/symbol, "param": "rsi_buy_min" etc.}}

logging.basicConfig(level=logging.INFO)

@app.route('/')
def health_check():
    return "Bot is running!"

# --- Helper: get effective settings for a pair ---
def get_effective_settings(symbol):
    """Return dict with rsi_buy_min, rsi_buy_max, rsi_sell_min, rsi_sell_max, gap_multiplier"""
    if symbol in pair_settings:
        return {
            "rsi_buy_min": pair_settings[symbol].get("rsi_buy_min", DEFAULT_RSI_BUY_MIN),
            "rsi_buy_max": pair_settings[symbol].get("rsi_buy_max", DEFAULT_RSI_BUY_MAX),
            "rsi_sell_min": pair_settings[symbol].get("rsi_sell_min", DEFAULT_RSI_SELL_MIN),
            "rsi_sell_max": pair_settings[symbol].get("rsi_sell_max", DEFAULT_RSI_SELL_MAX),
            "gap_multiplier": pair_settings[symbol].get("gap_multiplier", DEFAULT_GAP_MULTIPLIER)
        }
    else:
        return {
            "rsi_buy_min": DEFAULT_RSI_BUY_MIN,
            "rsi_buy_max": DEFAULT_RSI_BUY_MAX,
            "rsi_sell_min": DEFAULT_RSI_SELL_MIN,
            "rsi_sell_max": DEFAULT_RSI_SELL_MAX,
            "gap_multiplier": DEFAULT_GAP_MULTIPLIER
        }

# --- Inline Keyboards ---
def get_main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Status", callback_data="status"),
        InlineKeyboardButton("📋 Watchlist", callback_data="watchlist"),
        InlineKeyboardButton("🚫 Blocked", callback_data="blocked_list"),
        InlineKeyboardButton("📈 Stats", callback_data="stats_page"),
        InlineKeyboardButton("⏱️ Entry", callback_data="entry_menu"),
        InlineKeyboardButton("💬 Chat", callback_data="chat_start"),
        InlineKeyboardButton("🐛 Debug", callback_data="debug_start"),
        InlineKeyboardButton("🧪 Test AI", callback_data="test_ai"),
        InlineKeyboardButton("⚙️ Settings", callback_data="settings_menu"),
        InlineKeyboardButton("▶️ Start Scanner", callback_data="start_scanner"),
        InlineKeyboardButton("⏸️ Pause Scanner", callback_data="pause_scanner"),
        InlineKeyboardButton("➕ Add Pair", callback_data="add_menu"),
        InlineKeyboardButton("➖ Remove Pair", callback_data="remove_menu"),
        InlineKeyboardButton("📈 Quick Scan", callback_data="quick_scan"),
        InlineKeyboardButton("ℹ️ Help", callback_data="help"),
    )
    return kb

def get_pairs_keyboard(pairs, action="info", page=0, per_page=10):
    kb = InlineKeyboardMarkup(row_width=2)
    total_pages = (len(pairs) + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    page_pairs = pairs[start:end]
    for pair in page_pairs:
        if action == "info":
            kb.add(InlineKeyboardButton(f"📌 {pair}", callback_data=f"info_{pair}"))
        elif action == "remove":
            kb.add(InlineKeyboardButton(f"❌ {pair}", callback_data=f"remove_{pair}"))
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
    kb = InlineKeyboardMarkup(row_width=3)
    popular = [
        "EURUSD=X", "GBPUSD=X", "USDJPY=X", "BTC-USD", "ETH-USD",
        "GC=F", "CL=F", "^GSPC", "^IXIC"
    ]
    buttons = [InlineKeyboardButton(p, callback_data=f"add_{p}") for p in popular]
    kb.add(*buttons)
    kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
    return kb

# --- Spread Detection (unchanged) ---
def is_spread_present(symbol):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval="5m")
        if len(df) < 10: return False
        last_candles = df.tail(5)
        high_low_range = last_candles['High'] - last_candles['Low']
        avg_range = high_low_range.mean()
        avg_price = last_candles['Close'].mean()
        body_size = abs(last_candles['Close'] - last_candles['Open'])
        avg_body = body_size.mean()
        condition1 = avg_price > 0 and avg_range < avg_price * 0.0002
        condition2 = avg_price > 0 and avg_body < avg_price * 0.00005
        condition3 = df.iloc[-1]['High'] == df.iloc[-1]['Low']
        if sum([condition1, condition2, condition3]) >= 2: return True
        return False
    except Exception as e:
        logging.error(f"Spread check error {symbol}: {e}")
        return False

# --- Strategy (use effective settings) ---
def calculate_strategy(df):
    fast_ema = df['Close'].ewm(span=12, adjust=False).mean()
    slow_ema = df['Close'].ewm(span=26, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    delta = df['Close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return macd, signal, hist, rsi

def quick_scan_single(symbol):
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m")
        if len(df) < 50: return None, "Insufficient data"
        m, s, h, rsi = calculate_strategy(df)
        prev_diff = m.iloc[-1] - s.iloc[-1]
        prev_diff_before = m.iloc[-2] - s.iloc[-2]
        df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
        m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)
        diff_1m_now = m_1m.iloc[-1] - s_1m.iloc[-1]
        recent_1m_diffs = abs(m_1m.tail(20) - s_1m.tail(20))
        avg_1m_diff = recent_1m_diffs.mean()

        # Use effective settings
        settings = get_effective_settings(symbol)
        multiplier = settings["gap_multiplier"]
        # Adaptive multiplier overrides the setting if the bot has learned a higher value due to losses
        if symbol in pair_gap_multiplier:
            multiplier = max(multiplier, pair_gap_multiplier[symbol])

        min_gap = max(avg_1m_diff * multiplier, 0.0000001)
        is_1m_strong = diff_1m_now > min_gap if diff_1m_now > 0 else diff_1m_now < -min_gap
        is_bull = (prev_diff_before < 0) and (prev_diff > 0) and is_1m_strong
        is_bear = (prev_diff_before > 0) and (prev_diff < 0) and is_1m_strong

        rsi_val = rsi.iloc[-1]
        if is_bull and settings["rsi_buy_min"] <= rsi_val <= settings["rsi_buy_max"]:
            return "BUY", {"symbol": symbol, "macd_5m": m.iloc[-1], "signal_5m": s.iloc[-1],
                           "diff_5m": prev_diff, "macd_1m": m_1m.iloc[-1], "signal_1m": s_1m.iloc[-1],
                           "diff_1m": diff_1m_now, "rsi_5m": rsi_val, "min_gap": min_gap}
        elif is_bear and settings["rsi_sell_min"] <= rsi_val <= settings["rsi_sell_max"]:
            return "SELL", {"symbol": symbol, "macd_5m": m.iloc[-1], "signal_5m": s.iloc[-1],
                           "diff_5m": prev_diff, "macd_1m": m_1m.iloc[-1], "signal_1m": s_1m.iloc[-1],
                           "diff_1m": diff_1m_now, "rsi_5m": rsi_val, "min_gap": min_gap}
        return "NEUTRAL", None
    except Exception as e:
        return None, str(e)

# --- Scanner Engine (uses effective settings) ---
def scanner_engine():
    global alert_cooldowns, spread_blocked, signal_log, pair_loss_streak, pair_gap_multiplier, full_signal_messages
    while True:
        if STATE["running"]:
            with data_lock:
                current_pairs = list(STRATEGY_PAIRS)
            random.shuffle(current_pairs)
            for symbol in current_pairs:
                try:
                    if symbol in spread_blocked:
                        if time.time() < spread_blocked[symbol]:
                            continue
                        else:
                            del spread_blocked[symbol]
                    if is_spread_present(symbol):
                        if symbol not in spread_blocked:
                            spread_blocked[symbol] = time.time() + 3600
                        continue

                    df = yf.Ticker(symbol).history(period="5d", interval="5m")
                    if len(df) < 50: continue
                    m, s, h, rsi = calculate_strategy(df)
                    prev_diff = m.iloc[-1] - s.iloc[-1]
                    prev_diff_before = m.iloc[-2] - s.iloc[-2]

                    df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
                    if len(df_1m) < 30: continue
                    m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)
                    diff_1m_now = m_1m.iloc[-1] - s_1m.iloc[-1]
                    recent_1m_diffs = abs(m_1m.tail(20) - s_1m.tail(20))
                    avg_1m_diff = recent_1m_diffs.mean()

                    settings = get_effective_settings(symbol)
                    multiplier = settings["gap_multiplier"]
                    if symbol in pair_gap_multiplier:
                        multiplier = max(multiplier, pair_gap_multiplier[symbol])

                    min_gap = max(avg_1m_diff * multiplier, 0.0000001)
                    is_1m_bullish = diff_1m_now > min_gap
                    is_1m_bearish = diff_1m_now < -min_gap

                    rsi_val = rsi.iloc[-1]
                    confirm_bull = (prev_diff_before < 0) and (prev_diff > 0) and (abs(prev_diff) >= 0.00001) and is_1m_bullish and (settings["rsi_buy_min"] <= rsi_val <= settings["rsi_buy_max"])
                    confirm_bear = (prev_diff_before > 0) and (prev_diff < 0) and (abs(prev_diff) >= 0.00001) and is_1m_bearish and (settings["rsi_sell_min"] <= rsi_val <= settings["rsi_sell_max"])

                    if confirm_bull or confirm_bear:
                        if time.time() - alert_cooldowns.get(symbol, 0) > 300:
                            direction = "BUY" if confirm_bull else "SELL"
                            icon = "🟢" if confirm_bull else "🔴"

                            short_msg = f"{icon} *{direction}* {symbol}\n\n✅ Confirmed"
                            full_msg = (
                                f"{icon} *{direction}* {symbol}\n\n"
                                f"✅ MACD Cross + 1m Gap Confirmed\n\n"
                                f"5m MACD: {m.iloc[-1]:.5f} | Signal: {s.iloc[-1]:.5f}\n"
                                f"5m Diff: {prev_diff:.5f}\n"
                                f"1m MACD: {m_1m.iloc[-1]:.5f} | Signal: {s_1m.iloc[-1]:.5f}\n"
                                f"1m Diff: {diff_1m_now:.5f}\n"
                                f"1m Min Gap: {min_gap:.8f}\n"
                                f"5m RSI: {rsi_val:.2f}\n\n"
                                f"Status: SAFE (Active Liquidity)"
                            )

                            kb = InlineKeyboardMarkup(row_width=2)
                            kb.add(
                                InlineKeyboardButton("✅ WIN", callback_data=f"win_"),
                                InlineKeyboardButton("❌ LOSS", callback_data=f"loss_interview_"),
                                InlineKeyboardButton("💬 Feedback", callback_data=f"fbdetails_"),
                                InlineKeyboardButton("🔍 More Details", callback_data=f"showdetails_"),
                                InlineKeyboardButton("📊 Quick Scan", callback_data=f"quick_{symbol}"),
                                InlineKeyboardButton("🔕 Mute 30min", callback_data=f"mute_{symbol}"),
                                InlineKeyboardButton("❌ Remove Pair", callback_data=f"remove_{symbol}"),
                            )
                            try:
                                sent_msg = bot.send_message(CHAT_ID, short_msg, reply_markup=kb, parse_mode="Markdown")
                                full_signal_messages[sent_msg.message_id] = full_msg
                                log_entry = {
                                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "symbol": symbol,
                                    "direction": direction,
                                    "macd_5m": m.iloc[-1],
                                    "signal_5m": s.iloc[-1],
                                    "diff_5m": prev_diff,
                                    "macd_1m": m_1m.iloc[-1],
                                    "signal_1m": s_1m.iloc[-1],
                                    "diff_1m": diff_1m_now,
                                    "min_gap": min_gap,
                                    "rsi_5m": rsi_val,
                                    "msg_id": sent_msg.message_id,
                                    "entry_delay": "",
                                    "loss_reason": "",
                                    "loss_notes": "",
                                    "analysis_result": "",
                                    "result": ""
                                }
                                signal_log.append(log_entry)
                            except Exception as e:
                                logging.error(f"Failed to send/log: {e}")
                            alert_cooldowns[symbol] = time.time()
                    time.sleep(1)
                except Exception as e:
                    logging.error(f"Scanner Error {symbol}: {e}")
                    time.sleep(2)
            time.sleep(30)
        else:
            time.sleep(5)

# --- Feedback helper (unchanged) ---
def record_feedback(msg_id, result, delay_sec=0, reason="", notes="", analysis=""):
    global pair_loss_streak, pair_gap_multiplier, pair_entry_stats
    for entry in signal_log:
        if entry.get("msg_id") == msg_id:
            entry["result"] = result
            entry["entry_delay"] = delay_sec
            entry["loss_reason"] = reason
            entry["loss_notes"] = notes
            entry["analysis_result"] = analysis
            symbol = entry["symbol"]

            if result == "LOSS":
                pair_loss_streak[symbol] = pair_loss_streak.get(symbol, 0) + 1
                if pair_loss_streak[symbol] >= 3:
                    old = pair_gap_multiplier.get(symbol, 0.15)
                    new = min(old * 1.5, 0.6)
                    pair_gap_multiplier[symbol] = new
                    return True, f"📉 {symbol} gap multiplier increased to {new:.2f}"
            else:
                pair_loss_streak[symbol] = 0
                if symbol in pair_gap_multiplier and pair_gap_multiplier[symbol] > 0.15:
                    new = max(0.15, pair_gap_multiplier[symbol] * 0.9)
                    pair_gap_multiplier[symbol] = new

            if symbol not in pair_entry_stats:
                pair_entry_stats[symbol] = {"wins": [], "losses": []}
            if result == "WIN":
                pair_entry_stats[symbol]["wins"].append(delay_sec)
            else:
                pair_entry_stats[symbol]["losses"].append(delay_sec)

            try:
                file_exists = os.path.isfile("signal_log.csv")
                with open("signal_log.csv", "a", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(entry.keys()))
                    if not file_exists:
                        writer.writeheader()
                    writer.writerow(entry)
            except Exception as e:
                logging.error(f"CSV write error: {e}")
            return True, None
    return False, None

# --- Gemini API helper (unchanged) ---
def ask_ai_core(question, system_prompt="You are a helpful trading assistant. Be concise."):
    api_key = "AQ.Ab8RN6KZuaQAZUgJ9IGiVCSz2JVIHG2LJ2YiR81h1cKrddkaCQ"
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        prompt = f"{system_prompt}\n\nUser: {question}"
        data = {"contents": [{"parts": [{"text": prompt}]}]}
        response = requests.post(url, headers=headers, json=data)
        result = response.json()
        if "candidates" in result and len(result["candidates"]) > 0:
            return result["candidates"][0]["content"]["parts"][0]["text"]
        else:
            return f"❌ Gemini error: {result}"
    except Exception as e:
        return f"❌ Error: {e}"

# --- Loss Interview Flow (unchanged) ---
def start_loss_interview(chat_id, msg_id):
    loss_interview_state[chat_id] = {"msg_id": msg_id, "step": "timing"}
    bot.send_message(chat_id, "⏱️ *When did you place the trade?*\nReply with something like: `immediately`, `30s`, `2m`, or a number in seconds.",
                     parse_mode="Markdown")

def process_loss_timing(message, msg_id):
    chat_id = message.chat.id
    if chat_id not in loss_interview_state:
        return
    timing_text = message.text.strip().lower()
    delay_sec = 0
    if timing_text in ["immediately", "instant", "0", "0s", "0m"]:
        delay_sec = 0
    elif timing_text.endswith("s") and timing_text[:-1].isdigit():
        delay_sec = int(timing_text[:-1])
    elif timing_text.endswith("m") and timing_text[:-1].isdigit():
        delay_sec = int(timing_text[:-1]) * 60
    elif timing_text.isdigit():
        delay_sec = int(timing_text)
    else:
        delay_sec = 0
    loss_interview_state[chat_id]["timing"] = delay_sec
    loss_interview_state[chat_id]["step"] = "reason"
    bot.send_message(chat_id, "📝 *What went wrong?*\nReply with a short reason like `macd compression on 1m`, `spread`, `reversed`, `news`, etc.",
                     parse_mode="Markdown")
    bot.register_next_step_handler(message, process_loss_reason, msg_id)

def process_loss_reason(message, msg_id):
    chat_id = message.chat.id
    reason = message.text.strip()
    loss_interview_state[chat_id]["reason"] = reason
    loss_interview_state[chat_id]["step"] = "notes"
    bot.send_message(chat_id, "🗒️ *Any additional notes?* (Reply or send `SKIP`)",
                     parse_mode="Markdown")
    bot.register_next_step_handler(message, process_loss_notes, msg_id)

def process_loss_notes(message, msg_id):
    chat_id = message.chat.id
    notes = message.text.strip()
    if notes.upper() == "SKIP":
        notes = ""
    state = loss_interview_state.pop(chat_id)
    timing = state["timing"]
    reason = state["reason"]

    signal_details = ""
    for entry in signal_log:
        if entry.get("msg_id") == msg_id:
            signal_details = (
                f"Signal: {entry['direction']} {entry['symbol']}\n"
                f"5m Diff: {entry['diff_5m']:.5f}, 1m Diff: {entry['diff_1m']:.5f}, "
                f"1m Min Gap: {entry['min_gap']:.8f}, RSI: {entry['rsi_5m']:.2f}"
            )
            break

    thinking_msg = bot.send_message(chat_id, "🤔 *Analyzing the loss with AI Core...*", parse_mode="Markdown")
    prompt = (
        f"Analyze this trade signal that resulted in a loss.\n"
        f"{signal_details}\n"
        f"User entered the trade {timing}s after the signal.\n"
        f"Reason given: {reason}\n"
        f"Additional notes: {notes if notes else 'None'}\n\n"
        "Determine if the bot's signal criteria were likely flawed. "
        "If yes, suggest specific parameter adjustments (e.g., increase min 1m gap, change RSI bounds). "
        "If the loss was likely due to external factors (spread, news, market noise), reply 'No bot error detected.' "
        "Be concise."
    )
    system_prompt = "You are a trading bot debugger. Analyze the trade and give actionable advice."
    analysis = ask_ai_core(prompt, system_prompt)

    success, extra = record_feedback(msg_id, "LOSS", timing, reason, notes, analysis)

    if success:
        response = f"📉 *Loss recorded* (delay {timing}s, reason: {reason})\n\n🧠 *AI Core Analysis:*\n{analysis}"
        if extra:
            response += f"\n\n{extra}"
    else:
        response = "❌ Could not find the original signal."

    bot.edit_message_text(response, chat_id, thinking_msg.message_id, parse_mode="Markdown")

# --- New Settings Handlers ---
@bot.callback_query_handler(func=lambda call: call.data == "settings_menu")
def settings_menu(call):
    if str(call.message.chat.id) != CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🌐 All Pairs (Global)", callback_data="set_global"))
    # Add each pair as a button
    with data_lock:
        pairs = list(STRATEGY_PAIRS)
    for pair in pairs[:10]:  # first 10 for simplicity; you can paginate
        kb.add(InlineKeyboardButton(pair, callback_data=f"setpair_{pair}"))
    kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
    bot.edit_message_text("⚙️ *Settings*\nChoose a target to configure:", call.message.chat.id, call.message.message_id,
                          reply_markup=kb, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_global") or call.data.startswith("setpair_"))
def settings_target_selected(call):
    if str(call.message.chat.id) != CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return
    if call.data == "set_global":
        target = "all"
        target_name = "All Pairs (Global)"
    else:
        target = call.data[8:]  # remove "setpair_"
        target_name = target

    # Store state for later
    settings_state[call.message.chat.id] = {"target": target}

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("📈 RSI Buy", callback_data="param_rsi_buy"))
    kb.add(InlineKeyboardButton("📉 RSI Sell", callback_data="param_rsi_sell"))
    kb.add(InlineKeyboardButton("📏 Gap Multiplier", callback_data="param_gap_mult"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="settings_menu"))
    bot.edit_message_text(f"⚙️ *Settings for {target_name}*\nSelect parameter to change:", call.message.chat.id,
                          call.message.message_id, reply_markup=kb, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("param_"))
def settings_param_selected(call):
    if str(call.message.chat.id) != CHAT_ID or call.message.chat.id not in settings_state:
        bot.answer_callback_query(call.id, "Unauthorized or no state")
        return
    param = call.data[6:]  # rsi_buy, rsi_sell, gap_mult
    state = settings_state[call.message.chat.id]
    target = state["target"]

    if param == "rsi_buy":
        prompt_text = f"Enter new RSI Buy range as `min-max` (e.g., 30-40). Current:\n"
        if target == "all":
            prompt_text += f"Global: {DEFAULT_RSI_BUY_MIN}-{DEFAULT_RSI_BUY_MAX}"
        else:
            settings = get_effective_settings(target)
            prompt_text += f"{target}: {settings['rsi_buy_min']}-{settings['rsi_buy_max']}"
        param_name = "rsi_buy"
    elif param == "rsi_sell":
        prompt_text = f"Enter new RSI Sell range as `min-max` (e.g., 50-70). Current:\n"
        if target == "all":
            prompt_text += f"Global: {DEFAULT_RSI_SELL_MIN}-{DEFAULT_RSI_SELL_MAX}"
        else:
            settings = get_effective_settings(target)
            prompt_text += f"{target}: {settings['rsi_sell_min']}-{settings['rsi_sell_max']}"
        param_name = "rsi_sell"
    else:  # gap_mult
        prompt_text = f"Enter new Gap Multiplier (e.g., 0.15). Current:\n"
        if target == "all":
            prompt_text += f"Global: {DEFAULT_GAP_MULTIPLIER}"
        else:
            settings = get_effective_settings(target)
            prompt_text += f"{target}: {settings['gap_multiplier']}"
        param_name = "gap_mult"

    # Store parameter in state
    state["param"] = param_name
    bot.edit_message_text(prompt_text, call.message.chat.id, call.message.message_id)
    # Ask for value via a new message
    msg = bot.send_message(call.message.chat.id, "Please reply with the new value:")
    bot.register_next_step_handler(msg, process_settings_value)
    bot.answer_callback_query(call.id)

def process_settings_value(message):
    chat_id = message.chat.id
    if chat_id not in settings_state:
        return
    state = settings_state[chat_id]
    target = state["target"]
    param = state.get("param")
    value = message.text.strip()

    try:
        if param in ("rsi_buy", "rsi_sell"):
            # Expect min-max format
            parts = value.split('-')
            if len(parts) != 2:
                raise ValueError("Format must be min-max")
            min_val = int(parts[0].strip())
            max_val = int(parts[1].strip())
            if min_val < 0 or min_val > 100 or max_val < 0 or max_val > 100 or min_val >= max_val:
                raise ValueError("Invalid range")
            if target == "all":
                if param == "rsi_buy":
                    global DEFAULT_RSI_BUY_MIN, DEFAULT_RSI_BUY_MAX
                    DEFAULT_RSI_BUY_MIN = min_val
                    DEFAULT_RSI_BUY_MAX = max_val
                else:
                    global DEFAULT_RSI_SELL_MIN, DEFAULT_RSI_SELL_MAX
                    DEFAULT_RSI_SELL_MIN = min_val
                    DEFAULT_RSI_SELL_MAX = max_val
                bot.reply_to(message, f"✅ Global {param} set to {min_val}-{max_val}")
            else:
                if target not in pair_settings:
                    pair_settings[target] = {}
                if param == "rsi_buy":
                    pair_settings[target]["rsi_buy_min"] = min_val
                    pair_settings[target]["rsi_buy_max"] = max_val
                else:
                    pair_settings[target]["rsi_sell_min"] = min_val
                    pair_settings[target]["rsi_sell_max"] = max_val
                bot.reply_to(message, f"✅ {target} {param} set to {min_val}-{max_val}")
        else:  # gap_mult
            mult = float(value)
            if mult <= 0 or mult > 2:
                raise ValueError("Multiplier must be >0 and ≤2")
            if target == "all":
                global DEFAULT_GAP_MULTIPLIER
                DEFAULT_GAP_MULTIPLIER = mult
                bot.reply_to(message, f"✅ Global gap multiplier set to {mult}")
            else:
                if target not in pair_settings:
                    pair_settings[target] = {}
                pair_settings[target]["gap_multiplier"] = mult
                bot.reply_to(message, f"✅ {target} gap multiplier set to {mult}")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid input: {e}. Please try again.")
        # Let them retry? For simplicity, just cancel state
        settings_state.pop(chat_id, None)
        return

    # Clear state
    settings_state.pop(chat_id, None)
    # Show main menu again
    bot.send_message(chat_id, "Settings updated.", reply_markup=get_main_menu())

# --- Callback Handlers (updated with settings handlers removed from generic) ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if str(call.message.chat.id) != CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return
    data = call.data
    msg_id = call.message.message_id
    try:
        if data.startswith("win_"):
            success, extra_msg = record_feedback(msg_id, "WIN", 0)
            if success:
                bot.answer_callback_query(call.id, "Recorded: WIN")
                try:
                    new_kb = InlineKeyboardMarkup(row_width=2)
                    for row in call.message.reply_markup.keyboard:
                        new_row = []
                        for btn in row:
                            if btn.callback_data.startswith("win_") or btn.callback_data.startswith("loss_interview_"):
                                if btn.callback_data.startswith("win_"):
                                    new_row.append(InlineKeyboardButton("✅ RECORDED", callback_data="none"))
                                else:
                                    new_row.append(btn)
                            else:
                                new_row.append(btn)
                        if new_row:
                            new_kb.add(*new_row)
                    bot.edit_message_reply_markup(call.message.chat.id, msg_id, reply_markup=new_kb)
                except:
                    pass
                if extra_msg:
                    bot.send_message(call.message.chat.id, extra_msg)
            else:
                bot.answer_callback_query(call.id, "Signal not found in log", show_alert=True)

        elif data.startswith("loss_interview_"):
            start_loss_interview(call.message.chat.id, msg_id)
            bot.answer_callback_query(call.id, "Answer the question below to record loss details.")

        elif data.startswith("fbdetails_"):
            pending_feedback[call.message.chat.id] = msg_id
            ask_msg = bot.send_message(
                call.message.chat.id,
                "📝 Reply with the result and optional details.\n"
                "Format: `WIN 2m reason: good entry` or `LOSS reason: spread`\n"
                "Reply with SKIP to cancel.",
                parse_mode="Markdown"
            )
            bot.register_next_step_handler(ask_msg, process_feedback_reply, msg_id)
            bot.answer_callback_query(call.id, "Reply with WIN/LOSS and details...")

        elif data.startswith("showdetails_") or data.startswith("hidedetails_"):
            full_msg = full_signal_messages.get(msg_id)
            if not full_msg:
                bot.answer_callback_query(call.id, "Details not available.")
                return
            if data.startswith("showdetails_"):
                new_kb = InlineKeyboardMarkup(row_width=2)
                for row in call.message.reply_markup.keyboard:
                    new_row = []
                    for btn in row:
                        if btn.callback_data.startswith("showdetails_"):
                            new_row.append(InlineKeyboardButton("🔍 Hide Details", callback_data="hidedetails_"))
                        elif btn.callback_data.startswith("hidedetails_"):
                            new_row.append(InlineKeyboardButton("🔍 More Details", callback_data="showdetails_"))
                        else:
                            new_row.append(btn)
                    if new_row:
                        new_kb.add(*new_row)
                bot.edit_message_text(full_msg, call.message.chat.id, msg_id, reply_markup=new_kb, parse_mode="Markdown")
                bot.answer_callback_query(call.id, "Showing details")
            else:
                short_msg = None
                for entry in signal_log:
                    if entry.get("msg_id") == msg_id:
                        direction = entry.get("direction", "BUY")
                        symbol = entry.get("symbol", "")
                        icon = "🟢" if direction == "BUY" else "🔴"
                        short_msg = f"{icon} *{direction}* {symbol}\n\n✅ Confirmed"
                        break
                if not short_msg:
                    short_msg = "Signal (tap to refresh)"
                new_kb = InlineKeyboardMarkup(row_width=2)
                for row in call.message.reply_markup.keyboard:
                    new_row = []
                    for btn in row:
                        if btn.callback_data.startswith("hidedetails_"):
                            new_row.append(InlineKeyboardButton("🔍 More Details", callback_data="showdetails_"))
                        elif btn.callback_data.startswith("showdetails_"):
                            new_row.append(InlineKeyboardButton("🔍 Hide Details", callback_data="hidedetails_"))
                        else:
                            new_row.append(btn)
                    if new_row:
                        new_kb.add(*new_row)
                bot.edit_message_text(short_msg, call.message.chat.id, msg_id, reply_markup=new_kb, parse_mode="Markdown")
                bot.answer_callback_query(call.id, "Hiding details")

        elif data == "chat_start":
            chat_mode[call.message.chat.id] = "chat"
            bot.send_message(call.message.chat.id, "💬 *Chat mode activated*\nType your message (or /cancel to exit).", parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Chat mode on")
        elif data == "debug_start":
            chat_mode[call.message.chat.id] = "debug"
            bot.send_message(call.message.chat.id, "🐛 *Debug mode activated*\nPaste signal details for analysis (or /cancel to exit).", parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Debug mode on")

        elif data == "test_ai":
            answer = ask_ai_core("Reply with 'AI Core is connected and ready.' Keep it very short.")
            bot.answer_callback_query(call.id, answer[:200], show_alert=True)

        # (all other callbacks: main_menu, status, blocked_list, watchlist, add/remove, etc. unchanged)
        # For brevity, I'll include a few essentials; you must keep the full set from previous code.
        elif data == "main_menu":
            bot.edit_message_text("📋 *Main Menu*", call.message.chat.id, call.message.message_id,
                                  reply_markup=get_main_menu(), parse_mode="Markdown")
        elif data == "status":
            blocked_count = len(spread_blocked)
            status_text = f"🟢 Scanner: {'RUNNING' if STATE['running'] else 'PAUSED'}\n📊 Pairs: {len(STRATEGY_PAIRS)}\n🚫 Blocked: {blocked_count}"
            kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(status_text, call.message.chat.id, call.message.message_id, reply_markup=kb)
        # (include all other callbacks exactly as in the last full code: blocked_list, watchlist, remove_menu, add_menu, add_, remove_, start_scanner, pause_scanner, quick_scan, mute_, info_, stats_page, entry_menu, entry_, help)
        # For brevity, they are omitted here but must be present. Copy them from the last full code.

        else:
            bot.answer_callback_query(call.id, "Unknown action")
    except Exception as e:
        logging.error(f"Callback error: {e}")
        bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)

# --- Old feedback reply handler (unchanged) ---
def process_feedback_reply(message, msg_id):
    if str(message.chat.id) != CHAT_ID: return
    pending_feedback.pop(message.chat.id, None)
    text = message.text.strip()
    if text.upper() == "SKIP":
        bot.reply_to(message, "Feedback details skipped.")
        return
    if not (text.upper().startswith("WIN") or text.upper().startswith("LOSS")):
        bot.reply_to(message, "Please start with WIN or LOSS. Try again.")
        return
    result = text.split()[0].upper()
    rest_text = text[len(result):].strip()
    delay_sec = 0
    reason = ""
    if rest_text:
        parts2 = rest_text.split(maxsplit=1)
        first_part = parts2[0].upper()
        if first_part.endswith("M") or first_part.endswith("S") or first_part.isdigit():
            if first_part.endswith("M"):
                try: delay_sec = int(float(first_part[:-1]) * 60)
                except: pass
            elif first_part.endswith("S"):
                try: delay_sec = int(float(first_part[:-1]))
                except: pass
            else:
                try: delay_sec = int(float(first_part))
                except: pass
            if len(parts2) > 1:
                reason = parts2[1].strip()
                if reason.lower().startswith("reason:"):
                    reason = reason[7:].strip()
        else:
            reason = rest_text.strip()
            if reason.lower().startswith("reason:"):
                reason = reason[7:].strip()
    success, extra = record_feedback(msg_id, result, delay_sec, reason, "", "")
    if success:
        resp = f"✅ Feedback recorded: {result} (delay {delay_sec}s)"
        if reason: resp += f", reason: {reason}"
        bot.reply_to(message, resp)
        if extra: bot.send_message(message.chat.id, extra)
    else:
        bot.reply_to(message, "❌ Could not find the original signal message.")

# --- Message Handlers ---
@bot.message_handler(commands=['start'])
def start(m):
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(m.chat.id, "🚀 *Forex Scanner Online*\nUse the buttons below.",
                         reply_markup=get_main_menu(), parse_mode="Markdown")

@bot.message_handler(commands=['cancel'])
def cancel_chat(m):
    if str(m.chat.id) == CHAT_ID:
        chat_mode.pop(m.chat.id, None)
        loss_interview_state.pop(m.chat.id, None)
        bot.reply_to(m, "❌ Mode cancelled.")

@bot.message_handler(func=lambda m: str(m.chat.id) == CHAT_ID and m.chat.id in loss_interview_state)
def loss_interview_handler(m):
    state = loss_interview_state.get(m.chat.id)
    if not state: return
    msg_id = state["msg_id"]
    step = state["step"]
    if step == "timing":
        process_loss_timing(m, msg_id)

@bot.message_handler(func=lambda m: str(m.chat.id) == CHAT_ID and m.chat.id in chat_mode)
def handle_chat_message(m):
    mode = chat_mode[m.chat.id]
    if mode == "chat":
        system_prompt = "You are a friendly and knowledgeable trading assistant. Be concise."
    else:
        system_prompt = "You are a trading bot debugger. Analyze the signal details, explain what went wrong, and suggest improvements."
    thinking = bot.send_message(m.chat.id, "🤔 Thinking...")
    answer = ask_ai_core(m.text, system_prompt)
    bot.edit_message_text(answer, m.chat.id, thinking.message_id)

# --- Main Entry ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    print(f"Bot and Web Server starting on port {port}...")
    app.run(host="0.0.0.0", port=port)
