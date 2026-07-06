
import telebot
import threading
from datetime import datetime, timedelta
from config import TOKEN, CHAT_ID

sync_bot = telebot.TeleBot(TOKEN)

def get_rounded_future_time():
    """
    Adds 5 minutes to the current server time and rounds down 
    to the nearest 5-minute chart block (e.g., :00, :05, :10, :15).
    """
    now = datetime.now() + timedelta(minutes=5)
    discard_minutes = now.minute % 5
    rounded_time = now - timedelta(minutes=discard_minutes, seconds=now.second, microseconds=now.microsecond)
    return rounded_time.strftime("%H:%M")

def send_telegram_signal(msg):
    try:
        sync_bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
        print("Telegram notification dispatched.")
    except Exception as e:
        print(f"Signal Routing Error: {e}")

def send_touching_pre_alert(pair, price, direction_guess):
    time_str = get_rounded_future_time()
    emoji = "⚡" if direction_guess == "BULLISH" else "🚨"
    msg = f"{emoji} **TOUCHING:** `{pair}` @ `{price:.5f}` ({direction_guess}) | 🕒 Expiry Target: `{time_str}`"
    send_telegram_signal(msg)

def send_buy_signal(pair, price, rsi, gap):
    time_str = get_rounded_future_time()
    msg = f"🟢 **BUY LIMIT:** `{pair}` @ `{price:.5f}` | RSI: `{rsi:.1f}` | Gap: `{gap:.5f}` | 🕒 Target: `{time_str}`"
    send_telegram_signal(msg)

def send_sell_signal(pair, price, rsi, gap):
    time_str = get_rounded_future_time()
    msg = f"🔴 **SELL LIMIT:** `{pair}` @ `{price:.5f}` | RSI: `{rsi:.1f}` | Gap: `{gap:.5f}` | 🕒 Target: `{time_str}`"
    send_telegram_signal(msg)

@sync_bot.message_handler(commands=['status'])
def handle_status_command(message):
    if str(message.chat.id) != str(CHAT_ID):
        return
    try:
        # Fixed case-sensitivity for Linux deployments
        import Engine as engine
        loop_status = "🟢 ACTIVE" if engine.IS_RUNNING else "🔴 STOPPED"
    except Exception:
        loop_status = "🔴 OFFLINE"

    msg = f"🖥️ **STATUS:** {loop_status} | {datetime.now().strftime('%H:%M:%S')}"
    sync_bot.reply_to(message, msg, parse_mode="Markdown")

def start_bot_polling():
    polling_thread = threading.Thread(target=sync_bot.infinity_polling, daemon=True)
    polling_thread.start()
    print("🤖 Telegram command listener running...")
