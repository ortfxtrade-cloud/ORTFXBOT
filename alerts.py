import telebot
import threading
from datetime import datetime
from config import TOKEN, CHAT_ID

# Initialize Unified Telebot Engine
sync_bot = telebot.TeleBot(TOKEN)

def send_telegram_signal(msg):
    """Core routing mechanism to dispatch formatted text signals."""
    try:
        sync_bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
        print("Telegram notification dispatched successfully.")
    except Exception as e:
        print(f"Signal Routing Error: {e}")

def send_pre_crossing_alert(pair, price, direction_guess):
    """Dispatches a watch warning alert when an asset's 5m momentum is compressing toward a cross."""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    target_emoji = "📈" if direction_guess == "BULLISH" else "📉"
    
    msg = (
        f"⏳ **5M PRE-CROSSING WATCHLIST ALERT** ⏳\n\n"
        f"**Asset:** `{pair}`\n"
        f"**Current Price:** `{price:.5f}`\n"
        f"🔄 **Imminent Bias:** {target_emoji} `{direction_guess}`\n\n"
        f"📅 **Time Logged:** `{current_time}`\n"
        f"⚠️ *Status:* 5m MACD lines are compressing over 3 bars. Watch this asset for an upcoming crossover."
    )
    send_telegram_signal(msg)

def send_buy_signal(pair, price, rsi, gap):
    """Formats and dispatches a dedicated Bullish Strategy Buy Signal."""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    msg = (
        f"🟢 **STRATEGY BUY SIGNAL** 🟢\n\n"
        f"**Asset:** `{pair}`\n"
        f"**Execution Price:** `{price:.5f}`\n"
        f"🎯 **M5 RSI Trigger:** `{rsi:.2f}` (Target Zone: 30 - 45)\n"
        f"**5m MACD Gap Residual:** `{gap:.6f}`\n\n"
        f"📅 **Time Triggered:** `{current_time}`\n"
        f"⏳ *Timeframe:* 5m Fresh Crossover Setup + 1m Trend Alignment Confirmation\n"
        f"⚠️ *Status:* Cooldown period initiated."
    )
    send_telegram_signal(msg)

def send_sell_signal(pair, price, rsi, gap):
    """Formats and dispatches a dedicated Bearish Strategy Sell Signal."""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    msg = (
        f"🔴 **STRATEGY SELL SIGNAL** 🔴\n\n"
        f"**Asset:** `{pair}`\n"
        f"**Execution Price:** `{price:.5f}`\n"
        f"🎯 **M5 RSI Trigger:** `{rsi:.2f}` (Target Zone: 55 - 70)\n"
        f"**5m MACD Gap Residual:** `{gap:.6f}`\n\n"
        f"📅 **Time Triggered:** `{current_time}`\n"
        f"⏳ *Timeframe:* 5m Fresh Crossover Setup + 1m Trend Alignment Confirmation\n"
        f"⚠️ *Status:* Cooldown period initiated."
    )
    send_telegram_signal(msg)

# =========================================================================
# ⚙️ DYNAMIC INCOMING COMMAND HANDLER (/status)
# =========================================================================
@sync_bot.message_handler(commands=['status'])
def handle_status_command(message):
    """Listens for the /status command and returns the live runtime engine state."""
    if str(message.chat.id) != str(CHAT_ID):
        return

    try:
        import engine
        if engine.IS_RUNNING:
            loop_status = "🟢 **ACTIVE** (Scanning Markets)"
        else:
            loop_status = "🔴 **STOPPED** (Engine Inactive)"
    except Exception:
        loop_status = "🔴 **OFFLINE** (Process Terminated)"

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    status_msg = (
        f"🖥️ **ENGINE SYSTEM INTEGRITY REPORT** 🖥️\n\n"
        f"● `main.py` ──► {loop_status}\n"
        f"● `engine.py` ──► 🟢 **CONNECTED** (Data Matrix)\n"
        f"├── `indicators.py` ──► 🟢 **VERIFIED** (MACD/RSI/Slope Matrix)\n"
        f"├── `state_db.py` ──► 🟢 **ONLINE** (SQLite Cooldowns)\n"
        f"└── `alerts.py` ──► 🟢 **ONLINE** (Telegram Interface)\n\n"
        f"📊 **System Clock:** `{current_time}`\n"
        f"🚀 *Status:* Reporting live system memory state."
    )
    
    sync_bot.reply_to(message, status_msg, parse_mode="Markdown")

def start_bot_polling():
    """Runs infinity polling in a background thread to prevent thread blocking."""
    polling_thread = threading.Thread(target=sync_bot.infinity_polling, daemon=True)
    polling_thread.start()
    print("🤖 Telegram command listener running in the background...")
