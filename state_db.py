import sqlite3
import time

DB_FILE = "bot_state.db"

def init_db():
    """Initializes the database schema with a 5-second concurrency timeout pool."""
    with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alert_cooldowns (
                pair TEXT PRIMARY KEY,
                last_alert_time REAL
            )
        """)
        conn.commit()

def set_cooldown(pair: str):
    """Commits an epoch timestamp to local disk storage, insulated against multi-threaded lockouts."""
    current_time = time.time()
    with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO alert_cooldowns (pair, last_alert_time)
            VALUES (?, ?)
            ON CONFLICT(pair) DO UPDATE SET last_alert_time = excluded.last_alert_time
        """, (pair, current_time))
        conn.commit()

def is_on_cooldown(pair: str, cooldown_duration: int) -> bool:
    """Queries persistent ledger securely, allowing parallel read requests without collision."""
    current_time = time.time()
    with sqlite3.connect(DB_FILE, timeout=5.0) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_alert_time FROM alert_cooldowns WHERE pair = ?", (pair,))
        row = cursor.fetchone()
        
        if row:
            last_alert_time = row[0]
            if current_time - last_alert_time < cooldown_duration:
                return True
        return False
