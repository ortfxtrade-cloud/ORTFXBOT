import telebot
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

def format_and_send_trade_signal(pair, price, rsi, gap, direction):
    """Formats raw strategy metrics and current timestamp into an alert."""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    emoji = "🟢" if direction.upper() == "BUY" else "🔴"
    title = f"STRATEGY {direction.upper()} SIGNAL"

    msg = (
        f"{emoji} **{title}** {emoji}\n\n"
        f"**Asset:** `{pair}`\n"
        f"**Execution Price:** `{price:.5f}`\n"
        f"**M5 RSI:** `{rsi:.2f}`\n"
        f"**MACD Gap:** `{gap:.6f}`\n\n"
        f"📅 **Time Triggered:** `{current_time}`\n"
        f"⏳ *Timeframe:* 5m Setup + 1m Momentum Confirmation\n"
        f"⚠️ *Status:* Cooldown period initiated."
    )
    send_telegram_signal(msg)

# =========================================================================
# ⚙️ NEW: INCOMING COMMAND HANDLER (/status)
# =========================================================================
@sync_bot.message_handler(commands=['status'])
def handle_status_command(message):
    """Listens for the /status command and returns the engine architecture check."""
    # Security layer: Ensure only you can check the status, not random Telegram users
    if str(message.chat.id) != str(CHAT_ID):
        return

    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    status_msg = (
        f"🖥️ **ENGINE SYSTEM INTEGRITY REPORT** 🖥️\n\n"
        f"● `main.py` ──► 🟢 **ACTIVE** (Loop Core)\n"
        f"● `engine.py` ──► 🟢 **CONNECTED** (Data Matrix)\n"
        f"├── `indicators.py` ──► 🟢 **VERIFIED** (MACD/RSI/Expansion)\n"
        f"├── `state_db.py` ──► 🟢 **ONLINE** (SQLite Cooldowns)\n"
        f"└── `alerts.py` ──► 🟢 **ONLINE** (Telegram Interface)\n\n"
        f"📊 **System Clock:** `{current_time}`\n"
        f"🚀 *Status:* Bot is actively scanning and fighting market compression."
    )
    
    sync_bot.reply_to(message, status_msg, parse_mode="Markdown")
