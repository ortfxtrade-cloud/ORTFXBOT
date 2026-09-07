import os
import re
import time
import random
import threading
import logging
import csv
import requests
import pandas as pd
import telebot
import yfinance as yf
from flask import Flask
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# --- Load credentials with validation ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("❌ TELEGRAM_TOKEN is not set in environment variables")

if not CHAT_ID:
    raise RuntimeError("❌ CHAT_ID is not set in environment variables")

print(f"✅ TELEGRAM_TOKEN loaded: {TELEGRAM_TOKEN[:10]}...")
print(f"✅ CHAT_ID loaded: {CHAT_ID}")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

app = Flask(__name__)

# --- OANDA Spread Filter Settings (Hybrid) ---
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
MAX_SPREAD_PIPS = float(os.environ.get("MAX_SPREAD_PIPS", "3.0"))
UNBLOCK_CONSECUTIVE_CHECKS = int(os.environ.get("UNBLOCK_CONSECUTIVE_CHECKS", "2"))

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

spread_blocked_5m = {}

# Learning
signal_log = []
pair_loss_streak = {}
pair_entry_stats = {}
pending_feedback = {}
full_signal_messages = {}
loss_interview_state = {}

# Chat / Debug mode
chat_mode = {}

# --- Default RSI settings (5m only) ---
DEFAULT_RSI_BUY_MIN = 30
DEFAULT_RSI_BUY_MAX = 40
DEFAULT_RSI_SELL_MIN = 60
DEFAULT_RSI_SELL_MAX = 70

pair_settings = {}
settings_state = {}

# --- Martingale scheduler ---
martingale_jobs = []

# --- MACD Compression Filter ---
compression_counter = {}
compression_blocked = {}

logging.basicConfig(level=logging.INFO)

@app.route('/')
def health_check():
    return "Bot is running!"

# --- Debug message handler (catches ALL messages) ---
@bot.message_handler(func=lambda m: True)
def debug_all_messages(m):
    print(f"🔍 RECEIVED MESSAGE from {m.chat.id}: {m.text}")
    print(f"🔍 Expected CHAT_ID: {CHAT_ID}")
    print(f"🔍 Match: {str(m.chat.id) == str(CHAT_ID)}")

# --- Helper: get effective settings for a pair ---
def get_effective_settings(symbol):
    if symbol in pair_settings:
        return {
            "rsi_buy_min": pair_settings[symbol].get("rsi_buy_min", DEFAULT_RSI_BUY_MIN),
            "rsi_buy_max": pair_settings[symbol].get("rsi_buy_max", DEFAULT_RSI_BUY_MAX),
            "rsi_sell_min": pair_settings[symbol].get("rsi_sell_min", DEFAULT_RSI_SELL_MIN),
            "rsi_sell_max": pair_settings[symbol].get("rsi_sell_max", DEFAULT_RSI_SELL_MAX)
        }
    else:
        return {
            "rsi_buy_min": DEFAULT_RSI_BUY_MIN,
            "rsi_buy_max": DEFAULT_RSI_BUY_MAX,
            "rsi_sell_min": DEFAULT_RSI_SELL_MIN,
            "rsi_sell_max": DEFAULT_RSI_SELL_MAX
        }

# --- Inline Keyboards ---
def get_main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Status", callback_data="status"),
        InlineKeyboardButton("📋 Watchlist", callback_data="watchlist"),
        InlineKeyboardButton("🚫 Blocked", callback_data="blocked_list"),
        InlineKeyboardButton("📋 Conditions", callback_data="signal_conditions"),
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

# --- Spread Detection (5-Minute Flat Market – yfinance) ---
def is_spread_present_5m(symbol):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="1d", interval="5m")
        if len(df) < 10:
            return False
        last_candles = df.tail(5)
        high_low_range = last_candles['High'] - last_candles['Low']
        avg_range = high_low_range.mean()
        avg_price = last_candles['Close'].mean()
        body_size = abs(last_candles['Close'] - last_candles['Open'])
        avg_body = body_size.mean()
        condition1 = avg_price > 0 and avg_range < avg_price * 0.0002
        condition2 = avg_price > 0 and avg_body < avg_price * 0.00005
        condition3 = df.iloc[-1]['High'] == df.iloc[-1]['Low']
        if sum([condition1, condition2, condition3]) >= 2:
            return True
        return False
    except Exception as e:
        logging.error(f"5m spread check error {symbol}: {e}")
        return False

# --- OANDA Live Spread Helper ---
def get_oanda_spread_pips(symbol):
    if not OANDA_API_KEY or not OANDA_ACCOUNT_ID:
        return None
    instrument = symbol.replace("=X", "")
    base = instrument[:3]
    quote = instrument[3:]
    oanda_symbol = f"{base}_{quote}"
    try:
        url = f"https://api-fxtrade.oanda.com/v3/instruments/{oanda_symbol}/pricing"
        headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
        params = {"instruments": oanda_symbol}
        response = requests.get(url, headers=headers, params=params, timeout=5)
        data = response.json()
        prices = data.get("prices", [])
        if not prices:
            return None
        bid = float(prices[0]["bids"][0]["price"])
        ask = float(prices[0]["asks"][0]["price"])
        spread = ask - bid
        if "JPY" in quote:
            pip_size = 0.01
        else:
            pip_size = 0.0001
        return round(spread / pip_size, 2)
    except Exception as e:
        logging.error(f"OANDA spread check error {symbol}: {e}")
        return None

# --- Strategy ---
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

def latest_1m_cross(diff_series):
    diffs = list(diff_series)
    for i in range(len(diffs)-1, 0, -1):
        if diffs[i-1] < 0 and diffs[i] > 0:
            return 'bull'
        elif diffs[i-1] > 0 and diffs[i] < 0:
            return 'bear'
    return None

def quick_scan_single(symbol):
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m")
        if len(df) < 50: return None, "Insufficient data"
        m, s, h, rsi = calculate_strategy(df)
        prev_diff = m.iloc[-1] - s.iloc[-1]
        prev_diff_before = m.iloc[-2] - s.iloc[-2]
        df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
        m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)
        diff_1m_series = m_1m - s_1m
        cross_1m = latest_1m_cross(diff_1m_series)

        is_1m_bull = (cross_1m == 'bull')
        is_1m_bear = (cross_1m == 'bear')

        settings = get_effective_settings(symbol)
        rsi_val = rsi.iloc[-1]

        is_bull = (prev_diff_before < 0) and (prev_diff > 0) and is_1m_bull
        is_bear = (prev_diff_before > 0) and (prev_diff < 0) and is_1m_bear

        if is_bull and settings["rsi_buy_min"] <= rsi_val <= settings["rsi_buy_max"]:
            return "BUY", {"symbol": symbol, "macd_5m": m.iloc[-1], "signal_5m": s.iloc[-1],
                           "diff_5m": prev_diff, "macd_1m": m_1m.iloc[-1], "signal_1m": s_1m.iloc[-1],
                           "diff_1m": diff_1m_series.iloc[-1], "rsi_5m": rsi_val}
        elif is_bear and settings["rsi_sell_min"] <= rsi_val <= settings["rsi_sell_max"]:
            return "SELL", {"symbol": symbol, "macd_5m": m.iloc[-1], "signal_5m": s.iloc[-1],
                           "diff_5m": prev_diff, "macd_1m": m_1m.iloc[-1], "signal_1m": s_1m.iloc[-1],
                           "diff_1m": diff_1m_series.iloc[-1], "rsi_5m": rsi_val}
        return "NEUTRAL", None
    except Exception as e:
        return None, str(e)

# --- Diagnose all conditions for a pair ---
def diagnose_pair(symbol):
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m")
        if len(df) < 50:
            return "❌ Not enough data to evaluate."
        m, s, h, rsi = calculate_strategy(df)
        prev_diff = m.iloc[-1] - s.iloc[-1]
        prev_diff_before = m.iloc[-2] - s.iloc[-2]

        df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
        if len(df_1m) < 30:
            return "❌ Not enough 1m data."
        m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)
        diff_1m_series = m_1m - s_1m
        cross_1m = latest_1m_cross(diff_1m_series)

        settings = get_effective_settings(symbol)
        rsi_val = rsi.iloc[-1]

        spread_blocked = symbol in spread_blocked_5m
        if not spread_blocked:
            spread_blocked = is_spread_present_5m(symbol)
        spread_ok = not spread_blocked

        hist_abs = h.abs()
        avg_hist_abs = hist_abs.tail(100).mean() if len(hist_abs) >= 100 else hist_abs.mean()
        current_abs = abs(h.iloc[-1])
        low_thresh = avg_hist_abs * 0.20
        high_thresh = avg_hist_abs * 0.50
        if symbol in compression_blocked and compression_blocked[symbol]:
            compression_ok = False
        else:
            compression_ok = not (current_abs < low_thresh and compression_counter.get(symbol, 0) >= 10)

        bull_5m = (prev_diff_before < 0) and (prev_diff > 0) and (abs(prev_diff) >= 0.00001)
        bear_5m = (prev_diff_before > 0) and (prev_diff < 0) and (abs(prev_diff) >= 0.00001)

        bull_1m = (cross_1m == 'bull')
        bear_1m = (cross_1m == 'bear')

        rsi5_buy = settings["rsi_buy_min"] <= rsi_val <= settings["rsi_buy_max"]
        rsi5_sell = settings["rsi_sell_min"] <= rsi_val <= settings["rsi_sell_max"]

        cd = alert_cooldowns.get(symbol, 0)
        cooldown_ok = (time.time() - cd) > 300

        pair_display = symbol.replace("=X", "")
        report = f"📋 *Conditions for {pair_display}*\n\n"
        report += f"Spread filter: {'✅' if spread_ok else '❌'}\n"
        report += f"MACD compression: {'✅' if compression_ok else '❌'}\n"
        report += f"5m MACD cross: {'🟢 Bullish' if bull_5m else '🔴 Bearish' if bear_5m else '❌ None'}\n"
        report += f"Latest 1m cross: {'🟢 Bullish' if bull_1m else '🔴 Bearish' if bear_1m else '❌ None'}\n"
        report += f"5m RSI ({rsi_val:.1f}): {'✅' if (rsi5_buy or rsi5_sell) else '❌'}\n"
        report += f"Cooldown: {'✅' if cooldown_ok else '❌'}\n\n"

        if bull_5m and bull_1m and rsi5_buy and spread_ok and compression_ok and cooldown_ok:
            report += "🟢 *BUY signal READY*"
        elif bear_5m and bear_1m and rsi5_sell and spread_ok and compression_ok and cooldown_ok:
            report += "🔴 *SELL signal READY*"
        else:
            report += "⏳ *No signal ready yet*"

        return report
    except Exception as e:
        return f"❌ Error: {e}"

# --- Scanner Engine ---
def scanner_engine():
    global alert_cooldowns, signal_log, full_signal_messages, martingale_jobs, compression_counter, compression_blocked, spread_blocked_5m
    while True:
        if STATE["running"]:
            with data_lock:
                current_pairs = list(STRATEGY_PAIRS)
            random.shuffle(current_pairs)
            for symbol in current_pairs:
                try:
                    if symbol in spread_blocked_5m:
                        blocked_info = spread_blocked_5m[symbol]
                        if OANDA_API_KEY and OANDA_ACCOUNT_ID:
                            spread_pips = get_oanda_spread_pips(symbol)
                            if spread_pips is not None:
                                if spread_pips <= MAX_SPREAD_PIPS:
                                    blocked_info['low_spread_count'] = blocked_info.get('low_spread_count', 0) + 1
                                    if blocked_info['low_spread_count'] >= UNBLOCK_CONSECUTIVE_CHECKS:
                                        del spread_blocked_5m[symbol]
                                        logging.info(f"Unblocked {symbol} – OANDA spread {spread_pips} pips")
                                else:
                                    blocked_info['low_spread_count'] = 0
                            else:
                                if time.time() - blocked_info['blocked_since'] > 1800:
                                    del spread_blocked_5m[symbol]
                        else:
                            if time.time() - blocked_info['blocked_since'] > 1800:
                                del spread_blocked_5m[symbol]
                        if symbol in spread_blocked_5m:
                            continue

                    if is_spread_present_5m(symbol):
                        spread_blocked_5m[symbol] = {
                            'blocked_since': time.time(),
                            'low_spread_count': 0
                        }
                        continue

                    df = yf.Ticker(symbol).history(period="5d", interval="5m")
                    if len(df) < 50: continue
                    m, s, h, rsi = calculate_strategy(df)
                    prev_diff = m.iloc[-1] - s.iloc[-1]
                    prev_diff_before = m.iloc[-2] - s.iloc[-2]

                    hist_abs = h.abs()
                    avg_hist_abs = hist_abs.tail(100).mean() if len(hist_abs) >= 100 else hist_abs.mean()
                    current_abs = abs(h.iloc[-1])
                    low_threshold = avg_hist_abs * 0.20
                    high_threshold = avg_hist_abs * 0.50

                    if symbol not in compression_counter:
                        compression_counter[symbol] = 0
                    if symbol not in compression_blocked:
                        compression_blocked[symbol] = False

                    if compression_blocked[symbol]:
                        if current_abs > high_threshold:
                            compression_blocked[symbol] = False
                            compression_counter[symbol] = 0
                        else:
                            continue
                    else:
                        if current_abs < low_threshold:
                            compression_counter[symbol] += 1
                            if compression_counter[symbol] >= 10:
                                compression_blocked[symbol] = True
                                continue
                        else:
                            compression_counter[symbol] = 0

                    df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
                    if len(df_1m) < 30: continue
                    m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)
                    diff_1m_series = m_1m - s_1m
                    cross_1m = latest_1m_cross(diff_1m_series)

                    is_1m_bull = (cross_1m == 'bull')
                    is_1m_bear = (cross_1m == 'bear')

                    settings = get_effective_settings(symbol)
                    rsi_val = rsi.iloc[-1]

                    confirm_bull = (prev_diff_before < 0) and (prev_diff > 0) and (abs(prev_diff) >= 0.00001) \
                                   and is_1m_bull \
                                   and (settings["rsi_buy_min"] <= rsi_val <= settings["rsi_buy_max"])

                    confirm_bear = (prev_diff_before > 0) and (prev_diff < 0) and (abs(prev_diff) >= 0.00001) \
                                   and is_1m_bear \
                                   and (settings["rsi_sell_min"] <= rsi_val <= settings["rsi_sell_max"])

                    if confirm_bull or confirm_bear:
                        if time.time() - alert_cooldowns.get(symbol, 0) > 300:
                            direction = "BUY" if confirm_bull else "SELL"
                            pair_display = symbol.replace("=X", "")
                            parts = [pair_display[:3], pair_display[3:]]
                            flag_map = {
                                "EUR": "🇪🇺", "USD": "🇺🇸", "GBP": "🇬🇧", "JPY": "🇯🇵",
                                "CHF": "🇨🇭", "AUD": "🇦🇺", "NZD": "🇳🇿", "CAD": "🇨🇦",
                                "BTC": "₿", "ETH": "Ξ", "XAU": "🥇", "XAG": "🥈"
                            }
                            flag1 = flag_map.get(parts[0], "🏳️")
                            flag2 = flag_map.get(parts[1], "🏳️")
                            entry_dt = df.index[-1]
                            entry_time_str = entry_dt.strftime("%H:%M")
                            mart1 = (entry_dt + pd.Timedelta(minutes=5)).strftime("%H:%M")
                            mart2 = (entry_dt + pd.Timedelta(minutes=10)).strftime("%H:%M")
                            mart3 = (entry_dt + pd.Timedelta(minutes=15)).strftime("%H:%M")
                            trigger_time = entry_dt.timestamp() + (15 * 60) + 30
                            arrow = "🟥" if direction == "SELL" else "🟩"

                            short_msg = (
                                f"⚡ SIGNAL\n\n"
                                f"{flag1}{flag2} {pair_display}\n"
                                f"Timeframe: M5\n"
                                f"⏱ Expiration: 5 minutes\n"
                                f"⏰ Entry: {entry_time_str}\n"
                                f"{arrow} Direction: {direction}\n"
                                f"📊 Martingale:\n"
                                f"1⃣ {mart1}\n"
                                f"2⃣ {mart2}\n"
                                f"3⃣ {mart3}"
                            )

                            full_msg = (
                                f"{short_msg}\n\n"
                                f"✅ MACD Cross + 1m Cross Confirmed\n\n"
                                f"5m MACD: {m.iloc[-1]:.5f} | Signal: {s.iloc[-1]:.5f}\n"
                                f"5m Diff: {prev_diff:.5f}\n"
                                f"1m MACD: {m_1m.iloc[-1]:.5f} | Signal: {s_1m.iloc[-1]:.5f}\n"
                                f"1m Diff: {diff_1m_series.iloc[-1]:.5f}\n"
                                f"5m RSI: {rsi_val:.2f}\n\n"
                                f"Status: SAFE (Active Liquidity)"
                            )

                            kb = InlineKeyboardMarkup(row_width=2)
                            kb.add(
                                InlineKeyboardButton("🔍 More Details", callback_data=f"showdetails_"),
                                InlineKeyboardButton("📋 Conditions", callback_data=f"cond_{symbol}"),
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
                                    "diff_1m": diff_1m_series.iloc[-1],
                                    "rsi_5m": rsi_val,
                                    "msg_id": sent_msg.message_id,
                                    "entry_delay": "",
                                    "loss_reason": "",
                                    "loss_notes": "",
                                    "analysis_result": "",
                                    "result": ""
                                }
                                signal_log.append(log_entry)
                                martingale_jobs.append({
                                    "trigger_time": trigger_time,
                                    "chat_id": CHAT_ID,
                                    "symbol": symbol,
                                    "direction": direction,
                                    "entry_time": entry_time_str,
                                    "msg_id": sent_msg.message_id
                                })
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

# --- Martingale Scheduler ---
def martingale_scheduler():
    global martingale_jobs
    while True:
        now = time.time()
        due = [job for job in martingale_jobs if job["trigger_time"] <= now]
        for job in due:
            symbol = job["symbol"]
            pair_display = symbol.replace("=X", "")
            direction = job["direction"]
            entry_time = job["entry_time"]

            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("✅ WIN", callback_data=f"mart_win_{symbol}"),
                InlineKeyboardButton("❌ LOSS", callback_data=f"mart_loss_{symbol}"),
            )
            bot.send_message(
                job["chat_id"],
                f"📊 Martingale series for {pair_display} completed.\n"
                f"Direction: {direction}\n"
                f"Entry: {entry_time}\n"
                f"Did you win or lose?",
                reply_markup=kb
            )
        martingale_jobs = [j for j in martingale_jobs if j["trigger_time"] > now]
        time.sleep(10)

# --- Feedback helper ---
def record_feedback(msg_id, result, delay_sec=0, reason="", notes="", analysis=""):
    global pair_loss_streak, pair_entry_stats
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
            else:
                pair_loss_streak[symbol] = 0

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

# --- Gemini API helper ---
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

# --- Loss Interview Flow ---
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
                f"5m RSI: {entry['rsi_5m']:.2f}"
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
        "If yes, suggest specific parameter adjustments (e.g., change RSI bounds). "
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

# --- Settings Handlers ---
@bot.callback_query_handler(func=lambda call: call.data == "settings_menu")
def settings_menu(call):
    if str(call.message.chat.id) != CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🌐 All Pairs (Global)", callback_data="set_global"))
    with data_lock:
        pairs = list(STRATEGY_PAIRS)
    for pair in pairs[:10]:
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
        target = call.data[8:]
        target_name = target

    settings_state[call.message.chat.id] = {"target": target}

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("📈 RSI Buy (5m)", callback_data="param_rsi_buy"))
    kb.add(InlineKeyboardButton("📉 RSI Sell (5m)", callback_data="param_rsi_sell"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="settings_menu"))
    bot.edit_message_text(f"⚙️ *Settings for {target_name}*\nSelect parameter to change:", call.message.chat.id,
                          call.message.message_id, reply_markup=kb, parse_mode="Markdown")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("param_"))
def settings_param_selected(call):
    if str(call.message.chat.id) != CHAT_ID or call.message.chat.id not in settings_state:
        bot.answer_callback_query(call.id, "Unauthorized or no state")
        return
    param = call.data[6:]
    state = settings_state[call.message.chat.id]
    target = state["target"]

    if param == "rsi_buy":
        prompt_text = f"Enter new 5m RSI Buy range as `min-max` (e.g., 30-40). Current:\n"
        if target == "all":
            prompt_text += f"Global: {DEFAULT_RSI_BUY_MIN}-{DEFAULT_RSI_BUY_MAX}"
        else:
            settings = get_effective_settings(target)
            prompt_text += f"{target}: {settings['rsi_buy_min']}-{settings['rsi_buy_max']}"
    elif param == "rsi_sell":
        prompt_text = f"Enter new 5m RSI Sell range as `min-max` (e.g., 60-70). Current:\n"
        if target == "all":
            prompt_text += f"Global: {DEFAULT_RSI_SELL_MIN}-{DEFAULT_RSI_SELL_MAX}"
        else:
            settings = get_effective_settings(target)
            prompt_text += f"{target}: {settings['rsi_sell_min']}-{settings['rsi_sell_max']}"

    state["param"] = param
    bot.edit_message_text(prompt_text, call.message.chat.id, call.message.message_id)
    msg = bot.send_message(call.message.chat.id, "Please reply with the new value:")
    bot.register_next_step_handler(msg, process_settings_value)
    bot.answer_callback_query(call.id)

def process_settings_value(message):
    global DEFAULT_RSI_BUY_MIN, DEFAULT_RSI_BUY_MAX
    global DEFAULT_RSI_SELL_MIN, DEFAULT_RSI_SELL_MAX
    global pair_settings

    chat_id = message.chat.id
    if chat_id not in settings_state:
        return
    state = settings_state[chat_id]
    target = state["target"]
    param = state.get("param")
    value = message.text.strip()

    try:
        if param in ("rsi_buy", "rsi_sell"):
            parts = value.split('-')
            if len(parts) != 2:
                raise ValueError("Format must be min-max")
            min_val = int(parts[0].strip())
            max_val = int(parts[1].strip())
            if min_val < 0 or min_val > 100 or max_val < 0 or max_val > 100 or min_val >= max_val:
                raise ValueError("Invalid range")
            if target == "all":
                if param == "rsi_buy":
                    DEFAULT_RSI_BUY_MIN = min_val
                    DEFAULT_RSI_BUY_MAX = max_val
                elif param == "rsi_sell":
                    DEFAULT_RSI_SELL_MIN = min_val
                    DEFAULT_RSI_SELL_MAX = max_val
                bot.reply_to(message, f"✅ Global {param} set to {min_val}-{max_val}")
            else:
                if target not in pair_settings:
                    pair_settings[target] = {}
                if param == "rsi_buy":
                    pair_settings[target]["rsi_buy_min"] = min_val
                    pair_settings[target]["rsi_buy_max"] = max_val
                elif param == "rsi_sell":
                    pair_settings[target]["rsi_sell_min"] = min_val
                    pair_settings[target]["rsi_sell_max"] = max_val
                bot.reply_to(message, f"✅ {target} {param} set to {min_val}-{max_val}")
        else:
            bot.reply_to(message, "Unknown parameter.")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid input: {e}. Please try again.")
        settings_state.pop(chat_id, None)
        return

    settings_state.pop(chat_id, None)
    bot.send_message(chat_id, "Settings updated.", reply_markup=get_main_menu())

# --- Callback Handlers (with Conditions) ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if str(call.message.chat.id) != CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return
    data = call.data
    msg_id = call.message.message_id
    try:
        if data.startswith("mart_win_") or data.startswith("mart_loss_"):
            result = "WIN" if data.startswith("mart_win_") else "LOSS"
            symbol = data[9:] if data.startswith("mart_win_") else data[10:]
            for entry in reversed(signal_log):
                if entry.get("symbol") == symbol and not entry.get("result"):
                    record_feedback(entry["msg_id"], result)
                    break
            bot.answer_callback_query(call.id, f"Recorded: {result}")
            try:
                new_kb = InlineKeyboardMarkup()
                new_kb.add(InlineKeyboardButton("✅ RECORDED", callback_data="none"))
                bot.edit_message_reply_markup(call.message.chat.id, msg_id, reply_markup=new_kb)
            except:
                pass

        elif data.startswith("win_"):
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

        elif data == "signal_conditions":
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(
                InlineKeyboardButton("🔍 Choose from list", callback_data="cond_list"),
                InlineKeyboardButton("✏️ Enter pair manually", callback_data="cond_manual"),
                InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")
            )
            bot.edit_message_text("📋 *Check Signal Conditions*\nChoose a method:", call.message.chat.id,
                                  call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id)

        elif data == "cond_list":
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
            kb = InlineKeyboardMarkup(row_width=2)
            for pair in pairs[:10]:
                kb.add(InlineKeyboardButton(pair, callback_data=f"cond_{pair}"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="signal_conditions"))
            bot.edit_message_text("Select a pair:", call.message.chat.id, call.message.message_id,
                                  reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id)

        elif data == "cond_manual":
            msg = bot.send_message(call.message.chat.id,
                                   "Please type the pair symbol (e.g., EURUSD=X, GBPJPY=X):",
                                   parse_mode="Markdown")
            bot.register_next_step_handler(msg, process_cond_manual)
            bot.answer_callback_query(call.id)

        elif data.startswith("cond_"):
            pair = data[5:]
            logging.info(f"Condition check for {pair}")
            report = diagnose_pair(pair)
            bot.send_message(call.message.chat.id, report, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Conditions checked")

        elif data == "quick_scan":
            with data_lock: pairs = list(STRATEGY_PAIRS[:5])
            bot.send_message(call.message.chat.id, "🔍 *Quick scanning...*", parse_mode="Markdown")
            results = []
            for pair in pairs:
                direction, result = quick_scan_single(pair)
                if direction and direction != "NEUTRAL":
                    icon = "🟢" if direction == "BUY" else "🔴"
                    r = result if isinstance(result, dict) else None
                    if r: results.append(f"{icon} {pair}: {direction} (RSI5m: {r['rsi_5m']:.1f})")
            msg = "📊 *Quick Scan Results:*\n" + ("\n".join(results) if results else "No signals found.")
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="quick_scan"))
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.send_message(call.message.chat.id, msg, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Scan complete")

        elif data.startswith("quick_") and data != "quick_scan":
            pair = data.replace("quick_", "")
            logging.info(f"Rescan requested for {pair}")
            bot.send_message(call.message.chat.id, f"🔍 *Scanning {pair}...*", parse_mode="Markdown")
            direction, result = quick_scan_single(pair)
            if isinstance(result, dict):
                icon = {"BUY": "🟢", "SELL": "🔴"}.get(direction, "⚪")
                msg = (f"{icon} *{pair}*\n\n"
                       f"5m MACD: {result['macd_5m']:.5f} | Signal: {result['signal_5m']:.5f}\n"
                       f"5m Diff: {result['diff_5m']:.5f}\n"
                       f"1m MACD: {result['macd_1m']:.5f} | Signal: {result['signal_1m']:.5f}\n"
                       f"1m Diff: {result['diff_1m']:.5f}\n"
                       f"5m RSI: {result['rsi_5m']:.2f}\n"
                       f"Signal: {direction}")
            else:
                msg = f"❌ Error: {result}"
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Rescan", callback_data=f"quick_{pair}"))
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.send_message(call.message.chat.id, msg, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Rescan complete")

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

        elif data == "main_menu":
            bot.edit_message_text("📋 *Main Menu*", call.message.chat.id, call.message.message_id,
                                  reply_markup=get_main_menu(), parse_mode="Markdown")
        elif data == "status":
            blocked_count = len(spread_blocked_5m) + sum(compression_blocked.values())
            status_text = f"🟢 Scanner: {'RUNNING' if STATE['running'] else 'PAUSED'}\n📊 Pairs: {len(STRATEGY_PAIRS)}\n🚫 Blocked: {blocked_count}"
            kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(status_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

        elif data == "blocked_list":
            if not spread_blocked_5m and not any(compression_blocked.values()):
                msg = "✅ *No Blocked Pairs*\n\nAll pairs scanning normally."
            else:
                msg = "🚫 *Blocked Pairs:*\n\n"
                for sym, info in spread_blocked_5m.items():
                    msg += f"• {sym} — Spread (blocked since {time.strftime('%H:%M', time.localtime(info['blocked_since']))})\n"
                for sym, blocked in compression_blocked.items():
                    if blocked:
                        msg += f"• {sym} — MACD compression\n"
            kb = InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔄 Refresh", callback_data="blocked_list"),
                InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data == "watchlist" or data.startswith("page_info_"):
            page = int(data.split("_")[-1]) if data.startswith("page_info_") else 0
            with data_lock: pairs = list(STRATEGY_PAIRS)
            kb = get_pairs_keyboard(pairs, "info", page)
            bot.edit_message_text(f"📋 *Watchlist ({len(pairs)} pairs)*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data == "remove_menu" or data.startswith("page_remove_"):
            page = int(data.split("_")[-1]) if data.startswith("page_remove_") else 0
            with data_lock: pairs = list(STRATEGY_PAIRS)
            if not pairs:
                bot.edit_message_text("📭 No pairs to remove.", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())
            else:
                kb = get_pairs_keyboard(pairs, "remove", page)
                bot.edit_message_text("❌ *Select pair to remove:*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data == "add_menu":
            bot.edit_message_text("➕ *Add a pair:*\nSend /add SYMBOL or choose below:", call.message.chat.id, call.message.message_id, reply_markup=get_add_suggestions(), parse_mode="Markdown")

        elif data.startswith("add_"):
            pair = data.replace("add_", "")
            ticker = yf.Ticker(pair)
            if len(ticker.history(period="1d")) > 0:
                with data_lock:
                    if pair not in STRATEGY_PAIRS: STRATEGY_PAIRS.append(pair)
                bot.answer_callback_query(call.id, f"✅ {pair} added!")
            else:
                bot.answer_callback_query(call.id, f"❌ {pair} not found", show_alert=True)

        elif data.startswith("remove_"):
            pair = data.replace("remove_", "")
            with data_lock:
                if pair in STRATEGY_PAIRS: STRATEGY_PAIRS.remove(pair)
            bot.answer_callback_query(call.id, f"🗑️ {pair} removed!")
            with data_lock: pairs = list(STRATEGY_PAIRS)
            kb = get_pairs_keyboard(pairs, "remove", 0) if pairs else None
            if kb:
                bot.edit_message_text("❌ *Select pair to remove:*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            else:
                bot.edit_message_text("📭 No pairs left.", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())

        elif data == "start_scanner":
            STATE["running"] = True
            bot.answer_callback_query(call.id, "✅ Scanner started!")
        elif data == "pause_scanner":
            STATE["running"] = False
            bot.answer_callback_query(call.id, "⏸️ Scanner paused!")

        elif data.startswith("mute_"):
            pair = data.replace("mute_", "")
            alert_cooldowns[pair] = time.time() + 1800
            bot.answer_callback_query(call.id, f"🔕 {pair} muted for 30 min")

        elif data.startswith("info_"):
            pair = data.replace("info_", "")
            bot.send_message(call.message.chat.id, f"🔍 *Scanning {pair}...*", parse_mode="Markdown")
            direction, result = quick_scan_single(pair)
            if isinstance(result, dict):
                icon = {"BUY": "🟢", "SELL": "🔴"}.get(direction, "⚪")
                msg = (f"{icon} *{pair}*\n\n"
                       f"5m MACD: {result['macd_5m']:.5f} | Signal: {result['signal_5m']:.5f}\n"
                       f"5m Diff: {result['diff_5m']:.5f}\n"
                       f"1m MACD: {result['macd_1m']:.5f} | Signal: {result['signal_1m']:.5f}\n"
                       f"1m Diff: {result['diff_1m']:.5f}\n"
                       f"5m RSI: {result['rsi_5m']:.2f}\n"
                       f"Signal: {direction}")
            else: msg = f"❌ Error: {result}"
            kb = InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔄 Refresh", callback_data=f"info_{pair}"),
                InlineKeyboardButton("🔙 Watchlist", callback_data="watchlist"))
            bot.send_message(call.message.chat.id, msg, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Pair info loaded")

        elif data == "stats_page":
            total = len([x for x in signal_log if x.get("result")])
            wins = len([x for x in signal_log if x.get("result") == "WIN"])
            losses = len([x for x in signal_log if x.get("result") == "LOSS"])
            win_rate = (wins / total * 100) if total > 0 else 0
            msg = (f"📊 *Statistics*\n"
                   f"Total: {total} | Wins: {wins} | Losses: {losses}\n"
                   f"Win rate: {win_rate:.1f}%\n\n"
                   f"*Avg Win Entry Delay:*\n")
            for sym, stats in pair_entry_stats.items():
                if stats["wins"]:
                    avg = sum(stats["wins"])/len(stats["wins"])
                    msg += f"• {sym}: {avg:.1f}s\n"
            if not pair_entry_stats: msg += "No data yet\n"
            reason_counts = {}
            for entry in signal_log:
                if entry.get("result") == "LOSS" and entry.get("loss_reason"):
                    r = entry["loss_reason"]
                    reason_counts[r] = reason_counts.get(r, 0) + 1
            if reason_counts:
                msg += "\n*Top Loss Reasons:*\n"
                for r, cnt in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
                    msg += f"• {r}: {cnt}\n"
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="stats_page"))
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data == "entry_menu":
            pairs_with_data = list(pair_entry_stats.keys())
            if not pairs_with_data:
                bot.edit_message_text("No entry timing data yet.", call.message.chat.id, call.message.message_id,
                                      reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")), parse_mode="Markdown")
            else:
                kb = InlineKeyboardMarkup(row_width=2)
                for sym in pairs_with_data:
                    kb.add(InlineKeyboardButton(sym, callback_data=f"entry_{sym}"))
                kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
                bot.edit_message_text("Select a pair to see entry timing:", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data.startswith("entry_"):
            symbol = data[6:]
            if symbol in pair_entry_stats:
                stats = pair_entry_stats[symbol]
                wins = stats["wins"]
                losses = stats["losses"]
                avg_win = sum(wins)/len(wins) if wins else 0
                avg_loss = sum(losses)/len(losses) if losses else 0
                msg = f"📈 *Entry Timing: {symbol}*\n"
                msg += f"✅ Wins: {len(wins)} | Avg delay: {avg_win:.1f}s\n"
                msg += f"❌ Losses: {len(losses)} | Avg delay: {avg_loss:.1f}s\n\n"
                if wins: msg += f"Best entry: {avg_win:.1f}s after signal"
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("🔙 Entry Menu", callback_data="entry_menu"))
                kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
                bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            else:
                bot.answer_callback_query(call.id, "No data for this pair", show_alert=True)

        elif data == "help":
            help_text = (
                "🤖 *Forex Scanner Bot*\n\n"
                "• 💬 Chat: ask me anything\n"
                "• 🐛 Debug: analyze a signal\n"
                "• 🧪 Test AI: check connection\n"
                "• ⚙️ Settings: adjust RSI (5m)\n"
                "• 📋 Conditions: live checklist\n"
                "• 📈 Stats / ⏱️ Entry\n\n"
                "RSI: configurable per pair"
            )
            kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        else:
            logging.warning(f"Unknown callback data: {data}")
            bot.answer_callback_query(call.id, "Unknown action", show_alert=True)

    except Exception as e:
        logging.error(f"Callback error: {e}")
        bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)

# --- Process manual condition check ---
def process_cond_manual(message):
    if str(message.chat.id) != CHAT_ID:
        return
    pair = message.text.strip().upper()
    if "=X" not in pair:
        pair += "=X"
    report = diagnose_pair(pair)
    bot.reply_to(message, report, parse_mode="Markdown")

# --- Old feedback reply handler ---
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
    print(f"🔍 Received /start from {m.chat.id}")
    print(f"🔍 Expected CHAT_ID: {CHAT_ID}")
    print(f"🔍 Match: {str(m.chat.id) == str(CHAT_ID)}")
    
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(m.chat.id, "🚀 *Forex Scanner Online*\nUse the buttons below.",
                         reply_markup=get_main_menu(), parse_mode="Markdown")
        print("✅ Main menu sent")
    else:
        print("❌ CHAT_ID mismatch - user not authorized")

@bot.message_handler(commands=['menu'])
def menu_cmd(m):
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(m.chat.id, "📋 *Main Menu*", reply_markup=get_main_menu(), parse_mode="Markdown")

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

# --- Main Entry (clean) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=martingale_scheduler, daemon=True).start()
    
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port),
        daemon=True
    ).start()
    
    print("Starting bot polling...")
    print(f"Bot token: {TELEGRAM_TOKEN[:10]}...")
    print(f"CHAT_ID: {CHAT_ID}")
    
    bot.remove_webhook()
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
