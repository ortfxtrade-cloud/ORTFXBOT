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
TELEGRAM_TOKEN = "8686769653:AAEUvYlVgCAv9Rn1jL82aNl6wTxk1-w7g3Q"
CHAT_ID = "8701685996"
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# Flask setup
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
STATE = {"running": True}
alert_cooldowns = {}
spread_blocked = {}

logging.basicConfig(level=logging.INFO)

@app.route('/')
def health_check():
    return "Bot is running!"

# --- Inline Keyboards ---
def get_main_menu():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Status", callback_data="status"),
        InlineKeyboardButton("📋 Watchlist", callback_data="watchlist"),
        InlineKeyboardButton("🚫 Blocked", callback_data="blocked_list"),
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

# --- Spread Detection (5-Minute Timeframe) ---
def is_spread_present(symbol):
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
        
        conditions_met = sum([condition1, condition2, condition3])
        
        if conditions_met >= 2:
            return True
        
        return False
        
    except Exception as e:
        logging.error(f"Spread check error for {symbol}: {e}")
        return False

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
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="5m")
        if len(df) < 50:
            return None, "Insufficient data"
        
        m, s, h, rsi = calculate_strategy(df)
        prev_diff = m.iloc[-1] - s.iloc[-1]
        prev_diff_before = m.iloc[-2] - s.iloc[-2]
        
        df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
        m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)
        diff_1m_now = m_1m.iloc[-1] - s_1m.iloc[-1]
        
        # Dynamic gap for quick scan
        recent_1m_diffs = abs(m_1m.tail(20) - s_1m.tail(20))
        avg_1m_diff = recent_1m_diffs.mean()
        min_gap = max(avg_1m_diff * 0.15, 0.0000001)
        
        is_1m_strong = diff_1m_now > min_gap if diff_1m_now > 0 else diff_1m_now < -min_gap
        
        is_bull = (prev_diff_before < 0) and (prev_diff > 0) and is_1m_strong
        is_bear = (prev_diff_before > 0) and (prev_diff < 0) and is_1m_strong
        
        result = {
            "symbol": symbol,
            "macd_5m": m.iloc[-1],
            "signal_5m": s.iloc[-1],
            "diff_5m": prev_diff,
            "macd_1m": m_1m.iloc[-1],
            "signal_1m": s_1m.iloc[-1],
            "diff_1m": diff_1m_now,
            "rsi_5m": rsi.iloc[-1],
            "min_gap": min_gap,
        }
        
        if is_bull and 30 <= rsi.iloc[-1] <= 45:
            return "BUY", result
        elif is_bear and 55 <= rsi.iloc[-1] <= 70:
            return "SELL", result
        
        return "NEUTRAL", result
    except Exception as e:
        return None, str(e)

def scanner_engine():
    global alert_cooldowns, spread_blocked
    while True:
        if STATE["running"]:
            with data_lock:
                current_pairs = list(STRATEGY_PAIRS)

            random.shuffle(current_pairs)

            for symbol in current_pairs:
                try:
                    # ============ SPREAD FILTER (5-MINUTE CHECK) ============
                    if symbol in spread_blocked:
                        block_end_time = spread_blocked[symbol]
                        if time.time() < block_end_time:
                            continue
                        else:
                            del spread_blocked[symbol]
                            spread_msg = f"✅ *SPREAD OVER* {symbol}\nSignals resumed after 1 hour"
                            try:
                                bot.send_message(CHAT_ID, spread_msg, parse_mode="Markdown")
                            except:
                                bot.send_message(CHAT_ID, spread_msg, parse_mode=None)
                    
                    if is_spread_present(symbol):
                        if symbol not in spread_blocked:
                            spread_blocked[symbol] = time.time() + 3600
                            spread_msg = f"🚫 *SPREAD DETECTED* {symbol}\nSignals blocked for 1 hour"
                            try:
                                bot.send_message(CHAT_ID, spread_msg, parse_mode="Markdown")
                            except:
                                bot.send_message(CHAT_ID, spread_msg, parse_mode=None)
                        continue
                    # =========================================================
                    
                    # 5-minute data
                    df = yf.Ticker(symbol).history(period="5d", interval="5m")
                    if len(df) < 50:
                        continue

                    m, s, h, rsi = calculate_strategy(df)

                    prev_diff = m.iloc[-1] - s.iloc[-1]
                    prev_diff_before = m.iloc[-2] - s.iloc[-2]

                    # Get 1-minute data
                    df_1m = yf.Ticker(symbol).history(period="1d", interval="1m")
                    if len(df_1m) < 30:
                        continue
                    m_1m, s_1m, h_1m, rsi_1m = calculate_strategy(df_1m)

                    # Dynamic 1-minute gap filter
                    diff_1m_now = m_1m.iloc[-1] - s_1m.iloc[-1]
                    recent_1m_diffs = abs(m_1m.tail(20) - s_1m.tail(20))
                    avg_1m_diff = recent_1m_diffs.mean()
                    min_gap = max(avg_1m_diff * 0.15, 0.0000001)
                    
                    is_1m_bullish = diff_1m_now > min_gap
                    is_1m_bearish = diff_1m_now < -min_gap

                    # 5-minute signals
                    pre_bull = (prev_diff_before < 0) and (prev_diff > 0) and (30 <= rsi.iloc[-1] <= 45)
                    pre_bear = (prev_diff_before > 0) and (prev_diff < 0) and (55 <= rsi.iloc[-1] <= 70)

                    # PRE-ALERT: 5m sign change + 1m agrees with gap
                    alert_bull = pre_bull and is_1m_bullish
                    alert_bear = pre_bear and is_1m_bearish

                    # CONFIRMATION: 5m sign change + gap + 1m agrees
                    confirm_bull = alert_bull and (abs(prev_diff) >= 0.00001)
                    confirm_bear = alert_bear and (abs(prev_diff) >= 0.00001)

                    # ⚠️ PRE-ALERT
                    if alert_bull or alert_bear:
                        if time.time() - alert_cooldowns.get(f"pre_{symbol}", 0) > 1800:
                            direction = "BULLISH" if alert_bull else "BEARISH"
                            pre_msg = (
                                f"⚠️ *PRE-ALERT* {symbol}\n\n"
                                f"Direction: {direction}\n"
                                f"✅ 5m & 1m Both Agree!\n\n"
                                f"5m Diff: {prev_diff_before:.5f} → {prev_diff:.5f}\n"
                                f"1m Diff: {diff_1m_now:.5f}\n"
                                f"1m Min Gap: {min_gap:.8f}\n"
                                f"5m RSI: {rsi.iloc[-1]:.2f}\n\n"
                                f"Waiting for candle close..."
                            )
                            try:
                                bot.send_message(CHAT_ID, pre_msg, parse_mode="Markdown")
                            except:
                                bot.send_message(CHAT_ID, pre_msg, parse_mode=None)
                            alert_cooldowns[f"pre_{symbol}"] = time.time()

                    # 🟢/🔴 CONFIRMATION
                    if confirm_bull or confirm_bear:
                        if time.time() - alert_cooldowns.get(symbol, 0) > 300:
                            direction = "BUY" if confirm_bull else "SELL"
                            icon = "🟢" if confirm_bull else "🔴"

                            confirm_msg = (
                                f"{icon} *{direction} CONFIRMED* {symbol}\n\n"
                                f"✅ Candle Closed — 5m & 1m Agree!\n\n"
                                f"5m MACD: {m.iloc[-1]:.5f} | Signal: {s.iloc[-1]:.5f}\n"
                                f"5m Diff: {prev_diff:.5f}\n"
                                f"1m MACD: {m_1m.iloc[-1]:.5f} | Signal: {s_1m.iloc[-1]:.5f}\n"
                                f"1m Diff: {diff_1m_now:.5f}\n"
                                f"1m Min Gap: {min_gap:.8f}\n"
                                f"5m RSI: {rsi.iloc[-1]:.2f}\n\n"
                                f"Status: SAFE (Active Liquidity)"
                            )

                            kb = InlineKeyboardMarkup(row_width=2)
                            kb.add(
                                InlineKeyboardButton(f"📊 Quick Scan {symbol}", callback_data=f"quick_{symbol}"),
                                InlineKeyboardButton("🔕 Mute 30min", callback_data=f"mute_{symbol}"),
                                InlineKeyboardButton("❌ Remove Pair", callback_data=f"remove_{symbol}"),
                            )

                            try:
                                bot.send_message(CHAT_ID, confirm_msg, reply_markup=kb, parse_mode="Markdown")
                            except:
                                bot.send_message(CHAT_ID, confirm_msg, reply_markup=kb, parse_mode=None)
                            alert_cooldowns[symbol] = time.time()

                    time.sleep(1)

                except Exception as e:
                    logging.error(f"Scanner Error for {symbol}: {e}")
                    time.sleep(2)

            time.sleep(30)
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
        if data == "main_menu":
            bot.edit_message_text(
                "📋 *Main Menu*",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=get_main_menu(),
                parse_mode="Markdown"
            )

        elif data == "status":
            blocked_count = len(spread_blocked)
            status_text = f"🟢 Scanner: {'RUNNING' if STATE['running'] else 'PAUSED'}\n📊 Pairs: {len(STRATEGY_PAIRS)}\n🚫 Blocked: {blocked_count}"
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(status_text, call.message.chat.id, call.message.message_id, reply_markup=kb)

        elif data == "blocked_list":
            if not spread_blocked:
                msg = "✅ *No Blocked Pairs*\n\nAll pairs scanning normally."
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
                bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")
            else:
                msg = "🚫 *Blocked Pairs:*\n\n"
                now = time.time()
                for symbol, end_time in spread_blocked.items():
                    remaining = int((end_time - now) / 60)
                    if remaining > 0:
                        msg += f"• {symbol} — {remaining} min remaining\n"
                    else:
                        msg += f"• {symbol} — Expiring soon\n"
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="blocked_list"))
                kb.add(InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
                bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data == "watchlist" or data.startswith("page_info_"):
            page = int(data.split("_")[-1]) if data.startswith("page_info_") else 0
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
            kb = get_pairs_keyboard(pairs, "info", page)
            bot.edit_message_text(f"📋 *Watchlist ({len(pairs)} pairs)*", call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data == "remove_menu" or data.startswith("page_remove_"):
            page = int(data.split("_")[-1]) if data.startswith("page_remove_") else 0
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
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
                    if pair not in STRATEGY_PAIRS:
                        STRATEGY_PAIRS.append(pair)
                bot.answer_callback_query(call.id, f"✅ {pair} added!")
            else:
                bot.answer_callback_query(call.id, f"❌ {pair} not found", show_alert=True)

        elif data.startswith("remove_"):
            pair = data.replace("remove_", "")
            with data_lock:
                if pair in STRATEGY_PAIRS:
                    STRATEGY_PAIRS.remove(pair)
            bot.answer_callback_query(call.id, f"🗑️ {pair} removed!")
            with data_lock:
                pairs = list(STRATEGY_PAIRS)
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

        elif data == "quick_scan":
            with data_lock:
                pairs = list(STRATEGY_PAIRS[:5])
            bot.edit_message_text("🔍 *Quick scanning...*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
            results = []
            for pair in pairs:
                direction, result = quick_scan_single(pair)
                if direction and direction != "NEUTRAL":
                    icon = "🟢" if direction == "BUY" else "🔴"
                    r = result if isinstance(result, dict) else None
                    if r:
                        results.append(f"{icon} {pair}: {direction} (RSI: {r['rsi_5m']:.1f})")
            msg = "📊 *Quick Scan Results:*\n" + ("\n".join(results) if results else "No signals found.")
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Refresh", callback_data="quick_scan"), InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data.startswith("quick_"):
            pair = data.replace("quick_", "")
            bot.edit_message_text(f"🔍 *Scanning {pair}...*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
            direction, result = quick_scan_single(pair)
            if isinstance(result, dict):
                icon = {"BUY": "🟢", "SELL": "🔴"}.get(direction, "⚪")
                msg = (
                    f"{icon} *{pair}*\n\n"
                    f"5m MACD: {result['macd_5m']:.5f} | Signal: {result['signal_5m']:.5f}\n"
                    f"5m Diff: {result['diff_5m']:.5f}\n"
                    f"1m MACD: {result['macd_1m']:.5f} | Signal: {result['signal_1m']:.5f}\n"
                    f"1m Diff: {result['diff_1m']:.5f}\n"
                    f"1m Min Gap: {result['min_gap']:.8f}\n"
                    f"5m RSI: {result['rsi_5m']:.2f}\n"
                    f"Signal: {direction}"
                )
            else:
                msg = f"❌ Error: {result}"
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("🔄 Rescan", callback_data=f"quick_{pair}"), InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode="Markdown")

        elif data.startswith("mute_"):
            pair = data.replace("mute_", "")
            alert_cooldowns[pair] = time.time() + 1800
            bot.answer_callback_query(call.id, f"🔕 {pair} muted for 30 min")

        elif data.startswith("info_"):
            pair = data.replace("info_", "")
            bot.edit_message_text(f"🔍 *Scanning {pair}...*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
            direction, result = quick_scan_single(pair)
            if isinstance(result, dict):
                icon = {"BUY": "🟢", "SELL": "🔴"}.get(direction, "⚪")
                msg = (
                    f"{icon} *{pair}*\n\n"
                    f"5m MACD: {result['macd_5m']:.5f} | Signal: {result['signal_5m']:.5f}\n"
                    f"5m Diff: {result['diff_5m']:.5f}\n"
                    f"1m MACD: {result['macd_1m']:.5f} | Signal: {result['signal_1m']:.5f}\n"
                    f"1m Diff: {result['diff_1m']:.5f}\n"
                    f"1m Min Gap: {result['min_gap']:.8f}\n"
                    f"5m RSI: {result['rsi_5m']:.2f}\n"
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

        elif data == "help":
            help_text = (
                "🤖 *Forex Scanner Bot*\n\n"
                "*Alerts:*\n"
                "⚠️ Pre-Alert: 5m sign change + 1m gap\n"
                "🟢/🔴 Confirmed: Candle closed + gap\n\n"
                "*Filters:*\n"
                "🚫 Spread: 5m check, 1 hour block\n"
                "📏 1m Gap: Dynamic per pair (15% of avg)\n\n"
                "*Strategy:* MACD Crossover + RSI Filter\n"
                "*BUY:* Diff - → + | RSI 30-45\n"
                "*SELL:* Diff + → - | RSI 55-70\n\n"
                "*Commands:*\n"
                "/start - Launch bot\n"
                "/menu - Main menu\n"
                "/add SYMBOL - Add pair\n"
                "/remove SYMBOL - Remove pair"
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
@bot.message_handler(commands=['start'])
def start(m):
    if str(m.chat.id) == CHAT_ID:
        bot.send_message(
            m.chat.id,
            "🚀 *Forex Scanner Online*\n\n"
            "⚠️ Pre-Alert: 5m sign change + 1m gap\n"
            "🟢/🔴 Confirmed: Candle closed + gap\n"
            "🚫 Spread Filter: 5m check, 1 hour block\n"
            "📏 1m Gap Filter: Dynamic per pair\n\n"
            "Select an option:",
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
        blocked_count = len(spread_blocked)
        bot.reply_to(m, f"🟢 Scanner: {'RUNNING' if STATE['running'] else 'PAUSED'}\n📊 Pairs: {len(STRATEGY_PAIRS)}\n🚫 Blocked: {blocked_count}", reply_markup=get_main_menu())
    else:
        with data_lock:
            pairs = list(STRATEGY_PAIRS)
        bot.reply_to(m, f"📋 Watchlist ({len(pairs)}):\n" + "\n".join(pairs), reply_markup=get_main_menu())

# --- Main Entry ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    threading.Thread(target=scanner_engine, daemon=True).start()
    threading.Thread(target=bot.infinity_polling, daemon=True).start()

    print(f"Bot and Web Server starting on port {port}...")
    app.run(host="0.0.0.0", port=port)
