import os
import time
import threading
import requests
from flask import Flask
from config import CHAT_ID, DEPLOY_HOOK, STRATEGY_PAIRS
from state_db import init_db
from alerts import sync_bot
import engine

# --- WEB SERVER FOR RENDER HEALTH CHECKS ---
app = Flask('')

@app.route('/')
def home():
    status = "RUNNING" if engine.IS_RUNNING else "STOPPED"
    return f"System status: {status}. Monitoring market strategies 24/5."

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def strategy_loop():
    print("Strategy scanning mechanism initialized...")
    while True:
        current_day = time.gmtime().tm_wday
        if current_day < 5: 
            if engine.IS_RUNNING:
                for pair in STRATEGY_PAIRS:
                    if not engine.IS_RUNNING: 
                        break
                    try:
                        engine.analyze_ticker(pair)
                    except Exception as e:
                        print(f"Error checking {pair}: {e}")
                    time.sleep(2)
                time.sleep(300)
            else:
                time.sleep(5)
        else:
            time.sleep(3600)

# --- TELEGRAM ADMIN INTERFACE ---
@sync_bot.message_handler(commands=['start_bot'])
def start_bot_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    engine.IS_RUNNING = True
    sync_bot.reply_to(message, "🚀 Technical strategy matrix active. Scanning markets...")

@sync_bot.message_handler(commands=['stop_bot'])
def stop_bot_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    engine.IS_RUNNING = False
    sync_bot.reply_to(message, "🛑 Technical scans paused.")

@sync_bot.message_handler(commands=['status'])
def status_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): return
    state = "🟢 ACTIVE" if engine.IS_RUNNING else "🔴 PAUSED"
    sync_bot.reply_to(message, f"Strategy Scan Status: {state}")

@sync_bot.message_handler(commands=['deploy'])
def deploy_cmd(message):
    if str(message.chat.id) != str(CHAT_ID): 
        return
    if not DEPLOY_HOOK:
        sync_bot.reply_to(message, "❌ Deploy hook missing from environment setup.")
        return
        
    sync_bot.reply_to(message, "🔄 Triggering remote build architecture on Render...")
    try:
        response = requests.post(DEPLOY_HOOK, timeout=10)
        if response.status_code in [200, 204, 201]:
            sync_bot.send_message(CHAT_ID, "🚀 Deploy command accepted! Render is now compiling your latest code.")
        else:
            sync_bot.send_message(CHAT_ID, f"⚠️ Render server responded with status: {response.status_code}")
    except Exception as e:
        sync_bot.send_message(CHAT_ID, f"❌ Failed to reach Render endpoint: {e}")

# --- INIT AND RUN ---
if __name__ == "__main__":
    print("Initializing state storage schema database layer...")
    init_db()

    t_web = threading.Thread(target=run_web_server)
    t_web.daemon = True
    t_web.start()

    t_strategy = threading.Thread(target=strategy_loop)
    t_strategy.daemon = True
    t_strategy.start()

    print("Background components online. Starting command sync listener...")
    sync_bot.infinity_polling()
