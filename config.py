import os

# --- CONFIGURATION FROM ENVIRONMENT ---
TOKEN = "8686769653:AAFGUPCasmvUo3UFyHtyCljAgtfCbysn-08"

CHAT_ID = "8701685996"
FINNHUB_API_KEY = "D8sh4dpr01qq7apvl2egd8sh4dpr01qq7apvl2f0"
DEPLOY_HOOK = "https://api.render.com/deploy/srv-d8slig6gvqtc738d9rjg?key=oAz0lVAFCyc"

# Comprehensive Watchlist (Exotic, Volatile, and Major Assets)
STRATEGY_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCAD", "EURAUD", "EURNZD", "EURCHF",
    "GBPJPY", "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY", "AUDCAD", "AUDNZD",
    "USDZAR", "USDTRY", "USDINR", "USDMXN", "USDSGD", "USDHKD", "USDCNH"
]

# Standard Strategy Cooldown (5 Minutes)
COOLDOWN_TIME = 300
