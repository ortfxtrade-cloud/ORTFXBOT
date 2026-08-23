import os
import re
import time
import random
import threading
import logging
import csv
import json
import requests
import pandas as pd
import telebot
import yfinance as yf
from flask import Flask
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta

# -------------------- IQ Option API --------------------
try:
    from iqoptionapi.stable_api import IQOption
except ImportError:
    IQOption = None
    logging.warning("IQOption library not installed. Auto-trading disabled.")

# -------------------- Configuration --------------------
TELEGRAM_TOKEN = "8686769653:AAEUvYlVgCAv9Rn1jL82aNl6wTxk1-w7g3Q"
CHAT_ID = "8701685996"
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

app = Flask(__name__)

# --- OANDA Spread Filter Settings (Hybrid) ---
OANDA_API_KEY = os.environ.get("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
MAX_SPREAD_PIPS = float(os.environ.get("MAX_SPREAD_PIPS", "3.0"))
UNBLOCK_CONSECUTIVE_CHECKS = int(os.environ.get("UNBLOCK_CONSECUTIVE_CHECKS", "2"))

# --- IQ Option Credentials ---
IQ_OPTION_EMAIL = os.environ.get("IQ_OPTION_EMAIL", "")
IQ_OPTION_PASSWORD = os.environ.get("IQ_OPTION_PASSWORD", "")
TRADE_MODE = os.environ.get("TRADE_MODE", "demo")  # "demo" or "real"

# --- Trading Parameters (fixed expiry, configurable stake) ---
TRADE_EXPIRATION_MINUTES = 5  # always 5 minutes
DEFAULT_STAKE = 10.0  # USD

# Per-pair overrides for stake
pair_trade_settings = {}  # key: symbol, value: {"stake": float}

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

# Pending trades for manual confirmation (before placement)
pending_trades = {}  # key: msg_id, value: {"symbol":, "direction":, "stake":}

# Pending trades for auto-result check (after placement)
pending_expiry_trades = {}  # key: msg_id, value: {"symbol":, "direction":, "expiry_time":, "stake":, "msg_id":}

# --- Default RSI settings (5m + 1m) ---
DEFAULT_RSI_BUY_MIN = 30
DEFAULT_RSI_BUY_MAX = 40
DEFAULT_RSI_SELL_MIN = 50
DEFAULT_RSI_SELL_MAX = 70

DEFAULT_RSI_1M_BUY_MIN = 30
DEFAULT_RSI_1M_BUY_MAX = 40
DEFAULT_RSI_1M_SELL_MIN = 50
DEFAULT_RSI_1M_SELL_MAX = 70

pair_settings = {}
settings_state = {}

# --- MACD Compression Filter ---
compression_counter = {}
compression_blocked = {}

logging.basicConfig(level=logging.INFO)

# -------------------- IQ Option Client --------------------
iq_api = None

def init_iq_option():
    global iq_api
    if IQOption is None:
        logging.warning("IQOption library not available. Auto-trading disabled.")
        return
    if not IQ_OPTION_EMAIL or not IQ_OPTION_PASSWORD:
        logging.warning("IQ Option credentials not set. Auto-trading disabled.")
        return
    try:
        iq_api = IQOption(IQ_OPTION_EMAIL, IQ_OPTION_PASSWORD)
        iq_api.connect()
        if TRADE_MODE == "demo":
            iq_api.change_balance("PRACTICE")
        else:
            iq_api.change_balance("REAL")
        logging.info(f"IQ Option connected in {TRADE_MODE} mode.")
    except Exception as e:
        logging.error(f"IQ Option connection error: {e}")
        iq_api = None

def place_iq_option_trade(api, symbol, direction, amount):
    """Place an IQ Option trade with fixed 5-minute expiry."""
    if api is None:
        return None
    asset = symbol.replace("=X", "")  # e.g., "EURUSD"
    # Some assets may need "-OTC" suffix; adjust if needed.
    try:
        expiry_seconds = TRADE_EXPIRATION_MINUTES * 60
        result = api.buy(amount, asset, direction, expiry_seconds)
        return result
    except Exception as e:
        logging.error(f"IQ Option trade error: {e}")
        return None

def check_trade_result(api, symbol, direction):
    """
    Try to get the result of a closed trade for the given symbol and direction.
    Returns: "WIN" or "LOSS" if found, else None.
    """
    if api is None:
        return None
    asset = symbol.replace("=X", "")
    try:
        # Get closed positions from the last 10 minutes
        if hasattr(api, 'get_position_history'):
            positions = api.get_position_history()
        else:
            positions = api.get_all_open_orders()
        if not positions:
            return None
        for pos in positions:
            if pos.get('asset') == asset and pos.get('direction') == direction.lower():
                profit = float(pos.get('profit', 0))
                status = pos.get('status', '').lower()
                if 'win' in status or 'closed' in status:
                    return "WIN" if profit > 0 else "LOSS"
        return None
    except Exception as e:
        logging.error(f"Error checking trade result for {symbol}: {e}")
        return None

# -------------------- Persistence for Trade Settings --------------------
def load_trade_settings():
    global DEFAULT_STAKE, pair_trade_settings
    try:
        with open("trade_settings.json", "r") as f:
            data = json.load(f)
            DEFAULT_STAKE = data.get("stake", DEFAULT_STAKE)
            pair_trade_settings = data.get("pairs", {})
    except FileNotFoundError:
        pass

def save_trade_settings():
    data = {
        "stake": DEFAULT_STAKE,
        "pairs": pair_trade_settings
    }
    with open("trade_settings.json", "w") as f:
        json.dump(data, f, indent=2)

# -------------------- Helper: get effective settings --------------------
def get_effective_settings(symbol):
    if symbol in pair_settings:
        return {
            "rsi_buy_min": pair_settings[symbol].get("rsi_buy_min", DEFAULT_RSI_BUY_MIN),
            "rsi_buy_max": pair_settings[symbol].get("rsi_buy_max", DEFAULT_RSI_BUY_MAX),
            "rsi_sell_min": pair_settings[symbol].get("rsi_sell_min", DEFAULT_RSI_SELL_MIN),
            "rsi_sell_max": pair_settings[symbol].get("rsi_sell_max", DEFAULT_RSI_SELL_MAX),
            "rsi_1m_buy_min": pair_settings[symbol].get("rsi_1m_buy_min", DEFAULT_RSI_1M_BUY_MIN),
            "rsi_1m_buy_max": pair_settings[symbol].get("rsi_1m_buy_max", DEFAULT_RSI_1M_BUY_MAX),
            "rsi_1m_sell_min": pair_settings[symbol].get("rsi_1m_sell_min", DEFAULT_RSI_1M_SELL_MIN),
            "rsi_1m_sell_max": pair_settings[symbol].get("rsi_1m_sell_max", DEFAULT_RSI_1M_SELL_MAX)
        }
    else:
        return {
            "rsi_buy_min": DEFAULT_RSI_BUY_MIN,
            "rsi_buy_max": DEFAULT_RSI_BUY_MAX,
            "rsi_sell_min": DEFAULT_RSI_SELL_MIN,
            "rsi_sell_max": DEFAULT_RSI_SELL_MAX,
            "rsi_1m_buy_min": DEFAULT_RSI_1M_BUY_MIN,
            "rsi_1m_buy_max": DEFAULT_RSI_1M_BUY_MAX,
            "rsi_1m_sell_min": DEFAULT_RSI_1M_SELL_MIN,
            "rsi_1m_sell_max": DEFAULT_RSI_1M_SELL_MAX
        }

def get_effective_trade_settings(symbol):
    if symbol in pair_trade_settings:
        return {"stake": pair_trade_settings[symbol].get("stake", DEFAULT_STAKE)}
    else:
        return {"stake": DEFAULT_STAKE}

# -------------------- Inline Keyboards (with Check IQ) --------------------
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
        InlineKeyboardButton("🔌 Check IQ", callback_data="check_iq"),
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

# -------------------- Spread & Filters (unchanged) --------------------
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

# -------------------- Strategy Functions (unchanged) --------------------
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
        rsi_1m_val = rsi_1m.iloc[-1]

        is_bull = (prev_diff_before < 0) and (prev_diff > 0) and is_1m_bull
        is_bear = (prev_diff_before > 0) and (prev_diff < 0) and is_1m_bear

        if is_bull and settings["rsi_buy_min"] <= rsi_val <= settings["rsi_buy_max"] \
                and settings["rsi_1m_buy_min"] <= rsi_1m_val <= settings["rsi_1m_buy_max"]:
            return "BUY", {"symbol": symbol, "macd_5m": m.iloc[-1], "signal_5m": s.iloc[-1],
                           "diff_5m": prev_diff, "macd_1m": m_1m.iloc[-1], "signal_1m": s_1m.iloc[-1],
                           "diff_1m": diff_1m_series.iloc[-1], "rsi_5m": rsi_val, "rsi_1m": rsi_1m_val}
        elif is_bear and settings["rsi_sell_min"] <= rsi_val <= settings["rsi_sell_max"] \
                and settings["rsi_1m_sell_min"] <= rsi_1m_val <= settings["rsi_1m_sell_max"]:
            return "SELL", {"symbol": symbol, "macd_5m": m.iloc[-1], "signal_5m": s.iloc[-1],
                           "diff_5m": prev_diff, "macd_1m": m_1m.iloc[-1], "signal_1m": s_1m.iloc[-1],
                           "diff_1m": diff_1m_series.iloc[-1], "rsi_5m": rsi_val, "rsi_1m": rsi_1m_val}
        return "NEUTRAL", None
    except Exception as e:
        return None, str(e)

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
        rsi_1m_val = rsi_1m.iloc[-1]

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
        rsi1_buy = settings["rsi_1m_buy_min"] <= rsi_1m_val <= settings["rsi_1m_buy_max"]
        rsi1_sell = settings["rsi_1m_sell_min"] <= rsi_1m_val <= settings["rsi_1m_sell_max"]

        cd = alert_cooldowns.get(symbol, 0)
        cooldown_ok = (time.time() - cd) > 300

        pair_display = symbol.replace("=X", "")
        report = f"📋 *Conditions for {pair_display}*\n\n"
        report += f"Spread filter: {'✅' if spread_ok else '❌'}\n"
        report += f"MACD compression: {'✅' if compression_ok else '❌'}\n"
        report += f"5m MACD cross: {'🟢 Bullish' if bull_5m else '🔴 Bearish' if bear_5m else '❌ None'}\n"
        report += f"Latest 1m cross: {'🟢 Bullish' if bull_1m else '🔴 Bearish' if bear_1m else '❌ None'}\n"
        report += f"5m RSI ({rsi_val:.1f}): {'✅' if (rsi5_buy or rsi5_sell) else '❌'}\n"
        report += f"1m RSI ({rsi_1m_val:.1f}): {'✅' if (rsi1_buy or rsi1_sell) else '❌'}\n"
        report += f"Cooldown: {'✅' if cooldown_ok else '❌'}\n\n"

        if bull_5m and bull_1m and rsi5_buy and rsi1_buy and spread_ok and compression_ok and cooldown_ok:
            report += "🟢 *BUY signal READY*"
        elif bear_5m and bear_1m and rsi5_sell and rsi1_sell and spread_ok and compression_ok and cooldown_ok:
            report += "🔴 *SELL signal READY*"
        else:
            report += "⏳ *No signal ready yet*"

        return report
    except Exception as e:
        return f"❌ Error: {e}"

# -------------------- Scanner Engine (Martingale removed) --------------------
def scanner_engine():
    global alert_cooldowns, signal_log, full_signal_messages, compression_counter, compression_blocked, spread_blocked_5m, pending_trades
    while True:
        if STATE["running"]:
            with data_lock:
                current_pairs = list(STRATEGY_PAIRS)
            random.shuffle(current_pairs)
            for symbol in current_pairs:
                try:
                    # ----- Hybrid Spread Filter -----
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

                    # ----- MACD & RSI Calculation -----
                    df = yf.Ticker(symbol).history(period="5d", interval="5m")
                    if len(df) < 50: continue
                    m, s, h, rsi = calculate_strategy(df)
                    prev_diff = m.iloc[-1] - s.iloc[-1]
                    prev_diff_before = m.iloc[-2] - s.iloc[-2]

                    # ----- MACD Compression -----
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

                    # ----- 1m Data -----
                    df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
                    if len(df_1m) < 30: continue
                    m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)
                    diff_1m_series = m_1m - s_1m
                    cross_1m = latest_1m_cross(diff_1m_series)

                    is_1m_bull = (cross_1m == 'bull')
                    is_1m_bear = (cross_1m == 'bear')

                    settings = get_effective_settings(symbol)
                    rsi_val = rsi.iloc[-1]
                    rsi_1m_val = rsi_1m.iloc[-1]

                    confirm_bull = (prev_diff_before < 0) and (prev_diff > 0) and (abs(prev_diff) >= 0.00001) \
                                   and is_1m_bull \
                                   and (settings["rsi_buy_min"] <= rsi_val <= settings["rsi_buy_max"]) \
                                   and (settings["rsi_1m_buy_min"] <= rsi_1m_val <= settings["rsi_1m_buy_max"])

                    confirm_bear = (prev_diff_before > 0) and (prev_diff < 0) and (abs(prev_diff) >= 0.00001) \
                                   and is_1m_bear \
                                   and (settings["rsi_sell_min"] <= rsi_val <= settings["rsi_sell_max"]) \
                                   and (settings["rsi_1m_sell_min"] <= rsi_1m_val <= settings["rsi_1m_sell_max"])

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
                            arrow = "🟥" if direction == "SELL" else "🟩"

                            # Removed Martingale timestamps (1⃣, 2⃣, 3⃣)
                            short_msg = (
                                f"⚡ SIGNAL\n\n"
                                f"{flag1}{flag2} {pair_display}\n"
                                f"Timeframe: M5\n"
                                f"⏱ Expiration: 5 minutes\n"
                                f"⏰ Entry: {entry_time_str}\n"
                                f"{arrow} Direction: {direction}\n"
                                f"1m RSI: {rsi_1m_val:.2f}"
                            )

                            full_msg = (
                                f"{short_msg}\n\n"
                                f"✅ MACD Cross + 1m Cross Confirmed\n\n"
                                f"5m MACD: {m.iloc[-1]:.5f} | Signal: {s.iloc[-1]:.5f}\n"
                                f"5m Diff: {prev_diff:.5f}\n"
                                f"1m MACD: {m_1m.iloc[-1]:.5f} | Signal: {s_1m.iloc[-1]:.5f}\n"
                                f"1m Diff: {diff_1m_series.iloc[-1]:.5f}\n"
                                f"5m RSI: {rsi_val:.2f}\n"
                                f"1m RSI: {rsi_1m_val:.2f}\n\n"
                                f"Status: SAFE (Active Liquidity)"
                            )

                            # ----- Build keyboard with Confirm/Cancel buttons -----
                            kb = InlineKeyboardMarkup(row_width=2)
                            kb.add(
                                InlineKeyboardButton("✅ Confirm Trade", callback_data=f"trade_confirm_{symbol}_{direction}"),
                                InlineKeyboardButton("❌ Cancel", callback_data=f"trade_cancel_{symbol}_{direction}"),
                            )
                            kb.add(
                                InlineKeyboardButton("🔍 More Details", callback_data="showdetails_"),
                                InlineKeyboardButton("📋 Conditions", callback_data=f"cond_{symbol}"),
                                InlineKeyboardButton("📊 Quick Scan", callback_data=f"quick_{symbol}"),
                                InlineKeyboardButton("🔕 Mute 30min", callback_data=f"mute_{symbol}"),
                                InlineKeyboardButton("❌ Remove Pair", callback_data=f"remove_{symbol}"),
                            )

                            try:
                                sent_msg = bot.send_message(CHAT_ID, short_msg, reply_markup=kb, parse_mode="Markdown")
                                full_signal_messages[sent_msg.message_id] = full_msg

                                # Store pending trade details (for confirmation)
                                trade_cfg = get_effective_trade_settings(symbol)
                                pending_trades[sent_msg.message_id] = {
                                    "symbol": symbol,
                                    "direction": direction,
                                    "stake": trade_cfg["stake"]
                                }

                                # Log signal (no martingale job)
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
                                    "rsi_1m": rsi_1m_val,
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

# -------------------- Auto-Result Checker --------------------
def auto_result_checker():
    global pending_expiry_trades, signal_log
    while True:
        now = time.time()
        expired = []
        for msg_id, trade in pending_expiry_trades.items():
            if now >= trade["expiry_time"]:
                expired.append(msg_id)
        for msg_id in expired:
            trade = pending_expiry_trades.pop(msg_id)
            symbol = trade["symbol"]
            direction = trade["direction"]
            result = check_trade_result(iq_api, symbol, direction)
            if result:
                success, _ = record_feedback(msg_id, result, 0, "", "", "")
                if success:
                    logging.info(f"Auto-recorded {result} for {symbol} (msg {msg_id})")
                else:
                    logging.warning(f"Auto-record failed for {symbol} (msg {msg_id})")
            else:
                logging.info(f"Could not auto-check result for {symbol} (msg {msg_id}), will rely on manual feedback if any.")
        time.sleep(30)

# -------------------- Feedback helpers --------------------
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

# -------------------- AI Helper --------------------
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

# -------------------- Loss Interview Flow (unchanged) --------------------
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
                f"5m RSI: {entry['rsi_5m']:.2f}, 1m RSI: {entry['rsi_1m']:.2f}"
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

# -------------------- Settings Handlers --------------------
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
    kb.add(InlineKeyboardButton("📈 RSI Buy (1m)", callback_data="param_rsi_1m_buy"))
    kb.add(InlineKeyboardButton("📉 RSI Sell (1m)", callback_data="param_rsi_1m_sell"))
    kb.add(InlineKeyboardButton("⚡ Enter All RSI", callback_data="param_rsi_all"))
    kb.add(InlineKeyboardButton("💰 Stake", callback_data="param_stake"))
    kb.add(InlineKeyboardButton("⏱️ Expiration", callback_data="param_expiration"))  # read-only
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

    # --- Read-only Expiration ---
    if param == "expiration":
        bot.answer_callback_query(call.id, f"⏱️ Expiration is fixed at {TRADE_EXPIRATION_MINUTES} minutes.", show_alert=True)
        bot.edit_message_text(
            f"⏱️ *Expiration*\n\nThis is fixed at **{TRADE_EXPIRATION_MINUTES} minutes** and cannot be changed via settings.\n"
            f"(You can change the constant `TRADE_EXPIRATION_MINUTES` in the code.)",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )
        return

    # --- Stake ---
    if param == "stake":
        prompt_text = f"Enter new stake amount (in USD). Current:\n"
        if target == "all":
            prompt_text += f"Global: {DEFAULT_STAKE}"
        else:
            settings = get_effective_trade_settings(target)
            prompt_text += f"{target}: {settings['stake']}"
        param_name = "stake"
    else:
        # RSI parameters
        if param == "rsi_buy":
            prompt_text = f"Enter new 5m RSI Buy range as `min-max` (e.g., 30-40). Current:\n"
            if target == "all":
                prompt_text += f"Global: {DEFAULT_RSI_BUY_MIN}-{DEFAULT_RSI_BUY_MAX}"
            else:
                settings = get_effective_settings(target)
                prompt_text += f"{target}: {settings['rsi_buy_min']}-{settings['rsi_buy_max']}"
            param_name = "rsi_buy"
        elif param == "rsi_sell":
            prompt_text = f"Enter new 5m RSI Sell range as `min-max` (e.g., 50-70). Current:\n"
            if target == "all":
                prompt_text += f"Global: {DEFAULT_RSI_SELL_MIN}-{DEFAULT_RSI_SELL_MAX}"
            else:
                settings = get_effective_settings(target)
                prompt_text += f"{target}: {settings['rsi_sell_min']}-{settings['rsi_sell_max']}"
            param_name = "rsi_sell"
        elif param == "rsi_1m_buy":
            prompt_text = f"Enter new 1m RSI Buy range as `min-max` (e.g., 30-40). Current:\n"
            if target == "all":
                prompt_text += f"Global: {DEFAULT_RSI_1M_BUY_MIN}-{DEFAULT_RSI_1M_BUY_MAX}"
            else:
                settings = get_effective_settings(target)
                prompt_text += f"{target}: {settings['rsi_1m_buy_min']}-{settings['rsi_1m_buy_max']}"
            param_name = "rsi_1m_buy"
        elif param == "rsi_1m_sell":
            prompt_text = f"Enter new 1m RSI Sell range as `min-max` (e.g., 50-70). Current:\n"
            if target == "all":
                prompt_text += f"Global: {DEFAULT_RSI_1M_SELL_MIN}-{DEFAULT_RSI_1M_SELL_MAX}"
            else:
                settings = get_effective_settings(target)
                prompt_text += f"{target}: {settings['rsi_1m_sell_min']}-{settings['rsi_1m_sell_max']}"
            param_name = "rsi_1m_sell"
        elif param == "rsi_all":
            if target == "all":
                cur = (f"{DEFAULT_RSI_BUY_MIN}-{DEFAULT_RSI_BUY_MAX}, "
                       f"{DEFAULT_RSI_SELL_MIN}-{DEFAULT_RSI_SELL_MAX}, "
                       f"{DEFAULT_RSI_1M_BUY_MIN}-{DEFAULT_RSI_1M_BUY_MAX}, "
                       f"{DEFAULT_RSI_1M_SELL_MIN}-{DEFAULT_RSI_1M_SELL_MAX}")
            else:
                settings = get_effective_settings(target)
                cur = (f"{settings['rsi_buy_min']}-{settings['rsi_buy_max']}, "
                       f"{settings['rsi_sell_min']}-{settings['rsi_sell_max']}, "
                       f"{settings['rsi_1m_buy_min']}-{settings['rsi_1m_buy_max']}, "
                       f"{settings['rsi_1m_sell_min']}-{settings['rsi_1m_sell_max']}")
            prompt_text = f"Enter all four RSI ranges as: buy5m, sell5m, buy1m, sell1m\nExample: 30-40, 50-70, 30-40, 50-70\n\nCurrent:\n{cur}"
            param_name = "rsi_all"
        else:
            bot.answer_callback_query(call.id, "Unknown parameter")
            return

    state["param"] = param_name
    bot.edit_message_text(prompt_text, call.message.chat.id, call.message.message_id)
    msg = bot.send_message(call.message.chat.id, "Please reply with the new value:")
    bot.register_next_step_handler(msg, process_settings_value)
    bot.answer_callback_query(call.id)

def process_settings_value(message):
    global DEFAULT_RSI_BUY_MIN, DEFAULT_RSI_BUY_MAX
    global DEFAULT_RSI_SELL_MIN, DEFAULT_RSI_SELL_MAX
    global DEFAULT_RSI_1M_BUY_MIN, DEFAULT_RSI_1M_BUY_MAX
    global DEFAULT_RSI_1M_SELL_MIN, DEFAULT_RSI_1M_SELL_MAX
    global pair_settings, DEFAULT_STAKE, pair_trade_settings

    chat_id = message.chat.id
    if chat_id not in settings_state:
        return
    state = settings_state[chat_id]
    target = state["target"]
    param = state.get("param")
    value = message.text.strip()

    try:
        # --- Stake handling ---
        if param == "stake":
            val = float(value)
            if val <= 0:
                raise ValueError("Stake must be positive")
            if target == "all":
                DEFAULT_STAKE = val
            else:
                if target not in pair_trade_settings:
                    pair_trade_settings[target] = {}
                pair_trade_settings[target]["stake"] = val
            save_trade_settings()
            bot.reply_to(message, f"✅ Stake set to {val} for {target}")
            settings_state.pop(chat_id, None)
            return

        # --- RSI handling ---
        if param == "rsi_all":
            parts = [p.strip() for p in value.split(',')]
            if len(parts) != 4:
                raise ValueError("Please provide 4 ranges separated by commas")
            ranges = []
            for p in parts:
                minmax = p.split('-')
                if len(minmax) != 2:
                    raise ValueError("Each range must be min-max")
                mn = int(minmax[0].strip())
                mx = int(minmax[1].strip())
                if mn < 0 or mn > 100 or mx < 0 or mx > 100 or mn >= mx:
                    raise ValueError("Invalid range")
                ranges.append((mn, mx))
            buy5m, sell5m, buy1m, sell1m = ranges

            if target == "all":
                DEFAULT_RSI_BUY_MIN, DEFAULT_RSI_BUY_MAX = buy5m
                DEFAULT_RSI_SELL_MIN, DEFAULT_RSI_SELL_MAX = sell5m
                DEFAULT_RSI_1M_BUY_MIN, DEFAULT_RSI_1M_BUY_MAX = buy1m
                DEFAULT_RSI_1M_SELL_MIN, DEFAULT_RSI_1M_SELL_MAX = sell1m
                bot.reply_to(message, "✅ Global RSI settings updated.")
            else:
                if target not in pair_settings:
                    pair_settings[target] = {}
                pair_settings[target].update({
                    "rsi_buy_min": buy5m[0], "rsi_buy_max": buy5m[1],
                    "rsi_sell_min": sell5m[0], "rsi_sell_max": sell5m[1],
                    "rsi_1m_buy_min": buy1m[0], "rsi_1m_buy_max": buy1m[1],
                    "rsi_1m_sell_min": sell1m[0], "rsi_1m_sell_max": sell1m[1]
                })
                bot.reply_to(message, f"✅ {target} RSI settings updated.")

        elif param in ("rsi_buy", "rsi_sell", "rsi_1m_buy", "rsi_1m_sell"):
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
                elif param == "rsi_1m_buy":
                    DEFAULT_RSI_1M_BUY_MIN = min_val
                    DEFAULT_RSI_1M_BUY_MAX = max_val
                elif param == "rsi_1m_sell":
                    DEFAULT_RSI_1M_SELL_MIN = min_val
                    DEFAULT_RSI_1M_SELL_MAX = max_val
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
                elif param == "rsi_1m_buy":
                    pair_settings[target]["rsi_1m_buy_min"] = min_val
                    pair_settings[target]["rsi_1m_buy_max"] = max_val
                elif param == "rsi_1m_sell":
                    pair_settings[target]["rsi_1m_sell_min"] = min_val
                    pair_settings[target]["rsi_1m_sell_max"] = max_val
                bot.reply_to(message, f"✅ {target} {param} set to {min_val}-{max_val}")

        else:
            bot.reply_to(message, "Unknown parameter.")
    except Exception as e:
        bot.reply_to(message, f"❌ Invalid input: {e}. Please try again.")
        settings_state.pop(chat_id, None)
        return

    settings_state.pop(chat_id, None)
    bot.send_message(chat_id, "Settings updated.", reply_markup=get_main_menu())

# -------------------- Main Callback Handler --------------------
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if str(call.message.chat.id) != CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return
    data = call.data
    msg_id = call.message.message_id

    try:
        # --- Check IQ Option Connection ---
        if data == "check_iq":
            if iq_api:
                try:
                    balance = iq_api.get_balance()
                    mode = "🔴 REAL" if TRADE_MODE == "real" else "🟢 DEMO"
                    msg = (
                        f"✅ *IQ Option Connected*\n\n"
                        f"Mode: {mode}\n"
                        f"Balance: ${balance:.2f}\n"
                        f"Status: Active"
                    )
                except Exception as e:
                    msg = (
                        f"⚠️ *IQ Option Client Exists*\n"
                        f"Connection seems alive, but balance check failed.\n"
                        f"Error: {e}\n\n"
                        f"Try restarting the bot."
                    )
            else:
                msg = (
                    f"❌ *IQ Option NOT Connected*\n\n"
                    f"Possible reasons:\n"
                    f"• Credentials not set (IQ_OPTION_EMAIL / PASSWORD)\n"
                    f"• Library not installed (iqoptionapi)\n"
                    f"• Invalid credentials\n"
                    f"• Network error on startup\n\n"
                    f"Check Render logs for details."
                )
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        # --- Trade Confirmation (IQ Only) ---
        if data.startswith("trade_confirm_"):
            parts = data.split("_")  # ["trade", "confirm", "symbol", "direction"]
            if len(parts) >= 4:
                symbol = parts[2]
                direction = parts[3]  # "BUY" or "SELL"
                trade_info = pending_trades.get(msg_id)
                if not trade_info:
                    bot.answer_callback_query(call.id, "⚠️ Trade details expired or not found.", show_alert=True)
                    return
                stake = trade_info["stake"]
                dir_opt = "call" if direction == "BUY" else "put"

                if iq_api:
                    result = place_iq_option_trade(iq_api, symbol, dir_opt, stake)
                    if result:
                        expiry_time = time.time() + (TRADE_EXPIRATION_MINUTES * 60)
                        pending_expiry_trades[msg_id] = {
                            "symbol": symbol,
                            "direction": direction,
                            "expiry_time": expiry_time,
                            "stake": stake,
                            "msg_id": msg_id
                        }
                        bot.answer_callback_query(call.id, f"✅ Trade placed for {symbol} ({direction})")
                        bot.edit_message_reply_markup(call.message.chat.id, msg_id, reply_markup=None)
                        pending_trades.pop(msg_id, None)
                        logging.info(f"IQ Option trade placed: {symbol} {direction} stake={stake}")
                    else:
                        bot.answer_callback_query(call.id, "❌ Trade failed. Check logs.", show_alert=True)
                else:
                    bot.answer_callback_query(call.id, "❌ IQ Option not connected. Check credentials.", show_alert=True)
            return

        # --- Trade Cancel ---
        if data.startswith("trade_cancel_"):
            pending_trades.pop(msg_id, None)
            pending_expiry_trades.pop(msg_id, None)
            bot.answer_callback_query(call.id, "❌ Trade cancelled.")
            bot.edit_message_reply_markup(call.message.chat.id, msg_id, reply_markup=None)
            return

        # --- Manual Win/Loss (override) ---
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
            return

        if data.startswith("loss_interview_"):
            start_loss_interview(call.message.chat.id, msg_id)
            bot.answer_callback_query(call.id, "Answer the question below to record loss details.")
            return

        # --- Other callbacks ---
        if data.startswith("fbdetails_"):
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
            return

        if data.startswith("showdetails_") or data.startswith("hidedetails_"):
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
            return

        # --- All remaining callbacks (unchanged) ---
        if data == "signal_conditions":
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(
                InlineKeyboardButton("🔍 Choose from list", callback_data="cond_list"),
                InlineKeyboardButton("✏️ Enter pair manually", callback_data="cond_manual"),
                InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")
            )
            bot.edit_message_text("📋 *Check Signal Conditions*\nChoose a method:", call.message.chat.id,
                                  call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "cond_list":
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
            kb = InlineKeyboardMarkup(row_width=2)
            for pair in pairs[:10]:
                kb.add(InlineKeyboardButton(pair, callback_data=f"cond_{pair}"))
            kb.add(InlineKeyboardButton("🔙 Back", callback_data="signal_conditions"))
            bot.edit_message_text("Select a pair:", call.message.chat.id, call.message.message_id,
                                  reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "cond_manual":
            msg = bot.send_message(call.message.chat.id,
                                   "Please type the pair symbol (e.g., EURUSD=X, GBPJPY=X):",
                                   parse_mode="Markdown")
            bot.register_next_step_handler(msg, process_cond_manual)
            bot.answer_callback_query(call.id)
            return

        if data.startswith("cond_"):
            pair = data[5:]
            report = diagnose_pair(pair)
            bot.send_message(call.message.chat.id, report, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Conditions checked")
            return

        if data == "quick_scan":
            with data_lock: pairs = list(STRATEGY_PAIRS[:5])
            bot.send_message(call.message.chat.id, "🔍 *Quick scanning...*", parse_mode="Markdown")
            results = []
            for pair in pairs:
                direction, result = quick_scan_single(pair)
                if direction and direction != "NEUTRAL":
                    icon = "🟢" if direction == "BUY" else "🔴"
                    r = result if isinstance(result, dict) else None
                    if r: results.append(f"{icon} {pair}: {direction} (RSI5m: {r['rsi_5m']:.1f}, RSI1m: {r['rsi_1m']:.1f})")
            msg = "📊 *Quick Scan Results:*\n" + ("\n".join(results) if results else "No signals found.")
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="quick_scan"))
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.send_message(call.message.chat.id, msg, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Scan complete")
            return

        if data.startswith("quick_") and data != "quick_scan":
            pair = data.replace("quick_", "")
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
                       f"1m RSI: {result['rsi_1m']:.2f}\n"
                       f"Signal: {direction}")
            else:
                msg = f"❌ Error: {result}"
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Rescan", callback_data=f"quick_{pair}"))
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.send_message(call.message.chat.id, msg, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Rescan complete")
            return

        if data == "chat_start":
            chat_mode[call.message.chat.id] = "chat"
            bot.send_message(call.message.chat.id, "💬 *Chat mode activated*\nType your message (or /cancel to exit).", parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Chat mode on")
            return

        if data == "debug_start":
            chat_mode[call.message.chat.id] = "debug"
            bot.send_message(call.message.chat.id, "🐛 *Debug mode activated*\nPaste signal details for analysis (or /cancel to exit).", parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Debug mode on")
            return

        if data == "test_ai":
            answer = ask_ai_core("Reply with 'AI Core is connected and ready.' Keep it very short.")
            bot.answer_callback_query(call.id, answer[:200], show_alert=True)
            return

        if data == "main_menu":
            bot.edit_message_text("📋 *Main Menu*", call.message.chat.id, call.message.message_id,
                                  reply_markup=get_main_menu(), parse_mode="Markdown")
            return

        if data == "status":
            blocked_count = len(spread_blocked_5m) + sum(compression_blocked.values())
            status_text = f"🟢 Scanner: {'RUNNING' if STATE['running'] else 'PAUSED'}\n📊 Pairs: {len(STRATEGY_PAIRS)}\n🚫 Blocked: {blocked_count}"
            kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(status_text, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        if data == "blocked_list":
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
            return

        if data == "watchlist" or data.startswith("page_info_"):
            page = int(data.split("_")[-1]) if data.startswith("page_info_") else 0
            with data_lock: pairs = list(STRATEGY_PAIRS)
            kb = get_pairs_keyboard(pairs, "info", page)
            bot.edit_message_text(f"📋 *Watchlist ({len(pairs)} pairs)*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            return

        if data == "remove_menu" or data.startswith("page_remove_"):
            page = int(data.split("_")[-1]) if data.startswith("page_remove_") else 0
            with data_lock: pairs = list(STRATEGY_PAIRS)
            if not pairs:
                bot.edit_message_text("📭 No pairs to remove.", call.message.chat.id, call.message.message_id, reply_markup=get_main_menu())
            else:
                kb = get_pairs_keyboard(pairs, "remove", page)
                bot.edit_message_text("❌ *Select pair to remove:*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            return

        if data == "add_menu":
            bot.edit_message_text("➕ *Add a pair:*\nSend /add SYMBOL or choose below:", call.message.chat.id, call.message.message_id, reply_markup=get_add_suggestions(), parse_mode="Markdown")
            return

        if data.startswith("add_"):
            pair = data.replace("add_", "")
            ticker = yf.Ticker(pair)
            if len(ticker.history(period="1d")) > 0:
                with data_lock:
                    if pair not in STRATEGY_PAIRS: STRATEGY_PAIRS.append(pair)
                bot.answer_callback_query(call.id, f"✅ {pair} added!")
            else:
                bot.answer_callback_query(call.id, f"❌ {pair} not found", show_alert=True)
            return

        if data.startswith("remove_"):
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
            return

        if data == "start_scanner":
            STATE["running"] = True
            bot.answer_callback_query(call.id, "✅ Scanner started!")
            return

        if data == "pause_scanner":
            STATE["running"] = False
            bot.answer_callback_query(call.id, "⏸️ Scanner paused!")
            return

        if data.startswith("mute_"):
            pair = data.replace("mute_", "")
            alert_cooldowns[pair] = time.time() + 1800
            bot.answer_callback_query(call.id, f"🔕 {pair} muted for 30 min")
            return

        if data.startswith("info_"):
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
                       f"1m RSI: {result['rsi_1m']:.2f}\n"
                       f"Signal: {direction}")
            else: msg = f"❌ Error: {result}"
            kb = InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔄 Refresh", callback_data=f"info_{pair}"),
                InlineKeyboardButton("🔙 Watchlist", callback_data="watchlist"))
            bot.send_message(call.message.chat.id, msg, reply_markup=kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Pair info loaded")
            return

        if data == "stats_page":
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
            return

        if data == "entry_menu":
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
            return

        if data.startswith("entry_"):
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
            return

        if data == "help":
            help_text = (
                "🤖 *Forex Scanner Bot*\n\n"
                "• 💬 Chat: ask me anything\n"
                "• 🐛 Debug: analyze a signal\n"
                "• 🧪 Test AI: check connection\n"
                "• ⚙️ Settings: adjust RSI (5m & 1m), stake\n"
                "• 📋 Conditions: live checklist\n"
                "• 📈 Stats / ⏱️ Entry\n\n"
                "RSI and stake are configurable per pair."
            )
            kb = InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            return

        logging.warning(f"Unknown callback data: {data}")
        bot.answer_callback_query(call.id, "Unknown action", show_alert=True)

    except Exception as e:
        logging.error(f"Callback error: {e}")
        bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)

# -------------------- Process manual condition check --------------------
def process_cond_manual(message):
    if str(message.chat.id) != CHAT_ID:
        return
    pair = message.text.strip().upper()
    if "=X" not in pair:
        pair += "=X"
    report = diagnose_pair(pair)
    bot.reply_to(message, report, parse_mode="Markdown")

# -------------------- Old feedback reply handler --------------------
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

# -------------------- Message Handlers --------------------
@bot.message_handler(commands=['start'])
def start(m):
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(m.chat.id, "🚀 *Forex Scanner Online*\nUse the buttons below.",
                         reply_markup=get_main_menu(), parse_mode="Markdown")

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

# --- Loss interview handler ---
@bot.message_handler(func=lambda m: str(m.chat.id) == CHAT_ID and m.chat.id in loss_interview_state)
def loss_interview_handler(m):
    state = loss_interview_state.get(m.chat.id)
    if not state: return
    msg_id = state["msg_id"]
    step = state["step"]
    if step == "timing":
        process_loss_timing(m, msg_id)

# --- Chat/Debug mode handler ---
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

# --- Quick commands for stake ---
@bot.message_handler(commands=['setstake'])
def set_stake_cmd(m):
    if str(m.chat.id) != CHAT_ID: return
    parts = m.text.split()
    if len(parts) != 2:
        bot.reply_to(m, "Usage: /setstake <amount>")
        return
    try:
        val = float(parts[1])
        if val <= 0:
            bot.reply_to(m, "Stake must be positive.")
            return
        global DEFAULT_STAKE
        DEFAULT_STAKE = val
        save_trade_settings()
        bot.reply_to(m, f"✅ Global stake set to {val}")
    except ValueError:
        bot.reply_to(m, "Invalid amount. Use a number like 10.5")

# -------------------- Main Entry --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    load_trade_settings()
    init_iq_option()
    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=auto_result_checker, daemon=True).start()
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    print(f"Bot and Web Server starting on port {port}...")
    app.run(host="0.0.0.0", port=port)
