import telebot
from config import TOKEN, CHAT_ID

# Initialize Unified Telebot Engine
sync_bot = telebot.TeleBot(TOKEN)

def send_telegram_signal(msg):
    try:
        sync_bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
    except Exception as e:
        print(f"Signal Routing Error: {e}")
