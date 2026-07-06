import telebot
from datetime import datetime
from config import TOKEN, CHAT_ID

# Initialize Unified Telebot Engine
sync_bot = telebot.TeleBot(TOKEN)

def send_telegram_signal(msg):
    """
    Core routing mechanism to dispatch formatted text signals directly to Telegram.
    """
    try:
        sync_bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
        print("Telegram notification dispatched successfully.")
    except Exception as e:
        print(f"Signal Routing Error: {e}")

def format_and_send_trade_signal(pair, price, rsi, gap, direction):
    """
    Formats raw strategy metrics and current timestamp into an easily 
    readable Telegram alert and sends it via the core routing mechanism.
    """
    # Capture the exact time the signal was fired
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Assign styling based on trade direction
    if direction.upper() == "BUY":
        emoji = "🟢"
        title = "STRATEGY BUY SIGNAL"
    elif direction.upper() == "SELL":
        emoji = "🔴"
        title = "STRATEGY SELL SIGNAL"
    else:
        print(f"Unknown signal direction: {direction}")
        return

    # Build the Markdown string safely with the Timestamp included
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

    # Route to Telegram
    send_telegram_signal(msg)
